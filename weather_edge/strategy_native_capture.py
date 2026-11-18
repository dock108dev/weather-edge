"""Adapt exact native Polymarket US temperature books to the paper ledger."""

from collections import defaultdict
import json

from . import listing_evidence, venue_archive, venue_fees
from .history import _check_bucket_partition
from .market import STATIONS, decimal, mark_book_progress, parse_time
from .strategy_rules import temperature_window


POLY = venue_archive.POLY


def listing_at(bucket, at):
    return listing_evidence.select(bucket.get("listing_revisions", []), at)


def terms_at(bucket, at):
    """Select the exact listing and applicable fee proof at a capture origin."""
    listing = listing_at(bucket, at)
    return {"listing": listing, "fees": venue_fees.poly_terms(listing, at)}


def exact_member(event, bucket):
    return {
        "instrument_id": bucket["id"], "event_id": event.get("id"), "native_event_id": event.get("native_id"),
        "native_id": bucket.get("market_id"), "yes_side_id": bucket.get("yes_side_id"),
        "no_side_id": bucket.get("no_side_id"),
    }


def with_listing_evidence(data, tape, through, *, refresh=False, reuse_bodies=False):
    """Attach receipt history for the exact captured contracts without importing books."""
    wanted = [
        exact_member(event, bucket)
        for event in tape.get("events", []) if event.get("venue", POLY) == POLY
        for bucket in event.get("buckets", [])
        if bucket.get("quantity_step") and bucket.get("books") and (refresh or "listing_revisions" not in bucket)
    ]
    if not wanted:
        return tape
    with venue_archive.connect(data) as db:
        db.execute("BEGIN")
        history = listing_evidence.poly_revisions(db, wanted, parse_time(through), reuse_bodies=reuse_bodies)
    exact = {member["instrument_id"] for member in wanted}
    return {**tape, "events": [
        {**event, "buckets": [
            {**bucket, "listing_revisions": history.get(bucket["id"], [])} if bucket["id"] in exact else bucket
            for bucket in event.get("buckets", [])
        ]} if event.get("venue", POLY) == POLY else event for event in tape["events"]
    ]}


def _book_bounds(db, instrument, since, through):
    # Use the covering market/time index. The stored clock mirrors the body
    # clock, but whole-second and fractional legacy timestamps sort differently.
    floor = " AND at>=?" if since else ""
    args = (venue_archive.receipt_time(since),) if since else ()
    clocks = (parse_time(row[0]) for row in db.execute(
        "SELECT at FROM books WHERE venue=? AND instrument_id=? AND at<=?" + floor,
        (POLY, instrument, venue_archive.receipt_time(through), *args),
    ))
    first = last = next(clocks, None)
    for clock in clocks:
        first = min(first, clock)
        last = max(last, clock)
    return (first, last) if first is not None else ()


def book_clocks(bucket):
    if "_book_bounds" in bucket:
        return iter(bucket["_book_bounds"])
    return (parse_time(book["at"]) for book in bucket["books"])


def captured_bucket(db, member, since, through, revisions=None, *, summarize_books=False):
    limits = temperature_window(member)
    identifiers = (member["native_id"], member["yes_side_id"], member["no_side_id"])
    if limits is None or not all(identifiers) or len(set(identifiers[1:])) != 2:
        raise ValueError("Incomplete exact bucket or native side identity")
    quantity = member["quantity_step"]
    if quantity is None or decimal(quantity) not in {decimal("0.01"), decimal("1")}:
        raise ValueError("Unverified Polymarket US quantity step")
    depth = ([] if summarize_books else
             venue_archive._book_query(db, POLY, member["instrument_id"], start=since, end=through))
    bucket = {
        "id": member["instrument_id"], "market_id": member["native_id"],
        "yes_side_id": member["yes_side_id"], "no_side_id": member["no_side_id"],
        "lower_f": limits[0], "upper_f": limits[1], "label": member["label"],
        "quantity_step": quantity, "rules": member["rules_primary"], "exchange_status": member["native_status"],
        "books": [{**book, "http_cache": book.get("http_headers", {})} for book in depth],
    }
    if summarize_books:
        bucket["_book_bounds"] = _book_bounds(db, member["instrument_id"], since, through)
    if revisions is not None:
        bucket["listing_revisions"] = revisions
    settlement_available = member["settlement_received_at"]
    if (member["yes_payout"] is not None and settlement_available and member["settlement_source"]
            and parse_time(settlement_available) <= through and 0 <= decimal(member["yes_payout"]) <= 1):
        bucket["settlement"] = {
            "yes_payout": member["yes_payout"], "available_at": settlement_available,
            "source": member["settlement_source"], "exchange_settled_at": member["settlement_at"],
        }
    return bucket


