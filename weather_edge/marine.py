"""Store native NOAA NDBC marine observations with their first local receipt clocks."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import math
from pathlib import Path
import re
import sqlite3

from .market import iso, parse_time

ORIGIN = "https://www.ndbc.noaa.gov"
SOURCE = f"{ORIGIN}/data/latest_obs/latest_obs.txt"
UTC = timezone.utc
FIELDS = {
    "WDIR": ("wind_from_deg", "degT"), "WSPD": ("wind_speed_mps", "m/s"),
    "GST": ("gust_mps", "m/s"), "WVHT": ("wave_height_m", "m"),
    "DPD": ("dominant_period_s", "sec"), "PRES": ("pressure_hpa", "hPa"),
    "ATMP": ("air_temperature_c", "degC"), "WTMP": ("water_temperature_c", "degC"),
    "DEWP": ("dewpoint_c", "degC"),
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS bodies (sha256 TEXT PRIMARY KEY, body BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS receipts (
    id INTEGER PRIMARY KEY, source TEXT NOT NULL, received TEXT NOT NULL, last_modified TEXT, etag TEXT,
    sha256 TEXT NOT NULL REFERENCES bodies(sha256)
);
CREATE TABLE IF NOT EXISTS observations (
    station TEXT NOT NULL, measured TEXT NOT NULL, signature TEXT NOT NULL, available TEXT NOT NULL,
    latitude REAL NOT NULL, longitude REAL NOT NULL, marine INTEGER NOT NULL, body TEXT NOT NULL,
    receipt_id INTEGER NOT NULL REFERENCES receipts(id),
    PRIMARY KEY(station,measured,signature)
);
CREATE INDEX IF NOT EXISTS marine_clock ON observations(marine,available,measured);
"""


def instant(value):
    return parse_time(value).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _connect(data):
    root = Path(data)
    root.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(root / "marine.sqlite3", timeout=12)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(SCHEMA)
        return db
    except BaseException:
        db.close()
        raise


def standard_wave_observations(raw):
    lines = raw.decode("ascii").splitlines()
    if len(lines) < 3:
        raise ValueError("Incomplete NDBC standard meteorological series")
    fields = lines[0].removeprefix("#").split()
    units = lines[1].removeprefix("#").split()
    height = fields.index("WVHT")
    if fields[:5] != ["YY", "MM", "DD", "hh", "mm"] or units[height] != "m":
        raise ValueError("Unexpected NDBC standard meteorological wave schema")
    values = {}
    for line in lines[2:]:
        cells = line.split()
        if len(cells) <= height or cells[height] == "MM":
            continue
        try:
            stamp = datetime(*(int(value) for value in cells[:5]), tzinfo=UTC)
            value = float(cells[height])
        except (ValueError, OverflowError):
            continue
        if 0 <= value <= 60:
            values.setdefault(stamp, value)
    return values


def wave_archive_measurement(data, station, valid_at):
    if not re.fullmatch(r"[A-Z0-9]{4,7}", station):
        raise ValueError("Choose a valid NDBC station identity")
    at = parse_time(valid_at)
    nearby = []
    for file in (Path(data) / "expansion" / "marine").glob(f"{station}-*.json"):
        receipt = json.loads(file.read_bytes())
        raw = file.with_suffix(".txt").read_bytes()
        if sha256(raw).hexdigest() != receipt["sha256"]:
            raise ValueError("NDBC archive original digest mismatch")
        readings = standard_wave_observations(raw)
        nearest = min((stamp for stamp in readings if abs(stamp - at) <= timedelta(minutes=20)),
                      key=lambda stamp: (abs(stamp - at), stamp), default=None)
        if nearest is not None:
            nearby.append((nearest, receipt, readings[nearest]))
    if not nearby:
        return None
    recorded, receipt, height = min(nearby, key=lambda row: (abs(row[0] - at), row[1]["received_at"]))
    return {"station": station, "measured_at": iso(recorded), "archive_received_at": receipt["received_at"],
            "height_m": height, "source": receipt["source"], "raw_sha256": receipt["sha256"]}


