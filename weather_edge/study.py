"""Fit and evaluate the versioned multi-station daily-high study."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import statistics
from bisect import bisect_right
from collections import defaultdict
from datetime import date, datetime, timedelta
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from tempfile import NamedTemporaryFile

import joblib
from sklearn import __version__ as sklearn_version
from sklearn.feature_extraction import DictVectorizer
from sklearn.linear_model import Ridge
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from . import model_evidence
from .model import (
    DATA_DIR,
    DECISION_HOURS_LST,
    MIN_BAND_DAYS,
    MIN_TAIL_SUPPORT_DAYS,
    RESIDUAL_SMOOTHING_F,
    _benchmark_buckets,
    _limits,
    _outcome,
    _probability,
    _quantile,
    _snapshot,
)
from .model_study_data import DEFAULT_LAG, OBSERVATION_LAGS, STATIONS, VERSION, WINDOWS, prepare_inputs, study_dir
from .nbm import climate_window, iso, utc

METHODS = ("noaa", "station_bias", "ridge", "ridge_sky")
METHOD_LABELS = {"noaa": "NOAA TXN", "station_bias": "Station/time bias", "ridge": "Ridge", "ridge_sky": "Ridge + forecast sky"}
MODEL_VERSION = f"daily-high-{VERSION}"
SELECTION_VERSION = f"{MODEL_VERSION}-pretest"
OPERATIONAL_VERSION = f"{MODEL_VERSION}-operational"
ARTIFACT_SCHEMA = 2
RIDGE_ALPHA = 10.0


def load_selected(data_dir: Path = DATA_DIR, *, vintage: str | None = None) -> dict | None:
    """Load the selected method from the trusted local study artifact."""
    root = study_dir(data_dir)
    if vintage is not None:
        if len(vintage) != 64 or any(letter not in "0123456789abcdef" for letter in vintage):
            raise ValueError("Invalid study vintage")
        root = root / "vintages" / vintage
    artifact_path = root / "selected.joblib"
    if not artifact_path.is_file():
        return None
    return decode_selected(data_dir, artifact_path.read_bytes(), vintage=vintage)


def decode_selected(data_dir, artifact_bytes, *, vintage=None):
    digest = hashlib.sha256(artifact_bytes).hexdigest()
    if vintage is not None and digest != vintage:
        raise ValueError("Study vintage does not match its artifact")
    artifact = joblib.load(BytesIO(artifact_bytes))
    if artifact["schema_version"] != ARTIFACT_SCHEMA or artifact["version"] != MODEL_VERSION:
        raise ValueError("Study artifact version does not match this source. Re-run the study.")
    artifact["card_cache"] = study_dir(data_dir) / "nbm-cards"
    artifact["vintage"] = digest
    return artifact


def _decision_times(day: str | date, offset: int) -> list[datetime]:
    start, _ = climate_window(day, offset)
    return [start + timedelta(hours=hour) for hour in DECISION_HOURS_LST]


def _sky_features(row: dict) -> dict[str, float]:
    hourly = row["forecast"].get("hourly", ())
    if not hourly:
        return {"has_peak_sky": 0.0, "peak_sky_pct": 0.0}
    peak = utc(max(hourly, key=lambda point: point["temp_f"])["valid_at"])
    sky = [float(point["sky_pct"]) for point in hourly
           if point.get("sky_pct") is not None and abs(utc(point["valid_at"]) - peak) <= timedelta(hours=1)]
    return {"has_peak_sky": float(bool(sky)), "peak_sky_pct": statistics.mean(sky) if sky else 0.0}


def _features(row: dict, *, sky: bool) -> dict:
    return {**row["features"], **_sky_features(row)} if sky else row["features"]


@lru_cache(maxsize=512)
def _read_card(path: Path) -> dict:
    return json.loads(path.read_text())


def _attach_archived_sky(row: dict, cache: Path | None) -> dict:
    forecast = row["forecast"]
    if cache is None or not forecast.get("hourly_source") or _sky_features(row)["has_peak_sky"]:
        return row
    issue = utc(forecast["issued_at"])
    path = cache / f"{row['station']}-{issue:%Y%m%d-%H}-nbh.json"
    if not path.is_file():
        return row
    card = _read_card(path)
    if (card["source"] != forecast["hourly_source"] or utc(card["issued_at"]) != issue
            or utc(card["available_at"]) > utc(forecast["available_at"])):
        return row
    weather = {(utc(point["valid_at"]), point["temp_f"]): point["sky_pct"] for point in card["hourly"]
               if point.get("sky_pct") is not None}
    points = []
    for point in forecast.get("hourly", []):
        sky_pct = weather.get((utc(point["valid_at"]), point["temp_f"]))
        points.append({**point, "sky_pct": sky_pct} if point.get("sky_pct") is None and sky_pct is not None else point)
    return {**row, "forecast": {**forecast, "hourly": points}}


def _mean(artifact: dict, row: dict, method: str) -> float:
    baseline = row["noaa_high_f"]
    if method == "noaa":
        return baseline
    if method == "station_bias":
        bias = artifact["station_bias"][row["station"]]
        return baseline + bias.get(row["band"], bias["all"])
    estimator = artifact["estimators"][method]
    return baseline + float(estimator.predict([_features(row, sky=method == "ridge_sky")])[0])


def _distribution(artifact: dict, row: dict, method: str) -> tuple[float, list[float], float, list[float], int]:
    mean = _mean(artifact, row, method)
    station_cal = artifact["calibration"][method][row["station"]]
    calibration = station_cal.get(row["band"], station_cal["all"])
    residual_centers = [mean + error for error in calibration["residuals_f"]]
    if method == "noaa":
        spread = float(row["noaa_sd_f"] or calibration["residual_sd_f"])
        return mean, [mean], spread, residual_centers, calibration["days"]
    return mean, residual_centers, RESIDUAL_SMOOTHING_F, residual_centers, calibration["days"]


def _partition(buckets: list[dict]) -> str:
    if not buckets:
        return "empty"
    ranges = sorted(_limits(bucket) for bucket in buckets)
    if any(next_lo < hi for (_, hi), (next_lo, _) in zip(ranges, ranges[1:])):
        return "overlap"
    if ranges[0][0] != -math.inf or ranges[-1][1] != math.inf:
        return "partial"
    return "exhaustive" if all(hi == next_lo for (_, hi), (next_lo, _) in zip(ranges, ranges[1:])) else "partial"


def _bucket_estimates(artifact: dict, row: dict, method: str, buckets: list[dict], *, interval: bool = False) -> dict:
    mean, centers, scale, support_centers, days = _distribution(artifact, row, method)
    baseline = (centers, scale) if method == "noaa" else _distribution(artifact, row, "noaa")[1:3]
    estimates = []
    for bucket in buckets:
        bounds = lower, upper = _limits(bucket)
        yes_support = sum(lower <= center < upper for center in support_centers)
        no_support = len(support_centers) - yes_support
        probability = _probability(bounds, centers, scale)
        estimates.append({"id": bucket["id"], "probability": probability, "no_probability": 1 - probability,
                          "baseline_probability": _probability(bounds, *baseline),
                          "uncertain": days < MIN_BAND_DAYS or min(yes_support, no_support) < MIN_TAIL_SUPPORT_DAYS,
                          "validation_days": days, "validation_residuals": len(support_centers),
                          "yes_tail_support": yes_support, "no_tail_support": no_support})
    result = {"model_high_f": mean, "buckets": estimates, "partition": _partition(buckets),
              "probability_mass": math.fsum(estimate["probability"] for estimate in estimates),
              "observed_high_f": row["observed_high_f"], "noaa_high_f": row["noaa_high_f"],
              "version": artifact["version"], "method": method, "method_label": METHOD_LABELS[method]}
    if interval:
        if method == "noaa":
            normal = statistics.NormalDist(mean, scale)
            result.update(q10_f=normal.inv_cdf(0.1), q90_f=normal.inv_cdf(0.9))
        else:
            result.update(q10_f=_quantile(centers, 0.1), q90_f=_quantile(centers, 0.9))
    return result


def _event_snapshot(event: dict, at: datetime) -> dict | None:
    station = event["station"]
    return _snapshot(station=station, day=event["date"], as_of=at, forecasts=event.get("forecasts", []),
                     observations=event.get("observations", []),
                     standard_utc_offset_hours=event.get("standard_utc_offset_hours", STATIONS[station]))


def predict(artifact: dict | None, event: dict, at: str | datetime,
            knowledge_at: str | datetime | None = None) -> dict | None:
    """Produce odds from the selected method using only captured evidence available at the cursor."""
    if artifact is None or event["station"] not in artifact["stations"]:
        return None
    as_of = utc(at)
    evidence_at = as_of if knowledge_at is None else min(as_of, utc(knowledge_at))
    if evidence_at < utc(artifact["evidence_cutoff"]):
        return None
    row = _event_snapshot(event, evidence_at)
    if row is None:
        return None
    operational = evidence_at >= utc(artifact["operational_from"])
    planned_method = artifact["operational_method"] if operational else artifact["selected"]
    method = planned_method
    fallback = None
    if method == "ridge_sky":
        row = _attach_archived_sky(row, artifact.get("card_cache"))
        if not _sky_features(row)["has_peak_sky"]:
            method = "ridge"
            fallback = "Forecast sky cover near the temperature peak is unavailable"
    forecast = row["forecast"]
    return {**_bucket_estimates(artifact, row, method, event.get("buckets", []), interval=True),
            "version": OPERATIONAL_VERSION if operational else SELECTION_VERSION,
            "vintage": artifact.get("vintage"),
            "selected_method": planned_method, "fallback_reason": fallback,
            "at": iso(as_of), "knowledge_at": iso(evidence_at), "issued_at": forecast["issued_at"],
            "available_at": forecast["available_at"], "valid_at": forecast["valid_at"]}


def _split_label_cutoff(split: str) -> datetime | None:
    order = tuple(WINDOWS)
    index = order.index(split)
    if index == len(order) - 1:
        return None
    following_start = WINDOWS[order[index + 1]][0]
    return min(_decision_times(following_start, offset)[0] for offset in STATIONS.values())


def _snapshots(inputs: dict, lag: int, splits=tuple(WINDOWS)) -> tuple[dict, list[dict]]:
    rows = {split: [] for split in splits}
    gaps = []
    for station, evidence in inputs["stations"].items():
        offset = STATIONS[station]
        observations = defaultdict(list)
        for item in evidence["observations"]:
            reported_at = utc(item["at"])
            day = str((reported_at + timedelta(hours=offset)).date())
            observations[day].append({**item, "available_at": iso(reported_at + timedelta(minutes=lag))})
        for split in splits:
            start, end = WINDOWS[split]
            cutoff = _split_label_cutoff(split)
            for offset_days in range((end - start).days + 1):
                day = str(start + timedelta(days=offset_days))
                label = evidence["labels"].get(day)
                if not label or (cutoff is not None and utc(label["available_at"]) >= cutoff):
                    gaps.append({"split": split, "station": station, "date": day, "issue": "No eligible complete CLI label"})
                    continue
                for at in _decision_times(day, offset):
                    snapshot = _snapshot(station=station, day=day, as_of=at, forecasts=evidence["forecasts"].get(day, []),
                                         observations=observations[day], standard_utc_offset_hours=offset)
                    if snapshot is None:
                        gaps.append({"split": split, "station": station, "date": day, "at": iso(at),
                                     "issue": "No eligible NBS forecast at decision"})
                        continue
                    rows[split].append({**snapshot, "target_f": label["high_f"],
                                        "label_available_at": label["available_at"], "label_product": label["product"]})
    return rows, gaps


def _same_weights_day(group: list[tuple[dict, float]]) -> list[float]:
    errors = defaultdict(list)
    for row, prediction in group:
        errors[row["date"]].append(row["target_f"] - prediction)
    return [statistics.mean(values) for values in errors.values()]


def _fit_predictors(training: list[dict]) -> dict:
    estimators = {}
    target = [row["target_f"] - row["noaa_high_f"] for row in training]
    for method in ("ridge", "ridge_sky"):
        ridge = Pipeline([("vectorizer", DictVectorizer(sparse=False)), ("scale", StandardScaler()), ("ridge", Ridge(alpha=RIDGE_ALPHA))])
        ridge.fit([_features(row, sky=method == "ridge_sky") for row in training], target)
        estimators[method] = ridge
    groups = defaultdict(list)
    for row in training:
        pair = (row, row["noaa_high_f"])
        groups[row["station"], row["band"]].append(pair)
        groups[row["station"], "all"].append(pair)
    bias = {station: {} for station in STATIONS}
    for (station, band), pairs in groups.items():
        bias[station][band] = statistics.mean(_same_weights_day(pairs))
    return {"version": MODEL_VERSION, "schema_version": ARTIFACT_SCHEMA, "stations": list(STATIONS),
            "estimators": estimators, "station_bias": bias}


def _calibrate(artifact: dict, rows: list[dict]) -> dict:
    distributions = {}
    for method in METHODS:
        by_group = defaultdict(list)
        for row in rows:
            pair = (row, _mean(artifact, row, method))
            by_group[row["station"], row["band"]].append(pair)
            by_group[row["station"], "all"].append(pair)
        distributions[method] = {station: {} for station in STATIONS}
        for (station, band), group in by_group.items():
            residuals = _same_weights_day(group)
            mean_residual = statistics.mean(residuals)
            distributions[method][station][band] = {
                "residuals_f": [residual - mean_residual for residual in residuals],
                "residual_sd_f": max(statistics.pstdev(residuals), RESIDUAL_SMOOTHING_F),
                "mean_residual_f": mean_residual, "days": len(residuals), "snapshots": len(group),
            }
    return distributions


def _daily_means(rows: list[dict], key: str) -> list[float]:
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["station"], row["date"]].append(row[key])
    return [statistics.mean(values) for values in grouped.values()]


def _metrics(rows: list[dict]) -> dict:
    return {"dates": len({row["date"] for row in rows}),
            "station_days": len({(row["station"], row["date"]) for row in rows}), "snapshots": len(rows),
            "bucket_cases": sum(row["bucket_count"] for row in rows),
            "mae_f": statistics.mean(_daily_means(rows, "abs_error_f")),
            "rmse_f": math.sqrt(statistics.mean(_daily_means(rows, "squared_error_f"))),
            "bias_f": statistics.mean(_daily_means(rows, "error_f")),
            "bucket_brier": statistics.mean(_daily_means(rows, "brier"))}


def _evaluate(artifact: dict, rows: list[dict], *, detail: bool = True) -> dict:
    predictions = defaultdict(list)
    partitions = {}
    for row in rows:
        key = row["station"], row["date"]
        buckets = partitions.setdefault(key, _benchmark_buckets(round(row["noaa_high_f"])))
        actual = row["target_f"]
        truth = [_outcome(bucket, actual) for bucket in buckets]
        for method in METHODS:
            prediction = _bucket_estimates(artifact, row, method, buckets)
            error = prediction["model_high_f"] - actual
            probabilities = [item["probability"] for item in prediction["buckets"]]
            brier = statistics.mean((probability - outcome) ** 2
                                    for probability, outcome in zip(probabilities, truth, strict=True))
            predictions[method].append({"station": row["station"], "date": row["date"], "at": row["as_of"],
                                        "forecast_issued_at": row["forecast"]["issued_at"], "band": row["band"],
                                        "forecast_high_f": prediction["model_high_f"], "actual_f": actual,
                                        "error_f": error, "abs_error_f": abs(error), "squared_error_f": error ** 2,
                                        "brier": brier, "bucket_count": len(buckets),
                                        "winning_probability": probabilities[truth.index(1)],
                                        "sky_available": bool(_sky_features(row)["has_peak_sky"]),
                                        "probability_mass": prediction["probability_mass"]})
    output = {"methods": {method: _metrics(records) for method, records in predictions.items()}}
    if not detail:
        return output
    output["by_station"] = {}
    for station in STATIONS:
        output["by_station"][station] = {
            method: _metrics([row for row in records if row["station"] == station])
            for method, records in predictions.items()}
    output["by_time_band"] = {}
    for band in dict.fromkeys(row["band"] for row in predictions[METHODS[0]]):
        output["by_time_band"][band] = {method: _metrics([row for row in records if row["band"] == band])
                                         for method, records in predictions.items()}
    output["worst_misses"] = {}
    for method, records in predictions.items():
        worst_per_day = {}
        for row in sorted(records, key=lambda item: item["abs_error_f"], reverse=True):
            worst_per_day.setdefault((row["station"], row["date"]), row)
        output["worst_misses"][method] = list(worst_per_day.values())[:6]
    output["matched_sky_ablation"] = {}
    for method in ("ridge", "ridge_sky"):
        paired = [row for row in predictions[method] if row["sky_available"]]
        if paired:
            output["matched_sky_ablation"][method] = _metrics(paired)
    output["exhaustive_probability_mass_check"] = {
        "decisions": sum(len(records) for records in predictions.values()),
        "max_absolute_error": max(abs(row["probability_mass"] - 1) for records in predictions.values() for row in records),
    }
    output["by_decision"] = dict(predictions)
    return output


def _event_sources(data_dir: Path) -> tuple[list[dict], dict]:
    events = {}
    provenance = {}
    start, end = (str(day) for day in WINDOWS["test"])

    def add(event, path, digest):
        if (event.get("station") in STATIONS and start <= event.get("date", "") <= end
                and event.get("buckets") and (event.get("settlement") or {}).get("high_f") is not None):
            event_id = event["id"]
            events[event_id] = event
            provenance[event_id] = {"path": str(path.relative_to(data_dir)), "source_sha256": digest}

    replay = data_dir / "replay.json"
    if replay.is_file():
        content = replay.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        for event in json.loads(content).get("events", []):
            add(event, replay, digest)
    for path in sorted((data_dir / "archive").glob("????-??-??/K???.json")):
        content = path.read_bytes()
        add(json.loads(content), path, hashlib.sha256(content).hexdigest())
    return list(events.values()), provenance


def _matched_markets(artifact: dict, events: list[dict], card_cache: Path) -> dict:
    cases = []
    test_start, test_end = (str(day) for day in WINDOWS["test"])
    minute = timedelta(minutes=1)
    for event in events:
        station, day = event["station"], event["date"]
        settlement = event.get("settlement") or {}
        buckets = event.get("buckets") or []
        if station not in STATIONS or not test_start <= day <= test_end or not buckets or settlement.get("high_f") is None:
            continue
        price_series = {}
        observation_timing = event.get("observation_timing") or (
            "MADIS receipt" if any("MADIS" in item.get("source", "") for item in event.get("observations", ()))
            else "Archive proxy or capture receipt"
        )
        for bucket in buckets:
            quotes = []
            for index, item in enumerate(bucket.get("prices", [])):
                if item.get("yes") is None:
                    continue
                sample = utc(item["at"])
                receipt = utc(item["available_at"]) if item.get("available_at") else sample + minute
                if sample <= receipt and 0 <= float(item["yes"]) <= 1:
                    quotes.append((receipt, sample, index))
            price_series[bucket["id"]] = sorted(quotes)
        trade_end = utc(event.get("trading_closed_at") or event["ends_at"])
        for decision in _decision_times(day, STATIONS[station]):
            if not utc(event["starts_at"]) <= decision < trade_end or decision < utc(artifact["evidence_cutoff"]):
                continue
            snapshot = _event_snapshot(event, decision)
            if snapshot is None:
                continue
            snapshot = _attach_archived_sky(snapshot, card_cache)
            sky_available = bool(_sky_features(snapshot)["has_peak_sky"])
            estimates = {method: {estimate["id"]: estimate for estimate in _bucket_estimates(artifact, snapshot, method, buckets)["buckets"]}
                         for method in METHODS}
            for bucket in buckets:
                series = price_series[bucket["id"]]
                index = bisect_right(series, (decision, decision, len(series))) - 1
                if index < 0:
                    continue
                receipt, sample, quote_index = series[index]
                if decision - sample > minute:
                    continue
                actual = _outcome(bucket, settlement["high_f"])
                if bucket.get("result") is not None and bool(bucket["result"]) != bool(actual):
                    continue
                quote = bucket["prices"][quote_index]
                cases.append({"event": event["id"], "station": station, "date": day, "at": iso(decision),
                              "observation_timing": observation_timing,
                              "bucket": bucket["label"], "bucket_id": bucket["id"], "outcome": actual,
                              "quote_at": iso(sample), "quote_available_at": iso(receipt),
                              "forecast_issued_at": snapshot["forecast"]["issued_at"],
                              "method": artifact["selected"], "version": SELECTION_VERSION, "sky_available": sky_available,
                              "market_display_yes": float(quote["yes"]),
                              **{method: estimates[method][bucket["id"]]["probability"] for method in METHODS}})
    scores = {method: statistics.mean((row[method] - row["outcome"]) ** 2 for row in cases)
              for method in (*METHODS, "market_display_yes")} if cases else {}
    by_station = {}
    for station in STATIONS:
        # Preserve coverage differences instead of treating absent station prices as losses.
        subset = [row for row in cases if row["station"] == station]
        if subset:
            by_station[station] = {"cases": len(subset), "dates": len({row["date"] for row in subset}),
                                   "brier": {method: statistics.mean((row[method] - row["outcome"]) ** 2 for row in subset)
                                             for method in (*METHODS, "market_display_yes")}}
    return {"cases": len(cases), "calendar_dates": len({row["date"] for row in cases}),
            "station_days": len({(row["station"], row["date"]) for row in cases}),
            "snapshots": len({(row["event"], row["at"]) for row in cases}), "brier": scores,
            "best_method": min(scores, key=scores.get) if scores else None,
            "by_station": by_station, "by_case": cases,
            "matching": "Test dates. Exact settled bucket; archived YES display sample available by decision and labeled no more than 60 seconds earlier. No simulated depth or fills."}


def _coefficients(artifact: dict) -> dict:
    summary = {}
    for method, ridge in artifact["estimators"].items():
        weights = ridge["ridge"].coef_.tolist()
        features = ridge["vectorizer"].get_feature_names_out()
        summary[method] = {"intercept_f": float(ridge["ridge"].intercept_),
                           "standardized_coefficients_f": dict(zip(features, weights, strict=True))}
    return summary


def _write_atomically(path: Path, writer) -> None:
    with NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}-", delete=False) as handle:
        temporary = Path(handle.name)
        try:
            writer(handle)
            handle.flush()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def _write_if_missing(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        if path.read_bytes() != content:
            raise ValueError(f"Archived study content differs at {path.name}")
        return
    _write_atomically(path, lambda handle: handle.write(content))


def _snapshot_vintage(root: Path, artifact_bytes: bytes, report_bytes: bytes, input_bytes: bytes | None,
                      market_bytes: bytes | None = None) -> str:
    vintage = hashlib.sha256(artifact_bytes).hexdigest()
    folder = root / "vintages" / vintage
    _write_if_missing(folder / "selected.joblib", artifact_bytes)
    if input_bytes is not None:
        _write_if_missing(folder / "inputs.json.gz", input_bytes)
    evaluation = folder / "evaluations" / hashlib.sha256(report_bytes).hexdigest()
    if market_bytes is not None:
        _write_if_missing(evaluation / "market-inputs.json.gz", market_bytes)
    _write_if_missing(evaluation / "report.json", report_bytes)
    return vintage


def _preserve_current(root: Path, input_bytes: bytes, inputs_digest: str) -> None:
    previous_model = root / "selected.joblib"
    previous_report = root / "report.json"
    if not previous_model.is_file() or not previous_report.is_file():
        return
    report_bytes = previous_report.read_bytes()
    report = json.loads(report_bytes)
    if report.get("vintage"):
        # The current aliases were already published from an immutable snapshot.
        return
    previous_inputs = input_bytes if report.get("inputs_sha256") == inputs_digest else None
    _snapshot_vintage(root, previous_model.read_bytes(), report_bytes, previous_inputs)


def fit_study(data_dir: Path = DATA_DIR) -> dict:
    data_dir = Path(data_dir)
    root = study_dir(data_dir)
    inputs = prepare_inputs(data_dir)
    input_bytes = (root / "inputs.json.gz").read_bytes()
    inputs_digest = hashlib.sha256(input_bytes).hexdigest()
    groups, gaps = _snapshots(inputs, DEFAULT_LAG)
    for split, rows in groups.items():
        if {row["station"] for row in rows} != set(STATIONS):
            raise ValueError(f"Study lacks evidence from one or more stations in {split}")
    artifact = _fit_predictors(groups["fit"])
    artifact["calibration"] = _calibrate(artifact, groups["calibration"])
    artifact["evidence_cutoff"] = max(row["label_available_at"] for split in ("fit", "calibration", "selection")
                                     for row in groups[split])
    artifact["inputs_sha256"] = inputs_digest
    artifact["training_sklearn_version"] = sklearn_version
    selection = _evaluate(artifact, groups["selection"])
    artifact["selected"] = min(METHODS, key=lambda method: selection["methods"][method]["bucket_brier"])
    print(f"Selected {METHOD_LABELS[artifact['selected']]} on pre-test dates by bucket Brier", flush=True)
    test = _evaluate(artifact, groups["test"])
    artifact["operational_method"] = min(METHODS, key=lambda method: test["methods"][method]["bucket_brier"])
    artifact["operational_from"] = max(row["label_available_at"] for row in groups["test"])
    sensitivity = {}
    for lag in OBSERVATION_LAGS:
        if lag == DEFAULT_LAG:
            sensitivity[str(lag)] = {"methods": test["methods"]}
            continue
        other, _ = _snapshots(inputs, lag, splits=("test",))
        sensitivity[str(lag)] = _evaluate(artifact, other["test"], detail=False)
    artifact_buffer = BytesIO()
    joblib.dump(artifact, artifact_buffer)
    artifact_bytes = artifact_buffer.getvalue()
    vintage = hashlib.sha256(artifact_bytes).hexdigest()
    market_events, market_sources = _event_sources(data_dir)
    market_inputs = json.dumps({"events": market_events, "sources": market_sources},
                               separators=(",", ":"), allow_nan=False).encode()
    market_bytes = gzip.compress(market_inputs, mtime=0)
    source_root = Path(__file__).resolve().parent
    code_hashes = {name: hashlib.sha256((source_root / name).read_bytes()).hexdigest() for name in (
        "study.py", "model.py", "model_study_data.py", "model_data.py", "nbm.py",
    )}
    report = {"version": MODEL_VERSION, "selection_method": artifact["selected"],
              "vintage": vintage,
              "evaluation_inputs": {"market_sha256": hashlib.sha256(market_bytes).hexdigest(),
                                    "market_sources": market_sources, "code_sha256": code_hashes,
                                    "scikit_learn_version": sklearn_version},
              "selection_method_label": METHOD_LABELS[artifact["selected"]],
              "selection_version": SELECTION_VERSION,
              "operational_method": artifact["operational_method"],
              "operational_method_label": METHOD_LABELS[artifact["operational_method"]],
              "operational_version": OPERATIONAL_VERSION, "operational_from": artifact["operational_from"],
              "selection_rule": "Lowest mean per-station day bucket Brier on October 15–27. Ties favor NOAA, then bias, then Ridge, then Ridge with sky.",
              "evidence_cutoff": artifact["evidence_cutoff"], "inputs_sha256": inputs_digest,
              "decision_hours_local_standard": DECISION_HOURS_LST, "method_labels": METHOD_LABELS,
              "groups": {split: {"window": inputs["windows"][split], "snapshots": len(rows),
                                 "dates": len({row["date"] for row in rows}),
                                 "station_days": len({(row["station"], row["date"]) for row in rows}),
                                 "by_station": {station: len({row["date"] for row in rows if row["station"] == station})
                                                for station in STATIONS}}
                         for split, rows in groups.items()},
              "selection": selection, "test": test, "observation_lag_sensitivity": sensitivity,
              "weather_test_winners": {metric: min(METHODS, key=lambda method: test["methods"][method][metric])
                                       for metric in ("bucket_brier", "mae_f", "rmse_f")},
              "matched_market": _matched_markets(artifact, market_events, root / "nbm-cards"),
              "predictors": _coefficients(artifact), "station_bias_f": artifact["station_bias"],
              "source_gaps": {station: evidence["archive_gaps"] for station, evidence in inputs["stations"].items()},
              "snapshot_gaps": gaps, "source_urls": {station: evidence["sources"] for station, evidence in inputs["stations"].items()}}
    report_bytes = (json.dumps(report, indent=2, allow_nan=False) + "\n").encode()
    _preserve_current(root, input_bytes, inputs_digest)
    _snapshot_vintage(root, artifact_bytes, report_bytes, input_bytes, market_bytes)
    for filename, content in (("selected.joblib", artifact_bytes), ("report.json", report_bytes)):
        path = root / filename
        if not path.is_file() or path.read_bytes() != content:
            _write_atomically(path, lambda handle: handle.write(content))
    model_evidence.published(data_dir, "study", vintage, root / "vintages" / vintage / "selected.joblib")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR, help="Local cache and output directory")
    args = parser.parse_args()
    report = fit_study(args.data_dir)
    summary = {"selected": report["selection_method_label"], "evidence_cutoff": report["evidence_cutoff"],
               "vintage": report["vintage"],
               "operational_method": report["operational_method_label"], "operational_from": report["operational_from"],
               "groups": report["groups"], "test": report["test"]["methods"],
               "matched_market": {key: report["matched_market"][key] for key in ("cases", "station_days", "brier")}}
    print(json.dumps(summary, indent=2))
    print(f"Saved under {study_dir(args.data_dir)}")


if __name__ == "__main__":
    main()
