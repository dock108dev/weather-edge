"""Extract native 3 km HRRR daily precipitation over the exact UTC−06 climate day."""
from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta
from hashlib import sha256
import json
import math
from pathlib import Path
from urllib.request import urlopen

from . import gefs, hrrr
from .history import _write
from .nbm import iso, utc


LEADS = (6, 30)


def range_from_index(index, lead):
    field = f":APCP:surface:0-{lead} hour acc fcst:"
    lines = index.splitlines()
    matches = [(int(a.split(":")[1]), int(b.split(":")[1]) - 1)
               for a, b in zip(lines, lines[1:]) if field in a]
    if len(matches) != 1:
        raise ValueError("Expected one HRRR cumulative precipitation field")
    first, last = matches[0]
    if not 0 <= first <= last < first + 12 * 2**20:
        raise ValueError("HRRR cumulative precipitation range is invalid")
    return first, last


def decode_cells(payload, issue, lead):
    from eccodes import CodesInternalError, codes_get, codes_grib_find_nearest, codes_new_from_message, codes_release

    valid = issue + timedelta(hours=lead)
    expected = {
        "discipline": 0, "parameterCategory": 1, "parameterNumber": 8, "units": "kg m**-2",
        "typeOfLevel": "surface", "level": 0, "stepType": "accum", "typeOfStatisticalProcessing": 1,
        "startStep": 0, "endStep": lead, "stepUnits": 1, "gridType": "lambert", "Nx": 1799, "Ny": 1059,
        "DxInMetres": 3000.0, "DyInMetres": 3000.0,
        "dataDate": int(issue.strftime("%Y%m%d")), "dataTime": int(issue.strftime("%H%M")),
        "validityDate": int(valid.strftime("%Y%m%d")), "validityTime": int(valid.strftime("%H%M")),
    }
    handle = codes_new_from_message(payload)
    try:
        for key, value in expected.items():
            if codes_get(handle, key) != value:
                raise ValueError(f"Unexpected NOAA HRRR precipitation {key}")
        cells = {}
        for station in gefs.CANDIDATES:
            lat, lon, _, _ = hrrr.STATIONS[station]
            cell, = codes_grib_find_nearest(handle, lat, lon, is_lsm=False, npoints=1)
            amount = cell["value"]
            if not math.isfinite(amount) or amount < 0:
                raise ValueError(f"Invalid HRRR cumulative liquid amount at {station}")
            cells[station] = {"grid_index": cell["index"], "distance_km": cell["distance"],
                              "grid_lat": cell["lat"], "grid_lon": ((cell["lon"] + 180) % 360) - 180,
                              "cumulative_mm": amount}
        decimal_multiplier = 10.0 ** -codes_get(handle, "decimalScaleFactor")
        rounding = (2.0 ** codes_get(handle, "binaryScaleFactor") / 2 + abs(codes_get(handle, "referenceValueError")))
        return cells, rounding * decimal_multiplier
    except CodesInternalError as exc:
        raise ValueError(f"Unable to decode HRRR cumulative precipitation: {exc}") from exc
    finally:
        codes_release(handle)


def frame_path(data, issue, lead):
    return Path(data) / "hrrr-precipitation" / issue.strftime("%Y%m%d%H") / f"f{lead:02d}.json"


def cache_frame(data, issue, lead, *, get=urlopen):
    path = frame_path(data, issue, lead)
    try:
        return json.loads(path.read_bytes())
    except FileNotFoundError:
        pass
    url = hrrr.object_url(issue, lead)
    with hrrr._request(data, url + ".idx", get) as reply:
        first, last = range_from_index(reply.read(512 * 1024).decode("ascii"), lead)
    body, published, ingested, tag = hrrr.read_range(data, issue, url, first, last, get)
    cells, rounding = decode_cells(body, issue, lead)
    raw = path.with_suffix(".grib2")
    result = {"model": "NOAA HRRR CONUS", "field": "APCP cumulative surface water equivalent", "resolution_m": 3000,
              "issued_at": iso(issue), "available_at": iso(published), "ingested_at": iso(ingested),
              "valid_at": iso(issue + timedelta(hours=lead)), "packing_error_mm": rounding,
              "source": url, "source_index": url + ".idx", "source_etag": tag, "byte_range": [first, last],
              "sha256": sha256(body).hexdigest(), "raw": str(raw.relative_to(data)), "stations": cells}
    _write(raw, body)
    _write(path, json.dumps(result, allow_nan=False, separators=(",", ":")).encode())
    return result


def day_amount(data, day, station):
    if station not in gefs.CANDIDATES or hrrr.STATIONS[station][3] != -6:
        return None
    issue = datetime.combine(day, time.min, hrrr.UTC)
    start = issue + timedelta(hours=LEADS[0])
    frames = []
    for lead in LEADS:
        try:
            frame = json.loads(frame_path(data, issue, lead).read_bytes())
        except FileNotFoundError:
            return None
        if (utc(frame["issued_at"]) != issue or utc(frame["valid_at"]) != issue + timedelta(hours=lead)
                or not issue <= utc(frame["available_at"]) <= start):
            return None
        frames.append(frame)
    first, last = (frame["stations"][station] for frame in frames)
    if first["grid_index"] != last["grid_index"]:
        return None
    amount = last["cumulative_mm"] - first["cumulative_mm"]
    uncertainty = sum(frame["packing_error_mm"] for frame in frames)
    if amount < -uncertainty:
        return None
    return {"amount_mm": max(0.0, amount), "packing_error_mm": uncertainty, "distance_km": first["distance_km"],
            "grid_lat": first["grid_lat"], "grid_lon": first["grid_lon"], "available_at": max(f["available_at"] for f in frames),
            "starts_at": frames[0]["valid_at"], "ends_at": frames[1]["valid_at"],
            "sources": [{key: frame[key] for key in ("source", "raw", "sha256", "available_at", "valid_at")} for frame in frames]}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data", type=Path, default=Path(".data"))
    p.add_argument("--start", type=date.fromisoformat, required=True)
    p.add_argument("--end", type=date.fromisoformat)
    p.add_argument("--workers", type=int, choices=range(1, 9), default=5)
    args = p.parse_args()
    counts, errors = hrrr.backfill(args.data, args.start, args.end or args.start, args.workers, leads=LEADS, fetch=cache_frame)
    print(json.dumps({"days": counts, "issues": errors}, indent=2))
    return 0 if any(counts.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
