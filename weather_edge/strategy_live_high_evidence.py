"""Inspect frozen late-high model estimates on their actual qualifying book receipts."""

import argparse
from collections import Counter, defaultdict
from datetime import timedelta
import gzip
import json
from pathlib import Path

from . import research_model, study
from .market import _price, decimal, iso, parse_time
from .paper_runs import _fee_at
from .strategy_rules import StrategyRule, book_status
from .strategy_runs import DEFAULT_DATA, FIXED, HIGH_PROBABILITIES, POLYMARKET, Predictor, SHADOW_RULES, digest


VERSION = "same-book-live-late-high-v2"
MODELS = ("guarded", "transfer", "dew-point", "hrrr")
FIRST_MINUTES = 15


def _inputs(snapshot, model, event, bucket, at):
    return snapshot.get(HIGH_PROBABILITIES[model], {}).get(event["id"], {}).get(at, {}).get(bucket["id"])


def _price_sides(event, bucket, book, at, weather, rules, schedule):
    if weather is None:
        return []
    cap = decimal(FIXED.order_size)
    quotes = []
    for side in ("YES", "NO"):
        selection = _price(event, bucket, side, book, weather, at, cap, 0, fee_at=schedule, rules=rules)
        quotes.append({**selection, "paper_signal_at_fixed_floor": _qualifies(selection)})
    return quotes


def _qualifies(selection):
    edge = selection["ev_dollars"]
    return not selection["uncertain"] and edge is not None and edge >= FIXED.minimum_dollars


def compare(snapshot, ordinary):
    matched = defaultdict(list)
    models = tuple(name for name in MODELS if HIGH_PROBABILITIES[name] in snapshot)
    rules = StrategyRule("two-sided", "origin-confirmed", FIXED.minimum_dollars)
    fee = _fee_at(snapshot["fee_schedule"])
    split = parse_time(snapshot["split"])
    for event in snapshot["capture"]["events"]:
        if event.get("family", "daily-high") != "daily-high":
            continue
        anchor = parse_time(event["climate_starts_at"]) + timedelta(hours=17)
        end = min(anchor + timedelta(minutes=FIRST_MINUTES), parse_time(event["ends_at"]))
        ordinary_by_time = {}
        for bucket in event["buckets"]:
            for book in bucket["books"]:
                at = parse_time(book["at"])
                if not max(anchor, split) <= at < end or book_status(book, None, at, "origin-confirmed"):
                    continue
                receipt = iso(at)
                if receipt not in ordinary_by_time:
                    ordinary_by_time[receipt] = ordinary(event, at)
                baseline = (ordinary_by_time[receipt].get(bucket["id"])
                            or _inputs(snapshot, "native-noaa", event, bucket, receipt))
                for model in models:
                    estimate = _inputs(snapshot, model, event, bucket, receipt)
                    if not estimate:
                        continue
                    model_clock = estimate["strategy_context"].get("knowledge_at")
                    if model_clock is None or parse_time(model_clock) != anchor:
                        continue
                    new_quotes = _price_sides(event, bucket, book, at, estimate, rules, fee)
                    old_quotes = _price_sides(event, bucket, book, at, baseline, rules, fee)
                    matched[model, event["id"]].append({
                        "model": model, "mode": "live", "station": event["station"], "date": event["date"], "venue": POLYMARKET,
                        "event_id": event["id"], "instrument_id": bucket["id"], "label": bucket["label"], "at": receipt,
                        "model_knowledge_at": model_clock, "book": book, "baseline": baseline, "shadow": estimate,
                        "shadow_paper_preview": new_quotes, "baseline_paper_preview": old_quotes,
                        "bucket": {field: bucket.get(field) for field in ("label", "lower_f", "upper_f", "rules", "quantity_step")},
                    })
    return matched


