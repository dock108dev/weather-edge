"""Test measurable precipitation forecasts across six stations and both venues."""

from __future__ import annotations

import argparse
import gzip
import json
import statistics
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

import numpy as np
from sklearn.feature_extraction import DictVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from . import expansion_contract, expansion_replay, expansion_weather, research_model
from . import research_model_low_market, research_model_rain, research_model_venue, venue_archive
from .nbm import climate_window, iso, utc

VERSION = "six-station-rain-v1"
STATIONS = ("KNYC", "KPHL", "KMDW", "KORD", "KBOS", "KDEN")
METHODS = research_model_rain.METHODS


def _inputs(data):
    windows = research_model.WINDOWS
    initial, last = windows["fit"][0], windows["test"][1]
    splits = {split: [] for split in windows}
    gaps, provenance = [], {}

    def station_evidence(station):
        source = expansion_weather.daily_labels(data, station, initial, last)
        offset = expansion_weather.STATIONS[station][3]
        names = tuple(windows)
        points, missing = [], []
        for index, (split, (start, end)) in enumerate(windows.items()):
            cutoff = (climate_window(windows[names[index + 1]][0], offset)[0] +
                      timedelta(hours=research_model_rain.HOUR_LST) if index < len(names) - 1 else None)
            for day in research_model._days(start, end):
                label = source.get(str(day))
                forecast = research_model_rain._weather(data, day, station)
                try:
                    if label and split in ("selection", "test"):
                        label = expansion_weather.raw_report(data, station, day, label["product"])
                except (OSError, ValueError, KeyError, TypeError) as error:
                    missing.append({"station": station, "date": str(day), "split": split,
                                    "issue": expansion_contract.availability_error(error)})
                    continue
                outcome = expansion_contract.rain_outcome(label["precip_as_reported"]) if label else None
                if (outcome is None or not forecast or (cutoff and utc(label["published_at"]) >= cutoff)):
                    missing.append({"station": station, "date": str(day), "split": split,
                                    "issue": "No verified forecast and complete report available for this stage"})
                    continue
                points.append((split, {**forecast, "truth": outcome, "official_report": label}))
        identity = research_model._source_entry(data, expansion_weather.cli_file(data, station, initial.year))
        return station, points, missing, identity

    with ThreadPoolExecutor(max_workers=4) as pool:
        for station, points, missing, identity in pool.map(station_evidence, STATIONS):
            for split, row in points:
                splits[split].append(row)
            gaps.extend(missing)
            provenance[station] = identity
    return {"version": VERSION, "rows": splits, "missing": gaps, "windows": research_model._window_strings(),
            "source_files": provenance}


def _features(row, qpf):
    values = research_model_rain._features(row, qpf)
    result = {"station": row["station"], "logit_noaa_pop": values[0]}
    if qpf:
        result["log_qpf"] = values[1]
    return result


def _model(training, rows):
    station_rows = defaultdict(list)
    for row in training:
        station_rows[row["station"]].append(row["truth"])
    output = {
        "climatology": [statistics.mean(station_rows[row["station"]]) for row in rows],
        "nbm": [row["forecast"]["p06_overlap_probability"] for row in rows],
    }
    for method, qpf in (("calibrated", False), ("qpf", True)):
        fit = make_pipeline(DictVectorizer(sparse=False), StandardScaler(), LogisticRegression(C=1, random_state=20261115))
        fit.fit([_features(row, qpf) for row in training], [row["truth"] for row in training])
        output[method] = [float(p) for _, p in fit.predict_proba([_features(row, qpf) for row in rows])]
    return [{**row, "probabilities": {key: vals[index] for key, vals in output.items()}}
            for index, row in enumerate(rows)]


def _scores(rows, group):
    if not rows:
        return []
    dates = len({row["date"] for row in rows})
    stations = len({(row["station"], row["date"]) for row in rows})
    methods = dict(METHODS)
    if all(row.get("market_midpoint") is not None for row in rows):
        methods["market_midpoint"] = "Venue quote midpoint"
    return [{"method": method, "label": label, "group": group,
             "calendar_dates": dates, "station_days": stations, "cases": len(rows), "yes_cases": sum(row["truth"] for row in rows),
             "brier": statistics.mean(((row["market_midpoint"] if method == "market_midpoint"
                                        else row["probabilities"][method]) - row["truth"]) ** 2 for row in rows)}
            for method, label in methods.items()]


