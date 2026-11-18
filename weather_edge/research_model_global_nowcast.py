"""Test early-month Copernicus reanalysis against exact NOAA monthly anomaly payouts."""

from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from datetime import date, datetime, timezone
import gzip
from io import StringIO
import json
from math import sqrt
from pathlib import Path
from statistics import mean, NormalDist
import sys

import numpy as np
from sklearn import __version__ as sklearn_version
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from . import expansion_global_monthly, research_model, research_model_global_monthly as monthly
from .nbm import iso, utc

VERSION = "noaa-monthly-copernicus-nowcast-v1"
DATASET = "https://sites.ecmwf.int/data/climatepulse/timeseries/csv/daily/timeseries_era5_t2m_daily_global.csv"
C3S_DOCS = "https://pulse.climate.copernicus.eu/faqs"
C3S_SOURCE = "https://pulse.climate.copernicus.eu/"
PREFIX_LENGTH = 10
METHODS = {"persistence": "Prior dated NOAA release", "ridge": "NOAA-only Ridge",
           "partial": "ERA5 first ten days", "partial_ridge": "NOAA Ridge plus ERA5 first ten days"}


def _daily(data, through):
    key = through.date().isoformat()
    raw, receipt = expansion_global_monthly._get(data, f"c3s-daily-global-{key}", DATASET)
    body = raw.decode("utf-8")
    headings = [line for line in body.splitlines() if line.startswith("#")]
    if "# Title: ERA5 daily 2m temperature, Global domain" not in headings or "# Reference period: 1991-2020" not in headings:
        raise ValueError("Climate Pulse dataset identity or climatology changed")
    output = {}
    rows = csv.DictReader(StringIO("\n".join(line for line in body.splitlines() if not line.startswith("#"))))
    for row in rows:
        day = date.fromisoformat(row["date"])
        actual = float(row["t2m"])
        relative = float(row["anom_1991_2020"])
        if (row["status"] not in {"FINAL", "PRELIMINARY"} or not -20 <= actual <= 40 or not -10 <= relative <= 10):
            raise ValueError("Unrecognized Climate Pulse date or value")
        output[day] = {"date": str(day), "temperature_c": actual, "anomaly_1991_2020_c": relative, "source_flag": row["status"]}
    return output, receipt


def _subsample(row, daily):
    first = monthly._month(row["month"])
    days = [first.replace(day=offset) for offset in range(1, PREFIX_LENGTH + 1)]
    sample = [daily.get(day) for day in days]
    if not all(sample):
        return None
    average = mean(entry["anomaly_1991_2020_c"] for entry in sample)
    return {**row, "daily_prior": sample, "early_month_era5_anomaly_c": average}


def gather(data, source, cutoff):
    inputs = json.loads(gzip.decompress(source.read_bytes()))
    if inputs["version"] != monthly.VERSION:
        raise ValueError("Expected frozen NOAA monthly anomaly report inputs")
    daily, receipt = _daily(data, cutoff)
    rows = {name: [updated for row in items if (updated := _subsample(row, daily))] for name, items in inputs["cases"].items()}
    live = _subsample(inputs["live"], daily) if inputs["live"] else None
    # The archived NOAA comparison determines the original model cut. The live
    # nowcast exposes new inputs only when both local snapshots have been taken.
    if live:
        live["at"] = iso(max(utc(live["at"]), utc(receipt["received_at"])))
    return {"version": VERSION, "rows": rows, "live": live,
            "calendar_windows": monthly.WINDOWS, "date_count_from_source": {name: len(items) for name, items in inputs["cases"].items()},
            "daily_source": receipt, "noaa_frozen": research_model._source_entry(data, source)}


def _features(row, method):
    if method == "partial":
        return [row["early_month_era5_anomaly_c"]]
    base = monthly._design(row)
    if method == "partial_ridge":
        return [*base, row["early_month_era5_anomaly_c"]]
    return base


def _points(row, models):
    estimates = {"persistence": row["lag"][0]}
    for method, model in models.items():
        point = float(model.predict([_features(row, method)])[0])
        estimates[method] = point if method == "partial" else row["lag"][0] + point
    return estimates


def _fit(rows):
    models = {}
    for method in ("ridge", "partial", "partial_ridge"):
        model = make_pipeline(StandardScaler(), Ridge(alpha=10))
        forecast = [row["actual_c"] - (0 if method == "partial" else row["lag"][0]) for row in rows["fit"]]
        model.fit([_features(row, method) for row in rows["fit"]], forecast)
        models[method] = model
    residuals = defaultdict(list)
    for row in rows["calibration"]:
        for method, pred in _points(row, models).items():
            residuals[method].append(row["actual_c"] - pred)
    calibration = {key: {"bias_c": mean(values), "sd_c": max(0.02, sqrt(mean((value - mean(values)) ** 2 for value in values)))}
                   for key, values in residuals.items()}
    return models, calibration


def _score(rows, models, calibration):
    output = []
    for row in rows:
        points = _points(row, models)
        distributions = {key: NormalDist(value + calibration[key]["bias_c"], calibration[key]["sd_c"]) for key, value in points.items()}
        prices = []
        for market in row["markets"]:
            prices.append({"native_yes": market["yes_payout"], "instrument_id": market["instrument_id"],
                           "probabilities": {key: monthly._probability(dist, market) for key, dist in distributions.items()}})
        output.append({**row, "points_c": points, "probabilities": prices})
    return output


