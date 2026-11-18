"""Measure price, contract size, and cash on identical origin-qualified signals."""

import argparse
from bisect import bisect_left
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import gzip
import json
from pathlib import Path
import statistics

from . import study
from .market import _price, decimal, iso, parse_time
from .paper_runs import _fee_at
from .strategy_rules import FRESHNESS, StrategyRule, book_status
from .strategy_runs import (
    DEFAULT_DATA, FIXED, POLYMARKET, RECHECK_SECONDS, Predictor, digest, json_bytes, run_trial, slice_tape, write_new,
)


ENGINE = "matched-book-size-and-age-v3"
DELAYS = (5, 15, 30, 45, 60)
CAPS = (10, 25)


def clocks(book):
    elapsed = (parse_time(book["at"]) - parse_time(book["source_at"])).total_seconds()
    return {
        "cdn_status": book.get("http_cache", {}).get("cf-cache-status") or "unreported",
        "http_age": book.get("cache_age_seconds"),
        "seconds_from_exchange_clock": elapsed,
        "exchange_age_band": "0–30s" if elapsed <= 30 else "30–120s" if elapsed <= 120 else "over 120s",
    }


def pricing(event, bucket, side, book, at, size, info, rules, fees, intended=None):
    quote = _price(event, bucket, side, book, info, at, decimal(size), 0,
                   max_quantity=decimal(intended) if intended else None, fee_at=fees, rules=rules)
    return {
        "probability": quote["probability"], "uncertain": quote["uncertain"], "size": quote.get("quantity"),
        "average_entry": quote["entry_price"], "fee": quote.get("fee"), "all_in": quote["max_loss"],
        "gross_expected": quote["quantity"] * quote["probability"]
        if quote.get("quantity") and quote["probability"] is not None else None,
        "estimated_after_fee": quote["ev_dollars"], "status": quote["status"], "levels": quote.get("fills"),
    }


def recheck_cause(initial, later, floor, *, compare_swaps):
    """Toggle book and forecast inputs independently before classifying a lost edge."""
    if initial["estimated_after_fee"] is None or initial["estimated_after_fee"] < floor:
        return {"category": "signal cost cap did not qualify"}
    if not later["size"]:
        return {"category": "displayed liquidity gap"}
    if later["uncertain"] or later["probability"] is None:
        return {"category": "forecast support gap"}
    if later["estimated_after_fee"] >= floor:
        return {"category": "edge persisted", "size_changed": later["size"] != initial["size"]}
    new_book, new_weather = compare_swaps()
    book_ev, weather_ev = new_book["estimated_after_fee"], new_weather["estimated_after_fee"]
    book_kills = book_ev is None or book_ev < floor
    weather_kills = weather_ev is None or weather_ev < floor
    if book_kills and weather_kills:
        category = "either change removes edge"
    elif book_kills:
        category = "updated book alone removes edge"
    elif weather_kills:
        category = "updated forecast alone removes edge"
    else:
        category = "combined changes remove edge"
    return {"category": category, "size_changed": later["size"] != initial["size"],
            "signal_probability_at_delayed_book_net_dollars": book_ev,
            "delayed_probability_at_signal_book_net_dollars": weather_ev,
            "signal_probability_at_delayed_book_quantity": new_book["size"],
            "delayed_probability_at_signal_book_quantity": new_weather["size"]}


