"""Test late cooling with as-issued remaining-day forecasts and station observations."""

from __future__ import annotations

import argparse
import gzip
import json
import math
import statistics
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import numpy as np
from sklearn import __version__ as sklearn_version
from sklearn.feature_extraction import DictVectorizer
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from . import expansion_nbm, expansion_weather, research_model
from .model import _benchmark_buckets, _limits, _outcome, _probability
from .nbm import climate_window, iso, utc

VERSION = "full-climate-day-low-v1"
WINDOWS = {
    "fit": (date(2025, 9, 1), date(2025, 10, 26)),
    "calibration": (date(2025, 10, 28), date(2025, 11, 9)),
    "selection": (date(2025, 11, 11), date(2025, 11, 21)),
    "test": (date(2025, 11, 23), date(2025, 12, 15)),
}
METHODS = {
    "txn": "Carried last NOAA TXN minimum",
    "remaining": "Minimum of remaining NBS guidance",
    "combined": "Observed minimum + remaining NBS",
    "ridge": "Ridge correction to combined minimum",
}


def _interp(points, at):
    for (before, value), (after, following) in zip(points, points[1:]):
        if before <= at <= after and after - before <= timedelta(hours=3):
            return value + ((at - before) / (after - before)) * (following - value)
    return next((value for valid, value in points if valid == at), None)


def remaining_minimum(card, left, right):
    rows = [(utc(point["valid_at"]), point["TMP"]) for point in card["points"] if "TMP" in point]
    first = _interp(rows, left)
    last = _interp(rows, right)
    if first is None or last is None:
        return None
    knots = [(left, first), *[(at, value) for at, value in rows if left < at < right], (right, last)]
    if any(after - before > timedelta(hours=3) for (before, _), (after, _) in zip(knots, knots[1:])):
        return None
    return min(value for _, value in knots)


def _at(station, day, decision, card_data, observations, target, *, delay_minutes=10):
    start, end = climate_window(day, expansion_weather.STATIONS[station][3])
    cards = [row for row in card_data if utc(row["issued_at"]) <= utc(row["available_at"]) <= decision
             and decision - utc(row["issued_at"]) <= timedelta(hours=36)]
    candidates = sorted(cards, key=lambda row: row["issued_at"], reverse=True)
    remaining = None
    remaining_card = None
    for card in candidates:
        remaining = remaining_minimum(card, max(start, decision), end)
        if remaining is not None:
            remaining_card = card
            break
    old_txn = next((forecast for card in candidates if (forecast := research_model._forecast(
                    card, day, expansion_weather.STATIONS[station][3]))), None)
    if remaining_card is None or old_txn is None:
        return None
    available = expansion_weather.observations_as_of(observations, start, decision, delay_minutes=delay_minutes)
    past = min((point["temp_f"] for point in available), default=None)
    latest = available[-1] if available else None
    mixed = min(remaining, past) if past is not None else remaining
    return {
        "station": station, "date": str(day), "at": iso(decision), "hour": (decision - start).total_seconds() / 3600,
        "official_report": target, "actual_f": target["low_f"], "observed_min_f": past, "latest_observation": latest,
        "txn_age_hours": (decision - utc(old_txn["issued_at"])).total_seconds() / 3600,
        "forecast": {"mean_f": old_txn["mean_f"], "issued_at": remaining_card["issued_at"],
                     "available_at": remaining_card["available_at"], "source": remaining_card["source"],
                     "card_sha256": remaining_card["card_sha256"],
                     "covered_from": iso(max(start, decision)), "covered_to": iso(end)},
        "means": {"txn": old_txn["mean_f"], "remaining": remaining, "combined": mixed},
    }


