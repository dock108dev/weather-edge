"""Compare as-issued HRRR station-day extrema with the NOAA NBM baseline."""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
import gzip
from hashlib import sha256
import json
import math
from pathlib import Path
import statistics
import sys

from sklearn.feature_extraction import DictVectorizer
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from . import expansion_nbm, expansion_replay, expansion_weather, hrrr, research_model, venue_archive
from .archive import date_range
from .nbm import climate_window, iso, utc
from .venue_contracts import temperature_window

VERSION = "hrrr-extrema-v1"
STATIONS = ("KNYC", "KMDW", "KPHL")
WINDOWS = (
    ("fit", date(2026, 10, 15), date(2026, 10, 25)),
    ("calibration", date(2026, 10, 26), date(2026, 10, 31)),
    ("test", date(2026, 11, 1), date(2026, 11, 14)),
)
SUMMER = (
    ("fit", date(2026, 7, 1), date(2026, 7, 21)),
    ("calibration", date(2026, 7, 22), date(2026, 7, 31)),
    ("test", date(2026, 8, 1), date(2026, 8, 21)),
)
SUMMER_STATIONS = ("KNYC", "KMDW", "KPHL", "KBOS", "KDEN", "KORD")
METHODS = ("nbm", "hrrr", "mean", "ridge")
UTC = timezone.utc


def _nbm(data, station, day, target, start, end):
    eligible = []
    for issue_day in (day - timedelta(days=1), day):
        for hour in (0, 6, 12, 18):
            issue = datetime.combine(issue_day, time(hour), UTC)
            card = expansion_nbm.load_card(data, station, issue)
            if not card or not utc(card["issued_at"]) <= utc(card["available_at"]) <= start:
                continue
            if start - issue > timedelta(hours=24):
                continue
            if target == "low":
                row = research_model._forecast(card, day, expansion_weather.STATIONS[station][3])
            else:
                high = expansion_replay._high_forecast(card, start, end)
                row = {**high, "mean_f": high["high_f"], "sd_f": high["high_sd_f"]} if high else None
            if row:
                eligible.append({**row, "archive": str(expansion_nbm.card_path(data, station, issue).relative_to(data))})
    return max(eligible, key=lambda row: utc(row["issued_at"]), default=None)


