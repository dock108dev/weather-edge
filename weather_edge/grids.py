"""Cache real NOAA GFS 2 metre temperatures from byte-indexed GRIB2 objects."""
from __future__ import annotations

import argparse
from base64 import b64encode
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone
from email.utils import parsedate_to_datetime
from functools import lru_cache
import json
from pathlib import Path
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

BUCKET = "https://noaa-gfs-bdp-pds.s3.amazonaws.com"
DEFAULT_CACHE = Path(".data/grids")
UTC = timezone.utc
TEMP_RECORD = re.compile(r":TMP:2 m above ground:(?:anl|\d+ hour fcst):")
GRID_HOURS = (0, 3, 6, 9, 12, 15, 18, 21, 24, 27, 30, 33, 36)
DEMO_DAY = date(2026, 11, 5)


def frame_path(cache, issue, lead):
    return cache / (issue.strftime("%Y%m%d%H") + f"-f{lead:03d}.json")


def object_url(issue, lead):
    prefix = issue.strftime("gfs.%Y%m%d/%H/atmos/gfs.t%Hz")
    return f"{BUCKET}/{prefix}.pgrb2.1p00.f{lead:03d}"


def locate_temp(index):
    rows = index.splitlines()
    for row, next_row in zip(rows, rows[1:]):
        if TEMP_RECORD.search(row):
            start, stop = int(row.split(":")[1]), int(next_row.split(":")[1])
            if stop > start:
                return start, stop - 1
    raise ValueError("No byte range for GFS 2 metre temperature in inventory")


def temperature_data(payload):
    from eccodes import codes_get, codes_get_values, codes_new_from_message, codes_release
    import numpy as np

    handle = codes_new_from_message(payload)
    try:
        fields = {key: codes_get(handle, key) for key in (
            "Ni", "Nj", "latitudeOfFirstGridPointInDegrees", "longitudeOfFirstGridPointInDegrees",
            "iScansNegatively", "jScansPositively", "jPointsAreConsecutive", "alternativeRowScanning",
        )}
        if tuple(fields.values()) != (360, 181, 90, 0, 0, 0, 0, 0):
            raise ValueError(f"Unexpected GFS grid orientation: {fields}")
        values = codes_get_values(handle)
        if len(values) != 360 * 181 or not np.isfinite(values).all() or values.min() < 150 or values.max() > 350:
            raise ValueError("Unexpected GFS Kelvin temperature field")
        fahrenheit = (values - 273.15) * 1.8 + 32
        # Pack tenths of a degree Fahrenheit as little-endian uint16 plus an offset.
        return b64encode(np.rint((fahrenheit + 256) * 10).astype("<u2").tobytes()).decode("ascii")
    finally:
        codes_release(handle)


def cache_frame(issue, lead, cache=DEFAULT_CACHE, *, get=urlopen):
    path = frame_path(cache, issue, lead)
    if _cached_metadata(path):
        return path
    url = object_url(issue, lead)
    with get(url + ".idx", timeout=20) as response:
        start, end = locate_temp(response.read().decode("ascii"))
    request = Request(url, headers={"Range": f"bytes={start}-{end}"})
    with get(request, timeout=20) as response:
        content_range = response.headers.get("Content-Range", "")
        if response.status != 206 or not content_range.startswith(f"bytes {start}-{end}/"):
            raise ValueError("NOAA did not return the requested GRIB2 byte range")
        available = parsedate_to_datetime(response.headers["Last-Modified"])
        payload = response.read(end - start + 2)
    if len(payload) != end - start + 1:
        raise ValueError("Incomplete GRIB2 record")
    frame = {
        "id": path.stem,
        "issued_at": issue.isoformat(),
        "available_at": available.isoformat(),
        "ingested_at": datetime.now(UTC).isoformat(),
        "valid_at": (issue + timedelta(hours=lead)).isoformat(),
        "source": url,
        "width": 360,
        "height": 181,
        "format": "u16le-0.1F-offset256",
        "pixels": temperature_data(payload),
    }
    cache.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.stem}-{uuid4().hex}.tmp")
    try:
        temp_path.write_text(json.dumps(frame, separators=(",", ":")))
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)
    return path


