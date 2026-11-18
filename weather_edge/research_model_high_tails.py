"""Replay exact daily-high tails with earlier residual shapes and verified payouts."""

from __future__ import annotations

import argparse
import gzip
import json
from math import sqrt
from pathlib import Path
from statistics import mean
import sys

from scipy import __version__ as scipy_version
from scipy.special import stdtr

from . import research_model, research_model_high_receipts, research_model_high_transfer as transfer
from .model import _limits, _probability

VERSION = "daily-high-calibrated-tails-transfer-v1"
DF = 5
NEAR_CERTAIN = 0.99
SMOOTH_F = 0.45
METHODS = {"gaussian": "Calibrated Gaussian width", "student": "Same-variance Student t",
           "residual": "Smoothed earlier clock-specific residuals", "blend": "Equal Gaussian and residual mixture"}


def _frozen(data, receipt_input):
    reference = json.loads(gzip.decompress(receipt_input.read_bytes()))
    if reference["version"] != research_model_high_receipts.VERSION:
        raise ValueError("Expected frozen original high receipt replay")
    return {"version": VERSION, "receipt_reference": research_model._source_entry(data, receipt_input),
            "cohorts": reference["cohorts"], "fit": reference["fit"]}


def _distribution(bounds, center, width, errors):
    lower, upper = bounds
    t_width = width * sqrt((DF - 2) / DF)
    low, high = (lower - center) / t_width, (upper - center) / t_width
    student = float(stdtr(DF, -low) - stdtr(DF, -high) if low > 0 else stdtr(DF, high) - stdtr(DF, low))
    gaussian = _probability(bounds, [center], width)
    residual = _probability(bounds, [center + error for error in errors], SMOOTH_F)
    return {"gaussian": gaussian, "student": student, "residual": residual, "blend": (gaussian + residual) / 2}


def _evaluate_weather(rows, residuals):
    output = []
    for row in rows:
        model = row["estimates"]["receipt_nonlinear"]
        base, width = model["mean_f"], model["scale_f"]
        distributions = [_distribution(_limits(bucket), base, width, residuals[row["hour"]]) for bucket in row["buckets"]]
        estimates = {}
        for method in METHODS:
            chances = [item[method] for item in distributions]
            estimates[method] = {**model, "probabilities": chances,
                                 "brier": mean((p - actual) ** 2 for p, actual in zip(chances, row["truth"], strict=True))}
        output.append({**row, "estimates": estimates, "center_f": base, "width_f": width})
    return output


def _native(rows, markets, residuals):
    output = []
    for row in rows:
        key = f"{row['station']}/{row['date']}/{row['hour']}"
        for native in markets.get(key, []):
            probabilities = _distribution(_limits(native["bucket"]), row["center_f"], row["width_f"], residuals[row["hour"]])
            output.append({**native, "station": row["station"], "date": row["date"], "at": row["at"], "hour": row["hour"],
                           "center_f": row["center_f"], "width_f": row["width_f"], "forecast": row["forecast"],
                           "observation": row["latest_observation"], "official_report": row["official_report"],
                           "probabilities": probabilities})
    return output


def _calls(rows, method):
    for row in rows:
        chance = row["probabilities"][method]
        if chance >= NEAR_CERTAIN:
            yield row, "YES", chance, row["native_yes"]
        if chance <= 1 - NEAR_CERTAIN:
            yield row, "NO", 1 - chance, 1 - row["native_yes"]


def _tail_metrics(rows, label):
    output = []
    for method, title in METHODS.items():
        calls = list(_calls(rows, method))
        if not calls:
            continue
        events = {(row["station"], row["date"]) for row, _, _, _ in calls}
        misses = {(row["station"], row["date"]) for row, _, _, outcome in calls if not outcome}
        output.append({"method": method, "label": title, "group": label, "near_certainty_calls": len(calls),
                       "calendar_dates": len({day for _, day in events}), "station_days": len(events),
                       "wrong_side_calls": sum(not outcome for _, _, _, outcome in calls),
                       "station_days_with_a_wrong_call": len(misses), "mean_probability": mean(p for _, _, p, _ in calls),
                       "observed_correct_side_rate": mean(y for _, _, _, y in calls)})
    return output


def _native_metrics(rows, label):
    if not rows:
        return []
    return [{"method": method, "label": title, "group": label,
             "calendar_dates": len({row["date"] for row in rows}), "station_days": len({(row["station"], row["date"]) for row in rows}),
             "bucket_cases": len(rows), "bucket_brier": mean((row["probabilities"][method] - row["native_yes"]) ** 2 for row in rows)}
            for method, title in METHODS.items()]


def _calibration(earlier):
    base = transfer._fit(earlier)
    calibrating = earlier["calibration"]
    points = transfer._points(base, calibrating)
    return {group[1]: values for group, values in transfer._residuals(calibrating, points, lambda row: (row["hour"],)).items()
            if group[0] == "nonlinear"}


