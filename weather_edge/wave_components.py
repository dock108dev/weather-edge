"""Retain the native wind-sea height and three GFS Wave swell heights."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from functools import lru_cache
from hashlib import sha256
import json
from pathlib import Path
from urllib.request import urlopen

from . import grids, waves, wind
from .history import _write
from .market import iso, parse_time

COMPONENTS = {
    "wind_sea_m": ("WVHGT", "surface", 5, "surface", 1),
    "primary_swell_m": ("SWELL", "1 in sequence", 8, "orderedSequenceData", 1),
    "secondary_swell_m": ("SWELL", "2 in sequence", 8, "orderedSequenceData", 2),
    "tertiary_swell_m": ("SWELL", "3 in sequence", 8, "orderedSequenceData", 3),
}
DOCS = "https://polar.ncep.noaa.gov/waves/Model_Description.pdf"
LIVE_LEADS = (3, 6, 9, 12, 18, 24, 36)


def component_ranges(index, lead):
    period = f"{lead} hour fcst" if lead else "anl"
    lines = index.splitlines()
    bounds = {}
    for name, (symbol, location, *_details) in COMPONENTS.items():
        token = f":{symbol}:{location}:{period}:"
        matches = [(int(a.split(":")[1]), int(b.split(":")[1]))
                   for a, b in zip(lines, lines[1:]) if token in a]
        if len(matches) != 1 or not 0 <= matches[0][0] < matches[0][1] <= matches[0][0] + 2**20:
            raise ValueError(f"Expected one valid GFS Wave GRIB2 {name} range")
        bounds[name] = matches[0]
    ordered = sorted(bounds.values())
    if any(left[1] != right[0] for left, right in zip(ordered, ordered[1:])):
        raise ValueError("The four GFS Wave component fields are not contiguous")
    return bounds


def _path(root, issue, lead):
    return grids.frame_path(root / "waves", issue, lead).with_suffix(".components.json")


@lru_cache(maxsize=512)
def _metadata(path, mtime):
    return json.loads(path.read_bytes())


def metadata(root, height):
    try:
        issue, lead = wind._parse_id(height["id"])
        path = _path(Path(root), issue, lead)
        row = _metadata(path, path.stat().st_mtime_ns)
        if (row["id"] == height["id"] and row["source_etag"] and row["source_etag"] == height["source_etag"]
                and parse_time(row["issued_at"]) == issue
                and parse_time(row["valid_at"]) == issue + timedelta(hours=lead)
                and parse_time(row["available_at"]) == parse_time(height["available_at"])
                and issue <= parse_time(row["available_at"]) <= parse_time(row["ingested_at"])):
            return row
    except (OSError, TypeError, KeyError, ValueError):
        pass
    return None


@lru_cache(maxsize=2)
def _grids(path, digest, ident, archived_ranges):
    body = path.read_bytes()
    if sha256(body).hexdigest() != digest:
        raise ValueError("GFS Wave component bytes differ from the retained digest")
    issue, lead = wind._parse_id(ident)
    origin = min(start for _, start, _ in archived_ranges)
    return {name: waves.native_grid(body[start-origin:end-origin], issue, lead, parameter=COMPONENTS[name][2],
                                    level_type=COMPONENTS[name][3], level=COMPONENTS[name][4])
            for name, start, end in archived_ranges}


def fields(data, row):
    ranges = tuple((name, *row["ranges"][name]) for name in COMPONENTS)
    return _grids(Path(data) / row["raw"], row["sha256"], row["id"], ranges)


def point(data, height, lat, lon, at, *, local_receipt=False):
    row = metadata(data, height)
    at = parse_time(at)
    if row is None or parse_time(row["available_at"]) > at or (local_receipt and parse_time(row["ingested_at"]) > at):
        return None
    cells = {name: waves.sample_grid(grid, lat, lon)[0] for name, grid in fields(data, row).items()}
    return {**cells, **{key: row[key] for key in ("available_at", "ingested_at", "sha256", "source")}}


def cache_frame(data, issue, lead, *, get=urlopen):
    root = Path(data)
    height = waves._cached(grids.frame_path(root / "waves", issue, lead))
    if not height:
        return None
    if existing := metadata(root, height):
        return existing
    path = _path(root, issue, lead)

    def collect(request, *, timeout):
        if (root / "collector" / "paused").exists():
            raise TimeoutError("Collection stopped")
        return get(request, timeout=timeout)

    url = waves.object_url(issue, lead)
    with collect(url + ".idx", timeout=20) as response:
        ranges = component_ranges(response.read(512 * 1024).decode("ascii"), lead)
    first, stop = min(start for start, _ in ranges.values()), max(end for _, end in ranges.values())
    body, published, tag = wind._range(url, (first, stop - 1), collect, height["source_etag"])
    received = datetime.now(waves.UTC)
    if (not tag or tag != height["source_etag"] or parse_time(height["available_at"]) != published
            or not issue <= published <= received):
        raise ValueError("Component field and wave height are from different NOAA object versions")
    for name, (start, end) in ranges.items():
        _, _, parameter, level_type, level = COMPONENTS[name]
        waves.native_grid(body[start-first:end-first], issue, lead, parameter=parameter,
                          level_type=level_type, level=level)
    raw = path.with_suffix(".grib2")
    row = {"id": height["id"], "source": url, "source_index": url + ".idx", "source_etag": tag, "ranges": ranges,
           "issued_at": iso(issue), "valid_at": iso(issue + timedelta(hours=lead)), "available_at": iso(published),
           "ingested_at": iso(received), "sha256": sha256(body).hexdigest(), "raw": str(raw.relative_to(root)),
           "resolution_degrees": .25, "units": "m", "docs": DOCS}
    _write(raw, body)
    _write(path, json.dumps(row, separators=(",", ":"), allow_nan=False).encode())
    return row


def backfill(data, requests, *, workers=3):
    frames, errors = [], {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        jobs = {(issue, lead): pool.submit(cache_frame, data, issue, lead) for issue, lead in requests}
        for (issue, lead), job in jobs.items():
            try:
                row = job.result()
                if row is None:
                    raise ValueError("Companion significant-wave-height field is absent")
                frames.append(row)
            except (OSError, TypeError, ValueError, KeyError) as error:
                errors[f"{iso(issue)} f{lead:03d}"] = str(error)
    return frames, errors