def _station_inputs(data, station, first, last):
    labels = expansion_weather.daily_labels(data, station, first, last)
    obs_start = first - timedelta(days=1)
    obs_end = last + timedelta(days=3)
    obs = expansion_weather.observed_data(data, station, obs_start, obs_end)
    by_day = defaultdict(list)
    offset = timedelta(hours=expansion_weather.STATIONS[station][3])
    for row in obs:
        by_day[(utc(row["at"]) + offset).date()].append(row)
    cards = defaultdict(list)
    card_names = []
    for day in research_model._days(obs_start, obs_end):
        for hour in research_model.NBS_CYCLES:
            issue = datetime.combine(day, time(hour), timezone.utc)
            if card := expansion_nbm.load_card(data, station, issue):
                cards[day].append(card)
                card_names.append(expansion_nbm.card_path(data, station, issue))
    return labels, by_day, cards, [
        *[research_model._source_entry(data, name) for name in card_names],
        research_model._source_entry(data, expansion_weather.cli_file(data, station, first.year)),
        research_model._source_entry(data, expansion_weather.observation_file(data, station, obs_start, obs_end)),
    ]


def make_inputs(data):
    source = {}
    groups = {split: [] for split in WINDOWS}
    issues = []
    first, last = WINDOWS["fit"][0], WINDOWS["test"][1]
    with ThreadPoolExecutor(max_workers=2) as pool:
        tasks = {station: pool.submit(_station_inputs, data, station, first, last) for station in research_model.STATIONS}
        for station, task in tasks.items():
            labels, observations, cards, files = task.result()
            source[station] = files
            offset = expansion_weather.STATIONS[station][3]
            names = tuple(WINDOWS)
            for index, (split, (begin, end)) in enumerate(WINDOWS.items()):
                cutoff = (climate_window(WINDOWS[names[index + 1]][0], offset)[0] + timedelta(hours=min(research_model.HOURS))
                          if index < len(names) - 1 else datetime.max.replace(tzinfo=timezone.utc))
                for day in research_model._days(begin, end):
                    label = labels.get(str(day))
                    if not label or label["low_f"] is None or utc(label["published_at"]) >= cutoff:
                        issues.append({"station": station, "date": str(day), "split": split,
                                       "reason": "No documented complete CLI minimum ahead of following stage"})
                        continue
                    day_start, _ = climate_window(day, offset)
                    eligible = [card for delta in (-1, 0, 1) for card in cards[day + timedelta(days=delta)]]
                    for hour in research_model.HOURS:
                        snapshot = _at(station, day, day_start + timedelta(hours=hour), eligible, observations[day], label)
                        if snapshot:
                            groups[split].append(snapshot)
                        else:
                            issues.append({"station": station, "date": str(day), "split": split, "hour_lst": hour,
                                           "reason": "No full remaining NBS profile or 36-hour carried TXN"})
    return {"version": VERSION, "windows": {key: [str(start), str(end)] for key, (start, end) in WINDOWS.items()},
            "rows": groups, "source_files": source, "missing": issues}


def _features(row):
    estimated = row["means"]["combined"]
    observation = row["latest_observation"]
    return {
        "station": row["station"], "hour_lst": row["hour"], "hour_squared": row["hour"] ** 2,
        "remaining_f": row["means"]["remaining"], "physical_f": estimated,
        "has_latest_temp": float(observation is not None),
        "latest_minus_remaining_f": 0.0 if observation is None else observation["temp_f"] - row["means"]["remaining"],
        "old_txn_minus_physical_f": row["means"]["txn"] - estimated,
    }


def _model(rows):
    fit = make_pipeline(DictVectorizer(sparse=False), StandardScaler(), Ridge(alpha=10.0))
    fit.fit([_features(row) for row in rows], [row["actual_f"] - row["means"]["combined"] for row in rows])
    return fit


def _means(rows, fitted):
    values = fitted.predict([_features(row) for row in rows])
    return [{**row["means"], "ridge": row["means"]["combined"] + float(value)}
            for row, value in zip(rows, values, strict=True)]


