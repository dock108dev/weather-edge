"""Compare measurable-rain guidance across nine unseen stations and two date blocks."""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date
import gzip
import json
from math import log1p
from pathlib import Path
from statistics import mean
import sys

from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from . import expansion_contract, expansion_nbm, expansion_weather, research_model, research_model_high_warm
from . import research_model_high_transfer, research_model_rain, research_model_rain_multi, research_model_rain_windows as windows
from .nbm import climate_window, utc

VERSION = "daily-rain-cross-season-transfer-v1"
FIT_THROUGH = "2026-08-15"
STATIONS = research_model_high_transfer.STATIONS
SEASONS = ("late summer", "autumn")
METHODS = {**windows.METHODS, "calibrated_amount": "Earlier P06 plus same-issue Q06 amount"}
FEATURES = {"calibrated_p06": ("p06",), "calibrated_both": ("p06", "p12_coherent"),
            "calibrated_amount": ("p06",)}


def _with_amount(data, row):
    station, day = row["station"], date.fromisoformat(row["date"])
    start, end = climate_window(day, expansion_weather.STATIONS[station][3])
    card = expansion_nbm.load_card(data, station, utc(row["forecast"]["issued_at"]))
    amount = research_model_rain.estimate_rain_window(card, start, end) if card and card["card_sha256"] == row["forecast"]["card_sha256"] else None
    if amount is None:
        return None
    return {**row, "q06_amount_inches": amount["weighted_q06_inches"], "native_amount_periods": amount["source_periods"]}


def _warm_station(data, station):
    span = research_model_high_warm.TEST
    labels = expansion_weather.daily_labels(data, station, *span)
    rows, gaps = [], Counter()
    for day in research_model._days(*span):
        candidate = labels.get(str(day))
        try:
            official = expansion_weather.raw_report(data, station, day, candidate["product"]) if candidate else None
        except (OSError, KeyError, ValueError, TypeError):
            official = None
        value = official["precip_as_reported"] if official else None
        if value in (None, "M", "MM") or (truth := expansion_contract.rain_outcome(value)) is None:
            gaps["no complete raw measurable-precipitation label"] += 1
            continue
        forecast = windows._forecast(data, station, day)
        adjusted = _with_amount(data, forecast) if forecast else None
        if adjusted is None:
            gaps["no complete prior-day matched P06, P12, and Q06 issue"] += 1
            continue
        rows.append({**adjusted, "truth": truth, "official_report": official})
    return rows, dict(gaps)


