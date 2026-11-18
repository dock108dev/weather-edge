"""Time-qualified trade previews backed by retained public evidence."""

from collections import Counter
from datetime import timedelta
from decimal import Decimal
import hashlib
import json
import re
import sqlite3

from . import capture_index, collector, listing_evidence, paper_eligibility, strategy_native_capture, venue_archive, venue_fees
from .market import (
    MAX_QUOTE_AGE, ONE, _ladder, _latest_book, _price, decimal, fee_coefficient, iso, parse_time, taker_fee,
)
from .paper_eligibility import origin_witness, same_image as _same_image
from .strategy_rules import EXCHANGE_AGE_SECONDS, temperature_window

POLY, KALSHI = venue_archive.POLY, venue_archive.KALSHI
SIDES = ("YES", "NO")
FRESHNESS = "origin-confirmed"
DEFAULT_BUDGET = 25
BANKROLL = 500
STATION_LIMIT = 25
REACTION_SECONDS = 30
RECHECK_LAG = 15
READINESS_BASIS = "unchanged-origin-image"
FEED_SCOPES = {
    collector.BOOKS: (POLY, venue_archive.POLY_HIGH_PRIORITY),
    collector.POLY_WEATHER_BOOKS: (POLY, venue_archive.POLY_LOW_RAIN_PRIORITY),
    collector.KALSHI_BOOKS: (KALSHI, venue_archive.KALSHI_PRIORITY),
}


def book_lane(venue, family):
    if venue == KALSHI:
        return collector.KALSHI_BOOKS
    return collector.BOOKS if family == "daily-high" else (
        collector.POLY_WEATHER_BOOKS if family in ("daily-low", "daily-rain") else None)


def recheck_window(at, closes_at, fixture):
    due = at + timedelta(seconds=5 if fixture else REACTION_SECONDS)
    ends = due + timedelta(seconds=RECHECK_LAG)
    return due, min(ends, parse_time(closes_at)) if closes_at else ends


def identity(venue, instrument, side):
    if (venue not in venue_archive.VENUES or side not in SIDES or not isinstance(instrument, str)
            or not re.fullmatch(r"[A-Za-z0-9._-]{1,200}", instrument)):
        raise ValueError("Choose an exact exchange contract and side")
    return venue, instrument, side


def order_size(value):
    amount = decimal(value)
    if not 1 <= amount <= 500 or amount % Decimal("0.01"):
        raise ValueError("Enter a budget from $1 to $500, in cents")
    return float(amount)


def exchange_url(row):
    event = row.get("event_id")
    if not event or not re.fullmatch(r"[A-Za-z0-9-]+", event):
        return None
    if row["venue"] == POLY:
        return f"https://polymarket.us/event/{event}"
    series = (row.get("series_id") or "").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", (row.get("series_title") or "").lower()).strip("-")
    if slug and re.fullmatch(r"[a-z0-9]+", series) and event.lower().startswith(series + "-"):
        return f"https://kalshi.com/markets/{series}/{slug}/{event.lower()}"
    return None


def _catalog(data, at, selected):
    where = """m.family IN ('daily-high','daily-low','daily-rain')
               AND m.day BETWEEN ? AND ? AND m.status IN ('open','upcoming','suspended')"""
    args = [(at - timedelta(days=1)).date().isoformat(), (at + timedelta(days=1)).date().isoformat()]
    if selected:
        where = "m.venue=? AND m.instrument_id=?"
        args = selected[:2]
    with venue_archive.connect(data) as db:
        rows = db.execute(
            """SELECT m.*, s.title AS series_title,
                    CASE WHEN m.venue='polymarket-us' AND m.family IN ('daily-high','daily-low','daily-rain')
                         THEN json_extract(e.metadata, '$.endDate') END AS listed_event_end_at,
                    (SELECT json_set(b.body, '$.receipt_id', b.receipt_id, '$.http_headers', json(r.headers),
                                     '$.http_status', r.status, '$.receipt_sha256', r.digest)
                     FROM books b JOIN receipts r ON r.id=b.receipt_id WHERE b.venue=m.venue AND b.instrument_id=m.instrument_id
                     AND b.at<=? ORDER BY b.at DESC LIMIT 1) AS last_book
               FROM markets m LEFT JOIN series s USING(venue,series_id)
               LEFT JOIN events e ON e.venue=m.venue AND e.event_id=m.event_id WHERE """ + where,
            [venue_archive.receipt_time(at), *args],
        )
        return {(row["venue"], row["instrument_id"]): dict(row) for row in rows}


