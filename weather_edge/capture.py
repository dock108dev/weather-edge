"""Run a bounded public-data capture or exercise the same flow using offline fixtures."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
from threading import Lock
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

from . import capture_index
from .marine import ORIGIN as NDBC
from .market import DISCOVERY, GATEWAY, MARKET_SLUG, OPEN, decimal, iso, mark_book_progress, parse_book, parse_event, parse_time
from .nbm import AWS_NBM, STATION_RANGES, forecast_for_day, nbm_url, parse_nbh, parse_nbs

LIVE_DIR = Path(".data/captures")
FIXTURE_DIR = Path(".data/fixtures")
NWS = "https://api.weather.gov"
AWC = "https://aviationweather.gov/api/data/metar"
IEM = "https://mesonet.agron.iastate.edu"
FORECAST_CAP = 40 * 2**20
JSON_CAP = 4 * 2**20
USER_AGENT = "WeatherEdge/0.1 local weather research"
SCHEMA_VERSION = 1
SETTLEMENT_LIMIT = 36


class ImmutableOrigin(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


@dataclass(frozen=True)
class Reply:
    body: bytes
    at: datetime
    headers: dict
    requested_at: datetime | None = None

    def json(self):
        result = json.loads(self.body)
        if not isinstance(result, (dict, list)):
            raise ValueError("Unexpected JSON response")
        return result


class ReadOnlyHTTP:
    """Credential-free GET transport with a shared deadline and exchange pacing."""

    exchange_lock = Lock()
    next_exchange = 0.0

    def __init__(self, deadline):
        self.deadline = deadline
        self.opener = build_opener(ImmutableOrigin())

    def __call__(self, url, limit=JSON_CAP, headers=None):
        route = urlsplit(url)
        origin = route.netloc
        if route.scheme != "https" or origin not in {urlsplit(host).netloc for host in (NWS, AWC, AWS_NBM, GATEWAY, IEM, NDBC)}:
            raise ValueError("Unexpected source origin")
        if origin == urlsplit(GATEWAY).netloc:
            with ReadOnlyHTTP.exchange_lock:
                now = time.monotonic()
                wait = max(0, ReadOnlyHTTP.next_exchange - now)
                if now + wait >= self.deadline:
                    raise TimeoutError("Capture deadline reached")
                time.sleep(wait)
                # 12.5 requests/second at most. This works below both published
                # limits, including the 20/second limit named in price-history docs.
                ReadOnlyHTTP.next_exchange = time.monotonic() + 0.08
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Capture deadline reached")
        request_headers = {"User-Agent": USER_AGENT, "Cache-Control": "no-cache", "Accept-Encoding": "identity"}
        if headers and "Range" in headers and origin == urlsplit(AWS_NBM).netloc:
            request_headers["Range"] = headers["Range"]
        req = Request(url, headers=request_headers, method="GET")
        requested_at = datetime.now(timezone.utc)
        try:
            with self.opener.open(req, timeout=min(6, remaining)) as response:
                body = bytearray()
                while chunk := response.read1(min(65536, limit + 1 - len(body))):
                    body.extend(chunk)
                    if len(body) > limit:
                        raise ValueError("Source exceeded response limit")
                    if time.monotonic() > self.deadline:
                        raise TimeoutError("Capture deadline reached")
                return Reply(bytes(body), datetime.now(timezone.utc),
                             {name.lower(): value for name, value in response.headers.items()}, requested_at)
        except HTTPError as error:
            if error.code == 429 and origin == urlsplit(GATEWAY).netloc:
                delay = error.headers.get("Retry-After", "")
                cooldown = min(60, max(2, int(delay))) if delay.isdigit() else 2
                with ReadOnlyHTTP.exchange_lock:
                    ReadOnlyHTTP.next_exchange = max(ReadOnlyHTTP.next_exchange, time.monotonic() + cooldown)
            raise


def _temperature_c(value):
    f = decimal(value) * decimal("1.8") + 32
    if not -150 <= f <= 150:
        raise ValueError("Observation temperature out of range")
    return float(f)


def _observed(at, receipt, temp_c, source, url, report=None):
    reading_at = parse_time(at)
    if reading_at > receipt:
        return None
    return {"at": iso(reading_at), "available_at": iso(receipt), "temp_f": _temperature_c(temp_c),
            "source": source, "source_url": url, "report": report}


def _nws_obs(get, station, *, limit=72):
    url = f"{NWS}/stations/{station}/observations?limit={limit}"
    reply = get(url)
    result = []
    for feature in reply.json().get("features", []):
        metadata = feature.get("properties") or {}
        if not str(metadata.get("station", "")).endswith("/stations/" + station):
            continue
        temp = metadata.get("temperature") or {}
        if temp.get("value") is None or temp.get("unitCode") not in {"wmoUnit:degC", "uc:Cel"}:
            continue
        ob = _observed(metadata["timestamp"], reply.at, temp["value"], "NWS", url,
                       metadata.get("rawMessage"))
        if ob:
            dew = metadata.get("dewpoint") or {}
            if dew.get("value") is not None and dew.get("unitCode") in {"wmoUnit:degC", "uc:Cel"}:
                ob["dewpoint_f"] = _temperature_c(dew["value"])
            wind = metadata.get("windSpeed") or {}
            if wind.get("value") is not None and wind.get("unitCode") == "wmoUnit:km_h-1":
                ob["wind_speed_mps"] = round(float(decimal(wind["value"]) / decimal("3.6")), 3)
            result.append(ob)
    return result


def _awc_obs(get, stations, *, hours=24):
    if not stations:
        return {}
    url = AWC + "?" + urlencode({"ids": ",".join(stations), "format": "json", "hours": hours})
    reply = get(url)
    result = {station: [] for station in stations}
    for observation in reply.json() if reply.body else []:
        station = observation.get("icaoId")
        if station not in result or observation.get("temp") is None:
            continue
        reading_at = datetime.fromtimestamp(observation["obsTime"], timezone.utc)
        ob = _observed(reading_at, reply.at, observation["temp"], "Aviation Weather Center",
                       url, observation.get("rawOb"))
        if ob:
            if observation.get("dewp") is not None:
                ob["dewpoint_f"] = _temperature_c(observation["dewp"])
            if isinstance(observation.get("wspd"), (int, float)):
                ob["wind_speed_mps"] = round(float(decimal(observation["wspd"]) * decimal("0.514444")), 3)
            result[station].append(ob)
    return result


def _forecasts(get, cycle, stations, *, station_range=False):
    result = {}
    for product, parser in (("nbh", parse_nbh), ("nbs", parse_nbs)):
        url = nbm_url(cycle.date(), cycle.hour, product)
        extra = {"Range": STATION_RANGES["KNYC"][product]} if station_range else None
        reply = get(url, FORECAST_CAP, extra) if extra else get(url, FORECAST_CAP)
        for station in stations:
            try:
                card = parser(reply.body, station)
            except (ValueError, KeyError):
                continue
            if parse_time(card["issued_at"]) != cycle or reply.at < cycle:
                raise ValueError("NOAA issue does not match requested cycle")
            result.setdefault(station, {})[product] = {
                **card, "available_at": iso(reply.at), "source": url,
                "source_last_modified": reply.headers.get("last-modified"),
                "availability_basis": "local_receipt",
            }
    return result


def _latest_forecasts(get, at, stations, events):
    if not stations:
        return {}
    # Start with the preceding hour because a new NBM cycle is published after
    # its nominal UTC issue time. Check at most three consecutive cycles.
    cycle = at.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    for hour in range(3):
        requested = cycle - timedelta(hours=hour)
        try:
            result = _forecasts(get, requested, stations)
        except HTTPError as error:
            if error.code in {403, 404}:
                continue
            raise
        if result:
            by_station = {station: [cards] for station, cards in result.items()}
            nyc = by_station.get("KNYC")
            needs_txn = nyc and any(
                not ((forecast_for_day(nyc[0], event["date"]) or {}).get("high_source") == "noaa-nbm-nbs-txn")
                for event in events if event["station"] == "KNYC"
            )
            if needs_txn:
                earlier = requested - timedelta(hours=1)
                earlier = earlier.replace(hour=(earlier.hour // 6) * 6)
                try:
                    older = _forecasts(get, earlier, ["KNYC"], station_range=True)
                except (KeyError, ValueError, HTTPError, URLError, OSError):
                    older = {}
                if "KNYC" in older:
                    nyc.insert(0, older["KNYC"])
            return by_station
    raise ValueError("No recent NOAA NBH/NBS station cards")


def _append_observations(event, observations):
    lower, upper = map(parse_time, (event["climate_starts_at"], event["climate_ends_at"]))
    event["observations"].extend(item for item in observations if lower <= parse_time(item["at"]) < upper)


def _gap(book_at, reason):
    return {"at": iso(book_at), "bids": None, "offers": None, "reason": reason, "depth_kind": "gap"}


def _error_message(error):
    return f"HTTP {error.code}" if isinstance(error, HTTPError) else str(error)


def _fetch_book(get, bucket, clock):
    slug = bucket["id"]
    url = GATEWAY + f"/v1/markets/{slug}/book"
    try:
        reply = get(url)
        age = int(reply.headers["age"]) if reply.headers.get("age") is not None else None
        result = parse_book(reply.json(), slug, reply.at, cache_age=age)
        cache_fields = ("date", "age", "cache-control", "expires", "last-modified", "cf-cache-status")
        cache_headers = {field: reply.headers[field] for field in cache_fields if field in reply.headers}
        if cache_headers:
            result["http_cache"] = cache_headers
        if reply.requested_at:
            result["requested_at"] = iso(reply.requested_at)
            result["request_seconds"] = (reply.at - reply.requested_at).total_seconds()
        return result
    except (KeyError, TypeError, ValueError, HTTPError, URLError, OSError) as error:
        return _gap(clock(), _error_message(error))


def _get_settlement(get, slug):
    if MARKET_SLUG.fullmatch(slug) is None:
        raise ValueError("Invalid settlement symbol")
    reply = get(GATEWAY + f"/v1/markets/{slug}/settlement")
    payload = reply.json()
    if payload.get("slug") != slug:
        raise ValueError("Settlement identity does not match requested slug")
    payout = decimal(payload["settlement"])
    if not 0 <= payout <= 1:
        raise ValueError("Settlement payout outside [0, 1]")
    return {
        "yes_payout": float(payout), "available_at": iso(reply.at), "settled_at": None,
        "source": GATEWAY + f"/v1/markets/{slug}/settlement",
    }


def _settlement_targets(previous, now):
    earlier_events = previous.get("events", []) if previous else []
    for event in sorted(earlier_events, key=lambda item: (item["date"], item["station"] != "KNYC")):
        if not parse_time(event["climate_ends_at"]) <= now:
            continue
        for bucket in event["buckets"]:
            if not bucket.get("settlement"):
                yield event["id"], bucket["id"]


def capture(get, *, kind, duration_seconds, poll_seconds, max_rounds, max_events,
            previous=None, clock=None, monotonic=time.monotonic, sleep=time.sleep):
    """Collect weather references, live depth images and recently available settlements."""
    if kind not in {"live", "fixture"}:
        raise ValueError("Unknown provenance")
    if not (0 < duration_seconds <= 180 and 0 < poll_seconds <= 60
            and 1 <= max_rounds <= 12 and 1 <= max_events <= 12):
        raise ValueError("Capture must stay within the documented command bounds")
    clock = clock or (lambda: datetime.now(timezone.utc))
    started = clock()
    deadline = monotonic() + duration_seconds
    issues = []
    events = []
    settlements = {}
    try:
        catalog = get(DISCOVERY).json()
    except (TypeError, ValueError, HTTPError, URLError, OSError) as error:
        catalog = {}
        issues.append({"source": DISCOVERY, "reason": _error_message(error)})
    for entry in catalog.get("events", []):
        if not str(entry.get("slug", "")).startswith("temp-") or "high-" not in entry["slug"]:
            continue
        try:
            event = parse_event(entry)
            if parse_time(event["ends_at"]) > started and any(b["exchange_status"] in OPEN for b in event["buckets"]):
                events.append(event)
        except (TypeError, ValueError, KeyError) as error:
            issues.append({"source": entry.get("slug"), "reason": _error_message(error)})
    events.sort(key=lambda event: (event["date"], event["station"] != "KNYC", event["station"]))
    if len(events) > max_events:
        issues.append({"source": DISCOVERY, "reason": f"Session limit omitted {len(events) - max_events} daily-high events"})
        events = events[:max_events]
    if len(catalog.get("events", [])) == 50:
        issues.append({"source": DISCOVERY, "reason": "Catalog reached the bounded page limit"})
    stations = sorted({event["station"] for event in events})
    buckets = [bucket for event in events for bucket in event["buckets"]]
    rounds = 0
    observations = {station: [] for station in stations}
    forecasts = {}
    with ThreadPoolExecutor(max_workers=8) as weather, ThreadPoolExecutor(max_workers=6) as exchange:
        def weather_jobs():
            jobs = {f"NWS {station}": weather.submit(_nws_obs, get, station) for station in stations}
            if stations:
                jobs["AWC"] = weather.submit(_awc_obs, get, stations)
            return jobs

        weather_pending = weather_jobs()
        weather_next = monotonic() + 60
        forecast_pending = weather.submit(_latest_forecasts, get, started, stations, events)
        settlement_pending = {}
        for index, (event_id, slug) in enumerate(_settlement_targets(previous, started)):
            if index == SETTLEMENT_LIMIT:
                issues.append({"source": GATEWAY, "reason": "Session settlement request limit reached"})
                break
            settlement_pending[(event_id, slug)] = exchange.submit(_get_settlement, get, slug)
        while buckets and rounds < max_rounds and monotonic() < deadline:
            round_start = monotonic()
            if weather_pending and all(job.done() for job in weather_pending.values()):
                _collect_observations(weather_pending, observations, issues)
                weather_pending = {}
            if not weather_pending and round_start >= weather_next:
                weather_pending = weather_jobs()
                weather_next = round_start + 60
            jobs = [exchange.submit(_fetch_book, get, bucket, clock) for bucket in buckets]
            rounds += 1
            for bucket, job in zip(buckets, jobs):
                book = job.result()
                book["round"] = rounds
                bucket["books"].append(book)
            remaining = min(deadline, round_start + poll_seconds) - monotonic()
            if remaining > 0 and rounds < max_rounds:
                sleep(remaining)

        _collect_observations(weather_pending, observations, issues)
        try:
            forecasts = forecast_pending.result()
        except (KeyError, ValueError, HTTPError, URLError, OSError) as error:
            issues.append({"source": AWS_NBM, "reason": _error_message(error)})
        for (event_id, slug), future in settlement_pending.items():
            try:
                settled = future.result()
            except HTTPError as error:
                if error.code != 404:
                    issues.append({"source": slug, "reason": _error_message(error)})
            except (KeyError, ValueError, URLError, OSError) as error:
                issues.append({"source": slug, "reason": _error_message(error)})
            else:
                settlements.setdefault(event_id, {})[slug] = settled

    for event in events:
        station = event["station"]
        _append_observations(event, observations[station])
        event["observations"] = _merge_observations([], event["observations"])
        for cycle in forecasts.get(station, []):
            forecast = forecast_for_day(cycle, event["date"], event["standard_utc_offset_hours"])
            if forecast:
                event["forecasts"].append(forecast)
        if not event["forecasts"]:
            issues.append({"source": event["id"], "reason": "No matching NOAA forecast"})
        for bucket in event["buckets"]:
            mark_book_progress(bucket["books"])
    return {
        "version": SCHEMA_VERSION, "kind": kind, "started_at": iso(started),
        "captured_at": iso(clock()), "poll_seconds": poll_seconds, "rounds": rounds,
        "events": events, "settlements": settlements, "issues": issues,
    }


def _collect_observations(jobs, observations, issues):
    for source, job in jobs.items():
        try:
            result = job.result()
            if source == "AWC":
                for station, readings in result.items():
                    observations[station].extend(readings)
            else:
                observations[source.removeprefix("NWS ")].extend(result)
        except (KeyError, ValueError, HTTPError, URLError, OSError) as error:
            issues.append({"source": source, "reason": _error_message(error)})


def _merge_observations(old, new):
    # Keep corrected values as separate receipts so they cannot affect earlier
    # frames. Identical observations reuse their earliest known receipt.
    candidates = sorted(old + new, key=lambda item: parse_time(item["available_at"]))
    last = {}
    readings = []
    for observation in candidates:
        key = (observation["at"], observation["source"])
        content = tuple(observation.get(field) for field in ("temp_f", "report", "dewpoint_f", "wind_speed_mps"))
        if content != last.get(key):
            last[key] = content
            readings.append(observation)
    return sorted(readings, key=lambda item: (parse_time(item["at"]), parse_time(item["available_at"])))


def write_capture(record, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = parse_time(record["captured_at"]).strftime("%Y%m%dT%H%M%S")
    path = directory / f"{timestamp}-{uuid4().hex[:8]}.json"
    temporary = path.with_suffix(".tmp")
    try:
        temporary.write_text(json.dumps(record, separators=(",", ":"), allow_nan=False) + "\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def _validate_capture(body):
    report = json.loads(body)
    if not isinstance(report, dict):
        raise ValueError("Capture must be a JSON object")
    parse_time(report["captured_at"])
    parse_time(report["started_at"])
    if not isinstance(report.get("issues", []), list) or not isinstance(report.get("events", []), list):
        raise ValueError("Malformed capture sections")
    for event in report.get("events", []):
        for key in ("id", "native_id", "date", "station", "ends_at", "climate_ends_at", "buckets", "observations", "forecasts"):
            if key not in event:
                raise ValueError(f"Capture event missing {key}")
        parse_time(event["ends_at"])
        parse_time(event["climate_ends_at"])
        for datum in event["observations"] + event["forecasts"]:
            parse_time(datum["available_at"])
        for bucket in event["buckets"]:
            for key in ("id", "market_id", "yes_side_id", "no_side_id", "lower_f", "upper_f", "quantity_step", "books"):
                if key not in bucket:
                    raise ValueError(f"Capture bucket missing {key}")
            for book in bucket["books"]:
                parse_time(book["at"])
                if book.get("source_at"):
                    parse_time(book["source_at"])
                for side in ("bids", "offers"):
                    levels = book.get(side)
                    if levels is not None:
                        if not isinstance(levels, list):
                            raise ValueError("Malformed capture book ladder")
                        for price, shares in levels:
                            if not 0 <= decimal(price) <= 1 or decimal(shares) < 0:
                                raise ValueError("Malformed capture book level")
    for payouts in report.get("settlements", {}).values():
        for value in payouts.values():
            if not 0 <= decimal(value["yes_payout"]) <= 1:
                raise ValueError("Capture payout outside [0, 1]")
            parse_time(value["available_at"])
    return report


class _CaptureMerge:
    def __init__(self, *, fixture=False, versioned_windows=False):
        self.fixture = fixture
        self.event_identity = ("native_id", "station", "date", "climate_starts_at", "climate_ends_at") if versioned_windows else (
            "native_id", "station", "date", "ends_at"
        )
        self.sessions = []
        self.events = {}
        self.buckets = {}
        self.settlements = {}
        self.conflicts = []
        self.finished = {}
        self.dirty = set()
        self.latest = None

    def add(self, receipts):
        summary_fields = ("started_at", "captured_at", "rounds", "grids", "clouds", "winds")
        bucket_identity = ("market_id", "yes_side_id", "no_side_id", "lower_f", "upper_f", "quantity_step")
        witness_fields = {"source_time_progress", "available_at", "capture_file", "capture_sha256"}
        for filename, _, digest, _, raw in receipts:
            report = self.latest = json.loads(raw)
            self.sessions.append({field: report[field] for field in summary_fields if field in report})
            witness = {} if self.fixture else {
                "available_at": report["captured_at"], "capture_file": filename, "capture_sha256": digest.hex(),
            }
            for event in report.get("events", []):
                event_id = event["id"]
                prior = self.events.get(event_id)
                if prior is None:
                    prior = self.events[event_id] = {**event, "buckets": [], "forecasts": [], "observations": []}
                    self.buckets[event_id] = {}
                elif any(event.get(key) != prior.get(key) for key in self.event_identity):
                    self.conflicts.append({"source": event_id, "reason": "Conflicting captured event identity"})
                    continue
                self.dirty.add(event_id)
                for bucket in event["buckets"]:
                    bucket_id = bucket["id"]
                    old_bucket = self.buckets[event_id].get(bucket_id)
                    if old_bucket is None:
                        old_bucket = self.buckets[event_id][bucket_id] = {**bucket, "books": {}}
                    elif any(bucket[key] != old_bucket[key] for key in bucket_identity):
                        self.conflicts.append({"source": bucket_id, "reason": "Conflicting captured market identity"})
                        continue
                    indexed = old_bucket["books"]
                    for book in bucket["books"]:
                        original = indexed.get(book["at"])
                        if original is None:
                            indexed[book["at"]] = {**book, **witness}
                        elif {key: value for key, value in original.items() if key not in witness_fields} != {
                            key: value for key, value in book.items() if key not in witness_fields
                        }:
                            self.conflicts.append({"source": bucket_id, "reason": "Conflicting book receipt at an existing timestamp"})
                prior["observations"].extend(event["observations"])
                prior["forecasts"].extend(event["forecasts"])
            for event_id, payouts in report.get("settlements", {}).items():
                self.dirty.add(event_id)
                for bucket_id, settlement in payouts.items():
                    self.settlements.setdefault(event_id, {}).setdefault(bucket_id, settlement)

    def finish(self, errors, heads=None):
        if self.latest is None:
            return None
        heads = heads or {}
        fields = ("issued_at", "high_source", "low_f", "low_sd_f", "low_valid_at", "low_availability_basis",
                  "short_range_issued_at", "short_range_availability_basis")
        for event_id in self.dirty:
            event = self.events.get(event_id)
            if event is None:
                continue
            by_cycle = {}
            for cycle in sorted(event["forecasts"], key=lambda item: parse_time(item["available_at"])):
                by_cycle.setdefault(tuple(cycle.get(key) for key in fields), cycle)
            buckets = []
            payouts = self.settlements.get(event_id, {})
            for bucket_id, bucket in self.buckets[event_id].items():
                # Progress marks can change when a later receipt supplies an older book.
                # Publish new dictionaries so an in-flight reader retains its own marks.
                books = sorted((book.copy() for book in bucket["books"].values()), key=lambda book: parse_time(book["at"]))
                prior = heads.get((event_id, bucket_id))
                mark_book_progress([prior, *books] if prior else books)
                bucket["books"].update((book["at"], book) for book in books)
                visible = {**bucket, "books": books}
                if bucket_id in payouts:
                    visible["settlement"] = payouts[bucket_id]
                buckets.append(visible)
            self.finished[event_id] = {
                **event, "buckets": buckets, "forecasts": list(by_cycle.values()),
                "observations": _merge_observations([], event["observations"]),
            }
        self.dirty.clear()
        return {**self.latest, "sessions": self.sessions.copy(), "session_count": len(self.sessions),
                "started_at": self.sessions[0]["started_at"], "events": [self.finished[key] for key in self.events],
                "issues": errors + list(self.latest.get("issues", [])) + self.conflicts}


class CaptureSnapshot:
    """Reuse an assembled live tape across atomic publications, under the caller's lock."""

    def __init__(self, directory):
        self.directory = directory
        self.revision = None
        self.receipts = []
        self.merge = None
        self.record = None

    def load(self, revision):
        if self.revision == revision:
            return self.record
        extends, rows, errors = capture_index.read_snapshot(
            self.directory, _validate_capture, version=SCHEMA_VERSION, previous=self.receipts,
        )
        merge = self.merge if extends else _CaptureMerge(versioned_windows=True)
        receipts = self.receipts if extends else []
        # If assembly fails, the next reader rebuilds from the index instead of
        # reusing a partially updated reducer. Published records remain readable.
        self.revision, self.receipts, self.merge, self.record = None, [], None, None
        merge.add(rows)
        record = merge.finish(errors)
        receipts.extend(row[:4] for row in rows)
        self.revision, self.receipts, self.merge, self.record = revision, receipts, merge, record
        return record