def match_study(tape, artifact, split, fees):
    phase_tapes = {
        "development": slice_tape(tape, parse_time(tape["started_at"]), split - timedelta(microseconds=1)),
        "forward": slice_tape(tape, split, parse_time(tape["captured_at"])),
    }
    model = Predictor(artifact)
    predict = model("two-sided")
    records = {}
    for window, capture in phase_tapes.items():
        _, baseline = run_trial(capture, model, "two-sided", FIXED, "origin-confirmed", fee_at=fees)
        events = {(event["id"], bucket["id"]): (event, bucket, [parse_time(b["at"]) for b in bucket["books"]])
                  for event in capture["events"] for bucket in event["buckets"]}
        by_variant = defaultdict(list)
        pending = Counter()
        evaluated = {}
        for event_choice in baseline["attempts"]:
            source_at = parse_time(event_choice["signal_at"])
            venue = event_choice.get("venue", POLYMARKET)
            ident, side = event_choice["bucket_id"], event_choice["side"]
            event, bucket, book_times = events[event_choice["event_id"], ident]
            source_index = bisect_left(book_times, source_at)
            book = bucket["books"][source_index]
            clock = clocks(book)
            signal_weather = predict(event, source_at).get(ident)
            signal_rule = StrategyRule("two-sided", "origin-confirmed", FIXED.minimum_dollars)
            raw_quotes = {cap: pricing(event, bucket, side, book, source_at, cap, signal_weather, signal_rule, fees)
                          for cap in CAPS}
            for delay in DELAYS:
                target = source_at + timedelta(seconds=delay)
                end = target + timedelta(seconds=RECHECK_SECONDS)
                earliest = bisect_left(book_times, target, lo=source_index + 1)
                expiry = parse_time(event["ends_at"])
                for policy in FRESHNESS:
                    found = None
                    for index in range(earliest, len(book_times)):
                        checked = book_times[index]
                        if checked > end or checked >= expiry:
                            break
                        sample = bucket["books"][index]
                        if book_status(sample, side, checked, policy) is None:
                            found = checked, sample
                            break
                    if found is None:
                        if parse_time(capture["captured_at"]) < min(end, expiry):
                            pending[policy, delay] += 1
                        continue
                    checked, sample = found
                    follow_clock = clocks(sample)
                    later_weather = predict(event, checked).get(ident)
                    for cap, initial in raw_quotes.items():
                        if not initial["size"]:
                            continue
                        rules = StrategyRule("two-sided", policy, FIXED.minimum_dollars)
                        key = ident, side, checked, cap, initial["size"]
                        if key not in evaluated:
                            evaluated[key] = pricing(event, bucket, side, sample, checked, cap, later_weather, rules, fees,
                                                     initial["size"])
                        later = evaluated[key]

                        def compare_swaps():
                            # Fees follow the specific receipt being priced. Both
                            # comparisons keep the stake and original size ceiling.
                            delayed_book = pricing(event, bucket, side, sample, checked, cap, signal_weather, rules, fees,
                                                   initial["size"])
                            delayed_forecast = pricing(event, bucket, side, book, source_at, cap, later_weather, signal_rule, fees,
                                                       initial["size"])
                            return delayed_book, delayed_forecast

                        qty = later["size"]
                        same_signal_expected = initial["probability"] * qty if qty else None
                        cohort_key = (event["station"], event["date"], ident, side, iso(source_at))
                        by_variant[policy, delay, cap].append({
                            "station": event["station"], "date": event["date"], "venue": venue,
                            "instrument_id": ident, "at": iso(source_at), "side": side,
                            "signal": initial, "recheck": later,
                            "clock": clock, "later_clock": follow_clock,
                            "actual_lag_seconds": (checked - source_at).total_seconds(),
                            "recheck_at": iso(checked), "signal_model_gross_at_recheck": same_signal_expected,
                            "recheck_cause": recheck_cause(initial, later, FIXED.minimum_dollars, compare_swaps=compare_swaps),
                            "settlement_known": bool(bucket.get("settlement") and
                                                     parse_time(bucket["settlement"]["available_at"]) <= parse_time(tape["captured_at"])),
                            "cohort_key": list(cohort_key),
                        })
        records[window] = {"signals": len(baseline["attempts"]), "variants": by_variant, "pending": pending}
    return records


