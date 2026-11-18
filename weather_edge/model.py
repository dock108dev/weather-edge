"""Learn and evaluate as-issued NOAA forecast corrections for Central Park daily highs."""

from __future__ import annotations

import argparse
import json
import math
import statistics
from io import BytesIO
from bisect import bisect_right
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import joblib
from sklearn.feature_extraction import DictVectorizer
from sklearn.linear_model import Ridge
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from weather_edge import model_evidence
from weather_edge.model_data import HISTORICAL_OBSERVATION_LAG, STATION, archive_days, asos_url, load_asos, load_daily_highs
from weather_edge.nbm import climate_window, iso, utc

DATA_DIR = Path(__file__).resolve().parents[1] / ".data"
MODEL_PATH = DATA_DIR / "model" / "model.joblib"
START = date(2026, 8, 1)
TRAIN_END = date(2026, 10, 7)
VALIDATION_START = date(2026, 10, 9)
VALIDATION_END = date(2026, 10, 28)
TEST_START = date(2026, 10, 30)
END = date(2026, 11, 12)
DECISION_HOURS_LST = (-3, 3, 9, 13, 17, 21)
RESIDUAL_SMOOTHING_F = 0.5
MIN_TAIL_SUPPORT_DAYS = 5
MIN_BAND_DAYS = 8
MAX_FORECAST_AGE = timedelta(hours=36)
ARTIFACT_VERSION = 1


def load_model(path: Path = MODEL_PATH) -> dict | None:
    """Load only the trusted local artifact produced by this module."""
    path = Path(path)
    if not path.is_file():
        return None
    return decode_model(path.read_bytes())


def decode_model(blob: bytes) -> dict:
    artifact = joblib.load(BytesIO(blob))
    if artifact["schema_version"] != ARTIFACT_VERSION:
        raise ValueError("Model format has changed. Re-run training.")
    return artifact


def _hourly_at(hourly: list[tuple[datetime, float]], at: datetime) -> float | None:
    for (left_at, left_f), (right_at, right_f) in zip(hourly, hourly[1:]):
        if left_at <= at <= right_at and right_at - left_at == timedelta(hours=1):
            fraction = (at - left_at) / (right_at - left_at)
            return left_f + (right_f - left_f) * fraction
    return next((temp_f for valid_at, temp_f in hourly if valid_at == at), None)


def _observed_temperatures(observations: list[dict], start: datetime, end: datetime,
                           as_of: datetime) -> list[tuple[datetime, float]]:
    available = {}
    for item in observations:
        timestamp, receipt = utc(item["at"]), utc(item["available_at"])
        temperature = float(item["temp_f"])
        if not (start <= timestamp < end and timestamp <= receipt <= as_of and math.isfinite(temperature)):
            continue
        # For equal report times, prefer NWS over AWC and the latest correction within a source.
        priority = (item.get("source") == "NWS", receipt)
        prior = available.get(timestamp)
        if prior is None or priority >= prior[0]:
            available[timestamp] = (priority, temperature)
    return [(timestamp, entry[1]) for timestamp, entry in sorted(available.items())]


