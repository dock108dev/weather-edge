"""Test native GFS Wave guidance against exact archived NOAA buoy observations."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import date, datetime, time, timedelta, timezone
import gzip
from hashlib import sha256
import json
from pathlib import Path
import statistics
import sys
from urllib.request import urlopen

from . import grids, marine, research_gefs, research_model, waves
from .archive import date_range
from .history import _write
from .nbm import iso, utc


VERSION = "noaa-wave-buoy-v1"
UTC = timezone.utc
START, SPLIT, END = date(2026, 11, 2), date(2026, 11, 8), date(2026, 11, 14)
BUOYS = {
    "44008": "Northeast shelf", "44013": "Northeast shelf", "44017": "Northeast shelf", "44025": "Northeast shelf",
    "44065": "New York Harbor entrance", "44066": "Northeast shelf", "44009": "Mid-Atlantic shelf",
    "41025": "Mid-Atlantic shelf", "41013": "Mid-Atlantic shelf", "41001": "Outer Atlantic", "42001": "Gulf",
    "42002": "Gulf", "46042": "Pacific", "46026": "Pacific", "46012": "Pacific", "46014": "Pacific", "46029": "Pacific",
    "46041": "Pacific",
}
METHODS = {"early": "Native 18-hour lead", "latest": "Native 6-hour lead",
           "display": "Whole-degree 6-hour map cell", "adjusted": "Native 6-hour with earlier station bias"}


def _get_observations(data, station):
    path = data / "expansion" / "marine" / f"{station}-november-2026.txt"
    metadata = path.with_suffix(".json")
    source = f"{marine.ORIGIN}/data/realtime2/{station}.txt"
    if not metadata.is_file():
        if (data / "collector" / "paused").exists():
            raise TimeoutError("Collection stopped")
        with urlopen(source, timeout=18) as response:
            raw = response.read(4 * 2**20 + 1)
            last_modified = response.headers.get("Last-Modified")
        if len(raw) > 4 * 2**20:
            raise ValueError("NDBC observation archive exceeded the expected size")
        receipt = {"source": source, "archive": str(path.relative_to(data)), "sha256": sha256(raw).hexdigest(),
                   "received_at": iso(datetime.now(UTC)), "last_modified": last_modified}
        rows = marine.standard_wave_observations(raw)
        _write(path, raw)
        _write(metadata, research_model._json_bytes(receipt))
    else:
        raw = path.read_bytes()
        receipt = json.loads(metadata.read_bytes())
        if sha256(raw).hexdigest() != receipt["sha256"]:
            raise ValueError(f"Raw NDBC {station} file changed after collection")
        rows = marine.standard_wave_observations(raw)
    return rows, receipt


def _locations(data):
    with closing(marine._connect(data)) as db:
        records = db.execute("""WITH locations AS (
            SELECT station,latitude,longitude,
            ROW_NUMBER() OVER (PARTITION BY station ORDER BY measured DESC,available DESC) AS row
            FROM observations WHERE marine=1)
            SELECT station,latitude,longitude FROM locations WHERE row=1""")
        return {name: (lat, lon) for name, lat, lon in records if name in BUOYS}


def _fields(data, at):
    for lead in (6, 18):
        issue = at - timedelta(hours=lead)
        path = grids.frame_path(data / "waves", issue, lead)
        frame = waves._cached(path)
        if (frame is None or utc(frame["issued_at"]) != issue or utc(frame["valid_at"]) != at
                or not utc(frame["issued_at"]) <= utc(frame["available_at"]) <= at):
            return
        raw = (data / frame["raw"]).read_bytes()
        if sha256(raw).hexdigest() != frame["sha256"]:
            raise ValueError(f"Original GFS Wave digest mismatch at {at}")
        grid = waves.native_grid(raw, issue, lead)
        yield grid, {key: frame[key] for key in ("issued_at", "available_at", "valid_at", "source", "raw", "sha256")}


def inputs(data):
    locations = _locations(data)
    missing = Counter()
    sources, obs = {}, {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = {station: pool.submit(_get_observations, data, station) for station in locations}
        for station, job in jobs.items():
            try:
                obs[station], sources[station] = job.result()
            except (OSError, ValueError) as error:
                missing[f"{station}: {error}"] += 1
    rows = []
    for day in date_range(START, END):
        for hour in (6, 18):
            target = datetime.combine(day, time(hour), UTC)
            fields = list(_fields(data, target))
            if len(fields) != 2:
                missing["no paired issue was public by the target"] += 1
                continue
            (latest, latest_meta), (earlier, earlier_meta) = fields
            for station, values in obs.items():
                lat, lon = locations[station]
                nearest = min((at for at in values if abs(at - target) <= timedelta(minutes=20)),
                              key=lambda at: (abs(at - target), at), default=None)
                if nearest is None:
                    missing["no buoy wave measurement within 20 minutes"] += 1
                    continue
                forecasts = {"early": waves.sample_grid(earlier, lat, lon), "latest": waves.sample_grid(latest, lat, lon),
                             "display": waves.sample_grid(latest, lat, lon, 1)}
                for method, (estimate, _) in forecasts.items():
                    if estimate is None:
                        missing[f"dry cell for {method}"] += 1
                rows.append({"date": str(day), "target_at": iso(target), "measured_at": iso(nearest), "station": station,
                             "region": BUOYS[station], "lat": lat, "lon": lon, "truth_m": values[nearest],
                             "forecasts": {name: {"height_m": entry[0], "cell": entry[1]} for name, entry in forecasts.items()},
                             "frames": {"early": earlier_meta, "latest": latest_meta}, "measurement": sources[station]})
    return {"version": VERSION, "rows": rows, "sources": sources, "locations": locations, "missing": dict(missing),
            "unlocated": sorted(set(BUOYS) - locations.keys())}


def _scope(rows):
    return {"measurements": len(rows), "calendar_dates": len({row["date"] for row in rows}),
            "station_days": len({(row["date"], row["station"]) for row in rows}),
            "buoys": len({row["station"] for row in rows})}


def _case(row):
    forecasts = row["forecasts"]
    return {"title": f"NDBC {row['station']} · {row['target_at']}", "date": row["date"], "station": row["station"], "globe_station": "KNYC",
            "region": row["region"], "at": row["target_at"], "probe_lat": row["lat"], "probe_lon": row["lon"],
            "layers": "waves", "detail": (f"Buoy {row['station']} measured {row['truth_m']:.2f} m. The 6-hour native grid was "
                                           f"{forecasts['latest']['height_m']:.2f} m and the whole-degree display "
                                           f"was {forecasts['display']['height_m']:.2f} m."),
            "forecast": {"values": forecasts, "as_issued": row["frames"]},
            "evidence": {"NDBC_WVHT_m": row["truth_m"], "NDBC_measured_at": row["measured_at"], "source": row["measurement"]}}


def run(data, frozen=None):
    if frozen is None:
        evidence = inputs(data)
        frozen, digest = research_model.freeze_inputs(data, evidence)
    else:
        body = gzip.decompress(frozen.read_bytes())
        evidence, digest = json.loads(body), sha256(body).hexdigest()
    suitable = [row for row in evidence["rows"] if all(item["height_m"] is not None for item in row["forecasts"].values())]
    train = defaultdict(list)
    for row in suitable:
        if row["date"] < str(SPLIT):
            train[row["station"]].append(row["truth_m"] - row["forecasts"]["latest"]["height_m"])
    evaluated = [row for row in suitable if row["date"] >= str(SPLIT)]
    errors = defaultdict(list)
    contrasts = defaultdict(list)
    for row in evaluated:
        direct = row["forecasts"]["latest"]
        correction = statistics.mean(train[row["station"]]) if len(train[row["station"]]) >= 3 else 0
        row["forecasts"]["adjusted"] = {**direct, "height_m": max(0.0, direct["height_m"] + correction)}
        for method, forecast in row["forecasts"].items():
            errors[method].append(forecast["height_m"] - row["truth_m"])
        contrasts[row["date"]].append(abs(errors["latest"][-1]) - abs(errors["display"][-1]))
    metrics = [{"method": key, "label": label, **_scope(evaluated),
                "mae_m": statistics.mean(abs(value) for value in errors[key]),
                "bias_m": statistics.mean(errors[key])} for key, label in METHODS.items()]
    ranked = sorted(evaluated, key=lambda row: abs(row["forecasts"]["latest"]["height_m"] - row["forecasts"]["display"]["height_m"]), reverse=True)
    cases = [_case(row) for row in ranked[:3]]
    return research_model.write_record(data, {
        "version": VERSION, "title": "Checking GFS Wave forecasts against coastal buoys",
        "hypothesis": "The nearest wet value on the native quarter-degree grid may better reproduce the buoy reading than the whole-degree map sample. A newer issue and an earlier local bias estimate are evaluated on the same measurements.",
        "target": "Significant height of combined wind waves and swell", "venues": [],
        "date_window": {"earlier_bias": [str(START), str(SPLIT - timedelta(days=1))], "test": [str(SPLIT), str(END)]},
        "coverage": {"all_measured": _scope(evidence["rows"]), "paired_wet_cells": _scope(suitable), "held_out": _scope(evaluated),
                     "missing": evidence["missing"], "unlocated_buoys": evidence["unlocated"]},
        "metrics": metrics, "cases": cases,
        "uncertainty": {"native_minus_map_mae_date_interval_95": research_gefs._mean_interval(contrasts)},
        "limits": [
            "The forecast issue must have been available by its 06Z or 18Z valid time according to the original S3 Last-Modified. A buoy measurement may occur within twenty minutes on either side of that valid time.",
            "Buoy observations are downloaded as retrospective outcome labels from the public NDBC standard series. The study preserves measurement time and local archive receipt. The source does not supply the original row publication time.",
            "Grid locations use station coordinates from the local NDBC feed. Whole-degree cells use the same nearest sample as the globe. Dry cells are missing. Matched errors only cover sites and times with both native and display cells wet.",
            "The forecast represents total significant wave height on a grid cell. Coastal geometry and observation location affect comparability, particularly near the wet–dry boundary.",
        ],
        "provenance": {"frozen_input": str(frozen.relative_to(data)), "input_sha256": digest, "NOAA_Wave": waves.DOCS,
                       "sources": evidence["sources"], "source_sha256": research_model.run_sources(sys.modules[__name__], waves, marine),
                       "reproduce": f"python -m weather_edge.research_waves --inputs {frozen}"},
    }, {"held_out": evaluated})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--inputs", type=Path)
    args = parser.parse_args()
    print(run(args.data, args.inputs))


if __name__ == "__main__":
    main()