def _contract(event, bucket, market):
    venue = event.get("venue", POLY) if event else market["venue"]
    raw = json.loads(market["raw_market"]) if market else {}
    event_id = market.get("event_id") or (event or {}).get("id")
    zone = (event or {}).get("timezone") or collector.WEATHER_STATIONS.get(market.get("station"), (None, None, "UTC"))[2]
    settle = (event or {}).get("settles_at") or raw.get("expected_expiration_time")
    ends = [value for value in ((event or {}).get("ends_at"), (event or {}).get("native_event_ends_at"),
                                market.get("closed_at"), market.get("listed_event_end_at")) if value]
    proof = market.get("listing_evidence")
    if proof is not None:
        ends = [value for value in (proof.get("listed_event_end_at"), proof.get("closed_at"))
                if value and proof["status"] == "verified"]
        settle = ((proof.get("expected_expiration_at") or proof.get("closed_at"))
                  if proof["status"] == "verified" else None)
    if venue == KALSHI:
        listing_offers = {side: raw.get(f"{side.lower()}_ask_dollars") for side in SIDES}
    else:
        bid = (raw.get("bestBidQuote") or {}).get("value")
        listing_offers = {"YES": (raw.get("bestAskQuote") or {}).get("value"),
                          "NO": str(ONE - decimal(bid)) if bid is not None else None}
    station = (event or {}).get("station") or market.get("station")
    return {
        "venue": venue,
        "instrument": bucket["id"] if bucket else market["instrument_id"],
        "event_id": event_id,
        "title": (event or {}).get("title") or market.get("event_title") or market.get("title"),
        "label": bucket["label"] if bucket else market["label"] or market["title"] or market["instrument_id"],
        "family": (event or {}).get("family") or market.get("family") or "daily-high",
        "station": station,
        "place": collector.station_place(station) or market.get("location_name"),
        "day": (event or {}).get("date") or market.get("day"),
        "timezone": zone,
        "rule": (bucket or {}).get("rules") or market.get("rules_primary"),
        "rules_secondary": (bucket or {}).get("rules_secondary") or market.get("rules_secondary"),
        "rules_url": market.get("rules_url") or (event or {}).get("rules_source"),
        "settles_at": settle,
        "closes_at": min(ends, key=parse_time, default=None),
        "exchange_url": exchange_url({**market, "event_id": event_id, "venue": venue}),
        "paper_supported": venue == POLY and bool(bucket and bucket.get("quantity_step")),
        "listing_offers": listing_offers,
    }


def _meaning(row, bounds, side):
    family = row["family"]
    if bounds and family in {"daily-high", "daily-low"}:
        low, high = bounds
        if low is None:
            condition = f"{high}°F or below" if side == "YES" else f"above {high}°F"
        elif high is None:
            condition = f"{low}°F or above" if side == "YES" else f"below {low}°F"
        elif low == high:
            condition = f"{low}°F" if side == "YES" else f"anything other than {low}°F"
        else:
            condition = f"{low}–{high}°F" if side == "YES" else f"outside {low}–{high}°F"
        target = "high" if family == "daily-high" else "low"
        subject = f"The reported {row['station'] or 'station'} daily {target} on {row['day']}"
        return f"{subject} is {condition}."
    if family == "daily-rain" and row["rule"]:
        return ("The venue’s rain condition is met." if side == "YES" else
                "The venue’s rain condition is not met under its settlement rule.")
    return "The exchange’s condition is met." if side == "YES" else "The exchange’s condition is not met."


def _open_status(event, bucket, market, at):
    if market.get("status") in {"settled", "closed"}:
        return "Market closed"
    if market.get("status") in {"suspended", "upcoming"}:
        return f"Market {market['status']}"
    if market.get("listed_event_end_at") and at >= parse_time(market["listed_event_end_at"]):
        return "Listed event window ended"
    starts = (event or {}).get("starts_at") or market.get("opened_at")
    ends = [(event or {}).get("ends_at"), (event or {}).get("native_event_ends_at"), market.get("closed_at")]
    if any(end and at >= parse_time(end) for end in ends):
        return "Market closed"
    if starts and at < parse_time(starts):
        return "Market upcoming"
    if not bucket and market.get("status") != "open":
        return "Open market status unavailable"
    return None


def _order_metrics(quote, budget, levels, *, fee_rate=None):
    used = quote["fills"]
    result = {key: quote.get(key) for key in (
        "quantity", "entry_price", "fee", "max_loss", "break_even_probability", "ev_dollars", "fills",
    )}
    result["highest_price"] = used[-1][0]
    available_size = sum(quantity for _, quantity in levels)
    result["available_size"] = float(available_size)
    result["payout"] = quote["quantity"]
    limit_price, size = decimal(result["highest_price"]), decimal(quote["quantity"])
    if fee_rate is not None:
        result["cost_at_limit"] = float(limit_price * size + taker_fee([(limit_price, size)], fee_rate))
    remaining = decimal(budget) - decimal(quote["max_loss"])
    # A leftover smaller than one more lot is ordinary quantity rounding.
    step = decimal(quote["step"])
    result["partial"] = None
    if decimal(quote["quantity"]) + step > available_size and remaining >= decimal(used[-1][0]) * step:
        result["partial"] = "Displayed depth limits size"
    elif fee_rate is not None:
        used_by_price = {decimal(price): decimal(size) for price, size in used}
        next_price = next((price for price, size in levels if size - used_by_price.get(price, 0) >= step), None)
        if next_price is not None:
            fees = taker_fee([*used, (next_price, step)], fee_rate) - decimal(quote["fee"])
            if remaining >= next_price * step + fees:
                result["partial"] = "Higher-priced depth would reduce estimated net EV"
    return result


