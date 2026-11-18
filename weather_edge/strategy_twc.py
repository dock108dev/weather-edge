"""Archive the anonymous daily climate readout used by Weather.com's Kalshi page."""

import argparse
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener
import zlib

from . import collector
from .capture import ImmutableOrigin, USER_AGENT
from .market import iso, parse_time
from .paper_runs import DEFAULT_DATA


SITE = "https://weather.com/kalshi"
SOURCE = SITE + "/api/climate/primary?date="
FILENAME = "twc-climate.sqlite3"
MAX_BODY = 512_000
FETCH_CADENCE_SECONDS = 1
SCHEMA = """
CREATE TABLE IF NOT EXISTS receipt (
  id INTEGER PRIMARY KEY, day TEXT NOT NULL, requested_at TEXT NOT NULL, received_at TEXT NOT NULL,
  source TEXT NOT NULL, status INTEGER NOT NULL, headers TEXT NOT NULL, body_sha256 TEXT NOT NULL,
  original_body BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS receipt_by_day ON receipt(day,received_at DESC,id DESC);
CREATE TABLE IF NOT EXISTS report (
  receipt_id INTEGER NOT NULL REFERENCES receipt(id), station TEXT NOT NULL, label_date TEXT NOT NULL,
  provider_status TEXT NOT NULL, provider_issued_at TEXT, city TEXT NOT NULL, timezone TEXT NOT NULL,
  source_class TEXT, temperature_high_f REAL, temperature_low_f REAL,
  precipitation_in TEXT, snowfall_in TEXT, hourly_average_f REAL, hours_reported INTEGER,
  native TEXT NOT NULL, PRIMARY KEY(receipt_id,station)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS report_by_station_day ON report(station,label_date,receipt_id DESC);
"""


