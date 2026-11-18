"""Test the as-issued HRRR surface longwave forecast against predawn daily minima."""

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

from . import expansion_hrrr_longwave, hrrr, research_model, research_model_night_cloud
from .nbm import utc

VERSION = "hrrr-predawn-longwave-low-v1"
STEFAN_BOLTZMANN = 5.670374419e-8
METHODS = {"noaa": "Station and remaining NOAA minimum", "bias": "Earlier station bias",
           "ridge": "Ridge with station sky and native satellite cloud", "ridge_down": "Ridge plus downward longwave",
           "ridge_net": "Ridge plus net surface longwave"}


def gather(data, original):
    source = json.loads(gzip.decompress(original.read_bytes()))
    if source["version"] != research_model_night_cloud.VERSION:
        raise ValueError("Expected frozen earlier predawn cloud inputs")
    requested = sorted({row["at"] for rows in source["rows"].values() for row in rows})

    def sample(at):
        try:
            return expansion_hrrr_longwave.frame(data, at, fetch=True)
        except (hrrr.SourcePaused, OSError, ValueError, KeyError) as error:
            return {"gap": str(error)}

    with ThreadPoolExecutor(max_workers=4) as pool:
        frames = dict(zip(requested, pool.map(sample, requested), strict=True))
    output, gaps = {}, Counter()
    for phase, relevant in source["rows"].items():
        rows = []
        for row in relevant:
            result = frames[row["at"]]
            if not result or "gap" in result:
                error = result.get("gap", "published after the decision") if result else "published after the decision"
                gaps[f"{phase}: {error}"] += 1
                continue
            observed = row["latest_observation"]
            if observed is None:
                gaps[f"{phase}: no eligible earlier station air measurement"] += 1
                continue
            point = result["stations"][row["station"]]
            reference = STEFAN_BOLTZMANN * ((observed["temp_f"] - 32) / 1.8 + 273.15) ** 4
            if not utc(result["issued_at"]) <= utc(result["available_at"]) <= utc(result["valid_at"]) == utc(row["at"]):
                raise ValueError("Ineligible longwave clock")
            keys = ("model", "field", "resolution_m", "source", "issued_at", "available_at", "received_at", "valid_at",
                    "native_units", "native_fields", "source_index")
            rows.append({**row, "radiation": {**{key: result[key] for key in keys}, "cell": point},
                         "relative_downward_longwave": point["downward_w_m2"] / reference})
        output[phase] = rows
    return {"version": VERSION, "rows": output, "windows": source["windows"], "source": research_model._source_entry(data, original),
            "gaps": dict(gaps), "issued_pair_frames": sum(bool(row and "gap" not in row) for row in frames.values()),
            "requested_pair_frames": len(requested)}


def _features(row, method):
    result = research_model_night_cloud._features(row, "ridge_both")
    if method == "ridge_down":
        result["relative_downward_longwave"] = row["relative_downward_longwave"]
    if method == "ridge_net":
        result["net_downward_w_m2"] = row["radiation"]["cell"]["net_downward_w_m2"]
    return result


def _case(row, native):
    held = next((item["native"] for item in native if row["station"] == item["station"] and row["date"] == item["date"]
                and item["payout_yes"]), None)
    radiation = row["radiation"]
    down, up = (radiation["cell"][name] for name in ("downward_w_m2", "upward_w_m2"))
    baseline, conditional = (row["estimates"][name]["mean_f"] for name in ("ridge", "ridge_net"))
    return {"station": row["station"], "date": row["date"], "at": row["at"],
            "title": f"{row['station']} · Predawn as-issued longwave radiation",
            "detail": (f"The two-hour HRRR forecast gave {down:.1f} W/m² down and {up:.1f} W/m² up at the nearest 3 km cell. "
                       f"Earlier estimated low {baseline:.1f}°F, net-longwave model {conditional:.1f}°F, complete CLI low {row['actual_f']}°F."),
            **({"venue": "polymarket-us", "instrument_id": held["instrument_id"], "market": held} if held else {}),
            "radiation": radiation, "official_report": row["official_report"], "estimates": row["estimates"]}


def evaluate(data, inputs):
    path, digest = research_model.freeze_inputs(data, inputs)
    stages = inputs["rows"]
    model = research_model.fit(stages["fit"], stages["calibration"], methods=METHODS, feature_fn=_features)
    errors = defaultdict(list)
    for row in stages["calibration"]:
        errors[row["station"]].append((row["actual_f"] - row["means"]["txn"]) ** 2)
    widths = {station: max(0.75, sqrt(mean(squared))) for station, squared in errors.items()}
    groups = {phase: research_model_night_cloud._score(rows, model, widths) for phase, rows in stages.items()
              if phase not in ("fit", "calibration")}
    metrics, uncertainty = [], []
    for phase, result in groups.items():
        metrics.extend(research_model.metrics(result, phase, METHODS))
        if result:
            uncertainty.extend({"group": phase, **research_model.date_bootstrap(result, "ridge", method, "brier")}
                               for method in ("ridge_down", "ridge_net"))
    recent = groups["later"]
    actual = research_model_night_cloud._native(data, recent)
    if actual:
        metrics.extend({"method": method, "label": name, "group": "Later actual payouts",
                        "calendar_dates": len({row["date"] for row in actual}),
                        "station_days": len({(row["station"], row["date"]) for row in actual}), "bucket_cases": len(actual),
                        "bucket_brier": mean((row["probabilities"][method] - row["payout_yes"]) ** 2 for row in actual)}
                       for method, name in METHODS.items())
    witnesses = sorted(recent, key=lambda row: row["estimates"]["ridge"]["brier"] - row["estimates"]["ridge_net"]["brier"])
    cases = [_case(row, actual) for row in (witnesses[0], witnesses[-1])] if witnesses else []
    record = {"version": VERSION, "title": "As-issued HRRR surface longwave radiation and the daily low",
              "hypothesis": "Net longwave loss can explain differing overnight cooling after accounting for the station temperature, remaining forecast and observed clouds.",
              "target": "Exact-station Central Park and Midway air-temperature minima",
              "venues": ["polymarket-us"] if actual else [], "date_window": inputs["windows"],
              "coverage": {"row_counts": {phase: research_model._independent_counts(rows) for phase, rows in stages.items()},
                           "requested_pair_frames": inputs["requested_pair_frames"], "issued_pair_frames": inputs["issued_pair_frames"]},
              "metrics": metrics, "uncertainty": uncertainty, "cases": cases, "missing": inputs["gaps"],
              "limits": ["Each HRRR value is on the native Lambert 3 km grid from the two-hour forecast valid at the fixed 03:00 local-standard decision.",
                         "Both instantaneous radiation directions share the same NOAA object and cell. Their source availability and local download clock are preserved.",
                         "The weather model's longwave forecast is an energy flux, distinct from the station air measurement and the satellite skin retrieval.",
                         "The original observations and fixed earlier fit are identical across arms. The historical station observation clock assumption remains explicit.",
                         "Binary market scores use verified payouts separate from the weather comparison."],
              "provenance": {"frozen_input": str(path.relative_to(data)), "frozen_input_sha256": digest,
                             "baseline": inputs["source"], "documentation": expansion_hrrr_longwave.DOCS,
                             "source_sha256": research_model.run_sources(sys.modules[__name__], expansion_hrrr_longwave, research_model_night_cloud)}}
    return research_model.write_record(data, record, {"weather": groups, "native": actual})


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
