"""Reconstruct exact listing revisions from the venue archive's immutable replies."""

from collections import defaultdict
from functools import lru_cache
from itertools import groupby
import json
from urllib.parse import parse_qs, quote, urlsplit
import zlib

from . import venue_archive as archive
from .market import decimal, iso, parse_time
from .venue_contracts import KALSHI_API, POLY_API, kalshi_market, kalshi_ticker_matches_event, poly_market

POLY, KALSHI = archive.POLY, archive.KALSHI
MISSING = "No exact listing receipt available at this decision clock"
IDENTITY = ("event_id", "native_event_id", "native_id", "yes_side_id", "no_side_id")
TERMS = ("opened_at", "listed_event_end_at", "closed_at", "native_status",
         "fee_multiplier", "fee_waiver_expiration_at", "quantity_step", "fee_waiver_starts_at",
         "binary_dollar_payout", "rules_primary", "rules_secondary")
RECEIPT_FIELDS = ("receipt_id", "received_at", "source", "digest")
VERSION_FIELDS = (*IDENTITY, *TERMS, "reason", "market_status", "event_opened_at")


def _reply_rows(db, venue, kinds, through, source_clause, source_args, newest):
    marks = ",".join("?" for _ in kinds)
    direction = "DESC" if newest else "ASC"
    return db.execute(f"""
        SELECT r.id AS receipt_id,r.received_at,r.source,r.digest,b.compressed
        FROM receipts r JOIN bodies b USING(digest)
        WHERE r.venue=? AND r.kind IN ({marks}) AND r.received_at<=? AND r.status=200
        {source_clause} ORDER BY
        CASE WHEN length(r.received_at)=20 THEN substr(r.received_at,1,19)||'.000000Z'
             ELSE r.received_at END {direction}, r.id {direction}
    """, (venue, *kinds, iso(through.replace(microsecond=0)), *source_args))


def replies(db, venue, kinds, through, *, source_clause="", source_args=(), newest=False):
    """Filter receipt metadata before opening any compressed bodies."""
    rows = _reply_rows(db, venue, kinds, through, source_clause, source_args, newest)
    for row in rows:
        try:
            payload = json.loads(zlib.decompress(row["compressed"]))
            if isinstance(payload, dict) and parse_time(row["received_at"]) <= through:
                yield {key: row[key] for key in RECEIPT_FIELDS}, payload
        except (ValueError, TypeError, zlib.error):
            continue


def select(revisions, at):
    """Inclusive local and effective bounds; simultaneous disagreements are explicit."""
    at = parse_time(at)
    eligible = [row for row in revisions if parse_time(row["received_at"]) <= at
                and parse_time(row["effective_at"]) <= at]
    if not eligible:
        return {"status": "missing", "reason": MISSING}
    latest = max(parse_time(row["received_at"]) for row in eligible)
    tied = [row for row in eligible if parse_time(row["received_at"]) == latest]
    selected = max(tied, key=lambda row: row["receipt_id"])
    if any(_signature(row) != _signature(selected) or row["effective_at"] != selected["effective_at"] for row in tied):
        return {**{k: selected[k] for k in (*RECEIPT_FIELDS, "effective_at")}, "status": "contradictory",
                "conflicting_receipts": sorted({row["receipt_id"] for row in tied}),
                "reason": "Listing receipts at the same local clock disagree"}
    return selected


def _revision(row, receipt, expected, **extra):
    revision = {**{key: row.get(key) for key in (*IDENTITY, *TERMS, "updated_at", "instrument_id", "venue")},
                **receipt, **extra, "effective_at": receipt["received_at"], "market_status": row.get("status")}
    if any(expected.get(key) is not None and revision.get(key) is not None and
           str(expected[key]) != str(revision[key]) for key in IDENTITY):
        return {**revision, "status": "contradictory", "reason": "Listing disagrees with the exact event, market or side identity"}
    if not all(revision[key] for key in IDENTITY) or revision["yes_side_id"] == revision["no_side_id"]:
        return {**revision, "status": "missing", "reason": "Listing receipt lacks an exact event, market or two-sided identity"}
    if revision["opened_at"] and any(parse_time(end) <= parse_time(revision["opened_at"])
                                     for end in (revision["closed_at"], revision["listed_event_end_at"]) if end):
        return {**revision, "status": "contradictory", "reason": "Listed market opens at or after its close"}
    return {**revision, "status": "verified", "reason": None}


