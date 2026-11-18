"""Test earlier satellite cloud against station conditions in the daily-low forecast."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
import gzip
import json
from math import sqrt
from pathlib import Path
from statistics import mean
import sys

from sklearn import __version__ as sklearn_version

from . import expansion_live_low, expansion_nbm, expansion_satellite, research_model, venue_archive
from . import research_model_low_full_day as physical, research_model_low_precision as precision
from . import research_model_satellite, research_model_solar
from .model import _limits, _outcome, _probability
from .nbm import utc

VERSION = "goes-night-cloud-low-v1"
HOUR = 3
METHODS = {
    "noaa": "Observed and remaining NOAA minimum",
    "bias": "Earlier station bias",
    "ridge": "Ridge with station cooling and forecast sky",
    "ridge_station": "Ridge plus METAR sky",
    "ridge_satellite": "Ridge plus GOES mask",
    "ridge_both": "Ridge plus METAR sky and GOES mask",
}
LABELS = {"txn": "Carried NOAA TXN", **METHODS}
GUIDE = "https://goes-r.noaa.gov/products/docs/PUG-L2%2B-vol5.pdf"


def _forecast_sky(card, decision):
    """Use the forecast knots for the next six hours of overnight weather."""
    values = [point["SKY"] / 100 for point in card["points"]
              if "SKY" in point and decision <= utc(point["valid_at"]) <= decision + timedelta(hours=6)]
    return mean(values) if len(values) >= 2 else None


def _gather(data, baseline, verified):
    original = json.loads(gzip.decompress(baseline.read_bytes()))
    exact = json.loads(gzip.decompress(verified.read_bytes()))
    if original["version"] != physical.VERSION or exact["version"] != precision.VERSION:
        raise ValueError("Expected frozen full-day low and exact CLI confirmation inputs")
    cohorts = {phase: [row for row in rows if row["hour"] == HOUR] for phase, rows in original["rows"].items()}
    verified_reports = {(row["station"], row["date"]): row["official_report"] for rows in exact["rows"].values() for row in rows}
    for phase in ("january_2026", "april_2026"):
        cohorts[phase] = [row for row in exact["rows"][phase] if row["hour"] == HOUR]
    clocks = sorted({row["at"] for rows in cohorts.values() for row in rows})

    def retrieve(at):
        try:
            return expansion_satellite.latest(data, at)
        except (OSError, ValueError, KeyError) as error:
            return {"error": str(error)}

    with ThreadPoolExecutor(max_workers=4) as pool:
        scenes = dict(zip(clocks, pool.map(retrieve, clocks), strict=True))
    windows = {**original["windows"], **{phase: exact["windows"][phase] for phase in ("january_2026", "april_2026")}}
    output = {phase: [] for phase in cohorts}
    missing = Counter()
    for phase, rows in cohorts.items():
        for row in rows:
            station, day = row["station"], row["date"]
            at = utc(row["at"])
            scene = scenes[row["at"]]
            point = scene["points"].get(station) if scene and "error" not in scene else None
            if not point or point["window_good_pixels"] < 20 or point["window_cloud_mask_fraction"] is None:
                missing[f"{phase}: no good-quality native footprint"] += 1
                continue
            sun = research_model_solar.solar_cosine(scene["valid_at"], point["lat"], point["lon"])
            if sun > -0.02:
                missing[f"{phase}: sun near or above horizon"] += 1
                continue
            forecast = row["forecast"]
            issued = utc(forecast["issued_at"])
            card = expansion_nbm.load_card(data, station, issued)
            if not card or card["card_sha256"] != forecast["card_sha256"] or utc(card["available_at"]) > at:
                missing[f"{phase}: no matching issued NBS sky card"] += 1
                continue
            sky = _forecast_sky(card, at)
            if sky is None:
                missing[f"{phase}: insufficient eligible forecast sky knots"] += 1
                continue
            official = verified_reports.get((station, day))
            if official is None and phase != "fit":
                missing[f"{phase}: no raw complete exact-station CLI"] += 1
                continue
            if official is None:
                official = row["official_report"]
            keys = ("source", "product", "satellite", "valid_at", "available_at", "received_at",
                    "scan_starts_at", "raw_path", "body_sha256", "projection")
            output[phase].append({**row, "forecast": {**forecast, "mean_f": row["means"]["combined"], "sd_f": None},
                                  "actual_f": official["low_f"], "official_report": official, "label_is_raw_cli": (station, day) in verified_reports,
                                  "satellite": {**{key: scene[key] for key in keys}, "point": point},
                                  "forecast_cloud_fraction": sky, "solar_zenith_cosine": sun,
                                  "station_cloud_fraction": research_model_satellite.station_clouds(row["latest_observation"])})
    return {"version": VERSION, "windows": windows, "rows": output,
            "missing": dict(missing), "expected": {phase: len(rows) for phase, rows in cohorts.items()},
            "sources": [research_model._source_entry(data, baseline), research_model._source_entry(data, verified)]}


def _features(row, method):
    features = physical._features(row)
    current = row["latest_observation"] or {}
    temp, dew = current.get("temp_f"), current.get("dewpoint_f")
    wind = current.get("wind_speed_knots")
    features.update(forecast_cloud_fraction=row["forecast_cloud_fraction"], has_station_dewpoint=float(temp is not None and dew is not None),
                    dewpoint_depression_f=temp - dew if temp is not None and dew is not None else 0,
                    has_station_wind=float(wind is not None), station_wind_knots=wind or 0)
    if method in ("ridge_station", "ridge_both"):
        station_sky = row["station_cloud_fraction"]
        features.update(has_station_sky=float(station_sky is not None), station_cloud_fraction=station_sky or 0)
    if method in ("ridge_satellite", "ridge_both"):
        satellite_sky = row["satellite"]["point"]["window_cloud_mask_fraction"]
        features["satellite_minus_forecast_cloud"] = satellite_sky - row["forecast_cloud_fraction"]
    return features


def _score(rows, fitted, txn_width):
    buckets = {(row["station"], row["date"]): research_model._benchmark_buckets(round(row["means"]["txn"])) for row in rows}
    outputs = research_model.predictions(fitted, rows, buckets)
    for row in outputs:
        txn, width = row["means"]["txn"], txn_width[row["station"]]
        probabilities = [_probability(_limits(bucket), [txn], width) for bucket in row["buckets"]]
        row["estimates"]["txn"] = {"mean_f": txn, "error_f": txn - row["actual_f"], "scale_f": width,
                                   "probabilities": probabilities, "brier": mean((p - outcome) ** 2
                                       for p, outcome in zip(probabilities, row["truth"], strict=True))}
    return outputs


def _native(data, rows):
    output = []
    for row in rows:
        day, station = row["date"], row["station"]
        contracts = venue_archive.query(data, venue="polymarket-us", family="daily-low", place=station, day=day, limit=40)["rows"]
        by_id = {market["instrument_id"]: market for market in contracts}
        event = {"family": "daily-low", "buckets": [{"id": ident, "rules": market["rules_primary"]} for ident, market in by_id.items()]}
        try:
            buckets = expansion_live_low._validated_buckets(event, date.fromisoformat(day), station)
        except (ValueError, KeyError, TypeError):
            continue
        for bucket in buckets:
            native = by_id[bucket["id"]]
            payout = native["yes_payout"]
            if payout not in ("0", "1") or _outcome(bucket, row["actual_f"]) != int(payout):
                continue
            decision = utc(row["at"])
            if not native["opened_at"] or not native["closed_at"] or not utc(native["opened_at"]) <= decision < utc(native["closed_at"]):
                continue
            probabilities = {key: _probability(_limits(bucket), [estimate["mean_f"]], estimate["scale_f"])
                             for key, estimate in row.get("estimates", {}).items()}
            output.append({"station": station, "date": day, "at": row["at"], "native": native, "payout_yes": int(payout),
                           "probabilities": probabilities, "bucket": bucket})
    return output


def _case(row, native):
    pick = next((item["native"] for item in native if item["station"] == row["station"] and item["date"] == row["date"]
                 and item["payout_yes"]), None)
    fraction = row["satellite"]["point"]["window_cloud_mask_fraction"]
    base, improved = (row["estimates"][method]["mean_f"] for method in ("ridge", "ridge_satellite"))
    return {"station": row["station"], "date": row["date"], "at": row["at"],
            "title": f"{row['station']} · Predawn cloud cover and the daily minimum",
            "detail": (f"Before dawn, GOES classified {fraction:.0%} of nearby usable pixels cloudy or probably cloudy. "
                       f"NOAA forecast {row['forecast_cloud_fraction']:.0%} sky cover over the next six hours. "
                       f"Ridge {base:.1f}°F, satellite correction {improved:.1f}°F, verified CLI low {row['actual_f']}°F."),
            **({"instrument_id": pick["instrument_id"], "venue": "polymarket-us", "market": pick} if pick else {}),
            "evidence": row}


def evaluate(data, corpus):
    inputs, digest = research_model.freeze_inputs(data, corpus)
    stages = corpus["rows"]
    fitted = research_model.fit(stages["fit"], stages["calibration"], methods=METHODS, feature_fn=_features)
    errors = defaultdict(list)
    for row in stages["calibration"]:
        errors[row["station"]].append((row["actual_f"] - row["means"]["txn"]) ** 2)
    txn_width = {station: max(0.75, sqrt(mean(values))) for station, values in errors.items()}
    results = {phase: _score(rows, fitted, txn_width) for phase, rows in stages.items() if phase not in ("fit", "calibration")}
    metrics = [value for phase, rows in results.items() for value in research_model.metrics(rows, phase, LABELS)]
    contrasts = [{"group": phase, **research_model.date_bootstrap(rows, base, added, "brier")}
                 for phase, rows in results.items() if rows for base, added in (
                     ("ridge", "ridge_station"), ("ridge", "ridge_satellite"), ("ridge_station", "ridge_both"))]
    native = _native(data, results["later"])
    if native:
        counts = research_model._independent_counts(native)
        counts["snapshots"] = len({(row["station"], row["date"], row["at"]) for row in native})
        metrics.extend({"method": method, "label": label, "group": "Native settled Polymarket US buckets",
                        **counts, "bucket_cases": len(native),
                        "bucket_brier": mean((row["probabilities"][method] - row["payout_yes"]) ** 2 for row in native)}
                       for method, label in LABELS.items())
    samples = sorted(results["later"], key=lambda row: row["estimates"]["ridge"]["brier"] -
                     row["estimates"]["ridge_satellite"]["brier"])
    cases = [_case(row, native) for row in (samples[0], samples[-1])] if samples else []
    record = {"version": VERSION, "title": "Predawn satellite clouds and the daily low",
              "hypothesis": "An observed cloudy or clear footprint may predict a different remaining cooling rate from NOAA's six-hour sky guidance. The paired models isolate the satellite feature from local METAR sky.",
              "target": "Minimum temperature on the complete fixed local-standard climate day",
              "venues": ["polymarket-us"] if native else [], "date_window": corpus["windows"],
              "coverage": {phase: {**research_model._independent_counts(rows),
                                  "native_scans": len({row["satellite"]["body_sha256"] for row in rows}),
                                  "raw_cli": sum(row["label_is_raw_cli"] for row in rows)} for phase, rows in stages.items()},
              "metrics": metrics, "contrasts": contrasts, "cases": cases, "missing": corpus["missing"],
              "limits": ["Every model uses the same 03:00 local-standard snapshot with a completed, published 2 km GOES CONUS cloud classification before that time.",
                         "The 2025 training window uses earlier published IEM report summaries. All later scores require complete exact-station NWS daily report text.",
                         "Historical point observations carry a ten-minute archive timing assumption. GOES and NBS keep their distinct original archive publication and local retrieval clocks.",
                         "The forecast sky value summarizes native knots in the next six hours. The satellite map measures a neighborhood at one earlier instant.",
                         "Synthetic weather buckets compare each arm. A separate score uses independently settled exact native Polymarket US buckets without execution."],
              "provenance": {"frozen_input": str(inputs.relative_to(data)), "frozen_input_sha256": digest,
                             "guide": GUIDE, "scikit_learn": sklearn_version,
                             "source_sha256": research_model.run_sources(sys.modules[__name__], expansion_satellite, physical)}}
    return research_model.write_record(data, record, {"weather": results, "settled_native": native})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--verified", type=Path)
    parser.add_argument("--input", type=Path)
    args = parser.parse_args()
    if args.input is None and (args.baseline is None or args.verified is None):
        parser.error("Provide both archived studies or a frozen input")
    corpus = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else _gather(args.data, args.baseline, args.verified)
    print(evaluate(args.data, corpus))


if __name__ == "__main__":
    main()
