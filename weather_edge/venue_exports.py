"""Export both venues and verify every compressed or spreadsheet-ready catalog file."""

import argparse
from collections import Counter
from contextlib import closing
import csv
from datetime import datetime, timedelta, timezone
import gzip
from hashlib import file_digest
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlsplit

from . import capture_index, venue_archive
from .capture import SCHEMA_VERSION as CAPTURE_SCHEMA_VERSION
from .market import iso, parse_time

NOVEMBER = {"day_from": "2026-11-01", "day_through": "2026-11-30"}
FILES = {
    "weather-venue-full.csv.gz": ({}, "All indexed instruments in both venues"),
    "weather-venue-open.csv": ({"status": "open"}, "All open listings in both venues"),
    "weather-venue-daily-high-november.csv": ({"family": "daily-high", **NOVEMBER}, "November daily highs in both venues"),
    "weather-venue-daily-low-november.csv": ({"family": "daily-low", **NOVEMBER}, "November daily lows in both venues"),
    "weather-venue-chicago-snow.csv": ({"family": "snow", "place": "Chicago"}, "Named Chicago snow contracts"),
    "weather-venue-monthly-rain.csv": ({"family": "monthly-rain"}, "Monthly precipitation rules, strikes and payouts"),
    "weather-venue-weekend-rain.csv": ({"family": "weekend-rain"}, "Exact weekend reporting dates and native settlement"),
}
MANIFEST = "weather-venue-manifest.json"

CATALOG_ENDPOINTS = {
    "catalog-archive": "/historical/markets?series_ticker={scope}",
    "catalog-live": "/markets?series_ticker={scope}&mve_filter=exclude",
    "catalog-recent": "/markets?series_ticker={scope}&mve_filter=exclude&limit=60",
    "catalog-multivariate": "/markets?series_ticker={scope}&mve_filter=only",
    "events": "/events?series_ticker={scope}",
    "catalog-open": "/v2/tags/weather/events?{scope}",
    "catalog-closed": "/v2/tags/weather/events?{scope}",
}


def _catalog_queries(db):
    placeholders = ",".join("?" for _ in CATALOG_ENDPOINTS)
    queries = []
    for row in db.execute(f"SELECT * FROM progress WHERE kind IN ({placeholders}) ORDER BY venue,kind,scope", tuple(CATALOG_ENDPOINTS)):
        item = dict(row)
        item["endpoint"] = CATALOG_ENDPOINTS[row["kind"]].format(scope=row["scope"])
        item["reached_terminal_page"] = (row["state"] == "complete" and (
            not row["next_page"] or row["venue"] == venue_archive.POLY
        ))
        item["terminal_evidence"] = ("terminal offset after a short page" if row["venue"] == venue_archive.POLY
                                     else "empty native cursor") if item["reached_terminal_page"] else None
        queries.append(item)
    return queries


def _scope_gaps(db):
    return [dict(row) for row in db.execute(
        """WITH missing AS (
            SELECT p.*, CASE WHEN instr(p.scope,'/') > 0 THEN substr(p.scope,1,instr(p.scope,'/')-1)
              ELSE p.scope END AS instrument_scope
            FROM progress p
            WHERE (p.state IN ('error','incomplete') OR (p.state='complete' AND p.rows=0))
                AND p.kind NOT LIKE 'catalog-%' AND p.kind NOT IN ('events','series')
        )
        SELECT p.venue,p.kind,p.scope,p.state,p.next_page,p.pages,p.rows,p.error,p.updated_at,
            m.family,m.station,m.day,m.opened_at,m.closed_at
        FROM missing p LEFT JOIN markets m ON m.venue=p.venue AND m.instrument_id=coalesce(
            (SELECT n.instrument_id FROM markets n WHERE n.venue=p.venue AND n.instrument_id=p.scope),
            p.instrument_scope
        )
        ORDER BY p.venue,p.kind,p.scope""",
    )]