def gather(data, original):
    archive = json.loads(gzip.decompress(original.read_bytes()))
    if archive["version"] != windows.VERSION:
        raise ValueError("Expected frozen earlier precipitation interval comparison")
    warm, gaps = [], {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        for station, (rows, missing) in zip(STATIONS, pool.map(lambda station: _warm_station(data, station), STATIONS), strict=True):
            warm.extend(rows)
            gaps[station] = missing
    earlier = [checked for row in archive["rows"]["fit"] if row["date"] <= FIT_THROUGH
               and row["official_report"]["precip_as_reported"] not in (None, "M", "MM")
               and (checked := _with_amount(data, row))]
    cool = [checked for row in archive["rows"]["test"] if row["station"] in STATIONS
            and row["official_report"]["precip_as_reported"] not in (None, "M", "MM")
            and (checked := _with_amount(data, row))]
    warm_payouts, warm_omissions = research_model_rain_multi._match(data, warm)
    cool_payouts, cool_omissions = research_model_rain_multi._match(data, cool)
    return {"version": VERSION, "fit": earlier, "weather": {"late summer": warm, "autumn": cool},
            "native": {"late summer": warm_payouts, "autumn": cool_payouts}, "gaps": gaps,
            "payout_gaps": {"late summer": warm_omissions, "autumn": cool_omissions},
            "prior": research_model._source_entry(data, original)}


def _features(row, method):
    values = windows._features(row, FEATURES[method])
    return [*values, log1p(row["q06_amount_inches"] * 100)] if method == "calibrated_amount" else values


def _predict(models, rows):
    if not rows:
        return []
    estimates = {key: model.predict_proba([_features(row, key) for row in rows])[:, 1] for key, model in models.items()}
    return [{**row, "probabilities": {**row["direct"], **{method: float(values[index]) for method, values in estimates.items()}}}
            for index, row in enumerate(rows)]


def _metrics(rows, group, *, price=False):
    if not rows:
        return []
    methods = {**METHODS, "market": "Earlier market midpoint"} if price else METHODS
    return [{"method": method, "label": title, "group": group, **research_model._independent_counts(rows),
             "wet_cases": sum(row["truth"] for row in rows),
             "bucket_brier": mean(((row["market_midpoint"] if method == "market" else row["probabilities"][method]) - row["truth"]) ** 2
                                  for row in rows)} for method, title in methods.items()]


def _case(row):
    odds = row["probabilities"]
    return {"station": row["station"], "date": row["date"], "at": row["at"], "venue": row["venue"],
            "instrument_id": row["market"]["instrument_id"], "title": f"{row['station']} · The exact measurable-rain case",
            "detail": (f"P06 aggregate {odds['p06']:.1%}, native P12 aggregate {odds['p12']:.1%}, "
                       f"weighted Q06 {row['q06_amount_inches']:.2f} inches. Complete CLI "
                       f"{row['official_report']['precip_as_reported']} and native YES paid ${row['truth']}."),
            "precipitation": row}


def evaluate(data, inputs):
    frozen, digest = research_model.freeze_inputs(data, inputs)
    fit = inputs["fit"]
    models = {}
    for method in FEATURES:
        model = make_pipeline(StandardScaler(), LogisticRegression(C=1))
        model.fit([_features(row, method) for row in fit], [row["truth"] for row in fit])
        models[method] = model
    metrics, comparisons, cases, coverage, details = [], [], [], {}, {}
    for group in SEASONS:
        weather = inputs["weather"][group]
        scored = _predict(models, weather)
        native = _predict(models, inputs["native"][group])
        priced = [row for row in native if row["market_midpoint"] is not None]
        metrics.extend(_metrics(scored, group + " · exact raw CLI"))
        metrics.extend(_metrics(native, group + " · paid native thresholds"))
        metrics.extend(_metrics(priced, group + " · matched price and payout", price=True))
        if scored:
            comparisons.extend({"group": group, **windows._paired(scored, "p06", method)}
                               for method in ("p12", "calibrated_p06", "calibrated_both", "calibrated_amount"))
        show = sorted(native, key=lambda row: (row["weather_truth"] != row["truth"],
                                              abs(row["probabilities"]["p06"] - row["truth"])), reverse=True)
        cases.extend(_case(row) for row in show[:2])
        coverage[group] = {**research_model._independent_counts(scored), "wet_station_days": sum(row["truth"] for row in scored),
                           "native_payouts": len(native), "priced_payouts": len(priced),
                           "native_weather_label_disagreements": sum(row["weather_truth"] != row["truth"] for row in native)}
        details[group] = {"weather": scored, "native": native}
    record = {"version": VERSION, "title": "Do six-hour rain forecasts transfer across warmer and cooler dates?",
              "hypothesis": "The same shorter weather interval method can predict daily measurable precipitation at unseen stations in two seasons. Longer guidance and quantitative amounts can reveal a season-dependent advantage.",
              "target": "Complete exact-station liquid precipitation and independently verified native Kalshi rain payouts",
              "date_window": {"fit": ["2026-06-01", FIT_THROUGH], "late_summer": ["2026-08-27", "2026-09-21"],
                              "autumn": ["2026-10-29", "2026-11-14"]},
              "venues": sorted({row["venue"] for row in cases}), "metrics": metrics, "cases": cases, "contrasts": comparisons,
              "coverage": {"fit": research_model._independent_counts(fit), **coverage},
              "missing": inputs["gaps"],
              "limits": ["The same 21:00 previous local-standard decision and original NBS bulletin supply all weather features.",
                         "Partial forecast periods are weighted by their overlap with the official climate day. The amount feature uses native Q06 in hundredths of an inch.",
                         "Both tests use stations omitted from training. The original archived station report and the native payout are recorded independently.",
                         "An official trace contributes zero measured liquid amount. Missing CLI measurements are omitted from this weather test.",
                         "Native display observations are used only for probability scoring."],
              "provenance": {"frozen_input": str(frozen.relative_to(data)), "frozen_input_sha256": digest,
                             "prior_rain_test": inputs["prior"], "nbm_docs": "https://vlab.noaa.gov/web/mdl/nbm-textcard-v5.0",
                             "source_sha256": research_model.run_sources(sys.modules[__name__], windows, research_model_rain_multi)}}
    return research_model.write_record(data, record, {**details, "payout_omissions": inputs["payout_gaps"]})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--prior-rain-input", type=Path)
    source.add_argument("--input", type=Path)
    args = parser.parse_args()
    inputs = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else gather(args.data, args.prior_rain_input)
    print(evaluate(args.data, inputs))


if __name__ == "__main__":
    main()
