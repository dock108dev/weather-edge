"""Build an offline replay from archived public observations and Polymarket US records."""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import math
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from .nbm import climate_window, forecast_for_day, iso, load_nbm_cycle, utc

POLYMARKET = "https://gateway.polymarket.us"
IEM = "https://mesonet.agron.iastate.edu"
MADIS = "https://madis-data.ncep.noaa.gov/madisPublic1/data/archive"
MADIS_DOCS = "https://madis.ncep.noaa.gov/madis_metar.shtml"
MARKET_REPORTS = "https://www.polymarketexchange.com/files/daily-market-report"
RULES = "https://www.cftc.gov/IndustryOversight/IndustryFilings/TradingOrganizationProducts/60333"
WEATHER_FAQ = "https://docs.polymarket.us/faqs/weather-faqs"
PRICE_DOCS = "https://docs.polymarket.us/api-reference/price-history/get-price-history"
DISPLAY_SAMPLE_DELAY = timedelta(minutes=1)

# The names of the bucket limits come from the exchange titles and rules. The
# slug is solely an identity; its "lt" fragment is misleading for these ranges.
RANGE = re.compile(r"(-?\d+) (?:to|-) (-?\d+)")
LOW_TAIL = re.compile(r"(-?\d+) or (?:below|lower)")
HIGH_TAIL = re.compile(r"(-?\d+) or (?:above|higher)")


@dataclass(frozen=True)
class MarketDay:
    slug: str
    day: date
    station: str
    station_name: str
    lat: float
    lon: float
    timezone: str
    standard_offset: int


REPLAY_DAY = date(2026, 11, 5)
EVENTS = {
    "nyc": MarketDay(
        f"temp-nychigh-{REPLAY_DAY}", REPLAY_DAY, "KNYC", "Central Park",
        40.779, -73.9692, "America/New_York", -5,
    ),
    "miami": MarketDay(
        f"temp-miahigh-{REPLAY_DAY}", REPLAY_DAY, "KMIA", "Miami International Airport",
        25.788, -80.3169, "America/New_York", -5,
    ),
    "la": MarketDay(
        f"temp-laxhigh-{REPLAY_DAY}", REPLAY_DAY, "KLAX", "Los Angeles International Airport",
        33.9382, -118.3865, "America/Los_Angeles", -8,
    ),
}


def _write(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}-", delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(body)
            handle.flush()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def _fetch(cache_file: Path, url: str, refresh: bool = False) -> bytes:
    if cache_file.exists() and not refresh:
        return cache_file.read_bytes()
    request = Request(url, headers={"User-Agent": "weather-edge local research replay"})
    with urlopen(request, timeout=35) as response:
        body = response.read()
    _write(cache_file, body)
    return body


def bucket_bounds(title: str) -> tuple[int | None, int | None]:
    normalized = " ".join(title.lower().replace("−", "-").split())
    if match := RANGE.fullmatch(normalized):
        lower, upper = map(int, match.groups())
        if lower <= upper:
            return lower, upper
    if match := LOW_TAIL.fullmatch(normalized):
        return None, int(match[1])
    if match := HIGH_TAIL.fullmatch(normalized):
        return int(match[1]), None
    raise ValueError(f"Unsupported daily-high bucket title: {title!r}")


def _check_bucket_partition(buckets: list[dict]) -> None:
    if not buckets or buckets[0]["lower_f"] is not None or buckets[-1]["upper_f"] is not None:
        raise ValueError("Daily-high buckets do not cover both tails")
    for left, right in zip(buckets, buckets[1:]):
        upper, lower = left["upper_f"], right["lower_f"]
        if upper is None or lower is None or upper + 1 != lower:
            raise ValueError("Daily-high buckets overlap or leave a gap")


def _price(value: str | float | int | None) -> float | None:
    if value in (None, ""):
        return None
    price = float(value)
    if not 0 <= price <= 1:
        raise ValueError(f"Price outside [0, 1]: {value!r}")
    return price


def price_history_url(symbol: str, starts: datetime, ends: datetime) -> str:
    return f"{POLYMARKET}/v1/price-history?" + urlencode({
        "symbol": symbol,
        "timestamp.startTimestamp": int(starts.timestamp()),
        "timestamp.endTimestamp": int(ends.timestamp()),
        "fidelity": 1,
    })


