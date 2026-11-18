"""Durable, idempotent single-order records for the local paper ledger."""

from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from hashlib import sha256
import json
import logging
import re
import sqlite3
import zlib

from . import (capture, capture_fixture, capture_index, collector, model_evidence, paper_eligibility,
               paper_runs, strategy_native_capture, trade, venue_archive, venue_contracts)
from .market import decimal, fee_coefficient, iso, mark_book_progress, parse_time
from .poly_fee_policy import frozen_schedule

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    fixture INTEGER NOT NULL,
    request_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL,
    intent TEXT NOT NULL,
    result TEXT,
    PRIMARY KEY (fixture, request_id)
)
"""

# Additive and lazy: an old ledger can be read and can accept reservations
# without migrating any order or touching an existing run.
OUTBOX = """
CREATE TABLE IF NOT EXISTS reconciliation (
    fixture INTEGER NOT NULL,
    request_id TEXT NOT NULL,
    expected TEXT NOT NULL,
    proposal TEXT NOT NULL,
    PRIMARY KEY (fixture, request_id)
)
"""
LOG = logging.getLogger(__name__)


class RequestConflict(ValueError):
    pass


@contextmanager
def _read_ledger(data):
    path = data / "trade-paper" / "orders.sqlite3"
    try:
        path.stat()
    except FileNotFoundError:
        yield None
        return
    db = sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        db.execute("BEGIN")
        # An interrupted initial submission can leave an empty database.
        present = db.execute("SELECT 1 FROM sqlite_schema WHERE name='orders' AND type='table'").fetchone()
        yield db if present else None
    finally:
        db.close()


@contextmanager
def _ledger(data):
    root = data / "trade-paper"
    root.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(root / "orders.sqlite3", timeout=10)
    db.row_factory = sqlite3.Row
    try:
        with db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(SCHEMA)
            yield db, root
    finally:
        db.close()


def _public(row):
    intent = json.loads(row["intent"])
    return {
        "id": row["request_id"], "status": row["status"], "created_at": row["created_at"],
        "reaction_due_at": intent["due"], "ticket": intent["ticket"],
        **({"policy": intent["policy"], "reaction_deadline_at": intent["deadline"]} if "policy" in intent else {}),
        "result": json.loads(row["result"]) if row["result"] else None,
    }


def _allocation(db, fixture):
    cash, committed = Decimal(trade.BANKROLL), Decimal("0")
    exposures = {}
    for row in db.execute("SELECT * FROM orders WHERE fixture=? AND status!='Skipped'", (fixture,)):
        record = _public(row)
        view = record["ticket"]
        position = (record["result"] or {}).get("position")
        cost = decimal(position["max_loss"] if position else view["order"]["max_loss"])
        cash -= cost
        if position and position["settled"]:
            cash += decimal(position["payout"])
        else:
            committed += cost
            group = (view["station"], view["day"])
            exposures[group] = exposures.get(group, Decimal("0")) + cost
    return cash, committed, exposures


def _find(db, fixture, request_id):
    return db.execute("SELECT * FROM orders WHERE fixture=? AND request_id=?", (fixture, request_id)).fetchone()


def validate_request_id(request_id):
    if not isinstance(request_id, str) or not re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", request_id):
        raise ValueError("Invalid paper order request ID")
    return request_id


def existing(data, fixture, request_id, selected, budget, review_key):
    with _read_ledger(data) as db:
        row = _find(db, fixture, request_id) if db is not None else None
        return _same_request(row, selected, budget, review_key) if row else None


def _same_request(row, selected, budget, review_key):
    record = _public(row)
    ticket = record["ticket"]
    # Legacy tickets without a saved key can only prove the other inputs.
    saved_key = ticket.get("review_key")
    if (selected != (ticket["venue"], ticket["instrument"], ticket["side"]) or budget != ticket["budget"]
            or (saved_key is not None and review_key != saved_key)):
        raise RequestConflict("This paper order ID belongs to a different order or reviewed quote")
    return record


def record(data, fixture, request_id, ticket, witness):
    event, bucket, book, info = witness
    size = ticket["budget"]
    selected = ticket["venue"], ticket["instrument"], ticket["side"]
    clock = parse_time(ticket["at"])
    due, deadline = trade.recheck_window(clock, None, fixture)
    clock_basis = paper_eligibility.RECEIPT_CLOCK if fixture else paper_eligibility.APP_CLOCK
    checked = paper_eligibility.qualify(
        event, bucket, selected[2], book, clock, fee_at=fee_coefficient, fee_clock=clock,
        freshness="legacy" if fixture else trade.FRESHNESS, clock_basis=clock_basis,
        listing=ticket.get("listing_evidence"), fixture=fixture,
        max_exchange_age=None if fixture else trade.EXCHANGE_AGE_SECONDS,
    )
    if gap := checked.reason or paper_eligibility.reaction_status(checked, due):
        raise ValueError(gap)
    clean_event = {key: event[key] for key in (
        "id", "native_id", "title", "station", "date", "standard_utc_offset_hours", "starts_at",
        "ends_at", "climate_starts_at", "climate_ends_at", "settles_at",
    )}
    clean_event["ends_at"] = checked.closes_at
    reaction = int((due - clock).total_seconds())
    clean_event["family"] = event.get("family", "daily-high")
    clean_event["venue"] = selected[0]
    clean_event["buckets"] = [{k: v for k, v in item.items() if k not in {"books", "prices", "settlement"}}
                              for item in event["buckets"]]
    if not fixture:
        for item in clean_event["buckets"]:
            if item["id"] == bucket["id"]:
                item["listing_revisions"] = [checked.listing]
    for key in ("forecasts", "observations", "research_observations", "official_reports"):
        clean_event[key] = [item for item in event.get(key, []) if parse_time(item["available_at"]) <= clock]
    with _ledger(data) as (db, _):
        row = _find(db, fixture, request_id)
        if row:
            return _same_request(row, selected, size, ticket.get("review_key"))
        cash, _, groups = _allocation(db, fixture)
        station_room = Decimal(trade.STATION_LIMIT) - groups.get((ticket["station"], ticket["day"]), Decimal("0"))
        cost = decimal(ticket["order"]["max_loss"])
        if cost > cash:
            raise ValueError("Insufficient available paper cash. Lower the budget.")
        if cost > station_room:
            raise ValueError(f"Only ${max(0, station_room):.2f} remains under the paper station-day cap. Lower the budget.")
        intent = {
            "ticket": ticket, "event": clean_event, "signal_book": book, "signal_info": info,
            "native_id": bucket["market_id"], "reaction": reaction, "due": iso(due),
            "deadline": iso(deadline),
            "cash": float(cash), "station_room": float(station_room),
            "policy": paper_eligibility.POLICY, "clock_basis": clock_basis,
            "fee_schedule": frozen_schedule(), "fee_clock": iso(clock),
            "selection": {**_reviewed_selection(ticket, book, fixture), "deadline": iso(deadline)},
        }
        db.execute("INSERT INTO orders VALUES (?,?,?,?,?,NULL)",
                   (fixture, request_id, iso(clock), "Pending recheck", venue_archive.dumps(intent)))
        return _public(_find(db, fixture, request_id))


def _recheck_books(data, fixture, capture, intent, due, end, archive_through):
    ticket = intent["ticket"]
    if fixture:
        return [book for event in (capture or {}).get("events", []) for bucket in event["buckets"]
                if bucket["id"] == ticket["instrument"] and bucket["market_id"] == intent["native_id"]
                for book in bucket["books"] if due <= parse_time(book["at"]) <= end]
    start = parse_time(intent["signal_book"]["at"]) + timedelta(microseconds=1)
    required = (parse_time(ticket["at"]), archive_through)
    if intent.get("policy"):
        listing = ticket["listing_evidence"]
        main = capture_index.latest_books(
            data / "captures", intent["native_id"], exact_instrument=ticket["instrument"],
            start=start, end=end, limit=None, member=listing, required_window=required,
        )
        with venue_archive.connect(data) as db:
            member = db.execute("SELECT event_id,native_event_id,native_id,yes_side_id,no_side_id FROM markets "
                                "WHERE venue=? AND instrument_id=?",
                                (trade.POLY, ticket["instrument"])).fetchone()
            native = (venue_archive._book_query(db, trade.POLY, ticket["instrument"], start=start, end=end)
                      if member and _native_identity(member, intent) else [])
        receipts = {parse_time(book["at"]): book for book in main if parse_time(book["available_at"]) <= end}
        for book in native:
            clock = parse_time(book["at"])
            enriched = {**book, "http_cache": book["http_headers"]}
            available = paper_eligibility.book_clock(enriched, paper_eligibility.APP_CLOCK)
            if available and available > end:
                continue
            previous = receipts.get(clock)
            prior_clock = paper_eligibility.book_clock(previous, paper_eligibility.APP_CLOCK) if previous else None
            if not previous or (available and (prior_clock is None or available < prior_clock)):
                receipts[clock] = enriched
            elif available and available == prior_clock and not trade._same_image(previous, enriched):
                receipts[clock] = {**enriched, "source_time_progress": "conflicting",
                                   "book_receipt_conflict": True}
        return list(receipts.values())
    if "receipt_id" in intent["signal_book"]:
        with venue_archive.connect(data) as db:
            market = db.execute("SELECT native_id FROM markets WHERE venue=? AND instrument_id=?",
                                (trade.POLY, ticket["instrument"])).fetchone()
            if not market or market["native_id"] != intent["native_id"]:
                return []
            return [{**book, "http_cache": book["http_headers"]} for book in venue_archive._book_query(
                db, trade.POLY, ticket["instrument"], start=start, end=end)]
    return capture_index.latest_books(
        data / "captures", intent["native_id"], start=start, end=end, limit=None,
        exact_instrument=ticket["instrument"], required_window=required,
    )


def _native_identity(row, intent):
    bucket = next(bucket for bucket in intent["event"]["buckets"] if bucket["id"] == intent["ticket"]["instrument"])
    event = intent["event"]
    return all(str(row[field]) == str(value) for field, value in {
        "event_id": event["id"], "native_event_id": event["native_id"], "native_id": bucket["market_id"],
        "yes_side_id": bucket["yes_side_id"], "no_side_id": bucket["no_side_id"],
    }.items())


def _reviewed_selection(ticket, book, fixture):
    return {
        "at": ticket["at"], "instrument": ticket["instrument"], "side": ticket["side"], "book_at": book["at"],
        "limit_price": ticket["order"]["highest_price"], "quantity": ticket["order"]["quantity"],
        "max_exchange_age": None if fixture else trade.EXCHANGE_AGE_SECONDS,
    }


def reviewed_probability(instrument, at, signal_info, predict):
    def lookup(event, clock):
        if clock == at:
            return {instrument: signal_info}
        latest = predict(event, clock).get(instrument)
        if not latest or any(latest.get(field) != signal_info.get(field) for field in ("model_version", "model_method")):
            return {}
        previous_digest, latest_digest = signal_info.get("model_vintage"), latest.get("model_vintage")
        # Old legacy intents identified their artifact through the digest prefix in the version.
        legacy_match = (previous_digest is None and signal_info.get("model_method") == "ridge"
                        and isinstance(latest_digest, str) and model_evidence.DIGEST.fullmatch(latest_digest)
                        and signal_info.get("model_version") == f"knyc-ridge-{latest_digest[:10]}")
        if previous_digest == latest_digest or legacy_match:
            return {instrument: latest}
        return {}
    return lookup


def practice():
    """Run one deterministic practice order in a new in-memory paper balance."""
    tape = capture_fixture.fixture_capture(duration_seconds=16, poll_seconds=8, max_rounds=3, max_events=1)
    instrument = f"tc-{capture_fixture.EVENT}-gte49lt50f"
    selected = (trade.POLY, instrument, "YES")

    def probability(_event, _at):
        return {instrument: {"probability": 0.30, "uncertain": False, "model_version": "synthetic-practice"}}

    at = parse_time(tape["started_at"])
    rows, (_, _, book, _) = trade.market_rows(None, tape, probability, at, fixture=True, selected=selected)
    ticket = rows[0]
    if ticket["blocker"]:
        raise ValueError(ticket["blocker"])
    run = paper_runs.create_run(
        tape, probability, order_size=ticket["order"]["max_loss"], reaction_seconds=5,
        selection=_reviewed_selection(ticket, book, True), bankroll=trade.BANKROLL, max_station_day=trade.STATION_LIMIT,
    )
    result = run["result"]
    position = next(iter(result["positions"]), None)
    return {
        "ticket": ticket, "position": position, "reason": result["attempts"][0]["result"],
        "starting_balance": trade.BANKROLL,
        "available_balance": trade.BANKROLL - (position["max_loss"] if position else 0),
    }


def _evaluate(data, fixture, capture, intent, as_of, predict):
    ticket = intent["ticket"]
    policy = intent.get("policy")
    if policy not in {None, paper_eligibility.POLICY}:
        raise ValueError("This paper intent requires a different eligibility policy")
    due, deadline = map(parse_time, (intent["due"], intent["deadline"]))
    if as_of < due and not fixture:
        return None
    if fixture and as_of < due:
        return "Skipped", {"reason": "Fixture has no retained receipt through the reaction time"}, None
    book = deepcopy(intent["signal_book"])
    horizon = min(as_of, deadline)
    wait_until = deadline if policy else deadline + timedelta(seconds=collector.BOOK_INTERVAL)
    candidates = _recheck_books(data, fixture, capture, intent, due, horizon, min(as_of, wait_until))
    event = deepcopy(intent["event"])
    if not fixture:
        weather = collector.weather_events(data, start=event["date"], end=event["date"], station=event["station"],
                                           through=horizon, strict=True)
        event = collector.enrich_capture(data, {"kind": "live", "events": [event]}, weather=weather)["events"][0]
    for bucket in event["buckets"]:
        bucket["books"] = [book] if bucket["id"] == ticket["instrument"] and bucket["market_id"] == intent["native_id"] else []
    selection = intent["selection"] if policy else _reviewed_selection(ticket, book, fixture)
    if policy and not fixture:
        event = strategy_native_capture.with_listing_evidence(data, {"events": [event]}, horizon, refresh=True)["events"][0]
        for bucket in event["buckets"]:
            if bucket["books"]:
                # The reviewed listing remains part of the intent even if the
                # archive can only supply later revisions during reconciliation.
                prior = ticket["listing_evidence"]
                if all(r["receipt_id"] != prior["receipt_id"] for r in bucket["listing_revisions"]):
                    bucket["listing_revisions"].append(prior)

    selected_probability = reviewed_probability(ticket["instrument"], parse_time(ticket["at"]),
                                                 intent["signal_info"], predict)

    def freeze(through, *, include_unknown=False):
        # In v3 an image can affect source progress only when the app could
        # see it. A missing publication clock can be reported, never used.
        basis = intent.get("clock_basis", paper_eligibility.RECEIPT_CLOCK)
        visible, unknown = [], []
        for receipt in candidates:
            at = paper_eligibility.book_clock(receipt, basis)
            if at is None and include_unknown:
                unknown.append(receipt.copy())
            elif at is not None and at <= through:
                visible.append((at, parse_time(receipt["at"]), receipt.copy()))
        books = [book.copy(), *(receipt for _, _, receipt in sorted(visible, key=lambda row: row[:2]))]
        mark_book_progress(books)
        for receipt in books:
            if receipt.get("book_receipt_conflict"):
                receipt["source_time_progress"] = "conflicting"
        selected = next(bucket for bucket in event["buckets"] if bucket["books"])
        selected["books"] = books + unknown
        return paper_runs.create_run(
            {"kind": "fixture" if fixture else "live", "events": [event], "captured_at": iso(through),
             "started_at": book["at"]}, selected_probability, as_of=through, order_size=ticket["order"]["max_loss"],
            reaction_seconds=intent["reaction"], freshness=None if fixture else trade.FRESHNESS,
            selection=selection, bankroll=intent["cash"], max_station_day=intent["station_room"],
            engine=paper_runs.ENGINE if policy else paper_runs.LEGACY_ENGINE,
            clock_basis=intent.get("clock_basis"), fee_schedule=intent.get("fee_schedule"),
        )

    run = freeze(horizon)
    attempts = run["result"]["attempts"]
    attempt = attempts[0] if attempts else None
    if attempt and not attempt["reaction_at"] and not fixture and as_of < wait_until:
        # A collected receipt may still be in the batch being written to disk.
        return None
    if policy and attempt and attempt["reaction_at"]:
        # The first eligible decision ends the entry. In particular, an
        # offline restart must not move this freeze past a later payout.
        reaction = parse_time(attempt["reaction_at"])
        if reaction < horizon:
            run = freeze(reaction)
            attempt = run["result"]["attempts"][0]
    elif policy and not fixture and any(not candidate.get("available_at") for candidate in candidates):
        run = freeze(horizon, include_unknown=True)
        attempts = run["result"]["attempts"]
        attempt = attempts[0] if attempts else None
    positions = run["result"]["positions"]
    return ("Simulated" if positions else "Skipped"), {
        "reason": (attempt["result"] if attempt else next(
            (row["reason"] for row in run["result"].get("signal_rejections", [])),
            "Reviewed signal could not be reproduced",
        )),
        "run_id": run["id"], "position": positions[0] if positions else None, "attempt": attempt,
    }, run


def _payout(data, root, result, intent, as_of):
    position = result.get("position")
    instrument = position["bucket_id"]
    run = paper_runs.load_run(root, result["run_id"])
    previous = parse_time(run["inputs"]["as_of"])
    with venue_archive.connect(data) as db:
        row = db.execute(
            """SELECT m.event_id,m.native_event_id,m.native_id,m.yes_side_id,m.no_side_id,
                      m.yes_payout,m.settlement_received_at,
                      r.id,r.venue,r.kind,r.source,r.received_at,r.status,r.digest,b.compressed
               FROM markets m LEFT JOIN receipts r ON r.id=m.settlement_receipt_id
               LEFT JOIN bodies b ON b.digest=r.digest
               WHERE m.venue=? AND m.instrument_id=? AND m.status='settled'""", (trade.POLY, instrument),
        ).fetchone()
    payout = None
    if row and _native_identity(row, intent) and row["settlement_received_at"] and row["yes_payout"] is not None:
        clock = parse_time(row["settlement_received_at"])
        if previous < clock <= as_of:
            if (row["venue"] != trade.POLY or row["kind"] != "settlement" or row["status"] != 200
                    or not row["compressed"] or parse_time(row["received_at"]) != clock):
                raise ValueError("Native payout is missing its exact archived receipt")
            body = zlib.decompress(row["compressed"])
            value = venue_contracts.poly_settlement(json.loads(body), instrument)["yes_payout"]
            if sha256(body).hexdigest() != row["digest"] or decimal(value) != decimal(row["yes_payout"]):
                raise ValueError("Native payout disagrees with its archived receipt")
            payout = {"yes_payout": value, "available_at": iso(clock), "source": row["source"],
                      "receipt_id": row["id"], "receipt_sha256": row["digest"]}
    if not payout and "receipt_id" not in intent["signal_book"]:
        receipts, issues = capture_index.read(
            data / "captures", capture._validate_capture, kind="live", version=capture.SCHEMA_VERSION,
            since=previous, through=as_of, instrument=instrument,
        )
        capture_index.require_readable(issues, previous, as_of)
        expected = f"{capture.GATEWAY}/v1/markets/{instrument}/settlement"
        for filename, _, digest, _, raw in receipts:
            candidate = json.loads(raw).get("settlements", {}).get(intent["event"]["id"], {}).get(instrument)
            if (candidate and candidate.get("source") == expected
                    and previous < parse_time(candidate["available_at"]) <= as_of):
                payout = {**candidate, "capture_file": filename, "capture_sha256": digest.hex()}
                break
    if payout is None:
        return None
    later = {"captured_at": payout["available_at"], "events": [{"buckets": [
        {"id": instrument, "market_id": intent["native_id"], "settlement": payout}]}]}
    child = paper_runs.follow_payouts(run, later)
    return "Simulated", {**result, "run_id": child["id"], "position": child["result"]["positions"][0]}, child


def orders(data, fixture):
    """One read snapshot contains order rows, reservations and realized payouts."""
    with _read_ledger(data) as db:
        cash, committed, exposures = _allocation(db, fixture) if db is not None else (trade.BANKROLL, 0, {})
        recent = db.execute("SELECT * FROM orders WHERE fixture=? ORDER BY created_at DESC, rowid DESC LIMIT 30",
                            (fixture,)).fetchall() if db is not None else []
        active = committed > 0
        if fixture and active:
            # Fixtures have no payout feed; poll only for rechecks.
            active = db.execute("SELECT 1 FROM orders WHERE fixture=1 AND status='Pending recheck' LIMIT 1").fetchone() is not None
        return {"orders": [_public(row) for row in recent], "cash": float(cash), "committed": float(committed),
                "station_limit": trade.STATION_LIMIT, "active": active, "exposure": [
                    {"station": station, "day": day, "cost": float(cost)} for (station, day), cost in exposures.items()
                ]}


def _key(row):
    return row["fixture"], row["request_id"]


def _expected(row):
    return sha256(venue_archive.dumps([row["status"], row["intent"], row["result"]]).encode()).hexdigest()


def _proposal(data, row):
    with _read_ledger(data) as db:
        if db is None or not db.execute("SELECT 1 FROM sqlite_schema WHERE name='reconciliation'").fetchone():
            return None
        saved = db.execute("SELECT expected,proposal FROM reconciliation WHERE fixture=? AND request_id=?",
                           _key(row)).fetchone()
    if saved:
        if saved["expected"] != _expected(row):
            raise ValueError("Saved reconciliation no longer matches its order or parent run")
        return saved["proposal"]
    return None


def _record_proposal(data, row, evaluated):
    proposal = venue_archive.dumps(evaluated)
    with _ledger(data) as (db, _):
        current = _find(db, *_key(row))
        if current is None or _expected(current) != _expected(row):
            return None
        db.execute(OUTBOX)
        db.execute("INSERT OR IGNORE INTO reconciliation VALUES (?,?,?,?)",
                   (*_key(row), _expected(row), proposal))
    return _proposal(data, row)


def _publish(root, run):
    if run is None:
        return
    stored = paper_runs.store_run(root, run, durable=True)
    if paper_runs._json([stored["inputs"], stored["result"]]) != paper_runs._json([run["inputs"], run["result"]]):
        raise ValueError("Published paper run disagrees with the saved reconciliation")
    paper_runs.verify_run(stored)


def _finalize(data, row, proposal, status, result):
    with _ledger(data) as (db, _):
        updated = db.execute("""
            UPDATE orders SET status=?,result=?
            WHERE fixture=? AND request_id=? AND status=? AND intent=? AND result IS ?
            AND EXISTS (SELECT 1 FROM reconciliation WHERE fixture=? AND request_id=? AND expected=? AND proposal=?)
        """, (status, venue_archive.dumps(result), *_key(row), row["status"], row["intent"], row["result"],
              *_key(row), _expected(row), proposal))
        if updated.rowcount:
            db.execute("DELETE FROM reconciliation WHERE fixture=? AND request_id=?", _key(row))
            return _find(db, *_key(row))
    return None


def _advance(data, row, evaluate):
    # Freeze -> publish and verify -> compare-and-update. A restart after the
    # first commit never consults the books, model or payout for this transition.
    proposal = _proposal(data, row)
    if proposal is None:
        evaluated = evaluate()
        if evaluated is None or (proposal := _record_proposal(data, row, evaluated)) is None:
            return None
    status, result, run = json.loads(proposal)
    _publish(data / "trade-paper", run)
    return _finalize(data, row, proposal, status, result)


def reconcile(data, as_of, fixture_tape, predict_for, *, report=None):
    """One pass by the listener holding the data-root owner lock."""
    with _read_ledger(data) as db:
        if db is None:
            return
        work = db.execute("""
            SELECT * FROM orders WHERE status='Pending recheck' OR (fixture=0 AND status='Simulated')
            ORDER BY created_at, rowid
        """).fetchall()
    root = data / "trade-paper"
    for row in work:
        original = row
        try:
            intent = json.loads(row["intent"])
            fixture = bool(row["fixture"])
            if row["status"] == "Pending recheck":
                def entry():
                    tape = fixture_tape() if fixture else None
                    if fixture and not tape:
                        raise OSError("Fixture archive is unavailable")
                    cutoff = parse_time(tape["captured_at"]) if fixture else as_of
                    lookup = None

                    def predict(event, at):
                        nonlocal lookup
                        if lookup is None:
                            lookup = predict_for(intent)
                        return lookup(event, at)

                    return _evaluate(data, fixture, tape, intent, cutoff, predict)

                row = _advance(data, row, entry)
            if row is not None and row["status"] == "Simulated" and not fixture:
                result = json.loads(row["result"])
                evidence = (result.get("attempt") or {}).get("recheck_evidence") or {}
                closes = evidence.get("closes_at") or intent["event"]["ends_at"]
                if not result["position"]["settled"] and as_of >= parse_time(closes):
                    _advance(data, row, lambda: _payout(data, root, result, intent, as_of))
        except Exception as error:
            if report is None:
                LOG.warning("Paper order %s (%s): %s: %s", original["request_id"], original["status"],
                            type(error).__name__, error)
            else:
                report(_key(original), f"{type(error).__name__}: {error}")
        else:
            if report is not None:
                report(_key(original), None)
