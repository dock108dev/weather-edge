"""Measure open paper exposure over exact weather contract outcomes."""

import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import gzip
from hashlib import sha256
import json
from pathlib import Path

from . import expansion_metar, strategy_metar_high
from .history import _check_bucket_partition
from .market import _ladder, decimal, fee_coefficient, iso, parse_time, taker_fee
from .strategy_rules import book_status
from .strategy_runs import DEFAULT_DATA, POLYMARKET, digest, json_bytes, write_new


ENGINE = "paper-native-outcome-exposure-v4"
ZERO = Decimal(0)


def payoff(positions, winner):
    return sum((decimal(p["quantity"]) for p in positions if (p["bucket_id"] == winner) == (p["side"] == "YES")), ZERO)


def exclusive_partition(event):
    if not event or event.get("family", "daily-high") not in {"daily-high", "daily-low"}:
        return False
    try:
        _check_bucket_partition(event["buckets"])
    except ValueError:
        return False
    return all(any(source in (bucket.get("rules") or "").lower() for source in ("nws", "national weather service"))
               for bucket in event["buckets"])


def _binary_bounds(positions):
    by_bucket = defaultdict(list)
    for pos in positions:
        by_bucket[pos["bucket_id"]].append(pos)
    extrema = [sorted((payoff(group, ident), payoff(group, None))) for ident, group in by_bucket.items()]
    return sum((d[0] for d in extrema), ZERO), sum((d[1] for d in extrema), ZERO)


def event_exposure(positions, event):
    collateral = sum((decimal(p["max_loss"]) for p in positions), ZERO)
    winner_payouts = {}
    if exclusive_partition(event):
        winner_payouts = {b["id"]: payoff(positions, b["id"]) for b in event["buckets"]}
        floor, cap = min(winner_payouts.values()), max(winner_payouts.values())
    else:
        floor, cap = _binary_bounds(positions)
    return {"collateral": collateral, "minimum_binary_payout": floor, "maximum_binary_payout": cap,
            "worst_net_at_ordinary_payout": floor - collateral, "best_net_at_ordinary_payout": cap - collateral,
            "exact_native_partition": bool(winner_payouts), "payout_by_native_winner": winner_payouts,
            "absolute_gross_expected_ev": sum((abs(decimal(p["ev_dollars"])) for p in positions), ZERO)}


def observed_bound(event, cutoff):
    if not event or not event.get("climate_starts_at") or not event.get("climate_ends_at"):
        return None
    if event.get("family", "daily-high") not in {"daily-high", "daily-low"}:
        return None
    start, end = (parse_time(event[key]) for key in ("climate_starts_at", "climate_ends_at"))
    high = event.get("family", "daily-high") == "daily-high"
    exact = [row for row in event.get("observations", []) if row.get("source") in {"NWS", "Aviation Weather Center"}
             and not row.get("timing_proxy") and start <= parse_time(row["at"]) < end]
    decode = strategy_metar_high.metar_high_evidence if high else expansion_metar.metar_low_evidence
    choices = [w for row in exact for w in decode(row, event["station"], start, cutoff)]
    field = "lower_f" if high else "upper_f"
    extreme = max if high else min
    witness = extreme(choices, key=lambda row: row[field], default=None)
    return {"as_of": iso(cutoff), "kind": "observed daily-high floor" if high else "observed daily-low ceiling",
            "measurement_f": witness[field], "evidence": witness} if witness else None


def contradictions(event, positions, bound, *, entry_bounds=None):
    if bound is None:
        return []
    bands = {b["id"]: (b["lower_f"], b["upper_f"]) for b in event["buckets"]}
    high = event.get("family", "daily-high") == "daily-high"
    results = []
    for position in positions:
        lower, upper = bands[position["bucket_id"]]
        def crosses(witness):
            if witness is None:
                return False
            value = witness["measurement_f"]
            return upper is not None and value > upper if high else lower is not None and value < lower

        if crosses(bound):
            before = None if entry_bounds is None else entry_bounds.get(position["opened_at"])
            known_first = (crosses(before) if entry_bounds is not None else
                           parse_time(bound["evidence"]["available_at"]) <= parse_time(position["opened_at"]))
            results.append({"instrument_id": position["bucket_id"], "side": position["side"], "lower_f": lower, "upper_f": upper,
                            "measurement_known_before_open": known_first, "entry_clock_witness": before if known_first else None,
                            "held_yes_contradicted_by_measurement": position["side"] == "YES"})
    return results


