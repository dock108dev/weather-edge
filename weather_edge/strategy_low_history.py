"""Audit weather edges against actual Polymarket US daily-low display paths."""

import argparse
from bisect import bisect_left
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import gzip
from hashlib import sha256
import json
from pathlib import Path
import statistics

from .market import decimal, fee_coefficient, iso, parse_time, taker_fee
from .strategy_runs import DEFAULT_DATA, POLYMARKET, digest, json_bytes, write_new


ENGINE = "daily-low-price-only-strategies-v1"
FOLLOW_UP = timedelta(minutes=15)
DISPLAY_MARGIN = timedelta(minutes=1)
EDGE = Decimal("0.02")


def price_traces(events):
    traces = {}
    for event in events:
        for bucket in event["buckets"]:
            history = sorted(((parse_time(row["available_at"]), row) for row in bucket.get("prices", [])), key=lambda item: item[0])
            traces[bucket["id"]] = ([at for at, _ in history], history)
    return traces


def display_cost(price, at):
    raw = decimal(price)
    return raw + taker_fee([(raw, Decimal("1"))], fee_coefficient(at))


def offer(display, side):
    if not display or display.get("yes") is None or display.get("no") is None:
        return None
    yes, no = decimal(display["yes"]), decimal(display["no"])
    if not 0 < yes < 1 or not 0 < no < 1 or yes + no < 1:
        return None
    return yes if side == "YES" else no


def candidate(row, method):
    at = parse_time(row["at"])
    yes = decimal(row["probabilities"][method])
    for side, chance in (("YES", yes), ("NO", 1 - yes)):
        price = offer(row["market_display"], side)
        if price is None:
            continue
        cost = display_cost(price, at)
        if chance - cost >= EDGE:
            yield {
                "side": side, "probability": float(chance), "display_price": float(price),
                "fee": float(cost - price), "all_in": float(cost),
                "forecast_ev_per_contract": float(chance - cost),
            }


def delayed_check(row, signal, trace):
    due = parse_time(row["at"]) + FOLLOW_UP
    times, history = trace
    index = bisect_left(times, due)
    if index == len(times) or times[index] > due + DISPLAY_MARGIN:
        return {"result": "No timely later display"}
    checked_at, display = history[index]
    value = offer(display, signal["side"])
    if value is None:
        return {"result": "No coherent later display"}
    cost = display_cost(value, checked_at)
    edge = decimal(signal["probability"]) - cost
    return {
        "result": "Displayed edge persisted" if edge >= EDGE else "Displayed price removed edge",
        "at": iso(checked_at), "display_price": float(value), "all_in": float(cost),
        "same_forecast_ev_per_contract": float(edge),
        "price_move": float(value - decimal(signal["display_price"])),
    }


def read_signals(rows, method, events):
    traces = price_traces(events)
    candidates = []
    for row in rows:
        at = row["at"]
        instrument = row["instrument_id"]
        for signal in candidate(row, method):
            outcome = float(row["payout_yes"]) if signal["side"] == "YES" else 1 - float(row["payout_yes"])
            candidates.append({
                "method": method, "venue": POLYMARKET, "date": row["date"], "station": row["station"], "instrument_id": instrument,
                "at": at, "bucket": row["bucket"], "signal": signal, "payout_for_suggested_side": outcome,
                "forecast": row["forecast"], "official_report": row["official_report"],
                "recheck": delayed_check(row, signal, traces[instrument]),
            })
    return candidates