def native_event(db, members, since, through, revisions=None, *, summarize_books=False):
    stable = ("event_id", "native_event_id", "family", "station", "day",
              "climate_starts_at", "climate_ends_at", "resolution_source")
    root = members[0]
    if any(row[field] != root[field] for row in members for field in stable):
        raise ValueError("Inconsistent market identity, report window or expiry")
    if (root["family"] not in {"daily-high", "daily-low"}
            or root["resolution_source"] != "NWS Daily Climate Report" or root["station"] not in STATIONS
            or any(not root[key] for key in stable)):
        raise ValueError("Incomplete exact station and climate report scope")
    lat, lon, timezone, offset = STATIONS[root["station"]]
    buckets = [captured_bucket(db, member, since, through,
                               revisions.get(member["instrument_id"], []) if revisions is not None else None,
                               summarize_books=summarize_books)
               for member in members]
    buckets.sort(key=lambda bucket: float("-inf") if bucket["lower_f"] is None else bucket["lower_f"])
    try:
        _check_bucket_partition(buckets)
    except ValueError as error:
        raise ValueError("An executable temperature book requires a complete exact bucket partition") from error
    parent = db.execute("SELECT metadata FROM events WHERE venue=? AND event_id=?", (POLY, root["event_id"])).fetchone()
    metadata = json.loads(parent["metadata"]) if parent else {}
    native_end = metadata.get("endDate") if metadata.get("slug") == root["event_id"] else None
    market_end = root["closed_at"]
    market_open = min((m["opened_at"] for m in members if m["opened_at"]), key=parse_time, default=None)
    schedule_basis = "Listed market schedule. Paper eligibility also requires an open native book state."
    if revisions is not None:
        # Scalar clocks still serve readers of the v2 capture shape. Anchor them
        # at the first book, and leave every actual revision on its exact bucket.
        first = min((clock for b in buckets for clock in book_clocks(b)), default=through)
        known = [terms for b in buckets if (terms := listing_at(b, first))["status"] == "verified"]
        market_open = min((term["opened_at"] for term in known if term["opened_at"]), key=parse_time, default=None)
        event_ends = {term["listed_event_end_at"] for term in known if term["listed_event_end_at"]}
        market_ends = {term["closed_at"] for term in known if term["closed_at"]}
        native_end = next(iter(event_ends)) if len(event_ends) == 1 else None
        market_end = next(iter(market_ends)) if len(market_ends) == 1 else None
        schedule_basis = ("First-book listing schedule; select per-bucket listing revisions at each decision"
                          if market_end else "No confirmed first-book market end; compatibility clock uses climate window")
    return {
        "id": root["event_id"], "native_id": root["native_event_id"], "venue": POLY,
        "family": root["family"], "station": root["station"], "date": root["day"],
        "title": root["event_title"], "lat": lat, "lon": lon, "timezone": timezone,
        "standard_utc_offset_hours": offset, "starts_at": market_open or root["climate_starts_at"],
        "ends_at": market_end or root["climate_ends_at"], "native_event_ends_at": native_end,
        "native_market_ends_at": market_end, "settles_at": market_end or root["climate_ends_at"],
        "source": root["rules_url"], "settlement_time_basis": schedule_basis,
        "climate_starts_at": root["climate_starts_at"], "climate_ends_at": root["climate_ends_at"],
        "buckets": buckets, "observations": [], "forecasts": [],
    }


