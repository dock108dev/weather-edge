"""Join a named venue weather contract to compatible local weather replay."""
from __future__ import annotations

from datetime import date, timedelta
import json
from statistics import median

from . import gefs_precipitation, hrrr_precipitation, study, venue_archive
from .market import decimal, iso, parse_time
from .venue_contracts import temperature_window

TEMPERATURE = {"daily-high", "daily-low"}
FAMILIES = TEMPERATURE | {"daily-rain"}
PRICE_HISTORY = {
    venue_archive.POLY: ("display-price-1m", "YES display ask"),
    venue_archive.KALSHI: ("candle-60m", "YES midpoint at hourly candle close"),
}


def _price(value):
    if value is None:
        return None
    price = decimal(value)
    return float(price) if 0 <= price <= 1 else None


def _prices(db, market, first, last):
    venue, instrument = market["venue"], market["instrument_id"]
    kind, _ = PRICE_HISTORY[venue]
    rows = db.execute(
        """SELECT at,body,receipt_id FROM history WHERE venue=? AND instrument_id=? AND kind=?
           AND julianday(at)>=julianday(?) AND julianday(at)<=julianday(?) ORDER BY at""",
        (venue, instrument, kind, iso(first), iso(last)),
    )
    for at, raw, receipt in rows:
        try:
            body = json.loads(raw)
            clock = parse_time(at)
            if venue == venue_archive.POLY:
                yes, no = _price(body.get("longPrice")), _price(body.get("shortPrice"))
                available = clock + timedelta(minutes=1)
            else:
                bid = _price(venue_archive.kalshi_dollar_close(body.get("yes_bid")))
                ask = _price(venue_archive.kalshi_dollar_close(body.get("yes_ask")))
                yes = (bid + ask) / 2 if bid is not None and ask is not None and bid <= ask and (bid, ask) != (0, 1) else None
                no = None
                available = clock
        except (AttributeError, TypeError, ValueError):
            continue
        if yes is not None or no is not None:
            yield {"at": iso(clock), "available_at": iso(available), "yes": yes, "no": no, "receipt_id": receipt}


def _payout(row):
    raw = row["yes_payout"]
    if raw is None or not row["settlement_received_at"] or not row["settlement_source"]:
        return None
    amount = _price(raw)
    if amount is None:
        return None
    return {
        "yes_payout": amount,
        "source": row["settlement_source"],
        "venue_at": row["settlement_at"],
        "archive_received_at": row["settlement_received_at"],
    }


def _bucket(db, row, start, end, *, archive_prices=True):
    if row["family"] in TEMPERATURE:
        bounds = temperature_window(row)
        if bounds is None:
            return None
        threshold = {"lower_f": bounds[0], "upper_f": bounds[1]}
        label = row["label"]
    else:
        labels = {"observed-rain-code": "Eligible METAR rain code", "at-least": "At least", "above": "Above",
                  "above-or-trace": "Measurable precipitation or trace"}
        operator, lower = row["operator"], row["lower"]
        label = labels.get(operator)
        if label and lower is not None and operator != "above-or-trace":
            label = f"{label} {lower} {row['units']}"
        threshold = {"threshold_units": row["units"], "operator": operator}
    archived_payout = _payout(row)
    native_close = (venue_archive.native_closed_book_at(db, row["instrument_id"])
                    if archive_prices and row["venue"] == venue_archive.POLY else None)
    cutoff = parse_time(native_close) if native_close else end
    price_points = _prices(db, row, start, cutoff) if archive_prices else ()
    bucket = {
        "id": row["instrument_id"],
        "market_id": row["native_id"] or row["instrument_id"],
        "yes_side_id": row["yes_side_id"],
        "no_side_id": row["no_side_id"],
        "label": label or row["label"] or row["title"] or row["instrument_id"],
        "rules": row["rules_primary"],
        "rules_secondary": row["rules_secondary"],
        **threshold,
        "prices": [point for point in price_points if native_close is None or parse_time(point["available_at"]) <= cutoff],
        "archived_payout": archived_payout,
    }
    if native_close:
        bucket["native_trading_closed_at"] = native_close
    if archived_payout and archived_payout["venue_at"]:
        settlement = parse_time(archived_payout["venue_at"])
        if settlement >= start:
            bucket["settlement"] = {**archived_payout, "available_at": iso(settlement),
                                    "timeline_basis": "native venue settlement time"}
    return bucket