def _entry_context(event, position):
    if event is None:
        return {}
    bucket = next((item for item in event["buckets"] if item["id"] == position["bucket_id"]), None)
    if bucket is None:
        return {}
    when = parse_time(position["quote_at"])
    book = next((row for row in bucket["books"] if parse_time(row["at"]) == when), None)
    if not book or not book.get("bids") or not book.get("offers"):
        return {"execution_book": book, "midpoint_side_proxy": None}
    bid = max(decimal(level[0]) for level in book["bids"])
    ask = min(decimal(level[0]) for level in book["offers"])
    if not 0 <= bid < ask <= 1 or (bid == 0 and ask == 1):
        return {"execution_book": book, "midpoint_side_proxy": None}
    yes = (bid + ask) / 2
    proxy = yes if position["side"] == "YES" else 1 - yes
    return {"execution_book": book, "midpoint_side_proxy": proxy,
            "forecast_vs_midpoint_side_probability_difference": decimal(position["probability"]) - proxy,
            "midpoint_proxy_net_at_observed_quantity": decimal(position["quantity"]) * proxy - decimal(position["max_loss"])}


def exit_context(event, position, at):
    if event is None or at >= parse_time(event["ends_at"]):
        return {"full_size_priced": False, "reason": "No open native event at the report cutoff"}
    bucket = next((item for item in event["buckets"] if item["id"] == position["bucket_id"]), None)
    if bucket is None:
        return {"full_size_priced": False, "reason": "Exact native bucket unavailable"}
    latest = next((row for row in reversed(bucket["books"])
                   if timedelta(0) <= at - parse_time(row["at"]) <= timedelta(seconds=30)
                   and not book_status(row, None, parse_time(row["at"]), "origin-confirmed")), None)
    if latest is None:
        return {"full_size_priced": False, "reason": "No origin-confirmed ladder in the last 30 seconds"}
    remaining = decimal(position["quantity"])
    other = "NO" if position["side"] == "YES" else "YES"
    levels = []
    for opposite_price, available in _ladder(latest, other):
        budgeted = min(available, remaining)
        levels.append((1 - opposite_price, budgeted))
        remaining -= budgeted
        if remaining == ZERO:
            break
    priced = decimal(position["quantity"]) - remaining
    gross = sum((price * size for price, size in levels), ZERO)
    fee = taker_fee(levels, fee_coefficient(at))
    return {"full_size_priced": remaining == ZERO, "receipt_at": latest["at"], "exchange_at": latest.get("source_at"),
            "visible_exit_contracts": priced, "visible_exit_gross": gross, "exit_fee": fee,
            "full_net_proceeds": gross - fee if remaining == ZERO else None,
            "reason": None if remaining == ZERO else "Visible opposing depth below the held quantity"}