def _spread(rows, fitted):
    errors = defaultdict(list)
    for row, means in zip(rows, _means(rows, fitted), strict=True):
        for key, mean in means.items():
            errors[key, row["station"], row["hour"]].append((mean - row["actual_f"]) ** 2)
    return {group: max(0.75, math.sqrt(statistics.mean(values))) for group, values in errors.items()}


def _evaluate(rows, fitted, widths):
    results = []
    partitions = {}
    for row, means in zip(rows, _means(rows, fitted), strict=True):
        key = row["station"], row["date"]
        buckets = partitions.setdefault(key, _benchmark_buckets(round(means["txn"])))
        truth = [_outcome(bucket, row["actual_f"]) for bucket in buckets]
        scores = {}
        for method, mean in means.items():
            spread = widths[method, row["station"], row["hour"]]
            odds = [_probability(_limits(bucket), [mean], spread) for bucket in buckets]
            scores[method] = {"mean_f": mean, "scale_f": spread, "error_f": mean - row["actual_f"],
                              "brier": statistics.mean((p - y) ** 2 for p, y in zip(odds, truth, strict=True)),
                              "probabilities": odds}
        results.append({**row, "estimates": scores, "buckets": buckets, "truth": truth})
    return results


def _cases(rows):
    ranked = sorted(rows, key=lambda row: abs(row["estimates"]["txn"]["error_f"]), reverse=True)
    seen = set()
    cases = []
    for row in ranked:
        key = row["station"], row["date"]
        if key in seen:
            continue
        seen.add(key)
        cases.append({"station": key[0], "date": key[1], "at": row["at"],
                      "title": f"{key[0]} {key[1]} · Late cooling",
                      "detail": (f"Carried NOAA TXN {row['means']['txn']:.0f}°F at {row['txn_age_hours']:.0f}h age. "
                                 f"Observed and remaining-period forecast {row['means']['combined']:.1f}°F. "
                                 f"Official minimum {row['actual_f']:.0f}°F."),
                      "forecast": row["forecast"], "official_report": row["official_report"],
                      "observed_min_f": row["observed_min_f"], "latest_observation": row["latest_observation"]})
        if len(cases) == 5:
            break
    return cases


def _later_inputs(data, start, end):
    later = []
    gaps = []
    sources = {}
    for station in research_model.STATIONS:
        labels, observations, cards, files = _station_inputs(data, station, start, end)
        sources[station] = files
        offset = expansion_weather.STATIONS[station][3]
        for day in research_model._days(start, end):
            label = labels.get(str(day))
            official = expansion_weather.raw_report(data, station, day, label["product"]) if label else None
            if not official or official["low_f"] is None:
                gaps.append({"station": station, "date": str(day), "split": "later confirmatory",
                             "reason": "No complete raw CLI minimum"})
                continue
            opens, _ = climate_window(day, offset)
            eligible = [card for delta in (-1, 0, 1) for card in cards[day + timedelta(days=delta)]]
            for hour in research_model.HOURS:
                row = _at(station, day, opens + timedelta(hours=hour), eligible, observations[day], official)
                if row:
                    later.append(row)
                else:
                    gaps.append({"station": station, "date": str(day), "split": "later confirmatory",
                                 "hour_lst": hour, "reason": "Missing remaining NBS profile or carried TXN"})
    return later, sources, gaps