def _native_case(row, side, chance):
    show = row["probabilities"]
    probabilities = show if side == "YES" else {key: 1 - value for key, value in show.items()}
    return {"station": row["station"], "date": row["date"], "at": row["at"], "venue": "kalshi",
            "instrument_id": row["market"]["instrument_id"], "title": f"{row['station']} · A near-certain temperature call",
            "detail": (f"The Gaussian assigned {chance:.2%} to {side}; the empirical mixture assigned {probabilities['blend']:.2%}. "
                       f"Exact weather was {row['official_report']['high_f']}°F and YES paid ${row['native_yes']}."),
            "probabilities_of_selected_side": probabilities, "side": side, "native_threshold": row}


def evaluate(data, inputs):
    frozen, digest = research_model.freeze_inputs(data, inputs)
    calibration = _calibration(inputs["fit"])
    metrics, intervals, cases, coverage, details = [], [], [], {}, {}
    for label, source in inputs["cohorts"].items():
        same = research_model_high_receipts._same_cases(inputs, source)
        scored = _evaluate_weather(same, calibration)
        native = _native(scored, source["markets"], calibration)
        base_tails = list(_calls(native, "gaussian"))
        common_tail = [row for row, _, _, _ in base_tails]
        metrics.extend(research_model.metrics(scored, f"{label} · same weather cases", METHODS))
        metrics.extend(_native_metrics(native, f"{label} · native contract thresholds"))
        metrics.extend(_native_metrics(common_tail, f"{label} · fixed Gaussian near-99% cases"))
        metrics.extend(_tail_metrics(native, f"{label} · every near-99% call by model"))
        for hour in (3, 9, 13, 17, 21):
            metrics.extend(_tail_metrics([row for row in native if row["hour"] == hour], f"{label} · {hour:02} LST near-99%"))
        if scored:
            intervals.extend({"group": label, **research_model.date_bootstrap(scored, "gaussian", method, "brier")}
                             for method in ("student", "residual", "blend"))
        failures = sorted((row for row in base_tails if not row[3]), key=lambda row: row[2], reverse=True)
        examples = failures[:1] if failures else sorted(base_tails, key=lambda row: abs(row[0]["probabilities"]["blend"] -
                                                                                     row[0]["probabilities"]["gaussian"]), reverse=True)[:1]
        cases.extend(_native_case(row, side, chance) for row, side, chance, _ in examples)
        coverage[label] = {**research_model._independent_counts(scored), "native_thresholds": len(native),
                           "fixed_gaussian_tail_thresholds": len(common_tail)}
        details[label] = {"weather": scored, "native": native}
    older = inputs["fit"]["calibration"]
    record = {"version": VERSION, "title": "Do heavier temperature tails improve the transferred daily-high model?",
              "hypothesis": "A finite earlier residual distribution or a heavier parametric tail may improve the same held-out daily-high probabilities and reduce very high-confidence errors.",
              "target": "Distinct exact-station daily-high reports and independently settled Kalshi thresholds",
              "venues": ["kalshi"], "date_window": {"fit": ["2026-06-01", "2026-07-31"], "residual_calibration": ["2026-08-02", "2026-08-15"],
                 "warm": ["2026-08-27", "2026-09-21"], "cool": ["2026-10-29", "2026-11-14"]},
              "coverage": {"earlier_width_sample": research_model._independent_counts(older),
                           "earlier_residuals_per_local_clock": {str(hour): len(values) for hour, values in calibration.items()}, **coverage},
              "metrics": metrics, "uncertainty": intervals, "cases": cases,
              "limits": ["Every probability distribution uses the same earlier fitted nonlinear center. The Student t keeps the calibrated Gaussian variance with five degrees of freedom.",
                         "The empirical arm mixes normal kernels centered on earlier signed errors at each local-standard clock. Its fixed smoothing is 0.45°F.",
                         "Held-out features use original matching NOAA MADIS receipts. Earlier training and residual calibration retain the ten-minute observation timing assumption.",
                         "The independent test reporting counts calendar days alongside overlapping clocks and strike thresholds. A near-99% forecast can lack earlier observations sufficient to establish a one-percent error rate.",
                         "The score on fixed baseline tail calls keeps cases identical. The threshold-dependent diagnostic reports actual call counts and failures separately."],
              "provenance": {"frozen_input": str(frozen.relative_to(data)), "frozen_input_sha256": digest, "scipy": scipy_version,
                             "original_receipts": inputs["receipt_reference"],
                             "source_sha256": research_model.run_sources(sys.modules[__name__], transfer, research_model_high_receipts)}}
    return research_model.write_record(data, record, {"residuals": calibration, "cohorts": details})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--receipt-input", type=Path)
    source.add_argument("--input", type=Path)
    args = parser.parse_args()
    corpus = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else _frozen(args.data, args.receipt_input)
    print(evaluate(args.data, corpus))


if __name__ == "__main__":
    main()