def summarize(records):
    result, raw = [], {}
    for window, by_phase in records.items():
        for delay in DELAYS:
            for cap in CAPS:
                sets = {policy: {tuple(r["cohort_key"]): r for r in by_phase["variants"][policy, delay, cap]}
                        for policy in FRESHNESS}
                common = sets["legacy"].keys() & sets["origin-confirmed"].keys()
                for policy, selected in sets.items():
                    raw[f"{window}/{policy}/{delay}/{cap}"] = list(selected.values())
                    for cohort, matching in (
                        ("any recheck", list(selected.values())),
                        ("both policies rechecked", [selected[key] for key in sorted(common)]),
                    ):
                        for receipt_ages in ("all", "0–30s", "30–120s", "over 120s"):
                            ages = [r for r in matching if receipt_ages == "all" or r["clock"]["exchange_age_band"] == receipt_ages]
                            priced = [r for r in ages if r["recheck"]["size"]]
                            causes = Counter(r["recheck_cause"]["category"] for r in ages)

                            def median(field):
                                values = [r["recheck"][field] for r in priced if r["recheck"][field] is not None]
                                return statistics.median(values) if values else None

                            result.append({
                                "window": window, "policy": policy, "configured_delay_seconds": delay, "budget": cap,
                                "cohort": cohort, "signal_exchange_age": receipt_ages,
                                "signals_before_recheck_filter": by_phase["signals"],
                                "still_pending": by_phase["pending"][policy, delay],
                                "rechecks": len(ages), "priced_rechecks": len(priced),
                                "station_days": len({(r["station"], r["date"]) for r in ages}),
                                "rechecks_from_cache_hit": sum(r["later_clock"]["cdn_status"] == "HIT" for r in ages),
                                "recheck_age_unreported": sum(r["later_clock"]["http_age"] is None for r in ages),
                                "median_contracts": median("size"), "median_expected_gross_dollars": median("gross_expected"),
                                "median_all_in_dollars": median("all_in"), "median_fee_dollars": median("fee"),
                                "median_expected_net_dollars": median("estimated_after_fee"),
                                "signaled_quantity_reduced": sum(r["recheck"]["size"] < r["signal"]["size"] for r in priced),
                                "book_alone_removed_edge": causes["updated book alone removes edge"],
                                "forecast_alone_removed_edge": causes["updated forecast alone removes edge"],
                                "either_change_removed_edge": causes["either change removes edge"],
                                "combined_changes_removed_edge": causes["combined changes remove edge"],
                                "liquidity_gaps": causes["displayed liquidity gap"],
                                "forecast_support_gaps": causes["forecast support gap"],
                                "edge_persisted_at_cap": causes["edge persisted"],
                                "unqualified_initial_at_cap": causes["signal cost cap did not qualify"],
                            })
    return result, raw


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", type=Path, default=DEFAULT_DATA)
    ap.add_argument("evidence", type=Path, help="Frozen strategy portfolio evidence gzip file.")
    args = ap.parse_args()
    data = args.data
    frozen = json.loads(gzip.decompress(args.evidence.read_bytes()))
    actual = digest(frozen)
    if actual != args.evidence.name.removesuffix(".json.gz"):
        ap.error("Frozen paper evidence checksum mismatch.")
    artifact = study.load_selected(data, vintage=frozen["model_vintage"])
    if artifact is None:
        ap.error("Original model vintage is unavailable.")
    tape = {**frozen["capture"], "events": [event for event in frozen["capture"]["events"]
                                          if event.get("family", "daily-high") == "daily-high"]}
    rows, matches = summarize(match_study(tape, artifact, parse_time(frozen["split"]), _fee_at(frozen["fee_schedule"])))
    findings = [row for row in rows if row["signal_exchange_age"] == "all" and row["cohort"] == "both policies rechecked"]
    key = {"engine": ENGINE, "paper_input": actual, "caps": CAPS, "delays": DELAYS,
           "case_evidence": "exact signal and delayed native ladders",
           "rules": "Fixed origin-confirmed $25 signal cohort. Price same candidate at requested caps with same first recheck rule."}
    ident = "latency-" + digest(key)[:20]
    snapshot = data / "research" / "evidence" / "latency" / f"{ident}.json.gz"
    write_new(snapshot, gzip.compress(json_bytes({"settings": key, "metrics": rows, "cases": matches}), mtime=0))
    samples = [r for method, records in matches.items() if method.startswith("forward/") for r in records]
    samples = sorted(samples, key=lambda r: (r["recheck_cause"]["category"] == "updated book alone removes edge",
                     abs(r["recheck"]["estimated_after_fee"] or 0)), reverse=True)
    buckets = {bucket["id"]: bucket for event in tape["events"] for bucket in event["buckets"]}
    cases = []
    for sample in samples:
        if any(c["instrument_id"] == sample["instrument_id"] for c in cases):
            continue
        bucket = buckets[sample["instrument_id"]]
        clocks = {sample["at"], sample["recheck_at"]}
        books = [book for book in bucket["books"] if iso(book["at"]) in clocks]
        cases.append({
            **sample, "mode": "live", "reaction_at": sample["recheck_at"], "books": books,
            "bucket": {k: bucket.get(k) for k in ("label", "rules", "lower_f", "upper_f", "quantity_step")},
            "data": {k: sample[k] for k in ("signal", "recheck", "recheck_cause")},
            "title": f"{sample['station']} delayed {sample['side']} quote · {sample['instrument_id']}",
            "detail": f"Delayed receipt at {sample['actual_lag_seconds']:.1f} seconds with {sample['recheck']['size']} priceable "
                      f"contracts and CDN {sample['later_clock']['cdn_status']}. {sample['recheck_cause']['category'].capitalize()}.",
        })
        if len(cases) == 5:
            break
    report = {
        "id": ident, "title": "Did reaction timing change available quantity and cost?",
        "hypothesis": "Like-for-like delayed origin and cache receipts change priceable quantity and expected gross return once cap and quote clock are held explicit.",
        "target": "Daily-high captured book receipts",
        "venues": [POLYMARKET], "created_at": iso(datetime.now(timezone.utc)),
        "date_window": {"from": tape["started_at"], "split": frozen["split"], "through": tape["captured_at"]},
        "metrics": findings, "age_and_policy_groups": rows, "cases": cases,
        "limits": [
            "The primary comparison retains signal receipts that got a recheck under both policies.",
            "Book size and theoretical gross return are nested under snapshot, instrument and station-day.",
            "Gross uses forecast probability times the delayed quantity. It is distinct from cash at risk and P&L.",
            "The engine caps the recheck quantity at what the original signal proposed for the same stake.",
            "A counterfactual holds either the signal forecast or the signal book. Both quotes are repriced at the fixed stake and original quantity ceiling, using fees for the corresponding book receipt.",
        ],
        "provenance": {**key, "frozen_inputs": str(snapshot.relative_to(data)), "replay_details_path": str(snapshot.relative_to(data))},
    }
    path = data / "research" / "strategies" / f"{ident}.json"
    write_new(path, json_bytes(report) + b"\n")
    print(json.dumps({"path": str(path), "metrics": findings}, indent=2))


if __name__ == "__main__":
    main()