def _station_hrrr(data, station, day, start, end):
    issue = datetime.combine(day, time.min, UTC)
    frames = []
    for lead in hrrr.LEADS:
        if not start <= issue + timedelta(hours=lead) < end:
            continue
        path = hrrr.frame_path(data, issue, lead)
        try:
            frame = json.loads(path.read_bytes())
        except FileNotFoundError:
            continue
        if (frame.get("issued_at") != iso(issue) or frame.get("valid_at") != iso(issue + timedelta(hours=lead))
                or not utc(frame["issued_at"]) <= utc(frame["available_at"]) <= start
                or station not in frame["stations"]):
            continue
        frames.append({
            **frame["stations"][station], "valid_at": frame["valid_at"],
            "available_at": frame["available_at"], "source": frame["source"],
            "etag": frame["source_etag"], "byte_range": frame["byte_range"], "sha256": frame["sha256"],
            "archive": frame["raw"],
        })
    if len(frames) != int((end - start).total_seconds() // 3600):
        return None
    return {"issued_at": iso(issue), "available_at": max(row["available_at"] for row in frames),
            "frames": frames, "high_f": max(row["temp_f"] for row in frames),
            "low_f": min(row["temp_f"] for row in frames)}


def _verified_buckets(data, station, day, target, actual, start, end):
    rows = venue_archive.query(data, venue="polymarket-us", family=f"daily-{target}", place=station,
                               day=str(day), status="settled", limit=40)["rows"]
    buckets = []
    for market in rows:
        window = market.get("climate_starts_at"), market.get("climate_ends_at")
        if (market["station"] != station or market["resolution_source"] != "NWS Daily Climate Report"
                or not all(window) or (utc(window[0]), utc(window[1])) != (start, end)
                or market.get("yes_payout") not in {"0", "1"}):
            continue
        bounds = temperature_window(market)
        if bounds is None:
            continue
        low, high = bounds
        won = (low is None or low <= actual) and (high is None or actual <= high)
        if int(market["yes_payout"]) != won:
            return []
        buckets.append({
            "id": market["instrument_id"], "label": market["label"], "low_f": low, "high_f": high,
            "yes_payout": int(won), "payout_source": market["settlement_source"],
            "rules": market["rules_primary"], "settlement_received_at": market["settlement_received_at"],
        })
    if not buckets or sum(row["yes_payout"] for row in buckets) != 1:
        return []
    ordered = sorted(buckets, key=lambda row: row["low_f"] if row["low_f"] is not None else -math.inf)
    if ordered[0]["low_f"] is not None or ordered[-1]["high_f"] is not None:
        return []
    if any(left["high_f"] is None or right["low_f"] is None or left["high_f"] + 1 != right["low_f"]
           for left, right in zip(ordered, ordered[1:])):
        return []
    return ordered


def inputs(data, *, season="autumn"):
    data = Path(data)
    windows, stations = (SUMMER, SUMMER_STATIONS) if season == "summer" else (WINDOWS, STATIONS)
    version = "hrrr-summer-extrema-v1" if season == "summer" else VERSION
    labels = {
        station: expansion_weather.daily_labels(data, station, windows[0][1], windows[-1][2])
        for station in stations
    }
    sources = []
    for station in stations:
        path = expansion_weather.cli_file(data, station, 2026)
        sources.append({"path": str(path.relative_to(data)), "sha256": sha256(path.read_bytes()).hexdigest()})
    sets = {kind: [] for kind, _, _ in windows}
    missing = defaultdict(int)
    for idx, (split, first, last) in enumerate(windows):
        for day in date_range(first, last):
            for station in stations:
                offset = expansion_weather.STATIONS[station][3]
                start, end = climate_window(day, offset)
                next_cutoff = climate_window(windows[idx + 1][1], offset)[0] if idx < len(windows) - 1 else None
                label = labels[station].get(str(day))
                if not label or (next_cutoff and utc(label["published_at"]) >= next_cutoff):
                    missing[f"{split}: unavailable CLI"] += 1
                    continue
                native = _station_hrrr(data, station, day, start, end)
                if native is None:
                    missing[f"{split}: HRRR 24-hour coverage"] += 1
                    continue
                for target in ("high", "low"):
                    nbm = _nbm(data, station, day, target, start, end)
                    actual = label[f"{target}_f"]
                    if nbm is None or actual is None:
                        missing[f"{split} {target}: no earlier NBM or CLI value"] += 1
                        continue
                    buckets = _verified_buckets(data, station, day, target, actual, start, end) if split == "test" else []
                    sets[split].append({
                        "station": station, "date": str(day), "at": iso(start), "target": target, "actual_f": actual,
                        "label": label, "nbm": nbm, "hrrr": native, "buckets": buckets,
                    })
    return {
        "version": version, "season": season, "stations": stations,
        "date_window": {kind: [str(first), str(last)] for kind, first, last in windows},
        "sources": sources, "missing": dict(missing), "sets": sets,
    }


def _features(row):
    model, operational = row["hrrr"], row["nbm"]
    return {
        "station": row["station"],
        "nbm_f": float(operational["mean_f"]),
        "hrrr_minus_nbm_f": model[f"{row['target']}_f"] - operational["mean_f"],
        "hrrr_range_f": model["high_f"] - model["low_f"],
    }


def _prediction(row, method, model):
    nbm = row["nbm"]["mean_f"]
    native = row["hrrr"][f"{row['target']}_f"]
    if method == "ridge":
        return nbm + float(model.predict([_features(row)])[0])
    return {"nbm": nbm, "hrrr": native, "mean": (native + nbm) / 2}[method]


def _cdf(number, mean, sd):
    return (1 + math.erf((number - mean) / (sd * math.sqrt(2)))) / 2


def _probability(row, mean, sd):
    lower, upper = row["low_f"], row["high_f"]
    return ((1.0 if upper is None else _cdf(upper + .5, mean, sd))
            - (0.0 if lower is None else _cdf(lower - .5, mean, sd)))


def _case(row, values):
    station, day = row["station"], row["date"]
    bucket = next((item for item in row["buckets"] if item["yes_payout"]), None)
    return {
        "station": station, "date": day, "at": row["at"],
        "venue": "polymarket-us" if bucket else None, "instrument_id": bucket["id"] if bucket else None,
        "title": f"{station} {day} · HRRR {row['target']} forecast",
        "detail": (f"Official {row['target']} {row['actual_f']:.1f}°F. NBM {values['nbm']:.1f}°F. "
                   f"HRRR {values['hrrr']:.1f}°F. Ridge blend {values['ridge']:.1f}°F."),
        "forecast": {"nbm": row["nbm"], "hrrr": row["hrrr"]}, "official_report": row["label"], "bucket": bucket,
    }


def run(data, frozen=None, *, season="autumn"):
    if frozen is None:
        data_inputs = inputs(data, season=season)
        frozen, digest = research_model.freeze_inputs(Path(data), data_inputs)
    else:
        serialized = gzip.decompress(Path(frozen).read_bytes())
        data_inputs = json.loads(serialized)
        digest = sha256(serialized).hexdigest()
    metrics, cases = [], []
    fitted = {}
    for target in ("high", "low"):
        subsets = [[row for row in data_inputs["sets"][kind] if row["target"] == target] for kind, *_ in WINDOWS]
        training, calibration, test = subsets
        if not all(subsets):
            continue
        model = make_pipeline(DictVectorizer(sparse=False), StandardScaler(), Ridge(alpha=10))
        model.fit([_features(row) for row in training], [row["actual_f"] - row["nbm"]["mean_f"] for row in training])
        sigma = {method: max(.5, math.sqrt(statistics.mean(
            (row["actual_f"] - _prediction(row, method, model)) ** 2 for row in calibration
        ))) for method in METHODS}
        fitted[target] = {
            "fit_station_days": len(training), "calibration_station_days": len(calibration), "residual_rmse_f": sigma,
        }
        errors = defaultdict(list)
        brier = defaultdict(list)
        tests = []
        for row in test:
            values = {method: _prediction(row, method, model) for method in METHODS}
            tests.append((row, values))
            for method, value in values.items():
                errors[method].append(value - row["actual_f"])
                brier[method].extend((_probability(bucket, value, sigma[method]) - bucket["yes_payout"]) ** 2
                                     for bucket in row["buckets"])
        for method in METHODS:
            metrics.append({
                "target": target, "method": method, "stations": len({row["station"] for row in test}),
                "calendar_dates": len({row["date"] for row in test}), "station_days": len(test),
                "mean_error_f": statistics.mean(errors[method]), "mae_f": statistics.mean(map(abs, errors[method])),
                "rmse_f": math.sqrt(statistics.mean(x * x for x in errors[method])),
                "bucket_cases": len(brier[method]), "market_station_days": sum(bool(row["buckets"]) for row in test),
                "bucket_brier": statistics.mean(brier[method]) if brier[method] else None,
            })
        best = sorted(tests, key=lambda pair: abs(pair[1]["hrrr"] - pair[0]["actual_f"])
                      - abs(pair[1]["nbm"] - pair[0]["actual_f"]))
        cases.extend(_case(*item) for item in (best[0], best[-1]))
    return research_model.write_record(data, {
        "version": data_inputs["version"],
        "title": "3 km HRRR summer daily extremes against NOAA NBM" if data_inputs.get("season") == "summer"
        else "3 km HRRR daily extremes against NOAA NBM",
        "hypothesis": "A local hourly HRRR temperature trace improves a midnight forecast of the exact station climate-day high or low.",
        "target": "daily high and daily low",
        "date_window": data_inputs.get("date_window", {kind: [str(first), str(last)] for kind, first, last in WINDOWS}),
        "metrics": metrics, "cases": cases, "venues": ["polymarket-us"],
        "limits": [
            "A 3 km model cell represents an area near the observing station. The model extrema use 24 hourly values.",
            "All point and probability comparisons use the same cases. Earlier residual spreads calibrate normal bucket probabilities.",
            "Payout scoring requires a complete whole-degree bucket partition, exact reporting window, and agreement between the NWS value and the explicit venue payouts.",
            "Earlier labels use the IEM transport of official daily climate products. Its annual summary can reflect later corrections.",
        ],
        "provenance": {
            "inputs": str(frozen), "input_sha256": digest, "hrrr_documentation": hrrr.DOCS, "fitted": fitted,
            "missing": data_inputs["missing"], "station_ids": data_inputs.get("stations", STATIONS),
            "ridge_alpha": 10, "decision": "00:00 local standard time",
            "source_code": research_model.run_sources(sys.modules[__name__], hrrr, expansion_replay),
            "reproduce": f"python -m weather_edge.research_hrrr --inputs {frozen}",
        },
    })


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--inputs", type=Path)
    parser.add_argument("--season", choices=("autumn", "summer"), default="autumn")
    args = parser.parse_args()
    print(run(args.data, args.inputs, season=args.season))


if __name__ == "__main__":
    main()
