"""Compare as-issued NOAA temperature quantiles with Gaussian bucket probabilities."""

from __future__ import annotations

import argparse
import gzip
import json
import math
import statistics
import sys
from bisect import bisect_right
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from sklearn import __version__ as sklearn_version
from sklearn.feature_extraction import DictVectorizer
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from . import expansion_nbp, expansion_weather, research_model, research_model_high
from .model import _benchmark_buckets, _limits, _outcome, _probability
from .nbm import climate_window, utc

VERSION = "nbm-probability-spread-v1"
METHODS = {
    "nbs": "NOAA short-range TXN + XND",
    "nbp_gaussian": "NOAA probabilistic mean + Gaussian spread",
    "nbp_quantiles": "NOAA issued percentiles + monotone interpolation",
    "nbp_ridge_scale": "NOAA probabilistic mean + learned spread",
}


def percentile_cdf(bound, quantiles, scale):
    """Interpolate native rounded percentiles and extend their two tails."""
    probs = tuple(expansion_nbp.QUANTILES.values())
    if bound < quantiles[0]:
        return probs[0] * math.exp((bound - quantiles[0]) / scale)
    if bound >= quantiles[-1]:
        return 1 - (1 - probs[-1]) * math.exp((quantiles[-1] - bound) / scale)
    left = bisect_right(quantiles, bound) - 1
    right = left + 1
    fraction = (bound - quantiles[left]) / (quantiles[right] - quantiles[left])
    return probs[left] + fraction * (probs[right] - probs[left])


def _nbp_forecast(data, station, day, at, target):
    prior = day - timedelta(days=1)
    issue = datetime.combine(prior, time(13), timezone.utc)
    card = expansion_nbp.load_card(data, station, issue)
    if card is None or not utc(card["issued_at"]) <= utc(card["available_at"]) <= at:
        return None
    start, end = climate_window(day, expansion_weather.STATIONS[station][3])
    point = next((item for item in card["points"]
                  if item["target"] == target and start <= utc(item["valid_at"]) < end), None)
    if point:
        return {**point, "issued_at": card["issued_at"], "available_at": card["available_at"],
                "source": card["source"], "card_sha256": card["card_sha256"],
                "card_file": research_model._source_entry(data, expansion_nbp.card_path(data, station, issue))}
    return None


def _json(path):
    return json.loads(gzip.decompress(path.read_bytes()))


def make_inputs(data, high_path, low_path):
    if high_path:
        high_data = _json(high_path)
    else:
        high_data = research_model_high.make_inputs(data)
        high_path, _ = research_model.freeze_inputs(data, high_data)
    if low_path:
        low_data = _json(low_path)
    else:
        low_data = research_model.make_inputs(data)
        low_path, _ = research_model.freeze_inputs(data, low_data)
    low_rows, missing = research_model.snapshots(low_data)
    result = {"daily-high": {}, "daily-low": {}}
    gaps = []
    for target, group in (("daily-high", high_data["rows"]), ("daily-low", low_rows)):
        for stage, stage_rows in group.items():
            result[target][stage] = []
            for row in stage_rows:
                if row["hour"] != -3:
                    continue
                at = utc(row["at"])
                day = date.fromisoformat(row["date"])
                forecast = _nbp_forecast(data, row["station"], day, at, target)
                if forecast is None:
                    gaps.append({"station": row["station"], "date": row["date"], "target": target,
                                 "split": stage, "reason": "Previous 13Z NBP missing or published after decision"})
                    continue
                result[target][stage].append({**row, "nbp": forecast})
    return {
        "version": VERSION, "windows": high_data["windows"], "cases": result, "missing": gaps,
        "weather_inputs": {target: research_model._source_entry(data, path)
                           for target, path in (("daily-high", high_path), ("daily-low", low_path))},
        "low_archive_gaps": missing,
    }


def _features(row):
    q10, q25, q50, q75, q90 = row["nbp"]["quantiles_f"]
    return {"station": row["station"], "nbp_sd_f": row["nbp"]["sd_f"],
            "nbp_iqr_f": q75 - q25, "nbp_middle80_f": q90 - q10, "nbp_median_f": q50}


def _predict(fitted_scale, fallback_nbs, rows):
    if not rows:
        return []
    result = []
    scales = fitted_scale.predict([_features(row) for row in rows])
    for row, fitted in zip(rows, scales, strict=True):
        nbs, nbp = row["forecast"], row["nbp"]
        center = nbs["mean_f"]
        buckets = _benchmark_buckets(round(center))
        actual = row["actual_f"]
        truth = [_outcome(bucket, actual) for bucket in buckets]
        percentiles = nbp["quantiles_f"]
        native_sd = max(0.75, nbp["sd_f"])
        candidates = {
            "nbs": (center, max(0.75, nbs["sd_f"] or fallback_nbs)),
            "nbp_gaussian": (nbp["mean_f"], native_sd),
            "nbp_quantiles": (percentiles[2], native_sd),
            "nbp_ridge_scale": (nbp["mean_f"], max(0.75, fitted * math.sqrt(math.pi / 2))),
        }
        estimates = {}
        for method, (mean, sd) in candidates.items():
            probs = []
            for bucket in buckets:
                lo, hi = _limits(bucket)
                if method == "nbp_quantiles":
                    p = percentile_cdf(hi, percentiles, sd) - percentile_cdf(lo, percentiles, sd)
                else:
                    p = _probability((lo, hi), [mean], sd)
                probs.append(p)
            estimates[method] = {
                "mean_f": mean, "scale_f": sd, "probabilities": probs, "error_f": mean - actual,
                "brier": statistics.mean((p - y) ** 2 for p, y in zip(probs, truth, strict=True)),
            }
        result.append({**row, "buckets": buckets, "truth": truth, "estimates": estimates})
    return result