def _evidence_clocks(db):
    prices = [dict(row) for row in db.execute(
        """SELECT h.venue,m.family,h.kind,count(*) AS records,count(DISTINCT h.instrument_id) AS instruments,
            count(DISTINCT h.at) AS distinct_provider_clocks,count(DISTINCT h.receipt_id) AS distinct_local_receipts,
            min(h.at) AS oldest_provider_at,max(h.at) AS latest_provider_at,
            min(m.day) AS earliest_listed_day,max(m.day) AS latest_listed_day
        FROM history h LEFT JOIN markets m USING(venue,instrument_id)
        GROUP BY h.venue,m.family,h.kind ORDER BY h.venue,m.family,h.kind""",
    )]
    books = [dict(row) for row in db.execute(
        """WITH liquidity AS MATERIALIZED (
            SELECT b.venue,m.family,m.day,b.instrument_id,b.at,b.source_at,b.receipt_id,
              json_extract(b.body,'$.depth_kind')='displayed_ladder' AS has_depth
            FROM books b LEFT JOIN markets m USING(venue,instrument_id)
        )
        SELECT venue,family,count(*) AS attempts, sum(has_depth) AS stored_depth_snapshots,
            count(DISTINCT CASE WHEN has_depth THEN instrument_id END) AS instruments,
            count(DISTINCT CASE WHEN has_depth THEN at END) AS distinct_local_clocks,
            count(DISTINCT CASE WHEN has_depth THEN receipt_id END) AS distinct_local_receipts,
            count(DISTINCT CASE WHEN has_depth THEN source_at END) AS distinct_provider_clocks,
            sum(has_depth AND source_at IS NOT NULL) AS stored_depth_with_provider_clock,
            min(CASE WHEN has_depth THEN at END) AS oldest_local_at,max(CASE WHEN has_depth THEN at END) AS latest_local_at,
            min(CASE WHEN has_depth THEN day END) AS earliest_listed_day,max(CASE WHEN has_depth THEN day END) AS latest_listed_day
        FROM liquidity GROUP BY venue,family ORDER BY venue,family""",
    )]
    errors = [dict(row) for row in db.execute(
        """SELECT venue,kind,status,count(*) AS receipts,min(received_at) AS earliest_receipt,
            max(received_at) AS latest_receipt FROM receipts WHERE status>=400
        GROUP BY venue,kind,status ORDER BY venue,kind,status""",
    )]
    return {"price_trade_and_clearing": [row for row in prices if row["kind"] != "closed-book-clock"],
            "observed_native_closes": [row for row in prices if row["kind"] == "closed-book-clock"],
            "book_samples_by_family": books, "transport_error_receipts": errors}


def _failed_transports(db):
    failures = [dict(row) for row in db.execute(
        """SELECT id,venue,kind,status,source,requested_at,received_at,digest
        FROM receipts WHERE status>=400 ORDER BY id""",
    )]
    successes = {(row["venue"], row["source"]): row["latest_id"] for row in db.execute(
        """WITH failing AS MATERIALIZED (
            SELECT venue,source,min(id) AS first_failed_id FROM receipts WHERE status>=400 GROUP BY venue,source
        )
        SELECT f.venue,f.source,max(r.id) AS latest_id
        FROM failing f JOIN receipts r ON r.venue=f.venue AND r.source=f.source AND r.id>f.first_failed_id
        WHERE r.status BETWEEN 200 AND 299 GROUP BY f.venue,f.source""",
    )}
    for failure in failures:
        retry_id = successes.get((failure["venue"], failure["source"]))
        if retry_id is None or retry_id <= failure["id"]:
            continue
        row = dict(db.execute("SELECT id,kind,status,received_at,digest FROM receipts WHERE id=?", (retry_id,)).fetchone())
        source = urlsplit(failure["source"])
        if failure["venue"] == venue_archive.POLY and source.path == "/v1/price-history":
            symbol = parse_qs(source.query).get("symbol", [""])[0]
            row["retained_display_rows"] = db.execute(
                "SELECT count(*) FROM history WHERE venue=? AND instrument_id=? AND kind=? AND receipt_id=?",
                (venue_archive.POLY, symbol, "display-price-1m", row["id"]),
            ).fetchone()[0]
        failure["latest_later_successful_exact_url_receipt"] = row
    return failures


def _catalog_rejections(db):
    by_instrument = {}
    rows = db.execute(
        """SELECT m.venue,m.key,m.receipt_id,j.value AS reason,
        substr(j.value,1,instr(j.value,': ')-1) AS instrument_id,v.instrument_id AS retained
        FROM metadata m,json_each(m.value) j
        LEFT JOIN markets v ON v.venue=m.venue AND v.instrument_id=substr(j.value,1,instr(j.value,': ')-1)
        WHERE m.key LIKE 'rejected:%' ORDER BY m.venue,m.key,m.receipt_id""",
    )
    occurrences = 0
    for row in rows:
        occurrences += 1
        slot = by_instrument.setdefault((row["venue"], row["instrument_id"]), {"retained": bool(row["retained"]), "sources": []})
        slot["sources"].append({"scope_page": row["key"], "receipt_id": row["receipt_id"], "reason": row["reason"]})
    missing = [{"venue": venue, "instrument_id": instrument, "sources": item["sources"]}
               for (venue, instrument), item in by_instrument.items() if not item["retained"]]
    return {"recorded_rejection_entries": occurrences, "distinct_native_ids": len(by_instrument),
            "later_retained_native_ids": len(by_instrument) - len(missing), "still_unindexed": missing}


