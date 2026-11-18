"""Cache as-issued NOAA GFS Wave significant height and its native wet mask."""
from __future__ import annotations

import argparse
from base64 import b64decode, b64encode
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from hashlib import sha256
import json
import math
from pathlib import Path
from urllib.request import urlopen

from . import grids, wind
from .history import _write


DEFAULT_CACHE = Path(".data/waves")
DOCS = "https://www.nco.ncep.noaa.gov/pmb/products/gfs/"
ORIGIN = "https://noaa-gfs-bdp-pds.s3.amazonaws.com"
FORMAT = "u16le-0.01m-ocean"
NATIVE_SHAPE = (721, 1440)
SHAPE = (181, 360)
NO_VALUE = 65535
LEADS = range(0, 37, 3)
UTC = timezone.utc


def object_url(issue, lead):
    return (f"{ORIGIN}/gfs.{issue:%Y%m%d}/{issue:%H}/wave/gridded/"
            f"gfswave.t{issue:%H}z.global.0p25.f{lead:03d}.grib2")


def locate_height(index, lead):
    period = f"{lead} hour fcst" if lead else "anl"
    key = f":HTSGW:surface:{period}:"
    lines = index.splitlines()
    choices = [(int(a.split(":")[1]), int(b.split(":")[1]) - 1)
               for a, b in zip(lines, lines[1:]) if key in a]
    if len(choices) != 1:
        raise ValueError("Expected one exact GFS Wave significant height record")
    first, last = choices[0]
    if not 0 <= first <= last < first + 4 * 2**20:
        raise ValueError("Invalid GFS Wave indexed range")
    return first, last


def native_grid(payload, issue, lead, *, parameter=3, level_type="surface", level=None):
    import numpy as np
    from eccodes import CodesInternalError, codes_get, codes_get_values, codes_new_from_message, codes_release

    expected = {
        "discipline": 10, "parameterCategory": 0, "parameterNumber": parameter, "units": "m",
        "typeOfLevel": level_type, "stepType": "instant", "startStep": lead, "endStep": lead, "stepUnits": 1,
        "gridType": "regular_ll", "Ni": NATIVE_SHAPE[1], "Nj": NATIVE_SHAPE[0],
        "latitudeOfFirstGridPointInDegrees": 90, "longitudeOfFirstGridPointInDegrees": 0,
        "iDirectionIncrementInDegrees": .25, "jDirectionIncrementInDegrees": .25,
        "iScansNegatively": 0, "jScansPositively": 0, "jPointsAreConsecutive": 0, "alternativeRowScanning": 0,
        "bitmapPresent": 1, "dataDate": int(issue.strftime("%Y%m%d")), "dataTime": int(issue.strftime("%H%M")),
        "validityDate": int((issue + timedelta(hours=lead)).strftime("%Y%m%d")),
        "validityTime": int((issue + timedelta(hours=lead)).strftime("%H%M")),
    }
    if level is not None:
        expected["level"] = level
    handle = codes_new_from_message(payload)
    try:
        for key, value in expected.items():
            if codes_get(handle, key) != value:
                raise ValueError(f"Unexpected GFS Wave height field {key}")
        amounts = codes_get_values(handle).reshape(NATIVE_SHAPE)
        wet = amounts != codes_get(handle, "missingValue")
        if wet.sum() != amounts.size - codes_get(handle, "numberOfMissing"):
            raise ValueError("GFS Wave wet-mask count disagrees with the native bitmap")
        if not wet.any() or not np.isfinite(amounts[wet]).all() or amounts[wet].min() < 0 or amounts[wet].max() > 60:
            raise ValueError("GFS Wave returned invalid wet-cell heights")
        return np.where(wet, amounts, np.nan)
    except CodesInternalError as exc:
        raise ValueError(f"Unable to decode GFS Wave height field: {exc}") from exc
    finally:
        codes_release(handle)


def _encode(values):
    import numpy as np

    # The map selects native cells centered on whole degrees. It preserves the
    # native missing mask and keeps the original quarter-degree field on disk.
    display = values[::4, ::4]
    if display.shape != SHAPE:
        raise ValueError("Unexpected GFS Wave display grid")
    packed = np.where(np.isfinite(display), np.rint(100 * display), NO_VALUE).astype("<u2")
    return b64encode(packed.tobytes()).decode("ascii")


@lru_cache(maxsize=1024)
def _metadata(path, mtime, size):
    frame = json.loads(path.read_bytes())
    encoded = b64decode(frame.pop("pixels"), validate=True)
    if (len(encoded) != 2 * SHAPE[0] * SHAPE[1] or (frame["height"], frame["width"]) != SHAPE
            or frame["format"] != FORMAT or frame["units"] != "m" or frame["id"] != path.stem):
        raise ValueError("Invalid GFS Wave frame metadata")
    issue, lead = wind._parse_id(path.stem)
    issued, available, ingested, valid = (datetime.fromisoformat(frame[key]) for key in
                                          ("issued_at", "available_at", "ingested_at", "valid_at"))
    if (any(clock.utcoffset() is None for clock in (issued, available, ingested, valid))
            or issue != issued or not issued <= available <= ingested or valid != issued + timedelta(hours=lead)):
        raise ValueError("Invalid GFS Wave issue, publication, receipt or valid clock")
    return frame


def _cached(path):
    try:
        stat = path.stat()
        return _metadata(path, stat.st_mtime_ns, stat.st_size)
    except (OSError, ValueError, TypeError, KeyError):
        return None


