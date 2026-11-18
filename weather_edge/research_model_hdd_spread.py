"""Test if the native NBM extrema spread improves Cincinnati weekly HDD distributions."""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date, timedelta
import gzip
import hashlib
import json
from math import sqrt
from pathlib import Path
from statistics import mean
import sys

import numpy as np

from . import expansion_nbe, research_model, research_model_hdd as hdd, research_model_hdd_curve as curve
from . import research_model_hdd_forecast as extended

VERSION = "cincinnati-hdd-extended-spread-v1"
SPLITS = {"earlier fit": (2020, 2022), "selection": (2023, 2023), "later verified weeks": (2024, 2026)}
METHODS = {"calendar": "Earlier same-date HDD distribution", "fixed": "Earlier errors with fixed weekly scaling",
           "independent": "Earlier errors with root-sum-squares native XND scaling",
           "correlated": "Earlier errors scaled by the sum of native daily XND spreads"}
LEVELS = (50, 100, 150, 200, 250)
SOURCE_DOC = "https://vlab.noaa.gov/web/mdl/nbm-textcard-v5.0"


def _spreads(forecasts):
    if any(row["low"]["sd_f"] is None or row["high"]["sd_f"] is None for row in forecasts):
        return None
    daily = [sqrt(row["low"]["sd_f"] ** 2 + row["high"]["sd_f"] ** 2) / 2 for row in forecasts]
    return {"daily_temperature_spread_f": daily, "independent": max(1.0, sqrt(sum(value ** 2 for value in daily))),
            "correlated": max(1.0, sum(daily))}


def _retained_inputs(data, record_path):
    record = json.loads(record_path.read_bytes())
    if record["version"] != extended.VERSION:
        raise ValueError("Expected the completed Cincinnati extended forecast record")
    frozen = gzip.decompress((data / record["provenance"]["frozen_input"]).read_bytes())
    if hashlib.sha256(frozen).hexdigest() != record["provenance"]["frozen_input_sha256"]:
        raise ValueError("Earlier Cincinnati forecast input digest disagrees")
    return json.loads(frozen), research_model._source_entry(data, record_path)


def gather(data, record_path):
    baseline, provenance = _retained_inputs(data, record_path)
    acis, receipt = curve._acis(data)
    labels = curve._acis_days(acis)
    weather, missing = [], []
    days = [date(year, 10, 5) + timedelta(days=7 * i) for year in range(2020, 2027) for i in range(5 if year == 2026 else 11)]
    for day in days:
        forecast = extended._origin(data, day)
        first, last = day + timedelta(days=1), day + timedelta(days=7)
        actual = extended._observed(labels, first, last)
        spread = _spreads(forecast["daily"]) if forecast else None
        if not forecast or not spread or actual is None:
            missing.append({"date": str(day), "reason": "Missing complete native forecast, XND or seven settled physical labels"})
            continue
        historical = [values for year in range(1986, 2020)
                      if (values := extended._observed(labels, first.replace(year=year), last.replace(year=year))) is not None]
        weather.append({**forecast, **spread, "actual_daily_hdd": actual, "actual": sum(actual),
                        "calendar_seven_day_samples": [sum(week) for week in historical]})
    return {"version": VERSION, "weeks": weather, "missing": missing, "live": baseline["live"], "forecast": baseline["forecast"],
            "source_study_path": provenance, "climate_source": receipt, "as_of": baseline["as_of"],
            "historical_quarters": baseline["limited_acis_climate_seasons"]}


def _error_samples(row, earlier, method):
    if method == "fixed":
        return [prior["actual"] - prior["forecast_hdd"] for prior in earlier]
    return [(prior["actual"] - prior["forecast_hdd"]) * (row[method] / prior[method]) for prior in earlier]


def _measure(samples, actual):
    values = np.asarray(samples, dtype=float)
    average = float(values.mean())
    lo, hi = np.quantile(values, (.1, .9)).tolist()
    crps = np.abs(values - actual).mean() - .5 * np.abs(values[:, None] - values).mean()
    return {"crps_hdd": float(crps), "mae_hdd": abs(average - actual), "range80_contains_actual": lo <= actual <= hi,
            "range80_width_hdd": hi - lo, "point_hdd": average, "forecast_interval_80": [lo, hi],
            "bucket_brier": mean((float((values < strike).mean()) - (actual < strike)) ** 2 for strike in LEVELS)}


def _evaluate(rows, earlier):
    results = []
    for row in rows:
        samples = {"calendar": row["calendar_seven_day_samples"],
                   **{name: [max(0, row["forecast_hdd"] + error) for error in _error_samples(row, earlier, name)]
                      for name in ("fixed", "independent", "correlated")}}
        scored = {key: _measure(values, row["actual"]) for key, values in samples.items()}
        results.append({**row, "estimates": scored})
    return results


def _summary(rows, group):
    return [{"group": group, "method": method, "label": label, "weekly_origins": len(rows), "calendar_dates": len({row["date"] for row in rows}),
             "independent_seasons": len({row["year"] for row in rows}), "complete_station_days": 7 * len(rows),
             **{score: mean(row["estimates"][method][score] for row in rows)
                for score in ("crps_hdd", "mae_hdd", "range80_contains_actual", "range80_width_hdd", "bucket_brier")}}
            for method, label in METHODS.items()] if rows else []


def _compare(rows, method, group):
    per_year = defaultdict(list)
    for row in rows:
        per_year[row["year"]].append(row["estimates"][method]["crps_hdd"] - row["estimates"]["fixed"]["crps_hdd"])
    year_means = [mean(scores) for scores in per_year.values()]
    sampled = np.random.default_rng(20261115).choice(year_means, size=(3000, len(year_means))).mean(axis=1)
    return {"group": group, "method": method, "baseline": "fixed", "independent_seasons": len(year_means),
            "delta_crps_hdd": mean(year_means), "year_bootstrap_interval_95": np.quantile(sampled, (.025, .975)).tolist()}


