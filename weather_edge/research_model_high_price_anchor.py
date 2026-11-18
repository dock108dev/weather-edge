"""Check whether the scheduled midday weather forecast improves an exact market midpoint."""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date
import gzip
import json
from math import log
from pathlib import Path
from statistics import mean
import sys

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from . import research_model, research_model_high_receipts, research_model_high_tails
from .nbm import utc

VERSION = "daily-high-cross-season-price-anchor-v1"
FIT_THROUGH = "2026-09-08"
SELECT_FROM = "2026-09-10"
METHODS = {"venue": "Earlier Kalshi midpoint", "weather": "Original-receipt residual distribution",
           "fixed_blend": "Midpoint with 25% weather", "venue_calibrated": "Earlier midpoint calibration",
           "market_weather": "Earlier midpoint calibration with weather update"}


def _gather(data, baseline):
    corpus = json.loads(gzip.decompress(baseline.read_bytes()))
    if corpus["version"] != research_model_high_receipts.VERSION:
        raise ValueError("Expected frozen original-receipt temperature forecasts")
    residuals = research_model_high_tails._calibration(corpus["fit"])
    samples = {}
    for phase, source in corpus["cohorts"].items():
        midpoint_cases = source["strict_midday"]
        same = research_model_high_receipts._same_cases(corpus, source)
        weather = research_model_high_tails._evaluate_weather([row for row in same if row["hour"] == 13], residuals)
        samples[phase] = research_model_high_tails._native(weather, midpoint_cases, residuals)
    return {"version": VERSION, "samples": samples, "baseline": research_model._source_entry(data, baseline)}


def _features(row, method):
    market = min(0.995, max(0.005, row["display_price"]["midpoint"]))
    values = [log(market / (1 - market))]
    if method == "market_weather":
        model = row["probabilities"]["residual"]
        return [*values, model - market]
    return values


def _model(rows, method):
    model = make_pipeline(StandardScaler(), LogisticRegression(C=0.2, max_iter=400))
    model.fit([_features(row, method) for row in rows], [row["native_yes"] for row in rows])
    return model


def _points(rows, models):
    mapped = {key: model.predict_proba([_features(row, key) for row in rows])[:, 1] for key, model in models.items()}
    final = []
    for index, row in enumerate(rows):
        market, weather = row["display_price"]["midpoint"], row["probabilities"]["residual"]
        estimates = {"venue": market, "weather": weather, "fixed_blend": market * 0.75 + weather * 0.25,
                     **{key: float(values[index]) for key, values in mapped.items()}}
        scores = {key: (probability - row["native_yes"]) ** 2 for key, probability in estimates.items()}
        final.append({**row, "anchored_probabilities": estimates, "scores": scores})
    return final


def _means(rows, model):
    days = defaultdict(list)
    for row in rows:
        days[row["date"]].append(row["scores"][model])
    return {day: mean(errors) for day, errors in days.items()}


def _metrics(rows, group):
    return [{"method": method, "label": label, "group": group, "bucket_brier": mean(_means(rows, method).values()),
             "calendar_dates": len({row["date"] for row in rows}),
             "station_days": len({(row["station"], row["date"]) for row in rows}), "native_bucket_cases": len(rows)}
            for method, label in METHODS.items()]


def _contrast(rows, baseline, method):
    earlier, updated = _means(rows, baseline), _means(rows, method)
    delta = [updated[day] - value for day, value in earlier.items()]
    draws = np.random.default_rng(20261115).choice(delta, size=(3000, len(delta))).mean(axis=1)
    return {"method": method, "baseline": baseline, "calendar_dates": len(delta), "delta": mean(delta),
            "date_bootstrap_interval_95": np.quantile(draws, (0.025, 0.975)).tolist()}