def _snapshot(*, station: str, day: str, as_of: datetime, forecasts: list[dict], observations: list[dict],
              standard_utc_offset_hours: int) -> dict | None:
    start, end = climate_window(day, standard_utc_offset_hours)
    eligible = [forecast for forecast in forecasts
                if forecast.get("high_source") == "noaa-nbm-nbs-txn"
                and utc(forecast["issued_at"]) <= utc(forecast["available_at"]) <= as_of
                and start <= utc(forecast["valid_at"]) < end
                and as_of - utc(forecast["issued_at"]) <= MAX_FORECAST_AGE]
    if not eligible or not start - timedelta(hours=24) <= as_of < end:
        return None
    forecast = max(eligible, key=lambda item: utc(item["issued_at"]))
    available = _observed_temperatures(observations, start, end, as_of)
    hourly = sorted((utc(item["valid_at"]), float(item["temp_f"])) for item in forecast.get("hourly", []))
    observed_high = max((temperature for _, temperature in available), default=None)
    station_hour = (as_of - start).total_seconds() / 3600
    lead = (utc(forecast["valid_at"]) - utc(forecast["issued_at"])).total_seconds() / 3600
    features = {
        "station": station,
        "nbs_high_f": float(forecast["high_f"]),
        "forecast_lead_hours": lead,
        "local_standard_hour": station_hour,
        "local_standard_hour_squared": station_hour ** 2,
        "has_day_observation": float(observed_high is not None),
        "observed_high_minus_nbs_f": 0.0 if observed_high is None else observed_high - forecast["high_f"],
        "latest_observation_age_hours": 0.0,
        "has_recent_forecast_comparison": 0.0,
        "latest_observation_minus_nbh_f": 0.0,
        "has_recent_trend_comparison": 0.0,
        "three_hour_observed_minus_nbh_change_f": 0.0,
    }
    if available:
        latest_at, latest_temp = available[-1]
        features["latest_observation_age_hours"] = (as_of - latest_at).total_seconds() / 3600
        predicted_at_latest = _hourly_at(hourly, latest_at)
        if predicted_at_latest is not None:
            features["has_recent_forecast_comparison"] = 1.0
            features["latest_observation_minus_nbh_f"] = latest_temp - predicted_at_latest
            previous = next(((timestamp, temperature) for timestamp, temperature in reversed(available)
                             if timedelta(hours=2, minutes=30) <= latest_at - timestamp <= timedelta(hours=3, minutes=30)), None)
            if previous is not None:
                previous_at, previous_temp = previous
                predicted_at_previous = _hourly_at(hourly, previous_at)
                if predicted_at_previous is not None:
                    features["has_recent_trend_comparison"] = 1.0
                    features["three_hour_observed_minus_nbh_change_f"] = (
                        (latest_temp - previous_temp) - (predicted_at_latest - predicted_at_previous)
                    )
    band = _time_band(station_hour)
    return {"station": station, "date": day, "as_of": iso(as_of), "features": features, "band": band,
            "noaa_high_f": float(forecast["high_f"]), "noaa_sd_f": forecast.get("high_sd_f"),
            "observed_high_f": observed_high, "forecast": forecast}


def _time_band(hour: float) -> str:
    if hour < 0:
        return "prior evening"
    if hour < 12:
        return "overnight or morning"
    if hour < 17:
        return "midday"
    return "late day"


def _distribution(artifact: dict, row: dict) -> tuple[float, list[float], float, int]:
    model_high = row["noaa_high_f"] + float(artifact["ridge"].predict([row["features"]])[0])
    residuals = artifact["validation"].get(row["band"], artifact["validation"]["all"])
    baseline_sd = row["noaa_sd_f"] or residuals["baseline_sd_f"]
    return model_high, residuals["residuals_f"], float(baseline_sd), residuals["days"]


def _normal_cdf(z: float) -> float:
    return 0.5 * math.erfc(-z / math.sqrt(2))


def _cdf(value: float, centers: list[float], scale: float) -> float:
    return sum(_normal_cdf((value - center) / scale) for center in centers) / len(centers)


def _limits(bucket: dict) -> tuple[float, float]:
    # Published outcomes are integer Fahrenheit and interval endpoints are inclusive.
    lower_f, upper_f = bucket.get("lower_f"), bucket.get("upper_f")
    if lower_f is None and upper_f is None:
        raise ValueError("A temperature bucket needs at least one endpoint")
    if any(value != int(value) for value in (lower_f, upper_f) if value is not None):
        raise ValueError("Temperature bucket endpoints must be integer Fahrenheit")
    if lower_f is not None and upper_f is not None and upper_f < lower_f:
        raise ValueError("Temperature bucket endpoints are reversed")
    return (-math.inf if lower_f is None else lower_f - 0.5,
            math.inf if upper_f is None else upper_f + 0.5)


