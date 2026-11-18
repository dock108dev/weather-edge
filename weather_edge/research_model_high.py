"""Compare daily-high forecasts across six exact stations and test new weather fields."""

from __future__ import annotations

import argparse
import gzip
import json
import math
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from sklearn import __version__ as sklearn_version

from . import expansion_nbm, expansion_replay, expansion_weather, research_model
from .nbm import climate_window, iso, utc

VERSION = "expanded-daily-high-v1"
STATIONS = ("KNYC", "KMDW", "KPHL", "KDEN", "KBOS", "KORD")
METHODS = {
    "noaa": "NOAA TXN + XND",
    "bias": "Station and time bias",
    "ridge": "Ridge with ASOS temperature",
    "ridge_sky": "Ridge + forecast sky near peak",
    "ridge_dewpoint": "Ridge + forecast dew point depression",
    "ridge_wind": "Ridge + forecast wind at peak",
    "nonlinear": "Histogram gradient boosting",
}


def _interp(points, at):
    for (left_at, left_f), (right_at, right_f) in zip(points, points[1:]):
        if left_at <= at <= right_at and right_at - left_at == timedelta(hours=3):
            fraction = (at - left_at) / (right_at - left_at)
            return left_f + fraction * (right_f - left_f)
    return next((number for valid, number in points if valid == at), None)


def _snapshot(station, day, clock, forecasts, obs, label, *, observation_delay=10):
    offset = expansion_weather.STATIONS[station][3]
    start, end = climate_window(day, offset)
    at = start + timedelta(hours=clock)
    candidates = [forecast for forecast in forecasts if utc(forecast["issued_at"]) <= utc(forecast["available_at"]) <= at
                  and at - utc(forecast["issued_at"]) <= timedelta(hours=36)]
    if not candidates:
        return None
    forecast = max(candidates, key=lambda f: f["issued_at"])
    available = expansion_weather.observations_as_of(obs, start, at, delay_minutes=observation_delay)
    best = max((point["temp_f"] for point in available), default=None)
    newest = available[-1] if available else None
    hourly = forecast["hourly"]
    peak = max(hourly, key=lambda point: point["temp_f"], default=None)
    points = [(utc(row["valid_at"]), row["temp_f"]) for row in hourly]
    prior_model = _interp(points, utc(newest["at"])) if newest else None
    surprise = latest_obs_age = None
    if newest:
        latest_obs_age = (at - utc(newest["at"])).total_seconds() / 3600
        if prior_model is not None:
            surprise = newest["temp_f"] - prior_model
    return {
        "station": station, "date": str(day), "at": iso(at), "hour": clock, "actual_f": label["high_f"],
        "official_report": label, "observed_max_f": best, "latest_observation": newest, "latest_observation_age_h": latest_obs_age,
        "latest_observed_minus_nbs_f": surprise, "forecast_peak": peak,
        "forecast": {**forecast, "mean_f": forecast["high_f"], "sd_f": forecast["high_sd_f"],
                     "lead_hours": (utc(forecast["valid_at"]) - utc(forecast["issued_at"])).total_seconds() / 3600},
        "climate_starts_at": iso(start), "climate_ends_at": iso(end),
    }


