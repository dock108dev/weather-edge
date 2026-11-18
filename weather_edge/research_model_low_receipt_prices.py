"""Replay native display prices around the original receipt of a lower station reading."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date, timedelta
import gzip
import json
from pathlib import Path
from statistics import mean
import sys

import numpy as np

from . import expansion_live_low, expansion_weather, research_model, research_model_low_precision, venue_archive
from . import strategy_forecast_innovation as quotes
from .model import _outcome
from .nbm import climate_window, iso, utc

VERSION = "daily-low-receipt-price-response-v1"
HORIZONS = (5, 15, 30)
AGE = timedelta(minutes=2)
MIN_MOVE = 0.05
MIX = 0.25
DOCS = "https://docs.polymarket.us/api-reference/price-history/get-price-history"


def _triggers(rows):
    by_event = defaultdict(list)
    for row in rows:
        by_event[row["station"], row["date"]].append(row)
    output = {}
    for pair, snapshots in by_event.items():
        previous = None
        triggers = []
        for row in sorted(snapshots, key=lambda row: row["at"]):
            prior = (previous["new_bound"] if previous and previous["scheduled_at"] == row["scheduled_at"]
                     else row["previous_bound"])
            after = row["new_bound"]
            if row["at"] == after["available_at"] and after["upper_f"] < prior["upper_f"]:
                triggers.append({**row, "before_receipt": prior})
            previous = row
        if triggers:
            output[pair] = triggers
    return output


def _native_buckets(db, station, day, triggers):
    rows = [dict(row) for row in db.execute("""SELECT venue, station, day, event_id, instrument_id, rules_primary, label,
                                                   opened_at, closed_at, yes_payout, settlement_at, settlement_receipt_id, source
                                            FROM markets WHERE venue='polymarket-us' AND family='daily-low'
                                            AND station=? AND day=? ORDER BY instrument_id""", (station, day))]
    by_id = {row["instrument_id"]: row for row in rows}
    named = {"buckets": [{"id": identifier, "rules": row["rules_primary"]} for identifier, row in by_id.items()]}
    try:
        buckets = expansion_live_low._validated_buckets(named, date.fromisoformat(day), station)
    except (ValueError, KeyError, TypeError):
        return []
    result = []
    actual = triggers[0]["official_report"]["low_f"]
    for bucket in buckets:
        row = by_id[bucket["id"]]
        payout = row["yes_payout"]
        if (payout not in ("0", "1") or not row["settlement_receipt_id"] or not row["opened_at"] or not row["closed_at"]
                or int(payout) != _outcome(bucket, actual)):
            return []
        result.append({**row, **bucket, "payout_yes": int(payout)})
    return result if sum(row["payout_yes"] for row in result) == 1 else []


def _odds(row, buckets, witness):
    upper = witness["upper_f"]
    center = min(row["previous_weather"]["means"]["remaining"], upper)
    return research_model_low_precision._bucket_probabilities(buckets, center, row["band"]["width_f"],
                                                             upper, row["band"]["uncapped_weight"])


def _span(station, day, row):
    start, end = climate_window(day, expansion_weather.STATIONS[station][3])
    future = [start + timedelta(hours=hour) for hour in research_model.HOURS if hour > row["clock_lst"]]
    return min(future, default=end)


def _price_pair(db, bucket, at, clocks, end):
    span = timedelta(minutes=max(HORIZONS))
    opened, closed = (utc(bucket[key]) for key in ("opened_at", "closed_at"))
    if not opened < at < min(closed, end):
        return None, [], "native trading or trained clock"
    times, curve = quotes.prices(db, "polymarket-us", bucket, at - span - AGE - quotes.POINT_LAG, at + span)
    before = quotes.latest(times, curve, at - timedelta(microseconds=1), AGE)
    if before is None or utc(before["at"]) < opened:
        return None, [], "no coherent pre-receipt display"
    movement = []
    for minutes in HORIZONS:
        length = timedelta(minutes=minutes)
        left, right = at - length, at + length
        if left <= opened or right >= min(closed, end) or any(left <= stamp <= right and stamp != at for stamp in clocks):
            continue
        left_quote = quotes.latest(times, curve, left, AGE)
        right_quote = quotes.latest(times, curve, right, AGE)
        if not left_quote or not right_quote or utc(left_quote["at"]) < opened or utc(right_quote["at"]) <= at:
            continue
        movement.append({"minutes": minutes, "pre_window_display": left_quote, "before": before, "after": right_quote,
                         "after_delta": right_quote["midpoint"] - before["midpoint"],
                         "before_delta": before["midpoint"] - left_quote["midpoint"]})
    return before, movement, None


def gather(data, reference):
    study = json.loads(reference.read_bytes())
    if study["version"] != "daily-low-original-receipt-updates-v1" or set(study["stations"]) != set(research_model.STATIONS):
        raise ValueError("Expected the original-receipt study for both native low stations")
    path = data / study["provenance"]["replay_details_path"]
    if research_model._digest(path) != study["provenance"]["replay_details_sha256"]:
        raise ValueError("The original receipt replay detail checksum disagrees")
    rows = json.loads(gzip.decompress(path.read_bytes()))["test"]
    triggers = _triggers(rows)
    samples, gaps = [], Counter()
    with venue_archive.connect(data) as db:
        for (station, day), events in sorted(triggers.items()):
            buckets = _native_buckets(db, station, day, events)
            if not buckets:
                gaps["no exact settled native partition"] += len(events)
                continue
            clocks = {utc(row["at"]) for row in events}
            for row in events:
                at = utc(row["at"])
                old = _odds(row, buckets, row["before_receipt"])
                new = _odds(row, buckets, row["new_bound"])
                changes = [after - before for before, after in zip(old, new, strict=True)]
                selected = max(range(len(changes)), key=lambda index: abs(changes[index]))
                end = _span(station, day, row)
                for index, (bucket, prior, updated, change) in enumerate(zip(buckets, old, new, changes, strict=True)):
                    before, movement, reason = _price_pair(db, bucket, at, clocks, end)
                    if reason:
                        gaps[reason] += 1
                    signed = 1 if change > 0 else -1
                    marks = [{**arm, "aligned_after": signed * arm["after_delta"],
                              "aligned_before": signed * arm["before_delta"]} for arm in movement]
                    samples.append({"station": station, "date": day, "at": iso(at), "market": bucket,
                                    "old_weather": prior, "new_weather": updated, "signal": change,
                                    "largest_signal": index == selected, "qualifying_signal": index == selected and abs(change) >= MIN_MOVE,
                                    "outcome": bucket["payout_yes"], "before": before, "movement": marks,
                                    "weather": {name: row[name] for name in ("official_report", "scheduled_at", "previous_weather",
                                                                            "before_receipt", "new_bound", "band")}})
    return {"version": VERSION, "date_window": study["date_window"], "cases": samples, "gaps": dict(gaps),
            "trigger_count": sum(map(len, triggers.values())), "fit_reference": research_model._source_entry(data, reference),
            "trigger_days": len(triggers)}


def _interval(rows, value):
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["date"]].append(value(row))
    means = [mean(day) for day in grouped.values()]
    sampled = np.random.default_rng(20261115).choice(means, size=(3000, len(means))).mean(axis=1)
    return {"calendar_dates": len(means), "delta": mean(means), "date_bootstrap_interval_95": np.quantile(sampled, (.025, .975)).tolist()}


def _movement(rows):
    pairs, metrics, intervals = {}, [], []
    for minutes in HORIZONS:
        paired = [{**row, **arm} for row in rows if row["qualifying_signal"]
                  for arm in row["movement"] if arm["minutes"] == minutes]
        pairs[minutes] = paired
        if not paired:
            continue
        for method in ("aligned_before", "aligned_after"):
            metrics.append({"method": method, "label": "Timed display mark movement in the weather-signal direction",
                            "group": f"{minutes} minute paired windows", **research_model._independent_counts(paired),
                            "mean_aligned_move_cents": 100 * mean(row[method] for row in paired),
                            "fraction_prices_moved": mean(row[method] != 0 for row in paired)})
        ci = _interval(paired, lambda row: row["aligned_after"] - row["aligned_before"])
        intervals.append({"group": f"{minutes} minute paired windows", "method": "aligned_after", "baseline": "aligned_before",
                          **ci})
    return pairs, metrics, intervals


def _prediction_scores(samples):
    groups = {"Every verified native threshold": samples,
              "Thresholds with coherent before display": [row for row in samples if row["before"]],
              "Largest weather moves with before display": [row for row in samples if row["before"] and row["qualifying_signal"]]}
    metrics, contrasts = [], []
    for group, rows in groups.items():
        if not rows:
            continue
        methods = {"old_weather": lambda row: row["old_weather"], "new_weather": lambda row: row["new_weather"]}
        if all(row["before"] for row in rows):
            methods.update(display=lambda row: row["before"]["midpoint"],
                           display_plus_signal=lambda row: max(0.0, min(1.0, row["before"]["midpoint"] + MIX * row["signal"])))
        def error(row, method):
            return (methods[method](row) - row["outcome"]) ** 2

        for method in methods:
            metrics.append({"group": group, "method": method, "label": method.replace("_", " "),
                            **research_model._independent_counts(rows), "bucket_cases": len(rows),
                            "snapshots": len({(row["station"], row["date"], row["at"]) for row in rows}),
                            "bucket_brier": mean(error(row, method) for row in rows)})
        for baseline, candidate in (("old_weather", "new_weather"), ("display", "display_plus_signal")):
            if candidate in methods:
                contrasts.append({"baseline": baseline, "method": candidate, "group": group,
                                  **_interval(rows, lambda row: error(row, candidate) - error(row, baseline))})
    return metrics, contrasts


def _case(row):
    market = row["market"]
    before, after = row["weather"]["before_receipt"], row["weather"]["new_bound"]
    return {"station": row["station"], "date": row["date"], "at": row["at"], "venue": "polymarket-us",
            "instrument_id": market["instrument_id"], "title": f"{row['station']} · First lower report and the actual price history",
            "detail": (f"Upper reading changed from {before['upper_f']}°F to {after['upper_f']}°F at the original receipt. "
                       f"Weather chance moved from {row['old_weather']:.1%} to {row['new_weather']:.1%}. "
                       f"The native YES side paid ${row['outcome']}."),
            "evidence": row}


def evaluate(data, inputs):
    frozen, digest = research_model.freeze_inputs(data, inputs)
    rows = inputs["cases"]
    movements, metrics, contrasts = _movement(rows)
    prediction_metrics, comparison = _prediction_scores(rows)
    ranked = sorted(movements[15], key=lambda row: row["aligned_after"] - row["aligned_before"])
    strongest = sorted((row for row in rows if row["qualifying_signal"]), key=lambda row: abs(row["signal"]), reverse=True)
    options = [ranked[0], ranked[-1], strongest[0]] if ranked and strongest else strongest[:2]
    chosen = {(row["at"], row["market"]["instrument_id"]): row for row in options}
    record = {"version": VERSION, "title": "Did low-temperature market marks react to an exact colder report?",
              "hypothesis": "A newly tighter station upper bound changes the weather probabilities. Comparing the same market immediately before and after the original report receipt tests whether its display moved in the predicted direction.",
              "target": "Exact-station Polymarket US daily-low thresholds and settled payoffs",
              "venues": ["polymarket-us"], "date_window": inputs["date_window"],
              "coverage": {"exact_station_receipts": inputs["trigger_count"], "station_days_with_tightening": inputs["trigger_days"],
                           "bucket_predictions": len(rows), "quoted_bucket_predictions": sum(row["before"] is not None for row in rows),
                           "five_point_signals": len(strongest), "paired_movement_samples": {key: len(value) for key, value in movements.items()}},
              "metrics": [*prediction_metrics, *metrics], "cases": [_case(row) for row in chosen.values()],
              "uncertainty": [*comparison, *contrasts], "missing": inputs["gaps"],
              "limits": ["The six-hour group can update a bound only at its matched original NOAA MADIS receipt and only when the full interval had elapsed. The current scheduled forecast remains fixed.",
                         "The venue's archived price timestamp carries a one-minute subminute allowance. The allowed two-minute display age starts at that delayed timestamp.",
                         "The main response sample selects only the largest absolute weather change at each original receipt, requiring at least five percentage points. Its fixed windows end five, 15, and 30 minutes from the receipt and exclude other qualifying lower observations in each paired window.",
                         "The movement comparison is paired to the immediately preceding equal-length window and may still reflect pre-existing weather information. It does not establish causal price response.",
                         "Archived display marks do not show eligible order depth. The market-plus-signal arm adds one quarter of the weather innovation to the latest midpoint for prediction scoring."],
              "provenance": {"frozen_input": str(frozen.relative_to(data)), "frozen_input_sha256": digest,
                             "historical_source": inputs["fit_reference"], "display_documentation": DOCS,
                             "source_sha256": research_model.run_sources(sys.modules[__name__], quotes, research_model_low_precision)}}
    return research_model.write_record(data, record, {"verified_native_buckets": rows, "paired_movement": movements})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--receipt-study", type=Path)
    source.add_argument("--input", type=Path)
    args = parser.parse_args()
    corpus = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else gather(args.data, args.receipt_study)
    print(evaluate(args.data, corpus))


if __name__ == "__main__":
    main()
