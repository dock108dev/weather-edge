"""Resolve fee terms and the receipts that make them usable at a decision clock."""

from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
from urllib.parse import parse_qs, urlsplit

from . import listing_evidence, poly_fee_policy
from .market import decimal, iso, parse_time
from .venue_archive import KALSHI, connect
from .venue_contracts import KALSHI_API

FEE_SCHEDULE = "https://kalshi.com/docs/kalshi-fee-schedule.pdf"
ROUNDING = "https://docs.kalshi.com/getting_started/fee_rounding"
EFFECTIVE_SCHEDULE = datetime(2026, 7, 7, tzinfo=timezone.utc)
FEE_TYPES = {"quadratic", "quadratic_with_maker_fees"}


def _gap(reason, *, status="missing", **proof):
    return {**proof, "available": False, "status": status, "reason": reason}


def poly_terms(listing, at):
    at = parse_time(at)
    effective, rate = poly_fee_policy.version(at)
    proof = {"schedule": poly_fee_policy.SOURCE, "policy_coefficient": str(rate),
             "policy_effective_at": iso(effective), "lock_at": iso(at),
             "policy_basis": "Paper Tier 1 assumption; historical schedule documents are not receipt timed"}
    if poly_fee_policy.FILING_DAY <= at < poly_fee_policy.FEE_CHANGE:
        proof.update(policy_warning="Later regulatory filing describes different November 6 terms. "
                                    "The paper rate in this period is an assumption.",
                     policy_warning_source=poly_fee_policy.TIER_CHANGE_FILING)
    if listing["status"] != "verified":
        return _gap(listing["reason"], status=listing["status"], **proof)
    proof.update(listing_receipt_id=listing["receipt_id"], listing_received_at=listing["received_at"],
                 advertised_coefficient=listing.get("fee_multiplier"))
    if listing.get("fee_multiplier") is None:
        return _gap("Exact listing does not advertise a fee coefficient", **proof)
    try:
        if decimal(listing["fee_multiplier"]) == rate:
            return {**proof, "status": "verified", "available": True}
    except ValueError:
        return _gap("Invalid fee coefficient in exact listing", status="contradictory", **proof)
    return _gap("Listed fee differs from the retained fee schedule", status="contradictory", **proof)


def kalshi_taker_fee(price, size, rate):
    return (rate * size * price * (1 - price)).quantize(Decimal("0.000001"), rounding=ROUND_CEILING)


def kalshi_buy_debit(notional, fee):
    return (notional + fee).quantize(Decimal("0.01"), rounding=ROUND_CEILING)


def _effective(records, identity, value, at):
    changes = [row for row in records if row.get(identity) == value and parse_time(row["scheduled_ts"]) <= at]
    changes.sort(key=lambda row: parse_time(row["scheduled_ts"]))
    if changes:
        clock = parse_time(changes[-1]["scheduled_ts"])
        if any(row != changes[-1] for row in changes if parse_time(row["scheduled_ts"]) == clock):
            raise ValueError("Fee changes disagree at the same effective clock")
    return changes[-1] if changes else {}


def _fee_documents(db, at):
    """Complete event manifests can be recovered even after metadata is overwritten."""
    root = KALSHI_API + "/"
    series_source, event_source = root + "series/fee_changes", root + "events/fee_changes"
    series = None
    for receipt, payload in listing_evidence.replies(db, KALSHI, ("fee-series",), at, newest=True):
        url = urlsplit(receipt["source"])
        if receipt["source"].split("?")[0] == series_source and parse_qs(url.query).get("show_historical") == ["true"]:
            series = receipt, payload.get("series_fee_change_arr")
            break

    complete = None
    pages, changes, expected, seen = [], [], None, set()
    for receipt, payload in listing_evidence.replies(db, KALSHI, ("fee-event",), at):
        if receipt["source"].split("?")[0] != event_source:
            continue
        request = parse_qs(urlsplit(receipt["source"]).query, keep_blank_values=True).get("cursor", [""])
        if request == [""]:
            pages, changes, expected, seen = [], [], "", set()
        after = payload.get("cursor")
        rows = payload.get("event_fee_changes")
        if (expected is None or request != [expected] or not isinstance(after, str)
                or after in seen or not isinstance(rows, list)
                or not all(isinstance(row, dict) for row in rows)):
            pages, changes, expected, seen = [], [], None, set()
            continue
        pages.append(receipt)
        changes.extend(rows)
        if after:
            seen.add(after)
            expected = after
        else:
            complete = pages, changes
            pages, changes, expected, seen = [], [], None, set()
    return series, complete


def kalshi_terms(data, instrument, at):
    """Use only local receipts and effective changes available at the fee lock clock."""
    at = parse_time(at)
    with connect(data) as db:
        db.execute("BEGIN")
        member = db.execute("SELECT * FROM markets WHERE venue=? AND instrument_id=?", (KALSHI, instrument)).fetchone()
        if member is None:
            return _gap("Exact Kalshi instrument identity is unavailable")
        member = dict(member)
        sources = listing_evidence.kalshi_sources(db, member, at)
        return kalshi_terms_at(db, member, sources, listing_evidence.kalshi_listing(member, sources), at)


