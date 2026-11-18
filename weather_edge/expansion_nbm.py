"""Local as-issued NOAA station cards for temperature and precipitation research."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .history import _write
from .nbm import _columns, _station_fields, iso, nbm_url, utc

# Range hints sampled from NOAA objects before and after the fall version change.
# A missing or truncated station card is retried against the full object.
RANGES = {
    "KNYC": (17720000, 18080000),
    "KMDW": (16700000, 17080000),
    "KPHL": (18450000, 18880000),
    "KBOS": (11660000, 12050000),
    "KDEN": (12800000, 13240000),
    "KSEA": (19550000, 20000000),
    "KATL": (11200000, 11700000),
    "KDFW": (12800000, 13300000),
    "KAUS": (11200000, 11700000),
    "KDCA": (12750000, 13210000),
    "KSFO": (19600000, 20150000),
    "KORD": (18050000, 18500000),
    "KMIA": (16850000, 17300000),
    "KLAX": (15920000, 16350000),
    "KEWR": (13550000, 13950000),
    "KLAS": (15880000, 16310000),
    "KMSY": (17150000, 17600000),
    "KTTN": (20400000, 20810000),
    "KPVD": (18740000, 19150000),
    "KCVG": (12480000, 12930000),
    "KPDX": (18340000, 18740000),
    "KBUF": (11730000, 12130000),
}
EARLY_LAYOUT_RANGES = {
    "KNYC": (19100000, 19600000), "KMDW": (18100000, 18600000),
    "KATL": (12430000, 13030000), "KDCA": (13970000, 14570000),
    "KLAS": (17190000, 17790000), "KMSY": (18490000, 19090000),
    "KAUS": (12450000, 13050000), "KDFW": (14030000, 14630000),
    "KSEA": (20900000, 21500000),
}
NBM_V5_START = date(2026, 5, 5)

# NBS fields used in different experiments. Values retain the station card units.
FIELDS = ("TXN", "XND", "TMP", "TSD", "DPT", "SKY", "WDR", "WSP", "P06", "P12", "Q06", "Q12")


def _station_text(body: bytes, station: str, product: str = "NBS") -> tuple[bytes, int]:
    code = re.escape(product.encode("ascii"))
    header = re.search(rb"^ *" + re.escape(station.encode("ascii")) + rb" +NBM[^\r\n]+\b" + code + rb"\s+GUIDANCE[^\r\n]+\r?$",
                       body, re.MULTILINE)
    if not header:
        raise ValueError(f"No {station} {product} station header")
    # The following station header marks the boundary. A partial trailing card
    # must not be saved under the same identity as a complete card.
    tail = re.search(rb"^ *[A-Z0-9]{4} +NBM[^\r\n]+\b" + code + rb"\s+GUIDANCE", body[header.end():], re.MULTILINE)
    if not tail:
        raise ValueError(f"No complete {station} {product} station card")
    return body[header.start(): header.end() + tail.start()].rstrip() + b"\n", header.start()


def parse_nbs_evidence(body: bytes, station: str) -> dict:
    station_text, offset = _station_text(body, station)
    issued_at, fields = _station_fields(station_text, station, "nbs")
    leads = [int(lead) for lead in fields["FHR"].split()]
    if not leads or leads != sorted(set(leads)) or "TXN" not in fields:
        raise ValueError(f"Invalid NBS lead or temperature data for {station}")
    columns = {key: _columns(fields, key, len(leads)) for key in FIELDS}
    points = []
    for index, lead in enumerate(leads):
        valid_at = issued_at + timedelta(hours=lead)
        values = {key: items[index] for key, items in columns.items() if items[index] is not None}
        points.append({"valid_at": iso(valid_at), "lead_hours": lead, **values})
    return {"station": station, "issued_at": iso(issued_at), "points": points,
            "card_text": station_text.decode("ascii"), "card_sha256": hashlib.sha256(station_text).hexdigest(),
            "offset_in_response": offset}


def fetch_station_card(source, station, issue, parser, byte_range, source_product, *, get=urlopen):
    ranges = (f"bytes={byte_range[0]}-{byte_range[1]}", None) if byte_range else (None,)
    for hint in ranges:
        headers = {"User-Agent": "Weather Edge public weather research", "Accept-Encoding": "identity"}
        if hint:
            headers["Range"] = hint
        try:
            with get(Request(source, headers=headers), timeout=25) as response:
                body = response.read()
                last_modified = response.headers.get("Last-Modified")
                content_range = response.headers.get("Content-Range")
                received_at = datetime.now(timezone.utc)
                etag = response.headers.get("ETag")
        except HTTPError as error:
            if error.code == 416 and hint:
                continue
            raise
        try:
            card = parser(body, station)
        except (ValueError, KeyError):
            if hint:
                continue
            raise
        if utc(card["issued_at"]) != issue:
            raise ValueError(f"NOAA card issue mismatch at {source}")
        if not last_modified:
            raise ValueError(f"NOAA object lacks a historical publication clock at {source}")
        available_at = max(utc(parsedate_to_datetime(last_modified)), issue)
        return {**card, "source": source, "source_product": source_product,
                "available_at": iso(available_at), "received_at": iso(received_at),
                "source_last_modified": last_modified, "content_range": content_range, "etag": etag}
    raise AssertionError("Missing NOAA request variant")


def _fetch_card(station, issue, *, get=urlopen):
    source = nbm_url(issue.date(), issue.hour, "nbs")
    byte_range = RANGES.get(station)
    if issue.date() < NBM_V5_START:
        byte_range = EARLY_LAYOUT_RANGES.get(station, byte_range)
    return fetch_station_card(source, station, issue, parse_nbs_evidence, byte_range, "NOAA NBM NBS station bulletin", get=get)


def card_path(data, station, issue):
    return Path(data) / "expansion" / "nbm" / station / f"{issue:%Y%m%d-%H}-nbs.json"


def load_card(data, station, issue, *, fetch=False, get=urlopen):
    path = card_path(data, station, issue)
    if path.is_file():
        return json.loads(path.read_text())
    if not fetch:
        return None
    card = _fetch_card(station, issue, get=get)
    _write(path, (json.dumps(card, separators=(",", ":"), allow_nan=False) + "\n").encode())
    return card


def backfill(data, stations, start, end, cycles, *, workers=6, get=urlopen):
    jobs = [
        (station, datetime.combine(start + timedelta(days=offset), time(cycle), timezone.utc))
        for offset in range((end - start).days + 1) for cycle in cycles for station in stations
    ]
    def collect(job):
        station, issue = job
        exists = card_path(data, station, issue).is_file()
        try:
            load_card(data, station, issue, fetch=True, get=get)
        except (OSError, ValueError, KeyError) as error:
            return station, "missing", {"station": station, "issue": iso(issue), "error": str(error)}
        return station, "reused" if exists else "new", None

    counts = {station: {"reused": 0, "new": 0, "missing": 0} for station in stations}
    missing = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for index, (station, status, gap) in enumerate(pool.map(collect, jobs), 1):
            counts[station][status] += 1
            if gap:
                missing.append(gap)
            if index % 100 == 0:
                print(f"NOAA NBS {index}/{len(jobs)} {counts}", flush=True)
    return {"counts": counts, "missing": missing, "cycles_utc": list(cycles),
            "issue_dates_inclusive": [str(start), str(end)]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path(".data"))
    parser.add_argument("--stations", nargs="+", required=True)
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    parser.add_argument("--cycles", nargs="+", type=int, default=(0, 6, 12, 18))
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()
    if args.end < args.start or args.workers < 1 or any(not 0 <= cycle < 24 for cycle in args.cycles):
        parser.error("Invalid date range, worker count or UTC cycle")
    if any(not re.fullmatch(r"[A-Z0-9]{4}", station) for station in args.stations):
        parser.error("Station identifiers must be four uppercase ASCII alphanumeric characters")
    report = backfill(args.data_dir, args.stations, args.start, args.end, args.cycles, workers=args.workers)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
