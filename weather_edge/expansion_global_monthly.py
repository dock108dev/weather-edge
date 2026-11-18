"""Preserve NOAA climate-at-a-glance values and dated monthly report evidence."""

from __future__ import annotations

import calendar
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
from html import unescape
import json
from pathlib import Path
import re
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .history import _write
from .nbm import iso, utc

NCEI = "https://www.ncei.noaa.gov"
CAG_ROUTE = "/access/monitoring/climate-at-a-glance/global/time-series/globe/land_ocean/1/0/"
CAG_DOCS = NCEI + "/access/monitoring/climate-at-a-glance/global/time-series"
NOAA_SOURCE = NCEI + "/products/land-based-station/noaa-global-temp"
DATE = re.compile(r'<div\s+datetime="([^"]+)"\s+class="article--date-updated"')
MONTHLY_DEPARTURE = re.compile(r"\(([+-]?\d+(?:\.\d+)?)\s*°\s*C\)\s*(above|below|higher than|lower than)", re.I)


def _get(data, stem, url):
    root = data / "expansion" / "global-monthly"
    index = root / f"{stem}.json"
    if index.is_file():
        receipt = json.loads(index.read_bytes())
        payload = (data / receipt["file"]).read_bytes()
        if sha256(payload).hexdigest() != receipt["sha256"]:
            raise ValueError("NOAA climate capture digest mismatch")
        return payload, receipt
    request = Request(url, headers={"User-Agent": "Weather Edge public NOAA climate research"})
    began = datetime.now(timezone.utc)
    with urlopen(request, timeout=25) as response:
        payload = response.read(2_000_001)
        receipt = {"source": url, "requested_at": iso(began), "received_at": iso(datetime.now(timezone.utc)),
                   "headers": {name: response.headers[name] for name in ("Date", "Last-Modified", "ETag", "Content-Type") if name in response.headers}}
    if not payload or len(payload) > 2_000_000:
        raise ValueError("Unexpected NOAA climate payload size")
    digest = sha256(payload).hexdigest()
    path = root / "raw" / f"{stem}-{digest[:16]}.data"
    if not path.is_file():
        _write(path, payload)
    receipt.update(sha256=digest, file=str(path.relative_to(data)))
    _write(index, json.dumps(receipt, sort_keys=True).encode())
    return payload, receipt


def cag(data, end_year):
    url = f"{NCEI}{CAG_ROUTE}1850-{end_year}/data.json"
    raw, receipt = _get(Path(data), f"cag-monthly-{end_year}", url)
    value = json.loads(raw, parse_float=Decimal)
    description = value["description"]
    if description != {"title": "Global Land and Ocean Average Temperature Departures", "units": "Degrees Celsius", "base_period": "1901-2000"}:
        raise ValueError("NOAA monthly series units or baseline differ from the filed series")
    departures = {}
    for label, row in value["data"].items():
        stamp = datetime.strptime(label, "%Y%m")
        temperature = Decimal(row["departure"])
        if not temperature.is_finite() or not -10 <= temperature <= 10:
            raise ValueError("NOAA monthly departure outside valid range")
        departures[stamp.strftime("%Y-%m")] = str(temperature)
    return departures, {**receipt, "description": description}


def _archived_report(string, year, month):
    dateline = DATE.search(string)
    if not dateline:
        return None
    try:
        date_shown = utc(dateline[1])
    except ValueError:
        return None
    expected = date_shown.month == month % 12 + 1 and date_shown.year == year + (month == 12)
    if not expected:
        return None
    month_name = calendar.month_name[month].lower()
    article = string.split("<article", 1)[-1].split("</article>", 1)[0]
    for paragraph in re.findall(r"<p[^>]*>(.*?)</p>", article, re.S | re.I):
        prose = " ".join(unescape(re.sub(r"<[^>]+>", " ", paragraph)).split())
        first_sentence = re.split(r"(?<=\D)\.\s+", prose, maxsplit=1)[0]
        normalized = first_sentence.lower()
        global_value = ("global surface temperature" in normalized or "globally," in normalized
                        or "for the globe as a whole" in normalized or "surface temperature" in normalized)
        if not global_value or month_name not in normalized or "year-to-date" in normalized:
            continue
        amount = MONTHLY_DEPARTURE.search(first_sentence)
        visible_year = re.search(fr"\b{month_name}\s+(20\d{{2}})\b", normalized)
        if visible_year and int(visible_year[1]) != year:
            continue
        if amount and re.search(r"20th[- ]century\s+(?:average|baseline)", normalized):
            value = Decimal(amount[1]) * (-1 if amount[2].lower() in ("below", "lower than") else 1)
            return {"month": f"{year:04d}-{month:02d}", "displayed_report_at": iso(date_shown),
                    "departure_c": str(value), "original_public_receipt_verified": False}
    return None


def release(data, token):
    year, month = map(int, token.split("-"))
    key = f"{year:04d}{month:02d}"
    article_paths = [f"/news/global-climate-{key}"]
    if month == 12:
        article_paths += [f"/news/global-climate-{year}", f"/news/global-climate-{year}13"]
    for index, path in enumerate(article_paths):
        try:
            raw, receipt = _get(Path(data), f"release-{key}-{index}", NCEI + path)
        except HTTPError as error:
            if error.code == 404:
                continue
            raise
        report = _archived_report(raw.decode("utf-8"), year, month)
        if report:
            return {**report, **receipt}
    return None


def releases(data, months):
    with ThreadPoolExecutor(max_workers=3) as pool:
        samples = list(pool.map(lambda month: release(data, month), months))
    return dict(zip(months, samples, strict=True))