def load_captures(directory=None, *, fixture=False, since=None, through=None, versioned_windows=False, instrument=None):
    """Merge indexed sessions by identity, optionally limiting the receipt clock."""
    directory = Path(directory or (FIXTURE_DIR if fixture else LIVE_DIR))
    kind = "fixture" if fixture else "live"
    receipts, errors = capture_index.read(directory, _validate_capture, kind=kind,
                                          version=SCHEMA_VERSION, since=since, through=through, instrument=instrument)
    if not receipts:
        return None
    merge = _CaptureMerge(fixture=fixture, versioned_windows=versioned_windows)
    merge.add(receipts)
    exact = not fixture and since is not None and through is not None and parse_time(since) == parse_time(through)
    before = merge.sessions[0]["started_at"] if exact else since
    heads = None
    if since is not None:
        events = ({**event, "buckets": merge.buckets[event_id].values()} for event_id, event in merge.events.items())
        heads = capture_index.preceding_books(directory, events, before=before, kind=kind, version=SCHEMA_VERSION)
    record = merge.finish(errors, heads)
    if exact:
        earlier = capture_index.earlier_book_captures(directory, record["events"], before=since, version=SCHEMA_VERSION)
        for event in record["events"]:
            for bucket in event["buckets"]:
                for book in bucket["books"]:
                    if proof := earlier.get((event["id"], bucket["id"], book["at"])):
                        book.update({"selected_capture_file": book["capture_file"],
                                     "selected_capture_sha256": book["capture_sha256"], **proof})
    return record