def parse_snapshot(raw, received):
    rows = raw.decode("ascii").splitlines()
    if len(rows) < 3 or not rows[0].startswith("#STN"):
        raise ValueError("Unexpected NDBC latest observations header")
    names = rows[0].lstrip("#").split()
    units = rows[1].lstrip("#").split()
    columns = {name: pos for pos, name in enumerate(names)}
    if len(names) != len(units) or names[:8] != ["STN", "LAT", "LON", "YYYY", "MM", "DD", "hh", "mm"]:
        raise ValueError("Unexpected NDBC time or location fields")
    if any(name not in columns or units[columns[name]] != unit for name, (_, unit) in FIELDS.items()):
        raise ValueError("Unexpected NDBC observation units")
    received = parse_time(received)
    seen = set()
    result = []
    for line in rows[2:]:
        parts = line.split()
        if not parts or parts[0].startswith("#"):
            continue
        if len(parts) != len(names) or not re.fullmatch(r"[0-9A-Za-z]{3,12}", parts[0]):
            continue
        station = parts[0]
        try:
            lat, lon = float(parts[1]), float(parts[2])
            observed = datetime(*(int(value) for value in parts[3:8]), tzinfo=UTC)
            if not -90 <= lat <= 90 or not -180 <= lon <= 180 or observed > received:
                continue
            values = {}
            for name, (target, _) in FIELDS.items():
                cell = parts[columns[name]]
                if cell != "MM":
                    val = float(cell)
                    if math.isfinite(val):
                        values[target] = val
        except (TypeError, ValueError):
            continue
        if not values or (station, observed) in seen:
            continue
        seen.add((station, observed))
        result.append({"station": station, "lat": lat, "lon": lon, "measured_at": instant(observed), **values})
    return result


def collect(data, get):
    reply = get(SOURCE)
    rows = parse_snapshot(reply.body, reply.at)
    if not rows:
        raise ValueError("No valid NOAA NDBC observations")
    digest = sha256(reply.body).hexdigest()
    known = instant(reply.at)
    with closing(_connect(data)) as db, db:
        db.execute("INSERT OR IGNORE INTO bodies VALUES (?,?)", (digest, reply.body))
        receipt = db.execute(
            "INSERT INTO receipts(source,received,last_modified,etag,sha256) VALUES(?,?,?,?,?)",
            (SOURCE, known, reply.headers.get("last-modified"), reply.headers.get("etag"), digest),
        ).lastrowid
        records = []
        for row in rows:
            content = json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False)
            marine = int("water_temperature_c" in row or "wave_height_m" in row)
            records.append((row["station"], row["measured_at"], sha256(content.encode()).hexdigest(),
                            known, row["lat"], row["lon"], marine, content, receipt))
        inserted = db.executemany("INSERT OR IGNORE INTO observations VALUES(?,?,?,?,?,?,?,?,?)", records).rowcount
    return {"records": len(rows), "inserted": inserted, "latest": max(row["measured_at"] for row in rows), "sha256": digest}


def snapshot(data, at):
    """Return the latest identified marine report known by this local receipt clock."""
    if not (Path(data) / "marine.sqlite3").is_file():
        return []
    lower = instant(parse_time(at) - timedelta(hours=6))
    with closing(_connect(data)) as db:
        rows = db.execute(
            """WITH eligible AS (
                   SELECT o.station,o.available,o.body,o.receipt_id,
                     ROW_NUMBER() OVER (PARTITION BY station ORDER BY measured DESC,available DESC,receipt_id DESC) AS most_recent
                   FROM observations o WHERE marine=1 AND available<=? AND measured>=?
               ) SELECT body,available,r.last_modified,r.sha256
                 FROM eligible e JOIN receipts r ON e.receipt_id=r.id WHERE most_recent=1 ORDER BY e.station""",
            (instant(at), lower),
        ).fetchall()
    return [{**json.loads(body), "available_at": available, "source_modified": published, "source_sha256": digest}
            for body, available, published, digest in rows]