def _summary(rows, label):
    payouts = [case for row in rows for case in row["probabilities"] if case["native_yes"] is not None]
    return [{"group": label, "method": method, "label": title, "calendar_months": len(rows), "payout_cases": len(payouts),
             "mae_c": mean(abs(row["points_c"][method] - row["actual_c"]) for row in rows),
             **({"bucket_brier": mean((case["probabilities"][method] - int(case["native_yes"])) ** 2 for case in payouts)} if payouts else {})}
            for method, title in METHODS.items()]


def _changes(rows):
    phase = defaultdict(list)
    for row in rows:
        if not row["probabilities"]:
            continue
        first = monthly._month(row["month"])
        differences = [(entry["probabilities"]["partial_ridge"] - int(entry["native_yes"])) ** 2
                       - (entry["probabilities"]["ridge"] - int(entry["native_yes"])) ** 2 for entry in row["probabilities"]]
        phase[first.year, (first.month - 1) // 3].append(mean(differences))
    values = [mean(group) for group in phase.values()]
    distribution = np.random.default_rng(20261115).choice(values, size=(2500, len(values))).mean(axis=1)
    return {"partial_minus_noaa_ridge_brier": mean(values), "quarters": len(values),
            "quarter_bootstrap_ci_95": np.quantile(distribution, (0.025, 0.975)).tolist()}


def _case(row):
    market = next((item for item in row["markets"] if item["yes_payout"] == "1"), None)
    if market is None:
        market = next((item for item in row["markets"] if item["operator"] == "above"), None)
    if not market:
        return None
    before, after = (row["points_c"][key] for key in ("ridge", "partial_ridge"))
    detail = (f"First ten ERA5 days averaged {row['early_month_era5_anomaly_c']:+.2f}°C against 1991–2020. "
              f"The NOAA-only point was {before:.2f}°C and the ERA5-informed NOAA estimate was {after:.2f}°C.")
    if market["yes_payout"] == "1":
        detail += f" Actual native expiry recorded {market['expiration_value']}, and this strike paid YES."
    return {"title": f"{row['month']} · ERA5 and the NOAA global temperature strike", "date": row["date"], "at": row["at"],
            "venue": "kalshi", "instrument_id": market["instrument_id"], "region": "Global land and ocean",
            "detail": detail, "data": row, "market": market}


def evaluate(data, corpus):
    path, digest = research_model.freeze_inputs(data, corpus)
    models, calibration = _fit(corpus["rows"])
    earlier, later = (_score(corpus["rows"][phase], models, calibration) for phase in ("selection", "test"))
    cases = []
    if corpus["live"]:
        if live := _case(_score([corpus["live"]], models, calibration)[0]):
            cases.append(live)
    ranked = sorted((row for row in later if row["markets"]), key=lambda row: (
        abs(row["points_c"]["ridge"] - row["actual_c"]) - abs(row["points_c"]["partial_ridge"] - row["actual_c"])), reverse=True)
    cases.extend(case for row in (ranked[0], ranked[-1]) if (case := _case(row)))
    record = {"version": VERSION, "title": "Can early-month Copernicus temperatures improve the NOAA nowcast?",
              "hypothesis": "The first ten daily ERA5 values provide a measured trend within the ongoing month. An earlier calibrated link may improve the NOAA anomaly forecast.",
              "target": "NOAA Global Land and Ocean monthly departure and exact Kalshi payouts", "venues": ["kalshi"],
              "date_window": corpus["calendar_windows"], "metrics": _summary(earlier, "Earlier selection") + _summary(later, "Same later months and binary payouts"),
              "cases": cases, "matched_months": {key: len(rows) for key, rows in corpus["rows"].items()}, "uncertainty": _changes(later),
              "limits": ["The 16th at 00:00Z decision consumes only ERA5 dates 1–10, leaving five days after the last UTC day closes.",
                         "Historical ERA5 is the publisher's present reanalysis table. Original day-level archive receipt and earlier revisions are unavailable.",
                         "ERA5 represents two-meter air temperature against 1991–2020. The target combines NOAA land and ocean temperatures against 1901–2000.",
                         "Live probabilities require both previously received source snapshots, and remain research estimates."],
              "provenance": {"frozen_input": str(path.relative_to(data)), "frozen_input_sha256": digest,
                             "daily_source": corpus["daily_source"], "publisher_documentation": C3S_DOCS, "noaa_frozen": corpus["noaa_frozen"],
                             "calibration": calibration, "scikit_learn": sklearn_version,
                             "source_sha256": research_model.run_sources(sys.modules[__name__], monthly)}}
    return research_model.write_record(data, record, {"selection": earlier, "test": later, "live": corpus["live"]})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    sources = parser.add_mutually_exclusive_group(required=True)
    sources.add_argument("--noaa-input", type=Path)
    sources.add_argument("--input", type=Path)
    args = parser.parse_args()
    records = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else gather(args.data, args.noaa_input, datetime.now(timezone.utc))
    print(evaluate(args.data, records))


if __name__ == "__main__":
    main()