def parse_prices(payload: dict, starts: datetime, ends: datetime) -> list[dict]:
    """Keep provider display timestamps and wait one minute for subminute ambiguity."""
    result = []
    for item in payload["history"]:
        at = datetime.fromtimestamp(int(item["timestamp"]), timezone.utc)
        if starts <= at <= ends:
            result.append({
                "at": iso(at),
                "available_at": iso(at + DISPLAY_SAMPLE_DELAY),
                "yes": _price(item.get("longPrice")),
                "no": _price(item.get("shortPrice")),
            })
    return sorted(result, key=lambda item: item["at"])


def _asos_url(
    event: MarketDay, starts: datetime, ends: datetime, fields: tuple[str, ...] = ("tmpf", "metar"),
) -> str:
    begin = starts.date()
    stop = ends.date() + timedelta(days=1)
    return f"{IEM}/cgi-bin/request/asos.py?" + urlencode({
        "station": event.station.removeprefix("K"),
        "data": list(fields),
        "year1": begin.year,
        "month1": begin.month,
        "day1": begin.day,
        "year2": stop.year,
        "month2": stop.month,
        "day2": stop.day,
        "tz": "Etc/UTC",
        "format": "onlycomma",
        "latlon": "no",
        "elev": "no",
        "missing": "empty",
        "trace": "empty",
        "direct": "no",
        "report_type": [1, 2, 3, 4],
    }, doseq=True)


def _report_key(raw: str) -> str:
    parts = raw.rstrip().removesuffix("=").split()
    if parts and parts[0] in {"METAR", "SPECI"}:
        parts = parts[1:]
    return " ".join(parts)


def _decode_madis_hour(compressed, stations, url):
    from scipy.io import netcdf_file

    with netcdf_file(io.BytesIO(gzip.decompress(compressed)), "r", mmap=False) as dataset:
        fields = dataset.variables
        code_width = fields["stationName"].shape[1]
        station_codes = fields["stationName"].data.view(f"S{code_width}").ravel()
        observation_times, receipt_times = fields["timeObs"], fields["timeReceived"]
        results = {}
        for station in stations:
            receipts = results[station] = {}
            positions = (station_codes == station.encode()).nonzero()[0]
            for index in positions:
                report = fields["rawMETAR"].data[index].tobytes().strip(b"\x00 ").decode("ascii")
                report_at = float(observation_times.data[index])
                receipt_at = float(receipt_times.data[index])
                if (
                    not report or not 0 < report_at <= receipt_at
                    or report_at == observation_times._FillValue or receipt_at == receipt_times._FillValue
                ):
                    continue
                key = (iso(datetime.fromtimestamp(report_at, timezone.utc)), _report_key(report))
                timing = (iso(datetime.fromtimestamp(receipt_at, timezone.utc)), url)
                receipts[key] = min(receipts.get(key, timing), timing)
    return results


def _madis_hour(hour: datetime, station: str, cache_dir: Path) -> dict[tuple[str, str], tuple[str, str]]:
    filename = hour.strftime("%Y%m%d_%H00.gz")
    url = f"{MADIS}/{hour:%Y/%m/%d}/point/metar/netcdf/{filename}"
    try:
        compressed = _fetch(cache_dir / filename, url)
    except HTTPError as error:
        if error.code == 404:
            return {}
        raise
    return _decode_madis_hour(compressed, (station,), url)[station]


