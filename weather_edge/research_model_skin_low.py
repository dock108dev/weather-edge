"""Test whether high-quality predawn GOES land skin retrievals improve air-temperature lows."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
import gzip
import json
from math import sqrt
from pathlib import Path
from statistics import mean
import sys

from . import expansion_satellite, research_model, research_model_night_cloud, research_model_solar

VERSION = "goes-land-skin-temperature-low-v1"
MIN_PIXELS = 8
METHODS = {"noaa": "Observed and remaining NOAA minimum", "bias": "Earlier station bias",
           "ridge": "Ridge with observed sky and forecast temperatures",
           "ridge_skin": "Ridge plus skin–air temperature difference"}


def gather(data, baseline):
    source = json.loads(gzip.decompress(baseline.read_bytes()))
    if source["version"] != research_model_night_cloud.VERSION:
        raise ValueError("Expected earlier predawn GOES cloud matched input")
    dates = sorted({row["at"] for rows in source["rows"].values() for row in rows})

    def point(at):
        try:
            return expansion_satellite.latest(data, at, product=expansion_satellite.SKIN)
        except (OSError, KeyError, ValueError) as error:
            return {"error": str(error)}

    with ThreadPoolExecutor(max_workers=5) as pool:
        frames = dict(zip(dates, pool.map(point, dates), strict=True))
    rows, gaps = {}, Counter()
    for group, native in source["rows"].items():
        matched = []
        for row in native:
            frame = frames[row["at"]]
            if not frame or "error" in frame:
                gaps[f"{group}: original publication or skin raster missing"] += 1
                continue
            footprint = frame["points"].get(row["station"])
            if not footprint or footprint["window_good_pixels"] < MIN_PIXELS:
                gaps[f"{group}: fewer than eight high-quality land pixels"] += 1
                continue
            zenith = research_model_solar.solar_cosine(frame["valid_at"], footprint["lat"], footprint["lon"])
            latest = row["latest_observation"]
            if zenith > -0.02 or not latest:
                gaps[f"{group}: no nighttime scan or available station air reading"] += 1
                continue
            keys = ("source", "native", "body_sha256", "raw_path", "valid_at", "available_at", "received_at", "product", "satellite")
            air = latest["temp_f"]
            matched.append({**row, "skin_temperature": {**{key: frame[key] for key in keys}, "point": footprint},
                            "skin_temperature_minus_air_f": footprint["window_mean_f"] - air})
        rows[group] = matched
    return {"version": VERSION, "rows": rows, "windows": source["windows"], "missing": dict(gaps),
            "expected": {group: len(rows) for group, rows in source["rows"].items()},
            "baseline": research_model._source_entry(data, baseline)}


def _features(row, method):
    values = research_model_night_cloud._features(row, "ridge_both")
    if method == "ridge_skin":
        values["skin_temperature_minus_air_f"] = row["skin_temperature_minus_air_f"]
    return values


def _case(row, native):
    matched = next((entry["native"] for entry in native if entry["station"] == row["station"] and entry["date"] == row["date"]
                    and entry["payout_yes"]), None)
    skin = row["skin_temperature"]
    point = skin["point"]
    low = row["actual_f"]
    estimates = row.get("estimates")
    summary = f"Complete CLI low {low}°F."
    if estimates:
        base, added = (estimates[key]["mean_f"] for key in ("ridge", "ridge_skin"))
        summary = f"Ridge {base:.1f}°F, skin-feature Ridge {added:.1f}°F. " + summary
    return {"station": row["station"], "date": row["date"], "at": row["at"],
            "title": f"{row['station']} · Predawn land surface and station air",
            "detail": (f"GOES skin temperature averaged {point['window_mean_f']:.1f}°F over "
                       f"{point['window_good_pixels']} high-quality land pixels. The last station reading was "
                       f"{row['latest_observation']['temp_f']:.1f}°F. " + summary),
            **({"venue": "polymarket-us", "instrument_id": matched["instrument_id"], "market": matched} if matched else {}),
            "skin_temperature": skin, "station_measurement": row["latest_observation"], "official_report": row["official_report"],
            "predictions": estimates}


def evaluate(data, inputs):
    path, digest = research_model.freeze_inputs(data, inputs)
    rows = inputs["rows"]
    calibration = rows["calibration"]
    counts = Counter(row["station"] for row in calibration)
    all_good = {station for station in research_model.STATIONS if counts[station] >= 4}
    matched = {group: [row for row in phase if row["station"] in all_good] for group, phase in rows.items()}
    evidence = {}
    metrics, uncertainty, cases, details = [], [], [], {}
    if matched["fit"] and matched["calibration"]:
        model = research_model.fit(matched["fit"], matched["calibration"], methods=METHODS, feature_fn=_features)
        errors = defaultdict(list)
        for row in matched["calibration"]:
            errors[row["station"]].append((row["actual_f"] - row["means"]["txn"]) ** 2)
        widths = {station: max(0.75, sqrt(mean(squared))) for station, squared in errors.items()}
        evidence = {phase: research_model_night_cloud._score(values, model, widths) for phase, values in matched.items()
                    if phase not in ("fit", "calibration")}
        for phase, scored in evidence.items():
            metrics.extend(research_model.metrics(scored, phase, METHODS))
            if scored:
                uncertainty.append({"group": phase, **research_model.date_bootstrap(scored, "ridge", "ridge_skin", "brier")})
        late = evidence["later"]
        native = research_model_night_cloud._native(data, late)
        if native:
            metrics.extend({"method": method, "label": title, "group": "Later exact native payouts",
                            "calendar_dates": len({row["date"] for row in native}),
                            "station_days": len({(row["station"], row["date"]) for row in native}), "bucket_cases": len(native),
                            "bucket_brier": mean((row["probabilities"][method] - row["payout_yes"]) ** 2 for row in native)}
                           for method, title in METHODS.items())
        order = sorted(late, key=lambda row: abs(row["skin_temperature_minus_air_f"]), reverse=True)
        cases.extend(_case(row, native) for row in order[:2])
        details["native"] = native
    if not cases:
        source_cases = sorted(rows["later"], key=lambda row: abs(row["skin_temperature_minus_air_f"]), reverse=True)
        topic = research_model_night_cloud._native(data, source_cases)
        cases.extend(_case(row, topic) for row in source_cases[:2])
    details["weather"] = evidence
    details["measured_source_cases"] = rows["later"]
    coverage = {phase: {**research_model._independent_counts(values), "source_eligible": len(rows[phase]),
                        "candidate_case_count": inputs["expected"][phase]} for phase, values in matched.items()}
    metrics.extend({"group": group, "method": "coverage", "label": "Qualifying high-quality land observations",
                    "source_eligible": counts["source_eligible"], "candidate_case_count": counts["candidate_case_count"],
                    "scored_cases": counts["snapshots"]} for group, counts in coverage.items())
    record = {"version": VERSION, "title": "Can a predawn land temperature retrieval improve the air-temperature low?",
              "hypothesis": "The measured difference between the nearby land surface and station air can flag additional cooling while the sky remains clear.",
              "target": "Whole climate-day KNYC and KMDW minimum air temperature",
              "venues": ["polymarket-us"] if any(row.get("venue") for row in cases) else [], "date_window": inputs["windows"],
              "coverage": coverage, "metrics": metrics, "cases": cases, "uncertainty": uncertainty, "missing": inputs["missing"],
              "limits": ["Skin temperature is a satellite surface retrieval in kelvin. It is a different physical quantity from sheltered station air temperature.",
                         "Each footprint requires at least eight native high-quality land pixels. Cloud, missing data, or poor quality can remove much of a neighborhood.",
                         "The qualifying 2 km CONUS retrieval was published before 03:00 local-standard clock. The exact sample can be an hour old.",
                         "The paired models use identical covered cases. Sparse earlier station support limits portability to cloudy periods and stations with marine pixels.",
                         "The source observation time, publication time, and local acquisition time are all retained."],
              "provenance": {"frozen_input": str(path.relative_to(data)), "frozen_input_sha256": digest,
                             "baseline_input": inputs["baseline"], "documentation": expansion_satellite.SKIN_DOCS,
                             "source_sha256": research_model.run_sources(sys.modules[__name__], research_model_night_cloud, expansion_satellite)}}
    return research_model.write_record(data, record, details)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--cloud-input", type=Path)
    source.add_argument("--input", type=Path)
    args = parser.parse_args()
    inputs = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else gather(args.data, args.cloud_input)
    print(evaluate(args.data, inputs))


if __name__ == "__main__":
    main()
