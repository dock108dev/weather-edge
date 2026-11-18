"""Preserve as-issued hourly NBM cards for the NYC Weather Index station network."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path

from .expansion_nbm import _station_text, fetch_station_card
from .history import _write
from .nbm import nbm_url, parse_nbh

NYC_MEMBERS = ("KLGA", "KEWR", "KTEB", "KCDW", "KHPN", "KFRG", "KSMQ", "KISP")
CENTRAL_PARK = "KNYC"
NYC_AND_MEMBERS = (CENTRAL_PARK, *NYC_MEMBERS)
REFERENCE_OFFSETS = {
    "KNYC": 17384000, "KLGA": 15768000, "KEWR": 13395000, "KTEB": 19869000,
    "KCDW": 11907000, "KHPN": 14670000, "KFRG": 13801000, "KSMQ": 19419000, "KISP": 15164000,
}
CYCLES = (0, 6, 12, 18)


def parse_evidence(body, station):
    text, offset = _station_text(body, station, "NBH")
    return {"station": station, **parse_nbh(text, station), "card_text": text.decode("ascii"),
            "card_sha256": sha256(text).hexdigest(), "offset_in_response": offset}


def path(data, station, issued_at):
    return Path(data) / "expansion" / "nbh" / station / f"{issued_at:%Y%m%d-%H}-nbh.json"


def load(data, station, issued_at, *, fetch=False):
    cached = path(data, station, issued_at)
    if cached.exists():
        return json.loads(cached.read_text())
    if not fetch:
        return None
    offset = REFERENCE_OFFSETS[station]
    source = nbm_url(issued_at.date(), issued_at.hour, "nbh")
    card = fetch_station_card(source, station, issued_at, parse_evidence,
                              (offset - 200000, offset + 200000), "NOAA NBM NBH station bulletin")
    _write(cached, json.dumps(card, separators=(",", ":")).encode())
    return card


def backfill(data, first, last, workers=6):
    jobs = [(station, datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc))
            for index in range((last - first).days + 1)
            for day in (first + timedelta(days=index),)
            for hour in CYCLES for station in NYC_AND_MEMBERS]

    def collect(job):
        station, issued_at = job
        try:
            load(data, station, issued_at, fetch=True)
        except (OSError, ValueError, KeyError) as error:
            return {"station": station, "issued_at": issued_at.isoformat(), "error": str(error)}
        return None

    missing = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for index, error in enumerate(pool.map(collect, jobs), start=1):
            if error:
                missing.append(error)
            if index % 100 == 0:
                print(f"NOAA NBH {index}/{len(jobs)} cards checked", flush=True)
    return {"expected": len(jobs), "available": len(jobs) - len(missing), "missing": missing}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path(".data"))
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()
    if args.end < args.start or args.workers < 1:
        parser.error("Invalid date range or worker count")
    print(json.dumps(backfill(args.data_dir, args.start, args.end, args.workers), indent=2))


if __name__ == "__main__":
    main()
