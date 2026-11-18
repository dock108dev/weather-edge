"""Attach the retained Cincinnati forecast to the exact confirmed native market."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
from pathlib import Path

from .nbm import utc

STUDY_VERSION = "cincinnati-hdd-extended-forecast-v1"
SCHEMA = 1
EVENT_ID = "KXHDDCVG-KCVG-01OCT26-31DEC26"
SERIES_ID = "KXHDDCVG"
STATION = "KCVG"
PERIOD = {"first_date": "2026-10-01", "last_date": "2026-12-31", "starts_at": "2026-10-01T04:00:00Z",
          "ends_at": "2027-01-01T05:00:00Z", "source_timezone": "America/New_York"}


def _strike(market):
    if (market.get("venue"), market.get("family"), market.get("series_id"), market.get("event_id"), market.get("station"),
        market.get("operator"), market.get("units"), market.get("resolution_source")) != (
            "kalshi", "heating-degree-days", SERIES_ID, EVENT_ID, STATION, "below", "HDD (65°F)", "The Weather Company daily HDD (65)"):
        return None
    rules = market.get("rules_primary") or ""
    secondary = market.get("rules_secondary") or ""
    if ("KCVG, WMO identifier 72421" not in rules or "Oct 1, 2026 and Dec 31, 2026" not in rules
            or "HDD (65)" not in secondary or "Hourly Mean(F)" not in secondary or "The Weather Company" not in secondary):
        return None
    number = Decimal(str(market["upper"]))
    if not number.is_finite() or not (0 < number == number.to_integral_value()):
        return None
    if market.get("instrument_id") != f"{EVENT_ID}-T{int(number)}":
        return None
    return number


def market_projection(data, market, as_of):
    """Return a retained weather projection only during the exact market's native window."""
    try:
        if (number := _strike(market)) is None:
            return None
        at = utc(as_of)
        starts, ends = (utc(PERIOD[key]) for key in ("starts_at", "ends_at"))
        if (utc(market["climate_starts_at"]), utc(market["climate_ends_at"])) != (starts, ends):
            return None
        if not max(utc(market["opened_at"]), starts) <= at < min(utc(market["closed_at"]), ends):
            return None
    except (TypeError, ValueError, AttributeError, KeyError, InvalidOperation):
        return None

    newest, result = None, None
    for path in sorted((Path(data) / "research" / "models").glob(f"{STUDY_VERSION}-*.json")):
        try:
            record = json.loads(path.read_bytes())
            if record["version"] != STUDY_VERSION or record["id"] != path.stem:
                continue
            projection = record["market_projection"]
            if (projection["schema"], projection["station"], projection["event_id"], projection["contract_period"]) != (
                    SCHEMA, STATION, EVENT_ID, PERIOD):
                continue
            at_input = utc(projection["known_at"])
            forecast, accrued = projection["forecast"], projection["accrual"]
            if (not utc(forecast["issued_at"]) <= utc(forecast["source_published_at"]) <= utc(forecast["received_at"]) <= at_input <= at
                    or utc(accrued["last_source_available_at"]) > at_input):
                continue
            threshold = projection["thresholds"].get(market["instrument_id"])
            if (threshold is None or Decimal(str(threshold["threshold_hdd"])) != number or threshold["operator"] != "below"
                    or (newest is not None and newest >= at_input)):
                continue
            newest = at_input
            result = {"schema": SCHEMA, "study_id": record["id"], "known_at": projection["known_at"], "station": STATION, "event_id": EVENT_ID,
                      "contract_period": {**PERIOD, "native_closed_at": market["closed_at"]}, "accrual": accrued,
                      "forecast": forecast, "threshold": threshold}
        except (OSError, KeyError, ValueError, TypeError, AttributeError, InvalidOperation):
            continue
    return result