def _kalshi_case(data, row, source, candle_cache):
    at = utc(row["at"])
    detail = venue_archive.detail(data, "kalshi", source["instrument_id"])
    raw = detail["raw_market"]
    if (raw.get("ticker") != source["instrument_id"] or source["operator"] != "above" or source["lower"] != "0"
            or "strictly greater than 0 inches" not in source["rules_primary"]
            or "missing daily precipitation values are counted as 0 inches" not in (source["rules_secondary"] or "")):
        return None
    if not raw.get("open_time") or not raw.get("close_time") or not utc(raw["open_time"]) <= at < utc(raw["close_time"]):
        return None
    provider = raw.get("expiration_value")
    numeric = expansion_weather.parse_number(provider)
    if numeric is None and provider != "T":
        return None
    actual = int(numeric > 0) if numeric is not None else 0
    if actual != int(source["yes_payout"]):
        return None
    candle = research_model_venue._midpoint(data, source, candle_cache, at)
    return {**row, "weather_truth": row["truth"], "truth": actual, "venue": "kalshi", "market": source,
            "market_midpoint": candle["midpoint"] if candle else None, "market_price": candle, "provider_amount": provider}


def _poly_case(data, row, market):
    at = utc(row["at"])
    day = date.fromisoformat(row["date"])
    try:
        verified = expansion_contract.rain_market(data, day, row["official_report"], station=row["station"])
    except (OSError, TypeError, KeyError, ValueError):
        return None
    bucket = verified["buckets"][0]
    if bucket["id"] != market["instrument_id"] or not utc(verified["starts_at"]) <= at < utc(verified["scheduled_end_at"]):
        return None
    # This phase reuses the venue archive. Individual case fetch scripts retain
    # their own narrower custom display-history requests for comparison.
    entries = venue_archive.history(data, "polymarket-us", bucket["id"], kind="display-price-1m", limit=1200)
    quote = None
    for item in entries:
        stamp = utc(item["at"])
        if at - stamp != timedelta(minutes=1):
            continue
        yes, no = (expansion_weather.parse_number(item["body"].get(key)) for key in ("longPrice", "shortPrice"))
        if yes is not None and no is not None and 0 <= yes <= 1 and 0 <= no <= 1:
            quote = {"yes": yes, "no": no, "at": item["at"], "receipt_id": item["receipt_id"],
                     "available_at": iso(stamp + timedelta(minutes=1))}
            break
    return {**row, "weather_truth": row["truth"], "truth": bucket["payout_yes"], "venue": "polymarket-us",
            "market": market, "market_verified": verified, "market_price": quote,
            "market_midpoint": research_model_low_market.display_probability(quote)}


def _match(data, weather):
    keys = {(row["station"], row["date"]): row for row in weather}
    archive = defaultdict(list)
    for market in venue_archive.export(data, family="daily-rain"):
        key = market["station"], market["day"]
        if key in keys and market["units"] == "in" and market["yes_payout"] in ("0", "1"):
            archive[key].append(market)
    gaps, rows = [], []
    candles = {}
    for key, options in archive.items():
        for market in options:
            row = keys[key]
            venue = market["venue"]
            case = _kalshi_case(data, row, market, candles) if venue == "kalshi" else _poly_case(data, row, market)
            if case:
                rows.append(case)
            else:
                gaps.append({"venue": venue, "station": key[0], "date": key[1], "instrument_id": market["instrument_id"],
                             "issue": "Terms, open clock, source amount, or binary payout could not be verified for this decision"})
    return rows, gaps