def _poly_row(event, raw, receipt, expected):
    # The normalizer accepts partial metadata for search. An exact ticket requires
    # the side mapping to be unambiguous even if the last duplicate normalizes.
    sides = raw.get("marketSides") or []
    row = poly_market(event, raw, receipt["source"])
    try:
        quantity = decimal(row["quantity_step"])
        row["quantity_step"] = str(quantity.normalize()) if quantity in {decimal("0.01"), decimal(1)} else None
    except ValueError:
        row["quantity_step"] = None
    try:
        event_end = iso(event["endDate"]) if event.get("endDate") else None
    except (TypeError, ValueError):
        event_end = None
    revision = _revision(row, receipt, expected, listed_event_end_at=event_end,
                         event_opened_at=_optional_time(event.get("startDate")))
    if len(sides) > 2:
        return {**revision, "status": "contradictory", "reason": "Listing contains competing native sides"}
    return revision


def _optional_time(value):
    try:
        return iso(value) if value else None
    except (TypeError, ValueError):
        return None


def _poly_matches(compressed, detail, instruments):
    # Only immutable bytes and the requested slugs enter this cache. Receipt
    # clocks, expected identities, and terms still get evaluated on every read.
    payload = json.loads(zlib.decompress(compressed))
    if not isinstance(payload, dict):
        raise ValueError("Invalid Polymarket listing reply")
    events = [payload.get("event")] if detail else payload.get("events", [])
    if not isinstance(events, list):
        raise ValueError("Invalid Polymarket event list")
    return tuple(
        ({k: v for k, v in event.items() if k != "markets"}, raw)
        for event in events if isinstance(event, dict) and isinstance(event.get("markets"), list)
        for raw in event["markets"] if isinstance(raw, dict) and raw.get("slug") in instruments
    )


_board_poly_matches = lru_cache(maxsize=768)(_poly_matches)


def poly_revisions(db, members, through, *, reuse_bodies=False):
    """Read each candidate catalog body once per batch, plus precise event URLs."""
    expected = {row["instrument_id"]: row for row in members}
    if not expected:
        return {}
    catalogs = POLY_API + "/v2/tags/weather/events"
    details = {POLY_API + "/v1/events/slug/" + quote(event, safe="") for event in
               {row.get("search_event_id", row.get("event_id")) for row in expected.values()} if event}
    revisions = defaultdict(list)
    instruments = frozenset(expected)
    matches_for = _board_poly_matches if reuse_bodies else _poly_matches
    rows = _reply_rows(db, POLY, ("catalog-open", "catalog-closed", "event-detail"), through,
                       "AND (r.source LIKE ? OR r.source IN (SELECT value FROM json_each(?)))",
                       (catalogs + "?%", json.dumps(sorted(details))), False)
    for row in rows:
        try:
            if parse_time(row["received_at"]) > through:
                continue
            source = row["source"]
            detail = source in details
            if not detail and source.split("?")[0] != catalogs:
                continue
            matches = matches_for(row["compressed"], detail, instruments)
        except (ValueError, TypeError, zlib.error):
            continue
        receipt = {key: row[key] for key in RECEIPT_FIELDS}
        for event, raw in matches:
            instrument = raw["slug"]
            try:
                revision = _poly_row(event, raw, receipt, expected[instrument])
                if detail and source != POLY_API + "/v1/events/slug/" + quote(revision["event_id"], safe=""):
                    revision.update(status="contradictory", reason="Event in listing differs from the requested event")
            except (KeyError, TypeError, ValueError):
                revision = {**receipt, "effective_at": receipt["received_at"], "status": "contradictory",
                            "reason": "Exact listing receipt has invalid native terms"}
            revisions[instrument].append(revision)
    # Keep distinct clocks for actual terms changes and simultaneous conflicts,
    # without packing every identical poll into multi-origin captures.
    return {key: _compact(rows) for key, rows in revisions.items()}


def _signature(row):
    return tuple(row.get(key) for key in VERSION_FIELDS)


def _compact(rows):
    distinct = []
    previous = None
    for _, group in groupby(rows, key=lambda row: parse_time(row["received_at"])):
        simultaneous = list(group)
        signatures = {_signature(row) for row in simultaneous}
        if len(signatures) > 1:
            distinct.extend(simultaneous)
            previous = None
        elif (signature := signatures.pop()) != previous:
            distinct.append(simultaneous[-1])
            previous = signature
    return distinct


