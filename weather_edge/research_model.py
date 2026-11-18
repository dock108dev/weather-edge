"""Reproduce the held-out local daily-low forecast comparison."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import numpy as np
from sklearn import __version__ as sklearn_version
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.feature_extraction import DictVectorizer
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from threadpoolctl import threadpool_limits

from . import expansion_nbm, expansion_weather
from .history import _write
from .model import DECISION_HOURS_LST, _benchmark_buckets, _limits, _outcome, _probability
from .model_study_data import WINDOWS
from .nbm import climate_window, iso, utc

STATIONS = ("KNYC", "KMDW")
NBS_CYCLES = (0, 6, 12, 18)
HOURS = DECISION_HOURS_LST
METHODS = {
    "noaa": "NOAA TXN + XND",
    "bias": "Station and time bias",
    "ridge_no_observation": "Ridge without observations",
    "ridge": "Ridge with observed minimum",
    "ridge_dewpoint": "Ridge + latest observed dew point",
    "nonlinear": "Histogram gradient boosting",
}
VERSION = "daily-low-v1"
# The model holdout uses a full calendar-date block. One later known station-day
# is used only to verify the completed market replay.
FOLLOW_UP = date(2026, 11, 14)
MAX_FORECAST_AGE = timedelta(hours=24)


def _json_bytes(value):
    return (json.dumps(value, separators=(",", ":"), allow_nan=False, sort_keys=True) + "\n").encode()


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _forecast(card, target, offset):
    start, end = climate_window(target, offset)
    point = next((row for row in card["points"]
                  if "TXN" in row and utc(row["valid_at"]).hour == 12 and start <= utc(row["valid_at"]) < end), None)
    if not point:
        return None
    stamp = utc(point["valid_at"])
    return {"issued_at": card["issued_at"], "available_at": card["available_at"], "valid_at": point["valid_at"],
            "nbm_window_start": iso(stamp - timedelta(hours=12)), "nbm_window_end": iso(stamp + timedelta(hours=6)),
            "mean_f": point["TXN"], "sd_f": point.get("XND"), "lead_hours": point["lead_hours"],
            "source": card["source"], "card_sha256": card["card_sha256"]}


def available_forecast(forecasts, decision):
    usable = [row for row in forecasts
              if utc(row["issued_at"]) <= utc(row["available_at"]) <= decision
              and decision - utc(row["issued_at"]) <= MAX_FORECAST_AGE]
    return max(usable, key=lambda row: row["issued_at"], default=None)


def day_forecasts(data, station, day, cards_by_issue=None):
    offset = expansion_weather.STATIONS[station][3]
    forecasts = []
    for day_delta in (-1, 0):
        issue_day = day + timedelta(days=day_delta)
        for cycle in NBS_CYCLES:
            issue = datetime.combine(issue_day, time(cycle), timezone.utc)
            card = (cards_by_issue.get(issue) if cards_by_issue is not None
                    else expansion_nbm.load_card(data, station, issue))
            if card and (forecast := _forecast(card, day, offset)):
                forecasts.append(forecast)
    return forecasts


def make_snapshot(station, day, decision, forecasts, observations, *, delay_minutes=10):
    start, end = climate_window(day, expansion_weather.STATIONS[station][3])
    forecast = available_forecast(forecasts, decision)
    if forecast is None or not start - timedelta(hours=24) <= decision < end:
        return None
    obs = expansion_weather.observations_as_of(observations, start, decision, delay_minutes=delay_minutes)
    observed_min = min((row["temp_f"] for row in obs), default=None)
    latest = obs[-1] if obs else None
    latest_dewpoint = latest["dewpoint_f"] if latest else None
    return {"station": station, "date": str(day), "at": iso(decision),
            "hour": (decision - start).total_seconds() / 3600, "forecast": forecast,
            "observed_min_f": observed_min, "latest_observation": latest,
            "dewpoint_f": latest_dewpoint, "observation_delay_minutes": delay_minutes}


def _days(start, end):
    return (start + timedelta(days=index) for index in range((end - start).days + 1))


def _source_entry(data, path):
    return {"path": str(path.relative_to(data)), "sha256": _digest(path)}


def make_inputs(data):
    first, end = WINDOWS["fit"][0], FOLLOW_UP
    day_before, asos_end = first - timedelta(days=1), end + timedelta(days=3)
    evidence = {}
    for station in STATIONS:
        labels = expansion_weather.daily_labels(data, station, first, end)
        observations = expansion_weather.observed_data(data, station, day_before, asos_end)
        by_day = defaultdict(list)
        offset = timedelta(hours=expansion_weather.STATIONS[station][3])
        for obs in observations:
            by_day[str((utc(obs["at"]) + offset).date())].append(obs)
        cards = {}
        gaps = []
        for issue_day in _days(day_before, end):
            for cycle in NBS_CYCLES:
                issue = datetime.combine(issue_day, time(cycle), timezone.utc)
                card = expansion_nbm.load_card(data, station, issue)
                if card is None:
                    gaps.append(iso(issue))
                else:
                    cards[issue] = card
        evidence[station] = {
            "days": {str(day): {"forecasts": day_forecasts(data, station, day, cards),
                                "observations": by_day.get(str(day), []), "label": labels.get(str(day))}
                     for day in _days(first, end)},
            "nbm_missing_issues": gaps,
            "nbm_cards": [_source_entry(data, expansion_nbm.card_path(data, station, issue)) for issue in cards],
            "official_reports": _source_entry(data, expansion_weather.cli_file(data, station, first.year)),
            "station_archive": _source_entry(data, expansion_weather.observation_file(data, station, day_before, asos_end)),
        }
    return {"version": VERSION, "evidence": evidence, "windows": _window_strings(),
            "follow_up_case_only": str(FOLLOW_UP), "decision_hours_lst": HOURS}


def _window_strings():
    return {name: [str(start), str(end)] for name, (start, end) in WINDOWS.items()}


def freeze_inputs(data, inputs):
    blob = _json_bytes(inputs)
    ident = hashlib.sha256(blob).hexdigest()
    path = data / "research" / "model-inputs" / f"{inputs['version']}-{ident}.json.gz"
    if not path.exists():
        _write(path, gzip.compress(blob, mtime=0))
    return path, ident


def run_sources(*modules):
    return {Path(module.__file__).name: _digest(Path(module.__file__)) for module in modules}


def write_record(data, record, details=None):
    payload = None if details is None else gzip.compress(_json_bytes(details), mtime=0)
    details_digest = None if payload is None else hashlib.sha256(payload).hexdigest()
    identity = hashlib.sha256(_json_bytes([record, details_digest])).hexdigest()[:16]
    ident = f"{record['version']}-{identity}"
    rooted = Path(data) / "research"
    path = rooted / "models" / f"{ident}.json"
    if not path.exists():
        value = {**record, "id": ident, "created_at": iso(datetime.now(timezone.utc))}
        if payload is not None:
            frozen = rooted / "model-details" / f"{ident}.json.gz"
            _write(frozen, payload)
            value["provenance"] = {**value["provenance"], "replay_details_path": str(frozen.relative_to(data)),
                                   "replay_details_sha256": details_digest}
        _write(path, _json_bytes(value))
    return path


def snapshots(inputs, *, observation_delay=10, splits=None):
    rows = {key: [] for key in (tuple(WINDOWS) if splits is None else splits)}
    missing = defaultdict(int)
    windows = {name: tuple(map(date.fromisoformat, dates)) for name, dates in inputs["windows"].items()}
    split_order = sorted(windows, key=lambda name: windows[name][0])
    for split in rows:
        start, end = windows[split]
        index = split_order.index(split)
        later_split = split_order[index + 1] if index < len(split_order) - 1 else None
        for station, evidence in inputs["evidence"].items():
            offset = expansion_weather.STATIONS[station][3]
            cutoff = (climate_window(windows[later_split][0], offset)[0] + timedelta(hours=min(HOURS))
                      if later_split else datetime.max.replace(tzinfo=timezone.utc))
            for day in _days(start, end):
                bundle = evidence["days"].get(str(day))
                label = bundle.get("label") if bundle else None
                if label is None or label["low_f"] is None or utc(label["published_at"]) >= cutoff:
                    missing[f"{split}: missing, partial, or late CLI"] += len(HOURS)
                    continue
                opens, _ = climate_window(day, offset)
                for hour in HOURS:
                    decision = opens + timedelta(hours=hour)
                    row = make_snapshot(station, day, decision, bundle["forecasts"], bundle["observations"],
                                        delay_minutes=observation_delay)
                    if row is None:
                        missing[f"{split}: no eligible archived NBS minimum"] += 1
                        continue
                    rows[split].append({**row, "actual_f": label["low_f"], "label": label})
    return rows, dict(missing)


def features(row, feature_set):
    f = row["forecast"]
    output = {"station": row["station"], "noaa_f": float(f["mean_f"]), "hour_lst": row["hour"],
              "hour_squared": row["hour"] ** 2, "noaa_lead_hours": float(f["lead_hours"])}
    if feature_set == "ridge_no_observation":
        return output
    observed = row["observed_min_f"]
    output.update({"has_observed_min": float(observed is not None),
                   "observed_min_minus_noaa_f": 0.0 if observed is None else observed - f["mean_f"],
                   "observation_age_hours": 0.0 if observed is None else
                   (utc(row["at"]) - utc(row["latest_observation"]["at"])).total_seconds() / 3600})
    if feature_set == "ridge_dewpoint":
        dew = row["dewpoint_f"]
        output.update({"has_dewpoint": float(dew is not None),
                       "dewpoint_minus_noaa_f": 0.0 if dew is None else dew - f["mean_f"]})
    return output


def fit(training, calibration, *, methods=METHODS, feature_fn=features):
    bias_group = defaultdict(list)
    for row in training:
        bias_group[row["station"], row["hour"]].append(row["actual_f"] - row["forecast"]["mean_f"])
    models = {}
    train_y = [row["actual_f"] - row["forecast"]["mean_f"] for row in training]
    for key in methods.keys() - {"noaa", "bias"}:
        estimator = (HistGradientBoostingRegressor(max_iter=100, learning_rate=0.05, max_leaf_nodes=12,
                                                   min_samples_leaf=45, l2_regularization=2, random_state=20261115,
                                                   early_stopping=False)
                     if key == "nonlinear" else Ridge(alpha=10.0))
        models[key] = make_pipeline(DictVectorizer(sparse=False), StandardScaler(), estimator)
        with threadpool_limits(limits=1):
            models[key].fit([feature_fn(row, key) for row in training], train_y)
    fit_result = {"bias": {key: statistics.mean(values) for key, values in bias_group.items()},
                  "models": models, "feature_fn": feature_fn, "methods": methods}
    widths = {}
    for key, means in forecast_means(fit_result, calibration).items():
        errors = defaultdict(list)
        for row, mean in zip(calibration, means, strict=True):
            error_sq = (row["actual_f"] - mean) ** 2
            errors[row["station"], row["hour"]].append(error_sq)
        widths[key] = {group: max(0.75, math.sqrt(statistics.mean(values))) for group, values in errors.items()}
    return {**fit_result, "widths": widths}


def forecast_means(fitted, rows):
    output = {key: [] for key in fitted["methods"]}
    for row in rows:
        mean = row["forecast"]["mean_f"]
        output["noaa"].append(mean)
        output["bias"].append(mean + fitted["bias"][row["station"], row["hour"]])
    if rows:
        with threadpool_limits(limits=1):
            for key, pipeline in fitted["models"].items():
                corrections = pipeline.predict([fitted["feature_fn"](row, key) for row in rows])
                output[key] = [row["forecast"]["mean_f"] + float(value) for row, value in zip(rows, corrections, strict=True)]
    return output


def predictions(fitted, rows, buckets_by_day=None):
    partitions = {} if buckets_by_day is None else dict(buckets_by_day)
    result = []
    by_method = forecast_means(fitted, rows)
    for index, row in enumerate(rows):
        group = row["station"], row["date"]
        partition = partitions.setdefault(group, _benchmark_buckets(round(row["forecast"]["mean_f"])))
        truth = [_outcome(bucket, row["actual_f"]) for bucket in partition]
        estimates = {}
        for method, means in by_method.items():
            mean = means[index]
            native_sd = row["forecast"]["sd_f"]
            scale = (native_sd if method == "noaa" and native_sd is not None and native_sd > 0
                     else fitted["widths"][method][row["station"], row["hour"]])
            probabilities = [_probability(_limits(bucket), [mean], scale) for bucket in partition]
            estimates[method] = {"mean_f": mean, "scale_f": scale, "probabilities": probabilities,
                                 "error_f": mean - row["actual_f"],
                                 "brier": statistics.mean((p - y) ** 2 for p, y in zip(probabilities, truth, strict=True))}
        result.append({**row, "buckets": partition, "truth": truth, "estimates": estimates})
    return result


def _independent_counts(results):
    return {"calendar_dates": len({row["date"] for row in results}),
            "station_days": len({(row["station"], row["date"]) for row in results}), "snapshots": len(results)}


def metrics(results, group="all", methods=METHODS):
    output = []
    for name, label in methods.items():
        errors = [row["estimates"][name]["error_f"] for row in results]
        if errors:
            output.append({"method": name, "label": label, "group": group, **_independent_counts(results),
                            "mae_f": statistics.mean(map(abs, errors)),
                            "rmse_f": math.sqrt(statistics.mean(x ** 2 for x in errors)),
                            "bucket_brier": statistics.mean(row["estimates"][name]["brier"] for row in results)})
    return output


def date_bootstrap(results, reference, candidate, metric):
    grouped = defaultdict(list)
    for row in results:
        a, b = row["estimates"][reference], row["estimates"][candidate]
        diff = (b["brier"] - a["brier"] if metric == "brier" else abs(b["error_f"]) - abs(a["error_f"]))
        grouped[row["date"]].append(diff)
    dates = np.array([statistics.mean(values) for values in grouped.values()])
    sample = np.random.default_rng(20261115).choice(dates, size=(2500, len(dates)))
    bounds = np.quantile(sample.mean(axis=1), [0.025, 0.975])
    return {"method": candidate, "baseline": reference, "metric": metric, "calendar_dates": len(dates),
            "delta": float(dates.mean()), "date_bootstrap_interval_95": [float(x) for x in bounds]}


def calibration_bins(results, method):
    bins = [(0.5, 0.7), (0.7, 0.85), (0.85, 0.95), (0.95, 0.99), (0.99, math.inf)]
    group = defaultdict(list)
    for row in results:
        probabilities = row["estimates"][method]["probabilities"]
        for p, obs in zip(probabilities, row["truth"], strict=True):
            for lower, upper in bins:
                for side, prob, outcome in (("YES", p, obs), ("NO", 1 - p, 1 - obs)):
                    if lower <= prob < upper:
                        group[side, lower].append((prob, outcome, row["station"], row["date"]))
    output = []
    for (side, lower), entries in sorted(group.items()):
        output.append({"method": method, "side": side, "lower": lower, "bucket_calls": len(entries),
                       "station_days": len({(station, day) for _, _, station, day in entries}),
                       "calendar_dates": len({day for _, _, _, day in entries}),
                       "mean_probability": statistics.mean(entry[0] for entry in entries),
                       "realized_rate": statistics.mean(entry[1] for entry in entries)})
    return output


def weather_cases(results, anchor="noaa"):
    out = []
    seen = set()
    for row in sorted(results, key=lambda item: abs(item["estimates"][anchor]["error_f"]), reverse=True):
        key = row["station"], row["date"]
        if key in seen:
            continue
        seen.add(key)
        ref = row["estimates"][anchor]["mean_f"]
        chosen = row["estimates"]["ridge"]["mean_f"]
        out.append({"station": key[0], "date": key[1], "title": f"{key[0]} {key[1]} low forecast miss",
                    "at": row["at"], "detail": f"Official low {row['actual_f']:.0f}°F. NOAA {ref:.0f}°F. Ridge {chosen:.1f}°F.",
                    "forecast": row["forecast"], "official_report": row["label"]})
        if len(out) == 4:
            break
    return out


def study(data, inputs_path=None):
    data = Path(data)
    if inputs_path:
        inputs = json.loads(gzip.decompress(inputs_path.read_bytes()))
    else:
        inputs = make_inputs(data)
    frozen, input_digest = freeze_inputs(data, inputs)
    rows, missing = snapshots(inputs)
    fitted = fit(rows["fit"], rows["calibration"])
    evaluated = {group: predictions(fitted, rows[group]) for group in ("selection", "test")}
    test = evaluated["test"]
    metric_rows = metrics(test)
    for station in STATIONS:
        metric_rows.extend(metrics([row for row in test if row["station"] == station], station))
    for clock in HOURS:
        group = f"{(clock % 24):02}:00 LST" if clock >= 0 else "prior evening 21:00 LST"
        metric_rows.extend(metrics([row for row in test if row["hour"] == clock], group))
    confidence = [date_bootstrap(test, anchor, key, "brier")
                  for anchor, key in (("noaa", "bias"), ("noaa", "ridge"), ("ridge", "ridge_dewpoint"),
                                      ("ridge", "nonlinear"), ("noaa", "nonlinear"), ("ridge_no_observation", "ridge"))]
    lag_results = {}
    for lag in (0, 30):
        subset, _ = snapshots(inputs, observation_delay=lag, splits=("test",))
        lag_results[lag] = metrics(predictions(fitted, subset["test"]), f"{lag} min assumed lag")
    record = {
        "version": VERSION, "title": "Daily minimum forecast and weather evidence",
        "hypothesis": "An exact-station as-of observed minimum improves forecasts of the whole climate-day low over raw NOAA 18-hour TXN guidance.",
        "target": "Daily official minimum temperature (°F), Central Park and Chicago Midway",
        "venues": ["polymarket-us"], "date_window": inputs["windows"], "metrics": metric_rows,
        "cases": weather_cases(test), "limits": [
            "NBM TXN minimum covers 00Z–18Z, while the target uses the exact 24-hour station climate day. Late-day cold fronts can cause misses.",
            "Bulk ASOS observations use assumed 10-minute availability. The lag sensitivity keeps fitted models fixed and tests 0 and 30 minutes.",
            "Bulk NWS CLI labels use the last report in the frozen IEM summary. Individual market cases additionally verify the raw report and exchange payout.",
            "The benchmark buckets use a common artificial partition per station-day. Actual contract buckets are evaluated in the separate replay.",
            "At 21:00 LST the last current-day NBS minimum is usually older than the fixed 24-hour limit. Those planned decisions have no eligible TXN.",
            "Calendar dates are resampled together across stations and clocks. Near-99% YES calls are scarce and repeated NO buckets are correlated.",
        ],
        "provenance": {"input_path": str(frozen.relative_to(data)), "input_sha256": input_digest,
                       "versions": {"scikit-learn": sklearn_version, "numpy": np.__version__},
                       "source_sha256": run_sources(sys.modules[__name__], expansion_nbm, expansion_weather),
                       "settings": {"ridge_alpha": 10, "nonlinear_max_leaf_nodes": 12, "nonlinear_max_iter": 100,
                                    "probability": "Gaussian TXN with XND or held-out station-hour RMSE for learned candidates",
                                    "calibration_spread_floor_f": 0.75, "bootstrap_seed": 20261115}},
        "coverage": {split: _independent_counts(group) for split, group in rows.items()}, "missing": missing,
        "archive_gaps": {station: evidence["nbm_missing_issues"] for station, evidence in inputs["evidence"].items()},
        "selection_metrics": metrics(evaluated["selection"], "selection dates"), "contrasts": confidence,
        "calibration": [entry for method in ("noaa", "ridge", "ridge_dewpoint") for entry in calibration_bins(test, method)],
        "observation_delay_sensitivity": lag_results,
    }
    path = write_record(data, record, evaluated)
    return path, fitted, inputs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path(".data"))
    parser.add_argument("--from-input", type=Path)
    args = parser.parse_args()
    path, _, _ = study(args.data_dir, args.from_input)
    print(path)


if __name__ == "__main__":
    main()
