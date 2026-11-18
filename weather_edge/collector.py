"""Collect public weather independently of the browser and exchange catalog."""
from __future__ import annotations

from argparse import ArgumentParser
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing, contextmanager
from contextvars import ContextVar
import csv
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from fcntl import LOCK_EX, LOCK_NB, LOCK_UN, flock
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import sys
from tempfile import NamedTemporaryFile
from threading import Event
import time
import traceback
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen

from . import capture, marine
from .capture import ReadOnlyHTTP, _awc_obs, _nws_obs
from .expansion_weather import STATIONS as STATION_GEOGRAPHY
from .market import DISCOVERY, OPEN, STATIONS, iso, parse_event, parse_time
from .nbm import STATION_RANGES, climate_window, forecast_for_day, nbm_url, parse_nbh, parse_nbs


DATA = Path(__file__).resolve().parents[1] / ".data"
OBS_INTERVAL = 90
IEM_INTERVAL = 10 * 60
MADIS_DENSE_INTERVAL = 15 * 60
NBM_INTERVAL = 10 * 60
CLI_INTERVAL = 15 * 60
GFS_INTERVAL = 20 * 60
DISCOVERY_INTERVAL = 5 * 60
BOOK_INTERVAL = 15
PAYOUT_INTERVAL = 5 * 60
ACTIVE_MARKET_LIMIT = 4
NBM_RANGES = {
    **STATION_RANGES,
    "KMDW": {"nbh": "bytes=16000000-17099999", "nbs": "bytes=16500000-17599999"},
    "KPHL": {"nbh": "bytes=17700000-18699999", "nbs": "bytes=18100000-19299999"},
    "KSFO": {"nbh": "bytes=18800000-19999999", "nbs": "bytes=19400000-20499999"},
}
STATION_NAMES = {
    "KNYC": "Central Park", "KMDW": "Chicago Midway", "KPHL": "Philadelphia International Airport",
    "KMIA": "Miami International Airport", "KSFO": "San Francisco International Airport",
    "KLAX": "Los Angeles International Airport",
    "KAUS": "Austin Bergstrom", "KATL": "Atlanta Hartsfield–Jackson", "KBOS": "Boston Logan", "KDCA": "Washington Reagan National",
    "KDEN": "Denver International", "KDFW": "Dallas Fort Worth", "KEWR": "Newark Liberty", "KLAS": "Las Vegas Harry Reid",
    "KMSY": "New Orleans International", "KORD": "Chicago O’Hare", "KSEA": "Seattle–Tacoma", "KTTN": "Trenton Mercer",
    "KBUF": "Buffalo Niagara", "KCVG": "Cincinnati / Northern Kentucky", "KPDX": "Portland International", "KPVD": "Rhode Island T. F. Green",
}
WEATHER_STATIONS = {station: STATION_GEOGRAPHY[station] for station in STATION_NAMES}
EXPECTED_SOURCE_ERRORS = (KeyError, TypeError, ValueError, OSError, sqlite3.Error)
ATTEMPT = ContextVar("collector_attempt", default=None)


def station_place(station):
    if station == "KNYC":
        return "New York City · Central Park"
    return STATION_NAMES.get(station, station)


def now():
    return datetime.now(timezone.utc)


def canonical(value):
    return json.dumps(value, separators=(",", ":"), sort_keys=True, allow_nan=False)


