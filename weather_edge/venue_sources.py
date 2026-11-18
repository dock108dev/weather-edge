"""Credential-free venue catalog acquisition and targeted live book receipts."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from fcntl import LOCK_EX, flock
from http.client import HTTPException
import json
from pathlib import Path
import time
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, build_opener

from .capture import ImmutableOrigin, JSON_CAP, ReadOnlyHTTP, _error_message
from .collector import paused
from .market import decimal, iso, mark_book_progress, parse_book
from .venue_archive import (
    DEFAULT_DATA, KALSHI, KALSHI_PRIORITY, POLY, POLY_LOW_RAIN_PRIORITY,
    book_batch_target, connect, coverage, export_file, save_event, save_markets,
    raw_receipt, save_receipt, save_series, save_settlement, save_book, save_book_batch, save_metadata, set_progress,
)
from .venue_contracts import (
    INDEX_CITIES, KALSHI_API, POLY_API, STATION_OFFSETS, kalshi_family, kalshi_market, kalshi_ticker_matches_event, native_id, poly_event,
    poly_market, poly_settlement,
)

MAX_BODY = 9 * 2**20
KALSHI_SERIES = "/series"
KALSHI_CATEGORY = "Climate and Weather"
POLY_WEATHER = "/v2/tags/weather/events"
SCAN_LIMIT = 500
POLY_LIMIT = 100
BOOK_LIMIT = 72
POLY_WEATHER_BOOK_LIMIT = 16
ROTATING_SERIES_LIMIT = 18
SERIES_REFRESH = timedelta(hours=6)
ORIGINS = {KALSHI: KALSHI_API, POLY: POLY_API}
PACE = {KALSHI: 0.4, POLY: 0.25}
PREFERRED_SERIES = (
    "KXHIGHNY", "KXHIGHMIA", "KXHIGHLAX", "KXHIGHPHIL", "KXHIGHCHI", "KXHIGHTSFO", "KXHIGHDEN",
    "KXLOWTNYC", "KXLOWTCHI", "KXRAINDNYC", "KXRAINDLAX", "KXRAINDCHI", "KXHIGHAUS", "KXHIGHTSEA",
)
CATALOG_PRIORITY_SERIES = (*PREFERRED_SERIES, "KXRAIN", *INDEX_CITIES)
POLY_BOOK_GROUPS = (("daily-low", "KNYC"), ("daily-low", "KMDW"), ("daily-rain", "KNYC"), ("daily-rain", "KMDW"))
KALSHI_HIGH_BOOK_SERIES = PREFERRED_SERIES[:6]
KALSHI_LOW_BOOK_SERIES = ("KXLOWTNYC", "KXLOWTCHI")
KALSHI_RAIN_BOOK_STATIONS = ("KNYC", "KMDW", "KORD", "KLAX", "KMIA", "KPHL", "KAUS", "KDFW")
PUBLIC_HEADERS = {
    "age", "cache-control", "cf-cache-status", "date", "etag", "expires", "last-modified", "server-timing",
    "x-cache", "x-cache-hits", "content-type", "retry-after", "via", "x-amz-cf-pop",
}
EXCEPTIONS = (OSError, HTTPException, ValueError, KeyError, TypeError)


class CollectionPaused(Exception):
    pass


def clock():
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Receipt:
    source: str
    requested_at: str
    received_at: str
    status: int
    headers: dict
    body: bytes

    def json(self):
        if self.status != 200:
            raise ValueError(f"HTTP {self.status}")
        payload = json.loads(self.body)
        if not isinstance(payload, dict):
            raise ValueError("Unexpected venue JSON type")
        return payload


class PublicGET:
    def __init__(self, data, *, timeout=9):
        self.data = data
        self.pacing_dir = Path(data) / "venues"
        self.pacing_dir.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self.opener = build_opener(ImmutableOrigin())

    def _pace(self, venue, *, cooldown=None):
        with (self.pacing_dir / f"{venue}.pace").open("a+") as handle:
            flock(handle, LOCK_EX)
            handle.seek(0)
            try:
                due = min(float(handle.read()), time.time() + 60)
            except ValueError:
                due = 0
            if cooldown is None:
                while due > time.time():
                    if paused(self.data):
                        raise CollectionPaused
                    time.sleep(min(0.25, max(0, due - time.time())))
                if paused(self.data):
                    raise CollectionPaused
                due = time.time() + PACE[venue]
            else:
                due = max(due, time.time() + cooldown)
            handle.seek(0)
            handle.truncate(0)
            handle.write(str(due))
            handle.flush()

    def __call__(self, venue, path, params=()):
        if not path.startswith("/") or path.startswith("//") or "?" in path:
            raise ValueError("Expected known-origin API path")
        self._pace(venue)
        url = ORIGINS[venue] + path
        if params:
            url += "?" + urlencode(params, doseq=True)
        request = Request(url, method="GET", headers={
            "User-Agent": "WeatherEdge/0.1 local public market research",
            "Accept": "application/json", "Accept-Encoding": "identity", "Cache-Control": "no-cache",
        })
        requested_at = iso(clock())
        self.last_requested_at = requested_at
        try:
            response = self.opener.open(request, timeout=self.timeout)
        except HTTPError as error:
            response = error
            if error.code == 429:
                retry = error.headers.get("Retry-After", "")
                retry = min(30, max(2, int(retry))) if retry.isdigit() else 5
                self._pace(venue, cooldown=retry)
        with response:
            content = response.read(MAX_BODY + 1)
            if len(content) > MAX_BODY:
                raise ValueError(f"Response exceeded public fetch bound at {url}")
            headers = {
                key.lower(): ", ".join(response.headers.get_all(key)) for key in response.headers
                if key.lower() in PUBLIC_HEADERS
            }
            return Receipt(url, requested_at, iso(clock()), response.code, headers, content)


def _rows(payload, key):
    rows = payload[key]
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError(f"Invalid {key} collection")
    return rows


def _prepare_poly(events, receipt):
    for event in events:
        event_id, series_id, title, family = poly_event(event)
        raw_rows = _rows(event, "markets") if event.get("markets") is not None else []
        yield (event_id, series_id, title, family, event,
               [poly_market(event, market, receipt.source) for market in raw_rows], raw_rows)


def _apply_poly(db, receipt, receipt_id, events):
    for event_id, series_id, title, family, event, rows, raw_rows in _prepare_poly(events, receipt):
        save_series(db, POLY, series_id, family, series_id, {"seriesSlug": series_id}, receipt_id, placeholder=True)
        save_event(db, POLY, event_id, series_id, title,
                   {key: value for key, value in event.items() if key != "markets"}, receipt_id)
        save_markets(db, rows, raw_rows, receipt, receipt_id)


def _metadata_reply(data, get, venue, path, key, params=()):
    reply = get(venue, path, params)
    with connect(data) as db, db:
        receipt_id = save_receipt(db, venue, key, reply)
        if reply.status == 200:
            save_metadata(db, venue, key, reply.json(), receipt_id)
    return reply


def _seed_series(data, get):
    reply = get(KALSHI, KALSHI_SERIES, {"category": KALSHI_CATEGORY})
    with connect(data) as db, db:
        receipt_id = save_receipt(db, KALSHI, "series", reply)
        if reply.status == 200:
            rows = _rows(reply.json(), "series")
            for series in rows:
                if series.get("category") != KALSHI_CATEGORY:
                    continue
                native = native_id(series["ticker"])
                save_series(db, KALSHI, native, kalshi_family(series), series.get("title"), series, receipt_id)
            set_progress(db, KALSHI, KALSHI_CATEGORY, "series", "", 1, len(rows), "complete", reply.received_at)
        else:
            set_progress(db, KALSHI, KALSHI_CATEGORY, "series", "", 1, 0, "error", reply.received_at,
                         f"HTTP {reply.status}")
    return _known_series(data)


def _known_series(data):
    with connect(data) as db:
        series = [json.loads(row[0]) for row in db.execute("SELECT metadata FROM series WHERE venue=?", (KALSHI,))]
    priority = {series_id: index for index, series_id in enumerate(PREFERRED_SERIES)}
    return sorted(series, key=lambda row: (
        priority.get(row["ticker"], len(priority)), row.get("frequency") != "daily", row["ticker"],
    ))


def _scan_state(data, venue, scope, kind, default_page, resume):
    if resume:
        with connect(data) as db:
            row = db.execute(
                "SELECT next_page,pages,rows,state FROM progress WHERE venue=? AND scope=? AND kind=?",
                (venue, scope, kind),
            ).fetchone()
            if row:
                return tuple(row)
    return default_page, 0, 0, "incomplete"


def _scan_error(data, venue, scope, kind, page, pages, count, error, reply=None):
    with connect(data) as db, db:
        if reply is not None:
            save_receipt(db, venue, kind, reply)
        set_progress(db, venue, scope, kind, page, pages, count, "error", iso(clock()), _error_message(error))


def _kalshi_alias_events(data, markets):
    aliases = sorted({row["event_ticker"] for row in markets if isinstance(row.get("event_ticker"), str)
                      and not kalshi_ticker_matches_event(row.get("ticker"), row["event_ticker"])})
    if not aliases:
        return {}
    marks = ",".join("?" for _ in aliases)
    with connect(data) as db:
        return {(row["series_id"], row["event_id"]): json.loads(row["metadata"]) for row in db.execute(
            f"SELECT series_id,event_id,metadata FROM events WHERE venue=? AND event_id IN ({marks})", (KALSHI, *aliases),
        )}


def _kalshi_scan(data, get, series, *, kind, page_cap=None, resume=True, status=None, page_size=SCAN_LIMIT):
    ticker = native_id(series["ticker"])
    cursor, pages, count, state = _scan_state(data, KALSHI, ticker, kind, "", resume)
    if state == "complete":
        return count
    seen = set()
    requests = 0
    path = "/historical/markets" if kind == "catalog-archive" else "/markets"
    while page_cap is None or requests < page_cap:
        params = {"series_ticker": ticker, "limit": page_size}
        if kind != "catalog-archive":
            params["mve_filter"] = "only" if kind == "catalog-multivariate" else "exclude"
        if status:
            params["status"] = status
        if cursor:
            params["cursor"] = cursor
        reply = None
        try:
            reply = get(KALSHI, path, params)
            payload = reply.json()
            markets = _rows(payload, "markets")
            after = payload["cursor"]
            if not isinstance(after, str) or after and (after == cursor or after in seen):
                raise ValueError("Invalid or repeated cursor")
            normalized = []
            retained = []
            invalid = []
            aliases = _kalshi_alias_events(data, markets)
            for market in markets:
                try:
                    normalized.append(kalshi_market(series, market, reply.source, reply.received_at,
                                                    event=aliases.get((ticker, market.get("event_ticker")))))
                    retained.append(market)
                except EXCEPTIONS as error:
                    invalid.append(f"{market.get('ticker')}: {_error_message(error)}")
            pages += 1
            count += len(normalized)
            requests += 1
            with connect(data) as db, db:
                receipt_id = save_receipt(db, KALSHI, kind, reply)
                save_markets(db, normalized, retained, reply, receipt_id)
                if invalid:
                    save_metadata(db, KALSHI, f"rejected:{kind}:{ticker}:{pages}", invalid, receipt_id)
                state = "sampled" if kind == "catalog-recent" else "incomplete" if after else "complete"
                set_progress(db, KALSHI, ticker, kind, after, pages, count, state, reply.received_at)
            if not after:
                return count
            seen.add(after)
            cursor = after
        except EXCEPTIONS as error:
            _scan_error(data, KALSHI, ticker, kind, cursor, pages, count, error, reply)
            return count
    return count


def _poly_scan(data, get, *, active, closed, page_cap=None, resume=True):
    scope = f"active={str(active).lower()}&closed={str(closed).lower()}"
    kind = "catalog-closed" if closed else "catalog-open"
    offset, pages, count, state = _scan_state(data, POLY, scope, kind, 0, resume)
    if state == "complete":
        return count
    offset = int(offset)
    requests = 0
    seen_pages = set()
    while page_cap is None or requests < page_cap:
        reply = None
        try:
            reply = get(POLY, POLY_WEATHER, {
                "limit": POLY_LIMIT, "offset": offset, "active": str(active).lower(),
                "closed": str(closed).lower(), "orderBy": "startDate",
            })
            events = _rows(reply.json(), "events")
            page_ids = tuple(sorted(item["slug"] for item in events))
            if page_ids and page_ids in seen_pages:
                raise ValueError("Polymarket US repeated an offset page")
            seen_pages.add(page_ids)
            pages += 1
            requests += 1
            count += len(events)
            complete = len(events) < POLY_LIMIT
            with connect(data) as db, db:
                receipt_id = save_receipt(db, POLY, kind, reply)
                _apply_poly(db, reply, receipt_id, events)
                set_progress(db, POLY, scope, kind, offset + len(events), pages, count,
                             "complete" if complete else "incomplete", reply.received_at)
            if complete:
                return count
            offset += len(events)
        except EXCEPTIONS as error:
            _scan_error(data, POLY, scope, kind, offset, pages, count, error, reply)
            return count
    return count


def poly_series(data=DEFAULT_DATA, get=None):
    """Attach the public series records referenced by the weather tag."""
    get = get or PublicGET(data)
    with connect(data) as db:
        wanted = dict(db.execute(
            """SELECT series_id,family FROM series WHERE venue=? AND series_id!='climate'""", (POLY,),
        ))
    offset = pages = records = 0
    seen = set()
    scope, kind = "weather series metadata", "series"
    while wanted:
        reply = None
        try:
            reply = get(POLY, "/v1/series", {"limit": POLY_LIMIT, "offset": offset})
            rows = _rows(reply.json(), "series")
            names = tuple(sorted(native_id(item["slug"]) for item in rows))
            if names and names in seen:
                raise ValueError("Polymarket US series offset repeated a page")
            seen.add(names)
            with connect(data) as db, db:
                receipt_id = save_receipt(db, POLY, kind, reply)
                for item in rows:
                    slug = item["slug"]
                    if slug in wanted:
                        native_id(item["id"])
                        save_series(db, POLY, slug, wanted.pop(slug), item.get("title"), item, receipt_id)
                        records += 1
                pages += 1
                offset += len(rows)
                exhausted = len(rows) < POLY_LIMIT
                state = "complete" if not wanted else "error" if exhausted else "incomplete"
                missing = "Missing " + ", ".join(sorted(wanted)) if exhausted and wanted else None
                set_progress(db, POLY, scope, kind, offset, pages, records, state, reply.received_at, missing)
            if exhausted:
                break
        except EXCEPTIONS as error:
            _scan_error(data, POLY, scope, kind, offset, pages, records, error, reply)
            break
    return records


def _poly_event_slug(data, get, slug):
    path = "/v1/events/slug/" + quote(slug, safe="")
    reply = get(POLY, path)
    with connect(data) as db, db:
        receipt_id = save_receipt(db, POLY, "event-detail", reply)
        if reply.status == 200:
            event = reply.json()["event"]
            if event.get("slug") != slug:
                raise ValueError("Nested event slug identity mismatch")
            _apply_poly(db, reply, receipt_id, [event])
    return reply.status


def _poly_payout(data, get, instrument):
    path = "/v1/markets/" + quote(instrument, safe="") + "/settlement"
    reply = get(POLY, path)
    with connect(data) as db, db:
        receipt_id = save_receipt(db, POLY, "settlement", reply)
    if reply.status == 200:
        values = poly_settlement(reply.json(), instrument)
        with connect(data) as db, db:
            yes, no = values["yes_payout"], values["no_payout"]
            save_settlement(db, POLY, instrument, yes=yes, no=no, settlement_at=None, source=reply.source,
                            received_at=reply.received_at, receipt_id=receipt_id)
    return reply.status


def _poly_payout_targets(data, limit, *, important=False):
    with connect(data) as db:
        qualifier = "AND family IN ('daily-high','daily-low','daily-rain')" if important else ""
        return [row[0] for row in db.execute(
            """SELECT instrument_id FROM markets WHERE venue=? AND yes_payout IS NULL AND status='settled' """
            + qualifier + " ORDER BY day DESC,instrument_id LIMIT ?", (POLY, limit),
        )]


def _seed_kalshi_catalog(data, get, series, *, initial=False):
    for item in series:
        _kalshi_scan(data, get, item, kind="catalog-live" if initial else "catalog-recent",
                     page_cap=1, page_size=SCAN_LIMIT if initial else 60, resume=False)


def _refresh_series_if_due(data, get):
    with connect(data) as db:
        last = db.execute(
            """SELECT updated_at FROM progress WHERE venue=? AND scope=? AND kind='series'""",
            (KALSHI, KALSHI_CATEGORY),
        ).fetchone()
    if last is None or last[0] < iso(clock() - SERIES_REFRESH):
        return _seed_series(data, get)
    return _known_series(data)


def _next_series(data, series):
    with connect(data) as db:
        seen = dict(db.execute(
            "SELECT scope,updated_at FROM progress WHERE venue=? AND kind='catalog-recent'", (KALSHI,),
        ))
    broad = [item for item in series if item["ticker"] not in CATALOG_PRIORITY_SERIES]
    broad.sort(key=lambda row: (seen.get(row["ticker"], ""), row["ticker"]))
    return broad[:ROTATING_SERIES_LIMIT]


def seed(data=DEFAULT_DATA):
    """Obtain both live and resolved examples before the longer catalog jobs."""
    get = PublicGET(data)
    series = _seed_series(data, get)
    _metadata_reply(data, get, KALSHI, "/historical/cutoff", "historical-cutoff")
    _poly_scan(data, get, active=True, closed=False, page_cap=2, resume=False)
    _seed_kalshi_catalog(data, get, series[:len(PREFERRED_SERIES)], initial=True)
    today = clock().date()
    for day in (today, today - timedelta(days=1), today - timedelta(days=2)):
        for base in ("temp-nychigh-", "temp-nyclow-", "temp-mdwlow-", "rain-daily-"):
            _poly_event_slug(data, get, base + day.isoformat())
    for instrument in _poly_payout_targets(data, limit=24, important=True):
        _poly_payout(data, get, instrument)


def refresh_open(data=DEFAULT_DATA, *, report=None):
    """Refresh the priority head and rotate through the broad independent catalog."""
    if paused(data):
        return
    get = PublicGET(data, timeout=5)
    errors = {}

    def phase(venue, operation):
        if not report:
            return operation()
        try:
            return operation()
        except CollectionPaused:
            raise
        except Exception as issue:
            errors.setdefault(venue, _error_message(issue))

    def kalshi_priority():
        series = _refresh_series_if_due(data, get)
        priority = [item for item in series if item["ticker"] in CATALOG_PRIORITY_SERIES]
        _seed_kalshi_catalog(data, get, priority)
        return series

    def poly():
        _poly_scan(data, get, active=True, closed=False, page_cap=2, resume=False)
        _refresh_poly_weather_results(data, get)

    try:
        series = phase(KALSHI, kalshi_priority)
        phase(POLY, poly)
        if report and not paused(data):
            report(POLY, errors.get(POLY))
        if series is not None:
            phase(KALSHI, lambda: _seed_kalshi_catalog(data, get, _next_series(data, series)))
        if report and not paused(data):
            report(KALSHI, errors.get(KALSHI))
    except CollectionPaused:
        return


def _refresh_poly_weather_results(data, get):
    """Keep recent low and rain results current after they leave the open tag."""
    today = (clock() + timedelta(hours=STATION_OFFSETS["KNYC"])).date()
    for day in (today - timedelta(days=1), today - timedelta(days=2)):
        for base in ("temp-nyclow-", "temp-mdwlow-", "rain-daily-"):
            _poly_event_slug(data, get, base + day.isoformat())
    since = (today - timedelta(days=3)).isoformat()
    with connect(data) as db:
        tickers = [row[0] for row in db.execute(
            """SELECT m.instrument_id FROM markets AS m LEFT JOIN progress AS p
            ON p.venue=m.venue AND p.scope=m.instrument_id AND p.kind='settlement-check'
            WHERE m.venue=? AND m.family IN ('daily-low','daily-rain') AND m.day>=?
              AND m.status='settled' AND m.yes_payout IS NULL
              AND (p.updated_at IS NULL OR p.updated_at<?)
            ORDER BY m.day DESC,m.instrument_id LIMIT 14""", (POLY, since, iso(clock() - timedelta(minutes=10))),
        )]
    for instrument in tickers:
        response = _poly_payout(data, get, instrument)
        with connect(data) as db, db:
            set_progress(db, POLY, instrument, "settlement-check", "", 1, int(response == 200),
                         "complete" if response == 200 else "error", iso(clock()),
                         None if response == 200 else f"HTTP {response}")


def _ladder(rows):
    if rows is None:
        return None
    if not isinstance(rows, list):
        raise ValueError("Invalid Kalshi ladder")
    levels = {}
    for row in rows:
        if not isinstance(row, list) or len(row) != 2:
            raise ValueError("Invalid Kalshi price level")
        price, size = map(decimal, row)
        if not 0 <= price <= 1 or size <= 0 or price in levels:
            raise ValueError("Invalid or duplicate Kalshi price level")
        levels[price] = size
    return [[str(price), str(size)] for price, size in sorted(levels.items(), reverse=True)]


def _opposite_asks(bids):
    if bids is None:
        return None
    return [[str(1 - decimal(price)), quantity] for price, quantity in bids]


def parse_kalshi_book(row, reply):
    ticker = native_id(row["ticker"])
    raw = row.get("orderbook_fp")
    if not isinstance(raw, dict):
        raise ValueError("Missing fixed-point ladder")
    yes, no = _ladder(raw.get("yes_dollars")), _ladder(raw.get("no_dollars"))
    age = reply.headers.get("age", "")
    return {
        "instrument_id": ticker, "at": reply.received_at, "requested_at": reply.requested_at,
        "source_at": None, "source": reply.source, "cache_age_seconds": int(age) if age.isdigit() else None,
        "http_date": reply.headers.get("date"), "http_headers": reply.headers, "http_status": reply.status,
        "source_time_progress": "unknown",
        "yes_bids": yes, "no_bids": no, "yes_offers": _opposite_asks(no), "no_offers": _opposite_asks(yes),
        "state": None, "depth_kind": "displayed_ladder", "quantity_unit": "contracts",
    }


def _kalshi_book_targets(data, cap):
    now = clock()
    today = now.date().isoformat()
    series = tuple(dict.fromkeys(CATALOG_PRIORITY_SERIES))
    with connect(data) as db:
        rows = db.execute(
            """SELECT instrument_id,series_id,event_id,station,closed_at FROM markets
            WHERE venue=? AND status='open' AND opened_at<=? AND closed_at>?
            AND day BETWEEN date(?, '-1 day') AND date(?, '+1 day')
            AND series_id IN (""" + ",".join("?" for _ in series) + ")"
            " ORDER BY closed_at,event_id,instrument_id",
            (KALSHI, iso(now), iso(now), today, today, *series),
        ).fetchall()
    chosen = {}
    for row in rows:
        series_id = row["series_id"]
        key = ("KXRAIN", row["station"]) if series_id == "KXRAIN" else series_id
        group = chosen.setdefault(key, (row["event_id"], []))
        if row["event_id"] == group[0]:
            group[1].append(row["instrument_id"])
    hourly = tuple(INDEX_CITIES)
    rotate = int(now.timestamp()) // 60 % len(hourly)
    order = (
        *KALSHI_HIGH_BOOK_SERIES, *KALSHI_LOW_BOOK_SERIES,
        *(("KXRAIN", station) for station in KALSHI_RAIN_BOOK_STATIONS),
        *hourly[rotate:], *hourly[:rotate], *PREFERRED_SERIES,
    )
    instruments = []
    for key in dict.fromkeys(order):
        _, tickers = chosen.get(key, (None, []))
        if len(instruments) + len(tickers) <= cap:
            instruments.extend(tickers)
    return instruments


def watch_once(data=DEFAULT_DATA, *, cap=BOOK_LIMIT, outcome=False):
    """Archive one book batch. Public Kalshi REST has no image-time field."""
    if paused(data):
        return None if outcome else 0
    targets = _kalshi_book_targets(data, max(1, min(cap, 100)))
    return sample_kalshi_books(data, targets, scope=KALSHI_PRIORITY, outcome=outcome)


def sample_kalshi_books(data, instruments, *, scope, outcome=False):
    """Sample a bounded set of public native order books under one source receipt."""
    targets = tuple(dict.fromkeys(native_id(instrument) for instrument in instruments))
    started = iso(clock())
    if not targets:
        return _empty_book_batch(data, KALSHI, scope, started, outcome)
    if len(targets) > 100:
        raise ValueError("The native book batch accepts at most 100 instruments")
    get = PublicGET(data, timeout=5)
    try:
        reply = get(KALSHI, "/markets/orderbooks", {"tickers": targets})
    except CollectionPaused:
        return None if outcome else 0
    except EXCEPTIONS as error:
        _scan_error(data, KALSHI, scope, "book", "", 0, 0, error)
        request = getattr(get, "last_requested_at", None)
        entries = {ticker: book_batch_target({"reason": _error_message(error)}, requested_at=request,
                                             http_status=0) for ticker in targets}
        with connect(data) as db, db:
            batch = save_book_batch(db, KALSHI, scope, started, iso(clock()), entries, issue=_error_message(error))
        return batch if outcome else 0
    with connect(data) as db, db:
        receipt_id = save_receipt(db, KALSHI, "book", reply)
        known = {}
        invalid = []
        invalid_targets = {}
        try:
            rows = _rows(reply.json(), "orderbooks") if reply.status == 200 else []
        except EXCEPTIONS as error:
            rows = []
            invalid.append(_error_message(error))
        for row in rows:
            try:
                book = parse_kalshi_book(row, reply)
                ticker = book["instrument_id"]
                if ticker not in targets or ticker in known:
                    raise ValueError("Unrequested or duplicate book identity")
                save_book(db, KALSHI, ticker, receipt_id, book)
                known[ticker] = book
            except EXCEPTIONS as error:
                invalid.append(_error_message(error))
                if isinstance(row, dict) and isinstance(row.get("ticker"), str):
                    invalid_targets[row["ticker"]] = invalid[-1]
        books = dict(known)
        for ticker in set(targets) - known.keys():
            book = {
                "instrument_id": ticker, "at": reply.received_at, "requested_at": reply.requested_at,
                "source": reply.source, "source_at": None, "depth_kind": "gap", "reason": (
                    f"HTTP {reply.status}" if reply.status != 200 else invalid_targets.get(
                        ticker, "Requested market missing or malformed")
                ),
            }
            save_book(db, KALSHI, ticker, receipt_id, book)
            books[ticker] = book
        state = "complete" if len(known) == len(targets) and not invalid else "error"
        error = invalid[0] if invalid else f"{len(known)}/{len(targets)} books returned" if state == "error" else None
        set_progress(db, KALSHI, scope, "book", "", 1, len(known), state, reply.received_at, error)
        available = _publish_books(db, [(KALSHI, ticker, book["at"], receipt_id) for ticker, book in books.items()])
        entries = {ticker: book_batch_target(book, requested_at=reply.requested_at, receipt_id=receipt_id,
                                             http_status=reply.status, available_at=available, headers=reply.headers)
                   for ticker, book in books.items()}
        batch = save_book_batch(db, KALSHI, scope, started, iso(clock()), entries, issue=error)
        return batch if outcome else len(known)


def _empty_book_batch(data, venue, scope, started, outcome):
    with connect(data) as db, db:
        batch = save_book_batch(db, venue, scope, started, iso(clock()), {})
    return batch if outcome else 0


def _publish_books(db, identities):
    # An interrupted witness write leaves the original receipts usable for display.
    db.commit()
    if not identities:
        return None
    available = iso(clock())
    db.executemany(
        """UPDATE books SET body=json_set(body,'$.available_at',?)
           WHERE venue=? AND instrument_id=? AND at=? AND receipt_id=?""",
        [(available, *identity) for identity in identities],
    )
    return available


def _poly_book_targets(data, cap):
    at = clock()
    with connect(data) as db:
        rows = [dict(row) for row in db.execute(
            """SELECT instrument_id,event_id,day,family,station,operator FROM markets
            WHERE venue=? AND status='open' AND opened_at<=? AND closed_at>?
            AND family IN ('daily-low','daily-rain') AND station IN ('KNYC','KMDW')
            ORDER BY day,instrument_id""", (POLY, iso(at), iso(at)),
        )]
    instruments = []
    close_lane = []
    for family, station in POLY_BOOK_GROUPS:
        climate_day = (at + timedelta(hours=STATION_OFFSETS[station])).date()
        day = climate_day.isoformat()
        station_books = [row for row in rows if (row["family"], row["station"]) == (family, station)]
        if family == "daily-rain":
            prior = (climate_day - timedelta(days=1)).isoformat()
            previous_rain = next((row for row in station_books if row["day"] == prior and row["operator"] == "at-least"), None)
            if previous_rain:
                close_lane.append(previous_rain["instrument_id"])
        contracts = [row for row in station_books if row["day"] >= day]
        if not contracts:
            continue
        event = contracts[0]["event_id"]
        group = [row["instrument_id"] for row in contracts if row["event_id"] == event]
        if len(instruments) + len(group) <= cap:
            instruments.extend(group)
    return [*instruments, *close_lane[:max(0, min(2, cap - len(instruments)))]]


def _poly_book(get, pacing, instrument):
    source = POLY_API + "/v1/markets/" + quote(instrument, safe="") + "/book"
    requested_at = None
    try:
        pacing._pace(POLY)
        requested_at = iso(clock())
        result = get(source)
    except CollectionPaused:
        return instrument, None, None
    except HTTPError as error:
        with error:
            raw = error.read(JSON_CAP)
        reply = Receipt(source, requested_at, iso(clock()), error.code,
                        {key.lower(): value for key, value in error.headers.items() if key.lower() in PUBLIC_HEADERS}, raw)
        book = {"depth_kind": "gap", "reason": f"HTTP {reply.status}"}
    except EXCEPTIONS as error:
        if requested_at is None:
            return instrument, None, {"reason": _error_message(error)}
        reply = Receipt(source, requested_at, iso(clock()), 0, {}, b"")
        book = {"depth_kind": "gap", "reason": _error_message(error)}
    else:
        reply = Receipt(source, iso(result.requested_at), iso(result.at), 200,
                        {key: value for key, value in result.headers.items() if key in PUBLIC_HEADERS}, result.body)
        try:
            age = reply.headers.get("age", "")
            book = parse_book(reply.json(), instrument, result.at, cache_age=int(age) if age.isdigit() else None)
            book.update(yes_bids=book["bids"], yes_offers=book["offers"],
                        no_bids=_opposite_asks(book["offers"]), no_offers=_opposite_asks(book["bids"]))
        except EXCEPTIONS as error:
            book = {"depth_kind": "gap", "reason": _error_message(error)}
    return instrument, reply, {**book, "instrument_id": instrument, "at": reply.received_at,
                              "requested_at": reply.requested_at, "source": reply.source,
                              "http_date": reply.headers.get("date")}


def watch_poly_weather_once(data=DEFAULT_DATA, *, cap=POLY_WEATHER_BOOK_LIMIT, outcome=False):
    """Capture current low and rain ladders plus two prior-day rain settlements."""
    if paused(data):
        return None if outcome else 0
    targets = _poly_book_targets(data, max(1, min(cap, 24)))
    return watch_poly_book_ids(data, targets, scope=POLY_LOW_RAIN_PRIORITY, outcome=outcome)


def watch_poly_book_ids(data, targets, *, scope, outcome=False):
    """Archive a bounded set of public native ladders using the shared pacing."""
    if paused(data):
        return None if outcome else 0
    started = iso(clock())
    targets = tuple(dict.fromkeys(native_id(instrument) for instrument in targets))
    if not targets:
        return _empty_book_batch(data, POLY, scope, started, outcome)
    get = ReadOnlyHTTP(time.monotonic() + 12)
    pacing = PublicGET(data)
    with ThreadPoolExecutor(max_workers=4) as pool:
        images = list(pool.map(lambda instrument: _poly_book(get, pacing, instrument), targets))
    received = 0
    saved = []
    entries = {}
    with connect(data) as db, db:
        for instrument, reply, book in images:
            if reply is None:
                entries[instrument] = book_batch_target(book or {"reason": "Collection stopped"})
                continue
            receipt_id = save_receipt(db, POLY, "book", reply)
            if book["depth_kind"] == "displayed_ladder":
                high = db.execute(
                    """SELECT body FROM books WHERE venue=? AND instrument_id=? AND source_at IS NOT NULL
                    AND json_extract(body,'$.source_time_progress') IN ('first','advanced')
                    ORDER BY at DESC LIMIT 1""", (POLY, instrument),
                ).fetchone()
                prior = [json.loads(high["body"])] if high else []
                mark_book_progress([*prior, book])
                received += 1
            save_book(db, POLY, instrument, receipt_id, book)
            saved.append((POLY, instrument, book["at"], receipt_id))
            entries[instrument] = book_batch_target(book, requested_at=reply.requested_at,
                                                    receipt_id=receipt_id, http_status=reply.status, headers=reply.headers)
        state = "complete" if received == len(targets) or paused(data) else "error"
        error = f"{received}/{len(targets)} books returned" if state == "error" else None
        set_progress(db, POLY, scope, "book", "", 1, received, state, iso(clock()), error)
        available = _publish_books(db, saved)
        for target in entries.values():
            if target["receipt_id"] is not None:
                target["available_at"] = available
        families = dict(db.execute(
            """SELECT instrument_id,family FROM markets WHERE venue=? AND
               instrument_id IN (SELECT value FROM json_each(?))""", (POLY, json.dumps(targets)),
        ))
        for instrument, entry in entries.items():
            entry["family"] = families.get(instrument)
        batch = save_book_batch(db, POLY, scope, started, iso(clock()), entries, issue=error)
    return batch if outcome else received


def recover_kalshi_aliases(data=DEFAULT_DATA, series_filter=None):
    """Replay prior catalog rejections once native event membership is available."""
    with connect(data) as db:
        pages = [tuple(row) for row in db.execute(
            """SELECT DISTINCT m.key,r.id,r.source,r.requested_at,r.received_at,r.status,r.headers
            FROM metadata m JOIN receipts r ON r.id=m.receipt_id
            JOIN json_each(m.value) j
            LEFT JOIN markets k ON k.venue=m.venue AND k.instrument_id=substr(j.value,1,instr(j.value,': ')-1)
            WHERE m.venue=? AND m.key LIKE 'rejected:%' AND k.instrument_id IS NULL ORDER BY r.id""", (KALSHI,),
        )]
    if not pages:
        return 0
    series = {row["ticker"]: row for row in _known_series(data)}
    selected = set(series_filter or ())
    saved = 0
    for page, receipt_id, source, requested_at, received_at, status, headers in pages:
        key = page.split(":")[2]
        if selected and key not in selected:
            continue
        reply = Receipt(source, requested_at, received_at, status, json.loads(headers), raw_receipt(data, receipt_id))
        rows = _rows(reply.json(), "markets")
        chosen = []
        raw_rows = []
        aliases = _kalshi_alias_events(data, rows)
        for row in rows:
            try:
                chosen.append(kalshi_market(series[key], row, source, received_at, event=aliases.get((key, row.get("event_ticker")))))
                raw_rows.append(row)
            except EXCEPTIONS:
                continue
        with connect(data) as db, db:
            did = db.total_changes
            save_markets(db, chosen, raw_rows, reply, receipt_id)
            saved += db.total_changes - did
    return saved


def reindex_kalshi(data=DEFAULT_DATA, series_filter=None):
    """Extract updated station metadata and migrated symbols from captured pages."""
    series = {row["ticker"]: row for row in _known_series(data)}
    selected = set(series_filter or ())
    if unknown := selected - series.keys():
        raise ValueError("Unknown Kalshi series: " + ", ".join(sorted(unknown)))
    saved = recover_kalshi_aliases(data, selected)
    fields = ("family", "day", "day_basis", "station", "region", "units", "operator", "lower", "upper", "climate_starts_at", "climate_ends_at",
              "resolution_source", "report_policy", "fallback")
    families = {"daily-high", "daily-low", "daily-rain", "snow", "storm-count", "hourly-weather",
                "monthly-rain", "enso", "annual-temperature", "heating-degree-days", "tornado-count", "monthly-global-temperature"}
    tickers = sorted(selected or {key for key, info in series.items() if kalshi_family(info) in families})
    if not tickers:
        return saved
    clause = "(" + ",".join("?" for _ in tickers) + ")"
    with connect(data) as db, db:
        db.executemany("UPDATE series SET family=? WHERE venue=? AND series_id=?",
                       [(kalshi_family(series[key]), KALSHI, key) for key in tickers])
    last = 0
    while True:
        with connect(data) as db:
            rows = db.execute(
                "SELECT rowid,series_id,source,last_received_at,raw_market FROM markets"
                f" WHERE venue=? AND rowid>? AND series_id IN {clause}"
                " ORDER BY rowid LIMIT 1000", (KALSHI, last, *tickers),
            ).fetchall()
        if not rows:
            return saved
        updates = []
        raw_rows = [json.loads(row["raw_market"]) for row in rows]
        aliases = _kalshi_alias_events(data, raw_rows)
        for row, raw in zip(rows, raw_rows, strict=True):
            obj = kalshi_market(series[row["series_id"]], raw, row["source"], row["last_received_at"],
                                event=aliases.get((row["series_id"], raw.get("event_ticker"))))
            updates.append([*(obj.get(field) for field in fields), row["rowid"], row["last_received_at"]])
        with connect(data) as db, db:
            db.executemany(
                "UPDATE markets SET " + ",".join(f"{field}=?" for field in fields)
                + " WHERE rowid=? AND last_received_at=?", updates,
            )
            saved += db.total_changes
        last = rows[-1]["rowid"]


def reindex_polymarket(data=DEFAULT_DATA, series=None):
    fields = ("family", "day", "day_basis", "station", "region", "units", "operator", "lower", "upper", "climate_starts_at",
              "climate_ends_at", "resolution_source", "report_policy", "fallback")
    with connect(data) as db, db:
        scope, args = (" AND m.series_id=?", (POLY, series)) if series else ("", (POLY,))
        rows = db.execute(
            """SELECT m.instrument_id,m.source,m.raw_market,m.last_received_at,e.metadata FROM markets AS m
            JOIN events AS e USING(venue,event_id) WHERE m.venue=?""" + scope, args,
        ).fetchall()
        updates = []
        for row in rows:
            item = poly_market(json.loads(row["metadata"]), json.loads(row["raw_market"]), row["source"])
            updates.append([*(item.get(key) for key in fields), POLY, row["instrument_id"], row["last_received_at"]])
        db.executemany("UPDATE markets SET " + ",".join(f"{key}=?" for key in fields)
                       + " WHERE venue=? AND instrument_id=? AND last_received_at=?", updates)
        return db.total_changes


def sweep(data=DEFAULT_DATA, *, venue=None, pages=None, series_filter=None, workers=5, multivariate_only=False):
    get = PublicGET(data, timeout=12)
    if not multivariate_only and venue in (None, POLY):
        for active, closed in ((True, False), (False, False), (True, True), (False, True)):
            _poly_scan(data, get, active=active, closed=closed, page_cap=pages)
        poly_series(data, get)
    if venue in (None, KALSHI):
        series = _known_series(data) or _seed_series(data, get)
        if series_filter:
            accepted = set(series_filter)
            series = [item for item in series if item["ticker"] in accepted]

        kinds = ("catalog-multivariate",) if multivariate_only else ("catalog-live", "catalog-archive")

        def run(item):
            for kind in kinds:
                _kalshi_scan(data, get, item, kind=kind, page_cap=pages)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(run, series))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    subs = parser.add_subparsers(dest="command", required=True)
    subs.add_parser("seed")
    subs.add_parser("refresh-open")
    subs.add_parser("watch-poly-weather")
    watch = subs.add_parser("watch")
    watch.add_argument("--rounds", type=int, default=1)
    watch.add_argument("--interval", type=float, default=15)
    scan = subs.add_parser("sweep")
    scan.add_argument("--venue", choices=tuple(ORIGINS))
    scan.add_argument("--pages", type=int)
    scan.add_argument("--series", action="append")
    scan.add_argument("--workers", type=int, default=5)
    scan.add_argument("--kalshi-multivariate-only", action="store_true")
    subs.add_parser("coverage")
    subs.add_parser("poly-series")
    reindex = subs.add_parser("reindex-kalshi")
    reindex.add_argument("--series", action="append")
    reindex_poly = subs.add_parser("reindex-polymarket")
    reindex_poly.add_argument("--series")
    dump = subs.add_parser("export")
    dump.add_argument("path", type=Path)
    dump.add_argument("--venue", choices=tuple(ORIGINS))
    dump.add_argument("--family")
    dump.add_argument("--place")
    dump.add_argument("--day-from")
    dump.add_argument("--day-through")
    dump.add_argument("--status")
    args = parser.parse_args(argv)
    try:
        if args.command == "seed":
            seed(args.data)
        elif args.command == "refresh-open":
            refresh_open(args.data)
        elif args.command == "watch-poly-weather":
            print(f"{watch_poly_weather_once(args.data)} Polymarket US low or rain book images")
            return
        elif args.command == "sweep":
            if args.kalshi_multivariate_only and args.venue == POLY:
                parser.error("The multivariate-only catalog route is specific to Kalshi")
            sweep(args.data, venue=args.venue, pages=args.pages, series_filter=args.series, workers=args.workers,
                  multivariate_only=args.kalshi_multivariate_only)
        elif args.command == "watch":
            for index in range(args.rounds):
                started = time.monotonic()
                print(f"{iso(clock())} Kalshi images {watch_once(args.data)}", flush=True)
                if index < args.rounds - 1 and not paused(args.data):
                    time.sleep(max(0, args.interval - (time.monotonic() - started)))
                elif paused(args.data):
                    break
            return
        elif args.command == "export":
            print(export_file(args.data, args.path, venue=args.venue, family=args.family, place=args.place,
                              day_from=args.day_from, day_through=args.day_through, status=args.status))
            return
        elif args.command == "reindex-kalshi":
            print(f"Updated {reindex_kalshi(args.data, args.series)} retained market rows")
            return
        elif args.command == "reindex-polymarket":
            print(f"Updated {reindex_polymarket(args.data, args.series)} retained market rows")
            return
        elif args.command == "poly-series":
            print(f"Retained {poly_series(args.data)} public weather series")
            return
    except CollectionPaused:
        print("Collection is stopped. Saved receipts and cursors are available.")
    print(json.dumps(coverage(args.data), indent=2))


if __name__ == "__main__":
    main()
