"""Replay daily-low updates at exact original station receipt times."""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
import gzip
import hashlib
import json
from pathlib import Path
from statistics import mean
import sys

from . import expansion_live_low_receipts, expansion_metar, expansion_replay, expansion_weather, research_model, venue_archive
from . import research_model_low_full_day, research_model_low_portability as portable, research_model_low_precision as precision
from .model import _outcome
from .nbm import climate_window, iso, utc

VERSION = "daily-low-original-receipt-updates-v1"
METHODS = {"scheduled": "Guarded update at scheduled clock", "new_bound": "Same guidance, update as raw report arrives",
           "all_weather": "Update raw station report and remaining NOAA guidance"}


def _scheduled_before(station, first, acquired, widths, exceptions):
    return max(((hour, first + timedelta(hours=hour)) for hour in research_model.HOURS[1:]
                if first + timedelta(hours=hour) <= acquired and ("precision_center", station, hour) in widths
                and (station, hour) in exceptions), default=None)


def _day_snapshots(station, day, archive, forecasts, label, widths, exceptions):
    start, end = climate_window(day, expansion_weather.STATIONS[station][3])
    observations = [row for row in archive["observations"] if row.get("receipt_source") and row.get("raw")]
    receipts = sorted({utc(row["available_at"]) for row in observations if start <= utc(row["available_at"]) < end})
    records = []
    omitted = Counter()
    for receipt in receipts:
        previous = _scheduled_before(station, start, receipt, widths, exceptions)
        if not previous:
            omitted["no earlier calibrated clock"] += 1
            continue
        hour, as_of = previous
        if receipt == as_of:
            continue
        prior_obs = expansion_weather.observations_as_of(observations, start, as_of, delay_minutes=None)
        prior = expansion_metar.upper_bound(prior_obs, station, start, as_of)
        if not prior:
            omitted["no matched raw bound at scheduled clock"] += 1
            continue
        upgrade = expansion_live_low_receipts.tightened_bound(observations, station, start, as_of, receipt, prior["upper_f"])
        latest = upgrade or prior
        scheduled = research_model_low_full_day._at(station, day, as_of, forecasts, observations, label, delay_minutes=None)
        updated = research_model_low_full_day._at(station, day, receipt, forecasts, observations, label, delay_minutes=None)
        if not scheduled or not updated:
            omitted["no complete originally published remaining forecast"] += 1
            continue
        records.append({"station": station, "date": str(day), "at": iso(receipt), "scheduled_at": iso(as_of),
                        "clock_lst": hour, "report_wait_minutes": (receipt - as_of).total_seconds() / 60,
                        "previous_bound": prior, "new_bound": latest, "first_bound_seen_after_clock": upgrade is not None,
                        "previous_weather": scheduled, "new_weather": updated, "official_report": label,
                        "bounds_were_violated": label["low_f"] > latest["upper_f"],
                        "band": {"width_f": widths["precision_center", station, hour],
                                 **exceptions[station, hour]}})
    return records, omitted


def _source_station(data, station, widths, exceptions, test):
    labels, _, issued = expansion_replay._station_data(data, station, *test, madis_only=True)
    evidence = []
    gaps = Counter()
    refs = []
    for day in research_model._days(*test):
        replay = data / "expansion" / "replay" / str(day) / f"{station}.json"
        if not replay.is_file():
            gaps["no archived receipt projection"] += 1
            continue
        official = precision._scoped_labels(data, station, day, labels.get(str(day)))
        if not official or official["low_f"] is None:
            gaps["no verified raw official minimum"] += 1
            continue
        raw = json.loads(replay.read_bytes())
        forecasts = [card for issue, card in issued.items() if day - timedelta(days=2) <= issue.date() <= day + timedelta(days=1)]
        samples, omissions = _day_snapshots(station, day, raw, forecasts, official, widths, exceptions)
        evidence.extend(samples)
        gaps.update(omissions)
        if samples:
            refs.append(research_model._source_entry(data, replay))
    return evidence, dict(gaps), refs


