"""Retain Kalshi's public minute weather indices and calibration timelines."""

import argparse
from datetime import date, datetime, time as day_time, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path

from .market import decimal, iso, parse_time
from .venue_archive import KALSHI, connect, save_metadata, save_receipt, set_progress
from .venue_contracts import INDEX_CITIES
from .venue_sources import CollectionPaused, EXCEPTIONS, PublicGET, _rows, _scan_error

INDEX_PATH = "/live_data/weather/"
ACTIVE_CITIES = tuple(data[0] for data in INDEX_CITIES.values())
CITIES = (*ACTIVE_CITIES, "dfw", "houston", "phl-delaware-valley", "puget-sound", "sf-bay", "greater-boston",
          "southeast-michigan", "kansas-city", "minneapolis-st-paul")
CANONICAL = {"normal", "degraded"}
DOCS = "https://docs.kalshi.com/api-reference/live-data/get-weather-index"
CALIBRATION_DOCS = "https://docs.kalshi.com/api-reference/live-data/get-weather-index-calibrations"


def index_time(raw):
    if type(raw) is not int:
        raise ValueError("Weather Index clock must be Unix milliseconds")
    return datetime.fromtimestamp(raw / 1000, timezone.utc)


def _milliseconds(stamp):
    return int(parse_time(stamp).timestamp() * 1000)


def _rows_by_time(payload, city, start, end):
    if payload["city"] != city or payload["units"] != "fahrenheit":
        raise ValueError("Weather Index city or units mismatch")
    seen = set()
    for row in _rows(payload, "timeseries"):
        timestamp, status = row["t"], row["status"]
        if timestamp % 60000 or not start <= timestamp <= end or timestamp in seen:
            raise ValueError("Invalid weather index UTC minute")
        seen.add(timestamp)
        temperature = row.get("v")
        if temperature is not None:
            value = decimal(temperature)
            if status not in CANONICAL or not -150 <= value <= 150:
                raise ValueError("Invalid temperature or weather index status")
            temperature = str(value)
        elif status in CANONICAL:
            raise ValueError("Canonical Weather Index point has no value")
        version = json.dumps(row, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        digest = sha256(version.encode()).hexdigest()
        yield (city, iso(index_time(timestamp)), status, temperature, row.get("contributors"), digest, version)


def capture(data, city, start, end, *, detailed=True, get=None, scope=None):
    if city not in CITIES:
        raise ValueError("Unexpected Weather Index city")
    get = get or PublicGET(data)
    first, last = _milliseconds(start), _milliseconds(end)
    if last < first:
        raise ValueError("Weather Index start is after end")
    scope = scope or f"{city}/{iso(start)}"
    reply = None
    try:
        reply = get(KALSHI, INDEX_PATH + city, {"from": first, "to": last, "detailed": str(detailed).lower()})
        payload = reply.json()
        rows = list(_rows_by_time(payload, city, first, last))
    except EXCEPTIONS as error:
        _scan_error(data, KALSHI, scope, "weather-index", "", 0, 0, error, reply)
        return 0
    with connect(data) as db, db:
        receipt_id = save_receipt(db, KALSHI, "weather-index", reply)
        db.executemany(
            """INSERT OR IGNORE INTO weather_indices(city,at,status,temperature_f,contributors,point_key,receipt_id,body)
            VALUES (?,?,?,?,?,?,?,?)""",
            [(*point[:-1], receipt_id, point[-1]) for point in rows],
        )
        set_progress(db, KALSHI, scope, "weather-index", "", 1, len(rows), "complete", reply.received_at)
    return len(rows)


def capture_calibrations(data, city, get=None):
    if city not in CITIES:
        raise ValueError("Unexpected Weather Index city")
    get = get or PublicGET(data)
    reply = None
    try:
        reply = get(KALSHI, INDEX_PATH + city + "/calibrations")
        payload = reply.json()
        records = _rows(payload, "calibrations")
        if payload["city"] != city or payload["units"] != "celsius":
            raise ValueError("Weather Index calibration identity mismatch")
        dates = [index_time(row["effective_at_ms"]) for row in records]
        if dates != sorted(set(dates)):
            raise ValueError("Nonmonotonic Weather Index calibrations")
    except EXCEPTIONS as error:
        _scan_error(data, KALSHI, city, "weather-index-configuration", "", 0, 0, error, reply)
        return 0
    with connect(data) as db, db:
        receipt_id = save_receipt(db, KALSHI, "weather-index-configuration", reply)
        save_metadata(db, KALSHI, "weather-index-configuration:" + city, records, receipt_id)
        set_progress(db, KALSHI, city, "weather-index-configuration", "", 1, len(records), "complete", reply.received_at)
    return len(records)


def calibrations(data, city):
    with connect(data) as db:
        row = db.execute(
            """SELECT metadata.value,metadata.receipt_id,receipts.source,receipts.received_at
            FROM metadata JOIN receipts ON receipts.id=metadata.receipt_id WHERE metadata.venue=? AND key=?""",
            (KALSHI, "weather-index-configuration:" + city),
        ).fetchone()
    return {**dict(row), "value": json.loads(row["value"])} if row else None


def points(data, city, *, start, end, as_of=None):
    with connect(data) as db:
        statement = """SELECT p.at,p.status,p.temperature_f,p.contributors,p.body,p.receipt_id,
            r.received_at,r.requested_at,r.source FROM weather_indices AS p JOIN receipts AS r
            ON p.receipt_id=r.id WHERE p.city=? AND p.at>=? AND p.at<=?"""
        values = [city, iso(start), iso(end)]
        if as_of is not None:
            statement += " AND r.received_at<=?"
            values.append(iso(as_of))
        results = db.execute(statement + " ORDER BY p.at,r.received_at,p.receipt_id", values)
        return [{**dict(item), "body": json.loads(item["body"])} for item in results]


def refresh_recent(data, cities=ACTIVE_CITIES):
    get = PublicGET(data)
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=16)
    try:
        return sum(capture(data, city, start, end, get=get, scope=city + "/recent") for city in cities)
    except CollectionPaused:
        return 0


def backfill(data, start, end, cities, *, detailed=True):
    get = PublicGET(data)
    found = {}
    for city in cities:
        capture_calibrations(data, city, get)
        found[city] = 0
        for n in range((end - start).days + 1):
            at = datetime.combine(start + timedelta(days=n), day_time.min, timezone.utc)
            found[city] += capture(data, city, at, at + timedelta(hours=23, minutes=59),
                                   get=get, detailed=detailed)
    return found


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--from", dest="start", type=date.fromisoformat)
    parser.add_argument("--through", dest="end", type=date.fromisoformat)
    parser.add_argument("--city", choices=CITIES, action="append")
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args(argv)
    cities = args.city or ACTIVE_CITIES
    try:
        if args.start and args.end:
            print(json.dumps(backfill(args.data, args.start, args.end, cities, detailed=not args.compact), indent=2))
        elif args.start or args.end:
            parser.error("Set both --from and --through to backfill")
        else:
            print(f"{refresh_recent(args.data, cities)} minute records returned")
    except CollectionPaused:
        print("Collection paused. The durable index is available.")


if __name__ == "__main__":
    main()
