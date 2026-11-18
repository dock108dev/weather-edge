"""Check whether the hourly index surprise also appears in public trade prints."""

import argparse
from collections import Counter, defaultdict
from datetime import timedelta
import gzip
import json
from pathlib import Path
from statistics import mean, median
import sys

from . import research_model, venue_archive, venue_history, venue_index_market
from .market import decimal, iso, parse_time
from .venue_sources import PublicGET

VERSION = "hourly-index-trade-responses-v2"
EARLIER = "hourly-index-trade-responses-v1-c63012fca44a6d42"
BEFORE = 5
AFTER = (5, 10)
SAME_PRICE = 0.10
TERMS = ("https://docs.kalshi.com/api-reference/market/get-trades",
         "https://docs.kalshi.com/api-reference/historical/get-historical-trades")


def _cohort(corpus):
    positives, controls = [], defaultdict(list)
    for row in corpus["cases"]["test"]:
        gap = abs(row["market"] - row["weather"])
        if gap < venue_index_market.MIN_DIVERGENCE:
            continue
        if abs(row["delta_10_f"]) >= venue_index_market.MIN_SURPRISE:
            positives.append(row)
        else:
            controls[row["city"], row["date"]].append(row)
    pairs = []
    used = set()
    for row in sorted(positives, key=lambda row: (row["date"], row["city"], row["at"], row["market_rule"]["instrument_id"])):
        choices = [example for example in controls.get((row["city"], row["date"]), [])
                   if example["market_rule"]["instrument_id"] not in used and abs(example["market"] - row["market"]) <= SAME_PRICE]
        relative = lambda example: (abs(example["market"] - row["market"]), abs(example["original"]["width"] - row["original"]["width"]),
                                    abs((parse_time(example["at"]) - parse_time(row["at"])).total_seconds()), example["market_rule"]["instrument_id"])
        companion = min(choices, key=relative, default=None)
        if companion:
            used.add(companion["market_rule"]["instrument_id"])
        pairs.append({"surprise": row, "control": companion})
    return pairs


def _bounds(row):
    at = parse_time(row["at"])
    return at - timedelta(minutes=BEFORE), at + timedelta(minutes=max(AFTER))


def _download(data, pairs):
    get = PublicGET(data)
    cutoff = venue_history._cutoffs(data).get("trades_created_ts")
    received, attempts = 0, 0
    for pair in pairs:
        for row in pair.values():
            if row:
                received += venue_history._trades(data, get, row["market_rule"], cutoff=cutoff, pages=4, window=_bounds(row))
                attempts += 1
                if attempts % 50 == 0:
                    print(f"{attempts} bounded trade windows, {received} native prints", flush=True)
    return attempts, received


def _point(stored, instrument):
    raw = json.loads(stored["body"])
    try:
        price, other = decimal(raw["yes_price_dollars"]), decimal(raw["no_price_dollars"])
        quantity = decimal(raw["count_fp"])
        if raw["ticker"] != instrument or not 0 <= price <= 1 or price + other != 1 or quantity <= 0 or raw.get("is_block_trade"):
            return None
    except (KeyError, ValueError):
        return None
    return {"at": stored["at"], "receipt_id": stored["receipt_id"], "yes": str(price), "quantity": str(quantity),
            "side": raw.get("taker_outcome_side") or raw.get("taker_side"), "raw": raw}


def _window(db, case):
    instrument = case["market_rule"]["instrument_id"]
    start, end = _bounds(case)
    rows = db.execute(
        """SELECT at,receipt_id,body FROM history WHERE venue=? AND instrument_id=? AND kind='trade'
        AND julianday(at)>=julianday(?) AND julianday(at)<=julianday(?) ORDER BY at,native_key""",
        (venue_archive.KALSHI, instrument, iso(start), iso(end)),
    )
    expected_scope = f"{instrument}/{iso(start)}/{iso(end)}"
    routes = [dict(row) for row in db.execute(
        "SELECT kind,state,pages,rows,error,updated_at FROM progress WHERE venue=? AND scope=? AND kind LIKE '%-window'",
        (venue_archive.KALSHI, expected_scope),
    )]
    seen, invalid = [], 0
    for row in rows:
        point = _point(row, instrument)
        if point is None:
            invalid += 1
        else:
            seen.append(point)
    return {"case": case, "trades": seen, "status": routes, "excluded_native_prints": invalid}


