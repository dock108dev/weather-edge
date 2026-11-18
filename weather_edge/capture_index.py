"""An incremental, queryable index of the immutable market receipt files."""
from __future__ import annotations

from contextlib import closing
import argparse
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3

from .market import iso, parse_time

SCHEMA = """
CREATE TABLE IF NOT EXISTS receipts (
    filename TEXT PRIMARY KEY,
    modified_ns INTEGER NOT NULL,
    bytes INTEGER NOT NULL,
    digest BLOB,
    kind TEXT,
    version INTEGER,
    received_us INTEGER,
    raw BLOB,
    issue TEXT,
    CHECK ((raw IS NULL) = (issue IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS receipt_window ON receipts(kind, version, received_us, filename);
CREATE INDEX IF NOT EXISTS receipt_digest ON receipts(digest, filename);
CREATE TABLE IF NOT EXISTS instruments (
    filename TEXT NOT NULL REFERENCES receipts(filename) ON DELETE CASCADE,
    event_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    native_id TEXT,
    station TEXT,
    climate_day TEXT,
    evidence TEXT NOT NULL,
    PRIMARY KEY (filename, event_id, instrument_id, evidence)
);
CREATE INDEX IF NOT EXISTS instrument_receipts ON instruments(instrument_id, filename);
CREATE INDEX IF NOT EXISTS native_instrument_receipts ON instruments(native_id, filename);
CREATE INDEX IF NOT EXISTS station_day_receipts ON instruments(station, climate_day, filename);
CREATE INDEX IF NOT EXISTS event_instrument_receipts ON instruments(event_id, instrument_id, filename);
CREATE TABLE IF NOT EXISTS book_offsets (
    filename TEXT NOT NULL REFERENCES receipts(filename) ON DELETE CASCADE,
    location TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    native_id TEXT NOT NULL,
    identity BLOB NOT NULL,
    book_at TEXT NOT NULL,
    book_us INTEGER NOT NULL,
    source_sec INTEGER,
    source_fraction TEXT,
    PRIMARY KEY (filename, location)
);
CREATE INDEX IF NOT EXISTS book_by_trade_id ON book_offsets(instrument_id, book_us DESC, filename);
CREATE INDEX IF NOT EXISTS book_by_native_id ON book_offsets(native_id, book_us DESC, filename);
CREATE INDEX IF NOT EXISTS book_by_source_time ON book_offsets(
    identity, source_sec DESC, source_fraction DESC, book_us, filename
);
"""
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
INVALID_RECEIPT = "Unreadable or malformed capture file"
UNIQUE_RECEIPT = """r.filename=(SELECT first.filename FROM receipts first
                                WHERE first.digest=r.digest ORDER BY first.filename LIMIT 1)"""
RECEIPT_ORDER = "r.received_us, r.filename"


def path_for(directory):
    directory = Path(directory)
    if directory.name in {"captures", "fixtures"}:
        name = "capture-index.sqlite3" if directory.name == "captures" else "fixture-index.sqlite3"
        return directory.parent / name
    return directory / ".capture-index.sqlite3"


def _epoch_us(value):
    elapsed = parse_time(value) - EPOCH
    return (elapsed.days * 86400 + elapsed.seconds) * 1_000_000 + elapsed.microseconds


def _connection(directory):
    db = sqlite3.connect(path_for(directory), timeout=30)
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(SCHEMA)
        if db.execute("PRAGMA user_version").fetchone()[0] < 2:
            with db:
                db.execute("BEGIN IMMEDIATE")
                if db.execute("PRAGMA user_version").fetchone()[0] < 2:
                    for name, body in db.execute("SELECT filename, raw FROM receipts WHERE raw IS NOT NULL"):
                        db.executemany("INSERT OR IGNORE INTO book_offsets VALUES(?,?,?,?,?,?,?,?,?)",
                                       _book_rows(json.loads(body), name))
                    db.execute("PRAGMA user_version=2")
        return db
    except BaseException:
        db.close()
        raise


