"""Increase earlier high-temperature tail support with frozen walk-forward residuals."""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date, timedelta
import gzip
import json
from pathlib import Path
from statistics import mean
import sys

from . import research_model, research_model_high_receipts, research_model_high_tails, research_model_high_transfer
from .model import _limits, _probability
from .nbm import utc

VERSION = "daily-high-rolling-residual-calibration-v1"
FIRST = date(2026, 7, 7)
LAST = date(2026, 7, 31)
METHODS = {"gaussian": "Earlier calibrated Gaussian",
           "recent_errors": "Earlier 14-date residual calibration",
           "rolling_errors": "Walk-forward July plus earlier August residual calibration"}


def _rolling(source):
    rolling = defaultdict(list)
    fold_notes = []
    for lead in range(0, (LAST - FIRST).days + 1, 7):
        first = FIRST + timedelta(days=lead)
        through = min(first + timedelta(days=6), LAST)
        held = [row for row in source if first <= date.fromisoformat(row["date"]) <= through]
        before = min(utc(row["at"]) for row in held)
        older = [row for row in source if row["date"] < str(first) and utc(row["official_report"]["published_at"]) < before]
        width_start = first - timedelta(days=8)
        fit = [row for row in older if row["date"] < str(width_start)]
        widths = [row for row in older if row["date"] >= str(width_start)]
        model = research_model_high_transfer._fit({"fit": fit, "calibration": widths})
        guesses = research_model_high_transfer._points(model, held)["nonlinear"]
        for row, point in zip(held, guesses, strict=True):
            rolling[row["hour"]].append({"station": row["station"], "date": row["date"], "error_f": row["actual_f"] - point})
        fold_notes.append({"held_date_start": str(first), "held_date_end": str(through), "held_first_decision_at": min(row["at"] for row in held),
                           "last_fit_station_day": max(row["date"] for row in fit), "fit_station_days": research_model._independent_counts(fit)["station_days"],
                           "later_eligible_residual_rows": len(held)})
    return dict(rolling), fold_notes


def gather(data, baseline):
    inputs = json.loads(gzip.decompress(baseline.read_bytes()))
    if inputs["version"] != research_model_high_receipts.VERSION:
        raise ValueError("Expected the frozen, native-receipt high forecast replay")
    recent = research_model_high_tails._calibration(inputs["fit"])
    earlier, folds = _rolling(inputs["fit"]["fit"])
    return {"version": VERSION, "reference": research_model._source_entry(data, baseline),
            "cohorts": inputs["cohorts"], "fit": inputs["fit"], "rolling": earlier, "recent": recent, "folds": folds}


def _odds(limits, center, width, recent, combined):
    smooth = research_model_high_tails.SMOOTH_F
    return {"gaussian": _probability(limits, [center], width),
            "recent_errors": _probability(limits, [center + residual for residual in recent], smooth),
            "rolling_errors": _probability(limits, [center + residual for residual in combined], smooth)}


def _scores(rows, recent, combined):
    outcome = []
    for row in rows:
        model = row["estimates"]["receipt_nonlinear"]
        probability = [_odds(_limits(bucket), model["mean_f"], model["scale_f"], recent[row["hour"]], combined[row["hour"]])
                       for bucket in row["buckets"]]
        by_method = {}
        for method in METHODS:
            values = [chances[method] for chances in probability]
            by_method[method] = {**model, "probabilities": values,
                "brier": mean((chance - actual) ** 2 for chance, actual in zip(values, row["truth"], strict=True))}
        outcome.append({**row, "estimates": by_method})
    return outcome


def _native(rows, source, recent, combined):
    result = []
    for row in rows:
        key = f"{row['station']}/{row['date']}/{row['hour']}"
        point = row["estimates"]["gaussian"]
        for evidence in source["markets"].get(key, []):
            values = _odds(_limits(evidence["bucket"]), point["mean_f"], point["scale_f"], recent[row["hour"]], combined[row["hour"]])
            result.append({**evidence, "station": row["station"], "date": row["date"], "at": row["at"], "hour": row["hour"],
                           "probabilities": values, "official_report": row["official_report"]})
    return result


def _tails(rows, method):
    calls = list(research_model_high_tails._calls(rows, method))
    return {"method": method, "threshold_calls": len(calls), "calendar_dates": len({row["date"] for row, _, _, _ in calls}),
            "station_days": len({(row["station"], row["date"]) for row, _, _, _ in calls}),
            "wrong_calls": sum(not outcome for _, _, _, outcome in calls),
            "wrong_station_days": len({(row["station"], row["date"]) for row, _, _, paid in calls if not paid})}