def gather(data, pairs):
    with venue_archive.connect(data) as db:
        return {"version": VERSION, "pairs": [{label: _window(db, row) if row else None for label, row in pair.items()} for pair in pairs]}


def _stats(case, minutes):
    decision = parse_time(case["case"]["at"])
    cutoff = decision + timedelta(minutes=minutes)
    left, right = [], []
    for trade in case["trades"]:
        stamp = parse_time(trade["at"])
        if stamp < decision:
            left.append(trade)
        elif stamp <= cutoff:
            right.append(trade)
    result = {"before": len(left), "after": len(right), "response": None, "same_taker_side_response": None,
              "nearest_trade_response": None, "from_last_pre_decision": None, "pre_decision_movement": None}
    if not left or not right or not case["status"] or any(route["state"] != "complete" for route in case["status"]):
        return result
    direction = 1 if case["case"]["weather"] > case["case"]["market"] else -1
    move = lambda first, last: direction * (median(float(row["yes"]) for row in last) - median(float(row["yes"]) for row in first))
    result["response"] = move(left, right)
    sides = [(first, last) for side in ("yes", "no")
             if (first := [row for row in left if row["side"] == side]) and (last := [row for row in right if row["side"] == side])]
    if sides:
        result["same_taker_side_response"] = mean(move(first, last) for first, last in sides)
    result["contracts_after"] = str(sum(decimal(row["quantity"]) for row in right))
    result["last_before"] = left[-1]
    result["first_after"] = right[0]
    result["nearest_trade_response"] = direction * (float(right[0]["yes"]) - float(left[-1]["yes"]))
    result["from_last_pre_decision"] = move(left[-1:], right)
    late_before = [trade for trade in left if parse_time(trade["at"]) > decision - timedelta(minutes=1)]
    early_before = [trade for trade in left if parse_time(trade["at"]) <= decision - timedelta(minutes=1)]
    if late_before and early_before:
        result["pre_decision_movement"] = move(early_before, late_before)
    return result


def _move_table(rows, metric):
    responses = [row[metric] for row in rows if row[metric] is not None]
    return {"eligible": len(responses), "moves_toward": sum(value > 0 for value in responses),
            "mean_movement": mean(responses) if responses else None, "median_movement": median(responses) if responses else None}


def _paired_diagnostics(trials):
    results = {}
    for metric in ("response", "same_taker_side_response", "nearest_trade_response", "from_last_pre_decision", "pre_decision_movement"):
        by_day = defaultdict(list)
        paired = []
        for row in trials:
            surprise, control = row["surprise"], row["control"]
            if control and surprise[metric] is not None and control[metric] is not None:
                movement = surprise[metric] - control[metric]
                paired.append(movement)
                by_day[surprise["evidence"]["case"]["date"]].append(movement)
        trimmed = sorted(paired, key=abs)[:-max(1, round(0.05 * len(paired)))]
        results[metric] = {"pairs": len(paired), "calendar_dates": len(by_day),
                           "mean_paired_movement_difference": mean(paired) if paired else None,
                           "median_paired_movement_difference": median(paired) if paired else None,
                           "average_equal_calendar_date": mean(mean(pts) for pts in by_day.values()) if by_day else None,
                           "average_excluding_largest_5_pct_differences": mean(trimmed) if trimmed else None}
    return results


