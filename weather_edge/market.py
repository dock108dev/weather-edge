"""Decode Polymarket US daily-high markets and price paper orders."""

from bisect import bisect_left
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, ROUND_HALF_EVEN
import re

from .poly_fee_policy import version as fee_version

GATEWAY = "https://gateway.polymarket.us"
DISCOVERY = GATEWAY + "/v2/tags/weather/events?limit=50&active=true&closed=false"
OPEN = {"OPEN", "MARKET_STATE_OPEN", "MARKET_STATUS_OPEN"}
ONE = Decimal("1")
CENT = Decimal("0.01")
HALF_CENT = Decimal("0.005")
MAX_QUOTE_AGE = 30
MARKET_SLUG = re.compile(r"tc-temp-[a-z]+high-\d{4}-\d{2}-\d{2}-[a-z0-9-]+")
EVENT_SLUG = re.compile(r"temp-[a-z]+high-(\d{4}-\d{2}-\d{2})")
SETTLEMENT_RULE = re.compile(
    r"highest temperature recorded at .*? \(([A-Z]{4})\) .*? for (\d{4}-\d{2}-\d{2}) "
    r"as reported by the National Weather Service's Climatological Report \(Daily\) be (.+?)\?",
    re.IGNORECASE,
)
STATIONS = {
    "KNYC": (40.779, -73.969, "America/New_York", -5),
    "KPHL": (39.873, -75.227, "America/New_York", -5),
    "KMIA": (25.788, -80.317, "America/New_York", -5),
    "KMDW": (41.786, -87.752, "America/Chicago", -6),
    "KSFO": (37.619, -122.375, "America/Los_Angeles", -8),
    "KLAX": (33.938, -118.389, "America/Los_Angeles", -8),
}


