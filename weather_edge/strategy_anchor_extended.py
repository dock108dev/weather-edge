"""Test learned weather updates against Kalshi midpoint odds on a larger month-scale split."""

import argparse
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
import gzip
from hashlib import sha256
import json
import math
from pathlib import Path
import statistics

from sklearn import __version__ as sklearn_version
from sklearn.linear_model import LogisticRegression

from . import study, venue_archive
from .market import iso
from .strategy_kalshi_history import KALSHI, case_for, scenarios
from .strategy_market import BLEND_GRID, score
from .strategy_runs import DEFAULT_DATA, digest, json_bytes, write_new


ENGINE = "kalshi-month-scale-market-update-v1"
SPLITS = {
    "fit": ("2026-10-01", "2026-10-25"),
    "validation": ("2026-10-28", "2026-11-03"),
    "later test": ("2026-11-06", "2026-11-13"),
}
LATER_MINIMUM_PROBABILITY = 0.001
REGULARIZATION = (0.1, 1.0, 10.0)


def calendar_dates(bounds):
    start, end = map(date.fromisoformat, bounds)
    return [str(start + timedelta(days=i)) for i in range((end - start).days + 1)]


def read_panel(data, weather):
    markets = defaultdict(lambda: defaultdict(list))
    for row in venue_archive.export(data, venue=KALSHI, family="daily-high", status="settled"):
        station, day = row["station"], row["day"]
        if (station in weather["stations"] and day and SPLITS["fit"][0] <= day <= SPLITS["later test"][1]
                and row["closed_at"]):
            markets[station, day][row["event_id"]].append(row)
    panels, gaps = {}, {}
    for name, bounds in SPLITS.items():
        following = list(SPLITS)[list(SPLITS).index(name) + 1:]
        cutoff = SPLITS[following[0]][0] + "T00:00:00Z" if following else None
        issues = Counter()
        panels[name] = []
        for station, weather_source in weather["stations"].items():
            for day in calendar_dates(bounds):
                match = markets.get((station, day), {})
                if len(match) != 1:
                    issues["exact station/day settled event absent or ambiguous"] += 1
                    continue
                label = weather_source["labels"].get(day, {})
                event = {"station": station, "date": day, "standard_utc_offset_hours": study.STATIONS[station],
                         "observations": [], "forecasts": weather_source["forecasts"].get(day, []),
                         "settlement": label}
                group, = match.values()
                panels[name].extend(scenarios(data, event, group, None, name, issues, label_cutoff=cutoff))
        gaps[name] = dict(issues)
    return panels, gaps


def clipped_logit(value):
    p = min(1 - LATER_MINIMUM_PROBABILITY, max(LATER_MINIMUM_PROBABILITY, value))
    return math.log(p / (1 - p))


def features(bucket, weather):
    x = [clipped_logit(bucket["market"])]
    if weather:
        x.append(clipped_logit(bucket["noaa"]))
    return x


def training_rows(records, weather):
    frames = Counter((row["date"], row["station"]) for row in records)
    inputs, outputs, weights = [], [], []
    for row in records:
        same_day = frames[row["date"], row["station"]]
        for bucket in row["buckets"]:
            inputs.append(features(bucket, weather))
            outputs.append(bucket["outcome"])
            weights.append(1 / (same_day * len(row["buckets"])))
    return inputs, outputs, weights


def evaluate(records, predict):
    day_losses = defaultdict(list)
    for row in records:
        expected = predict(row)
        error = statistics.mean((prediction - bucket["outcome"]) ** 2
                                for prediction, bucket in zip(expected, row["buckets"], strict=True))
        day_losses[row["date"], row["station"]].append(error)
    return statistics.mean(statistics.mean(values) for values in day_losses.values()) if day_losses else None


def stacked(model, include_weather, row):
    probabilities = model.predict_proba([features(bucket, include_weather) for bucket in row["buckets"]])[:, 1]
    return list(probabilities / probabilities.sum())


def choose_learner(panels, include_weather):
    x, y, weights = training_rows(panels["fit"], include_weather)
    options = []
    for value in REGULARIZATION:
        model = LogisticRegression(C=value, solver="lbfgs", max_iter=500)
        model.fit(x, y, sample_weight=weights)
        options.append((evaluate(panels["validation"], lambda row: stacked(model, include_weather, row)), value, model))
    _, selected_c, model = min(options, key=lambda option: option[0])
    return selected_c, model, [{"C": c, "validation_brier": error} for error, c, _ in options]