def _original_polymarket_tape(data, through):
    directory = data / "captures"
    capture_index.coverage(directory)
    index = capture_index.path_for(directory)
    if not index.is_file():
        return {"status": "No original Polymarket capture index was available"}
    stamp = capture_index._epoch_us(parse_time(through))
    with closing(sqlite3.connect(index.resolve().as_uri() + "?mode=ro", uri=True, timeout=15)) as db:
        db.row_factory = sqlite3.Row
        item = dict(db.execute(
            """SELECT count(*) AS book_snapshots,count(DISTINCT b.instrument_id) AS instruments,
                count(DISTINCT b.book_us) AS distinct_local_clocks,count(DISTINCT r.filename) AS original_receipts,
                count(DISTINCT CASE WHEN b.source_sec IS NOT NULL THEN b.source_sec || '.' || b.source_fraction END)
                  AS distinct_provider_clocks,
                min(b.book_us) AS first_book_us,max(b.book_us) AS last_book_us,max(r.received_us) AS latest_receipt_us
            FROM book_offsets b JOIN receipts r USING(filename)
            WHERE r.kind='live' AND r.version=? AND r.received_us<=?""", (CAPTURE_SCHEMA_VERSION, stamp),
        ).fetchone())
        item["index_validation_errors"] = [dict(row) for row in db.execute(
            "SELECT filename,issue FROM receipts WHERE issue IS NOT NULL ORDER BY filename",
        )]
    for old, new in (("first_book_us", "first_local_book_at"), ("last_book_us", "last_local_book_at"),
                     ("latest_receipt_us", "latest_capture_receipt_at")):
        count = item.pop(old)
        item[new] = iso(capture_index.EPOCH + timedelta(microseconds=count)) if count is not None else None
    return {"venue": venue_archive.POLY, "family": "daily-high", "index": str(index), "through": through, **item}


def _inspect(path, filters, description):
    opener = gzip.open if path.suffix == ".gz" else open
    venues, statuses = Counter(), Counter()
    with opener(path, "rt", encoding="utf-8", newline="") as handle:
        rows = csv.DictReader(handle)
        if tuple(rows.fieldnames or ()) != venue_archive.EXPORT_COLUMNS:
            raise ValueError(f"Unexpected columns in {path.name}")
        for row in rows:
            if None in row or None in row.values() or row["venue"] not in venue_archive.VENUES or not row["instrument_id"]:
                raise ValueError(f"Malformed catalog row in {path.name}")
            venues[row["venue"]] += 1
            statuses[row["status"]] += 1
    with path.open("rb") as handle:
        digest = file_digest(handle, "sha256").hexdigest()
    return {"rows": sum(venues.values()), "filesize_bytes": path.stat().st_size, "sha256": digest,
            "venue_rows": dict(venues), "status_rows": dict(statuses), "filters": filters, "description": description}


def snapshot(data, output):
    output.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=output, prefix=".weather-venue-") as scratch:
        root = Path(scratch)
        with venue_archive.connect(data) as db:
            db.execute("BEGIN")
            receipt_highwater = db.execute("SELECT max(id) FROM receipts").fetchone()[0]
            at = iso(datetime.now(timezone.utc))
            coverage = venue_archive.coverage(data, connection=db)
            catalog = _catalog_queries(db)
            gaps = _scope_gaps(db)
            clocks = _evidence_clocks(db)
            failed_transport_receipts = _failed_transports(db)
            rejected_native_rows = _catalog_rejections(db)
            for filename, (filters, _) in FILES.items():
                venue_archive.export_file(data, root / filename, connection=db, **filters)
        files = {name: _inspect(root / name, filters, description) for name, (filters, description) in FILES.items()}
        if files["weather-venue-full.csv.gz"]["venue_rows"] != {row["venue"]: row["instruments"] for row in coverage["venues"]}:
            raise ValueError("Full export did not reconcile with the same database snapshot")
        summary = {"snapshot_at": at, "source_receipt_highwater_id": receipt_highwater, "csv_columns": venue_archive.EXPORT_COLUMNS,
                   "files": files, "coverage": coverage, "catalog_queries": catalog, "exact_scope_gaps": gaps,
                   "failed_transport_receipt_details": failed_transport_receipts,
                   "catalog_row_normalization_audit": rejected_native_rows,
                   "evidence_clocks": {**clocks, "original_polymarket_high_tape": _original_polymarket_tape(data, at)}, "readme": [
                       "Rules, payout amounts and venue clocks come from native retained public evidence when available.",
                       "A missing native source image timestamp stays unavailable. Capture and provider clocks remain separate.",
                       "The full catalog includes unsupported settlement and weather families because evidence is tracked per field.",
                       "The original Polymarket US high tape is counted from its separate index through the snapshot time.",
                       "A completed scope with zero rows records the public endpoint's answer. The catalog terminal page and a capped or recent-only sample are distinct.",
                       "A later successful HTTP response for the same URL preserves the original failed receipt. An empty successful response remains a data gap.",
                       "Historical provider clocks do not establish when a market image arrived locally. Local historical request receipts and live depth receipt clocks remain separately counted."]}
        (root / MANIFEST).write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        for name in (*FILES, MANIFEST):
            (root / name).replace(output / name)
    return output / MANIFEST


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=venue_archive.DEFAULT_DATA)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    print(snapshot(args.data, args.output or args.data / "exports"))


if __name__ == "__main__":
    main()
