"""Retain extended NOAA station temperature bulletins and their source clocks."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from hashlib import sha256
from itertools import groupby
import json
from pathlib import Path
import re
from urllib.request import urlopen

from . import expansion_nbm, expansion_weather, research_model
from .history import _write
from .nbm import AWS_NBM, _station_fields, climate_window, iso, utc

STATION = "KCVG"
DOCUMENTATION = "https://vlab.noaa.gov/web/mdl/nbm-text-products"
CYCLES = (12, 18)
STATION_OFFSETS = {"KCVG": 8_890_000, "KNYC": 12_500_000, "KMDW": 11_820_000, "KORD": 12_770_000,
                   "KDEN": 9_090_000, "KPDX": 13_000_000, "KBUF": 8_370_000}


def _cell(text, right):
    raw = text[right - 3:right].strip()
    return int(raw) if raw.lstrip("+-").isdigit() else None


def parse_card(body, station):
    text, offset = expansion_nbm._station_text(body, station, product="NBE")
    issued, rows = _station_fields(text, station, "nbe")
    lead_cells = [(int(item[0]), item.end()) for item in re.finditer(r"\d+", rows["FHR"])]
    leads = [lead for lead, _ in lead_cells]
    if not leads or leads != sorted(set(leads)) or "TXN" not in rows or "UTC" not in rows:
        raise ValueError("Invalid native extended temperature forecast")
    values = []
    for lead, right in lead_cells:
        valid = issued + timedelta(hours=lead)
        if valid.hour != _cell(rows["UTC"], right) or valid.hour not in (0, 12):
            raise ValueError("Native extended forecast valid hour disagrees with lead")
        value = _cell(rows["TXN"], right)
        spread = _cell(rows.get("XND", ""), right)
        if value is not None and -130 <= value <= 140:
            values.append({"lead_hours": lead, "valid_at": iso(valid), "temp_f": value,
                           "sd_f": spread if spread is not None and 0 <= spread <= 100 else None,
                           "field": "TXN minimum" if valid.hour == 12 else "TXN maximum"})
    if not values:
        raise ValueError("Native extended station card contains no temperatures")
    printed = re.search(rb"\bNBM\s+V(\d+(?:\.\d+)+)\s+NBE\b", text.splitlines()[0])
    return {"station": station, "issued_at": iso(issued), "printed_model_version": printed[1].decode() if printed else None,
            "forecasts": values, "card_text": text.decode("ascii"), "card_sha256": sha256(text).hexdigest(),
            "offset_in_response": offset}


def card_path(data, issue, *, station=STATION):
    return Path(data) / "expansion" / "nbe" / station / f"{issue:%Y%m%d-%H}-nbe.json"


def load_card(data, issue, *, station=STATION, fetch=False, get=urlopen):
    if station not in STATION_OFFSETS:
        return None
    path = card_path(data, issue, station=station)
    if path.is_file():
        payload = json.loads(path.read_bytes())
        if (payload["station"] != station or utc(payload["issued_at"]) != issue
                or sha256(payload["card_text"].encode("ascii")).hexdigest() != payload["card_sha256"]):
            raise ValueError("Retained NOAA NBE station card checksum disagrees")
        return payload
    if not fetch:
        return None
    if issue.hour not in CYCLES or issue.minute or issue.second or issue.microsecond or issue.utcoffset() != timedelta(0):
        raise ValueError("The retained NBE station scope supports the 12Z and 18Z modeled issues")
    source = f"{AWS_NBM}/blend.{issue:%Y%m%d}/{issue:%H}/text/blend_nbetx.t{issue:%H}z"
    if station == STATION:
        byte_range = (8_600_000, 9_200_000) if issue.year >= 2026 else (9_300_000, 10_650_000)
    elif issue.year == 2026:
        offset = STATION_OFFSETS[station]
        byte_range = (offset - 300_000, offset + 300_000)
    else:
        byte_range = None
    card = expansion_nbm.fetch_station_card(source, station, issue, parse_card, byte_range, "NOAA NBM NBE station bulletin", get=get)
    _write(path, research_model._json_bytes(card))
    return card


def daily_extrema(card):
    """Pair 18-hour NBE guides stamped by target calendar date."""
    issued = utc(card["issued_at"])
    points = {utc(row["valid_at"]): row for row in card["forecasts"]}
    result = []
    for noon, low in sorted(points.items()):
        lead_day = (noon.date() - issued.date()).days
        if noon.hour != 12 or not 1 <= lead_day <= 7:
            continue
        midnight = noon + timedelta(hours=12)
        high = points.get(midnight)
        if high is not None and low["temp_f"] <= high["temp_f"]:
            dated = {"target_date": str(noon.date()), "lead_day": lead_day}
            for kind, point, at in (("minimum", low, noon), ("maximum", high, midnight)):
                dated[kind] = {**point, "window_starts_at": iso(at - timedelta(hours=12)), "window_ends_at": iso(at + timedelta(hours=6))}
            result.append(dated)
    return result


def _retained_view(data, station, issue, start, through, original_release):
    try:
        card = load_card(data, issue, station=station)
        if card is None:
            return None
        published, received = (utc(card[field]) for field in ("available_at", "received_at"))
        known = published if original_release else received
        if not issue <= published <= received or known > through:
            return None
        days = []
        for day in daily_extrema(card):
            opens, ends = climate_window(day["target_date"], expansion_weather.STATIONS[station][3])
            if max(start, known) < ends:
                days.append({**day, "climate_starts_at": iso(opens), "climate_ends_at": iso(ends)})
    except (OSError, ValueError, KeyError, TypeError, UnicodeError):
        return None
    if not days:
        return None
    return {"station": station, "kind": "extended_station_temperature_forecast", "units": "°F",
            "known_at": iso(known), "knowledge_at": iso(known),
            "availability_basis": "public_archive_release" if original_release else "local_receipt",
            "source_published_at": card["available_at"], "daily_extrema": days,
            **{field: card[field] for field in ("issued_at", "received_at", "source", "card_sha256", "printed_model_version")}}


def station_outlooks(data, station, start, through, *, original_release=False):
    """Read the valid starting issue and subsequently known newer issues in a cursor interval."""
    if station not in STATION_OFFSETS:
        return []
    start, through = utc(start), utc(through)
    if start > through:
        raise ValueError("Weather outlook cursor closes before it opens")
    first_issue = start.date() - timedelta(days=8)
    candidates = []
    for day in research_model._days(first_issue, through.date()):
        for cycle in CYCLES:
            issued = datetime.combine(day, time(cycle), timezone.utc)
            if issued <= through and (view := _retained_view(data, station, issued, start, through, original_release)):
                candidates.append((utc(view["known_at"]), issued, view))

    prior = max((item for item in candidates if item[0] <= start), key=lambda item: item[1], default=None)
    found = [prior[2]] if prior else []
    newest_issue = prior[1] if prior else None
    later = sorted((item for item in candidates if item[0] > start), key=lambda item: item[:2])
    for _, arrivals in groupby(later, key=lambda item: item[0]):
        _, issue, view = max(arrivals, key=lambda item: item[1])
        if newest_issue is None or issue > newest_issue:
            found.append(view)
            newest_issue = issue
    return found


def station_outlook(data, station, as_of, *, original_release=False):
    """Read the most recent retained NBE issue known by the requested clock."""
    rows = station_outlooks(data, station, as_of, as_of, original_release=original_release)
    return rows[-1] if rows else None