def _scores(splits, family):
    calibrate = splits["calibration"]
    fit = make_pipeline(DictVectorizer(sparse=False), StandardScaler(), Ridge(alpha=10.0))
    target = [abs(row["nbp"]["mean_f"] - row["actual_f"]) for row in splits["fit"]]
    fit.fit([_features(row) for row in splits["fit"]], target)
    fallback = math.sqrt(statistics.mean((row["actual_f"] - row["forecast"]["mean_f"]) ** 2 for row in calibrate))
    selection = _predict(fit, fallback, splits["selection"])
    test = _predict(fit, fallback, splits["test"])
    by_group = research_model.metrics(test, family, METHODS)
    for station in sorted({row["station"] for row in test}):
        by_group.extend(research_model.metrics([row for row in test if row["station"] == station],
                                               f"{family} · {station}", METHODS))
    select = research_model.metrics(selection, f"{family} · selection", METHODS)
    bootstraps = [research_model.date_bootstrap(test, baseline, method, "brier") for baseline, method in
                  (("nbs", "nbp_gaussian"), ("nbp_gaussian", "nbp_quantiles"), ("nbp_gaussian", "nbp_ridge_scale"))]
    reliability = [entry for method in METHODS for entry in research_model.calibration_bins(test, method)]
    return by_group, {"selection": select, "test": test, "contrasts": bootstraps, "calibration": reliability}


def _cases(target, rows):
    ranked = sorted(rows, key=lambda row: row["nbp"]["quantiles_f"][-1] - row["nbp"]["quantiles_f"][0], reverse=True)
    return [{
        "station": row["station"], "date": row["date"], "at": row["at"],
        "title": f"{row['station']} {row['date']} · {target} spread",
        "detail": f"NOAA 10th–90th percentile {row['nbp']['quantiles_f'][0]}–{row['nbp']['quantiles_f'][-1]}°F. "
                  f"Official {row['actual_f']:.0f}°F. Short-range point {row['forecast']['mean_f']:.0f}°F.",
        "forecast": row["nbp"], "short_range_forecast": row["forecast"],
        "official_report": row.get("official_report") or row.get("label"),
    } for row in ranked[:3]]


def study(data, *, high_path=None, low_path=None, from_input=None):
    data = Path(data)
    inputs = _json(from_input) if from_input else make_inputs(data, high_path, low_path)
    path, digest = research_model.freeze_inputs(data, inputs)
    metrics, cases, snapshots, contrasts, validation = [], [], {}, {}, {}
    for family, profiles in inputs["cases"].items():
        results, groups = _scores(profiles, family)
        metrics.extend(results)
        cases.extend(_cases(family, groups["test"]))
        snapshots[family] = groups
        contrasts[family] = groups["contrasts"]
        validation[family] = groups["selection"]
    record = {
        "version": VERSION, "title": "Temperature probabilities from NOAA percentiles",
        "hypothesis": "NOAA native percentile shape or a learned spread improves bucket probabilities over an issued mean and Gaussian spread.",
        "target": "Daily climate high and minimum across the dated station-card archive",
        "venues": [], "date_window": inputs["windows"], "metrics": metrics, "cases": cases,
        "limits": [
            "All methods use the same prior 21:00 LST cases. Native NBP cards use the previous day's 13Z run. Short-range NBS can have a later eligible issue.",
            "The comparison between two NBP methods holds issue time and input forecast constant. Their five printed quantiles are rounded whole degrees.",
            "The percentile distribution linearly interpolates the central published points, uses exponential tails at 10% and 90%, and calibrates no extra precision inside the extrapolated tails.",
            "The spread candidate fits earlier absolute weather errors with Ridge and retains the issued probabilistic mean. No model uses a later date's observations.",
        ],
        "provenance": {"input_path": str(path.relative_to(data)), "input_sha256": digest,
                       "weather_inputs": inputs["weather_inputs"], "source_sha256": research_model.run_sources(
                           sys.modules[__name__], expansion_nbp, research_model),
                       "scikit_learn_version": sklearn_version,
                       "settings": {"issue": "previous day 13Z", "tail_probabilities": [0.10, 0.90],
                                    "minimum_sd_f": 0.75, "ridge_alpha": 10.0}},
        "contrasts": contrasts, "selection_metrics": validation, "missing": inputs["missing"],
    }
    return research_model.write_record(data, record, snapshots)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path(".data"))
    parser.add_argument("--high-input", type=Path)
    parser.add_argument("--low-input", type=Path)
    parser.add_argument("--from-input", type=Path)
    args = parser.parse_args()
    print(study(args.data_dir, high_path=args.high_input, low_path=args.low_input, from_input=args.from_input))


if __name__ == "__main__":
    main()