def kalshi_sources(db, member, at):
    """Return time-qualified raw series, event, and exact ticker evidence."""
    series_id, event_id, instrument = (member.get(key) for key in ("series_id", "event_id", "instrument_id"))
    if not series_id or not event_id:
        return None, None, None
    def newest_exact(kinds, paths, collection, key, value, *, scoped=False):
        clause = "AND (r.source LIKE ? OR r.source LIKE ?)"
        paths = [KALSHI_API + "/" + path for path in paths]
        selectors = tuple(path + "%" for path in paths)
        if len(selectors) == 1:
            selectors *= 2
        if scoped:
            clause += " AND instr(r.source, ?)>0"
            selectors += ("series_ticker=" + quote(series_id, safe=""),)
        chosen = None
        for receipt, payload in replies(db, KALSHI, kinds, at, source_clause=clause,
                                        source_args=selectors, newest=True):
            if chosen and parse_time(receipt["received_at"]) < parse_time(chosen[0]["received_at"]):
                break
            url = urlsplit(receipt["source"])
            if receipt["source"].split("?")[0] not in paths or (scoped and
                    parse_qs(url.query).get("series_ticker") != [series_id]):
                continue
            items = payload.get(collection)
            if not isinstance(items, list):
                continue
            matches = [row for row in items if isinstance(row, dict) and row.get(key) == value]
            if matches:
                if chosen and any(row != chosen[1] for row in matches):
                    return chosen[0], None
                chosen = receipt, matches[0] if all(row == matches[0] for row in matches) else None
        return chosen

    return (
        newest_exact(("series",), ("series",), "series", "ticker", series_id),
        newest_exact(("events",), ("events",), "events", "event_ticker", event_id, scoped=True),
        newest_exact(("catalog-recent", "catalog-live", "catalog-archive"), ("markets", "historical/markets"),
                     "markets", "ticker", instrument, scoped=True),
    )


def kalshi_listing(member, sources):
    series, event, market = sources
    if market is None:
        return {"status": "missing", "reason": MISSING}
    receipt, raw = market
    if raw is None or raw.get("event_ticker") != member["event_id"]:
        return {**receipt, "status": "contradictory", "reason": "Kalshi listing disagrees on the exact ticker or event"}
    if not kalshi_ticker_matches_event(raw["ticker"], raw["event_ticker"]) and not (event and event[1]):
        return {**receipt, "status": "missing", "reason": "No exact Kalshi event receipt for this ticker alias"}
    try:
        # A ticker listing does not require fee metadata to establish its clocks.
        series_raw = series[1] if series and series[1] else {"ticker": member["series_id"]}
        event_raw = event[1] if event else None
        row = kalshi_market(series_raw, raw, receipt["source"], receipt["received_at"], event=event_raw)
        unit = raw.get("notional_value_dollars")
        expiration = raw.get("fee_waiver_expiration_time")
        return _revision(row, receipt, member, fee_multiplier=None,
                         fee_waiver_starts_at=_optional_time(raw.get("fee_waiver_start_time")),
                         waiver_clock_invalid=bool(expiration and not _optional_time(expiration)),
                         binary_dollar_payout=raw.get("market_type") == "binary" and unit is not None and decimal(unit) == 1,
                         expected_expiration_at=_optional_time(raw.get("expected_expiration_time")))
    except (KeyError, TypeError, ValueError):
        return {**receipt, "status": "contradictory", "reason": "Kalshi receipt has invalid or conflicting native terms"}


def payout(db, member, at):
    """Select payout separately from the listing and scheduled close clocks."""
    if member["venue"] == POLY:
        from .venue_contracts import poly_settlement

        url = POLY_API + "/v1/markets/" + quote(member["instrument_id"], safe="") + "/settlement"
        proven = None
        for receipt, body in replies(db, POLY, ("settlement",), at, source_clause="AND r.source=?", source_args=(url,)):
            try:
                values = poly_settlement(body, member["instrument_id"])
            except (KeyError, ValueError, TypeError):
                continue
            if not proven or values["yes_payout"] != proven["yes_payout"]:
                proven = {**receipt, **values, "available_at": receipt["received_at"], "exchange_settled_at": None}
        return proven
    available, source = member.get("settlement_received_at"), member.get("settlement_source")
    if available and source and member.get("yes_payout") is not None and parse_time(available) <= at:
        return {"yes_payout": member["yes_payout"], "no_payout": member["no_payout"], "available_at": available,
                "receipt_id": member.get("settlement_receipt_id"), "source": source,
                "exchange_settled_at": member.get("settlement_at")}
    return None