def _probability(bounds: tuple[float, float], centers: list[float], scale: float) -> float:
    lower, upper = bounds
    probabilities = []
    for center in centers:
        low_z, up_z = (lower - center) / scale, (upper - center) / scale
        # Evaluate upper-tail differences on the survival scale to preserve small values.
        if low_z > 0:
            probabilities.append(_normal_cdf(-low_z) - _normal_cdf(-up_z))
        else:
            probabilities.append(_normal_cdf(up_z) - _normal_cdf(low_z))
    return sum(probabilities) / len(probabilities)


def _quantile(centers: list[float], mass: float) -> float:
    low = min(centers) - 6 * RESIDUAL_SMOOTHING_F
    high = max(centers) + 6 * RESIDUAL_SMOOTHING_F
    for _ in range(30):
        mid = (high + low) / 2
        if _cdf(mid, centers, RESIDUAL_SMOOTHING_F) < mass:
            low = mid
        else:
            high = mid
    return (high + low) / 2


def _probabilities(row: dict, buckets: list[dict], mean: float, residuals: list[float],
                   baseline_sd: float, validation_days: int) -> list[dict]:
    centers = [mean + error for error in residuals]
    result = []
    for bucket in buckets:
        bounds = _limits(bucket)
        lower, upper = bounds
        support = sum(lower <= center < upper for center in centers)
        no_support = len(centers) - support
        result.append({"id": bucket["id"],
                       "probability": _probability(bounds, centers, RESIDUAL_SMOOTHING_F),
                       "baseline_probability": _probability(bounds, [row["noaa_high_f"]], baseline_sd),
                       "uncertain": validation_days < MIN_BAND_DAYS or min(support, no_support) < MIN_TAIL_SUPPORT_DAYS,
                       "validation_days": validation_days, "validation_residuals": len(centers),
                       "yes_tail_support": support, "no_tail_support": no_support})
    return result


def predict(model: dict | None, *, station: str, day: str, as_of: str | datetime, forecasts: list[dict],
            observations: list[dict], buckets: list[dict], standard_utc_offset_hours: int = -5,
            knowledge_at: str | datetime | None = None) -> dict | None:
    """Return odds from evidence available at the decision point. Projected clocks add no evidence."""
    if model is None or station not in model["stations"]:
        return None
    as_of = utc(as_of)
    evidence_time = as_of if knowledge_at is None else min(as_of, utc(knowledge_at))
    if evidence_time < utc(model["evidence_cutoff"]):
        return None
    row = _snapshot(station=station, day=day, as_of=evidence_time, forecasts=forecasts,
                    observations=observations, standard_utc_offset_hours=standard_utc_offset_hours)
    if row is None:
        return None
    mean, residuals, baseline_sd, validation_days = _distribution(model, row)
    centers = [mean + value for value in residuals]
    forecast = row["forecast"]
    return {"at": iso(as_of), "knowledge_at": iso(evidence_time), "noaa_high_f": row["noaa_high_f"],
            "model_high_f": mean, "q10_f": _quantile(centers, 0.1), "q90_f": _quantile(centers, 0.9),
            "issued_at": forecast["issued_at"], "available_at": forecast["available_at"],
            "valid_at": forecast["valid_at"], "observed_high_f": row["observed_high_f"],
            "buckets": _probabilities(row, buckets, mean, residuals, baseline_sd, validation_days)}


def _decision_times(day: str) -> list[datetime]:
    start, _ = climate_window(day)
    return [start + timedelta(hours=hour) for hour in DECISION_HOURS_LST]


