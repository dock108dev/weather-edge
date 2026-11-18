"""Backfill and explore independent historical station days."""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import re
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from . import expansion_replay_enrichment, history
from .market import parse_event
from .nbm import climate_window, iso, utc

DEFAULT_DATA = Path(".data")
SOURCE_ERRORS = (OSError, ValueError, KeyError, TypeError, AttributeError, EOFError, OverflowError)
CLI_CODES = {"KNYC": ("KOKX", "CLINYC"), "KMIA": ("KMFL", "CLIMIA"), "KLAX": ("KLOX", "CLILAX")}
CLI_ID = re.compile(r"\d{12}-K[A-Z]{3}-CDUS\d{2}-CLI[A-Z]{3}(?:-[A-Z]{3})?")
STATION_ID = re.compile(r"K[A-Z0-9]{3}")
CLI_DATE = re.compile(r"CLIMATE SUMMARY FOR ([A-Z]+)\s+(\d{1,2})\s+(\d{4})\b")
MONTHS = {name: index for index, name in enumerate((
    "", "JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY",
    "AUGUST", "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER",
))}
TRACE_FIELDS = {
    "tmpf": ("temp_f", "°F"), "dwpf": ("dewpoint_f", "°F"),
    "sknt": ("wind_knots", "knots"), "drct": ("wind_direction_deg", "degrees"),
    "precip": ("precip_1m_in", "inches"),
}
MARKET_FIELDS = (
    "id", "native_id", "title", "series", "weather_only", "market_source", "market_url",
    "market_end_at", "market_event_ends_at", "settlement_time_kind", "settles_at",
    "trading_closed_at", "buckets", "rules_source", "weather_faq_source", "price_docs_source",
)
PAYOUT_FIELDS = (
    "winning_bucket", "yes_payout", "no_payout", "payout_available_at",
    "payout_report_source", "payout_report_row",
)


def _day(value):
    return value if isinstance(value, date) else date.fromisoformat(value)


def date_range(start, end):
    start, end = _day(start), _day(end)
    if start > end:
        raise ValueError("Start date must precede or equal end date")
    return (start + timedelta(days=n) for n in range((end - start).days + 1))


def _issue(source, error):
    return {"source": source, "reason": f"HTTP {error.code}" if isinstance(error, HTTPError) else str(error)}


def _spec(series, day):
    original = history.EVENTS[series]
    return replace(original, day=day, slug=f"{original.slug.removesuffix(str(original.day))}{day}")


def _fetch_json(path, url, refresh=False):
    return json.loads(history._fetch(path, url, refresh))


def _write_json(path, value):
    history._write(path, (json.dumps(value, separators=(",", ":"), allow_nan=False) + "\n").encode())


def _native_station_day(spec):
    start, end = climate_window(spec.day, spec.standard_offset)
    return {
        "id": f"weather-{spec.station.lower()}-{spec.day}",
        "title": f"{spec.station_name} weather",
        "station": spec.station,
        "station_name": spec.station_name,
        "lat": spec.lat,
        "lon": spec.lon,
        "date": str(spec.day),
        "timezone": spec.timezone,
        "standard_utc_offset_hours": spec.standard_offset,
        "starts_at": iso(start),
        "ends_at": iso(end),
        "climate_starts_at": iso(start),
        "climate_ends_at": iso(end),
        "settles_at": None,
        "buckets": [],
        "forecasts": [],
        "observations": [],
        "official_reports": [],
        "weather_only": True,
        "issues": [],
    }


