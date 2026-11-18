"""Parse as-issued NOAA National Blend of Models station text guidance."""

from __future__ import annotations

import json
import re
from datetime import date, datetime, time, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.error import HTTPError
from urllib.request import Request, urlopen

AWS_NBM = "https://noaa-nbm-grib2-pds.s3.amazonaws.com"
PRODUCTS = {"nbh": "nbhtx", "nbs": "nbstx"}
STATION_RANGES = {
    "KNYC": {"nbh": "bytes=17000000-17799999", "nbs": "bytes=17300000-18399999"},
    "KMIA": {"nbh": "bytes=16000000-17100000", "nbs": "bytes=16500000-17600000"},
    "KLAX": {"nbh": "bytes=15100000-16200000", "nbs": "bytes=15600000-16800000"},
}


def utc(value: str | datetime) -> datetime:
    """Parse a timestamp with an explicit offset."""
    timestamp = datetime.fromisoformat(value) if isinstance(value, str) else value
    if timestamp.utcoffset() is None:
        raise ValueError(f"Timestamp has no offset: {value}")
    return timestamp.astimezone(timezone.utc)


def iso(value: datetime) -> str:
    return utc(value).isoformat().replace("+00:00", "Z")


def climate_window(day: str | date, standard_utc_offset_hours: int = -5) -> tuple[datetime, datetime]:
    local_day = date.fromisoformat(day) if isinstance(day, str) else day
    local_standard = timezone(timedelta(hours=standard_utc_offset_hours))
    start = datetime.combine(local_day, time.min, local_standard).astimezone(timezone.utc)
    return start, start + timedelta(days=1)


def nbm_url(day: date, cycle: int, product: str) -> str:
    return f"{AWS_NBM}/blend.{day:%Y%m%d}/{cycle:02d}/text/blend_{PRODUCTS[product]}.t{cycle:02d}z"


def _station_fields(data: bytes, station: str, product: str) -> tuple[datetime, dict[str, str]]:
    header = re.compile(
        rb"^\s*" + re.escape(station.encode("ascii"))
        + rb"[ \t]+NBM[^\r\n]+?\b" + product.upper().encode("ascii")
        + rb"\s+GUIDANCE\s+(\d{1,2}/\d{1,2}/\d{4})\s+(\d{4})\s+UTC[ \t]*\r?$",
        re.MULTILINE,
    ).search(data)
    if header is None:
        raise ValueError(f"No {station} {product.upper()} station card")
    issued_at = datetime.strptime(
        f"{header[1].decode()} {header[2].decode()}", "%m/%d/%Y %H%M"
    ).replace(tzinfo=timezone.utc)
    rows = data[header.end():header.end() + 2500].decode("ascii", "replace").splitlines()
    fields = {}
    for row in rows:
        if "GUIDANCE" in row:
            break
        if len(row) > 5:
            fields[row[:5].strip()] = row[5:]
    return issued_at, fields


def _columns(fields: dict[str, str], label: str, count: int) -> list[int | None]:
    text = fields.get(label, "")
    numbers = []
    for index in range(count):
        value = text[index * 3:index * 3 + 3].strip()
        numbers.append(int(value) if value.lstrip("+-").isdigit() else None)
    return numbers


def parse_nbh(data: bytes, station: str) -> dict:
    issued_at, fields = _station_fields(data, station, "nbh")
    hours = fields["UTC"].split()
    temperatures = _columns(fields, "TMP", len(hours))
    deviations = _columns(fields, "TSD", len(hours))
    sky_cover = _columns(fields, "SKY", len(hours))
    valid_at = issued_at
    hourly = []
    for hour, temperature, deviation, sky_pct in zip(hours, temperatures, deviations, sky_cover, strict=True):
        valid_at += timedelta(hours=(int(hour) - valid_at.hour) % 24 or 24)
        if temperature is not None:
            point = {"valid_at": iso(valid_at), "temp_f": temperature, "sd_f": deviation}
            if sky_pct is not None and 0 <= sky_pct <= 100:
                point["sky_pct"] = sky_pct
            hourly.append(point)
    if not hourly:
        raise ValueError(f"No {station} NBH temperatures")
    return {"issued_at": iso(issued_at), "hourly": hourly}


def parse_nbs(data: bytes, station: str) -> dict:
    issued_at, fields = _station_fields(data, station, "nbs")
    leads = fields["FHR"].split()
    maxima = []
    minima = []
    temperatures = []
    for lead, temp, deviation, profile in zip(
        leads, _columns(fields, "TXN", len(leads)), _columns(fields, "XND", len(leads)),
        _columns(fields, "TMP", len(leads)), strict=True
    ):
        valid_at = issued_at + timedelta(hours=int(lead))
        if profile is not None:
            temperatures.append({"valid_at": iso(valid_at), "temp_f": profile})
        # In CONUS the 18-hour maximum is stamped 00 UTC and covers 12–06 UTC.
        if valid_at.hour == 0 and temp is not None:
            maxima.append({"valid_at": iso(valid_at), "high_f": temp, "sd_f": deviation})
        # The CONUS 18-hour minimum is stamped 12 UTC and covers 00–18 UTC.
        elif valid_at.hour == 12 and temp is not None:
            minima.append({"valid_at": iso(valid_at), "low_f": temp, "sd_f": deviation})
    return {"issued_at": iso(issued_at), "daily_maxima": maxima, "daily_minima": minima,
            "three_hour_temperature": temperatures}