def connection(data):
    data = Path(data)
    data.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(data / "weather.sqlite3", timeout=15)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""
        CREATE TABLE IF NOT EXISTS weather (
            station TEXT NOT NULL, day TEXT NOT NULL, family TEXT NOT NULL, source TEXT NOT NULL,
            stamp TEXT NOT NULL, seen TEXT NOT NULL, signature TEXT NOT NULL, payload TEXT NOT NULL,
            PRIMARY KEY (station, day, family, source, stamp, seen)
        ) WITHOUT ROWID
    """)
    db.execute("CREATE INDEX IF NOT EXISTS weather_by_day ON weather(day, station, family)")
    db.execute("""
        CREATE TABLE IF NOT EXISTS source_status (
            name TEXT PRIMARY KEY, last_attempt TEXT NOT NULL, last_success TEXT,
            source_at TEXT, latest_rows INTEGER, total_new INTEGER NOT NULL,
            issue TEXT, failures INTEGER NOT NULL
        ) WITHOUT ROWID
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS settlement_checks (
            slug TEXT PRIMARY KEY, checked_at TEXT NOT NULL
        ) WITHOUT ROWID
    """)
    return db


def station_day(station, stamp):
    return (parse_time(stamp) + timedelta(hours=WEATHER_STATIONS[station][3])).date().isoformat()


def save_weather(data, rows):
    db = connection(data)
    added = 0
    try:
        with db:
            for station, day, family, source, stamp, row in rows:
                seen = parse_time(row["available_at"]).isoformat(timespec="microseconds").replace("+00:00", "Z")
                change = {key: value for key, value in row.items() if key not in {"available_at", "source_last_modified", "source_url"}}
                signature = sha256(canonical(change).encode()).hexdigest()
                key = (station, day, family, source, iso(stamp))
                final = db.execute(
                    "SELECT signature, seen FROM weather WHERE station=? AND day=? AND family=? AND source=? AND stamp=? ORDER BY seen DESC LIMIT 1",
                    key,
                ).fetchone()
                if final and (final[0] == signature or parse_time(final[1]) > parse_time(seen)):
                    continue
                added += db.execute("INSERT OR IGNORE INTO weather VALUES (?,?,?,?,?,?,?,?)",
                                    (*key, seen, signature, canonical(row))).rowcount
    finally:
        db.close()
    return added


def _status_table(db):
    db.execute("CREATE TABLE IF NOT EXISTS source_health (name TEXT PRIMARY KEY, body TEXT NOT NULL)")


def _write_source_status(data, source, *, upstream_at=None, count=None, new=0, error=None,
                         succeeded=None, outcome=None, received_at=None, published_at=None, batch=None):
    completed = now()
    started = ATTEMPT.get() or completed
    t = iso(completed)
    succeeded = (error is None) if succeeded is None else succeeded
    db = connection(data)
    try:
        with db:
            _status_table(db)
            db.execute("""
                INSERT INTO source_status VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(name) DO UPDATE SET
                    last_attempt=excluded.last_attempt,
                    last_success=coalesce(excluded.last_success,source_status.last_success),
                    source_at=CASE WHEN excluded.source_at IS NULL THEN source_status.source_at
                        WHEN source_status.source_at IS NULL OR replace(excluded.source_at,'Z','')
                            >=replace(source_status.source_at,'Z','') THEN excluded.source_at
                        ELSE source_status.source_at END,
                    latest_rows=coalesce(excluded.latest_rows,source_status.latest_rows),
                    total_new=source_status.total_new+excluded.total_new,
                    issue=excluded.issue, failures=CASE WHEN excluded.last_success IS NOT NULL
                        OR excluded.issue IS NULL
                        THEN 0 ELSE source_status.failures+1 END
            """, (source, t, t if succeeded else None, upstream_at, count, new, error,
                  int(bool(error) and not succeeded)))
            prior = db.execute("SELECT body FROM source_health WHERE name=?", (source,)).fetchone()
            health = json.loads(prior[0]) if prior else {}
            health.update(attempt_started_at=iso(started), completed_at=t,
                          duration_seconds=max(0, (completed - started).total_seconds()),
                          outcome=outcome or ("partial" if error and succeeded else "error" if error else
                                              "published" if new or published_at else "unchanged"))
            if error:
                health.update(last_error=error, last_error_at=t)
            if received_at:
                health["last_receipt_at"] = received_at
                if succeeded:
                    health["last_successful_receipt_at"] = received_at
            if new or published_at:
                health["published_at"] = published_at or t
                health["revision"] = health.get("revision", 0) + 1
                if succeeded:
                    health["last_successful_publication_at"] = health["published_at"]
            if batch is not None:
                health["batch"] = batch
            db.execute("INSERT OR REPLACE INTO source_health VALUES (?,?)", (source, canonical(health)))
    finally:
        db.close()


def _local_diagnostic(data, source, exc, *, issue=None):
    diagnostic = {"at": iso(now()), "source": source, "issue": issue,
                  "error": capture._error_message(exc), "traceback": traceback.format_exc()}
    try:
        with (collect_path(data) / "status-errors.jsonl").open("a") as log:
            log.write(canonical(diagnostic) + "\n")
    except OSError:
        print(canonical(diagnostic), file=sys.stderr, flush=True)


def mark_source(data, source, **result):
    try:
        _write_source_status(data, source, **result)
    except Exception as exc:
        _local_diagnostic(data, source, exc, issue=result.get("error"))


def _run_task(task, data, source):
    if paused(data):
        return
    token = ATTEMPT.set(now())
    try:
        task(data)
        if source.startswith("Task issue · ") and source_status(data).get(source, {}).get("issue"):
            mark_source(data, source, outcome="unchanged")
    except Exception as exc:
        try:
            mark_source(data, source, error=capture._error_message(exc))
        except Exception as write_error:
            _local_diagnostic(data, source, write_error, issue=capture._error_message(exc))
    finally:
        ATTEMPT.reset(token)


@contextmanager
def _source_lane(data, source):
    try:
        yield
    except Exception as exc:
        mark_source(data, source, error=capture._error_message(exc))


def refresh_observations(data):
    get = tracked_http(data, 30)
    previous = source_status(data)
    with ThreadPoolExecutor(max_workers=6) as pool:
        jobs = {
            pool.submit(_nws_obs, get, station, limit=12 if f"NWS {station}" in previous else 72): (f"NWS {station}", station)
            for station in STATIONS
        }
        jobs[pool.submit(_awc_obs, get, tuple(WEATHER_STATIONS),
                         hours=4 if "Aviation Weather Center" in previous else 24)] = ("Aviation Weather Center", None)
        for job in as_completed(jobs):
            source, station = jobs[job]
            try:
                result = job.result()
                by_station = {station: result} if station else result
                rows = [(name, station_day(name, row["at"]), "observation", row["source"], row["at"], row)
                        for name, readings in by_station.items() for row in readings]
                added = save_weather(data, rows)
                latest = max((row[4] for row in rows), default=None)
                if not rows:
                    mark_source(data, source, count=0, error="No station observations returned")
                else:
                    mark_source(data, source, upstream_at=latest, count=len(rows), new=added,
                                received_at=max(row[5]["available_at"] for row in rows))
            except Exception as exc:
                mark_source(data, source, error=capture._error_message(exc))


def _materialize_nbm(cards):
    for station, cycles in cards.items():
        offset = STATIONS[station][3]
        for cycle in cycles:
            relevant = {station_day(station, item["valid_at"]) for item in cycle.get("nbh", {}).get("hourly", [])}
            relevant.update(station_day(station, item["valid_at"]) for item in cycle.get("nbs", {}).get("daily_maxima", []))
            relevant.update(station_day(station, item["valid_at"]) for item in cycle.get("nbs", {}).get("daily_minima", []))
            for day in relevant:
                row = forecast_for_day(cycle, day, offset)
                if row is not None:
                    stamps = [entry.get("source_last_modified") for entry in cycle.values()]
                    if stamps and all(stamps):
                        row["source_published_at"] = iso(max(parsedate_to_datetime(stamp) for stamp in stamps))
                    yield station, day, "forecast", row["high_source"], row["issued_at"], row


def _fetch_bulletin(get, issue, stations):
    result = {station: {} for station in stations}
    for product, parser in (("nbh", parse_nbh), ("nbs", parse_nbs)):
        url = nbm_url(issue.date(), issue.hour, product)
        by_range = {}
        for station in stations:
            by_range.setdefault(NBM_RANGES[station][product], []).append(station)
        whole = None
        for region, subset in by_range.items():
            reply = whole or get(url, capture.FORECAST_CAP, {"Range": region})
            for station in subset:
                try:
                    card = parser(reply.body, station)
                except (ValueError, KeyError):
                    whole = whole or get(url, capture.FORECAST_CAP)
                    reply = whole
                    try:
                        card = parser(reply.body, station)
                    except (ValueError, KeyError):
                        continue
                if parse_time(card["issued_at"]) != issue or reply.at < issue:
                    raise ValueError("NOAA station issue time does not match requested cycle")
                result[station][product] = {
                    **card, "available_at": iso(reply.at), "source": url,
                    "source_last_modified": reply.headers.get("last-modified"),
                    "availability_basis": "local_receipt",
                }
    return result


def refresh_nbm(data):
    get = tracked_http(data, 55)
    try:
        current = now()
        issue = (current - timedelta(minutes=50)).replace(minute=0, second=0, microsecond=0)
        prior = source_status(data).get("NOAA NBM", {}).get("source_at")
        for hours in range(3):
            cycle = issue - timedelta(hours=hours)
            if prior is not None and parse_time(prior) >= cycle:
                mark_source(data, "NOAA NBM", outcome="unchanged")
                return
            try:
                cards = _fetch_bulletin(get, cycle, STATIONS)
            except HTTPError as exc:
                if exc.code in {403, 404}:
                    continue
                raise
            rows = list(_materialize_nbm({station: [pair] for station, pair in cards.items()}))
            if not rows:
                raise ValueError("NOAA bulletin has no supported station forecast")
            added = save_weather(data, rows)
            absent = [station for station, pair in cards.items() if len(pair) < 2]
            issue = "Incomplete NBM guidance for " + ", ".join(absent) if absent else None
            mark_source(data, "NOAA NBM", upstream_at=iso(cycle), count=len(rows), new=added, error=issue, succeeded=True)
            return
        raise ValueError("No recent NBM bulletin is published")
    except EXPECTED_SOURCE_ERRORS as exc:
        mark_source(data, "NOAA NBM", error=capture._error_message(exc))


def refresh_nbm_extended(data):
    from . import expansion_nbe

    started = now()
    eligible = started - timedelta(hours=2)
    issue = eligible.replace(hour=12, minute=0, second=0, microsecond=0)
    if issue > eligible:
        issue -= timedelta(days=1)
    prior = source_status(data).get(NBM_EXTENDED, {})
    if prior.get("source_at") == iso(issue) and not prior.get("issue"):
        mark_source(data, NBM_EXTENDED, outcome="unchanged")
        return

    def public_request(request, *, timeout):
        if paused(data):
            raise TimeoutError("Collection stopped")
        return urlopen(request, timeout=timeout)

    cards, failures = [], []
    for station in expansion_nbe.STATION_OFFSETS:
        if paused(data):
            return
        try:
            card = expansion_nbe.load_card(data, issue, station=station, fetch=True, get=public_request)
            cards.append(card)
        except EXPECTED_SOURCE_ERRORS as error:
            failures.append(station + " · " + capture._error_message(error))
            if isinstance(error, HTTPError) and error.code in (403, 404):
                break  # All stations share one original source object.
    if not paused(data):
        mark_source(data, NBM_EXTENDED, upstream_at=iso(issue) if cards else None, count=len(cards),
                    new=sum(parse_time(card["received_at"]) >= started for card in cards),
                    error=failures[0] if failures else None, succeeded=bool(cards))


IEM_HF = "Iowa State MADIS HFMETAR"
MADIS_DENSE = "NOAA MADIS five-minute airport observations"
NBM_EXTENDED = "NOAA NBM seven-day station outlook"
CLI = "NWS Daily Climate Report"
MARKET_CATALOG = "Polymarket US listings"
BOOKS = "Polymarket US books"
KALSHI_BOOKS = "Kalshi public books"
KALSHI_INDEX = "Kalshi hourly weather index"
POLY_WEATHER_BOOKS = "Polymarket US low and rain books"
PAYOUTS = "Polymarket US realized payouts"
GFS = "NOAA GFS temperature/cloud grids"
GFS_WIND = "NOAA GFS 10 m wind"
GFS_PRECIP = "NOAA GFS 3-hour precipitation"
GFS_WAVE = "NOAA GFS Wave significant height"
GFS_WAVE_COMPONENTS = "NOAA GFS Wave native height components"
MRMS_RADAR = "NOAA MRMS one-hour radar estimates"
HRRR = "NOAA HRRR station temperature forecasts"
HRRR_INTRADAY = "NOAA HRRR intraday station forecasts"
NDBC = "NOAA NDBC marine observations"
KALSHI_CATALOG = "Kalshi native listings"
POLY_CATALOG = "Polymarket US native listings"
MARKET_SEARCH = "Local market search index"
HFMETAR_T = re.compile(r"\bT([01])(\d{3})([01])(\d{3})\b")
CLI_DAY = re.compile(r"CLIMATE SUMMARY FOR ([A-Z]+ \d{1,2} \d{4})", re.I)
REQUEST_ID = re.compile(r"[0-9a-f-]{36}")


def collect_path(data):
    root = Path(data) / "collector"
    root.mkdir(parents=True, exist_ok=True)
    return root


def read_json(path):
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def write_json(path, body):
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(canonical(body) + "\n")
            handle.flush()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def paused(data):
    return (collect_path(data) / "paused").exists()


def tracked_http(data, seconds):
    transport = ReadOnlyHTTP(time.monotonic() + seconds)

    def get(*args, **kwargs):
        if paused(data):
            raise OSError("Collection stopped locally")
        return transport(*args, **kwargs)

    return get


def _public_request(data, request, *, timeout):
    if paused(data):
        raise TimeoutError("Collection stopped")
    return urlopen(request, timeout=timeout)


def source_status(data, *, issues=None):
    db = connection(data)
    try:
        db.row_factory = sqlite3.Row
        result = {row["name"]: dict(row) for row in db.execute("SELECT * FROM source_status ORDER BY name")}
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='source_health' AND type='table'").fetchone():
            for row in db.execute("SELECT name,body FROM source_health"):
                try:
                    health = json.loads(row["body"])
                    if not isinstance(health, dict):
                        raise ValueError("Invalid source health record")
                    if row["name"] in result:
                        result[row["name"]].update(health)
                except (ValueError, TypeError) as error:
                    if issues is not None:
                        issues.append({"source": row["name"], "reason": capture._error_message(error)})
        return result
    finally:
        db.close()


def high_frequency_readings(reply, url):
    for item in csv.DictReader(reply.body.decode().splitlines()):
        station = "K" + item.get("station", "")
        if station not in STATIONS:
            continue
        raw = item.get("metar") or ""
        match = HFMETAR_T.search(raw)
        if "MADISHF" not in raw or match is None or not (raw.startswith(station + " ") or raw.startswith("METAR " + station + " ")):
            continue
        stamp = datetime.strptime(item["valid"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        if stamp > reply.at:
            continue
        temp_c = int(match[2]) / (-10 if match[1] == "1" else 10)
        dew_c = int(match[4]) / (-10 if match[3] == "1" else 10)
        row = {
            **capture._observed(stamp, reply.at, temp_c, IEM_HF, url, raw),
            "dewpoint_f": capture._temperature_c(dew_c),
            "precision": "0.1 °C from ASOS METAR tenths group",
            "display_only": True,
        }
        yield station, station_day(station, stamp), "research_observation", IEM_HF, stamp, row


def refresh_high_frequency(data):
    current = now()
    backoff_file = collect_path(data) / "iem-backoff.json"
    pause_until = (read_json(backoff_file) or {}).get("retry_at")
    prior = source_status(data).get(IEM_HF, {}).get("last_attempt")
    next_possible = max(
        parse_time(pause_until) if pause_until else current,
        parse_time(prior) + timedelta(seconds=IEM_INTERVAL) if prior else current,
    )
    if current < next_possible:
        return
    current_minute = current.replace(second=0, microsecond=0)
    # The archive synchronizes about every ten minutes. A short overlapping
    # window picks up newly published or corrected reports and preserves receipt.
    lower = current_minute - timedelta(hours=2)
    query = [
        *(("station", station[1:]) for station in STATIONS),
        ("data", "metar"),
        *((f"{key}{index}", stamp.strftime(fmt))
          for index, stamp in enumerate((lower, current_minute), start=1)
          for key, fmt in (("year", "%Y"), ("month", "%m"), ("day", "%d"), ("hour", "%H"), ("minute", "%M"))),
        ("tz", "Etc/UTC"), ("format", "onlycomma"), ("missing", "empty"), ("report_type", "1"),
    ]
    url = capture.IEM + "/cgi-bin/request/asos.py?" + urlencode(query)
    try:
        reply = tracked_http(data, 12)(url)
        rows = list(high_frequency_readings(reply, url))
        if not rows:
            raise ValueError("No five-minute reports with measured temperature in this source window")
        added = save_weather(data, rows)
        latest = iso(max(row[4] for row in rows))
        mark_source(data, IEM_HF, upstream_at=latest, count=len(rows), new=added, received_at=iso(reply.at))
        backoff_file.unlink(missing_ok=True)
    except HTTPError as exc:
        reason = capture._error_message(exc)
        if exc.code == 429:
            retry = exc.headers.get("Retry-After", "")
            next_attempt = current + timedelta(seconds=IEM_INTERVAL)
            if retry.isdecimal():
                next_attempt = max(next_attempt, current + timedelta(seconds=int(retry)))
            elif retry:
                try:
                    next_attempt = max(next_attempt, parsedate_to_datetime(retry))
                except (TypeError, ValueError):
                    pass
            write_json(backoff_file, {"retry_at": iso(next_attempt)})
            reason += f"; next attempt at or after {iso(next_attempt)}"
        mark_source(data, IEM_HF, error=reason)
    except EXPECTED_SOURCE_ERRORS as exc:
        mark_source(data, IEM_HF, error=capture._error_message(exc))


def refresh_madis_dense(data):
    from . import omo

    hour = (now() - timedelta(minutes=8)).replace(minute=0, second=0, microsecond=0)
    try:
        prior = omo.load(data, hour, live=True)
        frame = omo.fetch(data, hour, live=True)
        points = [point for rows in frame["stations"].values() for point in rows]
        if not points:
            raise ValueError("No valid dense airport observations in NOAA file")
        if not paused(data):
            earlier = {station: {row["at"] for row in rows} for station, rows in (prior or {}).get("stations", {}).items()}
            added = sum(row["at"] not in earlier.get(station, ()) for station, rows in frame["stations"].items() for row in rows)
            mark_source(data, MADIS_DENSE, upstream_at=max(row["at"] for row in points),
                        count=len(points), new=added)
    except EXPECTED_SOURCE_ERRORS as issue:
        if not paused(data):
            mark_source(data, MADIS_DENSE, error=capture._error_message(issue))


def daily_report(station, listing, reply, url):
    payload = reply.json()
    body = payload.get("productText", "")
    issue = parse_time(payload["issuanceTime"])
    if (
        payload["id"] != listing["id"] or payload["productCode"] != "CLI"
        or issue > reply.at or issue != parse_time(listing["issuanceTime"])
        or not re.search(rf"^CLI{station[1:]}\s*$", body, re.M)
    ):
        raise ValueError("Unexpected climate report identity")
    written_day = CLI_DAY.search(body)
    if written_day is None:
        return None
    day = datetime.strptime(written_day[1], "%B %d %Y").date()
    _, end = climate_window(day, STATIONS[station][3])
    measured = re.search(r"^TEMPERATURE \(F\)\s*\n\s*YESTERDAY[ \t\r]*\n[ \t]*MAXIMUM[ \t]+([+-]?\d+)[ \t]+(.*)$", body, re.M)
    if measured is None or issue < end:
        return None
    minima = re.search(r"^\s*MINIMUM[ \t]+([+-]?\d+)\b", body, re.M)
    rain = re.search(r"^PRECIPITATION \(IN\)\s*\n\s*YESTERDAY[ \t]+([\d.]+|T|MM?)\b", body, re.M)
    snow = re.search(r"^SNOWFALL \(IN\)\s*\n\s*YESTERDAY[ \t]+([\d.]+|T|MM?)\b", body, re.M)
    when = re.match(r"\d+ [AP]M\b", measured[2])
    return station, day.isoformat(), "official_report", CLI, issue, {
        "available_at": iso(reply.at), "published_at": iso(issue), "product": payload["id"],
        "high_f": int(measured[1]), "low_f": int(minima[1]) if minima else None,
        "precipitation_inches_as_reported": rain[1] if rain else None,
        "snow_inches_as_reported": snow[1] if snow else None,
        "high_at_local_standard": when[0] if when else None, "source": url,
    }


def refresh_reports(data):
    get = tracked_http(data, 40)
    threshold = now() - timedelta(days=3)
    db = connection(data)
    try:
        seen = {json.loads(row[0])["product"] for row in db.execute("SELECT payload FROM weather WHERE family='official_report'")}
    finally:
        db.close()

    def station_reports(station):
        index = get(f"{capture.NWS}/products/types/CLI/locations/{station[1:]}").json()["@graph"]
        count = 0
        latest = None
        for listing in index:
            ident = listing["id"]
            if ident in seen or REQUEST_ID.fullmatch(ident) is None or parse_time(listing["issuanceTime"]) < threshold:
                continue
            url = f"{capture.NWS}/products/{ident}"
            row = daily_report(station, listing, get(url), url)
            if row:
                count += save_weather(data, [row])
                latest = max(latest or row[4], row[4])
        return count, iso(latest) if latest else None

    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = {pool.submit(station_reports, station): station for station in STATIONS}
        for job in as_completed(jobs):
            source = f"{CLI} {jobs[job]}"
            try:
                count, latest = job.result()
            except Exception as exc:
                mark_source(data, source, error=capture._error_message(exc))
            else:
                mark_source(data, source, count=count, new=count, upstream_at=latest)


def _weather_rows(data, start=None, end=None, *, station=None, through=None):
    db = connection(data)
    try:
        predicates, params = [], []
        seen = parse_time(through).isoformat(timespec="microseconds").replace("+00:00", "Z") if through else None
        for value, predicate in ((start, "day>=?"), (end, "day<=?"), (station, "station=?"), (seen, "seen<=?")):
            if value is not None:
                predicates.append(predicate)
                params.append(value)
        query = "SELECT station, day, family, payload FROM weather"
        if predicates:
            query += " WHERE " + " AND ".join(predicates)
        return db.execute(query + " ORDER BY day DESC, station, stamp, seen", params).fetchall()
    finally:
        db.close()


def weather_events(data=DATA, *, start=None, end=None, station=None, through=None, strict=False):
    events = {}
    key_by_family = {"observation": "observations", "forecast": "forecasts",
                     "research_observation": "research_observations", "official_report": "official_reports"}
    for station, day, family, payload in _weather_rows(data, start, end, station=station, through=through):
        try:
            value = json.loads(payload)
        except ValueError:
            value = None
        if not isinstance(value, dict) or station not in WEATHER_STATIONS or family not in key_by_family:
            if strict:
                raise ValueError(f"Unreadable retained {family} evidence for {station} on {day}")
            continue
        key = (station, day)
        if key not in events:
            lat, lon, zone, offset = WEATHER_STATIONS[station]
            begins, ends = climate_window(day, offset)
            events[key] = {
                "id": f"weather-{station.lower()}-{day}", "station": station, "station_name": STATION_NAMES[station],
                "title": STATION_NAMES[station] + " weather", "weather_only": True, "lat": lat, "lon": lon,
                "timezone": zone, "standard_utc_offset_hours": offset, "date": day,
                "starts_at": iso(begins), "ends_at": iso(ends), "climate_starts_at": iso(begins), "climate_ends_at": iso(ends),
                "settles_at": None, "buckets": [], "observations": [], "forecasts": [],
                "research_observations": [], "official_reports": [],
            }
        events[key][key_by_family[family]].append(value)
    for event in events.values():
        if event["official_reports"]:
            event["settlement"] = min(event["official_reports"], key=lambda row: parse_time(row["available_at"]))
    return list(events.values())


def _merge_forecasts(captures, archive):
    by_issue = {}
    fields = ("hourly", "high_f", "high_sd_f", "low_f", "low_sd_f", "low_valid_at", "low_availability_basis",
              "short_range_temperature", "short_range_issued_at", "short_range_availability_basis")
    for item in sorted(captures + archive, key=lambda row: parse_time(row["available_at"])):
        key = (item["issued_at"], item["high_source"])
        prior = by_issue.setdefault(key, [])
        material = tuple(item.get(name) for name in fields)
        if not prior or material != tuple(prior[-1].get(name) for name in fields):
            prior.append(item)
    return sorted([item for history in by_issue.values() for item in history], key=lambda row: parse_time(row["available_at"]))


def enrich_capture(data, record, *, weather=None):
    if not record or record.get("kind") != "live" or not record.get("events"):
        return record
    if weather is None:
        dates = [event["date"] for event in record["events"]]
        weather = weather_events(data, start=min(dates), end=max(dates))
    known = {(event["station"], event["date"]): event for event in weather}
    merged = []
    for event in record["events"]:
        supplemental = known.get((event["station"], event["date"]))
        if supplemental:
            event = {
                **event, "observations": capture._merge_observations(event["observations"], supplemental["observations"]),
                "forecasts": _merge_forecasts(event["forecasts"], supplemental["forecasts"]),
                "research_observations": supplemental["research_observations"], "official_reports": supplemental["official_reports"],
            }
            if supplemental.get("settlement"):
                event["settlement"] = supplemental["settlement"]
        merged.append(event)
    return {**record, "events": merged}


def _listing_family(slug):
    for pattern, label in (
        (r"temp-[a-z]+high-\d{4}-\d{2}-\d{2}", "Daily highs"),
        (r"temp-[a-z]+low-\d{4}-\d{2}-\d{2}", "Daily lows"),
        (r"rain-daily-\d{4}-\d{2}-\d{2}", "Daily rain"),
        (r"yr-httst-rnk-glbl-\d{4}", "Global annual temperature rank"),
        (r"temp-[a-z]+-[a-z]+", "Monthly temperature"),
    ):
        if re.fullmatch(pattern, slug):
            return label
    return "Other weather"


def _pick_markets(events, clock):
    order = tuple(STATIONS)
    primary = ("KNYC", "KMIA", "KLAX")
    ordered = sorted(events, key=lambda event: (
        event["station"] not in primary,
        event["date"] != station_day(event["station"], clock),
        event["date"],
        primary.index(event["station"]) if event["station"] in primary else order.index(event["station"]),
    ))
    chosen = {}
    for event in ordered:
        chosen.setdefault(event["station"], event)
    return list(chosen.values())[:ACTIVE_MARKET_LIMIT]


def _catalog_selection(catalog, clock):
    """Reassign the existing sampling slots as retained events close."""
    candidates = catalog.get("candidates", catalog.get("selected", []))
    return _pick_markets((event for event in candidates if parse_time(event["ends_at"]) > clock), clock)


def refresh_market_catalog(data):
    try:
        reply = tracked_http(data, 13)(DISCOVERY)
        listings = reply.json()["events"]
        if not isinstance(listings, list):
            raise ValueError("Unexpected exchange catalog format")
        clock = reply.at
        events = []
        families = {}
        inventory = []
        issues = []
        for listing in listings:
            slug = listing.get("slug", "")
            if not isinstance(slug, str):
                continue
            family = _listing_family(slug)
            families[family] = families.get(family, 0) + 1
            markets = listing.get("markets") or []
            inventory.append({
                "slug": slug, "title": listing.get("title"), "family": family,
                "market_count": len(markets),
                "example_rules": [{"slug": item.get("slug"), "rule": item.get("description")} for item in markets[:1]],
                "source": f"{capture.GATEWAY}/v1/events/slug/{slug}",
            })
            if family != "Daily highs":
                continue
            try:
                event = parse_event(listing)
            except (TypeError, ValueError, KeyError) as exc:
                issues.append({"source": slug, "reason": capture._error_message(exc)})
                continue
            if parse_time(event["ends_at"]) > clock and any(b["exchange_status"] in OPEN for b in event["buckets"]):
                events.append(event)
        choice = _pick_markets(events, clock)
        path = collect_path(data) / "listings.json"
        body = dict(families=families, inventory=inventory, eligible=len(events),
                    selected=choice, candidates=events, issues=issues)
        previous = read_json(path) or {}
        write_json(path, {"at": iso(clock), **body})
        mark_source(data, MARKET_CATALOG, count=len(listings), received_at=iso(clock),
                    new=int(any(previous.get(key) != value for key, value in body.items())))
    except EXPECTED_SOURCE_ERRORS as exc:
        mark_source(data, MARKET_CATALOG, error=capture._error_message(exc))


def _record(started, *, events=None, settlements=None, issues=None, rounds=0, **extra):
    return {
        "version": capture.SCHEMA_VERSION, "kind": "live", "started_at": iso(started),
        "captured_at": iso(now()), "poll_seconds": BOOK_INTERVAL, "rounds": rounds,
        "events": events or [], "settlements": settlements or {}, "issues": issues or [], **extra,
    }


def refresh_books(data):
    from . import venue_archive

    catalog = read_json(collect_path(data) / "listings.json")
    started = now()
    if not catalog or started - parse_time(catalog["at"]) > timedelta(seconds=2 * DISCOVERY_INTERVAL):
        mark_source(data, BOOKS, count=0, error="No recent exchange listing response")
        return
    events = _catalog_selection(catalog, started)
    buckets = [bucket for event in events for bucket in event["buckets"]]
    get = tracked_http(data, BOOK_INTERVAL - 2)
    issues = []
    targets = {}

    def fetch(bucket):
        request = {}

        def observe(*args, **kwargs):
            if not paused(data):
                request["requested_at"] = iso(now())
            try:
                reply = get(*args, **kwargs)
            except OSError as error:
                request["http_status"] = error.code if isinstance(error, HTTPError) else 0
                raise
            request.update(requested_at=iso(reply.requested_at) if reply.requested_at else request.get("requested_at"),
                           http_status=200, received_at=iso(reply.at), headers=reply.headers)
            return reply

        return capture._fetch_book(observe, bucket, now), request

    with ThreadPoolExecutor(max_workers=6) as pool:
        jobs = {pool.submit(fetch, bucket): bucket for bucket in buckets}
        for job in as_completed(jobs):
            bucket = jobs[job]
            quote, request = job.result()
            quote["round"] = 1
            bucket["books"] = [quote]
            targets[bucket["id"]] = venue_archive.book_batch_target(
                quote, requested_at=request.get("requested_at"), http_status=request.get("http_status"),
                headers=request.get("headers"))
            if request.get("received_at"):
                targets[bucket["id"]]["received_at"] = request["received_at"]
            if quote["depth_kind"] != "displayed_ladder":
                issues.append({"source": bucket["id"], "reason": quote["reason"]})
    if paused(data) and not any(target["requested_at"] for target in targets.values()):
        return
    if buckets:
        record = _record(started, events=events, issues=issues, rounds=1)
        path = capture.write_capture(record, Path(data) / "captures")
        for target in targets.values():
            target.update(available_at=record["captured_at"], capture=path.name if path else None, family="daily-high")
    with venue_archive.connect(data) as db, db:
        batch = venue_archive.save_book_batch(db, venue_archive.POLY, venue_archive.POLY_HIGH_PRIORITY,
                                              iso(started), iso(now()), targets)
    _mark_book_batch(data, BOOKS, batch)


def refresh_venue_catalog(data):
    from . import venue_archive, venue_sources

    started = now()

    def report(venue, failure):
        source = KALSHI_CATALOG if venue == venue_archive.KALSHI else POLY_CATALOG
        with _source_lane(data, source), venue_archive.connect(data) as db:
            rows = db.execute(
                """SELECT rows,state,error,updated_at FROM progress WHERE venue=? AND updated_at>=?
                   AND kind IN ('catalog-live','catalog-recent','catalog-open','series','settlement-check')""",
                (venue, venue_archive.receipt_time(started)),
            ).fetchall()
            errors = [row["error"] for row in rows if row["error"] and row["state"] == "error"]
            usable = any(row["state"] != "error" for row in rows)
            count = sum(row["rows"] for row in rows)
            issue = failure or next(iter(errors), None)
            mark_source(data, source, count=count, error=issue, succeeded=usable or not issue,
                        received_at=max((row["updated_at"] for row in rows), key=parse_time, default=None))

    venue_sources.refresh_open(data, report=report)


def refresh_kalshi_books(data):
    from .venue_sources import watch_once

    _mark_book_batch(data, KALSHI_BOOKS, watch_once(data, outcome=True))


def refresh_poly_weather_books(data):
    from .venue_sources import watch_poly_weather_once

    _mark_book_batch(data, POLY_WEATHER_BOOKS, watch_poly_weather_once(data, outcome=True))


def _mark_book_batch(data, source, batch):
    if batch is None:
        return
    targets = list(batch["targets"].values())
    usable = batch["usable"]
    empty_depth = batch.get("empty_depth", 0)
    issue = batch.get("issue")
    interrupted = paused(data) and batch.get("unrequested", 0) > 0
    if not issue and not interrupted and usable + empty_depth != batch["selected"]:
        issue = next((target["reason"] for target in targets if target.get("state") != "empty_depth" and target["reason"]),
                     "Selected books unavailable")
    clocks = lambda field: max((target[field] for target in targets if target.get(field)), key=parse_time, default=None)
    outcome = ("interrupted" if interrupted else "empty" if not targets else
               "partial" if issue and (usable or empty_depth) else "error" if issue else
               "published" if usable else "empty_depth")
    mark_source(data, source, count=usable, new=usable, error=issue, outcome=outcome,
                succeeded=bool(usable or empty_depth) or not targets, upstream_at=clocks("source_at"),
                received_at=clocks("received_at"), published_at=clocks("available_at"),
                batch={key: value for key, value in batch.items() if key != "targets"})


def refresh_kalshi_index(data):
    from .venue_index import refresh_recent

    count = refresh_recent(data)
    if not paused(data):
        mark_source(data, KALSHI_INDEX, count=count, new=count,
                    error=None if count else "No weather index points returned")


def _payout_queue(data, previous, clock):
    targets = list(capture._settlement_targets(previous, clock))
    db = connection(data)
    try:
        checked = dict(db.execute("SELECT slug, checked_at FROM settlement_checks"))
    finally:
        db.close()
    # Move recently tested symbols behind unchecked ones even when a market
    # is still pending. A thin backlog can use the same follow-up cadence.
    return sorted(targets, key=lambda item: (checked.get(item[1], ""), item[0], item[1]))


def refresh_payouts(data):
    started = now()
    previous = capture.load_captures(Path(data) / "captures")
    targets = _payout_queue(data, previous, started)[:capture.SETTLEMENT_LIMIT]
    if not targets:
        mark_source(data, PAYOUTS, count=0, outcome="empty")
        return
    get = tracked_http(data, 22)
    issues = []
    settlements = {}
    checked = []
    with ThreadPoolExecutor(max_workers=5) as pool:
        jobs = {pool.submit(capture._get_settlement, get, slug): (event, slug) for event, slug in targets}
        for job in as_completed(jobs):
            event, slug = jobs[job]
            checked.append((slug, iso(now())))
            try:
                settlement = job.result()
            except HTTPError as exc:
                if exc.code != 404:
                    issues.append({"source": slug, "reason": capture._error_message(exc)})
            except EXPECTED_SOURCE_ERRORS as exc:
                issues.append({"source": slug, "reason": capture._error_message(exc)})
            else:
                settlements.setdefault(event, {})[slug] = settlement
    db = connection(data)
    try:
        with db:
            db.executemany("INSERT OR REPLACE INTO settlement_checks VALUES (?,?)", checked)
    finally:
        db.close()
    if settlements:
        capture.write_capture(_record(started, settlements=settlements, issues=issues), Path(data) / "captures")
    new = sum(map(len, settlements.values()))
    if issues:
        reason = f"{len(issues)} payout requests failed: {issues[0]['reason']}"
    else:
        reason = None
    mark_source(data, PAYOUTS, upstream_at=iso(now()), count=len(checked), new=new, error=reason)


def refresh_grids(data):
    from . import clouds, grids, wind

    started = now()
    complete = started - timedelta(hours=5)
    issue = complete.replace(hour=complete.hour // 6 * 6, minute=0, second=0, microsecond=0)
    status = source_status(data)
    ready = lambda name: status.get(name, {}).get("source_at") == iso(issue) and not status[name].get("issue")

    def get(request, *, timeout):
        return _public_request(data, request, timeout=timeout)

    temperature = None
    for name in (GFS, GFS_WIND):
        if paused(data):
            return
        if ready(name):
            mark_source(data, name, outcome="unchanged")
            continue
        with _source_lane(data, name):
            if temperature is None:
                temperature = grids.cache_live(started, cache=Path(data) / "grids", get=get)
            if paused(data):
                return
            if name == GFS:
                cloud = clouds.cache_matching(temperature["frames"], cache=Path(data) / "clouds", get=get)
                _save_grid_layers(data, GFS, started, issue, {"grids": temperature, "clouds": cloud})
            else:
                airflow = wind.cache_matching(temperature["frames"], cache=Path(data) / "wind", get=get)
                _save_grid_layers(data, GFS_WIND, started, issue, {"winds": airflow})


def refresh_precipitation(data):
    from . import expansion_precip, grids

    started = now()
    complete = started - timedelta(hours=5)
    issue = complete.replace(hour=complete.hour // 6 * 6, minute=0, second=0, microsecond=0)
    status = source_status(data).get(GFS_PRECIP, {})
    if status.get("source_at") == iso(issue) and not status.get("issue"):
        mark_source(data, GFS_PRECIP, outcome="unchanged")
        return

    def collect(request, *, timeout):
        if paused(data):
            raise TimeoutError("Collection stopped")
        return urlopen(request, timeout=timeout)

    cache = Path(data) / "precipitation"
    keys = [grids.frame_path(cache, issue, lead).stem for lead in grids.GRID_HOURS if lead > 0]
    layer = expansion_precip.cache_matching(keys, cache=cache, get=collect)
    if not paused(data):
        _save_grid_layers(data, GFS_PRECIP, started, issue, {"precipitation": layer})


def refresh_hrrr(data):
    from . import hrrr

    clock = now() - timedelta(hours=3)
    issue = clock.replace(hour=0, minute=0, second=0, microsecond=0)
    status = source_status(data).get(HRRR, {})
    if status.get("source_at") == iso(issue) and not status.get("issue"):
        mark_source(data, HRRR, outcome="unchanged")
        return
    day = issue.date()
    counts, errors = hrrr.backfill(data, day, day, workers=4)
    if paused(data):
        return
    count = counts.get(day.isoformat(), 0)
    failure = next(iter(errors.values()), None)
    mark_source(data, HRRR, upstream_at=iso(issue) if count else None, count=count, new=count,
                error=failure or (None if count else "No HRRR station forecasts"), succeeded=count > 0)


def refresh_hrrr_intraday(data):
    from . import hrrr

    eligible = now() - timedelta(minutes=85)
    cycle = next((hour for hour in reversed(hrrr.INTRADAY_LEADS) if hour <= eligible.hour), None)
    if cycle is None:
        return
    issue = eligible.replace(hour=cycle, minute=0, second=0, microsecond=0)
    status = source_status(data).get(HRRR_INTRADAY, {})
    if status.get("source_at") == iso(issue) and not status.get("issue"):
        mark_source(data, HRRR_INTRADAY, outcome="unchanged")
        return
    day = issue.date()
    counts, errors = hrrr.backfill(data, day, day, workers=3, cycle=cycle, leads=hrrr.INTRADAY_LEADS[cycle])
    if not paused(data):
        count = counts.get(day.isoformat(), 0)
        failure = next(iter(errors.values()), None)
        mark_source(data, HRRR_INTRADAY, upstream_at=iso(issue) if count else None, count=count, new=count,
                    error=failure or (None if count else "No HRRR intraday forecasts"), succeeded=count > 0)


def refresh_market_search(data):
    from . import venue_archive, venue_search

    if not paused(data):
        with venue_archive.connect(data) as db:
            db.execute("PRAGMA busy_timeout=500")
            try:
                venue_search.ensure(db)
            except sqlite3.OperationalError as issue:
                if "locked" not in str(issue).lower():
                    raise
                return
        mark_source(data, MARKET_SEARCH, outcome="unchanged")


def refresh_waves(data):
    from . import wave_components, waves

    started = now()
    complete = started - timedelta(hours=5)
    issue = complete.replace(hour=complete.hour // 6 * 6, minute=0, second=0, microsecond=0)
    sources = source_status(data)
    status = sources.get(GFS_WAVE, {})
    if status.get("source_at") != iso(issue) or status.get("issue"):
        with _source_lane(data, GFS_WAVE):
            layer = waves.cache_cycle(data, issue)
            if not paused(data):
                _save_grid_layers(data, GFS_WAVE, started, issue, {"waves": layer})
    else:
        mark_source(data, GFS_WAVE, outcome="unchanged")
    components = sources.get(GFS_WAVE_COMPONENTS, {})
    if not paused(data) and (components.get("source_at") != iso(issue) or components.get("issue")):
        with _source_lane(data, GFS_WAVE_COMPONENTS):
            requests = ((issue, lead) for lead in wave_components.LIVE_LEADS)
            frames, errors = wave_components.backfill(data, requests)
            if not paused(data):
                mark_source(data, GFS_WAVE_COMPONENTS, upstream_at=iso(issue) if frames else None,
                            count=len(frames), new=len(frames), error=next(iter(errors.values()), None),
                            succeeded=bool(frames))
    elif not paused(data):
        mark_source(data, GFS_WAVE_COMPONENTS, outcome="unchanged")


def refresh_marine(data):
    try:
        observed = marine.collect(data, tracked_http(data, 12))
        mark_source(data, NDBC, upstream_at=observed["latest"], count=observed["records"], new=observed["inserted"])
    except EXPECTED_SOURCE_ERRORS as exc:
        mark_source(data, NDBC, error=capture._error_message(exc))


def refresh_radar(data):
    from . import radar

    newest = (now() - timedelta(minutes=4)).replace(minute=0, second=0, microsecond=0)
    prior = source_status(data).get(MRMS_RADAR, {}).get("source_at")
    try:
        for offset in (0, 1):
            end = newest - timedelta(hours=offset)
            if paused(data):
                return
            if prior and parse_time(prior) >= end:
                mark_source(data, MRMS_RADAR, outcome="unchanged")
                return
            try:
                radar.cache_frame(data, end)
            except HTTPError as exc:
                if exc.code in {403, 404}:
                    continue
                raise
            if not paused(data):
                mark_source(data, MRMS_RADAR, upstream_at=iso(end), count=1, new=1)
            return
        mark_source(data, MRMS_RADAR, count=0, error="No recent public NOAA MRMS radar field")
    except EXPECTED_SOURCE_ERRORS as exc:
        if not paused(data):
            mark_source(data, MRMS_RADAR, error=capture._error_message(exc))


def _save_grid_layers(data, source, started, issue, layers):
    counts = sum(len(artifact.get("frames", [])) for artifact in layers.values())
    problems = {str(error) for artifact in layers.values() for error in artifact.get("errors", {}).values()}
    if counts and not paused(data):
        capture.write_capture(_record(started, **layers), Path(data) / "captures")
    reason = "; ".join(sorted(problems)) if problems else None if counts else "No GFS fields returned"
    mark_source(data, source, upstream_at=iso(issue) if counts else None, count=counts, new=counts,
                error=reason, succeeded=counts > 0)


def _worker_locked(root):
    with (root / "worker.lock").open("a+") as lock:
        try:
            flock(lock, LOCK_EX | LOCK_NB)
        except BlockingIOError:
            return True
        flock(lock, LOCK_UN)
        return False


def _schedule():
    return (
        (refresh_observations, OBS_INTERVAL, None), (refresh_high_frequency, 60, IEM_HF),
        (refresh_nbm, NBM_INTERVAL, "NOAA NBM"), (refresh_nbm_extended, NBM_INTERVAL, NBM_EXTENDED),
        (refresh_madis_dense, MADIS_DENSE_INTERVAL, MADIS_DENSE), (refresh_reports, CLI_INTERVAL, None),
        (refresh_grids, GFS_INTERVAL, None), (refresh_market_catalog, DISCOVERY_INTERVAL, MARKET_CATALOG),
        (refresh_books, BOOK_INTERVAL, BOOKS), (refresh_payouts, PAYOUT_INTERVAL, PAYOUTS),
        (refresh_venue_catalog, DISCOVERY_INTERVAL, None), (refresh_kalshi_books, BOOK_INTERVAL, KALSHI_BOOKS),
        (refresh_poly_weather_books, BOOK_INTERVAL, POLY_WEATHER_BOOKS),
        (refresh_precipitation, GFS_INTERVAL, GFS_PRECIP), (refresh_hrrr, GFS_INTERVAL, HRRR),
        (refresh_kalshi_index, OBS_INTERVAL, KALSHI_INDEX), (refresh_hrrr_intraday, GFS_INTERVAL, HRRR_INTRADAY),
        (refresh_market_search, GFS_INTERVAL, MARKET_SEARCH), (refresh_marine, IEM_INTERVAL, NDBC),
        (refresh_waves, GFS_INTERVAL, None), (refresh_radar, IEM_INTERVAL, MRMS_RADAR),
    )


def _task_source(task, source):
    return source or "Task issue · " + task.__name__.removeprefix("refresh_").replace("_", " ")


def _reconcile_status(data, schedule):
    with closing(connection(data)) as db, db:
        _status_table(db)
        for task, _, source in schedule:
            name = _task_source(task, source)
            orphan = db.execute("SELECT * FROM source_status WHERE name=?", (task.__name__,)).fetchone()
            if not orphan:
                continue
            existing = db.execute("SELECT * FROM source_status WHERE name=?", (name,)).fetchone()
            _, when, _, _, _, _, issue, failures = orphan
            if existing is None:
                db.execute("UPDATE source_status SET name=? WHERE name=?", (name, task.__name__))
            else:
                if parse_time(when) >= parse_time(existing[1]):
                    db.execute("UPDATE source_status SET last_attempt=?,issue=?,failures=? WHERE name=?",
                               (when, issue, failures, name))
                db.execute("DELETE FROM source_status WHERE name=?", (task.__name__,))
            row = db.execute("SELECT body FROM source_health WHERE name=?", (name,)).fetchone()
            health = json.loads(row[0]) if row else {}
            health["legacy_issue"] = {"name": task.__name__, "at": when, "reason": issue, "failures": failures}
            if not health.get("last_error_at") or parse_time(when) >= parse_time(health["last_error_at"]):
                health.update(last_error=issue, last_error_at=when)
            db.execute("INSERT OR REPLACE INTO source_health VALUES (?,?)", (name, canonical(health)))


def _telemetry(data, root, worker_state, clock, issues):
    from . import venue_archive
    from .strategy_rules import EXCHANGE_AGE_SECONDS

    try:
        sources = source_status(data, issues=issues)
    except Exception as exc:
        issues.append({"source": "Source status storage", "reason": capture._error_message(exc)})
        sources = {}
    batches = {}
    if (Path(data) / "venues.sqlite3").is_file():
        try:
            with venue_archive.connect(data) as db:
                for venue, scopes in venue_archive.AUTOMATIC_BOOK_SCOPES.items():
                    for scope in (*scopes, venue_archive.MANUAL_BOOK):
                        batch = venue_archive.latest_book_batch(db, venue, scope, clock)
                        if batch:
                            images = {"recent": 0, "stale": 0, "timestamp_unavailable": 0}
                            for target in batch["targets"].values():
                                if target["state"] != "usable":
                                    continue
                                stamp = target["source_at"]
                                age = (clock - parse_time(stamp)).total_seconds() if stamp else None
                                key = ("timestamp_unavailable" if age is None else
                                       "stale" if age > EXCHANGE_AGE_SECONDS else "recent")
                                images[key] += 1
                            batch["exchange_images"] = images
                            batches.setdefault(venue, {})[scope] = batch
        except (sqlite3.Error, ValueError, OSError) as exc:
            issues.append({"source": "Venue book batches", "reason": capture._error_message(exc)})
    cadence = {_task_source(task, source): seconds for task, seconds, source in _schedule()}
    cadence.update({IEM_HF: IEM_INTERVAL, GFS: GFS_INTERVAL, GFS_WIND: GFS_INTERVAL,
                    GFS_WAVE: GFS_INTERVAL, GFS_WAVE_COMPONENTS: GFS_INTERVAL,
                    KALSHI_CATALOG: DISCOVERY_INTERVAL, POLY_CATALOG: DISCOVERY_INTERVAL,
                    "Aviation Weather Center": OBS_INTERVAL})
    iem_retry = (read_json(root / "iem-backoff.json") or {}).get("retry_at")
    for name, row in sources.items():
        try:
            interval = cadence.get(name, OBS_INTERVAL if name.startswith("NWS ") else CLI_INTERVAL)
            row["cadence_seconds"] = interval
            row["worker_state"] = worker_state
            row["outcome"] = row.get("outcome") or ("error" if row.get("issue") else "legacy")
            last_check = row.get("last_success")
            row["check_stale"] = bool(last_check and (clock - parse_time(last_check)).total_seconds() > interval * 3)
            books = name in (BOOKS, POLY_WEATHER_BOOKS, KALSHI_BOOKS)
            publication = row.get("last_successful_publication_at")
            row["data_stale"] = bool(books and publication and
                                     (clock - parse_time(publication)).total_seconds() > interval * 3)
            reference = row["last_attempt"] if name == IEM_HF else row.get("attempt_started_at") or row["last_attempt"]
            retry = parse_time(reference) + timedelta(seconds=interval)
            if name == IEM_HF and iem_retry:
                retry = max(retry, parse_time(iem_retry))
                if parse_time(iem_retry) > clock:
                    row["backoff_until"] = iem_retry
            if name in (POLY_WEATHER_BOOKS, KALSHI_BOOKS, KALSHI_CATALOG, POLY_CATALOG):
                venue = (venue_archive.KALSHI if name in (KALSHI_BOOKS, KALSHI_CATALOG) else venue_archive.POLY)
                try:
                    due = float((Path(data) / "venues" / (venue + ".pace")).read_text())
                    if due > clock.timestamp():
                        row["provider_not_before"] = iso(datetime.fromtimestamp(due, timezone.utc))
                except (OSError, ValueError, OverflowError):
                    pass
            row["next_check_not_before"] = iso(retry) if worker_state == "running" else None
        except (ValueError, TypeError, KeyError) as exc:
            issues.append({"source": name, "reason": capture._error_message(exc)})
    return sources, batches


def status(data=DATA):
    root = collect_path(data)
    worker = read_json(root / "worker.json") or {}
    listings = read_json(root / "listings.json") or {}
    running = _worker_locked(root)
    enabled = not paused(data)
    state = "running" if running and enabled else "stopped" if not enabled else "offline"
    clock = now()
    issues = []
    sources, batches = _telemetry(data, root, state, clock, issues)
    return {
        "enabled": enabled, "running": running, "state": state, "at": iso(clock),
        "started_at": worker.get("started_at") if running else None, "pid": worker.get("pid") if running else None,
        "sources": sources, "telemetry_issues": issues, "book_batches": batches,
        "data_revision": [[name, row["revision"], row["published_at"]]
                          for name, row in sorted(sources.items()) if row.get("revision") and row.get("published_at")],
        "stations": list(WEATHER_STATIONS), "catalog_at": listings.get("at"),
        "market_families": listings.get("families", {}), "eligible_markets": listings.get("eligible", 0),
        "selected_markets": [(event["station"], event["date"], event["id"]) for event in _catalog_selection(listings, now())],
        "market_inventory": listings.get("inventory", []),
        "catalog_issues": listings.get("issues", []),
        "cadence_seconds": {
            "observations": OBS_INTERVAL, "dense_observations": IEM_INTERVAL, "station_guidance": NBM_INTERVAL,
            "reports": CLI_INTERVAL, "grids": GFS_INTERVAL, "listings": DISCOVERY_INTERVAL, "books": BOOK_INTERVAL,
            "payouts": PAYOUT_INTERVAL, "venue_listings": DISCOVERY_INTERVAL, "kalshi_books": BOOK_INTERVAL,
            "precipitation": GFS_INTERVAL, "waves": GFS_INTERVAL, "radar": IEM_INTERVAL,
            "polymarket_weather_books": BOOK_INTERVAL, "hrrr": GFS_INTERVAL, "hrrr_intraday": GFS_INTERVAL,
            "kalshi_weather_index": OBS_INTERVAL,
            "marine_observations": IEM_INTERVAL, "madis_airport_dense": MADIS_DENSE_INTERVAL,
        },
    }


def start(data=DATA, *, explicit=False):
    root = collect_path(data)
    if explicit:
        (root / "paused").unlink(missing_ok=True)
    if not paused(data) and not _worker_locked(root):
        with (root / "worker.log").open("a") as log:
            subprocess.Popen(
                [sys.executable, "-m", "weather_edge.collector", "run", "--data", str(Path(data).resolve())],
                cwd=Path(__file__).resolve().parents[1], stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                start_new_session=True,
            )
    return status(data)


def stop(data=DATA):
    marker = collect_path(data) / "paused"
    marker.touch()
    return status(data)


def run(data=DATA):
    root = collect_path(data)
    with (root / "worker.lock").open("a+") as lock:
        try:
            flock(lock, LOCK_EX | LOCK_NB)
        except BlockingIOError:
            return
        write_json(root / "worker.json", {"pid": os.getpid(), "started_at": iso(now())})
        shutdown = Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: shutdown.set())
        tasks = _schedule()
        try:
            _reconcile_status(data, tasks)
        except Exception as exc:
            _local_diagnostic(data, "Source status reconciliation", exc)
        pending_generation = {}
        next_runs = {}
        with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
            while not shutdown.is_set():
                clock = time.monotonic()
                for task, future in list(pending_generation.items()):
                    if future.done():
                        try:
                            future.result()
                        except Exception as exc:
                            _local_diagnostic(data, task.__name__, exc)
                        del pending_generation[task]
                if paused(data):
                    next_runs.clear()
                else:
                    for task, cadence, source in tasks:
                        if task not in pending_generation and clock >= next_runs.get(task, 0):
                            if task is refresh_books and refresh_market_catalog in pending_generation and not (root / "listings.json").exists():
                                continue
                            pending_generation[task] = pool.submit(_run_task, task, data, _task_source(task, source))
                            next_runs[task] = clock + cadence
                shutdown.wait(1)


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["run", "start", "stop", "status"])
    parser.add_argument("--data", type=Path, default=DATA)
    args = parser.parse_args()
    if args.action == "run":
        run(args.data)
        return
    operation = {"start": lambda data: start(data, explicit=True), "stop": stop, "status": status}
    print(json.dumps(operation[args.action](args.data), indent=2))


if __name__ == "__main__":
    main()
