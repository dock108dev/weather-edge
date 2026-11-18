"""Recover whole-degree ASOS temperatures from exact-station METAR remarks."""

from __future__ import annotations

import math
import re
from datetime import timedelta

from .nbm import iso, utc

ASOS_FAQ = "https://www.weather.gov/lot/weather_observations_faq"
METAR_DOCS = "https://aviationweather.gov/help/data/"
ASOS_USER_GUIDE = "https://www.weather.gov/media/asos/aum-toc.pdf"
REPORT_START = re.compile(r"^(?:(?:METAR|SPECI)\s+)?([A-Z0-9]{4})\s+(\d{6})Z\b")
POINT_TENTHS = re.compile(r"(?<!\S)T([01])(\d{3})(?:[01]\d{3}|////)(?=\s|$)")
POINT_WHOLE = re.compile(r"(?<!\S)(M?\d{2})/(?:M?\d{2}|//)?(?=\s|$)")
SIX_HOUR_LOW = re.compile(r"(?<!\S)2([01])(\d{3})(?=\s|$)")


def integer_asos_interval(celsius, display_step_c):
    """Return stored integer °F values compatible with the coded °C rounding."""
    low_c, high_c = celsius - display_step_c / 2, celsius + display_step_c / 2
    # Retain both possible rounded readings at ties and avoid float fuzz.
    lower = math.ceil(low_c * 9 / 5 + 32 - 1e-9)
    upper = math.floor(high_c * 9 / 5 + 32 + 1e-9)
    return (lower, upper) if lower <= upper else None


def _tenths(match):
    return int(match[2]) / 10 * (-1 if match[1] == "1" else 1)


def _witness(interval, stamp, available, start, end, kind, raw, degrees_c, step, code):
    lo, hi = interval
    return {"lower_f": lo, "upper_f": hi, "kind": kind, "report_at": iso(stamp), "available_at": iso(available),
            "period_start": iso(start), "period_end": iso(end), "raw_report": raw, "temperature_code": code,
            "reported_c": degrees_c, "temperature_precision_c": step}


def metar_low_evidence(row, station, climate_start, decision, *, include_six_hour=True):
    """Read precision and interval solely from an eligible exact-station raw report."""
    raw = (row.get("raw_report") or row.get("report") or row.get("raw") or "").strip().rstrip("=")
    ident = REPORT_START.match(raw)
    if not ident or ident[1] != station:
        return []
    stamp, available = utc(row["at"]), utc(row["available_at"])
    if not climate_start <= stamp <= available <= decision or stamp.strftime("%d%H%M") != ident[2]:
        return []
    body, _, remarks = raw.partition(" RMK ")
    output = []
    if exact := POINT_TENTHS.search(remarks):
        precision, temp_c, point_code = 0.1, _tenths(exact), exact[0]
    elif whole := POINT_WHOLE.search(body):
        cold = whole[1].startswith("M")
        precision, temp_c, point_code = 1.0, int(whole[1].removeprefix("M")) * (-1 if cold else 1), whole[0]
    else:
        precision, temp_c, point_code = None, None, None
    if precision is not None and (interval := integer_asos_interval(temp_c, precision)):
        output.append(_witness(interval, stamp, available, stamp, stamp, "point", raw, temp_c, precision, point_code))
    # Hourly extremes may be coded near the nominal synoptic time. Use the union
    # of nominal-hour and report-time interpretations to exclude a midnight overlap.
    if include_six_hour and (six := SIX_HOUR_LOW.search(remarks)):
        nominal = (stamp + timedelta(minutes=30)).replace(minute=0, second=0, microsecond=0)
        start = min(stamp, nominal) - timedelta(hours=6)
        end = max(stamp, nominal)
        if (nominal.hour % 6 == 0 and abs(stamp - nominal) <= timedelta(minutes=15)
                and climate_start <= start <= end <= decision
                and (interval := integer_asos_interval(_tenths(six), 0.1))):
            output.append(_witness(interval, stamp, available, start, end, "six-hour low", raw, _tenths(six), 0.1, six[0]))
    return output


def upper_bound(rows, station, climate_start, decision, *, include_six_hour=True):
    evidence = [witness for row in rows for witness in metar_low_evidence(
        row, station, climate_start, decision, include_six_hour=include_six_hour)]
    return min(evidence, key=lambda row: (row["upper_f"], row["temperature_precision_c"], row["available_at"]), default=None)