def _summary(model, event, rows):
    paired = [row for row in rows if row["baseline"] is not None]
    change = [abs(row["baseline"]["probability"] - row["shadow"]["probability"]) for row in paired]
    source_ages = [(parse_time(row["at"]) - parse_time(row["book"]["source_at"])).total_seconds()
                   for row in rows if row["book"].get("source_at")]
    return {"model": model, "event_id": event, "station": rows[0]["station"], "date": rows[0]["date"],
            "matched_eligible_book_receipts": len(rows), "distinct_instruments": len({r["instrument_id"] for r in rows}),
            "paired_forecast_and_book_receipts": len(paired), "unsupported_shadow_estimates": sum(r["shadow"]["uncertain"] for r in rows),
            "receipts_with_exchange_clock": len(source_ages),
            "receipts_with_exchange_clock_at_most_30s_old": sum(0 <= age <= 30 for age in source_ages),
            "receipts_with_exchange_clock_at_most_120s_old": sum(0 <= age <= 120 for age in source_ages),
            "advance_of_exchange_clock": sum(row["book"].get("source_time_progress") == "advanced" for row in rows),
            "first_local_book": min(rows, key=lambda row: parse_time(row["at"]))["at"],
            "last_local_book": max(rows, key=lambda row: parse_time(row["at"]))["at"],
            "paired_baseline_signals": sum(quote["paper_signal_at_fixed_floor"] for row in paired for quote in row["baseline_paper_preview"]),
            "paired_shadow_signals": sum(quote["paper_signal_at_fixed_floor"] for row in paired for quote in row["shadow_paper_preview"]),
            "largest_same_bucket_yes_probability_difference": max(change, default=None)}


def _market_window(event, split, cutoff):
    anchor = parse_time(event["climate_starts_at"]) + timedelta(hours=17)
    if not split <= anchor <= cutoff:
        return None
    end = min(anchor + timedelta(minutes=FIRST_MINUTES), parse_time(event["ends_at"]))
    caches, outcomes, progress = Counter(), Counter(), Counter()
    instrument_ids = set()
    for bucket in event["buckets"]:
        for book in bucket["books"]:
            at = parse_time(book["at"])
            if not anchor <= at < end or at > cutoff:
                continue
            caches[(book.get("http_cache") or {}).get("cf-cache-status") or "unreported"] += 1
            outcomes[book_status(book, None, at, "origin-confirmed") or "eligible"] += 1
            progress[book.get("source_time_progress") or "unreported"] += 1
            instrument_ids.add(bucket["id"])
    return {"station": event["station"], "event_id": event["id"], "starts_at": iso(anchor),
            "through": iso(min(cutoff, end)), "instrument_count": len(instrument_ids),
            "book_receipt_count": sum(outcomes.values()), "http_cache_status": dict(caches),
            "book_eligibility": dict(outcomes), "exchange_clock_progress": dict(progress)}


