"""The local two-venue research catalog and immutable public market receipts."""

from contextlib import contextmanager, nullcontext
import csv
import gzip
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
import zlib

from .market import decimal, parse_time

KALSHI = "kalshi"
POLY = "polymarket-us"
VENUES = (KALSHI, POLY)
DEFAULT_DATA = Path(".data")


def kalshi_dollar_close(field):
    if not isinstance(field, dict):
        return None
    if field.get("close_dollars") is not None:
        return field["close_dollars"]
    # Older fixed-point candles sometimes label decimal dollar strings `close`.
    value = field.get("close")
    return value if isinstance(value, str) and re.fullmatch(r"[01]\.\d{1,4}", value) else None

MARKET_COLUMNS = (
    "venue", "instrument_id", "event_id", "series_id", "native_id", "native_event_id",
    "title", "event_title", "label", "family", "station", "region", "day", "day_basis",
    "status", "native_status", "rules_primary", "rules_secondary", "rules_url", "source",
    "units", "operator", "lower", "upper", "climate_starts_at", "climate_ends_at",
    "resolution_source", "report_policy", "fallback", "yes_side_id", "no_side_id",
    "quantity_step", "opened_at", "closed_at", "updated_at", "settlement_at",
    "settlement_received_at", "yes_payout", "no_payout", "settlement_source",
    "settlement_result", "expiration_value", "fee_type", "fee_multiplier",
    "fee_waiver_expiration_at", "last_price", "yes_bid", "yes_ask",
)
EXPORT_COLUMNS = MARKET_COLUMNS + ("first_received_at", "last_received_at", "settlement_status", "weather_match")

