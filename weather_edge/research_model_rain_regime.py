"""Test earlier cold-period guidance against the warm-only measurable-rain fit."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import date
import gzip
import json
from pathlib import Path
from statistics import mean
import sys

from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from . import expansion_contract, expansion_nbm, expansion_weather, research_model
from . import research_model_rain_warm, research_model_rain_windows as windows
from .nbm import climate_window, utc

VERSION = "daily-rain-earlier-regime-calibration-v1"
OLD = ((date(2025, 9, 1), date(2025, 12, 15)), (date(2026, 1, 1), date(2026, 1, 31)),
       (date(2026, 4, 1), date(2026, 4, 30)))
METHODS = {"p06": "Published P06 aggregation", "warm_six": "Calibrated on six warm-weather stations",
           "warm_two": "Calibrated on two warm-weather stations", "multi_two": "Earlier seasons at the same two stations",
           "multi_six": "All earlier seasons", "dewpoint": "All earlier seasons plus forecast dew-point depression"}


def _air(data, row):
    station = row["station"]
    start, end = climate_window(row["date"], expansion_weather.STATIONS[station][3])
    card = expansion_nbm.load_card(data, station, utc(row["forecast"]["issued_at"]))
    if not card or card["card_sha256"] != row["forecast"]["card_sha256"]:
        return None
    relevant = [(point["TMP"], point["DPT"]) for point in card["points"] if "TMP" in point and "DPT" in point
                and start <= utc(point["valid_at"]) < end]
    if len(relevant) < 6:
        return None
    return {**row, "mean_forecast_depression_f": mean(max(0, air - dew) for air, dew in relevant),
            "mean_forecast_air_f": mean(air for air, _ in relevant), "forecast_thermal_points": len(relevant)}


def _older(data):
    result, gaps = [], Counter()
    for station in research_model.STATIONS:
        for start, end in OLD:
            local = expansion_weather.cli_file(data, station, start.year)
            if not local.is_file():
                gaps[f"{station}: no retained earlier climate summary"] += 1
                continue
            labels = expansion_weather.daily_labels(data, station, start, end)
            for day in research_model._days(start, end):
                label = labels.get(str(day))
                amount = label["precip_as_reported"] if label else None
                if amount in (None, "M", "MM") or (wet := expansion_contract.rain_outcome(amount)) is None:
                    gaps["earlier complete summary missing"] += 1
                    continue
                raw = windows._forecast(data, station, day)
                if not raw or not (ready := _air(data, raw)):
                    gaps["earlier issued model missing"] += 1
                    continue
                result.append({**ready, "truth": wet, "official_report": label})
    return result, dict(gaps)


def gather(data, baseline):
    source = json.loads(gzip.decompress(baseline.read_bytes()))
    if source["version"] != research_model_rain_warm.VERSION:
        raise ValueError("Expected exact rain cross-season inputs")
    older, gaps = _older(data)
    warm = [sample for row in source["fit"] if (sample := _air(data, row))]
    controls = [row for row in warm if row["station"] in research_model.STATIONS]
    observations = {name: [sample for row in values if (sample := _air(data, row))] for name, values in source["weather"].items()}
    payouts = {name: [sample for row in values if (sample := _air(data, row))] for name, values in source["native"].items()}
    return {"version": VERSION, "samples": {"warm_six": warm, "warm_two": controls, "multi_two": [*older, *controls],
                                           "multi_six": [*older, *warm], "dewpoint": [*older, *warm]},
            "observations": observations, "payouts": payouts, "older_missing": gaps,
            "reference": research_model._source_entry(data, baseline)}


def _features(row, name):
    base = windows._features(row, ("p06",))
    return [*base, row["mean_forecast_depression_f"]] if name == "dewpoint" else base


def _evaluate(rows, arms):
    if not rows:
        return []
    predictions = {name: estimator.predict_proba([_features(row, name) for row in rows])[:, 1] for name, estimator in arms.items()}
    return [{**row, "probabilities": {"p06": row["direct"]["p06"], **{name: float(values[index]) for name, values in predictions.items()}}}
            for index, row in enumerate(rows)]


def _metrics(rows, phase, price=False):
    if not rows:
        return []
    methods = {**METHODS, "midpoint": "Earlier native displayed midpoint"} if price else METHODS
    return [{"method": name, "label": title, "group": phase, **research_model._independent_counts(rows),
             "liquid_rain_days": sum(row["truth"] for row in rows),
             "bucket_brier": mean(((row["market_midpoint"] if name == "midpoint" else row["probabilities"][name]) - row["truth"]) ** 2
                                  for row in rows)} for name, title in methods.items()]


def evaluate(data, inputs):
    frozen, digest = research_model.freeze_inputs(data, inputs)
    samples = inputs["samples"]
    arms = {}
    for name, rows in samples.items():
        model = make_pipeline(StandardScaler(), LogisticRegression(C=1.0))
        model.fit([_features(row, name) for row in rows], [row["truth"] for row in rows])
        arms[name] = model
    metrics, comparisons, cases, details = [], [], [], {}
    for name in research_model_rain_warm.SEASONS:
        rows = inputs["observations"][name]
        scored = _evaluate(rows, arms)
        native = _evaluate(inputs["payouts"][name], arms)
        priced = [row for row in native if row["market_midpoint"] is not None]
        metrics.extend(_metrics(scored, name + " · complete raw CLI days"))
        metrics.extend(_metrics(native, name + " · verified payouts"))
        metrics.extend(_metrics(priced, name + " · native price and result", price=True))
        if scored:
            comparisons.extend({"group": name, **windows._paired(scored, baseline, challenger)}
                               for baseline, challenger in (("warm_two", "multi_two"), ("warm_six", "multi_six"),
                                                             ("multi_six", "dewpoint"), ("p06", "dewpoint")))
        rank = sorted(native, key=lambda row: (row["probabilities"]["warm_six"] - row["truth"]) ** 2 -
                      (row["probabilities"]["dewpoint"] - row["truth"]) ** 2, reverse=True)
        if rank:
            row = rank[0]
            odds = row["probabilities"]
            cases.append({"station": row["station"], "date": row["date"], "at": row["at"], "venue": row["venue"],
                          "instrument_id": row["market"]["instrument_id"], "title": f"{row['station']} · Rain under earlier-season calibration",
                          "detail": (f"Warm-only fit {odds['warm_six']:.1%}. The broader earlier training and dew-point feature give "
                                     f"{odds['dewpoint']:.1%}. CLI measured {row['official_report']['precip_as_reported']} and YES paid ${row['truth']}."),
                          "rain_evidence": row})
        details[name] = {"weather": scored, "payouts": native}
    record = {"version": VERSION, "title": "Does colder earlier weather improve measurable-rain calibration?",
              "hypothesis": "The recent warm-only fit may calibrate differently in late summer and autumn. Adding earlier cold-season data at the same stations separates time coverage from adding geography.",
              "target": "Native NBS short-range forecasts and exact daily measurable precipitation at nine independent stations",
              "venues": sorted({row["venue"] for row in cases}),
              "date_window": {"older_training": [list(map(str, period)) for period in OLD],
                 "warm_training": ["2026-06-01", research_model_rain_warm.FIT_THROUGH],
                 "test_periods": [["2026-08-27", "2026-09-21"], ["2026-10-29", "2026-11-14"]]},
              "coverage": {name: {**research_model._independent_counts(rows), "positive_days": sum(row["truth"] for row in rows)}
                           for name, rows in samples.items()},
              "metrics": metrics, "contrasts": comparisons, "cases": cases, "missing_old": inputs["older_missing"],
              "limits": ["Earlier rainy-day labels use the archived IEM summaries. Later weather scores use complete raw climate products and each venue outcome is verified separately.",
                         "The weather feature is mean air temperature minus dew point from the same as-issued card over the target climate day.",
                         "Only two sites supply earlier cold cases. The warm-two and multi-season-two comparison keeps their geography constant.",
                         "The earlier-season archive includes an older NBM release. The evaluation estimates practical transfer across both seasonal conditions and bulletin versions.",
                         "The same forecast cutoff, climate windows, and measurable threshold are applied to both verification periods."],
              "provenance": {"frozen_input": str(frozen.relative_to(data)), "frozen_input_sha256": digest,
                             "earlier_rain_input": inputs["reference"], "source_sha256": research_model.run_sources(
                                 sys.modules[__name__], windows, research_model_rain_warm, expansion_nbm)}}
    return research_model.write_record(data, record, details)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--baseline", type=Path)
    source.add_argument("--input", type=Path)
    args = parser.parse_args()
    inputs = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else gather(args.data, args.baseline)
    print(evaluate(args.data, inputs))


if __name__ == "__main__":
    main()