def _features(row, method):
    forecast = row["forecast"]
    mean = forecast["mean_f"]
    observation = row["observed_max_f"]
    surprise = row["latest_observed_minus_nbs_f"]
    features = {
        "station": row["station"], "noaa_f": float(mean), "lead_h": forecast["lead_hours"], "hour_lst": row["hour"],
        "hour_squared": row["hour"] ** 2, "has_day_observation": float(observation is not None),
        "observed_high_minus_noaa_f": 0.0 if observation is None else observation - mean,
        "latest_observation_age_h": row["latest_observation_age_h"] or 0.0,
        "has_observation_departure": float(surprise is not None), "observation_departure_f": surprise or 0.0,
    }
    peak = row["forecast_peak"] or {}
    if method == "ridge_sky":
        sky = peak.get("sky_pct")
        features.update(peak_sky_pct=sky or 0, has_peak_sky=float(sky is not None))
    elif method == "ridge_dewpoint":
        dew = peak.get("dewpoint_f")
        features.update(peak_dewpoint_depression_f=(peak["temp_f"] - dew if dew is not None else 0),
                        has_peak_dewpoint=float(dew is not None))
    elif method == "ridge_wind":
        heading, speed = peak.get("wind_direction_tens_deg"), peak.get("wind_knots")
        has_wind = heading is not None and speed is not None and 0 <= heading <= 36 and speed >= 0
        radians = math.radians(heading * 10) if has_wind else 0.0
        features.update(wind_east_component=-speed * math.sin(radians) if has_wind else 0.0,
                        wind_north_component=-speed * math.cos(radians) if has_wind else 0.0,
                        has_peak_wind=float(has_wind))
    return features


def make_inputs(data):
    windows = research_model.WINDOWS
    first, last = windows["fit"][0], research_model.FOLLOW_UP
    rows = {name: [] for name in windows}
    sources = {}
    gaps = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        tasks = {station: pool.submit(expansion_replay._station_data, data, station, first, last) for station in STATIONS}
        for station, task in tasks.items():
            labels, observations, cards = task.result()
            offset = expansion_weather.STATIONS[station][3]
            sources[station] = {
                "nbm_cards": [research_model._source_entry(data, expansion_nbm.card_path(data, station, issue)) for issue in cards],
                "official_reports": research_model._source_entry(data, expansion_weather.cli_file(data, station, first.year)),
                "station_archive": research_model._source_entry(
                    data, expansion_weather.observation_file(data, station, first - timedelta(days=1), last + timedelta(days=3))),
            }
            names = tuple(windows)
            for index, (split, (start, end)) in enumerate(windows.items()):
                cutoff = (climate_window(windows[names[index + 1]][0], offset)[0] + timedelta(hours=min(research_model.HOURS))
                          if index < len(names) - 1 else datetime.max.replace(tzinfo=timezone.utc))
                for day in research_model._days(start, end):
                    label = labels.get(str(day))
                    if not label or label["high_f"] is None or utc(label["published_at"]) >= cutoff:
                        gaps.append({"station": station, "date": str(day), "split": split, "issue": "No eligible complete CLI"})
                        continue
                    opens, closes = climate_window(day, offset)
                    forecasts = []
                    for delta in (-1, 0, 1):
                        for hour in research_model.NBS_CYCLES:
                            issue = (opens + timedelta(days=delta)).replace(hour=hour)
                            card = cards.get(issue)
                            if card and (value := expansion_replay._high_forecast(card, opens, closes)):
                                forecasts.append(value)
                    for hour in research_model.HOURS:
                        row = _snapshot(station, day, hour, forecasts, observations[day], label)
                        if row:
                            rows[split].append(row)
                        else:
                            gaps.append({"station": station, "date": str(day), "split": split, "hour_lst": hour,
                                         "issue": "No eligible as-issued TXN maximum"})
    return {"version": VERSION, "stations": STATIONS, "windows": research_model._window_strings(),
            "rows": rows, "sources": sources, "gaps": gaps}


def _report_cases(results):
    rows = []
    seen = set()
    for item in sorted(results, key=lambda row: abs(row["estimates"]["noaa"]["error_f"]), reverse=True):
        station, day = item["station"], item["date"]
        if (station, day) in seen:
            continue
        seen.add((station, day))
        predictions = item["estimates"]
        result = {"station": station, "date": day, "at": item["at"],
                  "title": f"{station} {day} · Daily-high error",
                  "detail": (f"Official high {item['actual_f']:.0f}°F. NOAA {predictions['noaa']['mean_f']:.0f}°F. "
                             f"Ridge {predictions['ridge']['mean_f']:.1f}°F. "
                             f"Nonlinear {predictions['nonlinear']['mean_f']:.1f}°F."),
                  "official_report": item["official_report"], "forecast": item["forecast"], "observations": item["latest_observation"]}
        rows.append(result)
        if len(rows) == 6:
            break
    return rows


