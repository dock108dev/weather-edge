"""Try an earlier calibrated weather correction on coherent native daily-rain prices."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date, timedelta
import gzip
import json
import math
from pathlib import Path
from statistics import mean
import sys

import numpy as np
from sklearn import __version__ as sklearn_version
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from . import expansion_contract, expansion_weather, research_model, research_model_rain_multi as native_rain
from . import research_model_rain_warm as warm, research_model_rain_windows as guidance, venue_archive, venue_benchmark
from .nbm import iso, utc

VERSION = "daily-rain-price-anchor-transfer-v1"
GROUPS = {"August–September fit": ("2026-08-27", "2026-09-08"), "September selection": ("2026-09-09", "2026-09-21"),
          "October untouched": ("2026-10-01", "2026-10-14"), "Late autumn follow-up": ("2026-10-29", "2026-11-14")}
METHODS = {"market": "Raw coherent Kalshi midpoint", "nbm": "Prior-day six-hour NOAA probability", "weather": "Earlier six-station rainfall calibration",
           "fixed_25": "Raw midpoint with a fixed 25% weather weight", "market_calibrated": "Earlier logistic calibration of market only",
           "anchored": "Earlier fitted market and weather logistic correction", "spread_anchored": "Same correction conditioned on the native spread"}
CHALLENGERS = ("market", "fixed_25", "market_calibrated", "anchored", "spread_anchored")


def _october_weather(data):
    first, last = map(date.fromisoformat, GROUPS["October untouched"])
    rows, omitted = [], Counter()
    for station in warm.STATIONS:
        daily = expansion_weather.daily_labels(data, station, first, last)
        for day in research_model._days(first, last):
            source = daily.get(str(day))
            if not source or not (data / "expansion" / "cli" / station / f"{source['product']}.txt").is_file():
                omitted["no originally retained raw climate day"] += 1
                continue
            official = expansion_weather.raw_report(data, station, day, source["product"])
            measured = official["precip_as_reported"] if official else None
            if measured in (None, "M", "MM") or (actual := expansion_contract.rain_outcome(measured)) is None:
                omitted["no complete raw measurable amount"] += 1
                continue
            row = guidance._forecast(data, station, day)
            if not row:
                omitted["no originally published complete prior-day P06 and P12"] += 1
                continue
            rows.append({**row, "truth": actual, "official_report": official})
    return rows, dict(omitted)


def _strict(data, cases):
    paired, missing = [], Counter()
    with venue_archive.connect(data) as db:
        for row in cases:
            market = row["market"]
            if row["venue"] != venue_archive.KALSHI or not market["settlement_at"] or row["weather_truth"] != row["truth"]:
                missing["no matching exact complete raw weather and native payout"] += 1
                continue
            at = utc(row["at"])
            clock = at + timedelta(minutes=5)
            if at < utc(market["opened_at"]) or clock >= utc(market["closed_at"]):
                missing["native trade clock outside rules"] += 1
                continue
            mark, issue = venue_benchmark._history_point(db, market, at)
            if not mark:
                missing[issue] += 1
                continue
            paired.append({**row, "strict_price": mark, "comparison_at": iso(clock)})
    return paired, dict(missing)


def gather(data, prior_warm):
    source = json.loads(gzip.decompress(prior_warm.read_bytes()))
    if source["version"] != warm.VERSION:
        raise ValueError("Expected earlier exact nine-station rain transfer inputs")
    october, weather_gaps = _october_weather(data)
    extra, payout_gaps = native_rain._match(data, october)
    universe = [*source["native"]["late summer"], *extra, *source["native"]["autumn"]]
    paired, missing = {}, {}
    for group, (first, last) in GROUPS.items():
        paired[group], missing[group] = _strict(data, [row for row in universe if first <= row["date"] <= last])
    return {"version": VERSION, "weather_fit": source["fit"], "groups": paired, "source": research_model._source_entry(data, prior_warm),
            "gaps": {"strict_price": missing, "october_weather": weather_gaps, "october_native": payout_gaps},
            "new_october_weather_days": len(october), "new_october_native_days": len(extra)}


def _logit(raw):
    clipped = max(.005, min(.995, raw))
    return math.log(clipped / (1 - clipped))


def _weather_chance(rows, estimate):
    if not rows:
        return []
    forecast = estimate.predict_proba([guidance._features(row, ("p06",)) for row in rows])[:, 1]
    return [{**row, "weather_probability": float(probability)} for row, probability in zip(rows, forecast, strict=True)]


def _features(row, name):
    price = _logit(row["strict_price"]["probability"])
    available = [price]
    if name != "market_calibrated":
        weather = _logit(row["weather_probability"])
        available.append(weather)
        if name == "spread_anchored":
            available.append((weather - price) * row["strict_price"]["width"])
    return available


def _fit(rows, keys):
    models = {}
    for key in keys:
        fitted = make_pipeline(StandardScaler(), LogisticRegression(C=1))
        matrix = [guidance._features(row, ("p06",)) if key == "weather" else _features(row, key) for row in rows]
        fitted.fit(matrix, [row["truth"] for row in rows])
        models[key] = fitted
    return models


def _score(rows, models):
    if not rows:
        return []
    fitted = {name: model.predict_proba([_features(row, name) for row in rows])[:, 1] for name, model in models.items()}
    results = []
    for index, row in enumerate(rows):
        market, weather = row["strict_price"]["probability"], row["weather_probability"]
        probabilities = {"market": market, "nbm": row["direct"]["p06"], "weather": weather, "fixed_25": .75 * market + .25 * weather,
                         **{name: float(values[index]) for name, values in fitted.items()}}
        results.append({**row, "probabilities": probabilities})
    return results


def _error(row, method):
    return (row["probabilities"][method] - row["truth"]) ** 2


def _interval(rows, candidate, title):
    dates = defaultdict(list)
    for row in rows:
        dates[row["date"]].append(_error(row, candidate) - _error(row, "market"))
    values = [mean(effects) for effects in dates.values()]
    sampled = np.random.default_rng(20261115).choice(values, size=(3000, len(values))).mean(axis=1)
    return {"group": title, "method": candidate, "baseline": "market", "calendar_dates": len(dates),
            "delta": mean(values), "date_bootstrap_interval_95": np.quantile(sampled, (.025, .975)).tolist()}


def _case(row, method, display):
    prices = row["probabilities"]
    return {"station": row["station"], "date": row["date"], "at": row["comparison_at"], "venue": row["venue"], "instrument_id": row["market"]["instrument_id"],
            "title": f"{row['station']} · A measured-rain disagreement between market and NOAA",
            "detail": (f"Native midpoint {prices['market']:.1%}, earlier calibrated weather {prices['weather']:.1%}, "
                       f"{display} {prices[method]:.1%}. Official precipitation was "
                       f"{row['official_report']['precip_as_reported']} and native YES paid ${row['truth']}."), "rain_evidence": row}


def evaluate(data, inputs):
    if inputs["version"] != VERSION:
        raise ValueError("Unexpected rain price-anchor source")
    frozen, digest = research_model.freeze_inputs(data, inputs)
    order = tuple(GROUPS)
    raw_fit, selection = (inputs["groups"][group] for group in order[:2])
    first_fit, first_selection = (min(utc(row["comparison_at"]) for row in group) for group in (raw_fit, selection))
    weather_fit = [row for row in inputs["weather_fit"] if utc(row["official_report"]["published_at"]) < first_fit]
    standalone = _fit(weather_fit, ("weather",))["weather"]
    fit = [row for row in raw_fit if max(utc(row["official_report"]["published_at"]), utc(row["market"]["settlement_at"])) < first_selection]
    trained = _fit(_weather_chance(fit, standalone), ("market_calibrated", "anchored", "spread_anchored"))
    scored = {key: _score(_weather_chance(inputs["groups"][key], standalone), trained) for key in order[1:]}
    best = min(CHALLENGERS, key=lambda method: mean(_error(row, method) for row in scored[order[1]]))
    metrics, intervals, examples = [], [], []
    for title, rows in scored.items():
        if not rows:
            continue
        for method, label in METHODS.items():
            metrics.append({"group": title, "method": method, "label": label, **research_model._independent_counts(rows),
                            "measurable_cases": sum(row["truth"] for row in rows), "bucket_brier": mean(_error(row, method) for row in rows)})
        if title != order[1]:
            intervals.extend(_interval(rows, candidate, title) for candidate in ("fixed_25", "market_calibrated", "anchored", "spread_anchored"))
            for sort in (-1, 1):
                row = max(rows, key=lambda row: sort * (_error(row, "market") - _error(row, "anchored")))
                examples.append(_case(row, "anchored", "learned correction"))
    record = {"version": VERSION, "title": "Can weather improve a native daily-rain price in a later month?",
              "hypothesis": "The known warm-weather forecast gain may transfer when blended with an earlier coherent market mark. The same raw market and market-only recalibration can reveal a gain that actually comes from weather.",
              "target": "Exact native Kalshi above-zero daily rain payouts, using the same six-hour NOAA forecast and completed hourly venue candle",
              "venues": [venue_archive.KALSHI], "date_window": GROUPS, "selected_on_earlier_dates": best,
              "coverage": {"earlier_weather_fit": research_model._independent_counts(weather_fit), "earlier_price_fit": research_model._independent_counts(fit),
                           **{title: research_model._independent_counts(rows) for title, rows in scored.items()},
                           "october_raw_station_days": inputs["new_october_weather_days"], "october_verified_native": inputs["new_october_native_days"]},
              "metrics": metrics, "uncertainty": intervals, "cases": examples,
              "limits": ["Only explicit named-station native payouts that agree with a verified complete raw climate report enter the blend. This avoids treating the provider's fallback as an identical independent weather rule.",
                         "The original six-hour NOAA bulletin must have been published by the prior 21:00 local-standard decision. Quotes require an exact completed hourly candle with coherent two-sided spread at most 30 cents and five minutes of allowance.",
                         "Model fitting uses earlier warm labels and market settlements available before the next comparison starts. The new October cases were assembled for the blend. Later fall is a follow-up after comparing raw weather methods separately.",
                         "Archived display candles carry current retrieval clocks and lack original live receipt and order depth. This is a prediction comparison."],
              "missing": inputs["gaps"], "provenance": {"baseline_source": inputs["source"], "frozen_input": str(frozen.relative_to(data)),
                 "frozen_input_sha256": digest, "sklearn": sklearn_version,
                 "source_sha256": research_model.run_sources(sys.modules[__name__], native_rain, warm, guidance, venue_benchmark)}}
    return research_model.write_record(data, record, scored)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--earlier-rain", type=Path)
    source.add_argument("--input", type=Path)
    args = parser.parse_args()
    inputs = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else gather(args.data, args.earlier_rain)
    print(evaluate(args.data, inputs))


if __name__ == "__main__":
    main()