def gather(data, source, *, test=portable.TEST):
    source_bytes = gzip.decompress(source.read_bytes())
    reference = json.loads(source_bytes)
    if reference["version"] == portable.VERSION:
        stations, earlier, interval = portable.STATIONS, reference["rows"]["adapt"], list(map(str, portable.ADAPT))
    elif reference["version"] == precision.VERSION:
        stations, earlier, interval = research_model.STATIONS, reference["rows"]["calibration"], reference["windows"]["calibration"]
    else:
        raise ValueError("Expected frozen daily-low calibration inputs")
    if not date.fromisoformat(interval[-1]) < test[0] <= test[1]:
        raise ValueError("Receipt verification must be ordered and later than calibration")
    widths, exceptions = precision._calibrate(earlier)
    records = []
    gaps, source_refs = {}, {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        tasks = {station: pool.submit(_source_station, data, station, widths, exceptions, test) for station in stations}
        for station, task in tasks.items():
            rows, omissions, refs = task.result()
            records.extend(rows)
            gaps[station] = omissions
            source_refs[station] = refs
    return {"version": VERSION, "cases": records, "missing": gaps, "source_refs": source_refs,
            "stations": stations, "calibration_window": interval, "test_window": list(map(str, test)),
            "scheduled_calibration_input_sha256": hashlib.sha256(source_bytes).hexdigest(),
            "fit_reference": research_model._source_entry(data, source)}


def _eval(row):
    before, after = row["previous_bound"]["upper_f"], row["new_bound"]["upper_f"]
    old_weather = row["previous_weather"]["means"]["remaining"]
    new_weather = row["new_weather"]["means"]["remaining"]
    models = {"scheduled": (min(before, old_weather), before), "new_bound": (min(after, old_weather), after),
              "all_weather": (min(after, new_weather), after)}
    width, reserve = row["band"]["width_f"], row["band"]["uncapped_weight"]
    buckets = research_model._benchmark_buckets(round(row["previous_weather"]["means"]["txn"]))
    actual = row["official_report"]["low_f"]
    truth = [_outcome(bucket, actual) for bucket in buckets]
    estimates = {}
    for name, (point, bound) in models.items():
        chances = precision._bucket_probabilities(buckets, point, width, bound, reserve)
        estimates[name] = {"mean_f": point, "error_f": point - actual, "probabilities": chances,
                           "brier": mean((p - y) ** 2 for p, y in zip(chances, truth, strict=True))}
    return {**row, "buckets": buckets, "truth": truth, "estimates": estimates}


def _case(data, row):
    actual = row["official_report"]["low_f"]
    native = venue_archive.query(data, family="daily-low", place=row["station"], day=row["date"], limit=40)["rows"]
    winners = [entry for entry in native if entry["yes_payout"] == "1"]
    winner = min(winners, key=lambda row: row["venue"] != "polymarket-us", default=None)
    before, after = row["previous_bound"]["upper_f"], row["new_bound"]["upper_f"]
    heading = f"{row['station']} · Local report received before next scheduled update"
    info = f"Known upper bound {before}°F at {row['scheduled_at'][11:16]}Z, falling to {after}°F at report receipt. Exact CLI minimum {actual}°F."
    return {"title": heading, "detail": info, "station": row["station"], "date": row["date"], "at": row["at"],
            **({"venue": winner["venue"], "instrument_id": winner["instrument_id"], "native_market": winner} if winner else {}),
            "receipt_comparison": row}


def evaluate(data, inputs):
    path, digest = research_model.freeze_inputs(data, inputs)
    compared = [_eval(row) for row in inputs["cases"]]
    changed = [row for row in compared if row["first_bound_seen_after_clock"]]
    first = {}
    for row in sorted(changed, key=lambda item: item["at"]):
        first.setdefault((row["station"], row["date"], row["clock_lst"]), row)
    groups = {"All originally timed receipt updates": compared, "A tighter bound arrived ahead of the clock": changed,
              "First tighter report per scheduled clock": list(first.values()),
              "At the exact tighter report receipt": [row for row in changed if row["at"] == row["new_bound"]["available_at"]]}
    metrics, uncertainty = [], []
    for name, rows in groups.items():
        metrics.extend(research_model.metrics(rows, name, METHODS))
        if rows:
            uncertainty.extend({"group": name, **research_model.date_bootstrap(rows, baseline, method, "brier")}
                               for baseline, method in (("scheduled", "new_bound"), ("scheduled", "all_weather"), ("new_bound", "all_weather")))
    candidates = sorted(changed, key=lambda row: row["estimates"]["scheduled"]["brier"] - row["estimates"]["new_bound"]["brier"], reverse=True)
    distinct = []
    seen = set()
    for row in candidates:
        if row["station"] in seen:
            continue
        distinct.append(_case(data, row))
        seen.add(row["station"])
        if len(distinct) == 4:
            break
    record = {"version": VERSION, "title": "Daily-low probabilities at real station report receipt times",
              "hypothesis": "A new decoded lower report can tighten daily-low probabilities before the next scheduled model update. The same-event test separates that bound from intervening forecast guidance.",
              "target": f"Whole-degree exact raw CLI minima at {len(inputs['stations'])} sites",
              "stations": list(inputs["stations"]),
              "venues": sorted({row["venue"] for row in distinct if "venue" in row}),
              "date_window": {"early_calibration": inputs["calibration_window"], "later_reports": inputs.get("test_window", list(map(str, portable.TEST))),
                              "official_evidence_cutoff_at": iso(max(utc(row["official_report"]["published_at"]) for row in compared))},
              "coverage": {**{name: research_model._independent_counts(rows) for name, rows in groups.items()},
                           "raw_cli_bound_disagreements": sum(row["bounds_were_violated"] for row in compared)},
              "metrics": metrics, "uncertainty": uncertainty, "cases": distinct, "missing": inputs["missing"],
              "limits": ["The earlier fit aligns error dispersion and a reserve with its scheduled climate-hour evidence. Every comparison keeps that same earlier support.",
                         "Station reports qualify by their matched original NOAA MADIS receipt. NOAA forecast availability uses the original object publication clock.",
                         "A six-hour observation can update probabilities at its original receipt only when its entire time interval is known to be inside the climate day by then. Conflicting same-time raw measurements cannot tighten the between-clock bound.",
                         "The tighter-bound subgroup includes repeated report receipts before the next scheduled model clock; date-block uncertainty accounts for within-date dependence.",
                         "Any linked venue payout confirms a case. The study scores the physical minimum and omits execution."],
              "provenance": {"frozen_input": str(path.relative_to(data)), "frozen_input_sha256": digest,
                             "scheduled_calibration_input_sha256": inputs["scheduled_calibration_input_sha256"],
                             "source_sha256": research_model.run_sources(sys.modules[__name__], expansion_live_low_receipts, expansion_metar, research_model_low_full_day)}}
    return research_model.write_record(data, record, {"test": compared})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    sources = parser.add_mutually_exclusive_group(required=True)
    sources.add_argument("--calibration-input", "--portability-input", dest="calibration_input", type=Path)
    sources.add_argument("--input", type=Path)
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--end", type=date.fromisoformat)
    args = parser.parse_args()
    if (args.start is not None) != (args.end is not None) or (args.input and args.start):
        parser.error("A custom verification period needs both --start and --end with original calibration")
    test = (args.start, args.end) if args.start else portable.TEST
    corpus = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else gather(args.data, args.calibration_input, test=test)
    print(evaluate(args.data, corpus))


if __name__ == "__main__":
    main()