def _grid_worker(arguments, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return {"frames": [], "errors": {"capture": "Grid phase reached the capture deadline"}}
    try:
        result = subprocess.run([sys.executable, *arguments], capture_output=True, text=True,
                                timeout=remaining, check=False)
        return json.loads(result.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        return {"frames": [], "errors": {"capture": f"Grid cache unavailable ({type(error).__name__})"}}


def _capture_grids(deadline, include_clouds):
    temperature = _grid_worker(["-m", "weather_edge.grids", "live"], deadline)
    result = {"grids": temperature}
    if include_clouds and temperature.get("frames"):
        worker = ("import json, sys; from weather_edge.clouds import cache_matching; "
                  "print(json.dumps(cache_matching(sys.argv[1:])))")
        result["clouds"] = _grid_worker(["-c", worker, *temperature["frames"]], deadline)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", action="store_true", help="Offline synthetic session in .data/fixtures")
    parser.add_argument("--duration-seconds", type=int, default=45)
    parser.add_argument("--poll-seconds", type=int, default=8)
    parser.add_argument("--max-rounds", type=int, default=8)
    parser.add_argument("--max-events", type=int, default=12)
    parser.add_argument("--with-grids", action="store_true", help="Cache NOAA GFS globe temperature grids")
    parser.add_argument("--with-clouds", action="store_true", help="Cache NOAA GFS temperature and cloud-cover grids")
    args = parser.parse_args(argv)
    duration, poll = args.duration_seconds, args.poll_seconds
    if not (10 <= duration <= 180 and 5 <= poll <= 60
            and 1 <= args.max_rounds <= 12 and 1 <= args.max_events <= 12):
        parser.error("Use duration 10–180s, poll interval 5–60s, 1–12 rounds and 1–12 events")
    options = {"duration_seconds": duration, "poll_seconds": poll,
               "max_rounds": args.max_rounds, "max_events": args.max_events}
    if args.fixture:
        if args.with_grids or args.with_clouds:
            parser.error("The offline fixture does not fetch NOAA grids")
        from .capture_fixture import fixture_capture
        record = fixture_capture(**options)
        directory = FIXTURE_DIR
    else:
        previous = load_captures()
        deadline = time.monotonic() + duration
        transport = ReadOnlyHTTP(deadline)
        with ThreadPoolExecutor(max_workers=1) as grid_executor:
            grid_job = (grid_executor.submit(_capture_grids, deadline, args.with_clouds)
                        if args.with_grids or args.with_clouds else None)
            record = capture(transport, kind="live", previous=previous, **options)
            if grid_job:
                record.update(grid_job.result())
        directory = LIVE_DIR
    path = write_capture(record, directory)
    images = sum(
        book["depth_kind"] == "displayed_ladder"
        for event in record["events"] for bucket in event["buckets"] for book in bucket["books"]
    )
    print(f"{record['kind']} capture {path} | {len(record['events'])} events | {record['rounds']} rounds | {images} depth images")
    for layer in ("grids", "clouds"):
        if layer in record:
            result = record[layer]
            print(f"{layer}: {len(result.get('frames', []))} NOAA frames, {len(result.get('errors', {}))} gaps")
    for issue in record["issues"]:
        print(f"{issue['source']}: {issue['reason']}")
    return 0 if images else 1


if __name__ == "__main__":
    raise SystemExit(main())