def study(data, input_path=None):
    data = Path(data)
    inputs = json.loads(gzip.decompress(input_path.read_bytes())) if input_path else make_inputs(data)
    frozen, sha = research_model.freeze_inputs(data, inputs)
    rows = inputs["rows"]
    fitted = research_model.fit(rows["fit"], rows["calibration"], methods=METHODS, feature_fn=_features)
    selection, test = (research_model.predictions(fitted, rows[name]) for name in ("selection", "test"))
    metrics = research_model.metrics(test, "all", METHODS)
    for station in STATIONS:
        metrics.extend(research_model.metrics([row for row in test if row["station"] == station], station, METHODS))
    for hour in research_model.HOURS:
        metrics.extend(research_model.metrics([row for row in test if row["hour"] == hour],
                                               "prior evening" if hour < 0 else f"{hour:02d}:00 LST", METHODS))
    comparisons = [research_model.date_bootstrap(test, base, method, "brier")
                   for base, method in (("noaa", "bias"), ("noaa", "ridge"), ("noaa", "nonlinear"),
                                        ("ridge", "nonlinear"), ("ridge", "ridge_sky"), ("ridge", "ridge_dewpoint"),
                                        ("ridge", "ridge_wind"))]
    legacy = data / "study" / "v1" / "report.json"
    record = {
        "version": VERSION, "title": "Daily highs across six stations",
        "hypothesis": "Incremental weather fields or a nonlinear correction reduce holdout bucket error over a same-case raw NOAA and Ridge baseline.",
        "target": "NWS daily maximum at Central Park, Midway, Philadelphia, Denver, Boston, and O’Hare",
        "venues": [], "date_window": inputs["windows"], "metrics": metrics, "cases": _report_cases(test),
        "limits": [
            "Each method sees the same NBS issue, station-day and decision clock. NOAA 18-hour TXN predicts the label for a full climate day.",
            "Earlier site corrections use a ten-minute archive ASOS availability assumption and a frozen NWS CLI summary. Research samples preserve the source timestamps.",
            "Dew point depression, cloud cover and wind components measured near the forecast peak are separate additions to Ridge.",
            "Synthetic common buckets compare model probabilities. They are independent of listed strike definitions. The legacy NOAA, Ridge and cloud study uses different stations and NBH observations.",
            "The common later dates are a temporal holdout for these stations and features. Some calendar dates overlap already-read legacy study dates.",
        ],
        "provenance": {"input_path": str(frozen.relative_to(data)), "input_sha256": sha,
                       "legacy_benchmark": research_model._source_entry(data, legacy) if legacy.exists() else None,
                       "versions": {"scikit-learn": sklearn_version, "numpy": np.__version__},
                       "settings": {"ridge_alpha": 10.0, "obs_availability_lag_min": 10,
                                    "point_interval_h": 3, "calendar_block_seed": 20261115},
                       "source_sha256": research_model.run_sources(sys.modules[__name__], research_model, expansion_replay)},
        "coverage": {stage: research_model._independent_counts(snapshots) for stage, snapshots in rows.items()},
        "selection_metrics": research_model.metrics(selection, "selection dates", METHODS),
        "contrasts": comparisons, "missing": inputs["gaps"],
        "calibration": [item for method in ("noaa", "ridge", "nonlinear")
                        for item in research_model.calibration_bins(test, method)],
    }
    return research_model.write_record(data, record, {"test": test, "selection": selection})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path(".data"))
    parser.add_argument("--from-input", type=Path)
    args = parser.parse_args()
    print(study(args.data_dir, args.from_input))


if __name__ == "__main__":
    main()
