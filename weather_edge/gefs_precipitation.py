"""Archive as-issued GEFS member rainfall over the exact Central climate day."""
from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta
from hashlib import sha256
import json
import math
from pathlib import Path
from urllib.request import urlopen

from . import gefs
from .history import _write
from .nbm import iso, utc


def indexed_precipitation(index, lead):
    field = f":APCP:surface:{lead - 6}-{lead} hour acc fcst:"
    lines = index.splitlines()
    choices = [(int(a.split(":")[1]), int(b.split(":")[1]))
               for a, b in zip(lines, lines[1:]) if field in a]
    if len(choices) != 1:
        raise ValueError("Expected one exact six-hour GEFS precipitation range")
    start, end = choices[0]
    if not 0 <= start < end <= start + 2 * 1024 * 1024:
        raise ValueError("Invalid GEFS precipitation range")
    return start, end


def _decode(body, issue, lead, member, *, coordinates=None):
    from eccodes import CodesInternalError, codes_get, codes_grib_find_nearest, codes_new_from_message, codes_release

    valid = issue + timedelta(hours=lead)
    expected = {
        "discipline": 0, "parameterCategory": 1, "parameterNumber": 8, "units": "kg m**-2",
        "typeOfLevel": "surface", "stepType": "accum", "stepUnits": 1,
        "startStep": lead - 6, "endStep": lead, "perturbationNumber": member,
        "gridType": "regular_ll", "Ni": 720, "Nj": 361,
        "iDirectionIncrementInDegrees": .5, "jDirectionIncrementInDegrees": .5,
        "dataDate": int(issue.strftime("%Y%m%d")), "dataTime": int(issue.strftime("%H%M")),
        "validityDate": int(valid.strftime("%Y%m%d")), "validityTime": int(valid.strftime("%H%M")),
    }
    handle = None
    try:
        handle = codes_new_from_message(body)
        for key, value in expected.items():
            if codes_get(handle, key) != value:
                raise ValueError(f"GEFS APCP field has unexpected {key}")
        cells = {}
        points = coordinates if coordinates is not None else {station: gefs.STATIONS[station][:2] for station in gefs.CANDIDATES}
        for station, (lat, lon) in points.items():
            cell, = codes_grib_find_nearest(handle, lat, lon, is_lsm=False, npoints=1)
            liquid = cell["value"]
            if not math.isfinite(liquid) or liquid < 0:
                raise ValueError(f"Missing GEFS precipitation for {station}")
            cells[station] = {
                "grid_lat": cell["lat"], "grid_lon": ((cell["lon"] + 180) % 360) - 180,
                "distance_km": cell["distance"], "liquid_mm": liquid,
            }
        return cells
    except CodesInternalError as error:
        raise ValueError(f"GEFS precipitation decoding failed: {error}") from error
    finally:
        if handle is not None:
            codes_release(handle)


def path(data, issue, lead, member):
    return Path(data) / "gefs-precipitation" / issue.strftime("%Y%m%d%H") / f"f{lead:03d}-m{member:02d}.json"


def collect_frame(data, issue, lead, member, *, get=urlopen):
    destination = path(data, issue, lead, member)
    try:
        return json.loads(destination.read_bytes())
    except FileNotFoundError:
        pass
    source = gefs.object_url(issue, lead, member)
    with gefs._get(data, source + ".idx", get) as reply:
        bounds = indexed_precipitation(reply.read(512 * 1024).decode("ascii"), lead)
    body, publication, ingested, tag = gefs._range(data, source, *bounds, issue, get)
    cells = _decode(body, issue, lead, member)
    raw = destination.with_suffix(".grib2")
    result = {
        "model": "NOAA GEFS", "member": member, "field": "APCP at surface, six-hour accumulated liquid equivalent",
        "native_units": "kg m**-2", "display_units": "mm", "grid_resolution_degrees": .5,
        "source": source, "source_etag": tag, "sha256": sha256(body).hexdigest(), "byte_range": [bounds[0], bounds[1] - 1],
        "issued_at": iso(issue), "available_at": iso(publication), "ingested_at": iso(ingested),
        "starts_at": iso(issue + timedelta(hours=lead - 6)), "valid_at": iso(issue + timedelta(hours=lead)),
        "raw": str(raw.relative_to(data)), "stations": cells,
    }
    _write(raw, body)
    _write(destination, json.dumps(result, separators=(",", ":"), allow_nan=False).encode())
    return result


def day_members(data, day, station):
    """Sum contiguous native windows independently for each complete member."""
    issue = datetime.combine(day, time.min, gefs.UTC)
    cutoff = issue + timedelta(hours=6)
    if station not in gefs.CANDIDATES or gefs.STATIONS[station][3] != -6:
        return []
    members = []
    for member in gefs.MEMBERS:
        frames = []
        for lead in gefs.LEADS:
            try:
                frame = json.loads(path(data, issue, lead, member).read_bytes())
            except FileNotFoundError:
                break
            if (frame["member"] != member or utc(frame["issued_at"]) != issue or utc(frame["available_at"]) > cutoff
                    or utc(frame["starts_at"]) != issue + timedelta(hours=lead - 6)
                    or utc(frame["valid_at"]) != issue + timedelta(hours=lead)):
                break
            frames.append(frame)
        if len(frames) == len(gefs.LEADS):
            members.append({
                "member": member,
                "liquid_mm": math.fsum(row["stations"][station]["liquid_mm"] for row in frames),
                "available_at": max(row["available_at"] for row in frames),
                "sources": [{key: row[key] for key in ("source", "raw", "sha256", "available_at", "starts_at", "valid_at")}
                           for row in frames],
            })
    return members


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data", type=Path, default=Path(".data"))
    p.add_argument("--start", type=date.fromisoformat, required=True)
    p.add_argument("--end", type=date.fromisoformat)
    p.add_argument("--workers", type=int, choices=range(1, 13), default=8)
    args = p.parse_args()
    counts, issues = gefs.backfill(args.data, args.start, args.end or args.start, args.workers, fetch=collect_frame)
    print(json.dumps({"days": counts, "issues": issues}, indent=2))
    return 0 if any(counts.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