def evaluate(data, corpus):
    frozen, digest = research_model.freeze_inputs(data, corpus)
    results, cases, stats = {}, [], []
    for horizon in AFTER:
        trials = [{label: ({"evidence": row, **_stats(row, horizon)} if row else None) for label, row in pair.items()}
                  for pair in corpus["pairs"]]
        results[str(horizon)] = trials
        for label in ("surprise", "control"):
            rows = [row[label] for row in trials if row[label]]
            lookup = Counter("no completed public trade window" if not row["evidence"]["status"] or any(
                route["state"] != "complete" for route in row["evidence"]["status"]) else
                "reported zero trades" if not row["before"] and not row["after"] else
                "no earlier print" if not row["before"] else "no later print" if not row["after"] else
                "observed before and after" for row in rows)
            observed_days = {row["evidence"]["case"]["date"] for row in rows if row["response"] is not None}
            stats.append({"group": f"{label.title()} after {horizon} minutes", "cases": len(rows), "calendar_dates": len(observed_days),
                          "counts": dict(lookup), "all_takers": _move_table(rows, "response"),
                          "same_taker_side": _move_table(rows, "same_taker_side_response"),
                          "nearest_trades": _move_table(rows, "nearest_trade_response"),
                          "from_last_pre_decision": _move_table(rows, "from_last_pre_decision"),
                          "pre_decision": _move_table(rows, "pre_decision_movement")})
        paired = [(row["surprise"]["response"], row["control"]["response"]) for row in trials
                  if row["control"] and row["surprise"]["response"] is not None and row["control"]["response"] is not None]
        stats.append({"group": f"Matched surprise and control after {horizon} minutes", "paired_cases": len(paired),
                      "mean_paired_movement_difference": mean(a - b for a, b in paired) if paired else None,
                      "clock_and_grouping_checks": _paired_diagnostics(trials)})
    observed = [row["surprise"] for row in results[str(max(AFTER))] if row["surprise"]["response"] is not None]
    for row in sorted(observed, key=lambda row: abs(row["response"]), reverse=True)[:5]:
        contract = row["evidence"]["case"]
        instrument = contract["market_rule"]["instrument_id"]
        cases.append({"title": f"{contract['city']} · {contract['market_rule']['label']}", "instrument_id": instrument,
                      "venue": venue_archive.KALSHI, "date": contract["date"], "at": contract["at"],
                      "detail": f"Window median move toward the weather estimate was {row['response']*100:+.1f}¢. "
                                f"The last print before the clock was {float(row['last_before']['yes'])*100:.1f}¢ "
                                f"and the first print after was {float(row['first_after']['yes'])*100:.1f}¢.", "data": row})
    result = {"version": VERSION, "title": "Hourly weather surprises and public trade prints",
              "hypothesis": "A large newly usable index change is followed by more price movement toward the weather estimate than comparable non-surprise clocks.",
              "target": "Median native YES transaction price around the fixed index decision", "venues": ["Kalshi"], "supersedes": EARLIER,
              "date_window": {"held dates": sorted({row["surprise"]["case"]["date"] for row in corpus["pairs"]})},
              "coverage": {"fixed surprise samples": len(corpus["pairs"]), "control samples": sum(bool(row["control"]) for row in corpus["pairs"]),
                           "transactions": sum(len(row["trades"]) for pair in corpus["pairs"] for row in pair.values() if row)},
              "metrics": stats, "cases": cases,
              "limits": ["These are prints for other participants. Historical available depth and the ability to obtain the print price remain unverified.",
                         "The index feature clocks are the filing schedule used by the earlier weather test. Original local receipt follows in a separate prospective run.",
                         "A wide pre-decision window can include a price move that already occurred. Exact immediately preceding and subsequent prints test that sensitivity.",
                         "Shared city-hour and calendar-day outcomes limit independent support. The control comparison is descriptive.",
                         "Block and malformed prints are excluded. A completed empty query and missing or incomplete requests are reported separately."],
              "provenance": {"frozen_input": str(frozen.relative_to(data)), "frozen_input_sha256": digest, "official_endpoints": TERMS,
                             "source_sha256": research_model.run_sources(sys.modules[__name__], venue_index_market, venue_history)}}
    return research_model.write_record(data, result, results)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--download", action="store_true")
    args = parser.parse_args(argv)
    corpus = json.loads(gzip.decompress(args.input.read_bytes()))
    if "pairs" in corpus:
        corpus["version"] = VERSION
    elif corpus["version"] != VERSION:
        pairs = _cohort(corpus)
        if args.download:
            windows, trades = _download(args.data, pairs)
            print(f"{windows} bounded windows returned {trades} native prints", flush=True)
        corpus = gather(args.data, pairs)
    print(evaluate(args.data, corpus), flush=True)


if __name__ == "__main__":
    main()