def report(data, paper):
    original = json.loads(paper.read_bytes())
    provenance = original["provenance"]
    snapshot = json.loads(gzip.decompress((data / provenance["frozen_inputs"]).read_bytes()))
    if digest(snapshot) != provenance["tape_sha256"]:
        raise ValueError("Frozen late-high model and native-receipt input differs from its paper hash")
    artifact = study.load_selected(data, vintage=snapshot["model_vintage"])
    if artifact is None:
        raise ValueError("The original daily-high baseline is unavailable")
    matched = compare(snapshot, Predictor(artifact)("two-sided"))
    metrics, cases, evidence = [], [], {}
    for (model, event), rows in matched.items():
        metrics.append(_summary(model, event, rows))
        evidence[f"{model}/{event}"] = rows
        case = max(rows, key=lambda row: abs(row["shadow"]["probability"] - row["baseline"]["probability"])
                   if row["baseline"] else -1)
        probability = case["shadow"]["probability"]
        reference = f" The corresponding NOAA estimate was {case['baseline']['probability']:.1%}." if case["baseline"] else ""
        source = case["book"].get("source_at")
        source_detail = ""
        if source:
            age = (parse_time(case["at"]) - parse_time(source)).total_seconds()
            source_detail = f" The exchange clock preceded this local receipt by {age:.1f} seconds."
        cases.append({**case, "title": f"{case['station']} {case['label']} · 17:00 local {model}",
                      "detail": f"The shadow assigned {probability:.1%} to YES on this origin-qualified receipt.{reference}{source_detail}",
                      "books": [case["book"]]})
    cases.sort(key=lambda row: abs(row["shadow"]["probability"] - row["baseline"]["probability"])
               if row["baseline"] else -1, reverse=True)
    events = {event["id"]: event for event in snapshot["capture"]["events"]}
    split, cutoff = [parse_time(original["date_window"][key]) for key in ("split", "through")]
    windows = [_market_window(event, split, cutoff) for event in events.values() if event.get("family", "daily-high") == "daily-high"]
    for ready in snapshot.get("scheduled_high_weather_checks", []):
        model, ident = ready["model"], ready["event_id"]
        if model not in MODELS or (model, ident) in matched:
            continue
        event = events[ident]
        instrument = max(ready["estimates"], key=lambda key: ready["estimates"][key]["probability"])
        estimate = ready["estimates"][instrument]
        bucket = next(row for row in event["buckets"] if row["id"] == instrument)
        anchor = parse_time(ready["decision_at"])
        display = next((book for book in bucket["books"] if anchor <= parse_time(book["at"]) <= cutoff
                        and parse_time(book["at"]) < anchor + timedelta(minutes=FIRST_MINUTES)), None)
        observed_display = {}
        if display:
            status = book_status(display, None, parse_time(display["at"]), "origin-confirmed")
            observed_display = {"observed_display": display, "book_eligibility": status or "eligible", "books": [display]}
        detail = f'The scheduled weather estimate assigned {estimate["probability"]:.1%} to YES.'
        detail += " The frozen comparison found no matched origin-qualified model and book receipt in this window."
        if observed_display:
            detail += f' The first stored display for this threshold has book status: {observed_display["book_eligibility"]}.'
        metrics.append({"model": model, "event_id": ident, "station": event["station"], "date": event["date"],
                        "matched_eligible_book_receipts": 0, "probability_rows_at_scheduled_clock": len(ready["estimates"]),
                        "unsupported_probability_rows": sum(b.get("uncertain", False) for b in ready["estimates"].values())})
        cases.append({"mode": "live", "station": event["station"], "date": event["date"], "event_id": ident, "venue": POLYMARKET,
                      "instrument_id": instrument, "at": display["at"] if display else ready["decision_at"],
                      "model_knowledge_at": ready["decision_at"], "title": f'{event["station"]} {bucket["label"]} · weather-only check',
                      "detail": detail, "data": ready, **observed_display})
    strategy_names = ("two-sided", "joint-native-noaa-high-guarded-low", "joint-late-guarded-high-low",
                      *(SHADOW_RULES[key] for key in MODELS if key in SHADOW_RULES))
    portfolios = [row for row in original["metrics"] if row["window"] == "forward" and row["strategy"] in strategy_names]
    selected = {key: provenance.get("options", {}).get(key) for key in
                ("late_guarded_high", "transfer_high", "dew_point_high", "hrrr_high", "native_noaa_high")}
    record = {"version": VERSION, "title": "Late-high estimates on the same actual book",
              "hypothesis": "An earlier-qualified late high estimate changes supported after-fee candidates during the first 15 minutes after the local 17:00 decision.",
              "target": "Exact Polymarket US daily-high contract, same origin-confirmed ladder and $25 order cap.",
              "venues": [POLYMARKET], "date_window": original["date_window"], "metrics": metrics, "cases": cases[:14],
              "market_windows": [window for window in windows if window is not None],
              "portfolio_reference": portfolios,
              "limits": ["Counts describe the first 15 minutes after 17:00 local standard time, with one weather climate date.",
                         "A preview uses the full $25 cap and omits delayed selection and prior station-day allocation. The linked paper record applies both.",
                         "Weather estimates without enough residual support and receipts lacking origin evidence cannot establish strategy signals.",
                         "Gateway origin confirmation and the age of the captured exchange image are separate observations."],
              "provenance": {"engine": VERSION, "frozen_paper": original["id"], "frozen_input_sha256": provenance["tape_sha256"],
                             "comparison_minutes": FIRST_MINUTES, "variant_vintages": selected, "models": list(MODELS)}}
    return research_model.write_record(data, record, {"matched_books": evidence, "scheduled_weather": snapshot.get("scheduled_high_weather_checks", [])})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("paper", type=Path)
    args = parser.parse_args()
    path = report(args.data, args.paper)
    record = json.loads(path.read_bytes())
    print(json.dumps({"path": str(path), "metrics": record["metrics"]}, indent=2))


if __name__ == "__main__":
    main()
