"""Preserve HRRR surface radiation fields with original forecast and acquisition times."""

from __future__ import annotations

import argparse
from datetime import timedelta
from email.utils import parsedate_to_datetime
from hashlib import sha256
import json
from math import isfinite
from pathlib import Path
import re
from urllib.request import urlopen

from . import expansion_weather, hrrr
from .history import _write
from .nbm import iso, utc

FIELDS = {"DLWRF": 3, "ULWRF": 4}
STATIONS = ("KNYC", "KMDW", "KORD", "KPHL", "KBOS", "KDEN", "KATL")
DOCS = "https://www.nco.ncep.noaa.gov/pmb/docs/grib2/grib2_doc/grib2_table4-2-0-5.shtml"
LEAD = 2
FORMAT = re.compile(r":(DLWRF|ULWRF):surface:(\d+) hour fcst:")


def _ranges(index, lead):
    output = {}
    lines = index.splitlines()
    for line, next_line in zip(lines, lines[1:]):
        if not (match := FORMAT.search(line)) or int(match[2]) != lead:
            continue
        first, end = int(line.split(":")[1]), int(next_line.split(":")[1])
        if match[1] in output or not 0 <= first < end <= first + 4 * 1024 * 1024:
            raise ValueError("Unexpected HRRR surface radiation range")
        output[match[1]] = (first, end - 1)
    if set(output) != set(FIELDS):
        raise ValueError("Both native HRRR longwave fields are required")
    return output


def _decode(body, parameter, issue, lead):
    from eccodes import CodesInternalError, codes_get, codes_grib_find_nearest, codes_new_from_message, codes_release

    valid = issue + timedelta(hours=lead)
    expected = {"discipline": 0, "parameterCategory": 5, "parameterNumber": parameter, "units": "W m**-2",
                "typeOfLevel": "surface", "level": 0, "stepType": "instant", "startStep": lead, "endStep": lead,
                "gridType": "lambert", "Nx": 1799, "Ny": 1059, "DxInMetres": 3000.0, "DyInMetres": 3000.0,
                "dataDate": int(issue.strftime("%Y%m%d")), "dataTime": int(issue.strftime("%H%M")),
                "validityDate": int(valid.strftime("%Y%m%d")), "validityTime": int(valid.strftime("%H%M"))}
    handle = codes_new_from_message(body)
    try:
        for key, value in expected.items():
            if codes_get(handle, key) != value:
                raise ValueError(f"Unexpected HRRR longwave field {key}")
        result = {}
        for station in STATIONS:
            lat, lon, *_ = expansion_weather.STATIONS[station]
            (match,) = codes_grib_find_nearest(handle, lat, lon, is_lsm=False, npoints=1)
            flux = match["value"]
            if not isfinite(flux) or not 0 <= flux <= 1200:
                raise ValueError(f"Unusable HRRR surface radiation for {station}")
            result[station] = {"value": flux, "grid_lat": match["lat"], "grid_lon": ((match["lon"] + 180) % 360) - 180,
                               "grid_index": match["index"], "distance_km": match["distance"]}
        return result
    except CodesInternalError as error:
        raise ValueError(f"Could not decode HRRR longwave radiation: {error}") from error
    finally:
        codes_release(handle)


def _path(data, issue, lead):
    return Path(data) / "expansion" / "hrrr-longwave" / f"{issue:%Y%m%d%H}" / f"f{lead:02d}.json"


def frame(data, as_of, *, fetch=False, get=urlopen):
    """Use the two-hour instantaneous fields valid at the fixed decision hour."""
    data, decision = Path(data), utc(as_of)
    if any((decision.minute, decision.second, decision.microsecond)):
        raise ValueError("HRRR longwave decisions use complete UTC hours")
    issue = decision - timedelta(hours=LEAD)
    path = _path(data, issue, LEAD)
    if path.is_file():
        cached = json.loads(path.read_bytes())
        return cached if utc(cached["available_at"]) <= decision else None
    if not fetch:
        return None
    url = hrrr.object_url(issue, LEAD)
    with hrrr._request(data, url + ".idx", get) as reply:
        index = reply.read(512 * 1024)
        index_available = utc(parsedate_to_datetime(reply.headers["Last-Modified"]))
    intervals = _ranges(index.decode("ascii"), LEAD)
    picked, metadata = {}, {}
    for field, (first, last) in intervals.items():
        payload, published, obtained, etag = hrrr.read_range(data, issue, url, first, last, get)
        picked[field] = _decode(payload, FIELDS[field], issue, LEAD)
        raw_path = path.with_name(f"{path.stem}-{field}.grib2")
        _write(raw_path, payload)
        metadata[field] = {"path": str(raw_path.relative_to(data)), "sha256": sha256(payload).hexdigest(), "etag": etag,
                           "byte_range": [first, last], "published_at": iso(published), "received_at": iso(obtained)}
    if len({field["etag"] for field in metadata.values()}) != 1 or not next(iter(metadata.values()))["etag"]:
        raise ValueError("HRRR surface radiation byte ranges must share one underlying object")
    stations = {}
    for station in STATIONS:
        down, up = picked["DLWRF"][station], picked["ULWRF"][station]
        if down["grid_index"] != up["grid_index"]:
            raise ValueError("HRRR up and down radiation use incompatible cells")
        stations[station] = {**{key: value for key, value in down.items() if key != "value"},
                            "downward_w_m2": down["value"], "upward_w_m2": up["value"], "net_downward_w_m2": down["value"] - up["value"]}
    raw_index = path.with_suffix(".idx")
    _write(raw_index, index)
    available = max(index_available, *[utc(field["published_at"]) for field in metadata.values()])
    ingested = max(utc(field["received_at"]) for field in metadata.values())
    if not issue <= available <= ingested:
        raise ValueError("Incompatible HRRR longwave receipt clocks")
    result = {"model": "NOAA HRRR CONUS", "field": "Instantaneous surface longwave radiation flux", "resolution_m": 3000,
              "source": url, "issued_at": iso(issue), "valid_at": iso(decision), "available_at": iso(available),
              "received_at": iso(ingested), "native_units": "W m⁻²", "lead_hours": LEAD,
              "source_index": {"url": url + ".idx", "path": str(raw_index.relative_to(data)), "sha256": sha256(index).hexdigest(),
                              "published_at": iso(index_available)},
              "native_fields": metadata, "stations": stations}
    _write(path, (json.dumps(result, separators=(",", ":"), allow_nan=False) + "\n").encode())
    return result if available <= decision else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--as-of", type=utc, required=True)
    parser.add_argument("--cached", action="store_true")
    args = parser.parse_args()
    print(json.dumps(frame(args.data, args.as_of, fetch=not args.cached), indent=2))


if __name__ == "__main__":
    main()
