"""Archive native 6-hour GEFS temperature extrema for Central Standard Time stations."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time, timedelta, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
import json
import math
from pathlib import Path
import re
from urllib.request import Request, urlopen

from .archive import date_range
from .expansion_weather import STATIONS
from .history import _write
from .hrrr import SourcePaused
from .nbm import iso, utc

UTC = timezone.utc
ORIGIN = "https://noaa-gefs-pds.s3.amazonaws.com"
DOCS = "https://www.nco.ncep.noaa.gov/pmb/products/gens/"
CANDIDATES = ("KMDW", "KORD", "KDFW", "KAUS")
LEADS = (12, 18, 24, 30)
MEMBERS = tuple(range(31))
RECORD = re.compile(r":(TMAX|TMIN):2 m above ground:(\d+)-(\d+) hour (max|min) fcst:")
FIELDS = {"TMAX": ("max", 4), "TMIN": ("min", 5)}


def object_url(issue, lead, member):
    name = "gec00" if member == 0 else f"gep{member:02d}"
    return (f"{ORIGIN}/gefs.{issue:%Y%m%d}/{issue:%H}/atmos/pgrb2ap5/"
            f"{name}.t{issue:%H}z.pgrb2a.0p50.f{lead:03d}")


def indexed_extremes(index, lead):
    records = {}
    lines = index.splitlines()
    for line, following in zip(lines, lines[1:]):
        match = RECORD.search(line)
        if not match or (int(match[2]), int(match[3])) != (lead - 6, lead):
            continue
        start, end = int(line.split(":")[1]), int(following.split(":")[1])
        if match[1] in records or not 0 <= start < end:
            raise ValueError("Ambiguous GEFS temperature extrema byte range")
        records[match[1]] = (start, end)
    if set(records) != set(FIELDS):
        raise ValueError("GEFS file does not cover both requested six-hour extrema")
    high, low = records["TMAX"], records["TMIN"]
    if high[1] != low[0] or low[1] - high[0] > 2 * 1024 * 1024:
        raise ValueError("Expected adjacent GEFS temperature extrema in the native inventory")
    return high, low


def _decode(body, field, issue, lead, member):
    from eccodes import (
        CodesInternalError, codes_get, codes_grib_find_nearest, codes_new_from_message, codes_release,
    )

    step, parameter = FIELDS[field]
    valid = issue + timedelta(hours=lead)
    expected = {
        "discipline": 0, "parameterCategory": 0, "parameterNumber": parameter, "units": "K",
        "typeOfLevel": "heightAboveGround", "level": 2, "stepType": step, "stepUnits": 1,
        "startStep": lead - 6, "endStep": lead, "perturbationNumber": member,
        "gridType": "regular_ll", "Ni": 720, "Nj": 361,
        "iDirectionIncrementInDegrees": .5, "jDirectionIncrementInDegrees": .5,
        "dataDate": int(issue.strftime("%Y%m%d")), "dataTime": int(issue.strftime("%H%M")),
        "validityDate": int(valid.strftime("%Y%m%d")), "validityTime": int(valid.strftime("%H%M")),
    }
    handle = None
    try:
        handle = codes_new_from_message(body)
        for key, expected_value in expected.items():
            if codes_get(handle, key) != expected_value:
                raise ValueError(f"GEFS {field} metadata differs at {key}")
        points = {}
        for station in CANDIDATES:
            lat, lon, _, _ = STATIONS[station]
            cell, = codes_grib_find_nearest(handle, lat, lon, is_lsm=False, npoints=1)
            temp = cell["value"]
            if not math.isfinite(temp) or not 150.0 < temp < 350.0:
                raise ValueError(f"GEFS {field} temperature unavailable for {station}")
            points[station] = {
                "grid_lat": cell["lat"],
                "grid_lon": ((cell["lon"] + 180) % 360) - 180,
                "distance_km": cell["distance"],
                "temp_k": temp,
                "temp_f": (temp - 273.15) * 1.8 + 32,
            }
        return points
    except CodesInternalError as error:
        raise ValueError(f"GEFS {field} decoding failed: {error}") from error
    finally:
        if handle is not None:
            codes_release(handle)


def path(data, issue, lead, member):
    return Path(data) / "gefs" / issue.strftime("%Y%m%d%H") / f"f{lead:03d}-m{member:02d}.json"


def _get(data, resource, urlopen_function):
    if (Path(data) / "collector" / "paused").exists():
        raise SourcePaused("Collection stopped")
    return urlopen_function(resource, timeout=20)


def _range(data, source, start, stop, issue, get):
    request = Request(source, headers={"Range": f"bytes={start}-{stop - 1}"})
    with _get(data, request, get) as reply:
        if reply.status != 206 or not reply.headers.get("Content-Range", "").startswith(f"bytes {start}-{stop - 1}/"):
            raise ValueError("GEFS server did not return the requested GRIB2 byte range")
        available = parsedate_to_datetime(reply.headers["Last-Modified"]).astimezone(UTC)
        tag = reply.headers.get("ETag")
        body = reply.read(stop - start + 1)
    ingested = datetime.now(UTC)
    if len(body) != stop - start or not issue <= available <= ingested:
        raise ValueError("GEFS partial response or invalid publication time")
    return body, available, ingested, tag


def collect_frame(data, issue, lead, member, *, get=urlopen):
    output = path(data, issue, lead, member)
    try:
        return json.loads(output.read_bytes())
    except FileNotFoundError:
        pass
    source = object_url(issue, lead, member)
    with _get(data, source + ".idx", get) as reply:
        raw_index = reply.read(512 * 1024).decode("ascii")
    (start, split), (_, stop) = indexed_extremes(raw_index, lead)
    body, available, ingested, tag = _range(data, source, start, stop, issue, get)
    high = _decode(body[:split - start], "TMAX", issue, lead, member)
    low = _decode(body[split - start:], "TMIN", issue, lead, member)
    if any(high[key]["temp_k"] < low[key]["temp_k"] for key in CANDIDATES):
        raise ValueError("GEFS TMAX is below same-cell six-hour TMIN")
    raw = output.with_suffix(".grib2")
    frame = {
        "model": "NOAA GEFS", "grid_resolution_degrees": .5, "native_units": "K", "member": member,
        "issued_at": iso(issue), "available_at": iso(available), "ingested_at": iso(ingested),
        "start_at": iso(issue + timedelta(hours=lead - 6)), "valid_at": iso(issue + timedelta(hours=lead)),
        "source": source, "source_etag": tag, "byte_range": [start, stop - 1], "raw": str(raw.relative_to(data)),
        "sha256": sha256(body).hexdigest(), "high": high, "low": low,
    }
    _write(raw, body)
    _write(output, json.dumps(frame, separators=(",", ":"), allow_nan=False).encode())
    return frame


def day_members(data, day, station):
    issue = datetime.combine(day, time.min, UTC)
    start = issue + timedelta(hours=6)
    if station not in CANDIDATES or STATIONS[station][3] != -6:
        return []
    available_members = []
    for member in MEMBERS:
        frames = []
        for lead in LEADS:
            try:
                frame = json.loads(path(data, issue, lead, member).read_bytes())
            except FileNotFoundError:
                break
            if (frame["member"] != member or utc(frame["issued_at"]) != issue
                    or utc(frame["start_at"]) != issue + timedelta(hours=lead - 6)
                    or utc(frame["valid_at"]) != issue + timedelta(hours=lead)
                    or utc(frame["available_at"]) > start):
                break
            frames.append(frame)
        if len(frames) != len(LEADS):
            continue
        available_members.append({
            "member": member, "available_at": max(row["available_at"] for row in frames),
            "high_f": max(row["high"][station]["temp_f"] for row in frames),
            "low_f": min(row["low"][station]["temp_f"] for row in frames),
            "frames": [{key: row[key] for key in ("raw", "sha256", "source", "available_at", "start_at", "valid_at")}
                       for row in frames],
        })
    return available_members


def backfill(data, start, end, workers, fetch=collect_frame):
    stats, issues = {}, {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for day in date_range(start, end):
            issue = datetime.combine(day, time.min, UTC)
            jobs = {pool.submit(fetch, data, issue, lead, member): (lead, member)
                    for lead in LEADS for member in MEMBERS}
            complete = 0
            for job in as_completed(jobs):
                lead, member = jobs[job]
                try:
                    job.result()
                    complete += 1
                except SourcePaused:
                    for pending in jobs:
                        pending.cancel()
                    return stats, issues
                except (OSError, TypeError, ValueError, KeyError) as error:
                    issues[f"{day}/f{lead:03d}-m{member:02d}"] = str(error)
            stats[str(day)] = complete
            print(f"{day}: {complete}/{len(jobs)} GEFS member-windows", flush=True)
    return stats, issues


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data", type=Path, default=Path(".data"))
    p.add_argument("--start", type=date.fromisoformat, required=True)
    p.add_argument("--end", type=date.fromisoformat)
    p.add_argument("--workers", type=int, choices=range(1, 13), default=8)
    args = p.parse_args()
    days, issues = backfill(args.data, args.start, args.end or args.start, args.workers)
    print(json.dumps({"days": days, "issues": issues}, indent=2))
    return 0 if any(days.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