def _poly(event, bucket, book, info, side, at, budget):
    quote = _price(event, bucket, side, book, info, at, decimal(budget), MAX_QUOTE_AGE, evidence_checked=True)
    order = (_order_metrics({**quote, "step": bucket["quantity_step"]}, budget, _ladder(book, side),
                            fee_rate=fee_coefficient(at))
             if quote.get("quantity") else None)
    if not order:
        return None, quote["status"], "depth"
    return order, None, None


def _kalshi_depth(data, market, book, side, at, budget, *, terms=None):
    """Price an indicative whole-contract order on Kalshi's native side ladder."""
    if not book or book.get("depth_kind") != "displayed_ladder":
        return None, (book or {}).get("reason") or "Missing Kalshi book depth"
    age = (at - parse_time(book["at"])).total_seconds() + (book.get("cache_age_seconds") or 0)
    if age > MAX_QUOTE_AGE:
        return None, "Kalshi book receipt is stale"
    headers = book.get("http_headers") or {}
    if book.get("http_status") != 200 or (book.get("cache_age_seconds") or 0) > 0 or headers.get("cf-cache-status") in {"HIT", "STALE", "UPDATING"}:
        return None, "Kalshi book freshness is unverified"
    offers, bids = book.get(f"{side.lower()}_offers"), book.get(f"{side.lower()}_bids")
    if not offers:
        return None, "No displayed Kalshi offers for this side"
    if bids and decimal(bids[0][0]) >= decimal(offers[0][0]):
        return None, "Kalshi book is crossed"
    terms = terms if terms is not None else venue_fees.kalshi_terms(data, market["instrument_id"], at)
    if not terms["available"]:
        return None, f"Kalshi fee coverage unavailable · {terms['reason']}"
    proof = market.get("listing_evidence")
    raw = json.loads(market["raw_market"])
    binary_unit = (proof["binary_dollar_payout"] if proof is not None else
                   raw.get("market_type") == "binary" and decimal(raw.get("notional_value_dollars") or 0) == ONE)
    if not binary_unit:
        return None, "Unverified Kalshi contract payout unit"
    # Use the research account convention: whole contracts and cent-aligned debits.
    # The fee calculation accumulates the per-level micros before cent rounding.
    rate = decimal(terms["taker_rate"]) * decimal(terms["multiplier"])
    levels = [(decimal(p), decimal(q)) for p, q in offers if 0 < decimal(p) < 1]
    quantity = notional = fees = Decimal("0")
    fills = []
    budget = decimal(budget)
    for price, size in levels:
        if size < 1:
            continue
        # All-in cost increases with quantity. Find the deepest affordable
        # whole lot at this level before moving to a worse price.
        low, high = 0, min(int(size), max(0, int((budget - notional - fees) / price)))
        while low < high:
            probe = (low + high + 1) // 2
            probe_fee = venue_fees.kalshi_taker_fee(price, probe, rate)
            if venue_fees.kalshi_buy_debit(notional + price * probe, fees + probe_fee) <= budget:
                low = probe
            else:
                high = probe - 1
        count = low
        if not count:
            break
        quantity += count
        notional += price * count
        fees += venue_fees.kalshi_taker_fee(price, count, rate)
        fills.append([float(price), count])
        if count < int(size):
            break
    if not quantity:
        return None, "Budget or displayed depth is below one whole Kalshi contract"
    cost = venue_fees.kalshi_buy_debit(notional, fees)
    result = _order_metrics({
        "quantity": float(quantity), "entry_price": float(notional / quantity), "fee": float(cost - notional),
        "max_loss": float(cost), "break_even_probability": float(cost / quantity), "fills": fills, "step": 1,
    }, budget, [(price, int(size)) for price, size in levels])
    limit = decimal(result["highest_price"])
    result["cost_at_limit"] = float(venue_fees.kalshi_buy_debit(
        limit * quantity, venue_fees.kalshi_taker_fee(limit, quantity, rate)))
    return result, None