def kalshi_terms_at(db, member, sources, listing, at):
    proof = {"schedule": FEE_SCHEDULE, "rounding": ROUNDING, "policy_effective_at": iso(EFFECTIVE_SCHEDULE),
             "series_id": member["series_id"], "event_id": member["event_id"],
             "instrument_id": member["instrument_id"], "lock_at": iso(at),
             "policy_basis": "General taker schedule; whole contracts and non-direct member cent balance"}
    if listing["status"] != "verified":
        return _gap(listing["reason"], status=listing["status"], **proof)
    proof.update(listing_receipt_id=listing["receipt_id"], listing_received_at=listing["received_at"])
    if at < EFFECTIVE_SCHEDULE:
        return _gap("Request predates the retained general Kalshi fee schedule", **proof)

    series, event, market = sources
    series_changes, event_changes = _fee_documents(db, at)
    proof.update(series_receipt=series[0] if series else None, event_receipt=event[0] if event else None,
                 series_change_proof=series_changes[0] if series_changes else None,
                 event_change_receipts=event_changes[0] if event_changes else [])
    if not series:
        return _gap("No exact Kalshi series terms receipt available by this clock", **proof)
    if series[1] is None or (event and event[1] is None):
        return _gap("Kalshi series or event terms receipts disagree", status="contradictory", **proof)
    if not series_changes:
        return _gap("Kalshi series fee history was not received by this clock", **proof)
    if not event_changes:
        return _gap("Complete Kalshi event fee receipt set was not available by this clock", **proof)

    series_body, event_body = series[1], event[1] if event else {}
    try:
        history = series_changes[1]
        if not isinstance(history, list) or not all(isinstance(row, dict) for row in history):
            raise ValueError("Invalid retained series fee change list")
        historical = _effective(history, "series_ticker", member["series_id"], at)
        event_override = _effective(event_changes[1], "event_ticker", member["event_id"], at)
        future_change = any(row.get("series_ticker") == member["series_id"]
                            and parse_time(row["scheduled_ts"]) > at for row in history)
        proof.update(effective_change=historical.get("scheduled_ts"), event_effective_change=event_override.get("scheduled_ts"),
                     fee_waiver_expiration_at=listing["fee_waiver_expiration_at"],
                     fee_waiver_starts_at=listing["fee_waiver_starts_at"])
        if not historical and future_change:
            return _gap("Series terms before the first scheduled change are unresolved", **proof)
        kind = historical.get("fee_type", series_body.get("fee_type"))
        multiplier = historical.get("fee_multiplier", series_body.get("fee_multiplier"))
        for key in ("fee_type_override", "fee_multiplier_override"):
            advertised = event_body.get(key)
            override = event_override.get(key)
            if advertised is not None and not event_override:
                return _gap("Event-specific fee override has no verified effective start", **proof)
            if (advertised is not None and parse_time(event[0]["received_at"]) >= parse_time(event_override["scheduled_ts"])
                    and (override is None or (advertised != override if key == "fee_type_override"
                                              else decimal(advertised) != decimal(override)))):
                return _gap("Scheduled fee override disagrees with the exact event receipt", status="contradictory", **proof)
        kind = event_override.get("fee_type_override") if event_override.get("fee_type_override") is not None else kind
        multiplier = (event_override.get("fee_multiplier_override")
                      if event_override.get("fee_multiplier_override") is not None else multiplier)
        if kind is None or multiplier is None:
            return _gap("Series fee type or multiplier missing from the retained terms", **proof)
        if kind not in FEE_TYPES or decimal(multiplier) < 0:
            raise ValueError("Unsupported Kalshi fee type or multiplier")
        expiry = listing["fee_waiver_expiration_at"]
        start = listing["fee_waiver_starts_at"]
        if listing["waiver_clock_invalid"] or (start and expiry and parse_time(start) >= parse_time(expiry)):
            raise ValueError("Kalshi instrument waiver has invalid clocks")
        if expiry and at < parse_time(expiry):
            if not start:
                return _gap("Active instrument waiver requires separate start evidence", **proof)
            if parse_time(start) <= at:
                multiplier = 0
    except (TypeError, KeyError, ValueError) as error:
        return _gap(str(error), status="contradictory", **proof)

    return {
        **proof, "available": True, "status": "verified", "type": kind, "multiplier": str(multiplier), "taker_rate": "0.07",
        "price_ranges": market[1].get("price_ranges"),
        "series_change_source": series_changes[0]["source"], "series_change_receipt": series_changes[0]["received_at"],
        "event_change_source": event_changes[0][-1]["source"], "event_change_receipt": event_changes[0][-1]["received_at"],
    }
