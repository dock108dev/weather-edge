"""Test the archived extended outlook at two stations outside the modeled venue footprint."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
import gzip
import hashlib
import json
from pathlib import Path
import sys

from . import expansion_nbe, expansion_weather, research_model, research_model_extended_calibration as calibration
from . import research_model_extended_temperatures as outlook
from .nbm import utc

VERSION = "nbm-seven-day-weather-only-transfer-v1"
STATIONS = ("KPDX", "KBUF")


def gather(data, reference):
    report = json.loads(reference.read_bytes())
    if report["version"] != calibration.VERSION:
        raise ValueError("Expected the four-station extended band calibration report")
    content = gzip.decompress((data / report["provenance"]["frozen_input"]).read_bytes())
    if hashlib.sha256(content).hexdigest() != report["provenance"]["frozen_input_sha256"]:
        raise ValueError("Four-station calibration input digest disagrees")
    earlier = json.loads(content)
    samples, gaps = [], Counter()
    for station in STATIONS:
        weather = expansion_weather.daily_labels(data, station, outlook.ORIGINS[0] + timedelta(days=1), outlook.CURRENT - timedelta(days=1))
        for origin in outlook.ORIGINS:
            issue = datetime.combine(origin, time(12), timezone.utc)
            card = expansion_nbe.load_card(data, issue, station=station)
            if not card or not issue <= utc(card["available_at"]) <= issue + timedelta(hours=3):
                gaps["no timely preserved complete station forecast"] += 1
                continue
            for day in expansion_nbe.daily_extrema(card):
                row = outlook._case_row(data, station, card, date.fromisoformat(day["target_date"]), weather, day)
                if row:
                    samples.append(row)
                else:
                    gaps["no complete original station report or normal"] += 1
    return {"version": VERSION, "earlier_fit": earlier["rows"]["fit"], "fit_clock": earlier["selection_clock"],
            "selected_method": report["selected_in_earlier_interval_test"], "samples": samples, "missing": dict(gaps),
            "reference": research_model._source_entry(data, reference)}


def evaluate(data, inputs):
    if inputs["version"] != VERSION or not inputs["samples"]:
        raise ValueError("No original forecast and raw reports for the weather-only stations")
    frozen, digest = research_model.freeze_inputs(data, inputs)
    raw = inputs["samples"]
    reserve = calibration._fit(inputs["earlier_fit"])
    evaluated = [result for row in calibration._records(raw) if (result := calibration._predictions(row, reserve))]
    metrics, comparisons, selected_cases = [], [], []
    for choice in ("all", *STATIONS):
        station_rows = [row for row in raw if choice == "all" or row["station"] == choice]
        station_ranges = [row for row in evaluated if choice == "all" or row["station"] == choice]
        if not station_rows:
            continue
        metrics.extend(outlook._metrics(station_rows, f"{choice} · all complete weather cases", ("nbe", "normal")))
        metrics.extend(calibration._metrics(station_ranges, f"{choice} · same-case historical forecast ranges"))
        for first, last in outlook.LEADS:
            subset = [row for row in station_rows if first <= row["lead_day"] <= last]
            metrics.extend(outlook._metrics(subset, f"{choice} · lead days {first}–{last}", ("nbe", "normal")))
        comparisons.extend({"station": choice, **calibration._uncertainty(station_ranges, method, "interval_score_f")}
                           for method in ("widened", "empirical") if station_ranges)
        if choice != "all" and station_ranges:
            case = max(station_ranges, key=lambda row: abs(row["actual_f"] - row["predicted_f"]))
            selected_cases.append(calibration._case(case, inputs["selected_method"]))
    record = {"version": VERSION, "title": "Seven-day temperature forecasts at Portland and Buffalo",
              "hypothesis": "Extended station weather remains more informative than normals outside active market coverage. Earlier interval calibration from four other stations might transfer as well.",
              "target": "Exact archived Portland and Buffalo temperatures, using no model-fitting data from either station",
              "venues": [], "date_window": {"later_issues": [str(outlook.ORIGINS[0]), str(outlook.ORIGINS[-1])],
                                            "earlier_calibration_known_by": inputs["fit_clock"]},
              "coverage": {**research_model._independent_counts(raw), "full_range_targets": len(evaluated), "independent_weekly_issues": len({row["issue"]["issued_at"] for row in raw})},
              "metrics": metrics, "uncertainty": comparisons, "cases": selected_cases, "missing": inputs["missing"],
              "limits": ["The original extended guidance and complete raw climate-day labels join by exact station and target date. The guidance's native 18-hour extremes are narrower than a full climate day.",
                         "Portland and Buffalo have no role in fitting or selecting the examined interval method. No native threshold payout is used in this weather-only comparison.",
                         "Original forecast-issuance weeks remain the comparison clusters. These six blocks give limited independent coverage of distinct weather."],
              "provenance": {"source_calibration": inputs["reference"], "frozen_input": str(frozen.relative_to(data)), "frozen_input_sha256": digest,
                   "source_sha256": research_model.run_sources(sys.modules[__name__], outlook, calibration, expansion_nbe, expansion_weather)}}
    return research_model.write_record(data, record, {"weather": raw, "transferred_intervals": evaluated})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--calibration-study", type=Path)
    src.add_argument("--input", type=Path)
    args = parser.parse_args()
    inputs = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else gather(args.data, args.calibration_study)
    print(evaluate(args.data, inputs))


if __name__ == "__main__":
    main()
