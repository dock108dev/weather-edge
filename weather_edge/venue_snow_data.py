"""Frozen daily snowfall and publication evidence from NWS reports carried by IEM."""

from calendar import monthrange
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import time
from urllib.error import HTTPError
from urllib.parse import quote

from .capture import ReadOnlyHTTP
from .collector import paused
from .expansion_weather import STATIONS, _valid_product, cli_url
from .history import _write
from .market import decimal, iso
from .nbm import climate_window

CORE_STATIONS = ("KNYC", "KORD", "KSEA", "KDEN")
START_YEAR, LAST_YEAR = 2006, 2026
SNOW_MONTHS = (1, 2, 3, 12)
IEM_TEXT = "https://mesonet.agron.iastate.edu/api/1/nwstext/"


def _archive(data):
    return Path(data) / "venues" / "snow-climate"


def _obtain(data, relative, source):
    root = _archive(data)
    path = root / relative
    manifest = path.with_suffix(path.suffix + ".receipt.json")
    if path.is_file() and manifest.is_file():
        raw, receipt = path.read_bytes(), json.loads(manifest.read_bytes())
        if sha256(raw).hexdigest() != receipt["sha256"]:
            raise ValueError(f"Archived source checksum changed for {relative}")
        return raw, receipt
    if paused(data):
        raise RuntimeError("Live collection has been stopped")
    retries = 2
    for attempt in range(retries + 1):
        try:
            received = ReadOnlyHTTP(time.monotonic() + 15)(source)
            break
        except HTTPError as error:
            if error.code not in (429, 500, 502, 503, 504) or attempt == retries:
                raise
            time.sleep(min(4, 2 ** attempt))
            if paused(data):
                raise RuntimeError("Live collection has been stopped")
    receipt = {
        "source": source, "requested_at": iso(received.requested_at),
        "received_at": iso(received.at), "sha256": sha256(received.body).hexdigest(),
        "path": str(path.relative_to(Path(data))),
        "server_last_modified": received.headers.get("last-modified"),
    }
    _write(path, received.body)
    _write(manifest, json.dumps(receipt, separators=(",", ":")).encode())
    time.sleep(0.35)
    return received.body, receipt


def obtain_year(data, station, year):
    source = cli_url(station, year)
    return _obtain(data, Path(station) / f"{year}.json", source)


def obtain_product(data, station, product):
    if not _valid_product(product, station):
        raise ValueError("Unexpected station's NWS CLI product")
    source = IEM_TEXT + quote(product, safe="")
    raw, receipt = _obtain(data, Path(station) / f"{product}.txt", source)
    return raw.decode("utf-8", errors="replace"), receipt


def backfill(data):
    fetched = []
    problems = []
    for station in CORE_STATIONS + ("KMDW",):
        years = range(START_YEAR, LAST_YEAR + 1) if station in CORE_STATIONS else (2025, 2026)
        for year in years:
            try:
                _, receipt = obtain_year(data, station, year)
                fetched.append(receipt)
            except (OSError, ValueError) as error:
                problems.append({"station": station, "year": year, "reason": str(error)})
    return {"sources": fetched, "problems": problems}


def _snow_value(raw):
    if raw == "T":
        return "trace", "0"
    try:
        value = decimal(raw)
    except ValueError:
        return "missing", None
    if value < 0:
        return "missing", None
    return "measured", str(value)


def _day_row(station, row, *, offset):
    product = row.get("product")
    if row.get("station") != station or not _valid_product(product, station):
        return None
    day = date.fromisoformat(row["valid"])
    published_at = datetime.strptime(product[:12], "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
    _, end_at = climate_window(day, offset)
    if published_at < end_at or published_at > end_at + timedelta(days=15):
        return None
    kind, snow = _snow_value(row.get("snow"))
    cumulative_kind, cumulative = _snow_value(row.get("snow_month"))
    return {
        "date": day.isoformat(), "product": product, "published_at": iso(published_at),
        "source": IEM_TEXT + product, "daily_kind": kind, "daily_inches": snow,
        "cumulative_kind": cumulative_kind, "cumulative_inches": cumulative,
        "high_f": row.get("high"), "low_f": row.get("low"),
    }


def load_corpus(data):
    rows = []
    sources = []
    missing = Counter()
    for station in (*CORE_STATIONS, "KMDW"):
        offset = STATIONS[station][3]
        for year in range(START_YEAR, LAST_YEAR + 1):
            if station == "KMDW" and year < 2025:
                continue
            annual_path = _archive(data) / station / f"{year}.json"
            receipt_path = annual_path.with_suffix(".json.receipt.json")
            if not annual_path.is_file() or not receipt_path.is_file():
                missing["station-year unavailable"] += 1
                continue
            body = annual_path.read_bytes()
            receipt = json.loads(receipt_path.read_bytes())
            if sha256(body).hexdigest() != receipt["sha256"]:
                raise ValueError(f"Changed immutable winter input {annual_path}")
            sources.append(receipt)
            observations = {}
            for original in json.loads(body)["results"]:
                day = _day_row(station, original, offset=offset)
                if day is None:
                    continue
                if day["date"] in observations:
                    raise ValueError(f"Ambiguous annual NWS publication at {station} {day['date']}")
                observations[day["date"]] = day
            for month in SNOW_MONTHS:
                if year == LAST_YEAR and month == 12:
                    continue
                days = [observations.get(date(year, month, number).isoformat())
                        for number in range(1, monthrange(year, month)[1] + 1)]
                final = days[-1]
                daily = [decimal(item["daily_inches"]) for item in days if item and item["daily_inches"] is not None]
                complete_daily = len(daily) == len(days)
                monthly = final["cumulative_inches"] if final else None
                if monthly is None:
                    missing["no complete official month-end snowfall total"] += 1
                if complete_daily and monthly is not None and sum(daily) != decimal(monthly):
                    missing["sum differs from reported month-end total"] += 1
                rows.append({
                    "station": station, "year": year, "month": month, "days": days,
                    "official_total": monthly, "complete_daily": complete_daily,
                    "daily_total": str(sum(daily)) if complete_daily else None,
                    "trace_days": sum(bool(item and item["daily_kind"] == "trace") for item in days),
                })
    return {"version": "kalshi-monthly-snow-v1", "months": rows, "sources": sources, "missing": dict(missing)}


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    args = parser.parse_args()
    result = backfill(args.data)
    print(f"{len(result['sources'])} archived station-years")
    print(json.dumps(result["problems"], indent=2))