BOOK_BATCH_TABLE = """CREATE TABLE IF NOT EXISTS book_batches (
    id INTEGER PRIMARY KEY, venue TEXT NOT NULL, scope TEXT NOT NULL,
    started_at TEXT NOT NULL, completed_at TEXT NOT NULL, body TEXT NOT NULL
)"""
BOOK_BATCH_INDEX = """CREATE INDEX IF NOT EXISTS book_batch_clock
    ON book_batches(venue, scope, completed_at DESC, id DESC)"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS bodies (
    digest TEXT PRIMARY KEY, compressed BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS receipts (
    id INTEGER PRIMARY KEY, venue TEXT NOT NULL, kind TEXT NOT NULL,
    source TEXT NOT NULL, requested_at TEXT NOT NULL, received_at TEXT NOT NULL,
    status INTEGER NOT NULL, headers TEXT NOT NULL, digest TEXT NOT NULL,
    FOREIGN KEY (digest) REFERENCES bodies(digest)
);
CREATE INDEX IF NOT EXISTS receipt_kind_idx ON receipts(venue, kind, received_at);
CREATE TABLE IF NOT EXISTS series (
    venue TEXT NOT NULL, series_id TEXT NOT NULL,
    family TEXT NOT NULL, title TEXT, metadata TEXT NOT NULL, receipt_id INTEGER NOT NULL,
    PRIMARY KEY (venue, series_id)
);
CREATE TABLE IF NOT EXISTS events (
    venue TEXT NOT NULL, event_id TEXT NOT NULL, series_id TEXT NOT NULL,
    title TEXT, metadata TEXT NOT NULL, receipt_id INTEGER NOT NULL,
    PRIMARY KEY (venue, event_id)
);
CREATE TABLE IF NOT EXISTS markets (
    __MARKET_COLUMNS__,
    first_received_at TEXT NOT NULL, last_received_at TEXT NOT NULL,
    receipt_id INTEGER NOT NULL, raw_market TEXT NOT NULL,
    settlement_receipt_id INTEGER,
    PRIMARY KEY (venue, instrument_id)
);
CREATE INDEX IF NOT EXISTS venue_filter_idx ON markets(venue, family, status, day DESC);
CREATE INDEX IF NOT EXISTS market_day_idx ON markets(day DESC, venue, instrument_id);
CREATE INDEX IF NOT EXISTS station_day_idx ON markets(station, day, family);
CREATE INDEX IF NOT EXISTS market_event_idx ON markets(venue, event_id);
CREATE TABLE IF NOT EXISTS books (
    venue TEXT NOT NULL, instrument_id TEXT NOT NULL, at TEXT NOT NULL,
    receipt_id INTEGER NOT NULL, source_at TEXT, body TEXT NOT NULL,
    PRIMARY KEY (venue, instrument_id, at)
);
CREATE INDEX IF NOT EXISTS book_receipt_time_idx ON books(venue, at);
CREATE TABLE IF NOT EXISTS history (
    venue TEXT NOT NULL, instrument_id TEXT NOT NULL, kind TEXT NOT NULL,
    native_key TEXT NOT NULL, at TEXT, receipt_id INTEGER NOT NULL, body TEXT NOT NULL,
    PRIMARY KEY (venue, instrument_id, kind, native_key)
);
CREATE INDEX IF NOT EXISTS history_market_idx ON history(venue, instrument_id, at);
CREATE INDEX IF NOT EXISTS history_coverage_idx ON history(venue, kind, instrument_id, at);
CREATE TABLE IF NOT EXISTS progress (
    venue TEXT NOT NULL, scope TEXT NOT NULL, kind TEXT NOT NULL,
    next_page TEXT NOT NULL, pages INTEGER NOT NULL, rows INTEGER NOT NULL,
    state TEXT NOT NULL, error TEXT, updated_at TEXT NOT NULL,
    PRIMARY KEY (venue, scope, kind)
);
CREATE TABLE IF NOT EXISTS metadata (
    venue TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, receipt_id INTEGER NOT NULL,
    PRIMARY KEY (venue, key)
);
CREATE TABLE IF NOT EXISTS weather_indices (
    city TEXT NOT NULL, at TEXT NOT NULL, status TEXT NOT NULL,
    temperature_f TEXT, contributors INTEGER,
    point_key TEXT NOT NULL, receipt_id INTEGER NOT NULL, body TEXT NOT NULL,
    PRIMARY KEY (city, at, point_key)
);
""".replace("__MARKET_COLUMNS__", ", ".join(f"{column} TEXT" for column in MARKET_COLUMNS))


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def receipt_time(value):
    return parse_time(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


@contextmanager
def connect(data=DEFAULT_DATA):
    root = Path(data)
    root.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(root / "venues.sqlite3", timeout=20)
    db.row_factory = sqlite3.Row
    try:
        schema_version = db.execute("PRAGMA user_version").fetchone()[0]
        if schema_version < 3:
            if schema_version == 0:
                db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            db.execute("PRAGMA user_version=3")
        yield db
    finally:
        db.close()


def save_receipt(db, venue, kind, reply):
    digest = sha256(reply.body).hexdigest()
    db.execute("INSERT OR IGNORE INTO bodies VALUES (?,?)", (digest, zlib.compress(reply.body)))
    cursor = db.execute(
        """INSERT INTO receipts(venue,kind,source,requested_at,received_at,status,headers,digest)
        VALUES (?,?,?,?,?,?,?,?)""",
        (venue, kind, reply.source, reply.requested_at, reply.received_at, reply.status, dumps(reply.headers), digest),
    )
    return cursor.lastrowid


KALSHI_PRIORITY = "current priority markets"
POLY_LOW_RAIN_PRIORITY = "priority lows and rain"
POLY_HIGH_PRIORITY = "priority daily highs"
MANUAL_BOOK = "selected Trade ticket"
AUTOMATIC_BOOK_SCOPES = {
    KALSHI: (KALSHI_PRIORITY,), POLY: (POLY_HIGH_PRIORITY, POLY_LOW_RAIN_PRIORITY),
}


def save_book_batch(db, venue, scope, started, completed, targets, *, issue=None):
    db.execute(BOOK_BATCH_TABLE)
    db.execute(BOOK_BATCH_INDEX)
    started, completed = receipt_time(started), receipt_time(completed)
    counts = {
        "selected": len(targets),
        "requested": sum(bool(target.get("requested_at")) for target in targets.values()),
        "usable": sum(target["state"] == "usable" for target in targets.values()),
        "empty_depth": sum(target["state"] == "empty_depth" for target in targets.values()),
        "missing_or_invalid": sum(target["state"] == "missing_or_invalid" for target in targets.values()),
        "failed": sum(target["state"] == "failed" for target in targets.values()),
        "unrequested": sum(target["state"] == "unrequested" for target in targets.values()),
    }
    result = {"venue": venue, "scope": scope, "started_at": started, "completed_at": completed,
              **counts, "targets": targets, "issue": issue}
    cursor = db.execute("INSERT INTO book_batches(venue,scope,started_at,completed_at,body) VALUES (?,?,?,?,?)",
                        (venue, scope, started, completed, dumps(result)))
    return {**result, "id": cursor.lastrowid}


def latest_book_batch(db, venue, scope, at, *, instrument=None):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='book_batches' AND type='table'").fetchone():
        return None
    match = "" if instrument is None else (
        " AND EXISTS (SELECT 1 FROM json_each(book_batches.body,'$.targets') WHERE key=?)"
    )
    args = [venue, scope, receipt_time(at)] + ([instrument] if instrument is not None else [])
    row = db.execute(
        "SELECT id,body FROM book_batches WHERE venue=? AND scope=? AND completed_at<=?" + match
        + " ORDER BY completed_at DESC,id DESC LIMIT 1", args,
    ).fetchone()
    return {**json.loads(row["body"]), "id": row["id"]} if row else None


def book_batch_target(book, *, requested_at=None, receipt_id=None, http_status=None, available_at=None, headers=None):
    depth = book.get("depth_kind")
    reason = book.get("reason")
    if depth == "displayed_ladder":
        ladders = [book[key] for key in ("offers", "bids", "yes_offers", "no_offers")
                   if book.get(key) is not None]
        if any(0 < decimal(price) < 1 and decimal(quantity) > 0
               for levels in ladders for price, quantity in levels):
            state = "usable"
        elif ladders:
            state, reason = "empty_depth", "Valid ladder with no usable price levels"
        else:
            state, reason = "missing_or_invalid", "Response has no known ladder sides"
    else:
        state = ("unrequested" if not requested_at else "failed" if http_status is not None and http_status != 200
                 else "missing_or_invalid")
    return {"state": state, "reason": reason, "requested_at": requested_at,
            "received_at": (book.get("at") if http_status != 0 else None), "receipt_id": receipt_id,
            "http_status": http_status, "available_at": available_at, "source_at": book.get("source_at"),
            "depth_kind": depth, "cache_status": (headers or {}).get("cf-cache-status"),
            "cache_age_seconds": book.get("cache_age_seconds")}


def save_series(db, venue, series_id, family, title, raw, receipt_id, *, placeholder=False):
    db.execute(
        """INSERT INTO series VALUES (?,?,?,?,?,?) ON CONFLICT(venue,series_id) DO UPDATE SET
        family=excluded.family,title=excluded.title,metadata=excluded.metadata,receipt_id=excluded.receipt_id
        WHERE excluded.receipt_id >= series.receipt_id AND NOT ?""",
        (venue, series_id, family, title, dumps(raw), receipt_id, placeholder),
    )


def save_event(db, venue, event_id, series_id, title, raw, receipt_id):
    db.execute(
        """INSERT INTO events VALUES (?,?,?,?,?,?) ON CONFLICT(venue,event_id) DO UPDATE SET
        series_id=excluded.series_id,title=excluded.title,metadata=excluded.metadata,receipt_id=excluded.receipt_id
        WHERE excluded.receipt_id >= events.receipt_id""",
        (venue, event_id, series_id, title, dumps(raw), receipt_id),
    )


def save_markets(db, rows, raw_rows, reply, receipt_id):
    columns = MARKET_COLUMNS + ("first_received_at", "last_received_at", "receipt_id", "raw_market", "settlement_receipt_id")
    payout_cols = {
        "yes_payout", "no_payout", "settlement_at", "settlement_received_at",
        "settlement_source", "settlement_result", "settlement_receipt_id",
    }
    updates = []
    for column in columns[2:]:
        if column == "first_received_at":
            updates.append("first_received_at=min(markets.first_received_at,excluded.first_received_at)")
        elif column in {"settlement_received_at", "settlement_receipt_id"}:
            updates.append(f"{column}=CASE WHEN excluded.yes_payout=markets.yes_payout "
                           f"THEN coalesce(markets.{column},excluded.{column}) "
                           f"ELSE coalesce(excluded.{column},markets.{column}) END")
        elif column in payout_cols:
            updates.append(f"{column}=coalesce(excluded.{column},markets.{column})")
        elif column == "status":
            updates.append("status=CASE WHEN markets.yes_payout IS NOT NULL AND excluded.status != 'settled'"
                           " THEN 'settled' ELSE excluded.status END")
        else:
            updates.append(f"{column}=coalesce(excluded.{column},markets.{column})")
    sql = (
        f"INSERT INTO markets({','.join(columns)}) VALUES ({','.join('?' for _ in columns)}) "
        f"ON CONFLICT(venue,instrument_id) DO UPDATE SET {','.join(updates)} "
        "WHERE excluded.last_received_at >= markets.last_received_at"
    )
    db.executemany(sql, [
        [row.get(field) for field in MARKET_COLUMNS]
        + [reply.received_at, reply.received_at, receipt_id, dumps(raw), receipt_id if row.get("yes_payout") is not None else None]
        for row, raw in zip(rows, raw_rows, strict=True)
    ])


def save_settlement(db, venue, instrument_id, *, yes, no, settlement_at, source, receipt_id, received_at):
    db.execute(
        """UPDATE markets SET yes_payout=?, no_payout=?, settlement_at=coalesce(?,settlement_at),
            settlement_received_at=CASE WHEN yes_payout=? AND no_payout=?
                THEN coalesce(settlement_received_at,?) ELSE ? END,
            settlement_receipt_id=CASE WHEN yes_payout=? AND no_payout=?
                THEN coalesce(settlement_receipt_id,?) ELSE ? END,
            status='settled', settlement_source=?,
            settlement_result=CASE WHEN ?='1' THEN 'yes' WHEN ?='0' THEN 'no' ELSE 'scalar' END
        WHERE venue=? AND instrument_id=?""",
        (yes, no, settlement_at, yes, no, received_at, received_at,
         yes, no, receipt_id, receipt_id, source, yes, yes, venue, instrument_id),
    )


def save_book(db, venue, instrument, receipt_id, book):
    db.execute(
        "INSERT OR IGNORE INTO books VALUES (?, ?, ?, ?, ?, ?)",
        (venue, instrument, book["at"], receipt_id, book.get("source_at"), dumps(book)),
    )


def save_history(db, venue, instrument, kind, entries, receipt_id):
    db.executemany(
        """INSERT INTO history VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(venue,instrument_id,kind,native_key) DO NOTHING""",
        [(venue, instrument, kind, str(key), at, receipt_id, dumps(body)) for key, at, body in entries],
    )


def set_progress(db, venue, scope, kind, next_page, pages, rows, state, updated_at, error=None):
    db.execute(
        """INSERT INTO progress VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT(venue,scope,kind) DO UPDATE SET next_page=excluded.next_page,pages=excluded.pages,
            rows=excluded.rows,state=excluded.state,error=excluded.error,updated_at=excluded.updated_at""",
        (venue, scope, kind, str(next_page), pages, rows, state, error, updated_at),
    )


def save_metadata(db, venue, key, value, receipt_id):
    db.execute(
        """INSERT INTO metadata VALUES (?,?,?,?) ON CONFLICT(venue,key) DO UPDATE SET
        value=excluded.value,receipt_id=excluded.receipt_id""", (venue, key, dumps(value), receipt_id),
    )


def _filters(*, venue=None, family=None, place=None, day=None, day_from=None, day_through=None, status=None, text=None):
    where = []
    values = []
    for column, value in (("venue", venue), ("family", family), ("day", day), ("status", status)):
        if value:
            where.append(f"{column}=?")
            values.append(value)
    if day_from:
        where.append("day>=?")
        values.append(day_from)
    if day_through:
        where.append("day<=?")
        values.append(day_through)
    if place:
        where.append("(lower(station)=lower(?) OR instr(lower(region),lower(?))>0)")
        values.extend((place, place))
    if text:
        fields = ("instrument_id", "event_id", "series_id", "title", "event_title", "label", "station", "region")
        where.append("(" + " OR ".join(f"instr(lower({field}),lower(?))>0" for field in fields) + ")")
        values.extend([text] * len(fields))
    return (" WHERE " + " AND ".join(where) if where else ""), values


def _market(row):
    result = dict(row)
    result["settlement_status"] = (
        "verified payout" if result["yes_payout"] is not None
        else "pending payout" if result["status"] in ("closed", "settled")
        else "open" if result["status"] == "open" else "unknown"
    )
    result["weather_match"] = bool(
        result["day"] and result["station"] and result["climate_starts_at"] and result["climate_ends_at"]
    )
    return result


PUBLIC_SELECT = ",".join(MARKET_COLUMNS + ("first_received_at", "last_received_at"))
ORDER = " ORDER BY day DESC,venue,instrument_id"


def query(data=DEFAULT_DATA, *, venue=None, family=None, place=None, day=None, status=None,
          day_from=None, day_through=None, text=None, limit=100, offset=0):
    from . import venue_search

    limit = max(1, min(1000, int(limit)))
    offset = max(0, int(offset))
    where, values = _filters(venue=venue, family=family, place=place, day=day, day_from=day_from,
                             day_through=day_through, status=status, text=text)
    with connect(data) as db:
        db.execute("BEGIN")
        if not (day or day_from or day_through):
            where, values = venue_search.narrow(db, where, values, text)
        total = db.execute("SELECT count(*) FROM markets" + where, values).fetchone()[0]
        rows = [_market(row) for row in db.execute(
            "SELECT " + PUBLIC_SELECT + " FROM markets" + where + ORDER + " LIMIT ? OFFSET ?",
            [*values, limit, offset],
        )]
    return {"rows": rows, "total": total, "limit": limit, "offset": offset}


def export(data=DEFAULT_DATA, *, connection=None, **filters):
    """Yield a consistent, streaming catalog export for a single filter selection."""
    from . import venue_search

    where, values = _filters(**filters)
    with (connect(data) if connection is None else nullcontext(connection)) as db:
        if not any(filters.get(field) for field in ("day", "day_from", "day_through")):
            where, values = venue_search.narrow(db, where, values, filters.get("text"))
        for row in db.execute("SELECT " + PUBLIC_SELECT + " FROM markets" + where + ORDER, values):
            yield _market(row)


def export_file(data, path, *, connection=None, **filters):
    """Write JSON Lines or CSV with native keys and contract evidence."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(path.name + ".part")
    compressed = path.suffix == ".gz"
    suffix = path.with_suffix("").suffix if compressed else path.suffix
    if suffix not in {".csv", ".jsonl"}:
        raise ValueError("Use .csv, .jsonl, .csv.gz, or .jsonl.gz")
    opener = gzip.open if compressed else open
    options = {"compresslevel": 5} if compressed else {}
    with opener(staging, "wt", encoding="utf-8", newline="", **options) as handle:
        if suffix == ".csv":
            writer = csv.DictWriter(handle, fieldnames=EXPORT_COLUMNS)
            writer.writeheader()
            writer.writerows(csv_row(row) for row in export(data, connection=connection, **filters))
        else:
            for row in export(data, connection=connection, **filters):
                handle.write(dumps(row) + "\n")
    staging.replace(path)
    return path


def csv_row(row):
    """Preserve signed numbers and neutralize formulas in any exported text field."""
    result = {}
    for key in EXPORT_COLUMNS:
        value = row[key]
        if (isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@"))
                and not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", value)):
            value = "'" + value
        result[key] = value
    return result


def _book_query(db, venue, instrument, *, start=None, end=None, limit=None):
    clause = " WHERE books.venue=? AND instrument_id=?"
    values = [venue, instrument]
    if start:
        clause += " AND at>=?"
        values.append(receipt_time(start))
    if end:
        clause += " AND at<=?"
        values.append(receipt_time(end))
    order = " ORDER BY at"
    if limit is not None:
        order += " DESC LIMIT ?"
        values.append(limit)
    rows = db.execute("SELECT body,receipt_id,headers,status,digest FROM books JOIN receipts ON books.receipt_id=receipts.id"
                      + clause + order, values)
    return [
        {**json.loads(row["body"]), "receipt_id": row["receipt_id"], "http_headers": json.loads(row["headers"]),
         "http_status": row["status"], "receipt_sha256": row["digest"]}
        for row in rows
    ]


def books(data, venue, instrument, *, start=None, end=None, limit=None):
    with connect(data) as db:
        return _book_query(db, venue, instrument, start=start, end=end, limit=limit)


def book_images(db, venue, instrument, *, start, end):
    """Read the retained images for a price chart; exact tickets join receipts separately."""
    return [json.loads(row[0]) for row in db.execute(
        "SELECT body FROM books WHERE venue=? AND instrument_id=? AND at>=? AND at<=? ORDER BY at",
        (venue, instrument, receipt_time(start), receipt_time(end)),
    )]


def history(data, venue, instrument, *, kind=None, limit=200):
    clause = " WHERE venue=? AND instrument_id=?"
    values = [venue, instrument]
    if kind:
        clause += " AND kind=?"
        values.append(kind)
    with connect(data) as db:
        return _history_query(db, clause, values, limit)


def _history_query(db, clause, values, limit):
    return [
        {**dict(row), "body": json.loads(row["body"])}
        for row in db.execute(
            "SELECT kind,at,receipt_id,body FROM history" + clause + " ORDER BY at DESC LIMIT ?", [*values, limit])
    ]


def _receipt(db, receipt_id):
    row = db.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
    return {**dict(row), "headers": json.loads(row["headers"])} if row else None


def raw_receipt(data, receipt_id):
    with connect(data) as db:
        row = db.execute(
            "SELECT compressed FROM receipts JOIN bodies USING(digest) WHERE id=?", (receipt_id,),
        ).fetchone()
        return zlib.decompress(row[0]) if row else None


def _compare(current, related):
    from .venue_contracts import temperature_window

    checks = {}
    for key in ("station", "region", "day", "climate_starts_at", "climate_ends_at", "units",
                "operator", "lower", "upper", "resolution_source", "report_policy", "fallback"):
        ours, theirs = current[key], related[key]
        if ours is None or theirs is None:
            checks[key] = "unknown"
        else:
            checks[key] = "same" if ours == theirs else "different"
    first_payout, other_payout = current["yes_payout"], related["yes_payout"]
    raw_difference = first_payout is not None and other_payout is not None and first_payout != other_payout
    family = current["family"]
    same_strike = False
    if family == related["family"] and checks["station"] == checks["day"] == checks["units"] == "same":
        if family in ("daily-high", "daily-low"):
            threshold = temperature_window(current)
            same_strike = threshold is not None and threshold == temperature_window(related)
        else:
            sides = ("operator", "lower", "upper")
            same_strike = (current["operator"] not in (None, "custom", "structured", "conflicting", "functional")
                           and all(current[side] == related[side] for side in sides))
    actual_difference = same_strike and raw_difference and all(
        checks[field] != "different" for field in ("climate_starts_at", "climate_ends_at")
    )
    return {
        "classification": "different verified payouts" if actual_difference else "weather and price comparison",
        "payout_equivalent": False,
        "basis": "Contract stipulations and both sources' report selection must be reviewed together",
        "fields": checks,
        "observed_yes_payout_difference": actual_difference,
        "different_verified_raw_yes_amounts": raw_difference,
        "same_weather_threshold": same_strike,
    }


def _component_ids(original):
    from .venue_contracts import native_id

    strike = original.get("custom_strike")
    if not isinstance(strike, dict):
        return []
    values = [strike.get(key) for key in ("Associated Markets", "Associated Events", "Associated Market Sides")]
    if not all(isinstance(value, str) for value in values):
        return []
    symbols, events, sides = [[item.strip() for item in value.split(",")] for value in values]
    if not (2 <= len(symbols) <= 20 and len(symbols) == len(events) == len(sides) and len(symbols) == len(set(symbols))
            and all(side in ("yes", "no") for side in sides)):
        return []
    try:
        return [(native_id(symbol), native_id(event), side) for symbol, event, side in zip(symbols, events, sides, strict=True)]
    except ValueError:
        return []


def _components(db, market):
    items = _component_ids(market["raw_market"])
    if market["venue"] != KALSHI or market["family"] != "multi-station-high" or not items:
        return []
    values = ",".join("?" for _ in items)
    rows = {row["instrument_id"]: _market(row) for row in db.execute(
        f"SELECT {PUBLIC_SELECT} FROM markets WHERE venue=? AND instrument_id IN ({values})", (KALSHI, *(part[0] for part in items)),
    )}
    result = []
    for instrument, event, side in items:
        exact = rows.get(instrument)
        reason = None
        if exact is None:
            reason = "Component not in the retained market catalog"
        elif exact["event_id"] != event or exact["family"] != "daily-high" or not exact["station"]:
            reason = "Archived event, weather type or station could not be verified"
        elif exact["day"] != market["day"]:
            reason = "Archived component belongs to a different day than this primary rule"
        result.append({"venue": KALSHI, "instrument_id": instrument, "event_id": event, "side": side,
                       "identity_confirmed": reason is None, "market": exact if reason is None else None,
                       "archived_component_day": exact["day"] if exact else None, "mismatch_reason": reason})
    return result


def _closed_book_evidence(db, instrument):
    observed = []
    for row in db.execute(
        """SELECT h.at,h.body,r.id,r.received_at FROM history h JOIN receipts r ON r.id=h.receipt_id
        WHERE h.venue=? AND h.instrument_id=? AND h.kind='closed-book-clock' AND r.venue=h.venue AND r.kind=h.kind AND r.status=200
        ORDER BY r.received_at,r.id""", (POLY, instrument),
    ):
        body = json.loads(row["body"])
        reported = body.get("closeSetTime")
        last_trade = body.get("lastTradeSetTime")
        try:
            valid = (body.get("book_state") == "MARKET_STATE_CLOSED" and parse_time(last_trade) <= parse_time(reported)
                     == parse_time(row["at"]) <= parse_time(row["received_at"]))
        except (ValueError, TypeError, AttributeError):
            valid = False
        if valid:
            observed.append({"native_closed_book_at": reported, "native_last_trade_at": last_trade,
                             "native_closed_book_image_at": body.get("book_transact_time"),
                             "native_closed_book_received_at": row["received_at"], "receipt_id": row["id"],
                             "native_settlement_stat_at": body.get("settlementSetTime"),
                             "native_settlement_preliminary": body.get("settlementPreliminaryFlag")})
    return observed


def _single_closed_book_at(observed):
    clocks = {row["native_closed_book_at"] for row in observed}
    return next(iter(clocks)) if len(clocks) == 1 else None


def native_closed_book_at(db, instrument):
    """Return a verified Polymarket US CLOSED book clock, if sources agree."""
    return _single_closed_book_at(_closed_book_evidence(db, instrument))


def _listing_clocks(db, market):
    metadata = (market["event"] or {}).get("metadata") or {}
    clocks = {"nominal_event_end_at": metadata.get("endDate") if market["venue"] == POLY else None,
              "listed_market_end_at": market["closed_at"], "climate_ends_at": market["climate_ends_at"],
              "native_closed_book_at": None, "native_last_trade_at": None, "native_closed_book_image_at": None,
              "native_closed_book_received_at": None, "native_closed_book_receipt": None,
              "native_settlement_stat_at": None, "native_settlement_preliminary": None,
              "native_close_conflict": False, "native_closed_book_observations": []}
    if market["venue"] != POLY:
        return clocks
    observed = _closed_book_evidence(db, market["instrument_id"])
    clocks["native_closed_book_observations"] = observed
    if not observed:
        return clocks
    if _single_closed_book_at(observed) is None:
        clocks["native_close_conflict"] = True
        return clocks
    first = observed[0]
    clocks.update({key: first[key] for key in clocks.keys() & first.keys()})
    clocks["native_closed_book_receipt"] = _receipt(db, first["receipt_id"])
    return clocks


def detail(data, venue, instrument):
    with connect(data) as db:
        row = db.execute("SELECT * FROM markets WHERE venue=? AND instrument_id=?", (venue, instrument)).fetchone()
        if row is None:
            return None
        market = _market(row)
        market["raw_market"] = json.loads(market["raw_market"])
        market["components"] = _components(db, market)
        series = db.execute("SELECT * FROM series WHERE venue=? AND series_id=?", (venue, row["series_id"])).fetchone()
        event = db.execute("SELECT * FROM events WHERE venue=? AND event_id=?", (venue, row["event_id"])).fetchone()
        market["series"] = {**dict(series), "metadata": json.loads(series["metadata"])} if series else None
        market["event"] = {**dict(event), "metadata": json.loads(event["metadata"])} if event else None
        market["clocks"] = _listing_clocks(db, market)
        market["receipt"] = _receipt(db, row["receipt_id"])
        market["settlement_receipt"] = _receipt(db, row["settlement_receipt_id"])
        market["books"] = _book_query(db, venue, instrument, limit=20)
        market["history"] = _history_query(db, " WHERE venue=? AND instrument_id=?", [venue, instrument], 10)
        market["related"] = []
        by = None
        annual = ("annual-temperature", "annual-temperature-rank")
        if row["station"] and row["day"]:
            by = "station=? AND day=? AND family=?", (row["station"], row["day"], row["family"])
        elif row["region"] and row["day"] and row["family"] in annual:
            by = "region=? AND day=? AND family IN (?,?)", (row["region"], row["day"], *annual)
        if by is not None:
            clause, args = by
            for candidate in db.execute(
                f"""SELECT {PUBLIC_SELECT} FROM markets
                WHERE {clause} AND venue != ?
                ORDER BY (operator IS ? AND lower IS ? AND upper IS ?) DESC, instrument_id LIMIT 20""",
                (*args, venue, row["operator"], row["lower"], row["upper"]),
            ):
                item = _market(candidate)
                item["comparison"] = _compare(row, item)
                market["related"].append(item)
            market["related"].sort(key=lambda item: not item["comparison"]["same_weather_threshold"])
        return market


def coverage(data=DEFAULT_DATA, *, connection=None):
    with (connect(data) if connection is None else nullcontext(connection)) as db:
        if not db.in_transaction:
            db.execute("BEGIN")
        markets = [dict(row) for row in db.execute(
            """SELECT venue,family,status,count(*) AS instruments,count(DISTINCT event_id) AS events,
                count(DISTINCT station) AS stations,sum(yes_payout IS NOT NULL) AS payouts,
                min(day) AS first_day,max(day) AS last_day
                FROM markets GROUP BY venue,family,status ORDER BY venue,family,status""",
        )]
        totals = [dict(row) for row in db.execute(
            """SELECT venue,count(*) AS instruments,count(DISTINCT event_id) AS events,
                count(DISTINCT series_id) AS series,count(DISTINCT station) AS stations,
                sum(yes_payout IS NOT NULL) AS payouts,min(day) AS first_day,max(day) AS last_day
                FROM markets GROUP BY venue""",
        )]
        by_family = [dict(row) for row in db.execute(
            """WITH evidence AS MATERIALIZED (
                SELECT m.venue,m.family,m.yes_payout IS NOT NULL AS has_payout,
                    m.station IS NOT NULL AND m.day IS NOT NULL AS has_station_day,
                    m.station IS NOT NULL AND m.day IS NOT NULL
                        AND m.climate_starts_at IS NOT NULL AND m.climate_ends_at IS NOT NULL AS has_rule_window,
                    EXISTS (SELECT 1 FROM history AS h WHERE h.venue=m.venue AND h.instrument_id=m.instrument_id
                        AND h.kind IN ('display-price-1m','candle-1m','candle-60m','candle-1d')) AS has_prices,
                    EXISTS (SELECT 1 FROM books AS b WHERE b.venue=m.venue AND b.instrument_id=m.instrument_id
                        AND json_extract(b.body,'$.depth_kind')='displayed_ladder') AS has_depth
                FROM markets AS m
            )
            SELECT venue,family,count(*) AS cataloged,sum(has_payout) AS verified_payouts,
                sum(has_station_day) AS exact_station_day,sum(has_rule_window) AS exact_station_rule_windows,
                sum(has_prices) AS display_price_history,sum(has_depth) AS depth_receipts,
                sum(has_payout AND has_prices) AS payout_and_price_history,
                sum(has_payout AND has_depth) AS payout_and_depth,
                sum(has_prices AND has_depth) AS price_history_and_depth
            FROM evidence GROUP BY venue,family ORDER BY venue,family""",
        )]
        catalog_series = dict(db.execute("SELECT venue,count(*) FROM series GROUP BY venue"))
        for entry in totals:
            entry["catalog_series"] = catalog_series.get(entry["venue"], 0)
        book_counts = [dict(row) for row in db.execute(
            """SELECT venue,count(*) AS snapshots,count(DISTINCT instrument_id) AS instruments,
                sum(source_at IS NOT NULL) AS source_clock,min(at) AS first_at,max(at) AS last_at
                FROM books GROUP BY venue""",
        )]
        history_counts = [dict(row) for row in db.execute(
            """SELECT venue,kind,count(*) AS points,count(DISTINCT instrument_id) AS instruments,
                min(at) AS first_at,max(at) AS last_at FROM history GROUP BY venue,kind""",
        )]
        index_counts = [dict(row) for row in db.execute(
            """SELECT city,count(DISTINCT at) AS minutes,
                count(DISTINCT CASE WHEN temperature_f IS NOT NULL THEN at END) AS canonical_minutes,
                min(at) AS first_at,max(at) AS last_at FROM weather_indices GROUP BY city""",
        )]
        scans = [dict(row) for row in db.execute(
            """SELECT venue,kind,state,count(*) AS scopes,sum(pages) AS pages,sum(rows) AS rows, max(updated_at) AS updated_at
            FROM progress GROUP BY venue,kind,state ORDER BY venue,kind,state""",
        )]
        pending = [dict(row) for row in db.execute(
            """SELECT venue,scope,kind,state,next_page,pages,rows,error,updated_at FROM progress
            WHERE state IN ('error','incomplete') ORDER BY error IS NOT NULL DESC, updated_at DESC LIMIT 50""",
        )]
        return {"venues": totals, "markets": markets, "families": by_family,
                "books": book_counts, "history": history_counts, "weather_indices": index_counts,
                "scans": scans, "pending": pending}
