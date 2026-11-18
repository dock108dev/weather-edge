"""Point-in-time evidence required to price an exact paper side."""

from dataclasses import dataclass
from datetime import timedelta

from . import listing_evidence
from .market import MAX_QUOTE_AGE, OPEN, _ladder, _normalized_ladders, decimal, iso, parse_time, source_time_key
from .strategy_rules import EXCHANGE_AGE_SECONDS, book_status, exchange_clock_status

POLICY = "witnessed-paper-v1"
FEE_POLICY = "polymarket-us-tier-1-decision-lock-v1"
APP_CLOCK = "app-availability"
RECEIPT_CLOCK = "http-receipt"


def book_clock(book, basis):
    if basis == APP_CLOCK:
        return parse_time(book["available_at"]) if book.get("available_at") else None
    if basis == RECEIPT_CLOCK:
        return parse_time(book["at"])
    raise ValueError("Unknown paper book clock basis")


def same_image(first, second):
    return (first.get("depth_kind") == second.get("depth_kind") == "displayed_ladder"
            and first.get("source_at") and second.get("source_at")
            and first.get("state") == second.get("state")
            and source_time_key(first["source_at"]) == source_time_key(second["source_at"])
            and _normalized_ladders(first) == _normalized_ladders(second))


def origin_witness(books, at, latest, public_book=None):
    """A matching cached copy can carry forward a current origin preview."""
    freshness = "origin-confirmed"
    if (not latest or latest.get("state") not in OPEN or latest.get("depth_kind") != "displayed_ladder"
            or book_status(latest, None, parse_time(latest["at"]), freshness) not in {
                "Cached response with recorded Age", "Cached response without positive Age",
            }):
        return latest
    through = parse_time(latest["at"])
    floor = at - timedelta(seconds=MAX_QUOTE_AGE)
    for candidate in sorted((book for book in books if floor <= parse_time(book["at"]) < through),
                            key=lambda book: parse_time(book["at"]), reverse=True):
        received = parse_time(candidate["at"])
        if not same_image(latest, candidate) or candidate.get("source_time_progress") in {"regressed", "conflicting"}:
            break
        if public_book and received < parse_time(public_book["at"]) <= at and not same_image(candidate, public_book):
            break
        if (book_status(candidate, None, received, freshness) is None
                and exchange_clock_status(candidate, at, EXCHANGE_AGE_SECONDS) is None):
            return candidate
    return latest


@dataclass(frozen=True)
class Qualification:
    reason: str | None
    category: str | None
    listing: dict | None
    closes_at: str | None
    cutoff_reason: str | None

    def detail(self, phase, at, book):
        return {
            "phase": phase, "reason": self.reason, "category": self.category, "at": iso(at),
            "closes_at": self.closes_at, "cutoff_reason": self.cutoff_reason,
            "listing_receipt_id": (self.listing or {}).get("receipt_id"),
            "listing_conflicting_receipts": (self.listing or {}).get("conflicting_receipts"),
            "book_at": (book or {}).get("at"), "book_available_at": (book or {}).get("available_at"),
            "book_receipt_id": (book or {}).get("receipt_id"), "exchange_image_at": (book or {}).get("source_at"),
        }


def qualify(event, bucket, side, book, at, *, fee_at, fee_clock, freshness, clock_basis,
            listing=None, fixture=False, due=None, deadline=None, max_exchange_age=EXCHANGE_AGE_SECONDS):
    """Check evidence only. Pricing, edge, and allocation remain with callers."""
    if not fixture and listing is None:
        listing = listing_evidence.select(bucket.get("listing_revisions", []), at)
    opens, ends = [], []
    if fixture:
        opens = [event.get("starts_at")]
        ends = [(event["ends_at"], "Market closed")]
    elif listing["status"] == "verified":
        opens = [listing.get(key) for key in ("opened_at", "event_opened_at")]
        ends = [(listing.get("listed_event_end_at"), "Listed event window ended"),
                (listing.get("closed_at"), "Listed native market window ended")]
    cutoff, cutoff_reason = min(
        ((clock, reason) for clock, reason in ends if clock),
        key=lambda item: parse_time(item[0]), default=(None, None),
    )

    def result(reason=None, category=None):
        return Qualification(reason, category, listing, cutoff, cutoff_reason)

    if not fixture:
        if listing["status"] != "verified":
            return result(listing["reason"], "listing")
        identity = {"event_id": event["id"], "native_event_id": event.get("native_id"),
                    "native_id": bucket.get("market_id"), "yes_side_id": bucket.get("yes_side_id"),
                    "no_side_id": bucket.get("no_side_id")}
        if any(value is None or str(listing.get(key)) != str(value) for key, value in identity.items()):
            return result("Listing disagrees with the exact event, market or side identity", "listing")
        missing = next((label for key, label in (
            ("opened_at", "scheduled opening"), ("listed_event_end_at", "listed event end"),
            ("closed_at", "native market close"), ("quantity_step", "quantity step"),
        ) if not listing.get(key)), None)
        if missing:
            return result(f"Exact listing is missing the {missing}", "listing")
        if decimal(listing["quantity_step"]) != decimal(bucket["quantity_step"]):
            return result("Listed quantity step disagrees with the retained contract", "listing")
    if any(clock and at < parse_time(clock) for clock in opens):
        return result("Market upcoming", "listing")
    if cutoff and at >= parse_time(cutoff):
        return result(cutoff_reason, "listing")
    if not fixture and listing.get("market_status") != "open":
        state = listing.get("market_status")
        return result(f"Market {state}" if state else "Open market status unavailable", "listing")
    if book and book.get("depth_kind") == "displayed_ladder":
        received = parse_time(book["at"])
        available = book_clock(book, clock_basis)
        if available is None:
            return result("First app availability of book is unverified", "clock")
        if not received <= available <= at:
            return result("Book was not available at the decision clock", "clock")
        if due and received < due:
            return result("Book receipt predates the reaction time", "clock")
        if deadline and available > deadline:
            return result("Book became available after the recheck deadline", "clock")
        if (at - received).total_seconds() + (book.get("cache_age_seconds") or 0) > MAX_QUOTE_AGE:
            return result("Book is stale", "stale")
    gap = book_status(book, side, parse_time(book["at"]) if book else at, freshness)
    if gap:
        category = "freshness" if "Age" in gap or "origin evidence" in gap else "depth"
        return result(gap, category)
    if not _ladder(book, side):
        return result("No displayed liquidity on this side", "depth")
    if max_exchange_age is not None and (gap := exchange_clock_status(book, at, max_exchange_age)):
        return result(gap, "stale" if gap == "Exchange image is stale" else "clock")
    if not fixture:
        advertised = listing.get("fee_multiplier")
        if advertised is None:
            return result("Exact listing does not advertise a fee coefficient", "fees")
        try:
            rate = decimal(advertised)
        except ValueError:
            return result("Invalid fee coefficient in exact listing", "fees")
        # The order retains its insertion rate even if a later listing
        # advertises the valid rate for new orders after a schedule change.
        if rate not in {fee_at(fee_clock), fee_at(at)}:
            return result("Listed fee differs from the retained fee schedule", "fees")
    return result()


def reaction_status(qualification, due):
    if qualification.closes_at and due >= parse_time(qualification.closes_at):
        return "Market closes before the paper recheck"
    return None