def summarize(record, tape):
    indexed = {(event.get("venue", POLYMARKET), event["id"]): event for event in tape["events"]}
    full, metrics, examples, weather, exits, entry_explanations = {}, [], [], {}, {}, set()
    cutoff = {"development": parse_time(record["date_window"]["split"]) - timedelta(microseconds=1),
              "forward": parse_time(tape["captured_at"])}
    for key, trial in record["records"].items():
        phase, strategy, freshness = key.split("/")
        grouped = defaultdict(list)
        for position in trial["positions"]:
            if not position["settled"]:
                grouped[position["venue"], position["event_id"]].append(position)
        risk, station_days = [], defaultdict(lambda: {"collateral": ZERO, "minimum_payout": ZERO,
                                                        "equivalent_events": 0, "forecast_net_ev": ZERO})
        for (venue, event_id), positions in grouped.items():
            native = indexed.get((venue, event_id))
            out = event_exposure(positions, native)
            cache = phase, venue, event_id
            if cache not in weather:
                weather[cache] = observed_bound(native, cutoff[phase])
            known = weather[cache]
            clocks = {pos["opened_at"] for pos in positions}
            for clock in clocks:
                key_at = venue, event_id, clock
                if key_at not in weather:
                    weather[key_at] = observed_bound(native, parse_time(clock))
            opened = {clock: weather[venue, event_id, clock] for clock in clocks}
            weather_conflicts = contradictions(native, positions, known, entry_bounds=opened)
            priced = []
            for pos in positions:
                exit_key = phase, venue, pos["bucket_id"], pos["side"], pos["quantity"]
                if exit_key not in exits:
                    exits[exit_key] = exit_context(native, pos, cutoff[phase])
                priced.append({**pos, **_entry_context(native, pos), "cutoff_exit_context": exits[exit_key]})
            site = tuple(positions[0]["group"])
            station_days[site]["collateral"] += out["collateral"]
            station_days[site]["minimum_payout"] += out["minimum_binary_payout"]
            station_days[site]["equivalent_events"] += bool(out["exact_native_partition"])
            station_days[site]["forecast_net_ev"] += sum((decimal(pos["ev_dollars"]) for pos in positions), ZERO)
            risk.append({**out, "venue": venue, "event_id": event_id, "station": site[0], "date": site[1],
                         "positions": priced, "station_weather_bound": known, "measured_crossings": weather_conflicts})
        maximum = max((row["collateral"] for row in station_days.values()), default=ZERO)
        collateral = sum((row["collateral"] for row in risk), ZERO)
        min_pay = sum((row["minimum_binary_payout"] for row in risk), ZERO)
        max_pay = sum((row["maximum_binary_payout"] for row in risk), ZERO)
        forecast_total = sum((row["forecast_net_ev"] for row in station_days.values()), ZERO)
        forecast_largest = max((row["forecast_net_ev"] for row in station_days.values()), default=ZERO)
        open_positions = [pos for row in risk for pos in row["positions"]]
        fully_quoted_exit = sum(pos["cutoff_exit_context"]["full_size_priced"] for pos in open_positions)
        exit_value = sum((pos["cutoff_exit_context"]["full_net_proceeds"] for pos in open_positions), ZERO) - collateral if fully_quoted_exit == len(open_positions) else None
        metrics.append({"window": phase, "strategy": strategy, "freshness": freshness, "open_positions": sum(len(group) for group in grouped.values()),
                        "station_days_with_positions": len(station_days), "events_with_positions": len(grouped),
                        "cash_committed": collateral, "largest_station_day_collateral": maximum,
                        "largest_station_day_collateral_share": maximum / collateral if collateral else ZERO,
                        "minimum_binary_payout": min_pay, "maximum_binary_payout": max_pay,
                        "worst_net_from_open_positions": min_pay - collateral,
                        "best_net_from_open_positions": max_pay - collateral,
                        "projected_net_ev_by_entry_forecasts": forecast_total,
                        "largest_station_day_projected_net_ev": forecast_largest,
                        "largest_station_day_share_of_projected_net_ev": forecast_largest / forecast_total if forecast_total > 0 else ZERO,
                        "projected_net_ev_excluding_largest_station_day": forecast_total - forecast_largest,
                        "fully_priceable_exit_positions_at_cutoff": fully_quoted_exit,
                        "projected_net_from_complete_visible_exits_at_cutoff": exit_value,
                        "yes_positions_where_measured_extreme_crossed_bucket": sum(row["held_yes_contradicted_by_measurement"]
                            for event in risk for row in event["measured_crossings"]),
                        "yes_positions_where_crossing_known_before_open": sum(row["held_yes_contradicted_by_measurement"] and row["measurement_known_before_open"]
                            for event in risk for row in event["measured_crossings"]),
                        "realized_pnl_at_cutoff": trial["metrics"]["realized_pnl"], "profit_mark_basis": "actual archived settlements only"})
        full[key] = {"events": risk, "station_day_exposure": {" / ".join(k): v for k, v in station_days.items()}}
        if phase == "forward" and forecast_total > 0:
            pos, owner = max(((pos, event) for event in risk for pos in event["positions"]), key=lambda row: row[0]["ev_dollars"])
            ident = pos["bucket_id"], pos["side"]
            if ident not in entry_explanations:
                entry_explanations.add(ident)
                estimate = decimal(pos["ev_dollars"])
                examples.append({"station": owner["station"], "date": owner["date"], "venue": pos["venue"],
                                 "event_id": pos["event_id"], "instrument_id": pos["bucket_id"], "at": pos["quote_at"], "strategy": strategy,
                                 "title": f'{owner["station"]} · largest forecast-dollar position',
                                 "detail": f'This ${pos["max_loss"]:.2f} paper position accounted for {estimate / forecast_total:.1%} of projected net dollars. It still awaits its exact venue payout.',
                                 "data": pos})
        for event in risk:
            if phase != "forward":
                continue
            base = {"station": event["station"], "date": event["date"], "venue": event["venue"],
                    "event_id": event["event_id"], "strategy": strategy, "window": phase, "data": event}
            for crossed in event["measured_crossings"]:
                if not crossed["held_yes_contradicted_by_measurement"]:
                    continue
                cause = crossed["entry_clock_witness"] or event["station_weather_bound"]
                examples.append({**base, "instrument_id": crossed["instrument_id"], "at": cause["evidence"]["available_at"],
                                 "title": f"{event['station']} observed extreme outside a held YES bucket",
                                 "detail": f"Exact station measurement {cause['measurement_f']}°F, from an archived raw report. The position still awaits a verified payout."})
            if event["minimum_binary_payout"]:
                original = event["positions"][-1]
                examples.append({**base, "instrument_id": original["bucket_id"], "at": original["quote_at"],
                                 "title": f"{event['station']} overlapping simulated exposure",
                                 "detail": f"Conditional ordinary payout at least ${event['minimum_binary_payout']:.2f} on ${event['collateral']:.2f} committed in the event, with all positions still open at the cutoff."})
    return metrics, examples, full


