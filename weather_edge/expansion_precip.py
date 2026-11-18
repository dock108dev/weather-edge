"""Cache exact three-hour NOAA GFS surface precipitation forecast grids."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from base64 import b64decode, b64encode
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from urllib.request import urlopen

from . import grids, wind
from .history import _write

DEFAULT_CACHE = Path(".data/precipitation")
WIDTH, HEIGHT = 360, 181
FORMAT = "u16le-0.01mm"
APCP = re.compile(r":APCP:surface:(\d+)-(\d+) (hour|day) acc fcst:")
WINDOW_HOURS = 3


def locate_apcp(index, lead, start_step=None):
    """Find the shortest published accumulation ending at the indicated lead."""
    lines = index.splitlines()
    candidates = {}
    for line, following in zip(lines, lines[1:]):
        match = APCP.search(line)
        if not match:
            continue
        start, stop = map(int, match.groups()[:2])
        if match[3] == "day":
            start, stop = start * 24, stop * 24
        if stop == lead and start < stop and (start_step is None or start == start_step):
            first, after = (int(row.split(":")[1]) for row in (line, following))
            if not 0 <= first < after:
                raise ValueError("Invalid GFS precipitation index range")
            # GFS can repeat the shortest window when it coincides with the
            # running total. Take the first native record for that window.
            candidates.setdefault(start, (first, after - 1))
    if not candidates:
        raise ValueError(f"No matching NOAA APCP accumulation ending at lead {lead}")
    start = max(candidates)
    return start, candidates[start]


def _decode(payload, issue, start, lead):
    import numpy as np
    from eccodes import CodesInternalError, codes_get, codes_get_values, codes_new_from_message, codes_release

    valid = issue + timedelta(hours=lead)
    expected = {
        "discipline": 0, "parameterCategory": 1, "parameterNumber": 8, "units": "kg m**-2",
        "typeOfLevel": "surface", "level": 0, "stepType": "accum", "typeOfStatisticalProcessing": 1,
        "startStep": start, "endStep": lead, "stepUnits": 1,
        "gridType": "regular_ll", "Ni": WIDTH, "Nj": HEIGHT,
        "latitudeOfFirstGridPointInDegrees": 90, "longitudeOfFirstGridPointInDegrees": 0,
        "iDirectionIncrementInDegrees": 1, "jDirectionIncrementInDegrees": 1,
        "iScansNegatively": 0, "jScansPositively": 0, "jPointsAreConsecutive": 0,
        "alternativeRowScanning": 0, "numberOfMissing": 0,
        "dataDate": int(issue.strftime("%Y%m%d")), "dataTime": int(issue.strftime("%H%M")),
        "validityDate": int(valid.strftime("%Y%m%d")), "validityTime": int(valid.strftime("%H%M")),
    }
    handle = codes_new_from_message(payload)
    try:
        for key, value in expected.items():
            if codes_get(handle, key) != value:
                raise ValueError(f"NOAA precipitation field has an unexpected {key}")
        values = codes_get_values(handle)
        if len(values) != WIDTH * HEIGHT or not np.isfinite(values).all() or values.min() < 0:
            raise ValueError("GFS precipitation field is incomplete or outside the physical range")
        # GRIB packs values as (reference + integer * 2**E) * 10**(-D).
        decimal_multiplier = 10.0 ** (-codes_get(handle, "decimalScaleFactor"))
        increment = 2.0 ** codes_get(handle, "binaryScaleFactor")
        rounding = (increment / 2 + abs(codes_get(handle, "referenceValueError"))) * decimal_multiplier
        return values, rounding
    except CodesInternalError as exc:
        raise ValueError(f"Could not decode GFS precipitation: {exc}") from exc
    finally:
        codes_release(handle)


def _download(issued, lead, get, start_step=None):
    url = grids.object_url(issued, lead)
    with get(url + ".idx", timeout=20) as response:
        index = response.read().decode("ascii")
    start, bounds = locate_apcp(index, lead, start_step)
    payload, published, etag = wind._range(url, bounds, get)
    values, error = _decode(payload, issued, start, lead)
    return values, error, start, published, {
        "source": url, "etag": etag, "range": list(bounds), "sha256": hashlib.sha256(payload).hexdigest(),
        "start_step_hours": start, "end_step_hours": lead, "packing_error_mm": error,
        "available_at": published.isoformat(),
    }


def _encode(values, error):
    import numpy as np

    if values.min() < -error - 1e-10:
        raise ValueError("NOAA derived precipitation has negative accumulation beyond packing precision")
    scaled = np.rint(np.maximum(values, 0) * 100)
    if scaled.max() > 65535:
        raise ValueError("GFS three-hour precipitation exceeds cache precision range")
    return b64encode(scaled.astype("<u2").tobytes()).decode("ascii")


@lru_cache(maxsize=1024)
def _metadata(path, mtime, size):
    frame = json.loads(path.read_text())
    data = b64decode(frame.pop("pixels"), validate=True)
    if len(data) != 2 * WIDTH * HEIGHT or (frame["width"], frame["height"]) != (WIDTH, HEIGHT):
        raise ValueError("Invalid precipitation frame shape")
    if frame["id"] != path.stem or frame["format"] != FORMAT or frame["units"] != "mm":
        raise ValueError("Invalid precipitation grid identity")
    issued, available, ingested, start, valid = (
        datetime.fromisoformat(frame[key]) for key in
        ("issued_at", "available_at", "ingested_at", "accumulation_starts_at", "valid_at")
    )
    if any(clock.utcoffset() is None for clock in (issued, available, ingested, start, valid)):
        raise ValueError("Missing precipitation frame timezone")
    issue, lead = wind._parse_id(frame["id"])
    if (not issued == issue <= available <= ingested or valid != issue + timedelta(hours=lead)
            or valid - start != timedelta(hours=WINDOW_HOURS)):
        raise ValueError("Precipitation frame times conflict with its identity")
    return frame


def _cached(path):
    try:
        stat = path.stat()
        return _metadata(path, stat.st_mtime_ns, stat.st_size)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


def cache_frame(issue, lead, cache=DEFAULT_CACHE, *, get=urlopen):
    if lead < WINDOW_HOURS or lead % WINDOW_HOURS:
        raise ValueError("APCP frames require a positive lead in three-hour steps")
    path = grids.frame_path(Path(cache), issue, lead)
    if _cached(path) is not None:
        return path
    values, error, start, published, record = _download(issue, lead, get)
    records = [record]
    window_start = lead - WINDOW_HOURS
    if start > window_start:
        raise ValueError("GFS APCP does not cover the full requested window")
    if start < window_start:
        earlier, earlier_error, _, earlier_at, earlier_record = _download(issue, window_start, get, start_step=start)
        values = values - earlier
        error += earlier_error
        published = max(published, earlier_at)
        records.append(earlier_record)
    received = datetime.now(timezone.utc)
    if not issue <= published <= received:
        raise ValueError("NOAA publication falls outside the issue–receipt window")
    frame = {
        "id": path.stem, "issued_at": issue.isoformat(), "available_at": published.isoformat(),
        "ingested_at": received.isoformat(), "valid_at": (issue + timedelta(hours=lead)).isoformat(),
        "accumulation_starts_at": (issue + timedelta(hours=window_start)).isoformat(),
        "accumulation_hours": WINDOW_HOURS, "source": record["source"], "source_records": records,
        "forecast": "NOAA GFS surface total liquid-equivalent precipitation",
        "width": WIDTH, "height": HEIGHT, "resolution_degrees": 1,
        "format": FORMAT, "units": "mm", "pixels": _encode(values, error),
    }
    _write(path, json.dumps(frame, separators=(",", ":"), allow_nan=False).encode())
    return path


def manifest(cache=DEFAULT_CACHE):
    return [frame for path in sorted(Path(cache).glob("20????????-f???.json"))
            if (frame := _cached(path)) is not None]


def cache_matching(frame_ids, cache=DEFAULT_CACHE, *, get=urlopen):
    frames, errors = [], {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = {}
        for ident in dict.fromkeys(frame_ids):
            try:
                issue, lead = wind._parse_id(ident)
            except (ValueError, TypeError) as error:
                errors[str(ident)] = str(error)
                continue
            if lead >= WINDOW_HOURS and lead % WINDOW_HOURS == 0:
                jobs[ident] = pool.submit(cache_frame, issue, lead, cache, get=get)
        for ident, job in jobs.items():
            try:
                frames.append(job.result().stem)
            except (OSError, ValueError, KeyError) as error:
                errors[ident] = str(error)
    return {"frames": frames, "errors": errors}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path(".data"))
    parser.add_argument("--day", type=date.fromisoformat)
    parser.add_argument("--frame", nargs="+")
    args = parser.parse_args()
    frames = grids.manifest(args.data_dir / "grids")
    if args.day:
        frames = grids.replay_frames(frames, args.day)
    print(json.dumps(cache_matching(args.frame or (frame["id"] for frame in frames),
                                     args.data_dir / "precipitation"), indent=2))


if __name__ == "__main__":
    main()