def sample_grid(grid, lat, lon, samples_per_degree=4):
    row = min(180 * samples_per_degree, math.floor((90 - lat) * samples_per_degree + .5))
    col = math.floor((lon % 360) * samples_per_degree + .5) % (360 * samples_per_degree)
    position = 90 - row / samples_per_degree, (col / samples_per_degree + 180) % 360 - 180
    stride = 4 // samples_per_degree
    value = float(grid[row * stride, col * stride])
    return value if math.isfinite(value) else None, position


def interpolation_corners(grid, lat, lon):
    y, x = (90 - lat) * 4, (lon % 360) * 4
    i, j = math.floor(y), math.floor(x)
    output = []
    for row, rw in ((i, 1 - (y - i)), (min(i + 1, grid.shape[0] - 1), y - i)):
        for col, cw in ((j, 1 - (x - j)), ((j + 1) % grid.shape[1], x - j)):
            weight = rw * cw
            if weight > 1e-12:
                raw = float(grid[row, col])
                output.append({"lat": 90 - row / 4, "lon": (col / 4 + 180) % 360 - 180,
                               "weight": weight, "height_m": raw if math.isfinite(raw) else None})
    return output


@lru_cache(maxsize=6)
def _original(path, ident, digest):
    body = path.read_bytes()
    if sha256(body).hexdigest() != digest:
        raise ValueError("GFS Wave native field differs from its archived digest")
    issue, lead = wind._parse_id(ident)
    return native_grid(body, issue, lead)


def point(cache, ident, lat, lon):
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise ValueError("Choose valid latitude and longitude")
    path = Path(cache) / (ident + ".json")
    frame = _cached(path)
    if frame is None or not path.with_suffix(".grib2").is_file():
        return None
    grid = _original(path.with_suffix(".grib2"), ident, frame["sha256"])
    height, (cell_lat, cell_lon) = sample_grid(grid, lat, lon)
    cells = interpolation_corners(grid, lat, lon)
    interpolated = sum(row["weight"] * row["height_m"] for row in cells) if all(row["height_m"] is not None for row in cells) else None
    return {"id": ident, "height_m": height, "cell_lat": cell_lat, "cell_lon": cell_lon,
            "interpolated_height_m": interpolated, "interpolation_corners": len(cells),
            "resolution_degrees": .25, "valid_at": frame["valid_at"], "available_at": frame["available_at"],
            "source_sha256": frame["sha256"]}


def cache_frame(data, issue, lead, *, get=urlopen):
    root = Path(data)
    path = grids.frame_path(root / "waves", issue, lead)
    if _cached(path) is not None:
        return path

    def collect(request, *, timeout):
        if (root / "collector" / "paused").exists():
            raise TimeoutError("Collection stopped")
        return get(request, timeout=timeout)

    url = object_url(issue, lead)
    with collect(url + ".idx", timeout=20) as response:
        bounds = locate_height(response.read(512 * 1024).decode("ascii"), lead)
    payload, published, tag = wind._range(url, bounds, collect)
    received = datetime.now(UTC)
    if not issue <= published <= received:
        raise ValueError("GFS Wave publication clock precedes issue or follows receipt")
    raw = path.with_suffix(".grib2")
    grid = native_grid(payload, issue, lead)
    result = {"id": path.stem, "forecast": "NOAA GFS Wave significant wave height", "source": url,
              "source_index": url + ".idx", "source_etag": tag, "range": list(bounds), "sha256": sha256(payload).hexdigest(),
              "raw": str(raw.relative_to(root)), "issued_at": issue.isoformat(), "available_at": published.isoformat(),
              "ingested_at": received.isoformat(), "valid_at": (issue + timedelta(hours=lead)).isoformat(),
              "width": SHAPE[1], "height": SHAPE[0], "resolution_degrees": 1, "native_resolution_degrees": .25,
              "format": FORMAT, "units": "m", "pixels": _encode(grid)}
    _write(raw, payload)
    _write(path, json.dumps(result, separators=(",", ":"), allow_nan=False).encode())
    return path


def manifest(cache=DEFAULT_CACHE):
    return [frame for path in sorted(Path(cache).glob("20????????-f???.json"))
            if (frame := _cached(path)) is not None]


def cache_cycle(data, issue, leads=LEADS):
    frames, errors = [], {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = {lead: pool.submit(cache_frame, data, issue, lead) for lead in leads}
        for lead, job in jobs.items():
            try:
                frames.append(job.result().stem)
            except (OSError, TypeError, ValueError, KeyError) as error:
                errors[lead] = str(error)
    return {"frames": frames, "errors": errors}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--issue", type=datetime.fromisoformat, required=True)
    parser.add_argument("--leads", nargs="+", type=int, default=LEADS)
    args = parser.parse_args()
    issue = args.issue
    if issue.utcoffset() != timedelta(0) or issue.hour not in (0, 6, 12, 18) or issue.minute or issue.second or issue.microsecond:
        parser.error("Choose an exact 00Z, 06Z, 12Z, or 18Z issue")
    if any(not 0 <= lead <= 384 or lead % 3 for lead in args.leads):
        parser.error("Choose three-hour leads from 0 through 384")
    result = cache_cycle(args.data, issue, args.leads)
    print(json.dumps(result, indent=2))
    return 0 if result["frames"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