def _case(row):
    source = row["market"]
    odds = row["anchored_probabilities"]
    return {"station": row["station"], "date": row["date"], "at": row["display_price"]["comparison_at"],
            "venue": "kalshi", "instrument_id": source["instrument_id"],
            "title": f"{row['station']} · Midday weather and the exact venue midpoint",
            "detail": (f"Published-candle midpoint {odds['venue']:.1%}, forecast {odds['weather']:.1%}, "
                       f"earlier-fitted combination {odds['market_weather']:.1%}. The exact threshold paid ${row['native_yes']} YES."),
            "comparison": row}


def evaluate(data, inputs):
    frozen, digest = research_model.freeze_inputs(data, inputs)
    warm, cool = (inputs["samples"][season] for season in ("late summer", "autumn"))
    selection_start = date.fromisoformat(SELECT_FROM)
    fit = [row for row in warm if row["date"] <= FIT_THROUGH and row["market"]["settlement_at"]
           and utc(row["market"]["settlement_at"]).date() < selection_start]
    selection = [row for row in warm if row["date"] >= SELECT_FROM]
    if not fit or not selection or not cool:
        raise ValueError("Missing independent fitting, selection, or final market cases")
    models = {method: _model(fit, method) for method in ("venue_calibrated", "market_weather")}
    selected, test = _points(selection, models), _points(cool, models)
    earlier = _metrics(selected, "Earlier later-summer selection")
    later = _metrics(test, "Untouched autumn cases")
    selection_winner = min(earlier, key=lambda row: row["bucket_brier"])["method"]
    intervals = [_contrast(test, base, method) for base, method in (
        ("venue", "fixed_blend"), ("venue", "venue_calibrated"), ("venue", "market_weather"), ("venue_calibrated", "market_weather"))]
    ranking = sorted(test, key=lambda row: row["scores"]["market_weather"] - row["scores"]["venue"])
    cases = [_case(row) for row in (ranking[0], ranking[-1])]
    record = {"version": VERSION, "title": "Does a midday weather update improve a Kalshi temperature midpoint?",
              "hypothesis": "An earlier-fitted weather correction may add information to a coherent native midday quote. The midpoint calibration arm tests whether the weather itself contributes.",
              "target": "Exact native Kalshi thresholds on six earlier-fit and nine independent weather stations",
              "venues": ["kalshi"], "date_window": {"fit": ["2026-08-27", FIT_THROUGH], "selection": [SELECT_FROM, "2026-09-21"],
                 "test": ["2026-10-29", "2026-11-14"]},
              "coverage": {"fit_native_bucket_cases": len(fit), "fit_calendar_dates": len({row["date"] for row in fit}),
                           "fit_station_days": len({(row["station"], row["date"]) for row in fit}),
                           "selection_winner": selection_winner},
              "metrics": [*earlier, *later], "contrasts": intervals, "cases": cases,
              "limits": ["Only finite, strict two-sided 13:00 local-standard candles from the exact hour enter the 13:05 comparison, with a 30-cent maximum spread.",
                         "Weather predictions stop at 13:00 local standard and preserve the exact station and original MADIS source clocks. The sample is restricted to cases with both pieces.",
                         "The native results of warm training cases had to be published before the selection block. The fixed later block did not tune the model.",
                         "The primary error averages within each calendar date before averaging dates. Multiple strikes share the same underlying temperature.",
                         "Historical candles are display probability proxies with no executable depth or later-origin recheck."],
              "provenance": {"frozen_input": str(frozen.relative_to(data)), "frozen_input_sha256": digest,
                             "receipt_input": inputs["baseline"], "source_sha256": research_model.run_sources(
                                 sys.modules[__name__], research_model_high_tails, research_model_high_receipts)}}
    return research_model.write_record(data, record, {"selection": selected, "test": test, "fit_native_buckets": fit})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--receipt-input", type=Path)
    source.add_argument("--input", type=Path)
    args = parser.parse_args()
    current = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else _gather(args.data, args.receipt_input)
    print(evaluate(args.data, current))


if __name__ == "__main__":
    main()
