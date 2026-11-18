"""Test whether observed GOES cloud classifications improve daily-high forecasts."""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date
import gzip
import json
from pathlib import Path
import re
from statistics import mean
import sys

from sklearn import __version__ as sklearn_version

from . import expansion_satellite, expansion_weather, research_model, research_model_high, venue_archive
from .nbm import utc

VERSION = "goes-observed-cloud-high-v1"
START = date(2026, 8, 15)
HOUR = 13
METHODS = {"noaa": "NOAA TXN + XND", "bias": "Earlier station/time bias", "ridge": "Ridge + forecast sky",
           "ridge_station": "Ridge + observed station sky", "ridge_satellite": "Ridge + observed GOES cloud mask",
           "ridge_both": "Ridge + station sky and GOES cloud mask"}
STATION_SKY = {"FEW": 0.1875, "SCT": 0.4375, "BKN": 0.75, "OVC": 1.0, "CLR": 0.0, "SKC": 0.0}
METAR_SKY = re.compile(r"(?<!\S)(FEW|SCT|BKN|OVC)\d{3}(?:CB|TCU)?(?=\s|$)|(?<!\S)(CLR|SKC)(?=\s|$)")


def station_clouds(observation):
    if not observation:
        return None
    raw = observation.get("raw_report") or observation.get("report")
    if not raw:
        return None
    main = raw.partition(" RMK ")[0]
    cover = [STATION_SKY[layer or clear] for layer, clear in METAR_SKY.findall(main)]
    return max(cover) if cover else None


def _weather(data, source):
    content = json.loads(gzip.decompress(source.read_bytes()))
    if content["version"] != research_model_high.VERSION:
        raise ValueError("Expected frozen six-station daily-high input")
    cohorts = {group: [row for row in rows if date.fromisoformat(row["date"]) >= START and row["hour"] == HOUR]
               for group, rows in content["rows"].items()}
    clocks = {row["at"] for rows in cohorts.values() for row in rows}
    gaps = Counter()

    def retrieve(when):
        try:
            return when, expansion_satellite.latest(data, when)
        except (OSError, ValueError, KeyError) as error:
            return when, {"error": str(error)}

    with ThreadPoolExecutor(max_workers=6) as pool:
        frames = dict(pool.map(retrieve, sorted(clocks)))
    matched = {group: [] for group in cohorts}
    for group, rows in cohorts.items():
        for row in rows:
            frame = frames.get(row["at"])
            if not frame or "error" in frame:
                gaps[f"{group}: {frame.get('error', 'No eligible GOES scan') if frame else 'No eligible GOES scan'}"] += 1
                continue
            point = frame["points"].get(row["station"])
            if not point or point["center_class_code"] is None or point["window_good_pixels"] < 20:
                gaps[f"{group}: no complete good-quality GOES footprint"] += 1
                continue
            original = row["official_report"]
            if group in ("selection", "test"):
                expected = original["product"]
                try:
                    original = expansion_weather.raw_report(data, row["station"], date.fromisoformat(row["date"]), expected)
                except (OSError, KeyError, ValueError):
                    original = None
                if not original or original["high_f"] is None:
                    gaps[f"{group}: no raw complete station CLI"] += 1
                    continue
            station_sky = station_clouds(row["latest_observation"])
            known = {key: frame[key] for key in ("valid_at", "scan_starts_at", "scan_ends_at", "created_at", "available_at", "received_at",
                                                "source", "satellite", "product", "projection", "native", "raw_path", "body_sha256", "h5py")}
            matched[group].append({**row, "actual_f": original["high_f"], "official_report": original, "satellite": {**known, "point": point},
                                   "station_sky_fraction": station_sky})
    windows = {**content["windows"], "fit": [str(START), content["windows"]["fit"][1]]}
    return {"version": VERSION, "rows": matched, "baseline": research_model._source_entry(data, source), "windows": windows,
            "expected": {group: len(rows) for group, rows in cohorts.items()}, "gaps": dict(gaps)}


def _features(row, method):
    values = research_model_high._features(row, "ridge_sky")
    if method in ("ridge_station", "ridge_both"):
        sky = row["station_sky_fraction"]
        values.update(station_sky_known=float(sky is not None), station_sky_fraction=sky or 0.0)
    if method in ("ridge_satellite", "ridge_both"):
        footprint = row["satellite"]["point"]
        fraction = footprint["window_cloud_mask_fraction"]
        forecast_sky = (row["forecast_peak"] or {}).get("sky_pct")
        values.update(goes_cloud_mask_fraction=fraction,
                      observed_cloud_minus_peak_forecast=fraction - (forecast_sky / 100 if forecast_sky is not None else 0.0))
    return values


def _native_event(data, station, day, observed):
    rows = venue_archive.query(data, venue="kalshi", family="daily-high", place=station, day=day, limit=50)["rows"]
    return next((row for row in rows if row["yes_payout"] == "1" and row["expiration_value"] is not None and
                 float(row["expiration_value"]) == observed), None)