def evaluate(data, inputs):
    frozen, digest = research_model.freeze_inputs(data, inputs)
    recent = {int(hour): values for hour, values in inputs["recent"].items()}
    earlier = {int(hour): [row["error_f"] for row in values] for hour, values in inputs["rolling"].items()}
    combined = {hour: [*values, *earlier[hour]] for hour, values in recent.items()}
    metrics, intervals, coverage, details, cases = [], [], {}, {}, []
    for cohort, source in inputs["cohorts"].items():
        paired = _scores(research_model_high_receipts._same_cases(inputs, source), recent, combined)
        native = _native(paired, source, recent, combined)
        metrics.extend(research_model.metrics(paired, cohort + " · same weather cases", METHODS))
        metrics.extend({"group": cohort + " · all native ≥99% calls", "label": label, **_tails(native, method)}
                       for method, label in METHODS.items())
        comparisons = [row for row, _, _, _ in research_model_high_tails._calls(native, "gaussian")]
        if comparisons:
            metrics.extend({"group": cohort + " · fixed Gaussian ≥99% cases", "method": method, "label": title,
                            "bucket_cases": len(comparisons), "calendar_dates": len({row["date"] for row in comparisons}),
                            "bucket_brier": mean((row["probabilities"][method] - row["native_yes"]) ** 2 for row in comparisons)}
                           for method, title in METHODS.items())
        for base, augmented in (("gaussian", "rolling_errors"), ("recent_errors", "rolling_errors")):
            intervals.append({"group": cohort, **research_model.date_bootstrap(paired, base, augmented, "brier")})
        coverage[cohort] = {**research_model._independent_counts(paired), "native_thresholds": len(native)}
        details[cohort] = {"weather": paired, "native": native}
        wrong = [row for row in comparisons if (row["probabilities"]["gaussian"] >= .99) != bool(row["native_yes"])]
        if wrong:
            pick = max(wrong, key=lambda row: abs(row["probabilities"]["rolling_errors"] - row["probabilities"]["gaussian"]))
            cases.append({"station": pick["station"], "date": pick["date"], "at": pick["at"], "venue": "kalshi",
                          "instrument_id": pick["market"]["instrument_id"],
                          "title": f"{pick['station']} · Earlier residual support and a native extreme",
                          "detail": (f"Gaussian YES {pick['probabilities']['gaussian']:.2%}, fourteen-day residual "
                                     f"{pick['probabilities']['recent_errors']:.2%}, widened earlier residual sample "
                                     f"{pick['probabilities']['rolling_errors']:.2%}. YES paid ${pick['native_yes']}."),
                          "evidence": pick})
    clocks = {str(hour): len(values) for hour, values in combined.items()}
    record = {"version": VERSION, "title": "Does a larger earlier residual sample improve near-certain daily highs?",
              "hypothesis": "Additional earlier walk-forward residuals at the same climate hour can make temperature tails more reliable without changing the fitted forecast or training on later dates.",
              "target": "Originally timed daily-high forecasts at nine held-out sites and exact Kalshi payouts",
              "venues": ["kalshi"], "date_window": {"rolling_earlier": [str(FIRST), str(LAST)], "fixed_earlier": ["2026-08-02", "2026-08-15"],
                 "late_summer": ["2026-08-27", "2026-09-21"], "autumn": ["2026-10-29", "2026-11-14"]},
              "coverage": {"earlier_residuals_by_hour": clocks, **coverage}, "metrics": metrics, "contrasts": intervals, "cases": cases,
              "limits": ["The larger calibration sample includes July folds. Every fold is predicted by an earlier model whose source labels were published before that fold.",
                         "The original verified fit and point forecasts are the same for both later tests. The smaller comparison uses only earlier August residuals.",
                         "Some early July fits use less temperature history than the production-length training window. The expanded archive tests whether that penalty is worthwhile.",
                         "The near-99% subset scores identical thresholds selected by the earlier Gaussian. Per-method coverage and wrong calls are a separate diagnostic."],
              "provenance": {"frozen_input": str(frozen.relative_to(data)), "frozen_input_sha256": digest, "earlier_inputs": inputs["reference"],
                             "rolling_folds": inputs["folds"], "source_sha256": research_model.run_sources(sys.modules[__name__],
                                research_model_high_receipts, research_model_high_tails, research_model_high_transfer)}}
    return research_model.write_record(data, record, details)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    options = parser.add_mutually_exclusive_group(required=True)
    options.add_argument("--receipt-input", type=Path)
    options.add_argument("--input", type=Path)
    args = parser.parse_args()
    inputs = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else gather(args.data, args.receipt_input)
    print(evaluate(args.data, inputs))


if __name__ == "__main__":
    main()
