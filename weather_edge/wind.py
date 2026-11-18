"""Cache paired as-issued NOAA GFS 10 m eastward and northward wind grids."""
from __future__ import annotations

import argparse
from base64 import b64decode, b64encode
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from functools import lru_cache
import json
from pathlib import Path
import re
from urllib.request import Request, urlopen
from uuid import uuid4

from . import grids

DEFAULT_CACHE = Path(".data/wind")
FORMAT = "i16le-uv-0.1mps"
WIDTH, HEIGHT = 360, 181
WIND_RECORD = re.compile(r":(UGRD|VGRD):10 m above ground:(?:anl|\d+ hour fcst):")
FRAME_ID = re.compile(r"(20\d{8})-f(\d{3})")


def locate_wind(index: str) -> dict[str, tuple[int, int]]:
    """Require exactly one instantaneous range for each component."""
    records = {}
    lines = index.splitlines()
    for line, next_line in zip(lines, lines[1:]):
        if match := WIND_RECORD.search(line):
            key = match[1]
            start, stop = int(line.split(":")[1]), int(next_line.split(":")[1])
            if not 0 <= start < stop or key in records:
                raise ValueError("Ambiguous GFS wind field inventory")
            records[key] = (start, stop - 1)
    if records.keys() != {"UGRD", "VGRD"}:
        raise ValueError("GFS inventory did not supply both 10 m wind components")
    return records


def _decode_component(payload, code, issue, valid):
    from eccodes import CodesInternalError, codes_get, codes_get_values, codes_new_from_message, codes_release
    import numpy as np

    expected = {
        "discipline": 0, "parameterCategory": 2, "parameterNumber": code, "units": "m s**-1",
        "typeOfLevel": "heightAboveGround", "level": 10, "stepType": "instant", "uvRelativeToGrid": 0,
        "gridType": "regular_ll", "Ni": WIDTH, "Nj": HEIGHT,
        "latitudeOfFirstGridPointInDegrees": 90, "longitudeOfFirstGridPointInDegrees": 0,
        "iDirectionIncrementInDegrees": 1, "jDirectionIncrementInDegrees": 1,
        "iScansNegatively": 0, "jScansPositively": 0, "jPointsAreConsecutive": 0,
        "alternativeRowScanning": 0, "numberOfMissing": 0,
        "dataDate": int(issue.strftime("%Y%m%d")), "dataTime": int(issue.strftime("%H%M")),
        "validityDate": int(valid.strftime("%Y%m%d")), "validityTime": int(valid.strftime("%H%M")),
    }
    handle = None
    try:
        handle = codes_new_from_message(payload)
        for key, value in expected.items():
            if codes_get(handle, key) != value:
                raise ValueError(f"NOAA wind field has an unexpected {key}")
        values = codes_get_values(handle)
        if len(values) != WIDTH * HEIGHT or not np.isfinite(values).all():
            raise ValueError("GFS wind field is truncated or contains missing values")
        scaled = np.rint(values * 10)
        if np.min(scaled) < -32768 or np.max(scaled) > 32767:
            raise ValueError("GFS wind values exceed cache encoding range")
        return scaled.astype("<i2")
    except CodesInternalError as exc:
        raise ValueError(f"Could not decode GFS wind component: {exc}") from exc
    finally:
        if handle is not None:
            codes_release(handle)


def wind_data(parts, issue, lead):
    import numpy as np

    valid = issue + timedelta(hours=lead)
    east = _decode_component(parts["UGRD"], 2, issue, valid)
    north = _decode_component(parts["VGRD"], 3, issue, valid)
    pixels = np.empty((WIDTH * HEIGHT, 2), dtype="<i2")
    pixels[:, 0] = east
    pixels[:, 1] = north
    return b64encode(pixels.tobytes()).decode("ascii")


def _range(url, bounds, get, expected_etag=None):
    first, last = bounds
    headers = {"Range": f"bytes={first}-{last}"}
    if expected_etag is not None:
        headers["If-Match"] = expected_etag
    with get(Request(url, headers=headers), timeout=20) as response:
        content_range = response.headers.get("Content-Range", "")
        if response.status != 206 or not content_range.startswith(f"bytes {first}-{last}/"):
            raise ValueError("NOAA did not return the requested wind byte range")
        modified = response.headers.get("Last-Modified")
        etag = response.headers.get("ETag")
        payload = response.read(last - first + 2)
    if len(payload) != last - first + 1 or not modified:
        raise ValueError("Incomplete GFS wind range or no publication timestamp")
    if expected_etag is not None and etag != expected_etag:
        raise ValueError("GFS archive changed between component requests")
    return payload, parsedate_to_datetime(modified), etag


