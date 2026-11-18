"""Create a compact map of native one-hour MRMS radar precipitation estimates."""
from __future__ import annotations

import argparse
from base64 import b64decode, b64encode
from datetime import timedelta
from functools import lru_cache
import gzip
from hashlib import sha256
import json
from pathlib import Path

from . import expansion_mrms, grids, wind
from .history import _write
from .nbm import iso, utc


DEFAULT_CACHE = Path(".data/radar")
PRODUCT = "RadarOnly_QPE_01H"
FORMAT = "u16le-0.01mm-mrms"
NATIVE_SHAPE = (3500, 7000)
SIZE = 25
SHAPE = (NATIVE_SHAPE[0] // SIZE, NATIVE_SHAPE[1] // SIZE)
NO_DATA = 65535


def _averages(values, missing):
    import numpy as np

    cells = values.reshape(SHAPE[0], SIZE, SHAPE[1], SIZE)
    present = np.isfinite(cells) & (cells >= 0) & (cells != missing)
    counts = present.sum(axis=(1, 3))
    if not counts.any():
        raise ValueError("No covered MRMS map cells")
    sum_mm = np.sum(cells, axis=(1, 3), where=present, dtype=np.float64)
    mean_mm = np.divide(sum_mm, counts, out=np.zeros(SHAPE), where=counts > 0)
    packed = np.rint(mean_mm * 100)
    if packed.max() >= NO_DATA:
        raise ValueError("MRMS 1-hour map amounts exceed cache precision")
    return np.where(counts > 0, packed, NO_DATA).astype("<u2")


def _decode(body, end):
    import numpy as np
    from eccodes import CodesInternalError, codes_get, codes_get_values, codes_new_from_message, codes_release

    expected = {
        "discipline": 209, "parameterCategory": 6, "parameterNumber": 2, "stepType": "instant",
        "gridType": "regular_ll", "Ni": NATIVE_SHAPE[1], "Nj": NATIVE_SHAPE[0],
        "latitudeOfFirstGridPointInDegrees": 54.995, "longitudeOfFirstGridPointInDegrees": 230.005,
        "iDirectionIncrementInDegrees": .01, "jDirectionIncrementInDegrees": .01,
        "iScansNegatively": 0, "jScansPositively": 0, "jPointsAreConsecutive": 0, "alternativeRowScanning": 0,
        "dataDate": int(end.strftime("%Y%m%d")), "dataTime": int(end.strftime("%H%M")),
        "validityDate": int(end.strftime("%Y%m%d")), "validityTime": int(end.strftime("%H%M")),
    }
    handle = codes_new_from_message(gzip.decompress(body))
    try:
        for key, value in expected.items():
            if codes_get(handle, key) != value:
                raise ValueError(f"Unexpected native MRMS radar field {key}")
        values = codes_get_values(handle).astype(np.float32, copy=False)
        return _averages(values, float(codes_get(handle, "missingValue")))
    except CodesInternalError as exc:
        raise ValueError(f"Unable to decode native MRMS radar field: {exc}") from exc
    finally:
        codes_release(handle)


@lru_cache(maxsize=1024)
def _metadata(path, mtime, size):
    frame = json.loads(path.read_bytes())
    raw = b64decode(frame.pop("pixels"), validate=True)
    end, lead = wind._parse_id(path.stem)
    if (frame["id"] != path.stem or frame["format"] != FORMAT or (frame["height"], frame["width"]) != SHAPE
            or len(raw) != 2 * SHAPE[0] * SHAPE[1] or utc(frame["valid_at"]) != end or lead != 0
            or utc(frame["accumulation_starts_at"]) != end - timedelta(hours=1)
            or not end <= utc(frame["available_at"]) <= utc(frame["ingested_at"])):
        raise ValueError("Invalid MRMS radar map metadata")
    return frame


def _cached(path):
    try:
        stat = path.stat()
        return _metadata(path, stat.st_mtime_ns, stat.st_size)
    except (OSError, KeyError, ValueError, TypeError):
        return None


def cache_frame(data, end):
    data = Path(data)
    path = grids.frame_path(data / "radar", end, 0)
    if _cached(path) is not None:
        return path
    if (data / "collector" / "paused").exists():
        raise TimeoutError("Collection stopped")
    native = expansion_mrms.get_frame(data, PRODUCT, end)
    raw = (data / native["raw_path"]).read_bytes()
    if sha256(raw).hexdigest() != native["raw_sha256"]:
        raise ValueError("The stored MRMS radar field differs from its original digest")
    cells = _decode(raw, end)
    result = {
        "id": path.stem, "product": PRODUCT, "description": "NOAA MRMS one-hour radar estimate", "format": FORMAT,
        "units": "mm", "native_resolution_degrees": .01, "resolution_degrees": .25,
        "grid_bounds": {"west": -130, "east": -60, "south": 20, "north": 55},
        "resampling": "Mean of valid original cells in each quarter-degree map cell",
        "width": SHAPE[1], "height": SHAPE[0], "issued_at": native["available_at"], "ingested_at": native["received_at"],
        **{key: native[key] for key in ("available_at", "accumulation_starts_at", "valid_at", "source", "source_etag", "raw_path", "raw_sha256")},
        "pixels": b64encode(cells.tobytes()).decode("ascii"),
    }
    _write(path, json.dumps(result, separators=(",", ":"), allow_nan=False).encode())
    return path


def manifest(cache=DEFAULT_CACHE):
    return [frame for path in sorted(Path(cache).glob("20????????-f000.json")) if (frame := _cached(path)) is not None]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--start", type=utc, required=True)
    parser.add_argument("--end", type=utc)
    args = parser.parse_args()
    first, last = args.start, args.end or args.start
    if last < first or any(t.minute or t.second or t.microsecond for t in (first, last)):
        parser.error("Choose an ascending range of whole UTC hours")
    added, missing = [], {}
    for hour in range(int((last - first).total_seconds() // 3600) + 1):
        clock = first + timedelta(hours=hour)
        try:
            added.append(cache_frame(args.data, clock).stem)
        except TimeoutError as exc:
            missing[iso(clock)] = str(exc)
            break
        except (OSError, TypeError, ValueError, KeyError) as exc:
            missing[iso(clock)] = str(exc)
    print(json.dumps({"frames": added, "missing": missing}, indent=2))
    return 0 if added else 1


if __name__ == "__main__":
    raise SystemExit(main())
