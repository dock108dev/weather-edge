"""Test the near-station HRRR thermal gradient on independent afternoon highs."""
from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
from datetime import date, timedelta
import gzip
from hashlib import sha256
import json
from math import asin, atan2, cos, degrees, radians, sin
from pathlib import Path
import re
import sys

from sklearn import __version__ as sklearn_version
from sklearn.feature_extraction import DictVectorizer
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from threadpoolctl import threadpool_limits

from . import expansion_weather, hrrr, research_hrrr_intraday, research_model
from .market import parse_time

VERSION = "hrrr-upwind-thermal-gradient-v1"
WIND = re.compile(r"^\d{3}(\d{2,3})(?:G\d{2,3})?KT$")
DISTANCE_KM = 40
MIN_SPEED_KT = 5
MAX_AGE = timedelta(minutes=90)
METHODS = {"nbm": "HRRR local Ridge", "upwind": "Local Ridge + 40 km upwind gradient",
           "downwind": "Local Ridge + 40 km downwind control"}


def destination(lat, lon, direction):
    latitude, longitude, bearing = map(radians, (lat, lon, direction))
    radius = DISTANCE_KM / 6371.0
    target = asin(sin(latitude) * cos(radius) + cos(latitude) * sin(radius) * cos(bearing))
    delta = atan2(sin(bearing) * sin(radius) * cos(latitude), cos(radius) - sin(latitude) * sin(target))
    return degrees(target), (degrees(longitude + delta) + 180) % 360 - 180


def wind_at(observations, clocks, at):
    index = bisect_right(clocks, at) - 1
    if index < 0:
        return None
    row = observations[index]
    if at - parse_time(row["at"]) > MAX_AGE:
        return None
    for part in (row["raw_report"] or "").split(" RMK ", 1)[0].split():
        match = WIND.fullmatch(part)
        if match and MIN_SPEED_KT <= int(match[1]) and 10 <= int(part[:3]) <= 360:
            return {"direction_deg": int(part[:3]) % 360, "speed_kt": int(match[1]),
                    "at": row["at"], "available_at": row["available_at"], "report": row["raw_report"]}
    return None


def _observations(data, parent, station):
    start = date.fromisoformat(parent["source_dates"]["from"])
    stop = date.fromisoformat(parent["source_dates"]["through"]) + timedelta(days=2)
    candidates = []
    for path in (data / "expansion" / "asos").glob(f"{station}-????-??-??-????-??-??.csv"):
        begin, end = date.fromisoformat(path.name[5:15]), date.fromisoformat(path.name[16:26])
        if begin <= start and stop <= end:
            candidates.append((end - begin, begin, end, path))
    if not candidates:
        return [], [], None
    _, begin, end, path = min(candidates)
    rows = expansion_weather.observed_data(data, station, begin, end)
    return rows, [parse_time(row["available_at"]) for row in rows], {
        "raw": str(path.relative_to(data)), "sha256": sha256(path.read_bytes()).hexdigest(),
        "source": rows[0]["source_url"],
    }


def _freeze(data, source):
    payload = gzip.decompress(source.read_bytes())
    parent = json.loads(payload)
    if parent["version"] != research_hrrr_intraday.VERSION:
        raise ValueError("This comparison requires frozen complete intraday HRRR inputs")
    inputs = {"version": VERSION, "parent_sha256": sha256(payload).hexdigest(),
              "source_record": parent["source_record"], "source_dates": parent["source_dates"],
              "payouts": parent["payouts"], "stations": {}, "calibration": [], "test": []}
    grouped = defaultdict(list)
    missing = Counter()
    sources = {key: _observations(data, parent, key) for key in sorted({
        row["station"] for split in ("calibration", "test") for row in parent[split]})}
    for split in ("calibration", "test"):
        for row in parent[split]:
            station, at = row["station"], parse_time(row["at"])
            obs, clocks, source_row = sources[station]
            if source_row is None:
                missing[f"{split}: missing ASOS archive"] += 1
                continue
            wind = wind_at(obs, clocks, at)
            if wind is None:
                missing[f"{split}: latest ASOS wind calm, variable, absent or older than 90 min"] += 1
                continue
            inputs["stations"][station] = source_row
            grouped[row["hrrr_trace"][0]["raw"]].append((split, row, wind))
    for path, records in sorted(grouped.items()):
        body = (data / path).read_bytes()
        frame = records[0][1]["hrrr_trace"][0]
        if sha256(body).hexdigest() != frame["sha256"]:
            raise ValueError(f"Original HRRR field digest mismatch for {path}")
        locations = {}
        for index, (_, row, wind) in enumerate(records):
            lat, lon, *_ = expansion_weather.STATIONS[row["station"]]
            for method, direction in (("upwind", wind["direction_deg"]), ("downwind", wind["direction_deg"] + 180)):
                locations[f"{index}/{method}"] = (*destination(lat, lon, direction), None, None)
        cells = hrrr.decode_station_cells(body, parse_time(frame["issued_at"]), frame["lead_hours"], stations=locations)
        for index, (split, row, wind) in enumerate(records):
            probes = {method: cells[f"{index}/{method}"] for method in ("upwind", "downwind")}
            if any(cell["distance_km"] > 5 for cell in probes.values()):
                missing[f"{split}: projected location outside the native grid"] += 1
                continue
            inputs[split].append({**row, "wind": wind, "thermal_probes": probes})
    inputs["missing"] = dict(missing)
    return inputs