def _observation_receipts(
    station: str, starts: datetime, ends: datetime, cache_dir: Path,
    *, on_error=None,
) -> dict[tuple[str, str], tuple[str, str]]:
    # Each nominal-hour file covers :45 of the prior hour through :44 of that hour.
    hours = [
        starts + timedelta(hours=offset)
        for offset in range((ends - starts) // timedelta(hours=1) + 1)
    ]
    receipts = {}
    with ThreadPoolExecutor(max_workers=4) as workers:
        futures = [workers.submit(_madis_hour, hour, station, cache_dir) for hour in hours]
        for hour, future in zip(hours, futures, strict=True):
            try:
                batch = future.result()
            except (OSError, ValueError, KeyError, TypeError, AttributeError, EOFError) as exc:
                if on_error is None:
                    raise
                on_error(f"{iso(hour)}: {exc}")
                continue
            for key, receipt in batch.items():
                receipts[key] = min(receipts.get(key, receipt), receipt)
    return receipts


def parse_observations(
    text: str, station: str, starts: datetime, ends: datetime,
    receipts: dict[tuple[str, str], tuple[str, str]],
    *, delay_proxy: timedelta | None = None,
) -> tuple[list[dict], int]:
    """Join ASOS values to actual NOAA MADIS receipt times for matching METARs."""
    observations = {}
    unmatched = 0
    rows = csv.DictReader(io.StringIO(text))
    if not {"station", "valid", "tmpf", "metar"} <= set(rows.fieldnames or ()):
        raise ValueError("IEM response is missing ASOS observation columns")
    for row in rows:
        if row["station"] not in (station, station.removeprefix("K")):
            raise ValueError(f"Unexpected observation station {row['station']!r}")
        at = datetime.strptime(row["valid"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        if not starts <= at < ends or not row["tmpf"]:
            continue
        temperature = float(row["tmpf"])
        if not math.isfinite(temperature):
            continue
        raw = row["metar"]
        at_iso = iso(at)
        timing = receipts.get((at_iso, _report_key(raw)))
        if timing is None:
            unmatched += 1
            if delay_proxy is None:
                continue
        point = {
            "at": at_iso,
            "available_at": timing[0] if timing else iso(at + delay_proxy),
            "temp_f": temperature,
            "source": "IEM ASOS / NOAA MADIS" if timing else "IEM ASOS",
            "source_kind": "station_observation",
            "raw": raw,
        }
        if timing:
            point["receipt_source"] = timing[1]
        else:
            point["timing_proxy"] = "fixed lag after observation time"
        for column, label in (("dwpf", "dewpoint_f"), ("drct", "wind_direction_deg"),
                              ("sknt", "wind_knots"), ("p01i", "precip_1h_in")):
            try:
                numeric = float(row.get(column) or "nan")
            except ValueError:
                continue
            if math.isfinite(numeric):
                point[label] = numeric
        observations[(at, point["available_at"], raw)] = point
    return [observations[key] for key in sorted(observations)], unmatched


def _cli(event: MarketDay, raw: Path, refresh: bool) -> dict:
    url = f"{IEM}/json/cli.py?" + urlencode({"station": event.station, "year": event.day.year})
    payload = json.loads(_fetch(raw / "cli.json", url, refresh))
    report = next((row for row in payload["results"] if row["valid"] == str(event.day)), None)
    if report is None or report["station"] != event.station:
        raise ValueError(f"No climate report for {event.station} on {event.day}")
    product = report["product"]
    if not re.fullmatch(r"\d{12}-K[A-Z]{3}-CDUS\d{2}-CLI[A-Z]{3}(?:-[A-Z]{3})?", product):
        raise ValueError(f"Unexpected climate report identity {product!r}")
    source = f"{IEM}/api/1/nwstext/{product}"
    original = _fetch(raw / "cli-original.txt", source, refresh).decode()
    high = re.search(r"^\s+MAXIMUM\s+(-?\d+)\s+", original, re.MULTILINE)
    if high is None or int(high[1]) != report["high"] or "YESTERDAY" not in original:
        raise ValueError(f"Official finalized report does not match IEM summary at {source}")
    published_at = datetime.strptime(product[:12], "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
    return {
        "available_at": iso(published_at),
        "high_f": int(high[1]),
        "high_at_local_standard": report.get("high_time"),
        "source": source,
        "summary_source": url,
        "product": product,
    }


def _matches(high: int, lower: int | None, upper: int | None) -> bool:
    return (lower is None or lower <= high) and (upper is None or high <= upper)


def _bucket(
    market: dict, event: MarketDay, raw: Path, starts: datetime, ends: datetime, high: int, refresh: bool,
) -> dict:
    symbol = market["slug"]
    if not re.fullmatch(rf"tc-{re.escape(event.slug)}-[a-z0-9-]+", symbol):
        raise ValueError(f"Unexpected Polymarket market slug {symbol!r}")
    terms = market["description"]
    if (
        event.station not in terms or "highest temperature" not in terms.lower()
        or "Climatological Report (Daily)" not in terms
    ):
        raise ValueError(f"Unsupported settlement terms for {symbol}")
    if market["status"] != "MARKET_STATUS_RESOLVED":
        raise ValueError(f"Polymarket market has yet to resolve: {symbol}")

    title = market["title"]
    lower, upper = bucket_bounds(title)
    sides = {side["long"]: side for side in market["marketSides"] if isinstance(side.get("long"), bool)}
    if set(sides) != {True, False} or sides[True]["description"] != "Yes" or sides[False]["description"] != "No":
        raise ValueError(f"Unsupported Polymarket side configuration: {symbol}")

    settlement_url = f"{POLYMARKET}/v1/markets/{symbol}/settlement"
    payout = json.loads(_fetch(raw / f"{symbol}-settlement.json", settlement_url, refresh))
    if payout["slug"] != symbol or payout["settlement"] not in (0, 1):
        raise ValueError(f"Missing final binary settlement for {symbol}")
    yes_wins = payout["settlement"] == 1
    if yes_wins != _matches(high, lower, upper):
        raise ValueError(f"Official high and exchange settlement disagree for {symbol}")

    url = price_history_url(symbol, starts, ends)
    prices = parse_prices(json.loads(_fetch(raw / f"{symbol}-prices.json", url, refresh)), starts, ends)
    return {
        "id": symbol,
        "market_id": str(market["id"]),
        "yes_side_id": str(sides[True]["id"]),
        "no_side_id": str(sides[False]["id"]),
        "label": title,
        "lower_f": lower,
        "upper_f": upper,
        "result": yes_wins,
        "description": terms,
        "prices": prices,
        "price_source": url,
        "settlement_source": settlement_url,
    }


def _closing_time(bucket: dict, raw: Path, refresh: bool) -> str | None:
    symbol = bucket["id"]
    url = f"{POLYMARKET}/v1/markets/{symbol}/book"
    book = json.loads(_fetch(raw / f"{symbol}-closed-book.json", url, refresh))["marketData"]
    if book["state"] != "MARKET_STATE_CLOSED":
        return None
    closed = book.get("stats", {}).get("closeSetTime")
    return iso(utc(closed)) if closed else None


def _forecasts(event: MarketDay, cache_dir: Path, starts: datetime, ends: datetime, *, on_error=None) -> list[dict]:
    first_day = starts.date() - timedelta(days=1)
    cycles = [(first_day, 18)]
    for offset in range((ends.date() - starts.date()).days + 1):
        day = starts.date() + timedelta(days=offset)
        for cycle in (0, 6, 12, 18):
            if datetime(day.year, day.month, day.day, cycle, tzinfo=timezone.utc) < ends:
                cycles.append((day, cycle))

    def collect(day_cycle: tuple[date, int]) -> dict | None:
        day, cycle = day_cycle
        cards = load_nbm_cycle(cache_dir, station=event.station, day=day, cycle=cycle)
        return forecast_for_day(cards, event.day, event.standard_offset)

    with ThreadPoolExecutor(max_workers=4) as workers:
        values = []
        futures = [workers.submit(collect, cycle) for cycle in cycles]
        for (day, cycle), future in zip(cycles, futures, strict=True):
            try:
                values.append(future.result())
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
                if on_error is None:
                    raise
                on_error(f"{day} {cycle:02d}Z: {exc}")
    return sorted(
        (value for value in values if value and utc(value["available_at"]) < ends),
        key=lambda value: value["available_at"],
    )


def _report_evidence(
    events: list[dict], cache_dir: Path, refresh: bool,
    *, business_day_offset: int = 1, on_missing=None,
) -> None:
    manifest_url = f"{MARKET_REPORTS}/manifest.json"
    raw = cache_dir / "replay-raw"
    manifest = json.loads(_fetch(raw / "report-manifest.json", manifest_url, refresh))
    reports = {item["filename"]: item for item in manifest["files"]}
    events_by_filename: dict[str, list[dict]] = {}
    for event in events:
        business_day = date.fromisoformat(event["date"]) + timedelta(days=business_day_offset)
        filename = f"{business_day:%Y%m%d}-daily-market-report.csv"
        events_by_filename.setdefault(filename, []).append(event)

    if not refresh and events_by_filename.keys() - reports.keys():
        manifest = json.loads(_fetch(raw / "report-manifest.json", manifest_url, refresh=True))
        reports = {item["filename"]: item for item in manifest["files"]}

    for filename, daily_events in events_by_filename.items():
        url = f"{MARKET_REPORTS}/{filename}"
        report = reports.get(filename)
        if report is None:
            if on_missing is None:
                raise ValueError(f"No publication in the manifest for {url}")
            for event in daily_events:
                on_missing(event, f"No publication in the manifest for {url}")
            continue
        targets = {event["settlement"]["winning_bucket"] for event in daily_events}
        cache_key = "-".join(event["station"] for event in daily_events)
        cache_file = raw / f"{filename}-{cache_key}.json"
        if cache_file.exists() and not refresh:
            rows = json.loads(cache_file.read_text())
        else:
            request = Request(url, headers={"User-Agent": "weather-edge local research replay"})
            with urlopen(request, timeout=35) as response, io.TextIOWrapper(response, encoding="utf-8-sig") as data:
                rows = {}
                for row in csv.DictReader(data):
                    if row["Symbol"] in targets and _price(row["Settlement Price"]) == 1:
                        rows[row["Symbol"]] = row["Report ID"]
                        if targets <= rows.keys():
                            break
            _write(cache_file, (json.dumps(rows) + "\n").encode())
        for event in daily_events:
            settlement = event["settlement"]
            symbol = settlement["winning_bucket"]
            posted = utc(report["lastModified"])
            if symbol not in rows or posted < utc(settlement["available_at"]):
                reason = f"Missing confirmed final payout report for {symbol} at {url}"
                if on_missing is None:
                    raise ValueError(reason)
                on_missing(event, reason)
                continue
            settlement["payout_available_at"] = iso(posted)
            settlement["payout_report_source"] = url
            settlement["payout_report_row"] = rows[symbol]


def build_event(event: MarketDay, cache_dir: Path, refresh: bool = False) -> dict:
    raw = cache_dir / "replay-raw" / event.slug
    starts, ends = climate_window(event.day, event.standard_offset)
    event_url = f"{POLYMARKET}/v1/events/slug/{event.slug}"
    metadata = json.loads(_fetch(raw / "event.json", event_url, refresh))["event"]
    if metadata["slug"] != event.slug or not metadata["closed"] or utc(metadata["startDate"]) != starts:
        raise ValueError(f"Expected the settled Polymarket event {event.slug}")
    settlement = _cli(event, raw, refresh)
    market_end = min(utc(market["endDate"]) for market in metadata["markets"])
    published = utc(settlement["available_at"])
    if not ends <= published <= market_end:
        raise ValueError("Finalized climate report was issued outside the expected settlement window")

    markets = sorted(metadata["markets"], key=lambda market: market["sortOrder"])
    buckets = [_bucket(market, event, raw, starts, ends, settlement["high_f"], refresh) for market in markets]
    _check_bucket_partition(buckets)
    winners = [bucket for bucket in buckets if bucket["result"]]
    if len(winners) != 1:
        raise ValueError(f"Expected one winning temperature bucket for {event.slug}")

    asos_url = _asos_url(event, starts, ends)
    asos = _fetch(raw / "asos.csv", asos_url, refresh).decode("utf-8")
    receipts = _observation_receipts(event.station, starts, ends, cache_dir / "madis-metar")
    observations, unmatched = parse_observations(asos, event.station, starts, ends, receipts)
    forecasts = _forecasts(event, cache_dir / "nbm-cards", starts, ends)
    if not observations or not forecasts or not any(bucket["prices"] for bucket in buckets):
        raise ValueError(f"Replay lacks observations, forecasts, or display prices for {event.slug}")

    scheduled = datetime.combine(
        event.day + timedelta(days=1), time(8), ZoneInfo("America/New_York")
    )
    closed = _closing_time(winners[0], raw, refresh)
    return {
        "id": event.slug,
        "series": metadata.get("seriesSlug"),
        "title": metadata["title"],
        "station": event.station,
        "station_name": event.station_name,
        "lat": event.lat,
        "lon": event.lon,
        "timezone": event.timezone,
        "standard_utc_offset_hours": event.standard_offset,
        "date": str(event.day),
        "starts_at": iso(starts),
        "ends_at": iso(ends),
        "market_end_at": iso(market_end),
        "trading_closed_at": closed,
        "settles_at": iso(scheduled),
        "settlement_time_kind": "published schedule; exact historical finalization time unavailable",
        "market_url": f"https://polymarket.us/event/{event.slug}",
        "market_source": event_url,
        "rules_source": RULES,
        "weather_faq_source": WEATHER_FAQ,
        "price_docs_source": PRICE_DOCS,
        "observation_source": asos_url,
        "observation_receipt_docs": MADIS_DOCS,
        "observations_missing_receipt": unmatched,
        "buckets": buckets,
        "observations": observations,
        "forecasts": forecasts,
        "settlement": {
            **settlement,
            "winning_bucket": winners[0]["id"],
            "yes_payout": 1,
            "no_payout": 0,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path(".data"))
    parser.add_argument("--series", nargs="+", choices=EVENTS, default=list(EVENTS))
    parser.add_argument("--refresh", action="store_true", help="Refresh archived exchange and IEM records")
    args = parser.parse_args()
    events = [build_event(EVENTS[name], args.data_dir, args.refresh) for name in dict.fromkeys(args.series)]
    _report_evidence(events, args.data_dir, args.refresh)
    path = args.data_dir / "replay.json"
    _write(path, (json.dumps({"events": events}, separators=(",", ":")) + "\n").encode())
    for event in events:
        count = sum(len(bucket["prices"]) for bucket in event["buckets"])
        print(f"{event['station']} {event['date']}: {len(event['buckets'])} buckets, {count} display price samples, "
              f"{len(event['observations'])} readings, {len(event['forecasts'])} forecast issues, "
              f"final daily high {event['settlement']['high_f']}°F")
    print(path)


if __name__ == "__main__":
    main()