def summarize(signals, method, rows):
    groups = defaultdict(list)
    for row in signals:
        groups[row["date"], row["station"]].append(row)
    rechecked = [s for s in signals if s["recheck"].get("at")]
    return {
        "strategy": method, "matched_decision_rows": len(rows),
        "case_dates": len({row["date"] for row in rows}),
        "case_station_days": len({(row["date"], row["station"]) for row in rows}),
        "signal_rows": len(signals), "signal_dates": len({s["date"] for s in signals}), "signal_station_days": len(groups),
        "one_payout_sides": sum(s["payout_for_suggested_side"] == 1 for s in signals),
        "station_day_weighted_one_payout_rate": statistics.mean(
            statistics.mean(s["payout_for_suggested_side"] for s in values) for values in groups.values()
        ) if groups else None,
        "later_display_available": len(rechecked), "missing_recheck": len(signals) - len(rechecked),
        "later_price_removed_edge": sum(s["recheck"]["result"] == "Displayed price removed edge" for s in rechecked),
        "later_edge_persisted": sum(s["recheck"]["result"] == "Displayed edge persisted" for s in rechecked),
        "median_display_price_move_cents": statistics.median(s["recheck"]["price_move"] * 100 for s in rechecked) if rechecked else None,
        "simulated_fills": 0,
    }


def case_for(row):
    signal, recheck = row["signal"], row["recheck"]
    return {**row, "title": f"{row['station']} {row['bucket']['label']} {signal['side']} · low-temperature display",
            "detail": f"Displayed {signal['display_price']:.2f}, model {signal['probability']:.1%}, "
                      f"{recheck['result'].lower()}, settled side paid ${row['payout_for_suggested_side']:.2f}."}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", type=Path, default=DEFAULT_DATA)
    ap.add_argument("replay", type=Path, help="Daily-low replay research JSON containing the frozen source path.")
    args = ap.parse_args()
    data = args.data
    replay = json.loads(args.replay.read_text())
    record = (data / replay["provenance"]["replay_details_path"]).read_bytes()
    source_hash = sha256(record).hexdigest()
    details = json.loads(gzip.decompress(record))
    test_first, test_last = replay["date_window"]["test"]
    rows = [r for r in details["decisions"] if test_first <= r["date"] <= test_last and r.get("market_midpoint") is not None]
    methods = tuple(dict.fromkeys(("noaa", replay["provenance"]["pretest_selected"])))
    strategy_signals = {method: read_signals(rows, method, details["events"]) for method in methods}
    settings = {
        "engine": ENGINE, "replay": replay["id"], "replay_evidence_sha256": source_hash,
        "predictors": methods, "recheck_delay_seconds": FOLLOW_UP.total_seconds(),
        "maximum_recheck_lag_seconds": DISPLAY_MARGIN.total_seconds(),
        "min_net_ev_per_contract": str(EDGE), "payout_scope": "actual exchange-reported Yes and No",
    }
    ident = "low-display-" + digest(settings)[:20]
    comparisons = []
    for method, signals in strategy_signals.items():
        for outcome in ("Displayed price removed edge", "Displayed edge persisted", "No timely later display"):
            possible = [item for item in signals if item["recheck"]["result"] == outcome]
            if possible:
                comparisons.append(case_for(max(possible, key=lambda s: s["signal"]["forecast_ev_per_contract"])))
    snapshot = data / "research" / "evidence" / "low-display" / f"{ident}.json.gz"
    write_new(snapshot, gzip.compress(json_bytes({"settings": settings, "signals": strategy_signals}), mtime=0))
    result = {
        "id": ident,
        "title": "Would daily-low model edges persist in display prices?",
        "hypothesis": "The earlier-selected daily-low model identifies after-fee indicative edges that persist in the later display record.",
        "venues": [POLYMARKET], "created_at": iso(datetime.now(timezone.utc)), "target": "Daily low temperature bucket",
        "date_window": {"from": test_first, "through": test_last},
        "metrics": [summarize([], "no trade", rows)] + [summarize(signals, method, rows) for method, signals in strategy_signals.items()],
        "cases": comparisons,
        "limits": [
            "Display data records price and carries no depth quantity. Every row leaves cash untouched.",
            "The later comparison locks the signal-time forecast and counts a missing recheck independently from adverse price movement.",
            "Multiple snapshots share a station-day payout. The report counts distinct station-days and display rows.",
        ],
        "provenance": {**settings, "frozen_signals": str(snapshot.relative_to(data))},
    }
    path = data / "research" / "strategies" / f"{ident}.json"
    write_new(path, json_bytes(result) + b"\n")
    print(json.dumps({"path": str(path), "metrics": result["metrics"]}, indent=2))


if __name__ == "__main__":
    main()
