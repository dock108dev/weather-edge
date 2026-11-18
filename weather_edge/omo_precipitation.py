"""Read quality-screened one-hour gauge accumulations from retained MADIS OMO files."""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path

import numpy as np

from . import omo
from .history import _write
from .market import iso, parse_time

DOCS = "https://madis.ncep.noaa.gov/omo_precip.shtml"


def _sample(fields, station, first, last):
    at = fields["observationTime"].data
    received = fields["receivedTime"].data
    modified = fields["modifyTime"].data
    amount = fields["precipAccum"].data
    flags = fields["precipAccumDD"].data
    if fields["precipAccum"].units != b"mm":
        raise ValueError("Unexpected MADIS precipitation units")
    keep = (station & (at >= first) & (at < last) & (at <= received) & (received <= modified) & (modified < last + 3 * 86400)
            & (amount >= 0) & (amount <= 250) & (fields["precipAccumStatus"].data == 0)
            & np.isin(flags, (b"C", b"S", b"V", b"K", b"G")))
    rows = []
    for i in np.flatnonzero(keep):
        mm = float(amount[i])
        cents = round(mm / .254)
        if abs(mm - cents * .254) <= .005:
            rows.append({"at": iso(datetime.fromtimestamp(at[i], timezone.utc)),
                         "madis_received_at": iso(datetime.fromtimestamp(received[i], timezone.utc)),
                         "madis_modified_at": iso(datetime.fromtimestamp(modified[i], timezone.utc)),
                         "amount_mm": mm, "amount_hundredths_in": cents, "quality": flags[i].decode("ascii"),
                         "raw_report": fields["rawMessage"].data[i].tobytes().strip(b"\x00").decode("ascii")})
    return sorted(rows, key=lambda row: (row["at"], row["madis_received_at"]))


def load(data, frame):
    raw = Path(data) / frame["raw"]
    derived = raw.with_name(raw.stem + "-precipitation.json")
    if derived.is_file():
        return json.loads(derived.read_bytes())
    body = raw.read_bytes()
    if sha256(body).hexdigest() != frame["sha256"]:
        raise ValueError("MADIS precipitation raw digest mismatch")
    rows = omo.decode(body, parse_time(frame["hour"]), sampler=_sample)
    _write(derived, json.dumps(rows, separators=(",", ":"), allow_nan=False).encode())
    return rows


def station_trace(data, station, starts, ends, *, original_receipts=False):
    start, close = parse_time(starts), parse_time(ends)
    hour = start.replace(minute=0, second=0, microsecond=0)
    points, seen = [], {}
    while hour < close:
        for frame in omo.editions(data, hour, original_receipts=original_receipts):
            try:
                rows = load(data, frame).get(station, [])
            except (OSError, KeyError, ValueError, TypeError):
                continue
            available = frame["ingested_at"] if original_receipts else frame["available_at"]
            clock = parse_time(available)
            for row in rows:
                time, amount = row["at"], row["amount_hundredths_in"]
                if (not start + 2 * omo.HOUR <= parse_time(time) < close
                        or parse_time(row["madis_modified_at"]) > clock or seen.get(time) == amount):
                    continue
                seen[time] = amount
                points.append({**row, "available_at": available, "source_at": frame["available_at"],
                               "source": frame["source"], "ingested_at": frame["ingested_at"], "sha256": frame["sha256"]})
        hour += omo.HOUR
    return sorted(points, key=lambda p: (p["at"], p["available_at"]))