def _side(data, base, event, bucket, market, book, info, side, at, budget, fixture, archived, *, detail,
          newer_book=None, display_book=None):
    row = {**base, "side": side, "budget": budget, "at": iso(at), "archived": archived}
    listing = row.pop("listing_offers")
    listing_proof = market.get("listing_evidence")
    fee_terms = market.get("fee_evidence")
    if listing_proof is not None:
        row["listing_evidence"] = listing_proof
        row["fee_evidence"] = fee_terms
        row["side_id"] = (listing_proof.get(f"{side.lower()}_side_id") if listing_proof["status"] == "verified"
                          else (bucket or {}).get(f"{side.lower()}_side_id"))
        row["verified_payout"] = market.get("verified_payout")
        if row["verified_payout"]:
            yes_payout = decimal(row["verified_payout"]["yes_payout"])
            row["verified_payout"] = {**row["verified_payout"], "side": side, "side_payout":
                                      str(yes_payout if side == "YES" else ONE - yes_payout)}
        row["book_receipt_id"] = (book or {}).get("receipt_id")
    bounds = ((bucket.get("lower_f"), bucket.get("upper_f"))
              if bucket and ("lower_f" in bucket or "upper_f" in bucket) else
              temperature_window(market) if market and market.get("units") == "°F" else None)
    row["meaning"] = _meaning(row, bounds, side)
    yes = (info or {}).get("probability")
    row["probability"] = None if yes is None else yes if side == "YES" else 1 - yes
    row["uncertain"] = (info or {}).get("uncertain", yes is None)
    row["model_method"] = (info or {}).get("model_method")
    row["forecast_at"] = (info or {}).get("forecast_issued_at")
    for key in ("model_vintage", "model_availability", "model_evidence_cutoff", "forecast_available_at"):
        row[key] = (info or {}).get(key)
    row["quote_at"] = (book or {}).get("at")
    row["quote_available_at"] = (book or {}).get("available_at")
    row["quote_source_at"] = (book or {}).get("source_at")
    row["exchange_age"] = (at - parse_time(row["quote_source_at"])).total_seconds() if row["quote_source_at"] else None
    row["cache_age"] = (book or {}).get("cache_age_seconds")
    row["receipt_age"] = (at - parse_time(book["at"])).total_seconds() if book else None
    row["book_age"] = row["receipt_age"] + (row["cache_age"] or 0) if book else None
    row["depth_kind"] = (book or {}).get("depth_kind")
    row["observed_receipt_at"] = (newer_book or book or {}).get("at")
    row["observed_cache_age"] = (newer_book or book or {}).get("cache_age_seconds")
    row["book_lane"] = book_lane(row["venue"], row["family"])
    row["first_listed_at"] = market.get("first_received_at")
    market_status = _open_status(None if listing_proof and listing_proof["status"] == "verified" else event,
                                 bucket, market, at)
    row["market_open"] = market_status is None and not archived
    display_book = display_book or newer_book or book
    shown = _posted_price(row["venue"], side, display_book, listing, market, at, archived)
    row["posted"] = shown
    posted_book = (display_book if shown and shown["kind"] in {"Book offer", "Retained book offer"}
                   else None)
    row["posted_source_at"] = (posted_book or {}).get("source_at")
    row["posted_available_at"] = (posted_book or {}).get("available_at")
    row["posted_image_state"] = _image_state(posted_book, at)
    row["posted_receipt_age"] = (at - parse_time(shown["at"])).total_seconds() if shown else None
    row["posted_exchange_age"] = (
        (at - parse_time(row["posted_source_at"])).total_seconds() if row["posted_source_at"] else None
    )
    reason = "Archived contract evidence" if archived else market_status
    category = "archived" if reason else None
    order = None
    qualification = None
    if not archived and row["paper_supported"]:
        qualification = paper_eligibility.qualify(
            event, bucket, side, book, at, fee_at=fee_coefficient, fee_clock=at,
            freshness="legacy" if fixture else FRESHNESS,
            clock_basis=paper_eligibility.RECEIPT_CLOCK if fixture else paper_eligibility.APP_CLOCK,
            listing=listing_proof, fixture=fixture, max_exchange_age=None if fixture else EXCHANGE_AGE_SECONDS,
        )
        reason, category = qualification.reason, qualification.category
        row["closes_at"] = qualification.closes_at
        if fixture or qualification.listing["status"] == "verified":
            row["market_open"] = category != "listing"
        if not reason:
            order, reason, category = _poly(event, bucket, book, info, side, at, budget)
        if not reason and newer_book and not _same_image(book, newer_book):
            order, reason, category = None, "Newer public book awaits collector confirmation", "freshness"
    elif row["venue"] == KALSHI and (reason is None or archived and detail):
        reason, category = "Kalshi paper rechecks are unavailable", "venue"
        if detail:
            order, gap = _kalshi_depth(data, market, book, side, at, budget, terms=fee_terms)
            if gap:
                reason, category = gap, ("fees" if gap.startswith("Kalshi fee coverage") else
                                         "stale" if "stale" in gap else "depth")
        elif book is None or book.get("depth_kind") != "displayed_ladder":
            reason, category = "Missing Kalshi book depth", "depth"
        elif row["book_age"] > MAX_QUOTE_AGE:
            reason, category = "Kalshi book receipt is stale", "stale"
        elif not book.get(f"{side.lower()}_offers"):
            reason, category = "No displayed Kalshi offers for this side", "depth"
    elif reason is None:
        reason, category = "Display evidence only · no qualified paper book", "depth"
    model_gap = (("Model availability unverified at this clock" if archived else "Model probability unavailable")
                 if row["probability"] is None else
                 "Uncertain model probability" if row["uncertain"] else None)
    if not reason:
        if model_gap:
            reason, category = model_gap, "model"
        elif order["ev_dollars"] <= 0:
            reason, category = "No positive net EV after fees", "edge"
        elif qualification and (gap := paper_eligibility.reaction_status(
                qualification, at + timedelta(seconds=5 if fixture else REACTION_SECONDS))):
            reason, category = gap, "listing"
    row["blocker"], row["category"], row["model_gap"] = reason, category, model_gap
    if qualification:
        row["paper_evidence"] = {**qualification.detail("signal", at, book), "reason": reason, "category": category}
    row["retained_origin"] = not reason and bool(book and newer_book and _same_image(book, newer_book))
    row["order"] = order
    row["valid_for"] = None
    if not reason:
        due, end = recheck_window(at, None, fixture)
        row["recheck_due_at"], row["recheck_ends_at"] = iso(due), iso(end)
    if order:
        row["valid_for"] = MAX_QUOTE_AGE - row["book_age"]
        if not fixture and row["venue"] == POLY:
            row["valid_for"] = min(row["valid_for"], EXCHANGE_AGE_SECONDS - row["exchange_age"])
            if row["closes_at"]:
                before_recheck = (parse_time(row["closes_at"]) - at).total_seconds() - REACTION_SECONDS
                row["valid_for"] = min(row["valid_for"], before_recheck)
    if detail and book and book.get("depth_kind") == "displayed_ladder":
        levels = (_ladder(book, side) if row["venue"] == POLY else
                  (book.get(f"{side.lower()}_offers") or []))
        row["levels"] = [[float(p), float(q)] for p, q in list(levels)[:12]]
    if not reason:
        # Equivalent refreshed receipts may keep the quote valid. A material price,
        # quantity, fee, model, or identity change requires reviewing a new ticket.
        signature = [row["venue"], row["instrument"], side, budget, bucket["market_id"],
                     bucket["yes_side_id"], bucket["no_side_id"], order, row["probability"],
                     (info or {}).get("model_version"), (info or {}).get("model_vintage"),
                     [listing_proof.get(key) for key in listing_evidence.VERSION_FIELDS] if listing_proof else None]
        row["review_key"] = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()
    if detail and not fixture:
        row["source_trace"] = source_trace(data, row, market, book, newer_book or book, posted_book, at)
    return row