def _parse_cli(text, product, spec, ends):
    identity = CLI_ID.fullmatch(product)
    month_day = CLI_DATE.search(text)
    if not identity or not month_day:
        return None
    month, day, year = month_day.groups()
    if date(int(year), MONTHS[month], int(day)) != spec.day or "YESTERDAY" not in text:
        return None
    published = datetime.strptime(product[:12], "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
    if published < ends:
        return None
    high = re.search(r"^\s+MAXIMUM\s+(-?\d+)\s+(?:(\d+)\s+([AP]M))?", text, re.MULTILINE)
    if not high:
        return None
    low = re.search(r"^\s+MINIMUM\s+(-?\d+)\s+", text, re.MULTILINE)
    rain = re.search(r"PRECIPITATION \(IN\)\s+YESTERDAY\s+(\d+(?:\.\d+)?|T|MM)\b", text)
    snow = re.search(r"SNOWFALL \(IN\)\s+YESTERDAY\s+(\d+(?:\.\d+)?|T|MM)\b", text)
    def amount(match):
        return match[1] if match and match[1] == "T" else float(match[1]) if match and match[1] != "MM" else None
    return {
        "available_at": iso(published),
        "high_f": int(high[1]),
        "low_f": int(low[1]) if low else None,
        "precip_in": amount(rain),
        "snow_in": amount(snow),
        "high_at_local_standard": f"{high[2]} {high[3]}" if high[2] else None,
        "source": f"{history.IEM}/api/1/nwstext/{product}",
        "product": product,
    }


def _official_reports(spec, ends, cache, refresh, issues):
    station, year = spec.station, spec.day.year
    key = cache / "archive-raw" / "cli" / station
    summary_url = f"{history.IEM}/json/cli.py?" + urlencode({"station": station, "year": year})
    final = None
    products = set()
    try:
        summary = _fetch_json(key / f"summary-{year}.json", summary_url, refresh)
        final = next((row for row in summary["results"] if row["valid"] == str(spec.day)
                      and row["station"] == station), None)
        if final:
            products.add(final["product"])
    except SOURCE_ERRORS as exc:
        issues.append(_issue("IEM CLI summary", exc))
    cccc, pil = CLI_CODES[station]
    # Include the next UTC date for late complete reports or corrections. The
    # annual summary can identify a final revision even outside this search.
    for day in (ends.date(), ends.date() + timedelta(days=1)):
        url = f"{history.IEM}/api/1/nws/afos/list.json?" + urlencode({"cccc": cccc, "pil": pil, "date": day})
        try:
            payload = _fetch_json(key / f"products-{day}.json", url, refresh=refresh)
            products.update(row["product_id"] for row in payload["data"]
                            if row["cccc"] == cccc and row["pil"] == pil)
        except SOURCE_ERRORS as exc:
            issues.append(_issue("IEM CLIs by issue time", exc))
    reports = []
    for product in sorted(products):
        if not CLI_ID.fullmatch(product) or product.split("-")[1] != cccc or product.split("-")[3] != pil:
            continue
        try:
            url = f"{history.IEM}/api/1/nwstext/{product}"
            text = history._fetch(key / f"{product}.txt", url, refresh).decode()
            if report := _parse_cli(text, product, spec, ends):
                reports.append({**report, "summary_source": summary_url})
        except SOURCE_ERRORS as exc:
            issues.append(_issue("IEM CLI product", exc))
    if final and not any(r["product"] == final["product"] and r["high_f"] == final["high"] for r in reports):
        issues.append(_issue("NWS official high", ValueError("Annual CLI summary did not agree with a complete archived product")))
    if not reports:
        issues.append(_issue("NWS official high", ValueError("No complete, date-matched climate product")))
    return reports


def _station_observations(spec, starts, ends, cache, refresh, issues, receipt_policy):
    source_url = history._asos_url(spec, starts, ends, fields=("tmpf", "dwpf", "sknt", "drct", "p01i", "metar"))
    raw_path = cache / "archive-raw" / "asos" / spec.station / f"{spec.day}.csv"
    try:
        text = history._fetch(raw_path, source_url, refresh).decode()
        receipts = history._observation_receipts(spec.station, starts, ends, cache / "madis-metar",
                                                  on_error=lambda message: issues.append({"source": "NOAA MADIS", "reason": message}))
        observations, unmatched = history.parse_observations(
            text, spec.station, starts, ends, receipts, delay_proxy=receipt_policy,
        )
    except SOURCE_ERRORS as exc:
        issues.append(_issue("IEM ASOS", exc))
        return [], 0, source_url
    if unmatched:
        handling = "shown using the stated receipt proxy" if receipt_policy else "left as gaps"
        issues.append({"source": "NOAA MADIS", "reason": f"{unmatched} ASOS observations lack a matching receipt and were {handling}"})
    if not observations:
        issues.append({"source": "IEM ASOS", "reason": "No usable station observations in this climate window"})
    return observations, unmatched, source_url


def _archive_minute_trace(spec, starts, ends, cache, refresh):
    """Delayed NCEI observations for a retrospective research-only trace."""
    params = {"station": spec.station.removeprefix("K"), "vars": list(TRACE_FIELDS), "tz": "UTC",
              "sample": "1min", "what": "download", "delim": "comma", "gis": "no"}
    for suffix, at in ((1, starts), (2, ends)):
        params.update({f"{part}{suffix}": getattr(at, part)
                       for part in ("year", "month", "day", "hour", "minute")})
    url = f"{history.IEM}/cgi-bin/request/asos1min.py?" + urlencode(params, doseq=True)
    path = cache / "archive-raw" / "asos-minute" / spec.station / f"{spec.day}.csv"
    body = history._fetch(path, url, refresh).decode()
    points = _parse_minute_trace(body, spec.station, starts, ends)
    if not points:
        return None
    available = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    return {
        "kind": "delayed_station_observations",
        "source": url,
        "source_name": "IEM / NOAA NCEI one-minute ASOS archive",
        "ingested_at": iso(available),
        "availability_kind": "local archive retrieval; original public receipt unavailable",
        "first_at": points[0]["at"],
        "last_at": points[-1]["at"],
        "count": len(points),
        "maximum_f": max(point["temp_f"] for point in points),
        "minimum_f": min(point["temp_f"] for point in points),
        "cadence_seconds": 60,
        "missing_minutes": int((ends - starts).total_seconds() / 60) - len(points),
        "units": dict(TRACE_FIELDS.values()),
        "points": points,
    }


def _parse_minute_trace(body, station, starts, ends):
    rows = csv.DictReader(io.StringIO(body))
    if not {"station", "valid(UTC)", "tmpf"} <= set(rows.fieldnames or ()):
        raise ValueError("IEM minute archive response lacks station, UTC or temperature columns")
    points = {}
    for row in rows:
        if row["station"] not in (station, station.removeprefix("K")):
            raise ValueError("One-minute row identifies the wrong station")
        at = datetime.strptime(row["valid(UTC)"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        if not starts <= at < ends:
            continue
        point = {"at": iso(at)}
        for key, (label, _) in TRACE_FIELDS.items():
            try:
                number = float(row.get(key) or "nan")
            except ValueError:
                continue
            if math.isfinite(number):
                point[label] = number
        if "temp_f" in point:
            points[at] = point
    return [points[at] for at in sorted(points)]


def _add_minute_trace(row, spec, cache, refresh):
    starts, ends = map(utc, (row["starts_at"], row["ends_at"]))
    try:
        trace = _archive_minute_trace(spec, starts, ends, cache, refresh)
        if trace:
            row["retrospective"] = trace
        else:
            row["issues"].append(_issue("IEM one-minute ASOS", ValueError("No valid temperatures")))
    except SOURCE_ERRORS as exc:
        row["issues"].append(_issue("IEM one-minute ASOS", exc))


def _load_market(spec, data, cache, refresh):
    raw = cache / "replay-raw" / spec.slug
    event_url = f"{history.POLYMARKET}/v1/events/slug/{spec.slug}"
    try:
        native = _fetch_json(raw / "event.json", event_url, refresh)["event"]
    except HTTPError as err:
        if err.code == 404:
            data["issues"].append({"source": "Polymarket US", "reason": "No listing at this station and date"})
            return
        raise
    event = parse_event(native)
    starts, ends = map(utc, (data["starts_at"], data["ends_at"]))
    if event["id"] != spec.slug or event["station"] != spec.station or utc(event["climate_starts_at"]) != starts:
        raise ValueError("Polymarket event does not match the station, date and climate window")
    history._check_bucket_partition(event["buckets"])
    reports = data["official_reports"]
    by_id = {market["slug"]: market for market in native["markets"]}
    final_high = reports[-1]["high_f"] if reports else None
    buckets = event["buckets"]
    for bucket in buckets:
        symbol = bucket["id"]
        raw_market = by_id[symbol]
        bucket["description"] = bucket["rules"]
        bucket["prices"] = []
        bucket["result"] = None
        bucket["settlement_source"] = f"{history.POLYMARKET}/v1/markets/{symbol}/settlement"
        bucket["price_source"] = history.price_history_url(symbol, starts, ends)
        try:
            prices = _fetch_json(raw / f"{symbol}-prices.json", bucket["price_source"], refresh)
            bucket["prices"] = history.parse_prices(prices, starts, ends)
        except SOURCE_ERRORS as exc:
            data["issues"].append(_issue(f"Polymarket US display prices {symbol}", exc))
        if raw_market.get("status") != "MARKET_STATUS_RESOLVED":
            continue
        try:
            payout = _fetch_json(raw / f"{symbol}-settlement.json", bucket["settlement_source"], refresh)
            if payout["slug"] != symbol:
                raise ValueError("Settlement response identifies another contract")
            value = history._price(payout["settlement"])
            if value is None:
                raise ValueError("Final settlement did not provide a payout")
            bucket["archived_payout"] = value
            if value in (0, 1):
                bucket["result"] = value == 1
        except SOURCE_ERRORS as exc:
            data["issues"].append(_issue(f"Polymarket US payout {symbol}", exc))
    winners = [bucket for bucket in buckets if bucket["result"] is True]
    final_by_rules = [bucket for bucket in buckets if final_high is not None
                      and history._matches(final_high, bucket["lower_f"], bucket["upper_f"])]
    if len(winners) == 1 and final_by_rules and winners[0]["id"] == final_by_rules[0]["id"]:
        data["settlement"].update({"winning_bucket": winners[0]["id"], "yes_payout": 1, "no_payout": 0})
    elif final_by_rules and any(bucket["result"] is not None for bucket in buckets):
        data["issues"].append({"source": "Payout and NWS high", "reason": "No verified common winning bin"})
    closed = None
    if winners:
        try:
            closed = history._closing_time(winners[0], raw, refresh)
        except SOURCE_ERRORS as exc:
            data["issues"].append(_issue("Polymarket US closed book", exc))
    data.update({
        "id": event["id"],
        "native_id": event["native_id"],
        "title": event["title"],
        "series": native.get("seriesSlug"),
        "weather_only": False,
        "market_source": event_url,
        "market_url": f"https://polymarket.us/event/{spec.slug}",
        "market_end_at": event["settles_at"],
        "market_event_ends_at": event["ends_at"],
        "settlement_time_kind": "Published exchange weather FAQ schedule",
        "settles_at": iso(datetime.combine(spec.day + timedelta(days=1), time(8), ZoneInfo("America/New_York"))),
        "trading_closed_at": closed,
        "buckets": buckets,
        "rules_source": history.RULES,
        "weather_faq_source": history.WEATHER_FAQ,
        "price_docs_source": history.PRICE_DOCS,
    })


def _preserve_weather(prior, data, receipt_policy):
    # Receipt/product clocks distinguish successive reports for one validity time.
    old_obs = [row for row in prior["observations"] if receipt_policy or not row.get("timing_proxy")]
    obs = {(point["at"], point["available_at"], point["raw"]): point
           for point in old_obs + data["observations"]}
    data["observations"] = [obs[key] for key in sorted(obs)]
    forecasts = {(row["issued_at"], row["available_at"]): row
                 for row in prior["forecasts"] + data["forecasts"]}
    data["forecasts"] = [forecasts[key] for key in sorted(forecasts)]
    reports = {row["product"]: row for row in prior.get("official_reports", []) + data["official_reports"]}
    data["official_reports"] = sorted(reports.values(), key=lambda row: row["available_at"])


def _preserve_market(prior, data):
    if not prior.get("weather_only", True) and data["weather_only"]:
        data.update({key: prior[key] for key in MARKET_FIELDS if key in prior})
    old = {bucket["id"]: bucket for bucket in prior["buckets"]}
    for bucket in data["buckets"]:
        before = old.get(bucket["id"], {})
        for lane in ("prices", "books"):
            if before.get(lane) and not bucket.get(lane):
                bucket[lane] = before[lane]
        if "archived_payout" not in bucket and "archived_payout" in before:
            bucket.update({key: before[key] for key in ("archived_payout", "result")})
    prior_settlement = prior.get("settlement", {})
    current_settlement = data.get("settlement", {})
    if current_settlement and prior_settlement.get("high_f") == current_settlement["high_f"]:
        current_settlement.update({key: prior_settlement[key] for key in PAYOUT_FIELDS
                                   if key in prior_settlement and key not in current_settlement})


def build_station_day(spec, cache, *, refresh=False, markets=True, receipt_policy=None, prior=None):
    data = _native_station_day(spec)
    starts, ends = map(utc, (data["starts_at"], data["ends_at"]))
    issues = data["issues"]
    data["observations"], data["observations_missing_receipt"], data["observation_source"] = _station_observations(
        spec, starts, ends, cache, refresh, issues, receipt_policy,
    )
    data["observation_receipt_docs"] = history.MADIS_DOCS
    data["observation_timing"] = "Verified NOAA MADIS receipt" if receipt_policy is None else (
        f"Verified NOAA MADIS receipt where available; proxy of {receipt_policy.total_seconds() / 60:g} minutes otherwise"
    )
    forecasts = history._forecasts(spec, cache / "nbm-cards", starts, ends,
                                   on_error=lambda message: issues.append({"source": "NOAA NBM", "reason": message}))
    data["forecasts"] = [{**forecast, "source_kind": "as_issued_forecast",
                           "availability_kind": "archive publication timestamp"} for forecast in forecasts]
    data["official_reports"] = _official_reports(spec, ends, cache, refresh, issues)
    if prior:
        _preserve_weather(prior, data, receipt_policy)
    if data["official_reports"]:
        data["settlement"] = dict(data["official_reports"][-1])
    if markets:
        try:
            _load_market(spec, data, cache, refresh)
        except SOURCE_ERRORS as exc:
            issues.append(_issue("Polymarket US event", exc))
    if prior:
        _preserve_market(prior, data)
    return data


def _read_event(path, day, station):
    event = json.loads(path.read_text())
    if not isinstance(event, dict) or event.get("date") != str(day) or event.get("station") != station:
        raise ValueError("Date or station does not match the archive filename")
    if not {k for k, v in event.items() if isinstance(v, list)} >= {"observations", "forecasts", "buckets"}:
        raise ValueError("Archive is missing typed weather or market arrays")
    if not all(isinstance(event.get(k), str) and event[k] for k in ("id", "station_name", "timezone")):
        raise ValueError("Archive is missing a station label or identifier")
    if utc(event["starts_at"]) >= utc(event["ends_at"]):
        raise ValueError("Empty climate window")
    for row in event["observations"]:
        utc(row["at"])
        utc(row["available_at"])
    for row in event["forecasts"]:
        for clock in ("issued_at", "available_at", "valid_at"):
            utc(row[clock])
    for row in event.get("minimum_forecasts", []):
        for clock in ("issued_at", "available_at", "valid_at"):
            utc(row[clock])
    for bucket in event["buckets"]:
        if not isinstance(bucket["id"], str) or not isinstance(bucket.get("prices", []), list):
            raise ValueError("Malformed market bucket")
    return event


def coverage(event):
    buckets = event.get("buckets", [])
    settlement = event.get("settlement") or {}
    observed = event.get("observations", [])
    return {
        "observations": len(observed),
        "retrospective_observations": (event.get("retrospective") or {}).get("count", 0),
        "proxy_observations": sum(bool(row.get("timing_proxy")) for row in observed),
        "unmatched_observations": event.get("observations_missing_receipt", 0),
        "forecasts": len(event.get("forecasts", [])),
        "prices": sum(len(bucket.get("prices", [])) for bucket in buckets),
        "books": sum(book.get("depth_kind") == "displayed_ladder" for bucket in buckets for book in bucket.get("books", [])),
        "official_reports": len(event.get("official_reports", [])) or int(bool(settlement.get("available_at"))),
        "payouts": sum(bucket.get("settlement", {}).get("available_at") is not None
                       for bucket in buckets)
                   or (sum(bucket.get("result") is not None for bucket in buckets)
                       if settlement.get("payout_available_at") else 0),
    }


def _legacy(cache):
    path = cache / "replay.json"
    if not path.exists():
        return []
    payload = json.loads(path.read_text())
    return [dict(event, weather_only=False) for event in payload["events"] if isinstance(event, dict)]


def _archive_paths(cache, day=None):
    directory = cache / "archive"
    candidates = (directory / str(day),) if day else sorted(directory.glob("????-??-??"))
    for folder in candidates:
        try:
            key = date.fromisoformat(folder.name)
        except ValueError:
            continue
        for path in sorted(folder.glob("K???.json")):
            if STATION_ID.fullmatch(path.stem):
                yield key, path


def _issue_at(path, message):
    return {"source": str(path.name), "reason": str(message), "date": path.parent.name}


def load_day(cache_dir=DEFAULT_DATA, day=None):
    cache, key = Path(cache_dir), _day(day or history.REPLAY_DAY)
    events, issues = {}, []
    if key == history.REPLAY_DAY:
        try:
            for event in _legacy(cache):
                if event.get("date") == str(key):
                    events[event["station"]] = event
        except SOURCE_ERRORS as exc:
            issues.append(_issue_at(cache / "replay.json", exc))
    for _, path in _archive_paths(cache, key):
        try:
            if path.stem not in events:
                events[path.stem] = _read_event(path, key, path.stem)
        except SOURCE_ERRORS as exc:
            issues.append(_issue_at(path, exc))
    for station, event in events.items():
        enrichment = expansion_replay_enrichment.path_for(cache, key, station)
        if enrichment.is_file():
            try:
                event = events[station] = expansion_replay_enrichment.attach(event, enrichment)
            except SOURCE_ERRORS as exc:
                issues.append(_issue_at(enrichment, exc))
        supplementary = cache / "expansion" / "replay" / str(key) / f"{station}.json"
        if not supplementary.is_file():
            continue
        try:
            low = _read_event(supplementary, key, station)
            same_day = all(utc(low[field]) == utc(event[field]) for field in ("climate_starts_at", "climate_ends_at"))
            if same_day and low.get("standard_utc_offset_hours") == event.get("standard_utc_offset_hours"):
                event["minimum_forecasts"] = low.get("minimum_forecasts", event.get("minimum_forecasts", []))
                event["official_low_reports"] = low.get("official_reports", [])
            else:
                issues.append(_issue_at(supplementary, "Supplementary daily-low climate window differs"))
        except SOURCE_ERRORS as exc:
            issues.append(_issue_at(supplementary, exc))
    ordered_stations = [spec.station for spec in history.EVENTS.values()]
    ordered_stations.extend(sorted(set(events).difference(ordered_stations)))
    ordered = [events[station] for station in ordered_stations if station in events]
    for event in ordered:
        event["coverage"] = coverage(event)
        issues.extend({**issue, "station": event["station"], "date": str(key)} for issue in event.get("issues", []))
    return {"events": ordered, "issues": issues}


def _station_metadata(event):
    return {key: event[key] for key in ("station", "station_name", "id")} | {
        "coverage": coverage(event), "market": not event.get("weather_only", False),
    }


@lru_cache(maxsize=4096)
def _summary(path, key, mtime, size, enrichment=None):
    event = _read_event(path, key, path.stem)
    if enrichment:
        try:
            event = expansion_replay_enrichment.attach(event, enrichment[0])
        except SOURCE_ERRORS:
            pass
    return _station_metadata(event)


@lru_cache(maxsize=3)
def _legacy_summary(cache, mtime, size):
    return [_station_metadata(event) for event in _legacy(cache) if event.get("date") == str(history.REPLAY_DAY)]


def catalog(cache_dir=DEFAULT_DATA):
    cache = Path(cache_dir)
    issues, days = [], {}
    legacy = cache / "replay.json"
    if legacy.exists():
        try:
            st = legacy.stat()
            days[history.REPLAY_DAY] = {entry["station"]: entry for entry in _legacy_summary(cache, st.st_mtime_ns, st.st_size)}
        except SOURCE_ERRORS as exc:
            issues.append(_issue_at(legacy, exc))
    for key, path in _archive_paths(cache):
        try:
            if path.stem in days.get(key, {}):
                continue
            st = path.stat()
            supplement = expansion_replay_enrichment.path_for(cache, key, path.stem)
            try:
                meta = supplement.stat()
                enrichment = supplement, meta.st_mtime_ns, meta.st_size
            except FileNotFoundError:
                enrichment = None
            days.setdefault(key, {})[path.stem] = _summary(path, key, st.st_mtime_ns, st.st_size, enrichment)
        except SOURCE_ERRORS as exc:
            issues.append(_issue_at(path, exc))
    return {"days": [{"date": str(key), "stations": list(entries.values())}
                     for key, entries in sorted(days.items(), reverse=True) if entries], "issues": issues}


def _attach_payout_publication(rows, cache, refresh):
    pending = [row for row in rows if row.get("settlement", {}).get("winning_bucket")
               and not row["settlement"].get("payout_available_at")]
    if not pending:
        return
    for offset in (1, 2):
        source_issues = {}
        try:
            history._report_evidence(pending, cache, refresh, business_day_offset=offset,
                                     on_missing=lambda event, reason: source_issues.update({event["id"]: reason}))
        except SOURCE_ERRORS as exc:
            source_issues.update({event["id"]: _issue("Daily Market Report", exc)["reason"] for event in pending})
        pending = [row for row in pending if not row["settlement"].get("payout_available_at")]
        if not pending:
            return
    for data in pending:
        data["issues"].append({
            "source": "Daily Market Report payout publication",
            "reason": source_issues.get(data["id"], "Final payout value has no verified archive publication time"),
        })


def backfill(cache, start, end, series, *, refresh=False, markets=True, receipt_policy=None, minute_trace=False):
    saved, failures = 0, []
    for key in date_range(start, end):
        rows = []
        jobs = []
        for name in dict.fromkeys(series):
            spec = _spec(name, key)
            path = cache / "archive" / str(key) / f"{spec.station}.json"
            if key == history.REPLAY_DAY and not refresh and not minute_trace:
                try:
                    legacy = next((e for e in _legacy(cache) if e["station"] == spec.station), None)
                except SOURCE_ERRORS:
                    legacy = None
                if legacy:
                    saved += 1
                    print(f"{spec.station} {key}: original replay cache", flush=True)
                    continue
            prior = None
            if path.is_file():
                try:
                    prior = _read_event(path, key, spec.station)
                    if not markets and not prior.get("weather_only", True):
                        if minute_trace and (refresh or not prior.get("retrospective")):
                            _add_minute_trace(prior, spec, cache, refresh)
                            _write_json(path, prior)
                        saved += 1
                        print(f"{spec.station} {key}: retained market-backed station-day", flush=True)
                        continue
                    if not refresh and markets == prior.get("market_lookup_attempted") and prior.get("proxy_minutes") == (
                        receipt_policy.total_seconds()/60 if receipt_policy else None
                    ) and not prior.get("needs_retry"):
                        if minute_trace and not prior.get("retrospective"):
                            _add_minute_trace(prior, spec, cache, refresh)
                            _write_json(path, prior)
                        saved += 1
                        print(f"{spec.station} {key}: cached", flush=True)
                        continue
                except SOURCE_ERRORS:
                    pass
            jobs.append((spec, path, prior))
        # Process stations serially so temperature receipts share the already
        # downloaded hourly files, and small parallel reads remain per source.
        for spec, path, prior in jobs:
            try:
                row = build_station_day(spec, cache, refresh=refresh, markets=markets,
                                        receipt_policy=receipt_policy, prior=prior)
                row["market_lookup_attempted"] = markets
                row["proxy_minutes"] = receipt_policy.total_seconds()/60 if receipt_policy else None
                if prior and prior.get("retrospective"):
                    row["retrospective"] = prior["retrospective"]
                if minute_trace:
                    _add_minute_trace(row, spec, cache, refresh)
                rows.append((row, path))
            except SOURCE_ERRORS as exc:
                failures.append({"station": spec.station, "date": str(key), **_issue("Backfill", exc)})
        if rows:
            _attach_payout_publication([row for row, _ in rows], cache, refresh)
        for row, path in rows:
            if row.get("settlement", {}).get("payout_available_at"):
                for bucket in row["buckets"]:
                    if "archived_payout" in bucket:
                        bucket["settlement"] = {
                            "yes_payout": bucket["archived_payout"],
                            "available_at": row["settlement"]["payout_available_at"],
                            "source": bucket["settlement_source"],
                        }
            row["needs_retry"] = any(
                issue["source"] not in {"NOAA MADIS", "Polymarket US"} or
                not ("were left as gaps" in issue["reason"] or "were shown using the stated" in issue["reason"] or "No listing" in issue["reason"])
                for issue in row["issues"]
            )
            if row["observations"] or row["forecasts"] or row.get("settlement") or row["buckets"]:
                _write_json(path, row)
                saved += 1
                print(f"{row['station']} {key}: {coverage(row)}", flush=True)
            failures.extend({"station": row["station"], "date": str(key), **issue} for issue in row["issues"])
    return {"saved": saved, "issues": failures}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", type=date.fromisoformat)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--series", nargs="+", choices=history.EVENTS, default=list(history.EVENTS))
    parser.add_argument("--refresh", action="store_true", help="Refresh exchange prices, station extracts and CLI listings")
    parser.add_argument("--weather-only", action="store_true")
    parser.add_argument("--receipt-proxy-minutes", type=int, help="Optional disclosed lag for unmatched historical observations")
    parser.add_argument("--minute-trace", action="store_true", help="Include separate delayed, retrospective ASOS measurements")
    args = parser.parse_args()
    if args.receipt_proxy_minutes is not None and args.receipt_proxy_minutes < 1:
        parser.error("Receipt proxy needs a positive minute count")
    try:
        result = backfill(args.data_dir, args.start, args.end or args.start, args.series, refresh=args.refresh,
                          markets=not args.weather_only, receipt_policy=timedelta(minutes=args.receipt_proxy_minutes)
                          if args.receipt_proxy_minutes else None, minute_trace=args.minute_trace)
    except ValueError as exc:
        parser.error(str(exc))
    print(f"{result['saved']} station-days available; {len(result['issues'])} gaps or source issues")
    for issue in result["issues"]:
        print(f"{issue['date']} {issue['station']} {issue['source']}: {issue['reason']}")
    return 0 if result["saved"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
