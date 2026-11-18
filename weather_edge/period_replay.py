"""Read period rain evidence and retained weather guidance for exact contracts."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
import gzip
from hashlib import sha256
import json
from pathlib import Path

from . import expansion_weather, venue_snow_data
from .market import iso, parse_time


def monthly_rain(data, market):
    if (market.get("family") != "monthly-rain" or market.get("station") not in expansion_weather.CLI_OFFICE
            or market.get("resolution_source") != "NWS Daily Climate Report" or market.get("operator") != "above"
            or market.get("units") != "in" or not market.get("climate_starts_at") or not market.get("climate_ends_at")):
        return None
    from .venue_monthly_rain import _parse_cli

    station = market["station"]
    month = date.fromisoformat(market["day"])
    raw_files = {}
    for location in (Path(data) / "expansion" / "cli", Path(data) / "venues" / "snow-climate"):
        for path in (location / station).glob("*.txt"):
            if expansion_weather._valid_product(path.stem, station):
                raw_files.setdefault(path.stem, path)
    reports = []
    cutoff = datetime.now(timezone.utc)
    for product, path in sorted(raw_files.items()):
        try:
            raw = path.read_bytes()
            text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n")
            label = expansion_weather.CLI_DATE.search(text)
            if not label:
                continue
            month_name, day_number, year = label.groups()
            day = date(int(year), expansion_weather.MONTHS[month_name], int(day_number))
            if (day.year, day.month) != (month.year, month.month):
                continue
            reading = _parse_cli(text, station, day, product)
            if reading is None or parse_time(reading["published_at"]) > cutoff:
                continue
            amount = 0.0 if reading["monthly_raw"] == "T" else expansion_weather.parse_number(reading["monthly_raw"])
            if amount is None:
                continue
            reports.append({**reading, "amount_in": amount, "source": venue_snow_data.IEM_TEXT + product,
                            "sha256": sha256(raw).hexdigest()})
        except (OSError, ValueError, KeyError):
            continue
    if not reports:
        return None
    return {"climate_start": market["climate_starts_at"], "climate_end": market["climate_ends_at"],
            "threshold_in": float(market["lower"]), "settled_at": market["settlement_at"],
            "observed_through": iso(cutoff), "reports": reports}


def reporting_days_rain(data, market):
    if (market.get("family") not in {"weekend-rain", "holiday-rain"} or not market.get("station")
            or market.get("resolution_source") != "The Weather Company" or market.get("operator") != "above"
            or market.get("lower") != "0"):
        return None
    from . import strategy_twc
    from .venue_contracts import _precipitation_dates

    first, last = _precipitation_dates(market.get("rules_primary") or "", market.get("rules_secondary") or "")
    if not first or not last or not timedelta() <= last - first <= timedelta(days=2):
        return None
    reports = {row["label_date"]: row for row in strategy_twc.reports(
        data, station=market["station"], start=str(first), end=str(last),
    )}
    days = []
    for n in range((last - first).days + 1):
        day = str(first + timedelta(days=n))
        found = reports.get(day)
        row = {"date": day, "provider_status": "awaiting a retained report"}
        if found:
            row.update({field: found.get(field) for field in ("provider_status", "available_at", "provider_issued_at", "source_url",
                                                              "receipt_id", "body_sha256")})
            if found["provider_status"] in ("official", "revised"):
                row["precipitation_in"] = found["precipitation_in"]
        days.append(row)
    return {"station": market["station"], "days": days}


@lru_cache(maxsize=4)
def _holiday_rows(path, digest):
    body = path.read_bytes()
    if sha256(body).hexdigest() != digest:
        raise ValueError("Holiday GEFS forecast evidence digest mismatch")
    return {row["venue_market"]["instrument_id"]: row for row in json.loads(gzip.decompress(body))["stations"]}


def holiday_rain_outlook(data, market, as_of):
    """Find a compatible retained forecast for the current strict holiday rain threshold."""
    if (market.get("family") != "holiday-rain" or market.get("venue") != "kalshi"
            or market.get("status") != "open" or market.get("operator") != "above"
            or market.get("lower") != "0" or market.get("units") != "in" or not market.get("station")):
        return None
    root = Path(data)
    clock = parse_time(as_of)
    available = []
    for path in (root / "research" / "models").glob("thanksgiving-gefs-exact-day-v1-*.json"):
        try:
            record = json.loads(path.read_bytes())
            meta = record["provenance"]
            rows = _holiday_rows(root / meta["replay_details_path"], meta["replay_details_sha256"])
            row = rows.get(market["instrument_id"])
            if row is None or any(row["venue_market"].get(key) != market.get(key) for key in
                                  ("station", "day", "rules_primary", "rules_secondary", "resolution_source")):
                continue
            lower, upper = row["known_wet_model_members"], row["possibly_wet_model_members"]
            zone = row["timezone_proof"]["timezone"]
            if (not parse_time(meta["issue"]) <= parse_time(row["source_available_at"])
                    <= parse_time(row["local_received_at"]) <= clock < parse_time(market["closed_at"])
                    or not 0 <= lower <= upper <= row["complete_members"] == 31 or not zone):
                continue
            available.append((parse_time(meta["issue"]), parse_time(record["created_at"]), record["id"], row, meta["GEFS"], zone))
        except (OSError, KeyError, EOFError, ValueError, TypeError):
            continue
    if not available:
        return None
    issue, _, ident, row, docs, zone = max(available, key=lambda item: item[:3])
    return {**{key: row[key] for key in ("station", "start", "end", "source_available_at", "local_received_at", "complete_members",
                                         "known_wet_model_members", "possibly_wet_model_members")},
            "issue": iso(issue), "docs": docs, "rain_threshold_in": .01,
            "reporting_timezone": zone, "study_id": ident}