def _posted_price(venue, side, book, listing, market, at, archived):
    if archived and (point := market.get("timeline_price")):
        value = point.get(side.lower())
        return {"price": float(value), "kind": "Timeline sample", "at": point["at"]} if value is not None else None
    if book and book.get("depth_kind") == "displayed_ladder":
        ladder = _ladder(book, side) if venue == POLY else (book.get(f"{side.lower()}_offers") or [])
        level = next(((decimal(p), decimal(q)) for p, q in ladder if 0 < decimal(p) < 1), None)
        if level:
            return {"price": float(level[0]), "quantity": float(level[1]),
                    "kind": "Book offer" if not archived else "Retained book offer", "at": book["at"]}
    stamp = market.get("last_received_at")
    value = listing.get(side)
    if not archived and stamp and parse_time(stamp) <= at and value is not None and 0 < decimal(value) < 1:
        return {"price": float(decimal(value)), "kind": "Listing offer", "at": stamp}
    return None


def _book_trace(book):
    if book is None:
        return None
    return {**{field: book.get(field) for field in (
        "at", "requested_at", "source_at", "available_at", "depth_kind", "reason", "receipt_id",
        "http_status", "source", "cache_age_seconds", "source_time_progress", "receipt_sha256",
        "capture_file", "capture_sha256",
    )}, "cache_status": (book.get("http_headers") or book.get("http_cache") or {}).get("cf-cache-status")}


def _image_state(book, at):
    if not book:
        return "unobserved"
    if not book.get("source_at"):
        return "timestamp_unavailable"
    age = (at - parse_time(book["source_at"])).total_seconds()
    return "stale" if age > EXCHANGE_AGE_SECONDS else "recent"