def _split_rows(forecasts_by_day: dict, observations: list[dict], labels: dict) -> tuple[dict, list]:
    splits = {"train": [], "validation": [], "test": []}
    report_gaps = []
    as_of_test = _decision_times(str(TEST_START))[0]
    as_of_validation = _decision_times(str(VALIDATION_START))[0]
    by_day = defaultdict(list)
    for report in observations:
        climate_day = (utc(report["at"]) - timedelta(hours=5)).date()
        by_day[str(climate_day)].append(report)
    for day, forecasts in sorted(forecasts_by_day.items()):
        local_day = date.fromisoformat(day)
        if local_day <= TRAIN_END:
            group, cutoff = "train", as_of_validation
        elif VALIDATION_START <= local_day <= VALIDATION_END:
            group, cutoff = "validation", as_of_test
        elif local_day >= TEST_START:
            group, cutoff = "test", None
        else:
            continue
        label = labels.get(day)
        if label is None or (cutoff is not None and utc(label["available_at"]) >= cutoff):
            report_gaps.append({"date": day, "issue": "No complete CLI label before the next split's first decision"})
            continue
        for decision in _decision_times(day):
            row = _snapshot(station=STATION, day=day, as_of=decision, forecasts=forecasts,
                            observations=by_day[day], standard_utc_offset_hours=-5)
            if row is None:
                report_gaps.append({"date": day, "at": iso(decision), "issue": "No eligible NBS forecast at decision"})
                continue
            splits[group].append({**row, "target_f": label["high_f"], "label_available_at": label["available_at"]})
    return splits, report_gaps


def _calibration(residual_rows: list[tuple[dict, float]]) -> dict:
    by_band = defaultdict(list)
    for row, corrected in residual_rows:
        by_band[row["band"]].append((row, corrected))
        by_band["all"].append((row, corrected))
    result = {}
    for band, pairs in by_band.items():
        # Different decisions on the same date are related, so every date carries equal weight.
        per_day = defaultdict(list)
        for row, corrected in pairs:
            per_day[row["date"]].append((row["target_f"] - corrected, row["target_f"] - row["noaa_high_f"]))
        days = len(per_day)
        if days < MIN_BAND_DAYS and band != "all":
            continue
        ridge_errors = [statistics.mean(item[0] for item in group) for group in per_day.values()]
        noaa_errors = [statistics.mean(item[1] for item in group) for group in per_day.values()]
        error_center = statistics.mean(ridge_errors)
        result[band] = {"residuals_f": [error - error_center for error in ridge_errors], "days": days,
                        "snapshots": len(pairs), "mean_residual_f": error_center,
                        "baseline_sd_f": max(RESIDUAL_SMOOTHING_F, statistics.pstdev(noaa_errors))}
    return result


def _benchmark_buckets(anchor: int) -> list[dict]:
    limits = [(None, anchor - 5), (anchor - 4, anchor - 3), (anchor - 2, anchor - 1),
              (anchor, anchor + 1), (anchor + 2, anchor + 3), (anchor + 4, anchor + 5), (anchor + 6, None)]
    return [{"id": str(index), "lower_f": lo, "upper_f": hi} for index, (lo, hi) in enumerate(limits)]


def _outcome(bucket: dict, temperature: int) -> int:
    lo, hi = _limits(bucket)
    return int(lo <= temperature < hi)


def _scores(rows: list[dict]) -> dict:
    def metrics(subset: list[dict]) -> dict:
        absolute_error = [abs(row["err_f"]) for row in subset]
        return {"mae_f": statistics.mean(absolute_error),
                "rmse_f": math.sqrt(statistics.mean(row["err_f"] ** 2 for row in subset)),
                "bias_f": statistics.mean(row["err_f"] for row in subset),
                "bucket_brier": statistics.mean(row["brier"] for row in subset)}

    noaa_rows = [row for row in rows if row["method"] == "noaa"]
    ridge_rows = [row for row in rows if row["method"] == "ridge"]
    return {"days": len({row["date"] for row in rows}), "snapshots": len(ridge_rows),
            "bucket_cases": sum(row["bucket_count"] for row in ridge_rows),
            "noaa": metrics(noaa_rows), "ridge": metrics(ridge_rows)}