def _pair(url, ranges, get):
    # Both official fields are normally adjacent and fit one range request.
    (key1, range1), (key2, range2) = sorted(ranges.items(), key=lambda item: item[1][0])
    if range1[1] + 1 == range2[0]:
        combined, modified, etag = _range(url, (range1[0], range2[1]), get)
        split = range2[0] - range1[0]
        return {key1: combined[:split], key2: combined[split:]}, modified, etag
    part1, modified1, etag = _range(url, range1, get)
    if not etag:
        raise ValueError("No object identity for separately fetched GFS wind components")
    part2, modified2, _ = _range(url, range2, get, expected_etag=etag)
    if modified1 != modified2:
        raise ValueError("GFS archive changed between component requests")
    return {key1: part1, key2: part2}, modified2, etag


@lru_cache(maxsize=512)
def _metadata(path, mtime, size):
    frame = json.loads(path.read_text())
    pixels = b64decode(frame.pop("pixels"), validate=True)
    if len(pixels) != WIDTH * HEIGHT * 4 or (frame["width"], frame["height"]) != (WIDTH, HEIGHT):
        raise ValueError("Invalid wind frame shape")
    if frame["id"] != path.stem or frame["format"] != FORMAT or frame["units"] != "m/s":
        raise ValueError("Invalid wind frame identity")
    issued, published, received, valid = (
        datetime.fromisoformat(frame[key]) for key in ("issued_at", "available_at", "ingested_at", "valid_at")
    )
    if any(clock.utcoffset() is None for clock in (issued, published, received, valid)):
        raise ValueError("Missing wind frame time zone")
    issue, lead = _parse_id(frame["id"])
    if not issued == issue <= published <= received or valid != issue + timedelta(hours=lead):
        raise ValueError("GFS wind frame times do not match its forecast identity")
    return frame


def _cached(path):
    try:
        stat = path.stat()
        return _metadata(path, stat.st_mtime_ns, stat.st_size)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


def cache_frame(issue, lead, cache=DEFAULT_CACHE, *, get=urlopen):
    cache = Path(cache)
    path = grids.frame_path(cache, issue, lead)
    if _cached(path):
        return path
    url = grids.object_url(issue, lead)
    with get(url + ".idx", timeout=20) as response:
        ranges = locate_wind(response.read().decode("ascii"))
    parts, available, etag = _pair(url, ranges, get)
    ingested = datetime.now(grids.UTC)
    if not issue <= available <= ingested:
        raise ValueError("NOAA wind frame publication falls outside the issue–receipt window")
    frame = {
        "id": path.stem,
        "issued_at": issue.isoformat(),
        "available_at": available.isoformat(),
        "ingested_at": ingested.isoformat(),
        "valid_at": (issue + timedelta(hours=lead)).isoformat(),
        "source": url,
        "source_etag": etag,
        "component_ranges": ranges,
        "width": WIDTH,
        "height": HEIGHT,
        "format": FORMAT,
        "units": "m/s",
        "resolution_degrees": 1,
        "height_m": 10,
        "pixels": wind_data(parts, issue, lead),
    }
    cache.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.stem}-{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(frame, separators=(",", ":"), allow_nan=False))
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def manifest(cache=DEFAULT_CACHE):
    return [frame for path in sorted(Path(cache).glob("20????????-f???.json"))
            if (frame := _cached(path)) is not None]


def _parse_id(frame_id):
    match = FRAME_ID.fullmatch(frame_id)
    if not match:
        raise ValueError("Invalid GFS wind frame ID")
    return datetime.strptime(match[1], "%Y%m%d%H").replace(tzinfo=grids.UTC), int(match[2])


def cache_matching(frame_ids, cache=DEFAULT_CACHE, *, get=urlopen):
    paths, failures = [], {}
    with ThreadPoolExecutor(max_workers=4) as workers:
        jobs = {}
        for key in dict.fromkeys(frame_ids):
            try:
                issue, lead = _parse_id(key)
            except (ValueError, TypeError) as exc:
                failures[str(key)] = str(exc)
                continue
            jobs[key] = workers.submit(cache_frame, issue, lead, cache, get=get)
        for key, outcome in jobs.items():
            try:
                paths.append(outcome.result().stem)
            except (OSError, ValueError, KeyError) as exc:
                failures[key] = str(exc)
    return {"frames": paths, "errors": failures}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", choices=("replay", "live"))
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--end", type=date.fromisoformat)
    parser.add_argument("--frame", nargs="+", help="Use specific cached GFS frame IDs")
    args = parser.parse_args()
    candidates = grids.manifest()
    if args.run == "replay":
        start = args.start or grids.DEMO_DAY
        if args.end and args.end < start:
            parser.error("Start date must precede or equal end date")
        candidates = grids.replay_frames(candidates, start, args.end)
    elif args.start or args.end:
        parser.error("Date range is for the replay command")
    elif candidates:
        issue = max(frame["issued_at"] for frame in candidates)
        candidates = [frame for frame in candidates if frame["issued_at"] == issue]
    known = {frame["id"] for frame in candidates}
    selected = args.frame if args.frame else sorted(known)
    if carry := set(selected) - known:
        parser.error("These temperature frames are outside the selection or absent: " + ", ".join(sorted(carry)))
    result = cache_matching(selected)
    print(json.dumps(result, indent=2))
    return 0 if result["frames"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