def source_trace(data, row, market, reviewed, observed, posted, at):
    lane = row["book_lane"]
    venue, instrument = row["venue"], row["instrument"]
    selection = {"state": "unobserved", "reason": "No retained automatic batch witness at this clock"}
    manual = None
    if lane:
        try:
            with venue_archive.connect(data) as db:
                _, scope = FEED_SCOPES[lane]
                batch = venue_archive.latest_book_batch(db, venue, scope, at)
                manual = venue_archive.latest_book_batch(db, venue, venue_archive.MANUAL_BOOK, at, instrument=instrument)
            if batch:
                target = batch["targets"].get(instrument)
                selection = {"batch_id": batch["id"], "at": batch["started_at"], "completed_at": batch["completed_at"],
                             "scope": scope, **(target if target else {
                                 "state": "outside_sample", "reason": "Outside the latest automatic priority sample"})}
            if manual:
                manual = {"batch_id": manual["id"], "scope": manual["scope"],
                          "completed_at": manual["completed_at"], **manual["targets"][instrument]}
        except (sqlite3.Error, OSError, ValueError) as exc:
            selection["reason"] = "Batch telemetry unavailable · " + str(exc)
    first_listed = market.get("first_received_at")
    if selection["state"] == "outside_sample" and (not first_listed or parse_time(first_listed) >
                                                     parse_time(selection["at"])):
        selection.update(state="unobserved", reason="Listing membership at the batch clock was not witnessed")
    recheck = ({"state": "awaiting_observation", "due_at": row["recheck_due_at"], "ends_at": row["recheck_ends_at"]}
               if not row["blocker"] else {"state": "blocked", "reason":
                   "Exchange image timestamp unavailable" if venue == KALSHI and reviewed and
                   not reviewed.get("source_at") and row["order"] else row["blocker"]})
    return {
        "at": row["at"], "venue": venue, "instrument": instrument, "side": row["side"],
        "side_id": row.get("side_id"), "lane": lane, "listing": row.get("listing_evidence"),
        "automatic_selection": selection, "manual_request": manual, "observed_book": _book_trace(observed),
        "displayed_book": _book_trace(posted), "reviewed_book": _book_trace(reviewed),
        "exchange_image_state": _image_state(posted or observed, at),
        "model": {"probability": row["probability"], "gap": row["model_gap"], "availability": row["model_availability"]},
        "fee": row.get("fee_evidence"), "ticket": {"blocker": row["blocker"], "category": row["category"],
                                                   "posted": row["posted"], "order": row["order"]},
        "recheck": recheck,
    }


def _exact_terms(data, key, market, event, bucket, at, *, archived):
    expected = {**market, "venue": key[0], "instrument_id": key[1]}
    if archived and key[0] == POLY:
        # Use the latest slug as a receipt lookup hint only. If retained, the
        # capture's exact identities take precedence over the mutable catalog.
        expected["search_event_id"] = expected.get("event_id") or (event or {}).get("id")
        for field in listing_evidence.IDENTITY:
            expected.pop(field, None)
    if key[0] == POLY and event and bucket:
        # The exact capture's independent IDs can contradict the latest search
        # projection. Never use today's side mapping to relabel that book.
        for field, value in {
            "event_id": event.get("id"), "native_event_id": event.get("native_id"),
            "native_id": bucket.get("market_id"), "yes_side_id": bucket.get("yes_side_id"),
            "no_side_id": bucket.get("no_side_id"),
        }.items():
            if value is not None:
                expected[field] = value
    with venue_archive.connect(data) as db:
        db.execute("BEGIN")
        if key[0] == POLY:
            history = listing_evidence.poly_revisions(db, [expected], at).get(key[1], [])
            listing = listing_evidence.select(history, at)
            fees = venue_fees.poly_terms(listing, at)
        else:
            sources = listing_evidence.kalshi_sources(db, expected, at)
            listing = listing_evidence.kalshi_listing(expected, sources)
            fees = venue_fees.kalshi_terms_at(db, expected, sources, listing, at)
        payout = listing_evidence.payout(db, expected, at) if archived else None
    if archived and (captured_payout := (bucket or {}).get("settlement")):
        clock = captured_payout.get("available_at")
        if (clock and captured_payout.get("source") and captured_payout.get("yes_payout") is not None
                and parse_time(clock) <= at and (payout is None or parse_time(clock) > parse_time(payout["available_at"]))):
            payout = captured_payout
    result = {"raw_market": "{}", "event_id": expected.get("event_id"), **market,
              "listing_evidence": listing, "fee_evidence": fees, "verified_payout": payout}
    if archived:
        # Keep identity and archived weather/book lookup hints. Strip the latest
        # mutable pricing and schedule before introducing selected receipt terms.
        for field in ("opened_at", "closed_at", "listed_event_end_at", "status", "settlement_at",
                      "rules_primary", "rules_secondary", "last_received_at"):
            result[field] = None
        result["raw_market"] = "{}"
    if listing["status"] == "verified":
        result.update({field: listing.get(field) for field in ("event_id", "opened_at", "closed_at", "listed_event_end_at",
                                                               "rules_primary", "rules_secondary")})
        result["status"] = listing["market_status"]
    return result


