"""Audit near-certain daily-low calls and test a reserve using earlier independent dates."""

from __future__ import annotations

import argparse
from collections import defaultdict
import gzip
import hashlib
import json
from pathlib import Path
from statistics import mean
import sys

import numpy as np

from . import expansion_live_low, expansion_live_precision, research_model, research_model_low_cross_season as cross
from . import research_model_low_winter as winter
from . import research_model_low_portability as portable, research_model_low_precision as precision
from .nbm import utc

VERSION = "daily-low-held-out-tail-calibration-v1"
SEASONS = ("winter", "late summer", "autumn")
LEVELS = (0.90, 0.95, 0.99, 0.995)
METHODS = {"txn": "NOAA carried TXN", "combined": "Observed and remaining NOAA minimum", "precision_center": "Raw precision-informed center",
           "hard_cap": "Hard station bound", "guarded_cap": "Original earlier-station reserve", "date_reserve": "Expanded earlier-date reserve"}


def _load(data, path, version):
    record = json.loads(path.read_bytes())
    if record["version"] != version:
        raise ValueError(f"Expected the {version} record")
    content = gzip.decompress((data / record["provenance"]["frozen_input"]).read_bytes())
    if hashlib.sha256(content).hexdigest() != record["provenance"]["frozen_input_sha256"]:
        raise ValueError("Earlier archive model input digest disagrees")
    return json.loads(content), research_model._source_entry(data, path)


def gather(data, cross_path, winter_path):
    cross_input, cross_ref = _load(data, cross_path, cross.VERSION)
    winter_input, winter_ref = _load(data, winter_path, winter.VERSION)
    if cross_input["calibration"] != winter_input["calibration"]:
        raise ValueError("Earlier winter and cross-season calibration rows differ")
    groups = {"winter": {name: winter_input[name] for name in ("weather", "native", "strict_prices")},
              **{group: {name: cross_input["cohorts"][group][name] for name in ("weather", "native", "strict_prices")}
                 for group in SEASONS[1:]}}
    return {"version": VERSION, "groups": groups, "calibration": cross_input["calibration"],
            "date_window": {"earlier_calibration": cross_input["calibration_window"],
                            "winter": list(map(str, winter.DATES)), **{key: list(map(str, span)) for key, span in cross.SEASONS.items()}},
            "sources": [cross_ref, winter_ref]}


def _reserves(past, cutoff):
    evidence = defaultdict(dict)
    for row in past:
        report = row["official_report"]
        bound = row["metar_upper_bound"]
        if not bound or utc(report["published_at"]) >= cutoff or utc(row["at"]) >= cutoff:
            continue
        by_date = evidence[row["hour"]]
        failed = row["actual_f"] > bound["upper_f"]
        by_date[row["date"]] = by_date.get(row["date"], False) or failed
    return {hour: {"calendar_dates": len(values), "dates_with_a_bound_violation": sum(values.values()),
                   "uncapped_weight": expansion_live_low._wilson(sum(values.values()), len(values))[1]}
            for hour, values in sorted(evidence.items())}


def _add_reserve(native, weather, reserves):
    clocks = {(row["station"], row["date"], row["at"]): row["hour"] for row in weather}
    results = []
    for row in native:
        hour = clocks[row["station"], row["date"], row["at"]]
        coefficients = reserves.get(hour)
        updated = dict(row["probabilities"])
        weight = coefficients["uncapped_weight"] if coefficients else 1.0
        updated["date_reserve"] = (1 - weight) * updated["hard_cap"] + weight * updated["precision_center"]
        results.append({**row, "hour": hour, "probabilities": updated, "reserve": coefficients})
    return results


def _tail(rows, method, threshold):
    for row in rows:
        yes = row["probabilities"].get(method)
        if yes is None:
            continue
        side = "YES" if yes >= 0.5 else "NO"
        probability = yes if side == "YES" else 1 - yes
        if probability + 1e-12 >= threshold:
            yield row, side, probability, bool(row["native_yes"]) == (side == "YES")


def _summary(calls, group, method, threshold):
    dates = {row["date"] for row, _, _, ok in calls}
    misses = {(row["station"], row["date"]) for row, _, _, ok in calls if not ok}
    days = {(row["station"], row["date"]) for row, _, _, _ in calls}
    wrong_dates = len({day for _, day in misses})
    date_interval = expansion_live_low._wilson(wrong_dates, len(dates))
    return {"group": group, "method": method, "label": METHODS.get(method, "Exact coherent market midpoint"),
            "minimum_confidence": threshold, "calendar_dates": len(dates), "station_days": len(days),
            "losing_calendar_dates": wrong_dates, "losing_station_days": len(misses),
            "calendar_date_any_loss_wilson_upper_95": date_interval[1], "tail_calls": len(calls),
            "wrong_side_calls": sum(not ok for _, _, _, ok in calls), "yes_calls": sum(side == "YES" for _, side, _, _ in calls),
            "mean_stated_probability": mean(probability for _, _, probability, _ in calls),
            "observed_correct_side_rate": mean(ok for _, _, _, ok in calls)}


def _side_implied(call):
    row, side, _, _ = call
    return expansion_live_precision.metar_implied_side(row["bucket"], row["metar_evidence"]["upper_f"]) == side


def _case(row, side, probability, model):
    market = row["market"]
    stated = "over 99.99%" if 0.9999 < probability < 1 else f"{probability:.2%}"
    return {"station": row["station"], "date": row["date"], "at": row["at"], "venue": "kalshi",
            "instrument_id": market["instrument_id"], "title": f"{row['station']} · A losing high-confidence daily-low call",
            "detail": (f"{METHODS[model]} assigned {stated} to {side}. The complete report gave "
                       f"{row['official_report']['low_f']}°F after the current station reading bounded the low at "
                       f"{row['metar_evidence']['upper_f']}°F. Native YES paid ${row['native_yes']}."),
            "probability_method": model, "threshold": row}