def _evaluate(artifact: dict, test: list[dict]) -> dict:
    rows = []
    by_date = {}
    for row in test:
        day = row["date"]
        baseline = row["noaa_high_f"]
        by_date.setdefault(day, _benchmark_buckets(round(baseline)))
        buckets = by_date[day]
        actual = row["target_f"]
        mean, residuals, baseline_sd, validation_days = _distribution(artifact, row)
        probs = _probabilities(row, buckets, mean, residuals, baseline_sd, validation_days)
        winner = next(item for item, bucket in zip(probs, buckets, strict=True) if _outcome(bucket, actual))
        for method, prediction, metric in (("noaa", baseline, "baseline_probability"),
                                           ("ridge", mean, "probability")):
            brier = statistics.mean((item[metric] - _outcome(bucket, actual)) ** 2
                                    for item, bucket in zip(probs, buckets, strict=True))
            rows.append({"date": day, "band": row["band"], "at": row["as_of"], "forecast_issued_at": row["forecast"]["issued_at"],
                         "method": method, "err_f": prediction - actual, "actual_f": actual, "prediction_f": prediction,
                         "noaa_high_f": baseline, "brier": brier, "winning_probability": winner[metric],
                         "bucket_count": len(buckets)})
    worst = sorted((row for row in rows if row["method"] == "ridge"), key=lambda row: -abs(row["err_f"]))
    distinct = {}
    for row in worst:
        distinct.setdefault(row["date"], row)
    by_band = {band: _scores([row for row in rows if row["band"] == band]) for band in dict.fromkeys(row["band"] for row in rows)}
    return {**_scores(rows), "by_time_band": by_band, "worst_misses": list(distinct.values())[:6],
            "by_decision": rows}


def compare_prices(artifact: dict, replay: dict) -> dict:
    cases = []
    max_quote_age = timedelta(minutes=1)
    for event in replay.get("events", []):
        if not TEST_START.isoformat() <= event["date"] <= END.isoformat() or event["station"] not in artifact["stations"]:
            continue
        actual = event["settlement"]["high_f"]
        buckets = event["buckets"]
        series = {}
        for bucket in buckets:
            prices = []
            for index, item in enumerate(bucket["prices"]):
                if item.get("yes") is None:
                    continue
                sample_at = utc(item["at"])
                available_at = utc(item["available_at"]) if item.get("available_at") else sample_at + max_quote_age
                if sample_at <= available_at:
                    prices.append((available_at, sample_at, index))
            series[bucket["id"]] = sorted(prices)
        for decision in _decision_times(event["date"]):
            if not utc(event["starts_at"]) <= decision < utc(event["ends_at"]):
                continue
            prediction = predict(artifact, station=event["station"], day=event["date"], as_of=decision,
                                 forecasts=event["forecasts"], observations=event["observations"], buckets=buckets)
            if prediction is None:
                continue
            for bucket, probability in zip(buckets, prediction["buckets"], strict=True):
                prices = series[bucket["id"]]
                index = bisect_right(prices, (decision, decision, len(bucket["prices"]))) - 1
                if index < 0:
                    continue
                available_at, sample_at, quote_index = prices[index]
                if decision - sample_at > max_quote_age:
                    continue
                quote = bucket["prices"][quote_index]
                cases.append({"event": event["id"], "date": event["date"], "bucket": bucket["label"], "at": iso(decision),
                              "quote_at": quote["at"], "quote_available_at": iso(available_at), "outcome": _outcome(bucket, actual),
                              "ridge": probability["probability"], "noaa": probability["baseline_probability"],
                              "market_display_yes": float(quote["yes"])})
    scores = ({method: statistics.mean((case[method] - case["outcome"]) ** 2 for case in cases)
               for method in ("noaa", "ridge", "market_display_yes")} if cases else {})
    return {"cases": len(cases), "days": len({case["date"] for case in cases}),
            "snapshots": len({(case["event"], case["at"]) for case in cases}),
            "brier": scores, "by_case": cases,
            "matching": "Untouched test dates, exact contract buckets, latest archived display YES available by decision and labeled no more than 60 seconds earlier, before market close."}