def _current(inputs, earlier, winner):
    opening = inputs["forecast"]
    spreads = _spreads(opening["daily"])
    if not spreads:
        return [], None
    live = inputs["live"]
    if len(live["received_days"]) != live["expected_days"]:
        return [], None
    available = [row for row in live["markets"] if row["status"] == "open"]
    if not available:
        return [], None
    options = sorted({float(row["upper"]) for row in available})
    current = {**opening, **spreads}
    origin, through = (date.fromisoformat(value) for value in (current["date"], live["through"]))
    models = {}
    for name in ("fixed", "independent", "correlated"):
        residual = _error_samples(current, earlier, name)
        cases = [max(0, opening["forecast_hdd"] + error) for error in residual]
        models[name] = {"weekly_80_interval_hdd": np.quantile(cases, (.1, .9)).tolist(), "weekly_center_hdd": mean(cases),
                        "quarter": extended._scenario(inputs["historical_quarters"], origin, through, live["total"], opening["forecast_hdd"], residual, options)}
    selected = models[winner]["quarter"]["extended_forecast"]
    chosen = min(available, key=lambda row: abs(float(row["upper"]) - selected["point_hdd"]))
    index = options.index(float(chosen["upper"]))
    baseline, adjusted = (models[name]["quarter"]["extended_forecast"]["probabilities"][index] for name in ("fixed", winner))
    return [{"station": hdd.STATION, "date": live["through"], "at": inputs["as_of"], "venue": "kalshi",
             "instrument_id": chosen["instrument_id"], "title": "Cincinnati · Weekly forecast uncertainty in the heating quarter",
             "detail": (f"The same raw week is {opening['forecast_hdd']:.0f} HDD. A fixed error distribution gave an 80% interval of "
                        f"{models['fixed']['weekly_80_interval_hdd'][0]:.0f}–{models['fixed']['weekly_80_interval_hdd'][1]:.0f} HDD, "
                        f"and the selected calibration gives {models[winner]['weekly_80_interval_hdd'][0]:.0f}–{models[winner]['weekly_80_interval_hdd'][1]:.0f}. "
                        f"The earlier and selected below-{float(chosen['upper']):g} probabilities are {baseline:.1%} and {adjusted:.1%}."),
             "market": chosen, "current_guidance": current, "current_research_distributions": models,
             "selected_at_earlier_dates": winner}], models


def evaluate(data, inputs):
    if inputs["version"] != VERSION:
        raise ValueError("Unexpected Cincinnati spread inputs")
    frozen, digest = research_model.freeze_inputs(data, inputs)
    cohorts = {name: [row for row in inputs["weeks"] if first <= row["year"] <= last] for name, (first, last) in SPLITS.items()}
    fit = cohorts["earlier fit"]
    selection = _evaluate(cohorts["selection"], fit)
    later = _evaluate(cohorts["later verified weeks"], fit)
    if not fit or not selection or not later:
        raise ValueError("Independent earlier fit, selection and later verified weeks are required")
    selected = min(("fixed", "independent", "correlated"), key=lambda name: mean(row["estimates"][name]["crps_hdd"] for row in selection))
    cases, current = _current(inputs, fit, selected)
    groups = {"Earlier selection": selection, "Later verified weeks": later,
              "2026 verified weather only": [row for row in later if row["year"] == 2026]}
    record = {"version": VERSION, "title": "Does NOAA's native temperature spread improve weekly heating uncertainty?",
              "hypothesis": "The extended station bulletin's daily maximum and minimum standard deviations may predict when a weekly HDD forecast needs a wider error distribution.",
              "target": "Same-station seven-day HDD distributions and a research-only open Cincinnati quarter scenario",
              "venues": ["kalshi"], "date_window": SPLITS, "selected_on_earlier_dates": selected,
              "coverage": {phase: {"weekly_origins": len(rows), "independent_years": len({row["year"] for row in rows}),
                                    "complete_station_days": 7 * len(rows)} for phase, rows in cohorts.items()},
              "metrics": [metric for title, rows in groups.items() for metric in _summary(rows, title)],
              "uncertainty": [_compare(later, method, "Later verified weeks") for method in ("independent", "correlated")],
              "cases": cases, "limits": ["NOAA XND is the native maximum/minimum temperature standard deviation. Combining the two equally before aggregating assumes an approximate conversion to degree-day uncertainty.",
                                         "One arm adds variances across days and one treats daily scales as fully correlated. These are alternative scaling assumptions, with historical error calibration in both.",
                                         "The selected arm uses 2023 forecasts exclusively. Earlier forecast errors fit the distributions. The 2024–2026 weather weeks are untouched and calendar quarters are used as uncertainty clusters.",
                                         "The exact active quarter is unresolved. The current estimate depends on historical analog quarters and is retained only as research context."],
              "missing": inputs["missing"], "provenance": {"frozen_input": str(frozen.relative_to(data)), "frozen_input_sha256": digest,
                   "earlier_current_projection": inputs["source_study_path"], "climate_source": inputs["climate_source"],
                   "nbm_documentation": SOURCE_DOC, "source_sha256": research_model.run_sources(sys.modules[__name__], expansion_nbe, extended, curve)}}
    return research_model.write_record(data, record, {"selection": selection, "later": later, "current": current})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--forecast-study", type=Path)
    source.add_argument("--input", type=Path)
    args = parser.parse_args()
    inputs = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else gather(args.data, args.forecast_study)
    print(evaluate(args.data, inputs))


if __name__ == "__main__":
    main()