def market_rows(data, capture, predict, at, *, fixture=False, selected=None, budget=DEFAULT_BUDGET,
                archived=False, retain_origin=True):
    """Pair native listings and books, including Watch cases with no executable evidence."""
    markets = {} if fixture else _catalog(data, at, selected)
    if capture and not fixture and not selected:
        capture = strategy_native_capture.with_listing_evidence(data, capture, at, reuse_bodies=True)
    captured = {}
    for event in (capture or {}).get("events", []):
        for bucket in event["buckets"]:
            key = event.get("venue", POLY), bucket["id"]
            if not selected or key == selected[:2]:
                captured[key] = event, bucket
    rows = []
    witness = None
    probabilities = {}
    for key in captured.keys() | markets.keys():
        market = markets.get(key, {})
        event, bucket = captured.get(key, (None, None))
        if selected and not fixture and (archived or event or market.get("native_id")):
            market = _exact_terms(data, key, market, event, bucket, at, archived=archived)
        elif not fixture and bucket and key[0] == POLY:
            terms = strategy_native_capture.terms_at(bucket, at)
            market = {"raw_market": "{}", **market, "listing_evidence": terms["listing"], "fee_evidence": terms["fees"]}
        base = _contract(event, bucket, market)
        if event and event["id"] not in probabilities:
            probabilities[event["id"]] = predict(event, at) or {}
        info = probabilities.get((event or {}).get("id"), {}).get(key[1])
        books = bucket.get("books", []) if bucket else []
        known = (books if fixture else
                 [b for b in books if not b.get("available_at") or parse_time(b["available_at"]) <= at])
        book = _latest_book(known, at)
        archive_book = json.loads(market["last_book"]) if market.get("last_book") else None
        newer_book = None
        if not book:
            book = archive_book
        else:
            latest = book
            if not fixture and not archived and retain_origin and base["paper_supported"]:
                book = origin_witness(known, at, latest, archive_book)
            for later in (latest, archive_book):
                if later and parse_time(later["at"]) > parse_time((newer_book or book)["at"]):
                    newer_book = later
        if not book and selected and not fixture and key[0] == POLY and base["family"] == "daily-high":
            book = next(iter(capture_index.latest_books(data / "captures", key[1], limit=1, end=at)), None)
        observed = newer_book or book
        display_book = None
        if observed and observed["depth_kind"] == "gap":
            display_book = _latest_book([image for image in known if image["depth_kind"] == "displayed_ladder"], at)
            if archive_book and not display_book:
                with venue_archive.connect(data) as db:
                    prior = db.execute(
                        """SELECT json_set(body, '$.receipt_id', receipt_id) FROM books
                           WHERE venue=? AND instrument_id=? AND at<=? AND at>=?
                           AND json_extract(body,'$.depth_kind')='displayed_ladder' ORDER BY at DESC LIMIT 1""",
                        (*key, venue_archive.receipt_time(at),
                         venue_archive.receipt_time(at - timedelta(minutes=5))),
                    ).fetchone()
                    display_book = json.loads(prior[0]) if prior else None
        if archived:
            price_rows = [p for p in (bucket or {}).get("prices", [])
                          if parse_time(p.get("available_at") or p["at"]) <= at]
            price = max(price_rows, key=lambda p: parse_time(p.get("available_at") or p["at"]), default=None)
            if price:
                market = {**market, "timeline_price": price}
        sides = (selected[2],) if selected else SIDES
        for side in sides:
            rows.append(_side(data, base, event, bucket, market, book, info, side, at, budget, fixture, archived,
                              detail=bool(selected), newer_book=newer_book, display_book=display_book))
        witness = event, bucket, book, info
    return rows, witness