def _fetch_card(url: str, station: str, product: str) -> dict:
    parser = parse_nbh if product == "nbh" else parse_nbs
    byte_range = STATION_RANGES.get(station, {}).get(product)
    ranges = (byte_range, None) if byte_range else (None,)
    for range_header in ranges:
        headers = {"User-Agent": "weather-edge local research app", "Accept-Encoding": "identity"}
        if range_header:
            headers["Range"] = range_header
        with urlopen(Request(url, headers=headers), timeout=35) as response:
            data = response.read()
            available_at = parsedate_to_datetime(response.headers["Last-Modified"])
        try:
            card = parser(data, station)
        except (ValueError, KeyError):
            if range_header:
                continue
            raise
        return {**card, "available_at": iso(max(available_at, utc(card["issued_at"]))), "source": url}
    raise AssertionError("Missing NOAA request variant")


def load_nbm_cycle(cache_dir: Path, *, station: str, day: date, cycle: int) -> dict:
    """Download compact station cards for one NOAA issue, caching station records only."""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    result = {}
    for product in PRODUCTS:
        cache_path = cache_dir / f"{station}-{day:%Y%m%d}-{cycle:02d}-{product}.json"
        if cache_path.exists():
            result[product] = json.loads(cache_path.read_text())
            continue
        url = nbm_url(day, cycle, product)
        try:
            card = _fetch_card(url, station, product)
        except HTTPError as error:
            if error.code == 404:
                continue
            raise
        expected_issue = datetime.combine(day, time(cycle), timezone.utc)
        if utc(card["issued_at"]) != expected_issue:
            raise ValueError(f"NOAA card time mismatch at {url}")
        with NamedTemporaryFile(mode="w", dir=cache_dir, prefix=f".{cache_path.name}-", delete=False) as handle:
            temporary = Path(handle.name)
            try:
                handle.write(json.dumps(card, separators=(",", ":")) + "\n")
                handle.flush()
                temporary.replace(cache_path)
            finally:
                temporary.unlink(missing_ok=True)
        result[product] = card
    return result


def forecast_for_day(cycle_data: dict, climate_day: str | date, standard_utc_offset_hours: int = -5) -> dict | None:
    """Pair the CONUS 18-h max (when present) with its same-issue hourly forecasts."""
    start, end = climate_window(climate_day, standard_utc_offset_hours)
    nbh = cycle_data.get("nbh") or {}
    nbs = cycle_data.get("nbs") or {}
    hourly = [point for point in nbh.get("hourly", ()) if start <= utc(point["valid_at"]) < end]
    peak = max(hourly, key=lambda point: point["temp_f"], default=None)
    direct = next((p for p in nbs.get("daily_maxima", ()) if start <= utc(p["valid_at"]) < end), None)
    minimum = next((p for p in nbs.get("daily_minima", ()) if start <= utc(p["valid_at"]) < end), None)
    lows = {"low_f": minimum["low_f"], "low_sd_f": minimum["sd_f"], "low_valid_at": minimum["valid_at"],
            "low_issued_at": nbs["issued_at"], "low_available_at": nbs["available_at"],
            "low_source": nbs["source"], "low_availability_basis": nbs.get("availability_basis")} if minimum else {}
    points = [point for point in nbs.get("three_hour_temperature", ())
              if start - timedelta(hours=3) <= utc(point["valid_at"]) <= end + timedelta(hours=3)]
    short_range = {"short_range_temperature": points, "short_range_issued_at": nbs["issued_at"],
                   "short_range_available_at": nbs["available_at"], "short_range_source": nbs["source"],
                   "short_range_availability_basis": nbs.get("availability_basis")} if points else {}
    if direct:
        return {
            "issued_at": nbs["issued_at"],
            "available_at": iso(max(utc(item["available_at"]) for item in (nbh, nbs) if item)),
            "valid_at": direct["valid_at"],
            "high_f": direct["high_f"],
            "high_sd_f": direct["sd_f"],
            "high_source": "noaa-nbm-nbs-txn",
            "hourly": hourly,
            "source": nbs["source"],
            "hourly_source": nbh.get("source"),
            **lows,
            **short_range,
        }
    if peak is None:
        return None
    return {
        "issued_at": nbh["issued_at"],
        "available_at": nbh["available_at"],
        "valid_at": peak["valid_at"],
        "high_f": peak["temp_f"],
        "high_sd_f": peak["sd_f"],
        "high_source": "noaa-nbm-nbh-hourly-max",
        "hourly": hourly,
        "source": nbh["source"],
        "hourly_source": nbh["source"],
        **lows,
        **short_range,
    }
