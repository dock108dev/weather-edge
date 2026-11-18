"""Calibrate long-lead station temperature ranges on earlier original NOAA issues."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
import gzip
import hashlib
import json
from pathlib import Path
from statistics import mean
import sys

import numpy as np
from scipy.optimize import brentq
from scipy.special import ndtr

from . import expansion_nbe, expansion_weather, research_model, research_model_extended_temperatures as extended
from .model import _limits, _outcome
from .nbm import iso, utc

VERSION = "nbm-seven-day-calibrated-spread-v1"
EARLIER = tuple(date(2026, 8, 2) + timedelta(days=7 * i) for i in range(9))
LEADS = extended.LEADS
TARGETS = tuple(extended.TARGETS)
METHODS = {"native": "Raw native NBE Gaussian scale", "widened": "Earlier scale adjustment at the same center",
           "empirical": "Earlier scaled forecast-error distribution"}
KERNEL = .35
P80 = 1.2815515655446004


def _lead_group(day):
    return next((first for first, last in LEADS if first <= day <= last), None)


def _provenance(data, record_path):
    record = json.loads(record_path.read_bytes())
    if record["version"] != extended.VERSION:
        raise ValueError("Expected the original retained seven-day weather comparison")
    packed = gzip.decompress((data / record["provenance"]["frozen_input"]).read_bytes())
    if hashlib.sha256(packed).hexdigest() != record["provenance"]["frozen_input_sha256"]:
        raise ValueError("Original extended forecast cohort input digest disagrees")
    return json.loads(packed), research_model._source_entry(data, record_path)


def gather(data, reference):
    later, source = _provenance(data, reference)
    cutoff = min(utc(row["at"]) for row in later["samples"])
    decision_selection = datetime.combine(EARLIER[7], time(15), timezone.utc)
    previous, omitted = {"fit": [], "selection": []}, Counter()
    for station in extended.REFERENCE_STATIONS:
        labels = expansion_weather.daily_labels(data, station, EARLIER[0] + timedelta(days=1), cutoff.date() - timedelta(days=1))
        for origin in EARLIER:
            issue = datetime.combine(origin, time(12), timezone.utc)
            card = expansion_nbe.load_card(data, issue, station=station)
            if not card or not issue <= utc(card["available_at"]) <= issue + timedelta(hours=3):
                omitted["earlier complete published station issue missing"] += 1
                continue
            fit = origin < EARLIER[7]
            for valid in expansion_nbe.daily_extrema(card):
                day = date.fromisoformat(valid["target_date"])
                row = extended._case_row(data, station, card, day, labels, valid)
                if not row or utc(row["official_report"]["published_at"]) >= (decision_selection if fit else cutoff):
                    omitted["earlier exact complete label missing by the next phase"] += 1
                    continue
                previous["fit" if fit else "selection"].append(row)
    return {"version": VERSION, "rows": {**previous, "later": later["samples"]}, "source": source,
            "latest": later["current"], "missing": dict(omitted), "selection_clock": iso(decision_selection), "test_clock": iso(cutoff)}


def _records(rows):
    for row in rows:
        lead = _lead_group(row["lead_day"])
        for target in TARGETS:
            values = row["targets"][target]
            if lead is not None and values["native_stddev_f"] is not None:
                yield {"station": row["station"], "date": row["date"], "at": row["at"], "lead_day": row["lead_day"],
                       "issue": row["issue"], "official_report": row["official_report"], "target": target, "lead_group": lead,
                       "predicted_f": values["nbe"], "actual_f": values["actual"], "sd_f": max(1., values["native_stddev_f"])}


def _fit(rows):
    sample = defaultdict(list)
    for row in _records(rows):
        error = (row["actual_f"] - row["predicted_f"]) / row["sd_f"]
        sample[row["target"], row["lead_group"]].append(error)
    return {key: {"errors": np.asarray(residual), "adjustment": max(.25, float(np.quantile(np.abs(residual), .80)) / P80)}
            for key, residual in sample.items() if len(residual) >= 20}


def _cdf(centers, width, point):
    return float(ndtr((point - centers) / width).mean())


def _arms(row, calibration):
    entry = calibration.get((row["target"], row["lead_group"]))
    if entry is None:
        return None
    center, sd = row["predicted_f"], row["sd_f"]
    return {"native": (np.asarray([center]), sd), "widened": (np.asarray([center]), sd * entry["adjustment"]),
            "empirical": (center + sd * entry["errors"], sd * KERNEL)}


def _interval(centers, width):
    lower = float(centers.min() - 8 * width)
    upper = float(centers.max() + 8 * width)
    return [float(brentq(lambda candidate: _cdf(centers, width, candidate) - quantile, lower, upper)) for quantile in (.1, .9)]


def _predictions(row, calibration):
    arms = _arms(row, calibration)
    if arms is None:
        return None
    buckets = research_model._benchmark_buckets(round(row["predicted_f"]))
    truth = [_outcome(bucket, row["actual_f"]) for bucket in buckets]
    actual = row["actual_f"]
    models = {}
    for method, (centers, width) in arms.items():
        prior = [_cdf(centers, width, hi) - _cdf(centers, width, lo) for lo, hi in map(_limits, buckets)]
        lo, hi = _interval(centers, width)
        score = (hi - lo) + 10 * (max(0, lo - actual) + max(0, actual - hi))
        models[method] = {"range_80": [lo, hi], "observed_in_range_80": lo <= actual <= hi,
                          "range_width_f": hi - lo, "interval_score_f": score, "mean_f": float(centers.mean()),
                          "bucket_brier": mean((p - y) ** 2 for p, y in zip(prior, truth, strict=True))}
    return {**row, "estimates": models}


def _metrics(rows, group, target=None):
    if not rows:
        return []
    return [{"group": group, "label": title, "method": method, "target": target, **research_model._independent_counts(rows),
             "issue_blocks": len({row["issue"]["issued_at"] for row in rows}),
             **{metric: mean(row["estimates"][method][metric] for row in rows)
                for metric in ("observed_in_range_80", "range_width_f", "interval_score_f", "bucket_brier")}}
            for method, title in METHODS.items()]


def _uncertainty(rows, challenger, metric):
    by_issue = defaultdict(list)
    for row in rows:
        left, right = (row["estimates"][key][metric] for key in ("native", challenger))
        by_issue[row["issue"]["issued_at"]].append(right - left)
    changes = [mean(deltas) for deltas in by_issue.values()]
    bootstrap = np.random.default_rng(20261115).choice(changes, size=(3000, len(changes))).mean(axis=1)
    return {"method": challenger, "baseline": "native", "metric": metric, "issue_blocks": len(changes),
            "delta": mean(changes), "issue_block_bootstrap_interval_95": np.quantile(bootstrap, (.025, .975)).tolist()}


def _case(row, selection):
    raw, calibrated = (row["estimates"][name]["range_80"] for name in ("native", selection))
    return {"station": row["station"], "date": row["date"], "at": row["at"],
            "globe_date": extended.globe_date(row["station"], row["at"]), "title": f"{row['station']} · A long-lead temperature range",
            "detail": (f"At lead day {row['lead_day']}, the native {row['target']} forecast was {row['predicted_f']}°F. "
                       f"The complete climate report said {row['actual_f']}°F. The raw 80% range was {raw[0]:.0f}–{raw[1]:.0f}°F and the "
                       f"earlier calibrated range was {calibrated[0]:.0f}–{calibrated[1]:.0f}°F."), "station_evidence": row}


def _live_ranges(current, calibration, method):
    examples = []
    for station in ("KPDX", "KBUF"):
        outlook = current.get(station)
        if outlook is None or len(outlook["daily_extrema"]) < 7:
            continue
        latest = outlook["daily_extrema"][-1]
        low = latest["minimum"]
        if low["sd_f"] is None:
            continue
        arms = _arms({"target": "minimum", "lead_group": _lead_group(latest["lead_day"]),
                      "predicted_f": low["temp_f"], "sd_f": max(1., low["sd_f"])}, calibration)
        if arms is None:
            continue
        before, after = map(lambda name: _interval(*arms[name]), ("native", method))
        examples.append({"station": station, "date": latest["target_date"], "at": outlook["knowledge_at"], "mode": "live",
                         "globe_date": extended.globe_date(station, outlook["knowledge_at"]),
                         "title": f"{station} · Forecast-only outlook at lead day {latest['lead_day']}",
                         "detail": (f"The retained guidance low is {low['temp_f']}°F, with a raw 80% range of {before[0]:.0f}–{before[1]:.0f}°F. "
                                    f"The experimental range transferred from the four earlier stations is {after[0]:.0f}–{after[1]:.0f}°F."),
                         "outlook": outlook, "raw_interval_f": before, "research_interval_f": after})
    return examples


def evaluate(data, inputs):
    if inputs["version"] != VERSION:
        raise ValueError("Unexpected extended forecast calibration inputs")
    frozen, digest = research_model.freeze_inputs(data, inputs)
    earlier = _fit(inputs["rows"]["fit"])
    if not earlier:
        raise ValueError("No earlier independent forecast errors for the temperature bands")
    selection, later = ([predicted for row in _records(inputs["rows"][phase]) if (predicted := _predictions(row, earlier))]
                        for phase in ("selection", "later"))
    if not selection or not later:
        raise ValueError("Need differently dated interval selection and verification cases")
    selected = min(METHODS, key=lambda method: mean(row["estimates"][method]["interval_score_f"] for row in selection))
    metrics = [*_metrics(selection, "Earlier interval selection"), *_metrics(later, "Later common weather cases")]
    for first, last in LEADS:
        for target in TARGETS:
            metrics.extend(_metrics([row for row in later if row["lead_group"] == first and row["target"] == target],
                                    f"Later lead days {first}–{last}", target))
    witness = max(later, key=lambda row: abs(row["actual_f"] - row["predicted_f"]))
    record = {"version": VERSION, "title": "Can earlier errors calibrate seven-day thermometer forecasts?",
              "hypothesis": "A scaled distribution of earlier as-issued errors can correct undercoverage in the native extended temperature ranges.",
              "target": "Exact raw climate-day minima and maxima, holding the underlying NOAA forecast fixed",
              "venues": [], "date_window": {"fit_origins": [str(EARLIER[0]), str(EARLIER[6])], "selection_origins": [str(EARLIER[7]), str(EARLIER[8])],
                                          "first_held_decision": inputs["test_clock"]},
              "coverage": {"fit_station_days": len(inputs["rows"]["fit"]), "calibration_error_counts": {f"{target}/{lead}": len(values["errors"])
                                                                                            for (target, lead), values in earlier.items()},
                           "selection": research_model._independent_counts(selection), "later": research_model._independent_counts(later)},
              "selected_in_earlier_interval_test": selected, "metrics": metrics, "cases": [_case(witness, selected), *_live_ranges(inputs["latest"], earlier, selected)],
              "uncertainty": [_uncertainty(later, method, metric) for method in ("widened", "empirical")
                              for metric in ("interval_score_f", "bucket_brier")],
              "limits": ["The early normalized error pools are grouped by temperature minimum or maximum and the published forecast lead. Native standard deviations below 1°F use a fixed floor in all arms.",
                         "Candidate ranges use the same originally visible forecast and exact final raw station label. The later test shares six weekly issuance blocks. The climate-date candidate errors are dependent across nearby stations and lead dates.",
                         "The interval score rewards shorter intervals and penalizes observations outside the range. Distribution scores use the same fixed whole-degree comparison buckets in all arms.",
                         "The Portland and Buffalo context transfers earlier errors from four other stations. Those sites did not fit or independently verify the calibration."],
              "missing": inputs["missing"], "provenance": {"frozen_input": str(frozen.relative_to(data)), "frozen_input_sha256": digest,
                       "earlier_outlook_reference": inputs["source"], "source_sha256": research_model.run_sources(sys.modules[__name__], extended, expansion_nbe)}}
    return research_model.write_record(data, record, {"selection": selection, "later": later})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--outlook-study", type=Path)
    source.add_argument("--input", type=Path)
    args = parser.parse_args()
    inputs = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else gather(args.data, args.outlook_study)
    print(evaluate(args.data, inputs))


if __name__ == "__main__":
    main()