def _files(directory):
    files = {}
    with os.scandir(directory) as entries:
        for entry in entries:
            if not entry.name.endswith(".json"):
                continue
            try:
                stat = entry.stat()
            except FileNotFoundError:
                continue
            files[entry.name] = stat.st_mtime_ns, stat.st_size
    return files


def _catalog(report, filename):
    for event in report.get("events", []):
        for bucket in event["buckets"]:
            yield (filename, event["id"], bucket["id"], str(bucket["market_id"]),
                   event["station"], event["date"], "event")
    for event, instruments in report.get("settlements", {}).items():
        for instrument in instruments:
            yield (filename, event, instrument, None, None, None, "payout")


def _market_identity(event, bucket):
    fields = ("market_id", "yes_side_id", "no_side_id", "lower_f", "upper_f", "quantity_step")
    values = [event["id"], event["native_id"], event["station"], event["date"], event["ends_at"],
              bucket["id"], *(bucket[field] for field in fields)]
    return sha256(json.dumps(values, separators=(",", ":")).encode()).digest()


def _book_rows(report, filename):
    for index, event in enumerate(report.get("events", [])):
        for position, bucket in enumerate(event["buckets"]):
            if not bucket["books"]:
                continue
            identity = _market_identity(event, bucket)
            for num, book in enumerate(bucket["books"]):
                source = book.get("source_at")
                match = re.search(r"\.(\d+)", source) if source else None
                fraction = match[1].rstrip("0") if match else ""
                yield (filename, f"$.events[{index}].buckets[{position}].books[{num}]",
                       bucket["id"], str(bucket["market_id"]), identity, book["at"], _epoch_us(book["at"]),
                       _epoch_us(source) // 1_000_000 if source else None, fraction)


def _sync(db, directory, validate):
    indexed = dict((name, (modified, size)) for name, modified, size in db.execute(
        "SELECT filename, modified_ns, bytes FROM receipts"
    ))
    files = _files(directory)
    if indexed == files:
        return
    with db:
        db.execute("BEGIN IMMEDIATE")
        # A second writer may have indexed an overlapping batch while we waited.
        indexed = dict((name, (modified, size)) for name, modified, size in db.execute(
            "SELECT filename, modified_ns, bytes FROM receipts"
        ))
        files = _files(directory)
        db.executemany("DELETE FROM receipts WHERE filename=?", ((name,) for name in indexed.keys() - files.keys()))
        for name, signature in files.items():
            if signature == indexed.get(name):
                continue
            try:
                body = (directory / name).read_bytes()
                report = validate(body)
                received = _epoch_us(report["captured_at"])
                instruments = list(_catalog(report, name))
                books = list(_book_rows(report, name))
            except (AttributeError, KeyError, TypeError, ValueError, OverflowError, OSError):
                db.execute(
                    "INSERT OR REPLACE INTO receipts(filename,modified_ns,bytes,issue) VALUES(?,?,?,?)",
                    (name, *signature, INVALID_RECEIPT),
                )
                continue
            db.execute(
                """INSERT OR REPLACE INTO receipts(filename,modified_ns,bytes,digest,kind,version,received_us,raw)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (name, *signature, sha256(body).digest(), report.get("kind"), report.get("version"), received, body),
            )
            db.executemany("INSERT OR IGNORE INTO instruments VALUES(?,?,?,?,?,?,?)", instruments)
            db.executemany("INSERT OR IGNORE INTO book_offsets VALUES(?,?,?,?,?,?,?,?,?)", books)


def _selection(kind, version, since, through, instrument, station, day, include_payout):
    filters = ["r.kind=?", "r.version=?"]
    params = [kind, version]
    for bound, operator in ((since, ">="), (through, "<=")):
        if bound is not None:
            filters.append(f"r.received_us{operator}?")
            params.append(_epoch_us(bound))
    market_filters = []
    values = []
    for column, value in (("station", station), ("climate_day", day)):
        if value is not None:
            market_filters.append(f"{column}=?")
            values.append(value)
    if instrument is not None:
        market_filters.append("(instrument_id=? OR native_id=?)")
        values.extend((str(instrument), str(instrument)))
    if market_filters:
        where = " AND ".join(market_filters)
        subquery = f"SELECT filename FROM instruments WHERE evidence='event' AND {where}"
        params.extend(values)
        if include_payout:
            # Payout-only receipts have the slug and event but no station metadata.
            subquery += f""" UNION SELECT x.filename
                FROM (SELECT DISTINCT event_id, instrument_id FROM instruments WHERE {where}) matched
                JOIN instruments x USING (event_id, instrument_id) WHERE x.evidence='payout'"""
            params.extend(values)
        filters.append("r.filename IN (" + subquery + ")")
    return " AND ".join(filters), params


def read(directory, validate, *, kind, version, since=None, through=None,
         instrument=None, station=None, day=None, raw=True, latest=None, include_payout=True):
    """Select unique capture files by inclusive UTC receipt time and native identity.

    A bounded read selects whole capture receipts. Individual evidence in a
    receipt retains its original, potentially earlier, availability clock.
    """
    directory = Path(directory)
    if not directory.is_dir():
        return [], []
    where, params = _selection(kind, version, since, through, instrument, station, day, include_payout)
    order = RECEIPT_ORDER
    limit = ""
    if latest is not None:
        order = "r.received_us DESC, r.filename DESC"
        limit = " LIMIT ?"
        params.append(max(1, int(latest)))
    with closing(_connection(directory)) as db:
        _sync(db, directory, validate)
        body = ", r.raw" if raw else ""
        rows = db.execute(
            f"""SELECT r.filename,r.received_us,r.digest,r.bytes{body} FROM receipts r
                 WHERE {where} AND {UNIQUE_RECEIPT}
                 ORDER BY {order}{limit}""",
            params,
        ).fetchall()
        errors = _issues(db)
    return rows, errors


def _issues(db):
    return [{"source": name, "reason": reason} for name, reason in db.execute(
        "SELECT filename, issue FROM receipts WHERE issue IS NOT NULL ORDER BY filename"
    )]


def read_snapshot(directory, validate, *, version, previous):
    """Read only new bodies if the ordered live receipt identities still have the same prefix."""
    query = f"""SELECT r.filename,r.received_us,r.digest,r.bytes{{body}} FROM receipts r
                WHERE r.kind='live' AND r.version=? AND {UNIQUE_RECEIPT} {{tail}}
                ORDER BY {RECEIPT_ORDER}"""
    with closing(_connection(directory)) as db:
        _sync(db, directory, validate)
        # The manifest, bodies and issue list must describe the same SQLite snapshot.
        db.execute("BEGIN")
        extends = False
        if previous:
            manifest = db.execute(query.format(body="", tail=""), (version,)).fetchall()
            extends = manifest[:len(previous)] == previous
        if extends:
            name, received, *_ = previous[-1]
            rows = db.execute(query.format(body=",r.raw", tail="AND (r.received_us,r.filename)>(?,?)"),
                              (version, received, name)).fetchall()
        else:
            rows = db.execute(query.format(body=",r.raw", tail=""), (version,)).fetchall()
        return extends, rows, _issues(db)


def require_readable(issues, since, through):
    """Don't conclude a window had no evidence if a receipt may be unreadable."""
    for issue in issues:
        filename = issue["source"]
        try:
            # Capture filenames carry the completed clock at second precision.
            clock = datetime.strptime(filename.split("-", 1)[0], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
        except ValueError:
            clock = None
        if clock is None or since.replace(microsecond=0) <= clock <= through:
            raise OSError(f"{issue['reason']}: {filename}")


def latest_books(directory, instrument, *, limit=20, start=None, end=None, exact_instrument=None,
                 member=None, required_window=None):
    """Read the latest archived Polymarket US receipts for the exact bucket."""
    from .capture import SCHEMA_VERSION, _validate_capture

    directory = Path(directory)
    if not directory.is_dir():
        return []
    with closing(_connection(directory)) as db:
        _sync(db, directory, _validate_capture)
        if required_window:
            issues = [{"source": name, "reason": issue} for name, issue in db.execute(
                "SELECT filename,issue FROM receipts WHERE issue IS NOT NULL ORDER BY filename"
            )]
            require_readable(issues, *required_window)
        bounds, clocks = "", []
        for clock, operator in ((start, ">="), (end, "<=")):
            if clock is not None:
                bounds += f" AND b.book_us{operator}?"
                clocks.append(_epoch_us(clock))
        if exact_instrument is not None:
            bounds += " AND b.instrument_id=? AND b.native_id=?"
            clocks.extend((exact_instrument, instrument))
        if member:
            event = "substr(b.location,1,instr(b.location,'.buckets[')-1)"
            bucket = "substr(b.location,1,instr(b.location,'.books[')-1)"
            for path, key, field in ((event, "id", "event_id"), (event, "native_id", "native_event_id"),
                                     (bucket, "yes_side_id", "yes_side_id"), (bucket, "no_side_id", "no_side_id")):
                bounds += f" AND CAST(json_extract(CAST(r.raw AS TEXT),{path} || '.{key}') AS TEXT)=?"
                clocks.append(str(member[field]))
        cap = "" if limit is None else " LIMIT ?"
        sizes = () if limit is None else (max(1, min(100, int(limit))),)
        matches = db.execute(
            """WITH matches AS (
                   SELECT b.filename,b.location,b.book_us,r.digest,r.received_us,
                          ROW_NUMBER() OVER (PARTITION BY b.book_at ORDER BY r.received_us,b.filename,b.location) AS copy
                   FROM book_offsets b JOIN receipts r USING(filename)
                   WHERE (b.instrument_id=? OR b.native_id=?) AND r.kind='live' AND r.version=?"""
            + bounds + """
               ) SELECT m.filename, m.digest, m.received_us, json_extract(CAST(r.raw AS TEXT),m.location)
                 FROM matches m JOIN receipts r USING(filename)
                 WHERE m.copy=1 ORDER BY m.book_us DESC, m.filename""" + cap,
            (str(instrument), str(instrument), SCHEMA_VERSION, *clocks, *sizes),
        ).fetchall()
    return [{**json.loads(body), "capture_file": filename, "capture_sha256": digest.hex(),
             "available_at": iso(EPOCH + timedelta(microseconds=received))}
            for filename, digest, received, body in matches]


def preceding_books(directory, events, *, before, kind, version):
    """Find the prior high-water image for each identity in a bounded read."""
    heads = {}
    with closing(_connection(directory)) as db:
        for event in events:
            for bucket in event["buckets"]:
                row = db.execute(
                    """SELECT b.filename, r.digest, json_extract(CAST(r.raw AS TEXT),b.location)
                       FROM book_offsets b INDEXED BY book_by_source_time CROSS JOIN receipts r USING(filename)
                       WHERE b.identity=? AND b.source_sec IS NOT NULL AND r.received_us<? AND r.kind=? AND r.version=?
                       ORDER BY b.source_sec DESC,b.source_fraction DESC,b.book_us,r.received_us,b.filename LIMIT 1""",
                    (_market_identity(event, bucket), _epoch_us(before), kind, version),
                ).fetchone()
                if row is not None:
                    heads[event["id"], bucket["id"]] = {
                        **json.loads(row[2]), "capture_file": row[0], "capture_sha256": row[1].hex(),
                    }
    return heads


def coverage(directory):
    from .capture import SCHEMA_VERSION, _validate_capture

    directory = Path(directory)
    if not directory.is_dir():
        return None
    with closing(_connection(directory)) as db:
        _sync(db, directory, _validate_capture)
        summary = db.execute(
            """SELECT count(*),count(DISTINCT b.instrument_id),sum(b.source_sec IS NOT NULL),min(b.book_us),max(b.book_us)
               FROM book_offsets b JOIN receipts r USING(filename) WHERE r.kind='live' AND r.version=?""",
            (SCHEMA_VERSION,),
        ).fetchone()
    snapshots, instruments, source_clock, minimum, maximum = summary
    if not snapshots:
        return None
    return {
        "venue": "polymarket-us", "scope": "Daily-high receipt files",
        "snapshots": snapshots, "instruments": instruments, "source_clock": source_clock,
        "first_at": (EPOCH + timedelta(microseconds=minimum)).isoformat(),
        "last_at": (EPOCH + timedelta(microseconds=maximum)).isoformat(),
    }


def recent_sessions(directory, limit=20):
    from .capture import SCHEMA_VERSION, _validate_capture

    directory = Path(directory)
    if not directory.is_dir():
        return []
    with closing(_connection(directory)) as db:
        _sync(db, directory, _validate_capture)
        rows = db.execute(
            """SELECT received_us FROM receipts WHERE kind='live' AND version=?
               GROUP BY received_us ORDER BY received_us DESC LIMIT ?""",
            (SCHEMA_VERSION, limit),
        ).fetchall()
    return [iso(EPOCH + timedelta(microseconds=at)) for (at,) in rows]


def book_capture_clocks(directory, *, since, through, version):
    """Read the first matching completed capture clock for each exact book."""
    with closing(_connection(directory)) as db:
        rows = db.execute(
            """SELECT b.instrument_id, b.book_at, MIN(r.received_us)
               FROM book_offsets b JOIN receipts r USING(filename)
               WHERE r.kind='live' AND r.version=? AND r.received_us<=? AND b.book_us>=?
               GROUP BY b.instrument_id,b.book_at""",
            (version, _epoch_us(through), _epoch_us(since)),
        ).fetchall()
    return {(instrument, at): EPOCH + timedelta(microseconds=received) for instrument, at, received in rows}


def earlier_book_captures(directory, events, *, before, version):
    """Find prior publications of an exact capture's books without adding books."""
    known = {}
    with closing(_connection(directory)) as db:
        for event in events:
            for bucket in event["buckets"]:
                requested = {b["at"]: b for b in bucket["books"]}
                if not requested:
                    continue
                rows = db.execute("""
                    SELECT b.book_at,b.filename,r.digest,r.received_us,
                           json_extract(CAST(r.raw AS TEXT),b.location)
                    FROM book_offsets b INDEXED BY book_by_source_time CROSS JOIN receipts r USING(filename)
                    WHERE b.identity=? AND b.book_at IN (SELECT value FROM json_each(?))
                      AND r.kind='live' AND r.version=? AND r.received_us<?
                    ORDER BY r.received_us,b.filename
                """, (_market_identity(event, bucket), json.dumps(list(requested)), version, _epoch_us(before)))
                for book_at, name, digest, received, raw in rows:
                    current = requested[book_at]
                    key = event["id"], bucket["id"], book_at
                    if key not in known and all(current.get(k) == v for k, v in json.loads(raw).items()
                                                if k != "source_time_progress"):
                        known[key] = {"available_at": iso(EPOCH + timedelta(microseconds=received)),
                                      "capture_file": name, "capture_sha256": digest.hex()}
    return known


def main():
    from .capture import LIVE_DIR, SCHEMA_VERSION, _validate_capture

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=LIVE_DIR)
    parser.add_argument("--since", help="Inclusive ISO 8601 receipt time with offset")
    parser.add_argument("--through", help="Inclusive ISO 8601 receipt time with offset")
    parser.add_argument("--instrument", help="Polymarket US instrument slug or native numeric ID")
    parser.add_argument("--station")
    parser.add_argument("--day", help="Station climate day, YYYY-MM-DD")
    parser.add_argument("--fixture", action="store_true")
    parser.add_argument("--include-raw", action="store_true")
    args = parser.parse_args()
    kind = "fixture" if args.fixture else "live"
    rows, issues = read(args.directory, _validate_capture, kind=kind, version=SCHEMA_VERSION,
                        since=args.since, through=args.through, instrument=args.instrument,
                        station=args.station, day=args.day, raw=args.include_raw)
    print(json.dumps({
        "database": str(path_for(args.directory).resolve()),
        "kind": kind,
        "receipts": [
            {"filename": name, "received_us_utc": at, "sha256": digest.hex(), "bytes": size,
             **({"record": json.loads(body[0])} if body else {})}
            for name, at, digest, size, *body in rows
        ],
        "issues": issues,
    }, indent=2))


if __name__ == "__main__":
    main()
