"""Exact-station weather inputs shared by the local expansion studies."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
from datetime import date, datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from urllib.parse import urlencode

from .archive import CLI_DATE, CLI_ID, MONTHS
from .history import IEM, _fetch
from .market import STATIONS as LEGACY_STATIONS
from .model_data import IEM_CLI, asos_url, fetch_text
from .nbm import climate_window, iso, utc

# Location metadata is for display; the actual contract text verifies the station.
STATIONS = {
    **LEGACY_STATIONS,
    "KATL": (33.630, -84.442, "America/New_York", -5),
    "KBOS": (42.364, -71.005, "America/New_York", -5),
    "KDEN": (39.847, -104.656, "America/Denver", -7),
    "KSEA": (47.444, -122.314, "America/Los_Angeles", -8),
    "KDFW": (32.900, -97.040, "America/Chicago", -6),
    "KORD": (41.960, -87.930, "America/Chicago", -6),
    "KAUS": (30.184, -97.680, "America/Chicago", -6),
    "KDCA": (38.852, -77.034, "America/New_York", -5),
    "KEWR": (40.693, -74.169, "America/New_York", -5),
    "KLAS": (36.072, -115.163, "America/Los_Angeles", -8),
    "KMSY": (29.993, -90.258, "America/Chicago", -6),
    "KTTN": (40.277, -74.814, "America/New_York", -5),
    "KPVD": (41.722, -71.433, "America/New_York", -5),
    "KCVG": (39.044, -84.673, "America/New_York", -5),
    "KPDX": (45.59578, -122.60917, "America/Los_Angeles", -8),
    "KBUF": (42.93998, -78.73604, "America/New_York", -5),
}
CLI_OFFICE = {
    "KNYC": ("KOKX", "CLINYC"), "KMDW": ("KLOT", "CLIMDW"), "KPHL": ("KPHI", "CLIPHL"),
    "KBOS": ("KBOX", "CLIBOS"), "KDEN": ("KBOU", "CLIDEN"), "KSEA": ("KSEW", "CLISEA"),
    "KATL": ("KFFC", "CLIATL"), "KDFW": ("KFWD", "CLIDFW"), "KORD": ("KLOT", "CLIORD"),
    "KAUS": ("KEWX", "CLIAUS"), "KDCA": ("KLWX", "CLIDCA"), "KSFO": ("KMTR", "CLISFO"),
    "KLAX": ("KLOX", "CLILAX"), "KMIA": ("KMFL", "CLIMIA"),
    "KEWR": ("KOKX", "CLIEWR"), "KLAS": ("KVEF", "CLILAS"), "KMSY": ("KLIX", "CLIMSY"),
    "KTTN": ("KPHI", "CLITTN"),
    "KPVD": ("KBOX", "CLIPVD"), "KCVG": ("KILN", "CLICVG"),
    "KPDX": ("KPQR", "CLIPDX"), "KBUF": ("KBUF", "CLIBUF"),
}
TEMPERATURE = re.compile(
    r"^[ \t]*(MAXIMUM|MINIMUM)[ \t]+([+-]?\d+|MM?)(?:[ \t]?R)?(?=[ \t]|$)"
    r"(?:[ \t]+(\d{1,2}:?\d{2}[ \t]+[AP]M))?", re.M,
)
TODAY_THROUGH_CLOSE = re.compile(r"VALID TODAY (?:AS OF|THROUGH)\s+11:?59 PM(?: LOCAL STANDARD TIME| LST)?\b")


def _reported_amount(body, heading, period):
    match = re.search(rf"^{heading} \(IN\)[ \t]*\n[ \t]*{period}[ \t]+(\d+(?:\.\d+)?|T|MM?)(?:[ \t]?R)?(?=[ \t]|$)",
                      body, re.M)
    return match[1] if match else None


def parse_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def cli_url(station, year):
    return f"{IEM_CLI}?{urlencode({'station': station, 'year': year})}"


def cli_file(data, station, year):
    return Path(data) / "expansion" / "cli" / station / f"{year}.json"


def _valid_product(product, station):
    if not isinstance(product, str) or not CLI_ID.fullmatch(product):
        return False
    parts = product.split("-")
    return (parts[1], parts[3]) == CLI_OFFICE[station]


def daily_labels(data, station, start, end):
    """Get complete NWS climate labels from the IEM summary transport.

    Annual summaries may reflect a late correction. They support weather
    scoring; individual contract replays verify the source text and payout.
    """
    rows = {}
    for year in range(start.year, end.year + 1):
        path = cli_file(data, station, year)
        payload = json.loads(fetch_text(cli_url(station, year), path))
        offset = STATIONS[station][3]
        for row in payload["results"]:
            day = date.fromisoformat(row["valid"])
            product = row.get("product")
            if row.get("station") != station or not start <= day <= end or not _valid_product(product, station):
                continue
            published = datetime.strptime(product[:12], "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
            _, end_at = climate_window(day, offset)
            if published < end_at:
                continue
            rain = row.get("precip")
            rows[str(day)] = {
                "station": station, "date": str(day), "product": product,
                "published_at": iso(published), "source": f"{IEM}/api/1/nwstext/{product}",
                "high_f": parse_number(row.get("high")), "low_f": parse_number(row.get("low")),
                "low_at_local_standard": row.get("low_time"), "precip_as_reported": rain,
                "snow_as_reported": row.get("snow"), "high_normal_f": parse_number(row.get("high_normal")),
                "low_normal_f": parse_number(row.get("low_normal")),
            }
    return rows


def raw_report(data, station, day, product):
    """Verify station product identity, specified climate date, and complete day."""
    if not _valid_product(product, station):
        raise ValueError(f"Unexpected climate report {product} for {station}")
    path = Path(data) / "expansion" / "cli" / station / f"{product}.txt"
    url = f"{IEM}/api/1/nwstext/{product}"
    return decode_raw_report(_fetch(path, url), station, day, product)


def decode_raw_report(raw, station, day, product):
    if not _valid_product(product, station):
        raise ValueError(f"Unexpected climate report {product} for {station}")
    body = raw.decode().replace("\r\n", "\n")
    period = "YESTERDAY" if re.search(r"^\s*YESTERDAY\b", body, re.M) else "TODAY"
    if period == "TODAY" and not TODAY_THROUGH_CLOSE.search(body):
        return None
    named_day = CLI_DATE.search(body)
    pil = CLI_OFFICE[station][1]
    if not named_day or re.search(rf"^{pil}\s*$", body, re.M) is None:
        return None
    month, day_number, year = named_day.groups()
    published = datetime.strptime(product[:12], "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
    _, ends = climate_window(day, STATIONS[station][3])
    if date(int(year), MONTHS[month], int(day_number)) != day or published < ends:
        return None
    extremes = {name: (parse_number(value), clock or None) for name, value, clock in TEMPERATURE.findall(body)}
    high, high_at = extremes.get("MAXIMUM", (None, None))
    low, low_at = extremes.get("MINIMUM", (None, None))
    rain = _reported_amount(body, "PRECIPITATION", period)
    snow = _reported_amount(body, "SNOWFALL", period)
    if high is None and low is None and rain is None and snow is None:
        return None
    return {"station": station, "date": str(day), "published_at": iso(published), "available_at": iso(published),
            "source": f"{IEM}/api/1/nwstext/{product}", "product": product, "high_f": int(high) if high is not None else None,
            "low_f": int(low) if low is not None else None, "high_at_local_standard": high_at,
            "low_at_local_standard": low_at, "precip_as_reported": rain, "precip_in": rain if rain == "T" else parse_number(rain),
            "snow_in": snow if snow == "T" else parse_number(snow),
            "sha256": hashlib.sha256(raw).hexdigest()}


def reports_for_day(data, station, day, known_product=None, *, following_days=1, refresh_listing=False):
    """List text revisions on specific issue dates following the climate day."""
    _, end_at = climate_window(day, STATIONS[station][3])
    cccc, pil = CLI_OFFICE[station]
    path = Path(data) / "expansion" / "cli" / station
    products = {known_product} if known_product else set()
    now = datetime.now(timezone.utc)
    for offset in range(following_days):
        issue_day = end_at.date() + timedelta(days=offset)
        if issue_day > now.date():
            break
        query = urlencode({"cccc": cccc, "pil": pil, "date": issue_day})
        title = f"afos-{issue_day}"
        if refresh_listing:
            title += f"-received-{now:%Y%m%dT%H%M%SZ}"
        payload = json.loads(_fetch(path / f"{title}.json", f"{IEM}/api/1/nws/afos/list.json?{query}"))
        products.update(row["product_id"] for row in payload["data"]
                        if row["cccc"] == cccc and row["pil"] == pil)
    return [report for product in sorted(products)
            if (report := raw_report(data, station, day, product))]


def observation_file(data, station, start, end):
    return Path(data) / "expansion" / "asos" / f"{station}-{start}-{end}.csv"


def observation_url(station, start, end):
    return asos_url(start, end, station).replace("data=tmpf", "data=tmpf&data=dwpf&data=drct&data=sknt&data=p01i&data=metar", 1)


def decode_observations(content, station, url):
    obs = []
    for row in csv.DictReader(StringIO(content)):
        if row.get("station") != station[1:]:
            continue
        temperature = parse_number(row.get("tmpf"))
        if temperature is None:
            continue
        stamp = datetime.fromisoformat(row["valid"]).replace(tzinfo=timezone.utc)
        obs.append({
            "at": iso(stamp), "available_at": iso(stamp + timedelta(minutes=10)), "temp_f": temperature,
            "dewpoint_f": parse_number(row.get("dwpf")), "wind_direction_deg": parse_number(row.get("drct")),
            "wind_speed_knots": parse_number(row.get("sknt")),
            "hour_precip_in": parse_number(row.get("p01i")), "raw_report": row.get("metar"),
            "source": f"IEM {station} ASOS", "source_url": url, "availability": "measurement time + 10 minutes",
        })
    if not obs:
        raise ValueError(f"No ASOS archive observations for {station}")
    return sorted(obs, key=lambda row: row["at"])


def observed_data(data, station, start, end):
    """Load exact-station ASOS archive with measured time and assumed receipt lag."""
    url = observation_url(station, start, end)
    content = fetch_text(url, observation_file(data, station, start, end))
    return decode_observations(content, station, url)


def observations_as_of(observations, start, as_of, *, delay_minutes=10):
    by_time = {}
    delay = timedelta(minutes=delay_minutes) if delay_minutes is not None else None
    for obs in observations:
        stamp = utc(obs["at"])
        if delay is None and (not obs.get("available_at") or obs.get("timing_proxy")):
            continue
        receipt = stamp + delay if delay is not None else utc(obs["available_at"])
        if start <= stamp <= receipt <= as_of:
            by_time[stamp] = obs
    return list(by_time.values())