def calculate(panels, *, case_builder=case_for):
    if not all(panels.values()):
        raise ValueError("Fit, validation, and later test must all contain matched archived cases.")
    locked_weight = min(BLEND_GRID, key=lambda w: score(panels["validation"], w))
    calibration_c, calibration, calibration_grid = choose_learner(panels, False)
    weather_c, weather_model, weather_grid = choose_learner(panels, True)
    predictors = {
        "Market midpoint proxy": lambda row: [b["market"] for b in row["buckets"]],
        "Raw NOAA NBS spread": lambda row: [b["noaa"] for b in row["buckets"]],
        "Locked constant NOAA blend": lambda row: [(1 - locked_weight) * b["market"] + locked_weight * b["noaa"]
                                                   for b in row["buckets"]],
        "Market-only logistic calibration": lambda row: stacked(calibration, False, row),
        "Market and NOAA logistic update": lambda row: stacked(weather_model, True, row),
    }
    reports = []
    for name in SPLITS:
        records = panels[name]
        for method, predictor in predictors.items():
            reports.append({
                "window": name, "method": method, "brier": evaluate(records, predictor),
                "dates": len({r["date"] for r in records}),
                "station_days": len({(r["date"], r["station"]) for r in records}),
                "frames": len(records), "buckets": sum(len(r["buckets"]) for r in records),
            })
    by_station = {}
    held = panels["later test"]
    for station in sorted({row["station"] for row in held}):
        rows = [row for row in held if row["station"] == station]
        by_station[station] = {method: evaluate(rows, predictor) for method, predictor in predictors.items()}
    days = []
    for day in sorted({row["date"] for row in held}):
        rows = [row for row in held if row["date"] == day]
        days.append({"date": day, **{method: evaluate(rows, predictor) for method, predictor in predictors.items()}})
    coefficients = {}
    for name, instance, value in (("market_only", calibration, calibration_c), ("market_weather", weather_model, weather_c)):
        coefficients[name] = {"C": value, "intercept": float(instance.intercept_[0]),
                              "coefficients": instance.coef_[0].tolist()}
    strongest = max(held, key=lambda row: sum(abs(a - b["market"]) for a, b in
                                             zip(predictors["Market and NOAA logistic update"](row), row["buckets"], strict=True)))
    cases = [case_builder(strongest)]
    cases[0]["learned_bucket_probabilities"] = predictors["Market and NOAA logistic update"](strongest)
    return {
        "metrics": reports, "cases": cases, "test_by_station": by_station, "test_by_date": days,
        "fit": {"blend_weather_weight": locked_weight, "calibration_grid": calibration_grid,
                "weather_grid": weather_grid, "logistic_parameters": coefficients},
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", type=Path, default=DEFAULT_DATA)
    ap.add_argument("--verify", type=Path, help="Reproduce scores from the frozen market and weather panel.")
    args = ap.parse_args()
    data = args.data
    if args.verify:
        evidence = json.loads(gzip.decompress(args.verify.read_bytes()))
        ident = "kalshi-month-" + digest(evidence)[:20]
        previous = json.loads((data / "research" / "models" / f"{ident}.json").read_text())
        if any(previous.get(key) != value for key, value in calculate(evidence["panels"]).items()):
            ap.error("Reproduced market-update result differs from saved record.")
        print(json.dumps({"verified": ident}, indent=2))
        return

    weather_path = study.study_dir(data) / "inputs.json.gz"
    weather_bytes = weather_path.read_bytes()
    weather = json.loads(gzip.decompress(weather_bytes))
    panels, gaps = read_panel(data, weather)
    settings = {
        "engine": ENGINE, "weather_input_sha256": sha256(weather_bytes).hexdigest(),
        "calendar_splits": SPLITS, "blend_grid": BLEND_GRID, "logistic_c": REGULARIZATION,
        "logit_floor": LATER_MINIMUM_PROBABILITY, "sklearn_version": sklearn_version,
        "source": "As-issued NOAA TXN and associated spread against exact Kalshi settlements and archived hourly prices",
    }
    evidence = {"settings": settings, "panels": panels, "gaps": gaps}
    ident = "kalshi-month-" + digest(evidence)[:20]
    result = calculate(panels)
    snapshot = data / "research" / "evidence" / "kalshi" / f"{ident}.json.gz"
    write_new(snapshot, gzip.compress(json_bytes(evidence), mtime=0))
    report = {
        "id": ident, "title": "Can scikit-learn improve Kalshi odds with NOAA?",
        "hypothesis": "Out-of-time market-plus-weather logistic probabilities beat the unmodified venue midpoint proxy and a market-only calibration.",
        "target": "Daily high temperature bucket",
        "venues": [KALSHI], "created_at": iso(datetime.now(timezone.utc)),
        "date_window": {"from": SPLITS["fit"][0], "fit_through": SPLITS["fit"][1], "test_from": SPLITS["later test"][0],
                        "through": SPLITS["later test"][1]},
        "limits": [
            "The learner fits earlier calendar dates with station-day weights and selects regularization on a separate validation window.",
            "Raw NOAA comes from archived issue clocks and provider spread. Its label is a physical forecast compared against venue payouts.",
            "Hourly market candles are probability evidence. They cannot prove executable size or realized paper P&L.",
        ],
        "evidence": {"gaps": gaps},
        "provenance": {**settings, "frozen_inputs": str(snapshot.relative_to(data))},
        **result,
    }
    path = data / "research" / "models" / f"{ident}.json"
    write_new(path, json_bytes(report) + b"\n")
    print(json.dumps({"path": str(path), "fit": result["fit"], "metrics": result["metrics"],
                      "test_by_station": result["test_by_station"], "evidence": gaps}, indent=2))


if __name__ == "__main__":
    main()
