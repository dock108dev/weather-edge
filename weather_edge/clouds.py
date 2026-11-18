"""Cache NOAA GFS instantaneous total cloud cover to match temperature frames."""

from __future__ import annotations

import argparse
import json
import re
from base64 import b64encode
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.request import Request, urlopen
from uuid import uuid4

from . import grids

DEFAULT_CACHE = Path(".data/clouds")
FRAME_ID = re.compile(r"(20\d{8})-f(\d{3})")
CLOUD_RECORD = re.compile(r":TCDC:entire atmosphere:(?:anl|\d+ hour fcst):")


def locate_cloud(index: str) -> tuple[int, int]:
    """Select the instantaneous entire-atmosphere field, skipping averaged and layer products."""
    rows = index.splitlines()
    for row, next_row in zip(rows, rows[1:]):
        if CLOUD_RECORD.search(row):
            start, stop = int(row.split(":")[1]), int(next_row.split(":")[1])
            if stop > start:
                return start, stop - 1
    raise ValueError("No byte range for instantaneous GFS total cloud cover in inventory")


def cloud_data(payload: bytes, issue: datetime, lead: int) -> str:
    from eccodes import CodesInternalError, codes_get, codes_get_values, codes_new_from_message, codes_release
    import numpy as np

    handle = None
    try:
        handle = codes_new_from_message(payload)
        conditions = {
            "discipline": 0, "parameterCategory": 6, "parameterNumber": 1,
            "typeOfLevel": "atmosphere", "stepType": "instant", "units": "%",
            "Ni": 360, "Nj": 181,
            "latitudeOfFirstGridPointInDegrees": 90, "longitudeOfFirstGridPointInDegrees": 0,
            "iScansNegatively": 0, "jScansPositively": 0,
            "jPointsAreConsecutive": 0, "alternativeRowScanning": 0,
        }
        for timestamp, date_key, time_key in ((issue, "dataDate", "dataTime"),
                                              (issue + timedelta(hours=lead), "validityDate", "validityTime")):
            conditions[date_key] = int(timestamp.strftime("%Y%m%d"))
            conditions[time_key] = int(timestamp.strftime("%H%M"))
        for key, expected in conditions.items():
            value = codes_get(handle, key)
            if value != expected:
                raise ValueError(f"Unexpected GFS cloud field {key}={value}; expected {expected}")
        values = codes_get_values(handle)
        if len(values) != 360 * 181 or not np.isfinite(values).all() or values.min() < 0 or values.max() > 100:
            raise ValueError("Unexpected GFS cloud cover values")
        return b64encode(np.rint(values).astype(np.uint8).tobytes()).decode("ascii")
    except CodesInternalError as exc:
        raise ValueError(f"Cannot decode GFS cloud field: {exc}") from exc
    finally:
        if handle is not None:
            codes_release(handle)


def cache_frame(issue: datetime, lead: int, cache: Path = DEFAULT_CACHE, *, get=urlopen) -> Path:
    path = grids.frame_path(cache, issue, lead)
    if grids._cached_metadata(path):
        return path
    url = grids.object_url(issue, lead)
    with get(url + ".idx", timeout=20) as response:
        start, end = locate_cloud(response.read().decode("ascii"))
    request = Request(url, headers={"Range": f"bytes={start}-{end}"})
    with get(request, timeout=20) as response:
        content_range = response.headers.get("Content-Range", "")
        if response.status != 206 or not content_range.startswith(f"bytes {start}-{end}/"):
            raise ValueError("NOAA did not return the requested GRIB2 byte range")
        available = parsedate_to_datetime(response.headers["Last-Modified"])
        if available < issue:
            raise ValueError("NOAA cloud frame publication predates the run issue")
        payload = response.read(end - start + 2)
    if len(payload) != end - start + 1:
        raise ValueError("Incomplete GRIB2 cloud record")
    frame = {
        "id": path.stem,
        "issued_at": issue.isoformat(),
        "available_at": available.isoformat(),
        "ingested_at": datetime.now(grids.UTC).isoformat(),
        "valid_at": (issue + timedelta(hours=lead)).isoformat(),
        "source": url,
        "width": 360,
        "height": 181,
        "format": "u8-percent",
        "pixels": cloud_data(payload, issue, lead),
    }
    cache.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.stem}-{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(frame, separators=(",", ":")))
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def cache_matching(frame_ids, cache: Path = DEFAULT_CACHE, *, get=urlopen) -> dict:
    """Cache matching GFS clouds for the requested temperature forecast frame IDs."""
    paths, errors = [], {}
    with ThreadPoolExecutor(max_workers=6) as workers:
        jobs = {}
        for frame_id in dict.fromkeys(frame_ids):
            match = FRAME_ID.fullmatch(frame_id)
            if not match:
                errors[frame_id] = "Invalid GFS frame ID"
                continue
            try:
                issue = datetime.strptime(match[1], "%Y%m%d%H").replace(tzinfo=grids.UTC)
            except ValueError:
                errors[frame_id] = "Invalid GFS frame timestamp"
                continue
            jobs[frame_id] = workers.submit(cache_frame, issue, int(match[2]), cache, get=get)
        for frame_id, job in jobs.items():
            try:
                paths.append(job.result())
            except (OSError, ValueError) as exc:
                errors[frame_id] = str(exc)
    return {"frames": [path.stem for path in paths], "errors": errors}


def manifest(cache: Path = DEFAULT_CACHE) -> list[dict]:
    return grids.manifest(cache)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", choices=("replay", "live"))
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--end", type=date.fromisoformat)
    args = parser.parse_args()
    if args.run == "replay":
        if args.end and args.end < (args.start or grids.DEMO_DAY):
            parser.error("Start date must precede or equal end date")
        frame_ids = [row["id"] for row in grids.replay_frames(grids.manifest(), args.start or grids.DEMO_DAY, args.end)]
    else:
        if args.start or args.end:
            parser.error("Date range is for the replay command")
        frame_ids = grids.cache_live()["frames"]
    result = cache_matching(frame_ids)
    if not result["frames"] and not result["errors"]:
        print("No matching temperature frames. Prepare the temperature cache first.")
    print(json.dumps(result, indent=2))
    return 0 if result["frames"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