def report(data, source):
    raw = source.read_bytes()
    saved = json.loads(raw)
    frozen_path = data / saved["provenance"]["frozen_inputs"]
    evidence = json.loads(gzip.decompress(frozen_path.read_bytes()))
    tape_digest = digest(evidence)
    if tape_digest != saved["provenance"]["tape_sha256"]:
        raise ValueError("Saved run and frozen inputs disagree")
    metrics, cases, risk = summarize(saved, evidence["capture"])

    def encode(value):
        return json.loads(json.dumps(value, default=float, allow_nan=False, sort_keys=True))

    inputs = {"engine": ENGINE, "origin_run": saved["id"], "frozen_tape_sha256": tape_digest,
              "run_sha256": sha256(raw).hexdigest()}
    ident = "exposure-" + digest(inputs)[:20]
    frozen = data / "research/evidence/strategy-exposure" / f"{ident}.json.gz"
    write_new(frozen, gzip.compress(json_bytes(encode({"input": inputs, "risk": risk})), mtime=0))
    report = {"id": ident, "title": "What is the net open exposure of the paper portfolios?",
              "hypothesis": "Opposite or overlapping positions at the same weather event can constrain the worst ordinary payout.",
              "target": "Exact binary payout scenarios on still-open positions, with one chronological cash balance per strategy.",
              "created_at": iso(datetime.now(timezone.utc)), "venues": saved["venues"], "date_window": saved["date_window"],
              "metrics": encode(metrics), "cases": encode(cases),
              "limits": ["Payout bounds assume ordinary binary resolution under the retained native contract terms. Voids and fee refunds are absent.",
                         "Different events are bounded independently. A shared climate process can further limit their joint outcome combinations.",
                         "A station weather floor or ceiling is scientific evidence at its recorded receipt. The venue's official payout still governs cash.",
                         "Prices reflect simulated recorded entries. Collateral and scenario bounds remain unrealized until verified payout receipts.",
                         "Forecast-dollar concentration uses paper entry probabilities. Any contemporaneous venue midpoint is a display proxy.",
                         "Cutoff liquidation uses actual origin-qualified opposing depth no more than 30 seconds old. Whole-portfolio liquidation value is unavailable when any exit lacks held size.",
                         "Opposite entries can occur at different times. The record does not imply an atomic two-leg trade."],
              "provenance": {**inputs, "replay_details_path": str(frozen.relative_to(data)),
                             "replay_details_sha256": sha256(frozen.read_bytes()).hexdigest()}}
    path = data / "research/strategies" / f"{ident}.json"
    write_new(path, json_bytes(report) + b"\n")
    return path, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("run", type=Path)
    args = parser.parse_args()
    path, result = report(args.data, args.run)
    print(json.dumps({"path": str(path), "metrics": result["metrics"], "cases": result["cases"]}, indent=2))


if __name__ == "__main__":
    main()