def opportunity_board(rows, at, fixture, budget=DEFAULT_BUDGET):
    ready = sorted((row for row in rows if not row["blocker"]), key=lambda row: (
        -row["order"]["ev_dollars"], row["instrument"], row["side"]))
    watch = sorted((row for row in rows if row["blocker"]), key=lambda row: (
        row["category"] == "archived", not bool(row["depth_kind"] == "displayed_ladder"),
        not row["paper_supported"], row["day"] or "", row["venue"], row["instrument"], row["side"]))
    fields = ("venue", "instrument", "side", "title", "station", "place", "day", "family", "label", "meaning",
              "quote_at", "valid_for", "posted", "market_open", "receipt_age", "book_age", "exchange_age",
              "posted_receipt_age", "posted_exchange_age", "posted_available_at", "posted_image_state", "book_lane",
              "settles_at", "timezone", "blocker", "category", "probability")
    prices = {}
    coverage = {}
    lane_coverage = {}
    for venue in (POLY, KALSHI):
        visible = [row for row in rows if row["venue"] == venue and row["market_open"] and row["posted"]]
        visible.sort(key=lambda row: parse_time(row["posted"]["at"]), reverse=True)
        prices[venue] = visible
        coverage[venue] = {"sides": len(visible), "latest": visible[0]["posted"]["at"] if visible else None,
                           "book_sides": sum(row["posted"]["kind"] == "Book offer" for row in visible)}
        for lane in (name for name, (source, _) in FEED_SCOPES.items() if source == venue):
            feed = [row for row in visible if row["book_lane"] == lane]
            lane_coverage[lane] = {"sides": len(feed), "latest": feed[0]["posted"]["at"] if feed else None}
    # Interleave the venues so the default Prices view includes both feeds.
    ordered = [lane[i] for i in range(max(map(len, prices.values()))) for lane in prices.values() if i < len(lane)]
    return {
        "ready": [{**{key: row[key] for key in fields}, "order": row["order"], "review_key": row["review_key"],
                   "retained_origin": row["retained_origin"]} for row in ready],
        "watch": [{key: row[key] for key in fields} for row in watch],
        "prices": [{**{key: row[key] for key in fields}, "order": row["order"]} for row in ordered],
        "coverage": coverage, "lane_coverage": lane_coverage,
        "at": iso(at), "fixture": fixture, "budget": budget,
        "blockers": dict(Counter(row["category"] for row in watch)),
    }


def funnel_snapshot(rows, *, data=None, at=None, budget=DEFAULT_BUDGET, fixture=False):
    """Count side appearances on the live board before any display grouping."""
    scopes, previews = {}, []
    for venue in (POLY, KALSHI):
        candidates = [row for row in rows if row["venue"] == venue and row["market_open"]]
        posted = [row for row in candidates if row["posted"]]
        ready = [row for row in candidates if not row["blocker"]]
        ladders = [row for row in posted if row["posted"]["kind"] == "Book offer"]
        scopes[venue] = {
            "open_sides": len(candidates), "posted_sides": len(posted), "ladder_sides": len(ladders),
            "ready_sides": len(ready), "ladder_ready_sides": sum(not row["blocker"] for row in ladders),
            "open_instruments": len({row["instrument"] for row in candidates}),
            "listed_window_ended_sides": sum(row["venue"] == venue and row["blocker"] == "Listed event window ended"
                                            for row in rows),
            "ready_blockers": dict(Counter(row["blocker"] for row in candidates if row["blocker"])),
            "ladder_blockers": dict(Counter(row["blocker"] for row in ladders if row["blocker"])),
        }
        if ready:
            chosen = min(ready, key=lambda row: (-row["order"]["ev_dollars"], row["instrument"], row["side"]))
            stage = "Ready"
        elif ladders:
            chosen = max(ladders, key=lambda row: (parse_time(row["posted"]["at"]), row["instrument"], row["side"]))
            stage = "Posted ladder"
        else:
            continue
        fields = ("venue", "instrument", "side", "at", "quote_at", "quote_source_at", "blocker", "review_key")
        previews.append({"stage": stage, **{key: chosen.get(key) for key in fields}})
    instruments = {}
    if data is not None:
        with venue_archive.connect(data) as db:
            for lane, (venue, scope) in FEED_SCOPES.items():
                candidates = {row["instrument"]: row.get("first_listed_at") for row in rows
                              if row["market_open"] and row["book_lane"] == lane}
                batch = venue_archive.latest_book_batch(db, venue, scope, at)
                counts = Counter({key: 0 for key in ("selected", "requested", "usable", "failed", "unrequested",
                                                     "empty_depth", "missing_or_invalid", "outside_sample", "unobserved")})
                for instrument, first_listed in candidates.items():
                    target = batch["targets"].get(instrument) if batch else None
                    if target:
                        counts["selected"] += 1
                        counts["requested"] += bool(target["requested_at"])
                        counts[target["state"]] += 1
                    elif batch and first_listed and parse_time(first_listed) <= parse_time(batch["started_at"]):
                        counts["outside_sample"] += 1
                    else:
                        counts["unobserved"] += 1
                instruments[lane] = {"venue": venue, "scope": scope, "open_instruments": len(candidates),
                                     "batch_id": batch["id"] if batch else None,
                                     "batch_clock": batch["started_at"] if batch else None, **counts}
    return {"readiness_basis": "fixture-legacy" if fixture else READINESS_BASIS,
            "candidate_scope": "retained-event-window", "at": iso(at) if at else None, "budget": budget,
            "denominators": {"open": "exact venue/instrument/side at this evaluation",
                             "posted": "open sides", "ladder": "posted sides", "ready": "open sides",
                             "preview_samples": "highest EV Ready or latest displayed ladder per venue",
                             "book_coverage": "distinct open instruments within each lane at this evaluation"},
            "venues": scopes, "preview_samples": previews, "instrument_coverage": instruments}
