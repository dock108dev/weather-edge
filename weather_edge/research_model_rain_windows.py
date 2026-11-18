"""Compare matched shorter and longer NBM precipitation probabilities across stations."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import gzip
import json
from math import log, prod
from pathlib import Path
from statistics import mean
import sys

import numpy as np
from sklearn import __version__ as sklearn_version
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from . import (expansion_contract, expansion_nbm, expansion_weather, research_model, research_model_high_transfer,
               research_model_rain, research_model_rain_multi)
from .nbm import climate_window, iso, utc

VERSION = "daily-rain-window-aggregation-v1"
STATIONS = (*research_model_rain_multi.STATIONS, *research_model_high_transfer.STATIONS)
TEST = research_model_high_transfer.TEST
HOUR = research_model_rain.HOUR_LST
METHODS = {"p06": "Independent 6-hour intervals", "p12": "Independent native 12-hour intervals",
           "p12_coherent": "12-hour adjusted to contain its published 6-hour probabilities", "peak_p06": "Largest adjusted 6-hour chance",
           "calibrated_p06": "Earlier calibration using only 6-hour guidance",
           "calibrated_both": "Earlier calibration using 6- and 12-hour guidance"}


def _window(card, start, end, duration, *, lower_bound=False):
    field = f"P{duration:02}"
    step = timedelta(hours=duration)
    p06 = {utc(point["valid_at"]): point["P06"] for point in card["points"] if 0 <= point.get("P06", -1) <= 100}
    intervals = []
    for point in card["points"]:
        valid = utc(point["valid_at"])
        previous = valid - step
        left, right = max(start, previous), min(end, valid)
        if not left < right or not 0 <= point.get(field, -1) <= 100:
            continue
        probability = point[field]
        children = [p06.get(valid - timedelta(hours=6)), p06.get(valid)] if duration == 12 else []
        minimum = max(children) if children and all(value is not None for value in children) else None
        adjusted = max(probability, minimum) if lower_bound and minimum is not None else probability
        intervals.append({"starts_at": iso(previous), "ends_at": iso(valid), "overlap_start": iso(left), "overlap_end": iso(right),
                          "overlap": (right - left) / step, "forecast_pct": probability, "used_pct": adjusted,
                          "nested_peak_p06_pct": minimum})
    cursor = start
    for item in intervals:
        if utc(item["overlap_start"]) != cursor:
            return None
        cursor = utc(item["overlap_end"])
    if cursor != end:
        return None
    derived = [1 - (1 - item["used_pct"] / 100) ** item["overlap"] for item in intervals]
    return {"probability": 1 - prod(1 - p for p in derived), "peak_probability": max(derived), "intervals": intervals}


def _forecast(data, station, day):
    begin, end = climate_window(day, expansion_weather.STATIONS[station][3])
    decision = begin + timedelta(hours=HOUR)
    issue_clock = decision.replace(hour=decision.hour // 6 * 6)
    for shift in range(7):
        issue = issue_clock - timedelta(hours=6 * shift)
        card = expansion_nbm.load_card(data, station, issue)
        if not card or not issue <= utc(card["available_at"]) <= decision:
            continue
        raw = _window(card, begin, end, 6)
        longer = _window(card, begin, end, 12)
        repaired = _window(card, begin, end, 12, lower_bound=True)
        if not all((raw, longer, repaired)):
            continue
        keys = ("issued_at", "available_at", "received_at", "source", "card_sha256")
        return {"station": station, "date": str(day), "at": iso(decision), "forecast": {key: card[key] for key in keys},
                "p06_periods": raw["intervals"], "p12_periods": repaired["intervals"],
                "direct": {"p06": raw["probability"], "p12": longer["probability"], "p12_coherent": repaired["probability"],
                           "peak_p06": raw["peak_probability"]}}
    return None


def _collect_station(data, station):
    windows = {**research_model.WINDOWS, "test": TEST} if station in research_model_rain_multi.STATIONS else {"test": TEST}
    first, last = min(begin for begin, _ in windows.values()), TEST[1]
    labels = expansion_weather.daily_labels(data, station, first, last)
    rows, gaps = defaultdict(list), Counter()
    phases = list(windows)
    for index, (phase, (start, end)) in enumerate(windows.items()):
        cutoff = (climate_window(windows[phases[index + 1]][0], expansion_weather.STATIONS[station][3])[0] + timedelta(hours=HOUR)
                  if index + 1 < len(phases) else None)
        for day in research_model._days(start, end):
            label = labels.get(str(day))
            if label and phase in ("selection", "test"):
                try:
                    label = expansion_weather.raw_report(data, station, day, label["product"])
                except (OSError, KeyError, ValueError):
                    label = None
            truth = expansion_contract.rain_outcome(label["precip_as_reported"]) if label else None
            if truth is None or (cutoff and utc(label["published_at"]) >= cutoff):
                gaps[f"{phase}: missing full-day CLI before next stage"] += 1
                continue
            forecast = _forecast(data, station, day)
            if not forecast:
                gaps[f"{phase}: incomplete matched 6- and 12-hour issue"] += 1
                continue
            rows[phase].append({**forecast, "truth": truth, "official_report": label})
    return dict(rows), dict(gaps)


def gather(data):
    rows = {phase: [] for phase in research_model.WINDOWS}
    gaps = {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        for station, (observed, missing) in zip(STATIONS, pool.map(lambda st: _collect_station(data, st), STATIONS), strict=True):
            for phase, sample in observed.items():
                rows[phase].extend(sample)
            gaps[station] = missing
    markets, omitted = research_model_rain_multi._match(data, rows["test"])
    return {"version": VERSION, "rows": rows, "markets": markets, "gaps": gaps, "payout_missing": omitted}


def _features(row, fields):
    values = [min(0.999, max(0.001, row["direct"][field])) for field in fields]
    return [log(value / (1 - value)) for value in values]


def _fit(rows):
    models = {}
    for name, features in (("calibrated_p06", ("p06",)), ("calibrated_both", ("p06", "p12_coherent"))):
        pipeline = make_pipeline(StandardScaler(), LogisticRegression(C=1, random_state=20261115))
        pipeline.fit([_features(row, features) for row in rows], [row["truth"] for row in rows])
        models[name] = pipeline, features
    return models


def _predict(models, rows):
    if not rows:
        return []
    fills = {method: estimator.predict_proba([_features(row, fields) for row in rows])[:, 1]
             for method, (estimator, fields) in models.items()}
    return [{**row, "probabilities": {**row["direct"], **{key: float(values[i]) for key, values in fills.items()}}}
            for i, row in enumerate(rows)]


def _metrics(rows, group, *, marks=False):
    if not rows:
        return []
    methods = {**METHODS, "market": "Earlier market display midpoint"} if marks else METHODS
    return [{"group": group, "method": method, "label": label, "calendar_dates": len({r["date"] for r in rows}),
             "station_days": len({(r["station"], r["date"]) for r in rows}), "cases": len(rows), "wet_cases": sum(r["truth"] for r in rows),
             "brier": mean(((r["market_midpoint"] if method == "market" else r["probabilities"][method]) - r["truth"]) ** 2 for r in rows)}
            for method, label in methods.items()]


def _paired(rows, a, b):
    dates = defaultdict(list)
    for row in rows:
        first, second = (row["probabilities"][method] for method in (a, b))
        dates[row["date"]].append((second - row["truth"]) ** 2 - (first - row["truth"]) ** 2)
    per_day = [mean(errors) for errors in dates.values()]
    resample = np.random.default_rng(20261115).choice(per_day, size=(2500, len(per_day))).mean(axis=1)
    return {"method": b, "baseline": a, "delta_brier": mean(per_day), "date_bootstrap_ci_95": np.quantile(resample, (0.025, 0.975)).tolist()}


def evaluate(data, inputs):
    path, digest = research_model.freeze_inputs(data, inputs)
    rows = inputs["rows"]
    models = _fit([*rows["fit"], *rows["calibration"]])
    selection, test = (_predict(models, rows[part]) for part in ("selection", "test"))
    markets = _predict(models, inputs["markets"])
    new = [row for row in test if row["station"] in research_model_high_transfer.STATIONS]
    metrics = _metrics(selection, "Earlier selection dates") + _metrics(test, "Later exact-station CLI dates")
    metrics += _metrics(new, "Later dates at unseen stations")
    for venue in ("kalshi", "polymarket-us"):
        selected = [row for row in markets if row["venue"] == venue]
        metrics += _metrics(selected, f"{venue} · native payouts")
        metrics += _metrics([row for row in selected if row["market_midpoint"] is not None], f"{venue} · identical quoted cases", marks=True)
    nested = [item for row in test for item in row["p12_periods"] if item["nested_peak_p06_pct"] is not None]
    conflicting = [entry for entry in nested if entry["used_pct"] > entry["forecast_pct"]]
    comparisons = [_paired(test, a, b) for a, b in (("p06", "p12"), ("p12", "p12_coherent"), ("calibrated_p06", "calibrated_both"))]
    cases = []
    for desired in (0, 1):
        pool = [row for row in markets if row["truth"] == desired]
        if not pool:
            continue
        row = max(pool, key=lambda x: abs(x["probabilities"]["p06"] - x["truth"]))
        p = row["probabilities"]
        cases.append({"title": f"{row['station']} · Original precipitation probability and payout", "station": row["station"],
                      "date": row["date"], "at": row["at"], "venue": row["venue"], "instrument_id": row["market"]["instrument_id"],
                      "detail": (f"Six-hour aggregate {p['p06']:.1%}, twelve-hour aggregate {p['p12']:.1%}. "
                                 f"CLI reported {row['official_report']['precip_as_reported']} inches and native YES paid ${row['truth']}."),
                      "weather": row})
    record = {"version": VERSION, "title": "Short and long precipitation forecasts for the same climate day",
              "hypothesis": "Native 12-hour probabilities or earlier calibration may reduce error caused by treating adjacent six-hour rain events as independent.",
              "target": "Station CLI measurable precipitation and exact venue binary settlement", "venues": ["kalshi", "polymarket-us"],
              "date_window": {**research_model._window_strings(), "test": list(map(str, TEST))}, "metrics": metrics,
              "cases": cases, "comparisons": comparisons,
              "coverage": {"matched_12_hour_periods": len(nested), "below_a_nested_6_hour_probability": len(conflicting),
                           "later_payouts": len(markets), "added_stations": list(research_model_high_transfer.STATIONS)},
              "limits": ["All versions use one exact station and forecast issue available by the preceding 21:00 local-standard clock. Partial intervals assume uniform risk.",
                         "A qualifying event in a full shorter interval implies measurable rain in its containing longer interval. The repair enforces this lower bound in the forecast inputs.",
                         "Learned calibration uses only the original six station sites and two earlier date ranges. Nine of the later sites are unseen.",
                         "Venue listings use distinct resolution and revision terms. Weather labels and paid outcomes remain explicit."],
              "provenance": {"frozen_input": str(path.relative_to(data)), "frozen_input_sha256": digest,
                             "nbm_guidance": "https://vlab.noaa.gov/web/mdl/nbm-textcard-v5.0", "scikit_learn": sklearn_version,
                             "source_sha256": research_model.run_sources(sys.modules[__name__], expansion_nbm, research_model_rain_multi)}}
    return research_model.write_record(data, record, {"selection": selection, "test": test, "payouts": markets})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--input", type=Path)
    args = parser.parse_args()
    source = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else gather(args.data)
    print(evaluate(args.data, source))


if __name__ == "__main__":
    main()