def polymarket_events(data, through, *, since=None, family="daily-low", station=None, versioned=True,
                      summarize_books=False):
    """Read an indexed consistent market/book window with actual venue receipt clocks."""
    until = parse_time(through)
    with venue_archive.connect(data) as db:
        db.execute("BEGIN")
        cutoff = venue_archive.receipt_time(until)
        args = [cutoff]
        floor = ""
        if since:
            floor = " AND b.at>=?"
            args.append(venue_archive.receipt_time(since))
        # Start with events in this family, then seek to the first qualifying
        # book on each member. A book on any sibling admits the event; return
        # the same complete set of catalog members visible at the cutoff.
        rows = db.execute(
            """WITH scoped_events AS (
                 SELECT DISTINCT event_id FROM markets
                 WHERE venue=? AND family=? AND first_received_at<=? AND (? IS NULL OR station=?)
               ), booked_events AS (
                 SELECT e.event_id FROM scoped_events e
                 WHERE EXISTS (
                   SELECT 1 FROM markets p WHERE p.venue=? AND p.event_id=e.event_id
                   AND EXISTS (SELECT 1 FROM books b
                     WHERE b.venue=p.venue AND b.instrument_id=p.instrument_id AND b.at<=?"""
            + floor + """
                   ))
               )
               SELECT m.* FROM booked_events e JOIN markets m ON m.venue=? AND m.event_id=e.event_id
               WHERE m.family=? AND m.first_received_at<=? AND (? IS NULL OR m.station=?)
               ORDER BY m.event_id,m.instrument_id""",
            (POLY, family, cutoff, station, station, POLY, *args, POLY, family, cutoff, station, station),
        )
        groups = defaultdict(list)
        for member in rows:
            groups[member["event_id"]].append(dict(member))
        revisions = (listing_evidence.poly_revisions(db, [m for rows in groups.values() for m in rows], until)
                     if versioned else None)
        events, issues = [], []
        for event_id, members in groups.items():
            try:
                events.append(native_event(db, members, since, until, revisions, summarize_books=summarize_books))
            except (KeyError, TypeError, ValueError) as error:
                issues.append({"source": event_id, "reason": str(error)})
        return events, issues


def merge_supplemental(capture, supplemental):
    """Add exact native receipts, including events already present on the main tape."""
    positions = {event["id"]: pos for pos, event in enumerate(capture["events"])}
    accepted, issues = [], []
    for event in supplemental:
        ident = event["id"]
        if ident not in positions:
            positions[ident] = len(capture["events"])
            capture["events"].append(event)
        else:
            pos = positions[ident]
            try:
                capture["events"][pos] = _merge_exact_event(capture["events"][pos], event)
            except ValueError as error:
                issues.append({"source": ident, "reason": str(error)})
                continue
        accepted.append(ident)
    return accepted, issues


def _merge_exact_event(prior, supplemental):
    identity = ("native_id", "station", "date", "climate_starts_at", "climate_ends_at")
    if (prior.get("family", "daily-high") != supplemental["family"]
            or prior.get("venue", POLY) != supplemental["venue"]
            or any(prior[key] != supplemental[key] for key in identity)):
        raise ValueError("Supplemental tape has different venue, event, station or climate timing")
    old = {bucket["id"]: bucket for bucket in prior["buckets"]}
    new = {bucket["id"]: bucket for bucket in supplemental["buckets"]}
    if old.keys() != new.keys():
        raise ValueError("Supplemental tape does not match the original exact bucket partition")
    fields = ("market_id", "yes_side_id", "no_side_id", "lower_f", "upper_f", "quantity_step", "rules")
    books_used = 0
    buckets = []
    for ident, bucket in old.items():
        added = new[ident]
        if any(bucket.get(key) != added.get(key) for key in fields):
            raise ValueError(f"Supplemental tape has different contract terms for {ident}")
        merged = {parse_time(book["at"]): dict(book) for book in bucket["books"]}
        for book in added["books"]:
            at = parse_time(book["at"])
            previous = merged.get(at)
            if previous is None:
                merged[at] = dict(book)
                books_used += 1
            elif any(previous.get(key) != book.get(key) for key in ("bids", "offers", "state", "depth_kind", "source_at", "cache_age_seconds")):
                raise ValueError(f"Conflicting main and supplemental book at {book['at']} for {ident}")
        images = [merged[at] for at in sorted(merged)]
        mark_book_progress(images)
        combined = {**bucket, "books": images}
        if "listing_revisions" in added:
            combined["listing_revisions"] = added["listing_revisions"]
        if "settlement" not in combined and "settlement" in added:
            combined["settlement"] = added["settlement"]
        buckets.append(combined)
    versioned = any("listing_revisions" in b for b in buckets)
    return {**prior, "venue": supplemental["venue"], "family": supplemental["family"],
            "ends_at": prior["ends_at"] if versioned else supplemental["ends_at"],
            "legacy_event_ends_at": prior.get("legacy_event_ends_at", prior["ends_at"]),
            "supplemental_book_receipts": books_used,
            "native_event_ends_at": (prior if versioned else supplemental).get("native_event_ends_at"),
            "native_market_ends_at": (prior if versioned else supplemental).get("native_market_ends_at"),
            "buckets": buckets}