def _features(row, method):
    floor = row["all_observations_bound"]["lower_f"]
    near = row["hrrr_trace"][0]["temp_f"]
    features = {"station/hour": f"{row['station']}/{row['local_hour']}",
                "remaining_minus_floor": row["hrrr_remaining_f"] - floor,
                "remaining_minus_nbm": row["hrrr_remaining_f"] - row["expected_remaining_max_f"],
                "surface_wind_knots": row["wind"]["speed_kt"]}
    if method != "nbm":
        features["directed_gradient_f"] = row["thermal_probes"][method]["temp_f"] - near
    return features


def _evaluate(inputs, *, methods=METHODS, features=_features):
    training = inputs["calibration"]
    all_rows = training + inputs["test"]
    centers = defaultdict(dict)
    with threadpool_limits(limits=1):
        for method in methods:
            model = make_pipeline(DictVectorizer(sparse=False), Ridge(alpha=20.0))
            model.fit([features(row, method) for row in training],
                      [row["actual_f"] - research_hrrr_intraday._centers(row)["hrrr"] for row in training])
            predictions = model.predict([features(row, method) for row in all_rows])
            for row, correction in zip(all_rows, predictions, strict=True):
                centers[research_hrrr_intraday._key(row)][method] = max(
                    row["all_observations_bound"]["lower_f"],
                    research_hrrr_intraday._centers(row)["hrrr"] + float(correction),
                )
    return research_hrrr_intraday._evaluate(training, inputs["test"], inputs["payouts"],
                                            candidates=lambda row: centers[research_hrrr_intraday._key(row)])


def probes(row):
    return [{"label": f"{direction.capitalize()} cell on globe", "lat": cell["station_lat"],
             "lon": cell["station_lon"], "issued_at": row["hrrr_trace"][0]["issued_at"]}
            for direction, cell in sorted(row["thermal_probes"].items(), reverse=True)]


def run(data, *, source=None, frozen=None):
    if frozen is None:
        inputs = _freeze(data, source)
        frozen, digest = research_model.freeze_inputs(data, inputs)
    else:
        payload = gzip.decompress(frozen.read_bytes())
        digest, inputs = sha256(payload).hexdigest(), json.loads(payload)
    if inputs["version"] != VERSION or not inputs["calibration"] or not inputs["test"]:
        raise ValueError("Earlier and later HRRR/wind overlap is required")
    cohorts, reserve = _evaluate(inputs)
    metrics, comparisons, cases = [], [], []
    for cohort, rows in sorted(cohorts.items()):
        metrics.extend({**item, "bucket_decisions": sum(len(row["buckets"]) for row in rows)}
                       for item in research_model.metrics(rows, cohort, METHODS))
        comparisons.extend({"group": cohort, **research_model.date_bootstrap(rows, "nbm", name, metric)}
                           for name in ("upwind", "downwind") for metric in ("brier", "mae"))
        if cohort != "official":
            ordered = sorted(rows, key=lambda row: row["estimates"]["upwind"]["brier"] - row["estimates"]["nbm"]["brier"])
            for row in (ordered[0], ordered[-1]):
                bucket = next(b for b in row["buckets"] if b["payout"])
                index = row["buckets"].index(bucket)
                upwind = row["thermal_probes"]["upwind"]["temp_f"] - row["hrrr_trace"][0]["temp_f"]
                cases.append({"station": row["station"], "date": row["date"], "at": row["at"], "venue": cohort,
                              "instrument_id": bucket["instrument_id"], "title": f"{row['station']} at {row['local_hour']} LST",
                              "detail": f"Wind from {row['wind']['direction_deg']}° at {row['wind']['speed_kt']} kt. The HRRR 40 km upwind cell was {upwind:+.1f}°F relative to the station. The official high was {row['actual_f']:.0f}°F.",
                              "weather_archive_fields": row["thermal_probes"], "wind_report": row["wind"],
                              "hrrr_probes": probes(row),
                              "official_report": row["official_report"], "actual_payout": bucket,
                              "probabilities": {key: value["probabilities"][index] for key, value in row["estimates"].items()}})
    record = {"version": VERSION, "title": "HRRR upwind surface temperature and daily highs",
              "hypothesis": "A forecast gradient aligned with the latest reported surface wind improves on a local-only HRRR correction.",
              "target": "Published station maxima and exact native daily-high payout intervals.",
              "venues": sorted(inputs["payouts"]), "date_window": inputs["source_dates"],
              "metrics": metrics, "comparisons": comparisons, "cases": cases,
              "limits": ["Latest exact-site routine or special ASOS report uses a historical 10-minute receipt proxy. Fixed 5 kt minimum, 90-minute age and 40 km distance.",
                         "Both directional cells use the first retained valid hour in the same already-published original 3 km field. The downwind projection is a direction control.",
                         "Each fixed-alpha Ridge adds a correction to the same guarded HRRR center. Only earlier rows fit the correction and residual distributions. No venue quotes enter the weather models."],
              "provenance": {"frozen_inputs": str(frozen.relative_to(data)), "frozen_input_sha256": digest,
                             "parent_inputs_sha256": inputs["parent_sha256"], "source_record": inputs["source_record"],
                             "station_wind_archives": inputs["stations"], "missing": inputs["missing"],
                             "fit_rows": len(inputs["calibration"]), "guard_reserve": reserve,
                             "sklearn": sklearn_version, "ridge_alpha": 20.0,
                             "run_sources": research_model.run_sources(sys.modules[__name__], hrrr, research_hrrr_intraday)}}
    return research_model.write_record(data, record, {"cohorts": cohorts})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--source", type=Path)
    source.add_argument("--frozen", type=Path)
    args = parser.parse_args()
    print(run(args.data, source=args.source, frozen=args.frozen))


if __name__ == "__main__":
    main()