def parse_time(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("Expected a time with time zone")
    return value.astimezone(timezone.utc)


def iso(value):
    return parse_time(value).isoformat().replace("+00:00", "Z")


def decimal(value):
    if isinstance(value, bool):
        raise ValueError("Expected a decimal amount")
    try:
        result = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError("Expected a decimal amount") from error
    if not result.is_finite():
        raise ValueError("Expected a finite decimal amount")
    return result


def _identifier(value):
    if isinstance(value, bool) or not isinstance(value, (int, str)) or not str(value).strip():
        raise ValueError("Missing native market ID")
    return str(value)


def _bucket_bounds(condition):
    text = condition.lower()
    match = re.fullmatch(r"between (-?\d+)f and (-?\d+)f", text)
    if match:
        low, high = map(int, match.groups())
        if low <= high:
            return low, high
    match = re.fullmatch(r"(less|greater) than or equal to (-?\d+)f", text)
    if match:
        value = int(match[2])
        return (None, value) if match[1] == "less" else (value, None)
    match = re.fullmatch(r"(?:equal to|exactly) (-?\d+)f", text)
    if match:
        return int(match[1]), int(match[1])
    raise ValueError(f"Unsupported daily-high condition: {condition}")


def parse_event(event):
    """Authorize a daily-high bucket from its rules, never from the slug suffix."""
    slug = event.get("slug", "")
    day_match = EVENT_SLUG.fullmatch(slug)
    if not day_match:
        raise ValueError("Unsupported event series")
    day = date.fromisoformat(day_match[1])
    buckets = []
    stations = set()
    end_times = set()
    for market in event.get("markets", []):
        market_slug = market.get("slug", "")
        if not MARKET_SLUG.fullmatch(market_slug) or not market_slug.startswith(f"tc-{slug}-"):
            raise ValueError("Market slug does not match the event")
        rule = market.get("description", "")
        match = SETTLEMENT_RULE.search(rule)
        if not match or match[2] != day.isoformat():
            raise ValueError("Unverified NWS date or settlement rule")
        station = match[1].upper()
        low, high = _bucket_bounds(match[3])
        stations.add(station)
        market_id = _identifier(market.get("id"))
        sides = {}
        for side in market.get("marketSides", []):
            long = side.get("long")
            label = side.get("description", "").upper()
            if type(long) is not bool or label != ("YES" if long else "NO") or long in sides:
                raise ValueError("Unverified YES/NO mapping")
            if _identifier(side.get("marketId")) != market_id:
                raise ValueError("Side belongs to a different market")
            sides[long] = _identifier(side.get("id"))
        if set(sides) != {True, False} or sides[True] == sides[False]:
            raise ValueError("Incomplete YES/NO mapping")
        end_time = iso(market["endDate"])
        end_times.add(end_time)
        quantity_step = decimal(market["minimumTradeQty"])
        if quantity_step not in {CENT, ONE}:
            raise ValueError("Unknown market quantity step")
        buckets.append({
            "id": market_slug,
            "market_id": market_id,
            "label": market.get("title") or market_slug,
            "lower_f": low,
            "upper_f": high,
            "rules": rule,
            "yes_side_id": sides[True],
            "no_side_id": sides[False],
            "quantity_step": str(quantity_step),
            "exchange_status": market.get("status"),
            "advertised_fee_coefficient": market.get("feeCoefficient"),
            "books": [],
        })
    if not buckets or len(stations) != 1 or len(end_times) != 1:
        raise ValueError("Inconsistent daily-high market metadata")
    station = stations.pop()
    if station not in STATIONS:
        raise ValueError(f"Unsupported settlement station: {station}")
    buckets.sort(key=lambda bucket: float("-inf") if bucket["lower_f"] is None else bucket["lower_f"])
    if len({b["id"] for b in buckets}) != len(buckets) or len({b["market_id"] for b in buckets}) != len(buckets):
        raise ValueError("Duplicate bucket identity")
    # The certified rules assign an overlapping boundary to the higher bracket.
    for current, following in zip(buckets, buckets[1:]):
        a, b = current["upper_f"], following["lower_f"]
        if a is None or b is None or a > b:
            raise ValueError("Ambiguous overlapping temperature ranges")
        if a == b:
            current["upper_f"] -= 1
            if current["lower_f"] is not None and current["lower_f"] > current["upper_f"]:
                raise ValueError("Bucket has no payable climate values")
    lat, lon, local_zone, offset = STATIONS[station]
    climate_start = datetime.combine(day, time.min, tzinfo=timezone(timedelta(hours=offset)))
    return {
        "id": slug,
        "native_id": _identifier(event.get("id")),
        "title": event.get("title") or slug,
        "station": station,
        "lat": lat,
        "lon": lon,
        "timezone": local_zone,
        "standard_utc_offset_hours": offset,
        "date": day.isoformat(),
        "starts_at": iso(event["startDate"]),
        "ends_at": iso(event["endDate"]),
        "climate_starts_at": iso(climate_start),
        "climate_ends_at": iso(climate_start + timedelta(days=1)),
        "settles_at": end_times.pop(),
        "settlement_time_basis": "Contract-specific exchange market end metadata",
        "buckets": buckets,
        "observations": [],
        "forecasts": [],
        "source": GATEWAY + "/v1/events/slug/" + slug,
    }


def parse_book(payload, slug, received_at, *, cache_age=None):
    """Interpret the public REST ladder, whose bids and offers are YES prices."""
    data = payload.get("marketData", {})
    if data.get("marketSlug") != slug:
        raise ValueError("Book identity does not match requested slug")
    receipt = parse_time(received_at)
    source = parse_time(data["transactTime"]) if data.get("transactTime") else None
    if source and source > receipt + timedelta(seconds=2):
        raise ValueError("Book exchange time is after receipt time")
    result = {
        "at": iso(receipt),
        "source_at": data.get("transactTime"),
        "state": data.get("state"),
        "cache_age_seconds": cache_age,
        "source": GATEWAY + f"/v1/markets/{slug}/book",
        "depth_kind": "displayed_ladder",
    }
    for side in ("bids", "offers"):
        rows = data.get(side)
        if rows is None:
            # Preserve a missing array as unknown, distinct from an explicit empty book.
            result[side] = None
            continue
        if not isinstance(rows, list):
            raise ValueError("Malformed book ladder")
        levels = {}
        for row in rows:
            if row["px"].get("currency") != "USD":
                raise ValueError("Expected book prices in USD")
            price = decimal(row["px"]["value"])
            qty = decimal(row["qty"])
            if not 0 <= price <= 1 or qty < 0 or price in levels:
                raise ValueError("Invalid or duplicate price level")
            levels[price] = qty
        result[side] = [
            [str(price), str(levels[price])]
            for price in sorted(levels, reverse=side == "bids")
            if levels[price] > 0
        ]
    return result


def _normalized_ladders(book):
    return tuple(None if book.get(side) is None else tuple((decimal(p), decimal(q)) for p, q in book[side])
                 for side in ("bids", "offers"))


def source_time_key(source_at):
    """Sort a native clock by UTC second and its full recorded fraction."""
    clock = parse_time(source_at)
    fractional = re.search(r"\.(\d+)", source_at)
    return clock.replace(microsecond=0), fractional[1].rstrip("0") if fractional else ""


def mark_book_progress(images):
    """Reject retrograde and conflicting snapshots without deleting raw depth."""
    last_clock = None
    last_book = None
    last_prices = None
    for book in images:
        source_at = book.get("source_at")
        if not source_at:
            book["source_time_progress"] = "unknown"
            continue
        key = source_time_key(source_at)
        if last_clock is None or key > last_clock:
            book["source_time_progress"] = "first" if last_clock is None else "advanced"
            last_clock, last_book, last_prices = key, book, None
        elif key < last_clock:
            book["source_time_progress"] = "regressed"
        else:
            same = book.get("bids") == last_book.get("bids") and book.get("offers") == last_book.get("offers")
            if not same:
                if last_prices is None:
                    last_prices = _normalized_ladders(last_book)
                same = _normalized_ladders(book) == last_prices
            book["source_time_progress"] = "unchanged" if same else "conflicting"


def fee_coefficient(at):
    return fee_version(parse_time(at))[1]


def _round_fee(amount):
    return amount.quantize(CENT, rounding=ROUND_HALF_EVEN)


def taker_fee(fills, coefficient):
    """Conservative ceiling since aggregate depth hides distinct resting orders."""
    theta = decimal(coefficient)
    return _round_fee(sum((theta * decimal(q) * decimal(p) * (ONE - decimal(p))
                           for p, q in fills), Decimal("0")))


def _walk(levels, probability, budget, step, theta, max_quantity=None):
    # Each flat fee interval admits a largest affordable lot. Compare those
    # endpoints to find the best prefix, including any partial level.
    spent = exact_fee = quantity = Decimal("0")
    fills = []
    best = None
    deepest = None
    for price, available in levels:
        cap = available if max_quantity is None else min(available, max_quantity - quantity)
        max_units = int(min(cap / step, (budget - spent) / (price * step)))
        if max_units <= 0:
            break
        unit_price = price * step
        unit_fee = theta * unit_price * (ONE - price)

        start_cent = int(_round_fee(exact_fee) / CENT)
        end_cent = int(_round_fee(exact_fee + max_units * unit_fee) / CENT)
        for fee_cents in range(start_cent, end_cent + 1):
            fee_limit = fee_cents * CENT
            affordable = int(((budget - spent - fee_limit) / unit_price).to_integral_value(rounding=ROUND_FLOOR))
            if affordable <= 0:
                break
            before_fee_change = int(((fee_limit + HALF_CENT - exact_fee) / unit_fee).to_integral_value(rounding=ROUND_FLOOR))
            units = min(max_units, affordable, before_fee_change)
            if units > 0 and _round_fee(exact_fee + units * unit_fee) > fee_limit:
                units -= 1
            if units <= 0:
                continue
            new_quantity = units * step
            combined_quantity = quantity + new_quantity
            order_fee = _round_fee(exact_fee + units * unit_fee)
            cost = spent + units * unit_price + order_fee
            candidate = {
                "quantity": combined_quantity,
                "notional": cost - order_fee,
                "fee": order_fee,
                "max_loss": cost,
                "break_even_probability": cost / combined_quantity,
                "ev_dollars": probability * combined_quantity - cost,
                "fills": [*fills, (price, new_quantity)],
            }
            if best is None or candidate["ev_dollars"] > best["ev_dollars"]:
                best = candidate
            if deepest is None or combined_quantity > deepest["quantity"]:
                deepest = candidate

        if deepest is None or deepest["quantity"] <= quantity:
            break
        actual = deepest["quantity"] - quantity
        fills.append((price, actual))
        quantity += actual
        spent += actual * price
        exact_fee += theta * actual * price * (ONE - price)
        if actual < available:
            break
    return best if best and best["ev_dollars"] > 0 else deepest


def _ladder(book, side):
    name = "offers" if side == "YES" else "bids"
    rows = book.get(name)
    if rows is None:
        return []
    levels = [(decimal(p) if side == "YES" else ONE - decimal(p), decimal(q)) for p, q in rows]
    return sorted((p, q) for p, q in levels if 0 < p < 1 and q > 0)


def _depth_usable(book, as_of, max_age):
    if not book or book.get("depth_kind") != "displayed_ladder" or book.get("state") not in OPEN:
        return False
    if not book.get("source_at") or book.get("source_time_progress") in {"regressed", "conflicting"}:
        return False
    age = (as_of - parse_time(book["at"])).total_seconds() + (book.get("cache_age_seconds") or 0)
    if not 0 <= age <= max_age:
        return False
    if book.get("bids") and book.get("offers"):
        best_bid = max(decimal(p) for p, _ in book["bids"])
        best_offer = min(decimal(p) for p, _ in book["offers"])
        if best_bid >= best_offer:
            return False
    return True


def _depth_status(book, side, at, max_age):
    if not book or book.get("depth_kind") != "displayed_ladder":
        return "Missing book depth"
    age = (at - parse_time(book["at"])).total_seconds() + (book.get("cache_age_seconds") or 0)
    if age > max_age:
        return "Old quote"
    if not _depth_usable(book, at, max_age):
        return "Unverified or unavailable depth"
    if book.get("offers" if side == "YES" else "bids") is None:
        return "Missing side depth"
    return None


def _latest_book(books, at):
    return max((book for book in books if parse_time(book["at"]) <= at),
               key=lambda book: parse_time(book["at"]), default=None)


def _serialize(fill):
    return {
        key: float(value) if isinstance(value, Decimal)
        else [[float(p), float(q)] for p, q in value] if key == "fills" else value
        for key, value in fill.items()
    }


def _price(event, bucket, side, book, info, at, budget, max_age, max_quantity=None, *,
           fee_at=fee_coefficient, rules=None, limit_price=None, fee_clock=None, evidence_checked=False,
           evidence_gap=None):
    raw_probability = info.get("probability") if isinstance(info, dict) else info
    probability = decimal(raw_probability) if raw_probability is not None else None
    if probability is not None and not 0 <= probability <= 1:
        raise ValueError("Expected a probability in [0, 1]")
    if probability is not None and side == "NO":
        probability = ONE - probability
    if rules is not None and probability is not None:
        probability = rules.side_probability(info, side, probability)
    uncertain = bool(info.get("uncertain", False)) if isinstance(info, dict) else probability is None
    levels = _ladder(book, side) if book else []
    if limit_price is not None:
        levels = [(price, size) for price, size in levels if price <= limit_price]
    depth_gap = evidence_gap
    if not evidence_checked:
        depth_gap = _depth_status(book, side, at, max_age) if rules is None else rules.depth_status(book, side, at)
    if not evidence_checked and at >= parse_time(event["ends_at"]):
        status = "Market closed"
    elif depth_gap:
        status = depth_gap
    elif not levels:
        status = "No displayed liquidity on this side"
    elif probability is None:
        status = "Model probability unavailable"
    else:
        status = "Uncertain model probability" if uncertain else "Priced"
    row = {
        "event_id": event["id"],
        "station": event["station"],
        "date": event["date"],
        "settles_at": event["settles_at"],
        "bucket_id": bucket["id"],
        "label": bucket["label"],
        "side": side,
        "probability": float(probability) if probability is not None else None,
        "uncertain": uncertain,
        "quote_at": book["at"] if book and book.get("depth_kind") == "displayed_ladder" else None,
        "quote_source_at": book.get("source_at") if book else None,
        "quote_age_seconds": (at - parse_time(book["at"])).total_seconds() if book and book.get("depth_kind") == "displayed_ladder" else None,
        "available_size": float(sum(quantity for _, quantity in levels)) if not depth_gap else None,
        "entry_price": None,
        "model_version": info.get("model_version") if isinstance(info, dict) else None,
        "model_method": info.get("model_method") if isinstance(info, dict) else None,
        "forecast_issued_at": info.get("forecast_issued_at") if isinstance(info, dict) else None,
        "forecast_available_at": info.get("forecast_available_at") if isinstance(info, dict) else None,
        "status": status,
        "ev_dollars": None,
        "max_loss": None,
        "break_even_probability": None,
    }
    if isinstance(info, dict) and info.get("model_vintage"):
        row["model_vintage"] = info["model_vintage"]
    if isinstance(info, dict) and info.get("model_availability"):
        row["model_availability"] = info["model_availability"]
    if isinstance(info, dict) and info.get("model_evidence_cutoff"):
        row["model_evidence_cutoff"] = info["model_evidence_cutoff"]
    if rules is not None:
        row["venue"] = event.get("venue", "polymarket-us")
        if event.get("family"):
            row["market_family"] = event["family"]
        row["http_cache_age"] = book.get("cache_age_seconds") if book else None
        row["http_cache_status"] = (book or {}).get("http_cache", {}).get("cf-cache-status")
        if isinstance(info, dict) and info.get("strategy_context"):
            row["strategy_context"] = info["strategy_context"]
    if status in {"Priced", "Uncertain model probability", "Model probability unavailable"}:
        step = decimal(bucket["quantity_step"])
        theta = fee_at(fee_clock or at)
        fill = _walk(levels, probability or Decimal("0"), budget, step, theta, max_quantity)
        if fill:
            row.update(_serialize(fill))
            row["entry_price"] = float(fill["notional"] / fill["quantity"])
            row["coefficient"] = float(theta)
            if probability is None:
                row["ev_dollars"] = None
            elif status == "Priced" and fill["ev_dollars"] <= 0:
                row["status"] = "Verified depth · no positive EV"
        else:
            row["status"] = "Budget too small for available size"
    return row


def scan(capture, as_of, probabilities, *, order_size=25, max_quote_age=MAX_QUOTE_AGE, freshness=None, clock_basis=None):
    """Return a row for every side, retaining both uncertainty and evidence gaps."""
    if freshness is not None:
        from .strategy_rules import book_status, validate_freshness

        validate_freshness(freshness)
    at = parse_time(as_of)
    budget = decimal(order_size)
    if budget <= 0 or max_quote_age <= 0:
        raise ValueError("Positive order size and quote age are required")
    rows = []
    for event in capture.get("events", []):
        for bucket in event.get("buckets", []):
            if not bucket.get("quantity_step"):
                continue
            book = _latest_book(bucket.get("books", []), at)
            if clock_basis is not None:
                from . import paper_eligibility

                books = [b for b in bucket.get("books", [])
                         if not (clock := paper_eligibility.book_clock(b, clock_basis)) or clock <= at]
                book = _latest_book(books, at)
                if freshness == "origin-confirmed":
                    book = paper_eligibility.origin_witness(books, at, book)
            display = max((point for point in bucket.get("prices", [])
                           if parse_time(point.get("available_at") or point["at"]) <= at),
                          key=lambda point: parse_time(point.get("available_at") or point["at"]), default=None)
            for side in ("YES", "NO"):
                checked = None
                if clock_basis is not None:
                    fixture = capture["kind"] == "fixture"
                    checked = paper_eligibility.qualify(
                        event, bucket, side, book, at, fee_at=fee_coefficient, fee_clock=at, freshness=freshness or "legacy",
                        clock_basis=clock_basis, fixture=fixture,
                        **({"max_exchange_age": None} if fixture else {}),
                    )
                row = _price(event, bucket, side, book, probabilities.get(bucket["id"]), at, budget, max_quote_age,
                             evidence_checked=checked is not None, evidence_gap=checked.reason if checked else None)
                if checked and checked.reason:
                    # Keep the displayed offer independently of executable sizing.
                    levels = _ladder(book, side) if book else []
                    row.update(paper_evidence_gap=checked.reason,
                               display_price=float(levels[0][0]) if levels else None)
                if display and display.get(side.lower()) is not None:
                    row["display_price"] = display[side.lower()]
                    row["display_quote_at"] = display["at"]
                if row["quote_age_seconds"] is not None:
                    age = book.get("cache_age_seconds")
                    if freshness is None:
                        row["response_cache_age"] = age or 0
                        row["effective_quote_age"] = row["quote_age_seconds"] + row["response_cache_age"]
                    else:
                        row["response_cache_age"] = age
                        row["response_cache_status"] = book.get("http_cache", {}).get("cf-cache-status")
                        row["effective_quote_age"] = None if age is None else row["quote_age_seconds"] + age
                        if checked is None:
                            row["paper_evidence_gap"] = book_status(book, side, parse_time(book["at"]), freshness)
                        row["paper_freshness"] = freshness
                rows.append(row)
    return rows


def paper_trial(capture, predict, *, reaction_seconds=5, order_size=25, bankroll=500,
                max_station_day=25, max_recheck_lag=15, as_of=None, fee_at=fee_coefficient,
                rules=None, freshness=None, selection=None, audit_rejections=False, clock_basis=None):
    """Replay saved ladders, then commit cash in chronological receipt order."""
    if freshness is not None and clock_basis is None:
        from .strategy_rules import StrategyRule, validate_freshness

        if rules is not None:
            raise ValueError("Pass either a complete strategy rule or a freshness policy")
        validate_freshness(freshness)
        rules = StrategyRule("two-sided", freshness, 0.0)
    budget, cash, station_limit = map(decimal, (order_size, bankroll, max_station_day))
    if min(budget, cash, station_limit) <= 0 or reaction_seconds <= 0 or max_recheck_lag < 0:
        raise ValueError("Positive budget, bankroll, exposure cap and reaction delay required")
    as_of = parse_time(as_of or capture["captured_at"])
    started_with = cash
    signals = []
    candidates = []
    retry = timedelta(seconds=reaction_seconds)
    lag = timedelta(seconds=max_recheck_lag)
    if clock_basis is not None:
        from . import paper_replay

        through = min(as_of, parse_time(capture["captured_at"])) if capture.get("captured_at") else as_of
        signals, candidates, coverage, rejected, phase_counts = paper_replay.candidates(
            capture, predict, through=through, retry=retry, lag=lag, budget=budget, rules=rules,
            fee_at=fee_at, selection=selection, clock_basis=clock_basis, freshness=freshness,
        )
    predictions = {}
    book_receipts = []
    book_station_days = set()
    usable_book_count = 0
    modeled_book_count = 0
    rejected_books = {}
    recheck_status = getattr(rules, "recheck_status", None)
    # A manual order starts its reaction clock at submission. The signal
    # retains the actual receipt time of the reviewed book in the audit.
    selected_at = parse_time(selection["at"]) if selection else None
    price_limit = decimal(selection["limit_price"]) if selection else None
    quantity_limit = decimal(selection["quantity"]) if selection else None
    exchange_age = selection.get("max_exchange_age") if selection else None
    if exchange_age is not None:
        from .strategy_rules import exchange_clock_status

    def qualifies(row, *, signal=False):
        if row["uncertain"] or (row["ev_dollars"] or 0) <= 0:
            return False
        return rules is None or rules.qualifies(row, signal=signal)

    for event in capture.get("events", []) if clock_basis is None else []:
        expiry = parse_time(event["ends_at"])
        for bucket in event.get("buckets", []):
            if not bucket.get("quantity_step") or (selection and bucket["id"] != selection["instrument"]):
                continue
            books = sorted((b for b in bucket.get("books", []) if parse_time(b["at"]) <= as_of),
                           key=lambda b: parse_time(b["at"]))
            timestamps = [parse_time(book["at"]) for book in books]
            for i, (at, book) in enumerate(zip(timestamps, books)):
                book_receipts.append(at)
                book_station_days.add((event["station"], event["date"]))
                if at >= expiry or (selection and book["at"] != selection["book_at"]):
                    if audit_rejections and at >= expiry:
                        rejected_books["Market closed"] = rejected_books.get("Market closed", 0) + 1
                    continue
                gap = (rules.depth_status(book, None, at) if rules is not None else
                       None if _depth_usable(book, at, 0) else "Unverified or unavailable depth")
                if gap is not None:
                    if audit_rejections:
                        rejected_books[gap] = rejected_books.get(gap, 0) + 1
                    continue
                usable_book_count += 1
                decision_at = selected_at if selection else at
                if not at <= decision_at <= as_of or decision_at >= expiry:
                    continue
                if exchange_age is not None and exchange_clock_status(book, decision_at, exchange_age):
                    continue
                key = (event["id"], decision_at)
                if key not in predictions:
                    predictions[key] = predict(event, decision_at) or {}
                initial = predictions[key].get(bucket["id"])
                if initial is not None:
                    modeled_book_count += 1
                for side in ("YES", "NO"):
                    if selection and side != selection["side"]:
                        continue
                    if rules is not None and not rules.considers(initial, side):
                        continue
                    if rules is not None:
                        levels = _ladder(book, side)
                        side_probability = decimal(initial["probability"])
                        if side == "NO":
                            side_probability = ONE - side_probability
                        side_probability = rules.side_probability(initial, side, side_probability)
                        if not levels or side_probability <= levels[0][0]:
                            continue
                    quote = _price(event, bucket, side, book, initial, at, budget, 0, quantity_limit,
                                   fee_at=fee_at, rules=rules, limit_price=price_limit, fee_clock=decision_at)
                    if not qualifies(quote, signal=True):
                        continue
                    target, limit = decision_at + retry, decision_at + retry + lag
                    missing = "Missing eligible recheck" if as_of >= min(limit, expiry) else "Awaiting eligible recheck"
                    attempt = {**quote, "signal_at": iso(decision_at), "reaction_due_at": iso(target),
                               "reaction_at": None, "reaction_quote": None,
                               "reaction_ev_dollars": None, "result": missing}
                    signals.append(attempt)
                    future_idx = bisect_left(timestamps, target, lo=i + 1)
                    match = None
                    gaps = {}
                    for index in range(future_idx, len(timestamps)):
                        checked_at = timestamps[index]
                        if checked_at > limit or checked_at >= expiry:
                            break
                        recheck = books[index]
                        gap = (_depth_status(recheck, side, checked_at, 0) if rules is None
                               else rules.depth_status(recheck, side, checked_at))
                        if gap is None and recheck_status is not None:
                            gap = recheck_status(book, recheck)
                        if gap is None and exchange_age is not None:
                            gap = exchange_clock_status(recheck, checked_at, exchange_age)
                        if gap is None:
                            match = (checked_at, recheck)
                            break
                        if rules is not None:
                            gaps[gap] = gaps.get(gap, 0) + 1
                    if rules is not None:
                        attempt["recheck_gaps"] = gaps
                    if match is None:
                        continue
                    checked_at, recheck = match
                    future_key = (event["id"], checked_at)
                    if future_key not in predictions:
                        predictions[future_key] = predict(event, checked_at) or {}
                    current = predictions[future_key].get(bucket["id"])
                    next_quote = _price(event, bucket, side, recheck, current, checked_at, budget, 0,
                                        decimal(quote["quantity"]), fee_at=fee_at, rules=rules,
                                        limit_price=price_limit)
                    attempt["reaction_at"] = iso(checked_at)
                    attempt["reaction_quote"] = next_quote
                    attempt["reaction_ev_dollars"] = next_quote["ev_dollars"]
                    if next_quote.get("quantity"):
                        attempt["result"] = ("Recheck probability unavailable or uncertain"
                                             if next_quote["uncertain"] else "Edge did not persist")
                    else:
                        recheck_levels = _ladder(recheck, side) if selection else []
                        attempt["result"] = ("Price moved above the reviewed limit"
                                             if recheck_levels and recheck_levels[0][0] > price_limit
                                             else "Insufficient size at recheck")
                    if not qualifies(next_quote):
                        continue
                    attempt["result"] = "Edge persisted"
                    candidates.append((checked_at, -quote["ev_dollars"], decision_at, attempt,
                                       event, bucket, side, recheck, current))

    positions = []
    accounting = []
    seen = set()
    realized = Decimal("0")

    def settle_available(when):
        nonlocal cash, realized
        for position in positions:
            if position["settled"] or not position["settlement_available_at"]:
                continue
            if parse_time(position["settlement_available_at"]) <= when:
                amount = position["quantity"] * position["side_payout"]
                cash += amount
                realized += amount - position["max_loss"]
                position["settled"] = True
                position["payout"] = amount
                accounting.append((parse_time(position["settlement_available_at"]), "Settlement",
                                   amount, -position["max_loss"], position["bucket_id"]))

    for checked_at, _, signal_at, attempt, event, bucket, side, book, probability in sorted(
        candidates, key=lambda item: (item[0], item[1], item[2], item[5]["id"], item[6])
    ):
        settle_available(checked_at)
        group = (event["station"], event["date"])
        used = sum((p["max_loss"] for p in positions if not p["settled"] and p["group"] == group), Decimal("0"))
        available = min(budget, cash, station_limit - used)
        side_key = ((bucket["id"], side) if rules is None and clock_basis is None
                    else (event.get("venue", "polymarket-us"), bucket["id"], side))
        if side_key in seen:
            attempt["result"] = "Repeated signal for an existing position"
            continue
        if available <= 0:
            attempt["result"] = "Insufficient available cash" if cash <= 0 else "Station-day exposure limit reached"
            continue
        row = _price(event, bucket, side, book, probability, checked_at, available, 0,
                     decimal(attempt["quantity"]), fee_at=fee_at, rules=rules, limit_price=price_limit,
                     fee_clock=signal_at if clock_basis is not None else None, evidence_checked=clock_basis is not None)
        if (row["ev_dollars"] or 0) <= 0 or (rules is not None and not qualifies(row)):
            attempt["result"] = ("Insufficient available cash" if not row.get("quantity") and cash < budget
                                 else "No edge at available allocation")
            continue
        cash -= decimal(row["max_loss"])
        seen.add(side_key)
        settlement = bucket.get("settlement")
        side_payout = decimal(settlement["yes_payout"]) if settlement else None
        if side_payout is not None and side == "NO":
            side_payout = ONE - side_payout
        positions.append({
            **row,
            "max_loss": decimal(row["max_loss"]),
            "quantity": decimal(row["quantity"]),
            "group": group,
            "opened_at": iso(checked_at),
            "signal_at": iso(signal_at),
            "signal_ev_dollars": attempt["ev_dollars"],
            "settlement_available_at": settlement["available_at"] if settlement else None,
            "side_payout": side_payout,
            "settled": False,
            "payout": None,
        })
        accounting.append((checked_at, "Paper fill", -decimal(row["max_loss"]), decimal(row["max_loss"]),
                           row["bucket_id"]))
        attempt["result"] = "Paper filled"

    settle_available(as_of)
    station_days = {}
    for position in positions:
        station, day = position["group"]
        group = station_days.setdefault((station, day), {
            "station": station, "date": day, "fills": 0, "settled": 0,
            "cash_committed": Decimal("0"), "realized_pnl": Decimal("0"),
        })
        group["fills"] += 1
        if position["settled"]:
            group["settled"] += 1
            group["realized_pnl"] += position["payout"] - position["max_loss"]
        else:
            group["cash_committed"] += position["max_loss"]
    groups = [group for _, group in sorted(station_days.items())]
    settled_station_days = [group for group in groups if group["fills"] == group["settled"]]
    exposure = {f"{group['station']} / {group['date']}": group["cash_committed"]
                for group in groups if group["cash_committed"]}
    locked = sum(exposure.values(), Decimal("0"))
    if cash + locked != started_with + realized:
        raise ArithmeticError("Paper cash and open cost do not reconcile")
    cash_now, locked_now = started_with, Decimal("0")
    cash_over_time = []
    for time, kind, cash_delta, locked_delta, bucket in sorted(accounting, key=lambda item: item[0]):
        cash_now += cash_delta
        locked_now += locked_delta
        cash_over_time.append({"at": iso(time), "kind": kind, "bucket_id": bucket,
                               "cash": float(cash_now), "cash_committed": float(locked_now)})
    return {
        "start_bankroll": float(started_with),
        "cash": float(cash),
        "cash_committed": float(locked),
        "realized_pnl": float(realized),
        "signals": len(signals),
        "distinct_signal_sides": len({(item["bucket_id"], item["side"]) for item in signals}),
        "rechecked": sum(attempt["reaction_at"] is not None for attempt in signals),
        "persisted": len(candidates),
        "fills": len(positions),
        "settled": sum(position["settled"] for position in positions),
        "reaction_seconds": reaction_seconds,
        "order_size": float(budget),
        "max_station_day": float(station_limit),
        "exposure": {name: float(amount) for name, amount in exposure.items()},
        "station_days": [{**group, "cash_committed": float(group["cash_committed"]),
                          "realized_pnl": float(group["realized_pnl"])} for group in groups],
        "settled_station_days": len(settled_station_days),
        "profitable_station_days": sum(group["realized_pnl"] > 0 for group in settled_station_days),
        "coverage": coverage if clock_basis is not None else {
                     "from": iso(min(book_receipts)) if book_receipts else None,
                     "through": iso(max(book_receipts)) if book_receipts else None,
                     "analyzed_at": iso(as_of), "book_receipts": len(book_receipts),
                     "eligible_books": usable_book_count, "modeled_books": modeled_book_count,
                     "station_days": len(book_station_days),
                     **({"rejected_books": rejected_books} if audit_rejections else {})},
        **({"signal_rejections": rejected, "eligibility_rejections": phase_counts} if clock_basis is not None else {}),
        "cash_over_time": cash_over_time,
        "attempts": signals,
        "positions": [_serialize({**p, "group": list(p["group"])}) for p in positions],
    }