def cache_run(issue, leads, cache=DEFAULT_CACHE, *, get=urlopen):
    files, errors = [], {}
    with ThreadPoolExecutor(max_workers=6) as workers:
        result = {lead: workers.submit(cache_frame, issue, lead, cache, get=get) for lead in leads}
        for lead, future in result.items():
            try:
                files.append(future.result())
            except (HTTPError, URLError, OSError, ValueError) as exc:
                errors[lead] = str(exc)
    return files, errors


def replay_runs(start, end):
    start = datetime.combine(start, time.min, UTC)
    end = datetime.combine(end, time.min, UTC)
    if start > end:
        raise ValueError("Start date must precede or equal end date")
    # The prior run fills the first hours while the day's 00Z files are
    # publishing. Following runs span the next day's public payout records.
    runs = {start - timedelta(hours=6): tuple(range(9, 37, 3))}
    for offset in range((end - start).days + 2):
        first = start + timedelta(days=offset)
        runs[first] = tuple(range(3, 34, 3))
        runs[first + timedelta(hours=12)] = tuple(range(3, 25, 3))
    return sorted(runs.items())


def cache_replay(cache=DEFAULT_CACHE, *, start=DEMO_DAY, end=None):
    runs = replay_runs(start, end or start)
    count = 0
    for issue, leads in runs:
        files, errors = cache_run(issue, leads, cache)
        count += len(files)
        print(f"{issue.isoformat()} {len(files)}/{len(leads)} NOAA temperature fields")
        for lead, error in errors.items():
            print(f"  f{lead:03d} {error}")
    return count


def cache_live(as_of=None, cache=DEFAULT_CACHE, *, get=urlopen):
    """Download as-issued forecast frames from a single recent GFS cycle."""
    as_of = as_of or datetime.now(UTC)
    if isinstance(as_of, str):
        as_of = datetime.fromisoformat(as_of)
    last_complete = as_of.astimezone(UTC) - timedelta(hours=5)
    issue = last_complete.replace(hour=(last_complete.hour // 6) * 6, minute=0, second=0, microsecond=0)
    files, errors = cache_run(issue, GRID_HOURS, cache, get=get)
    return {"frames": [file.stem for file in files], "errors": errors, "issued_at": issue.isoformat()}


@lru_cache(maxsize=2048)
def _frame_metadata(path, mtime, size):
    data = json.loads(path.read_text())
    pixels = data.pop("pixels")
    expected_size = {"u8-percent": 65160, "u16le-0.1F-offset256": 130320}.get(data["format"])
    if not expected_size or not isinstance(pixels, str) or len(pixels) != 4 * ((expected_size + 2) // 3):
        raise ValueError("Invalid weather grid buffer")
    if data["id"] != path.stem or data["width"] != 360 or data["height"] != 181:
        raise ValueError("Invalid weather grid identity or shape")
    for clock in ("issued_at", "available_at", "valid_at"):
        if datetime.fromisoformat(data[clock]).utcoffset() is None:
            raise ValueError("Grid timestamps must have an offset")
    return data


def _cached_metadata(path):
    try:
        stat = path.stat()
        return _frame_metadata(path, stat.st_mtime_ns, stat.st_size)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


def manifest(cache=DEFAULT_CACHE):
    return [frame for path in sorted(cache.glob("20????????-f???.json"))
            if (frame := _cached_metadata(path)) is not None]


def replay_frames(frames, start, end=None):
    """Select global runs that overlap station days without using a fixed date."""
    if isinstance(start, str):
        start = date.fromisoformat(start)
    if isinstance(end, str):
        end = date.fromisoformat(end)
    first = datetime.combine(start, time.min, UTC)
    last_issue = datetime.combine(end or start, time.min, UTC) + timedelta(hours=36)
    return [
        frame for frame in frames
        if first - timedelta(hours=12) <= datetime.fromisoformat(frame["issued_at"]) <= last_issue
        and datetime.fromisoformat(frame["valid_at"]) >= first
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", choices=("replay", "live"))
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--end", type=date.fromisoformat)
    args = parser.parse_args()
    if args.run == "replay":
        try:
            return 0 if cache_replay(start=args.start or DEMO_DAY, end=args.end) else 1
        except ValueError as exc:
            parser.error(str(exc))
    if args.start or args.end:
        parser.error("Date range is for the replay command")
    result = cache_live()
    print(json.dumps(result, indent=2))
    return 0 if result["frames"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