def study(data, input_path=None, later=None):
    data = Path(data)
    inputs = json.loads(gzip.decompress(input_path.read_bytes())) if input_path else make_inputs(data)
    if later:
        if later[0] <= date.fromisoformat(inputs["windows"]["test"][1]) or later[1] < later[0]:
            raise ValueError("The independent later dates must follow the original test block")
        rows, sources, gaps = _later_inputs(data, *later)
        inputs["rows"]["later"] = rows
        inputs["source_files"]["later"] = sources
        inputs["windows"]["later"] = list(map(str, later))
        inputs["missing"].extend(gaps)
    frozen, digest = research_model.freeze_inputs(data, inputs)
    by_stage = inputs["rows"]
    estimator = _model(by_stage["fit"])
    widths = _spread(by_stage["calibration"], estimator)
    selection, test = [_evaluate(by_stage[split], estimator, widths) for split in ("selection", "test")]
    late = [row for row in test if row["hour"] == 21]
    metrics = research_model.metrics(test, "all matched decisions", METHODS)
    metrics.extend(research_model.metrics(late, "21:00 LST", METHODS))
    for station in research_model.STATIONS:
        metrics.extend(research_model.metrics([row for row in test if row["station"] == station], station, METHODS))
    comparisons = [research_model.date_bootstrap(rows, "txn", method, "brier") |
                   {"group": label} for label, rows in (("all", test), ("21:00 LST", late))
                   for method in ("remaining", "combined", "ridge") if rows]
    later_test = _evaluate(by_stage["later"], estimator, widths) if by_stage.get("later") else []
    later_short = [row for row in later_test if row["hour"] == 21]
    metrics.extend(research_model.metrics(later_test, "2026 later · all matched decisions", METHODS))
    metrics.extend(research_model.metrics(later_short, "2026 later · 21:00 LST", METHODS))
    if later_short:
        comparisons.append(research_model.date_bootstrap(later_short, "txn", "combined", "brier") |
                           {"group": "2026 later · 21:00 LST"})
    selected = research_model.metrics(selection, "pretest selection", METHODS)
    case_examples = [*_cases(late or test)[:3], *_cases(later_short)[:3]]
    record = {
        "version": VERSION, "title": "Daily lows after the morning forecast ages",
        "hypothesis": "At late climate-day decisions a model of observed minimum and remaining weather outperforms an aged 18-hour TXN forecast.",
        "target": "Whole-day official minimum with late cooling through climate close",
        "venues": [], "date_window": inputs["windows"], "metrics": metrics, "cases": case_examples,
        "limits": [
            "This 2025 archival holdout was fixed for the late-window experiment after seeing the gap in the separate 2026 daily-low study. It tests a different set of dates and earlier NBM vintages.",
            "The remaining forecast requires contiguous three-hour NBS guidance through the fixed local-standard climate close. Temperature between valid steps uses linear interpolation.",
            "Routine archive ASOS observations use the stated ten-minute availability proxy and can differ from the whole-degree NWS CLI label.",
            "The carried TXN comparator intentionally keeps the last eligible 18-hour forecast to 36 hours to quantify late-day drift. The previous experiment stopped at 24 hours.",
            "Source forecast errors are likely correlated across stations. Confidence intervals resample calendar dates rather than treating all rows as independent.",
            "The optional 2026 replay uses the unchanged 2025 fit and calibration, with matching raw complete CLI temperatures.",
        ],
        "provenance": {"input_path": str(frozen.relative_to(data)), "input_sha256": digest,
                       "source_sha256": research_model.run_sources(sys.modules[__name__], expansion_nbm, expansion_weather),
                       "versions": {"scikit-learn": sklearn_version, "numpy": np.__version__},
                       "settings": {"ridge_alpha": 10.0, "clock_lookback_h": 36, "asos_delay_min": 10,
                                    "spread_floor_f": 0.75}},
        "coverage": {split: research_model._independent_counts(rows) for split, rows in by_stage.items()},
        "selection_metrics": selected, "contrasts": comparisons, "missing": inputs["missing"],
    }
    return research_model.write_record(data, record, {"test": test, "selection": selection, "later_test": later_test})


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--data-dir", type=Path, default=Path(".data"))
    cli.add_argument("--from-input", type=Path)
    cli.add_argument("--later", nargs=2, type=date.fromisoformat, metavar=("START", "END"))
    args = cli.parse_args()
    print(study(args.data_dir, args.from_input, args.later))


if __name__ == "__main__":
    main()