def _case_list(rows, chosen):
    cases = []
    for venue in ("kalshi", "polymarket-us"):
        subset = [row for row in rows if row["venue"] == venue]
        for row in sorted(subset, key=lambda row: (abs(row["truth"] - row["probabilities"][chosen]), row["date"]),
                          reverse=True)[:3]:
            station = row["station"]
            case = {"station": station, "venue": venue, "date": row["date"], "at": row["at"],
                    "instrument_id": row["market"]["instrument_id"],
                    "title": f"{expansion_replay.STATION_NAMES[station]} precipitation threshold",
                    "detail": (f"Forecast {row['probabilities'][chosen]:.1%}. "
                               f"CLI precipitation {row['official_report']['precip_as_reported']}. "
                               f"Verified binary payout YES {row['truth']}."),
                    "forecast": row["forecast"], "official_report": row["official_report"], "market": row["market"]}
            cases.append(case)
    return cases


def study(data, from_input=None):
    data = Path(data)
    inputs = json.loads(gzip.decompress(from_input.read_bytes())) if from_input else _inputs(data)
    path, digest = research_model.freeze_inputs(data, inputs)
    rows = inputs["rows"]
    selection = _model(rows["fit"], rows["selection"])
    test = _model(rows["fit"], rows["test"])
    selection_scores = _scores(selection, "pretest selection")
    chosen = min(selection_scores, key=lambda row: row["brier"])["method"]
    forecasts, gaps = _match(data, test)
    scores = _scores(test, "all same-case weather decisions")
    for station in STATIONS:
        scores.extend(_scores([row for row in test if row["station"] == station], station))
    for venue in ("kalshi", "polymarket-us"):
        venue_cases = [row for row in forecasts if row["venue"] == venue]
        scores.extend(_scores(venue_cases, f"{venue} · verified native payouts"))
        scores.extend(_scores([row for row in venue_cases if row["market_midpoint"] is not None],
                              f"{venue} · same-case prices"))
    by_date = defaultdict(list)
    for row in test:
        d = (row["probabilities"][chosen] - row["truth"]) ** 2 - (row["probabilities"]["nbm"] - row["truth"]) ** 2
        by_date[row["date"]].append(d)
    effects = np.array([statistics.mean(values) for values in by_date.values()])
    bootstrap = np.random.default_rng(20261115).choice(effects, size=(2500, len(effects))).mean(axis=1)
    record = {
        "version": VERSION, "title": "Measurable precipitation across six stations",
        "hypothesis": "Earlier meteorology across several cities supports a transferable calibration of exact weather-venue precipitation thresholds.",
        "target": "One hundredth inch of liquid-equivalent precipitation in the named CLI climate day",
        "venues": sorted({row["venue"] for row in forecasts}), "date_window": inputs["windows"], "metrics": scores,
        "cases": _case_list(forecasts, chosen),
        "limits": [
            "Weather test targets use complete original CLI products. NOAA P06 and Q06 cover six-hour intervals with weighted edge overlap.",
            "Polymarket US threshold and governing date are checked against the actual station rule, official report and explicit exchange payout.",
            "Kalshi's sampled contracts use The Weather Company and require strictly more than 0 inches at the named station, with trace and missing values counted as zero. The Weather Company provides its own report selection and CF6 fallback.",
            "Chicago O’Hare and Midway observations produce different targets even when an event title just says Chicago.",
            "Archived marks support comparisons and have no live recheck or depth. Forecast probabilities use pre-day guidance only.",
        ],
        "provenance": {"input_path": str(path.relative_to(data)), "input_sha256": digest, "pretest_selected": chosen,
                       "source_sha256": research_model.run_sources(sys.modules[__name__], research_model_rain, expansion_contract)},
        "paired_selection_delta_vs_raw_pop": {"calendar_dates": len(effects), "delta_brier": float(effects.mean()),
                                              "interval_95": [float(v) for v in np.quantile(bootstrap, [0.025, 0.975])]},
        "selection_metrics": selection_scores,
        "coverage": {key: len(sample) for key, sample in rows.items()} |
                    {"verified_payout_cases": len(forecasts),
                     "native_weather_payout_differences": sum(row["weather_truth"] != row["truth"] for row in forecasts)},
        "missing": [*inputs["missing"], *gaps],
    }
    return research_model.write_record(data, record, {"test_weather": test, "verified_markets": forecasts,
                                                      "validation": selection})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path(".data"))
    parser.add_argument("--from-input", type=Path)
    args = parser.parse_args()
    print(study(args.data_dir, args.from_input))


if __name__ == "__main__":
    main()