def json_value(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def clock_key(value):
    return parse_time(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


@contextmanager
def connection(data, *, write=False):
    path = Path(data) / FILENAME
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path if write else path.resolve().as_uri() + "?mode=ro", uri=not write, timeout=15)
    db.row_factory = sqlite3.Row
    try:
        if write and db.execute("PRAGMA user_version").fetchone()[0] == 0:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA foreign_keys=ON")
            db.executescript(SCHEMA)
            db.execute("PRAGMA user_version=1")
        yield db
    finally:
        db.close()


def fetch(day, opener):
    key = str(day)
    url = SOURCE + key
    request = Request(url, method="GET", headers={"User-Agent": USER_AGENT, "Accept": "application/json",
                                                  "Cache-Control": "no-cache", "Accept-Encoding": "identity"})
    began = datetime.now(timezone.utc)
    with opener.open(request, timeout=10) as response:
        body = response.read(MAX_BODY + 1)
        if len(body) > MAX_BODY:
            raise ValueError("Weather Company climate response is oversized")
        received = datetime.now(timezone.utc)
        keys = {"age", "cache-control", "date", "cf-cache-status", "last-modified", "content-type"}
        headers = {key.lower(): value for key, value in response.headers.items() if key.lower() in keys}
        return {"day": key, "requested_at": clock_key(began), "received_at": clock_key(received), "source": url,
                "status": response.status, "headers": headers, "body": body}


def quality(value):
    if value is None:
        return None
    if isinstance(value, str) and value.strip().upper() in {"T", "TRACE"}:
        return "T"
    if type(value) not in (int, float, str):
        return None
    return str(value)


def normalize(payload, day):
    if (not isinstance(payload, dict) or payload.get("date") != day or not isinstance(payload.get("results"), list)):
        raise ValueError("Weather Company response did not identify the requested date")
    records = []
    seen = set()
    for row in payload["results"]:
        station = row.get("station") or {}
        station_id = station.get("icao") or ""
        if not re.fullmatch(r"[A-Z][A-Z0-9]{3}", station_id) or station_id in seen:
            raise ValueError("Missing or duplicate weather station")
        seen.add(station_id)
        details = row.get("data")
        if details and details.get("reportDate") != day:
            raise ValueError("Station report identified another date")
        issued = (details or {}).get("issueTime")
        try:
            issued_at = iso(issued) if issued else None
        except ValueError:
            issued_at = None
        values = details or {}
        status = row.get("status")
        source_class = "official CLI or CF6" if status in {"official", "revised"} else "provider preview"
        records.append((
            station_id, day, status, issued_at, station["city"], station["timezone"], source_class,
            values.get("maxTemp"), values.get("minTemp"), quality(values.get("precipitation")),
            quality(values.get("snowfall")), row.get("avgTemp"), row.get("avgHours"), json_value(row),
        ))
    return records


def store(data, receipt):
    body = receipt["body"]
    rows = normalize(json.loads(body), receipt["day"])
    with connection(data, write=True) as db, db:
        identity = db.execute(
            """INSERT INTO receipt(day,requested_at,received_at,source,status,headers,body_sha256,original_body)
            VALUES(?,?,?,?,?,?,?,?)""",
            (receipt["day"], receipt["requested_at"], receipt["received_at"], receipt["source"], receipt["status"],
             json_value(receipt["headers"]), sha256(body).hexdigest(), zlib.compress(body)),
        ).lastrowid
        db.executemany("INSERT INTO report VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [(identity, *row) for row in rows])
    return len(rows)


def reports(data=DEFAULT_DATA, *, station=None, day=None, start=None, end=None, as_of=None):
    if not (Path(data) / FILENAME).exists():
        return []
    where = ["1=1"]
    params = []
    for field, op, value in (("p.station", "=", station), ("p.label_date", "=", day),
                             ("p.label_date", ">=", start), ("p.label_date", "<=", end),
                             ("r.received_at", "<=", clock_key(as_of) if as_of else None)):
        if value:
            where.append(field + op + "?")
            params.append(value)
    where_clause = " AND ".join(where)
    with connection(data) as db:
        result = db.execute(
            f"""SELECT * FROM (
                SELECT p.*,r.requested_at,r.received_at AS available_at,r.source AS source_url,r.body_sha256,
                       row_number() OVER (PARTITION BY p.station,p.label_date ORDER BY r.received_at DESC,r.id DESC) AS rank
                FROM report p JOIN receipt r ON r.id=p.receipt_id WHERE {where_clause}
            ) WHERE rank=1 ORDER BY label_date DESC,station""", params,
        )
        return [{**dict(row), "native": json.loads(row["native"])} for row in result]


def raw_receipt(data, receipt_id):
    with connection(data) as db:
        hit = db.execute("SELECT original_body FROM receipt WHERE id=?", (receipt_id,)).fetchone()
    return zlib.decompress(hit[0]) if hit else None


def backfill(data, start, end, *, refresh=False):
    begin, finish = date.fromisoformat(start), date.fromisoformat(end)
    if begin > finish:
        raise ValueError("Expected the start date to precede the end date")
    by_day = {row["label_date"] for row in reports(data, start=start, end=end)}
    opener = build_opener(ImmutableOrigin())
    failures, fetched = {}, {}
    next_request = time.monotonic()
    for offset in range((finish - begin).days + 1):
        day = begin + timedelta(days=offset)
        key = str(day)
        if not refresh and key in by_day:
            continue
        while not collector.paused(data) and (wait := next_request - time.monotonic()) > 0:
            time.sleep(min(wait, 0.25))
        if collector.paused(data):
            failures[key] = "Collection stopped locally"
            break
        next_request = time.monotonic() + FETCH_CADENCE_SECONDS
        try:
            receipt = fetch(day, opener)
            fetched[key] = store(data, receipt)
        except (HTTPError, URLError, OSError, ValueError, KeyError, TypeError) as error:
            failures[key] = str(error)
            if isinstance(error, HTTPError) and error.code == 429:
                delay = error.headers.get("Retry-After", "")
                next_request = time.monotonic() + (max(10, min(60, int(delay))) if delay.isdigit() else 10)
    return {"from": start, "through": end, "days_fetched": len(fetched),
            "station_rows": sum(fetched.values()), "rows_by_day": fetched, "issues": failures}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", type=Path, default=DEFAULT_DATA)
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--query", action="store_true")
    ap.add_argument("--station")
    ap.add_argument("--as-of")
    args = ap.parse_args()
    if args.query:
        value = reports(args.data, station=args.station, start=args.start, end=args.end, as_of=args.as_of)
    elif args.start and args.end:
        value = backfill(args.data, args.start, args.end, refresh=args.refresh)
    else:
        ap.error("Provide a start and an end date for a backfill, or pass --query.")
    print(json.dumps(value, indent=2))


if __name__ == "__main__":
    main()
