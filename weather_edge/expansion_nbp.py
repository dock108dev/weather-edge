"""Archive the NOAA National Blend probabilistic station temperature card."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from . import expansion_nbm
from .history import _write
from .nbm import AWS_NBM, _station_fields, iso

RANGES = {
    "KNYC": (20280000, 20750000), "KMDW": (19160000, 19620000), "KPHL": (21160000, 21650000),
    "KDEN": (14690000, 15150000), "KBOS": (13380000, 13860000), "KORD": (20710000, 21190000),
}
QUANTILES = {"TXNP1": 0.10, "TXNP2": 0.25, "TXNP5": 0.50, "TXNP7": 0.75, "TXNP9": 0.90}
COLUMNS = ("TXNMN", "TXNSD", *QUANTILES)


def parse_card(body, station):
    raw, offset = expansion_nbm._station_text(body, station, "NBP")
    issued, _ = _station_fields(raw, station, "nbp")
    rows = {}
    for line in raw.decode("ascii").splitlines()[1:]:
        key = line[:6].strip()
        if key in ("FHR", *COLUMNS):
            rows[key] = line[6:]
    leads = [(int(match[0]), match.end()) for match in re.finditer(r"\d+", rows["FHR"])]
    points = []
    for lead, right in leads:
        values = {}
        for key in COLUMNS:
            value = rows.get(key, "")[right - 3:right].strip()
            values[key] = int(value) if re.fullmatch(r"-?\d+", value) else None
        quantiles = [values[key] for key in QUANTILES]
        mean = values["TXNMN"]
        spread = values["TXNSD"]
        if (mean is None or spread is None or spread < 0 or any(value is None for value in quantiles)
                or quantiles != sorted(quantiles)):
            continue
        valid = issued + timedelta(hours=lead)
        if valid.hour not in (0, 12):
            continue
        points.append({"valid_at": iso(valid), "lead_hours": lead, "mean_f": mean,
                       "sd_f": spread, "quantiles_f": quantiles,
                       "target": "daily-low" if valid.hour == 12 else "daily-high"})
    if not points:
        raise ValueError(f"No complete NBP temperature distribution for {station}")
    return {"station": station, "issued_at": iso(issued), "points": points, "card_text": raw.decode("ascii"),
            "card_sha256": hashlib.sha256(raw).hexdigest(), "offset_in_response": offset}


def card_path(data, station, issue):
    return Path(data) / "expansion" / "nbp" / station / f"{issue:%Y%m%d-%H}-nbp.json"


def load_card(data, station, issue, *, fetch=False):
    target = card_path(data, station, issue)
    if target.is_file():
        return json.loads(target.read_text())
    if not fetch:
        return None
    source = f"{AWS_NBM}/blend.{issue:%Y%m%d}/{issue:%H}/text/blend_nbptx.t{issue:%H}z"
    card = expansion_nbm.fetch_station_card(source, station, issue, parse_card, RANGES.get(station),
                                             "NOAA NBM NBP probabilistic station bulletin")
    _write(target, (json.dumps(card, separators=(",", ":"), allow_nan=False) + "\n").encode())
    return card


def backfill(data, stations, start, end, *, workers=6):
    def acquire(job):
        station, day = job
        issue = datetime.combine(day, time(13), timezone.utc)
        try:
            card = load_card(data, station, issue, fetch=True)
        except (OSError, KeyError, ValueError) as error:
            return station, day, None, str(error)
        return station, day, len(card["points"]), None

    keys = [(station, start + timedelta(days=i)) for i in range((end - start).days + 1) for station in stations]
    found, gaps = [], []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for station, day, points, why in pool.map(acquire, keys):
            row = {"station": station, "issue_date": str(day)}
            if why:
                gaps.append({**row, "error": why})
            else:
                found.append({**row, "distributions": points})
    return {"success": len(found), "cards": found, "missing": gaps}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path(".data"))
    parser.add_argument("--stations", nargs="+", choices=sorted(RANGES), required=True)
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    args = parser.parse_args()
    if args.start > args.end:
        parser.error("Start date must precede or equal end date")
    print(json.dumps(backfill(args.data_dir, args.stations, args.start, args.end), indent=2))


if __name__ == "__main__":
    main()