def _paired(rows, group):
    dates = defaultdict(list)
    for row in rows:
        payout = row["native_yes"]
        old, new = (row["probabilities"][method] for method in ("guarded_cap", "date_reserve"))
        dates[row["date"]].append((new - payout) ** 2 - (old - payout) ** 2)
    values = [mean(effects) for effects in dates.values()]
    sampled = np.random.default_rng(20261115).choice(values, size=(3000, len(values))).mean(axis=1)
    return {"group": group, "method": "date_reserve", "baseline": "guarded_cap", "calendar_dates": len(dates),
            "delta": mean(values), "date_bootstrap_interval_95": np.quantile(sampled, (.025, .975)).tolist()}


def evaluate(data, inputs):
    if inputs["version"] != VERSION:
        raise ValueError("Unexpected daily-low tail calibration input")
    frozen, digest = research_model.freeze_inputs(data, inputs)
    widths, earlier = cross._fit_shared(inputs["calibration"])
    past = list(inputs["calibration"])
    metrics, coverage, details, cases, contrasts = [], {}, {}, [], []
    for season in SEASONS:
        group = inputs["groups"][season]
        weather = [row for row in group["weather"] if (row["station"], row["hour"]) in earlier]
        if not weather:
            continue
        reserved = _reserves(past, min(utc(row["at"]) for row in weather))
        scored = precision._eval(weather, widths, earlier)
        outcomes = _add_reserve(portable._prices(scored, group["native"], widths), weather, reserved)
        quoted = _add_reserve(portable._prices(scored, group["strict_prices"], widths), weather, reserved)
        coverage[season] = {**research_model._independent_counts(scored), "native_payout_cases": len(outcomes),
                            "strict_display_cases": len(quoted), "earlier_date_reserve_by_clock": reserved}
        for label, rows in (("all exact payouts", outcomes), ("strict same-clock quotes and payouts", quoted)):
            if not rows:
                continue
            methods = tuple(METHODS) + (("market",) if rows is quoted else ())
            item = f"{season} · {label}"
            contrasts.append(_paired(rows, item))
            for method in methods:
                metrics.append({"group": item, "method": method, "label": METHODS.get(method, "Exact coherent market midpoint"),
                                **research_model._independent_counts(rows), "bucket_brier": mean((row["probabilities"][method] - row["native_yes"]) ** 2 for row in rows)})
                for threshold in LEVELS:
                    calls = list(_tail(rows, method, threshold))
                    if calls:
                        metrics.append(_summary(calls, item + " · high-confidence calls", method, threshold))
                    if threshold == 0.99 and method in ("hard_cap", "guarded_cap", "date_reserve"):
                        for bound_implies, description in ((True, "station bound implies side"), (False, "model distribution implies side")):
                            distinct = [call for call in calls if _side_implied(call) == bound_implies]
                            if distinct:
                                metrics.append(_summary(distinct, item + f" · {description}", method, threshold))
        missed = [(row, side, chance, method) for method in ("date_reserve", "guarded_cap", "txn")
                  for row, side, chance, ok in _tail(outcomes, method, 0.99) if not ok]
        if missed:
            row, side, chance, method = max(missed, key=lambda item: item[2])
            cases.append(_case(row, side, chance, method))
        details[season] = {"native_thresholds": outcomes, "strict_quotes": quoted}
        past.extend(weather)
    record = {"version": VERSION, "title": "Daily-low confidence after the station reports a physical bound",
              "hypothesis": "A reserve estimated from more previously completed climate dates can retain tail reliability while reducing the original two-station mixture uncertainty.",
              "target": "Exact settled daily-low threshold sides, with the same weather distributions at each shared clock",
              "venues": ["kalshi"], "date_window": inputs["date_window"], "coverage": coverage, "metrics": metrics, "cases": cases,
              "uncertainty": contrasts,
              "limits": ["Both reserve variants retain the same earlier fitted centers and widths. The expanded reserve applies the upper Wilson bound to earlier dates with any measured bound violation at each climate clock.",
                         "Complete earlier climate results must have been published before the first holdout clock. Winter, warmer and cooler validation outcomes enter the reserve only after their own held-out period.",
                         "High-confidence calls use each model's own chosen side and support. The paired Brier rows use the same native thresholds. Many strikes and clocks repeat the same climate date.",
                         "A bound-implied YES needs an open lower tail covering the upper reading. A bound-implied NO needs a bucket entirely above the upper reading. The remaining calls depend on the fitted temperature distribution.",
                         "A date with any losing call is a conservative descriptive check across multiple stations. The Wilson bound cannot establish that any live instrument is 99% certain.",
                         "Strict market marks are dated archived candle estimates. No fill or paper strategy adopts the reserve."],
              "provenance": {"frozen_input": str(frozen.relative_to(data)), "frozen_input_sha256": digest, "settlement_and_weather_sources": inputs["sources"],
                             "source_sha256": research_model.run_sources(sys.modules[__name__], cross, winter, portable, precision,
                                                                           expansion_live_precision)}}
    return research_model.write_record(data, record, details)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--cross-season", type=Path)
    parser.add_argument("--winter", type=Path)
    parser.add_argument("--input", type=Path)
    args = parser.parse_args()
    if args.input is None and (args.cross_season is None or args.winter is None):
        parser.error("Provide both original completed records or the stored tail study inputs")
    inputs = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else gather(args.data, args.cross_season, args.winter)
    print(evaluate(args.data, inputs))


if __name__ == "__main__":
    main()