def _rain_ensemble(data, day, station):
    members = gefs_precipitation.day_members(data, date.fromisoformat(day), station)
    if len(members) != len(gefs_precipitation.gefs.MEMBERS):
        return None
    amounts = [row["liquid_mm"] for row in members]
    threshold = .254
    return {"available_at": max(row["available_at"] for row in members), "members": len(amounts),
            "members_at_threshold": sum(amount >= threshold for amount in amounts), "median_mm": median(amounts),
            "threshold_mm": threshold, "source": gefs_precipitation.gefs.DOCS}


def load(data, venue, instrument, day, weather_events, *, archive_prices=True):
    if venue not in PRICE_HISTORY:
        raise ValueError("Unsupported venue replay")
    with venue_archive.connect(data) as db:
        db.execute("BEGIN")
        selected = db.execute(
            "SELECT * FROM markets WHERE venue=? AND instrument_id=?",
            (venue, instrument),
        ).fetchone()
        if selected is None or selected["family"] not in FAMILIES or selected["day"] != day:
            return None
        event = next((row for row in weather_events
                      if row["station"] == selected["station"] and row["date"] == day), None)
        if event is None:
            return None
        first = parse_time(event["climate_starts_at"])
        close = parse_time(event["climate_ends_at"])
        market_start = parse_time(selected["opened_at"]) if selected["opened_at"] else first
        market_close = parse_time(selected["closed_at"]) if selected["closed_at"] else close
        if market_start >= market_close:
            return None
        # The station date can still be useful when a venue lists a different
        # resolution source or does not establish its own exact climate window.
        native_start, native_end = selected["climate_starts_at"], selected["climate_ends_at"]
        exact_window = bool(native_start and native_end and (parse_time(native_start), parse_time(native_end)) == (first, close))
        same_climate_report = exact_window and selected["resolution_source"] == "NWS Daily Climate Report"
        members = db.execute(
            """SELECT * FROM markets WHERE venue=? AND event_id=? AND station=? AND day=? AND family=? ORDER BY instrument_id""",
            (venue, selected["event_id"], selected["station"], day, selected["family"]),
        )
        start = max(market_start, first - timedelta(minutes=2))
        buckets = [
            bucket for row in members
            if (bucket := _bucket(db, row, start, market_close, archive_prices=archive_prices)) is not None
        ]
    if selected["family"] in TEMPERATURE:
        buckets.sort(key=lambda bucket: float("-inf") if bucket["lower_f"] is None else bucket["lower_f"])
    if not any(bucket["id"] == instrument for bucket in buckets):
        return None
    event_id = selected["event_id"]
    venue_url = f"https://polymarket.us/event/{event_id}" if venue == venue_archive.POLY else selected["source"]
    # The base station view may contain a different venue's outcome. Pull
    # weather provenance separately from the exact native payout above.
    weather = {key: value for key, value in event.items() if key not in (
        "settlement", "rules_source", "market_source", "market_url", "trading_closed_at", "market_end_at", "settles_at",
    )}
    if not weather.get("official_reports") and (base := event.get("settlement")) and base.get("available_at"):
        weather["official_reports"] = [{key: base[key] for key in (
            "high_f", "low_f", "product", "source", "available_at", "precip_in",
        ) if key in base}]
    if archive_prices and selected["family"] == "daily-rain" and selected["units"] == "in":
        ensemble = _rain_ensemble(data, day, selected["station"])
        if ensemble:
            weather["rain_ensemble"] = ensemble
        short_range = hrrr_precipitation.day_amount(data, date.fromisoformat(day), selected["station"])
        if short_range:
            weather["rain_hrrr"] = {key: short_range[key] for key in (
                "amount_mm", "packing_error_mm", "distance_km", "available_at", "starts_at", "ends_at")}
    return {
        **weather,
        "id": f"{venue}:{event_id}", "native_id": selected["native_event_id"] or event_id,
        "family": selected["family"], "venue": venue, "weather_only": False,
        "title": selected["event_title"] or selected["title"],
        "buckets": buckets, "starts_at": iso(market_start), "trading_closed_at": iso(market_close),
        "market_source": selected["source"], "market_url": venue_url, "rules_source": selected["rules_url"],
        "settles_at": selected["settlement_at"], "reporting_source": selected["resolution_source"],
        "replay_market_window_exact": exact_window, "replay_same_climate_report": same_climate_report,
        "replay_no_model": not (venue == venue_archive.POLY and selected["family"] == "daily-high"
                                and same_climate_report and study._partition(buckets) == "exhaustive"),
        "price_basis": PRICE_HISTORY[venue][1],
    }