def _case(data, row):
    station, day = row["station"], row["date"]
    satellite = row["satellite"]
    point = satellite["point"]
    weather = row["forecast_peak"] or {}
    base, added = (row["estimates"][key]["mean_f"] for key in ("ridge", "ridge_satellite"))
    market = _native_event(data, station, day, row["actual_f"])
    detail = (f"GOES classified {point['window_cloud_mask_fraction']:.0%} of {point['window_good_pixels']} nearby good-quality pixels cloudy or probably cloudy. "
              f"NOAA peak forecast sky {weather['sky_pct']}%. " if weather.get("sky_pct") is not None else
              f"GOES classified {point['window_cloud_mask_fraction']:.0%} of nearby pixels cloudy or probably cloudy. ")
    return {"title": f"{station} · Satellite footprint and the daily high", "station": station, "date": day,
            "at": row["at"], "detail": detail + f"Baseline {base:.1f}°F; satellite correction {added:.1f}°F; verified CLI {row['actual_f']}°F.",
            **({"venue": "kalshi", "instrument_id": market["instrument_id"], "market": market} if market else {}),
            "satellite": satellite, "official_report": row["official_report"], "estimates": row["estimates"]}


def _availability(results):
    lags = [(utc(row["satellite"]["available_at"]) - utc(row["satellite"]["valid_at"])).total_seconds() for row in results]
    return {"method": "cloud_mask_coverage", "label": "Native GOES publication clocks", "group": "Held-out detections",
            "native_scans": len({row["satellite"]["body_sha256"] for row in results}),
            "mean_publication_lag_seconds": mean(lags), "maximum_publication_lag_seconds": max(lags),
            "station_sky_report_present": sum(row["station_sky_fraction"] is not None for row in results)}


def evaluate(data, corpus):
    frozen, digest = research_model.freeze_inputs(data, corpus)
    rows = corpus["rows"]
    model = research_model.fit(rows["fit"], rows["calibration"], methods=METHODS, feature_fn=_features)
    earlier = research_model.predictions(model, rows["selection"])
    scored = research_model.predictions(model, rows["test"])
    metrics = research_model.metrics(scored, "Same-case held-out station-days", METHODS)
    metrics.extend(research_model.metrics(earlier, "Earlier selection station-days", METHODS))
    metrics.append(_availability(scored))
    for station in research_model_high.STATIONS:
        metrics.extend(research_model.metrics([row for row in scored if row["station"] == station], station, METHODS))
    changes = [("ridge", "ridge_station"), ("ridge", "ridge_satellite"), ("ridge_station", "ridge_both")]
    intervals = [research_model.date_bootstrap(scored, baseline, candidate, metric)
                 for baseline, candidate in changes for metric in ("brier", "mae")]
    sorted_cases = sorted(scored, key=lambda row: abs(row["estimates"]["ridge"]["error_f"])
                          - abs(row["estimates"]["ridge_satellite"]["error_f"]), reverse=True)
    witnesses = [sorted_cases[0], sorted_cases[-1]]
    central_park = next((row for row in scored if row["station"] == "KNYC" and row["date"] == "2026-11-12"), None)
    if central_park and central_park not in witnesses:
        witnesses.append(central_park)
    record = {"version": VERSION, "title": "Does observed satellite cloud improve the daily-high forecast?",
              "hypothesis": "An as-published cloud classification by the midday forecast decision may improve on station temperature and forecast sky. "
                            "An exact-station METAR sky arm tests whether the remote pixels add independent information.",
              "target": "Verified complete NWS daily maximum at six stations", "venues": [], "date_window": corpus["windows"],
              "metrics": metrics, "cases": [_case(data, row) for row in witnesses], "contrasts": intervals,
              "coverage": {key: research_model._independent_counts(items) for key, items in rows.items()}, "missing": corpus["gaps"],
              "limits": ["The 13:00 local-standard clock includes only completed scans with an original publication clock before the decision. Historical retrieval occurred later.",
                         "A five-by-five native-pixel footprint provides nearby cloud classification. It samples a view from space and can differ from a station sky report or forecast grid cloud cover.",
                         "Earlier station METAR reports use the archive timing assumption from the reference weather study. Exact CLI text is checked for later selection and test labels.",
                         "Synthetic common weather buckets score probability accuracy. Any linked native market is a matching case for inspection."],
              "provenance": {"frozen_input": str(frozen.relative_to(data)), "frozen_input_sha256": digest,
                             "native_docs": expansion_satellite.DOCS, "projection_docs": expansion_satellite.PROJECTION_DOCS,
                             "scikit_learn": sklearn_version,
                             "source_sha256": research_model.run_sources(sys.modules[__name__], expansion_satellite, research_model_high)}}
    return research_model.write_record(data, record, {"test": scored, "selection": earlier})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--baseline-input", type=Path)
    source.add_argument("--input", type=Path)
    args = parser.parse_args()
    corpus = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else _weather(args.data, args.baseline_input)
    print(evaluate(args.data, corpus))


if __name__ == "__main__":
    main()