def train(data_dir: Path = DATA_DIR) -> dict:
    output = data_dir / "model"
    output.mkdir(parents=True, exist_ok=True)
    asos_end = END + timedelta(days=2)
    observations = load_asos(output, START, asos_end)
    labels = load_daily_highs(output, START, END)
    print(f"Loaded {len(observations)} IEM ASOS records. Reading as-issued NOAA cards…", flush=True)
    forecasts, archive_gaps = archive_days(data_dir / "nbm-cards", START, END)
    groups, row_gaps = _split_rows(forecasts, observations, labels)
    if any(not group for group in groups.values()):
        raise ValueError("Archive lacks a complete train, validation and test split")
    ridge = Pipeline([("vectorizer", DictVectorizer(sparse=False)), ("scale", StandardScaler()), ("ridge", Ridge(alpha=10.0))])
    ridge.fit([row["features"] for row in groups["train"]], [row["target_f"] - row["noaa_high_f"] for row in groups["train"]])
    validation = groups["validation"]
    corrections = ridge.predict([row["features"] for row in validation])
    residuals = _calibration([(row, row["noaa_high_f"] + float(correction))
                              for row, correction in zip(validation, corrections, strict=True)])
    cutoff = max(utc(row["label_available_at"]) for row in groups["train"] + validation)
    artifact = {"ridge": ridge, "validation": residuals, "stations": [STATION],
                "evidence_cutoff": iso(cutoff), "schema_version": ARTIFACT_VERSION}
    coefficients = dict(zip(ridge["vectorizer"].get_feature_names_out(), ridge["ridge"].coef_.tolist(), strict=True))
    report = {"station": STATION, "model": "Ridge regression on NBS TXN errors", "ridge_alpha": 10.0,
              "evidence_cutoff": iso(cutoff), "decision_hours_local_standard": DECISION_HOURS_LST,
              "sources": {"nbm": "https://vlab.noaa.gov/web/mdl/nbm-textcard-v5.0",
                          "asos": asos_url(START, asos_end), "cli": "https://mesonet.agron.iastate.edu/json/cli.py?station=KNYC&year=2026"},
              "groups": {name: {"days": len({row["date"] for row in rows}), "snapshots": len(rows),
                                "first_date": rows[0]["date"], "last_date": rows[-1]["date"]}
                         for name, rows in groups.items()},
              "calibration": {name: {key: value for key, value in item.items() if key != "residuals_f"}
                              for name, item in residuals.items()},
              "standardized_coefficients_f": coefficients, "correction_intercept_f": float(ridge["ridge"].intercept_),
              "benchmark": _evaluate(artifact, groups["test"]),
              "historical_observation_lag_minutes": HISTORICAL_OBSERVATION_LAG.total_seconds() / 60,
              "archive_gaps": archive_gaps, "snapshot_gaps": row_gaps}
    replay_path = data_dir / "replay.json"
    report["matched_market"] = compare_prices(artifact, json.loads(replay_path.read_text()) if replay_path.exists() else {})
    target_model = output / "model.joblib"
    temporary_artifact = target_model.with_suffix(".tmp")
    joblib.dump(artifact, temporary_artifact)
    temporary_artifact.replace(target_model)
    target_report = output / "report.json"
    temporary_report = target_report.with_suffix(".tmp")
    temporary_report.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    temporary_report.replace(target_report)
    digest, snapshot = model_evidence.legacy_snapshot(data_dir, target_model.read_bytes())
    model_evidence.published(data_dir, "legacy", digest, snapshot)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR, help="Local cache and output directory")
    args = parser.parse_args()
    result = train(args.data_dir)
    print(json.dumps({"groups": result["groups"], "test": {key: result["benchmark"][key] for key in ("noaa", "ridge")},
                      "market": {key: result["matched_market"][key] for key in ("days", "cases", "brier")},
                      "archive_gaps": len(result["archive_gaps"]), "snapshot_gaps": len(result["snapshot_gaps"])}, indent=2))
    print(f"Saved model and report under {args.data_dir / 'model'}")


if __name__ == "__main__":
    main()
