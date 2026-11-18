"""Archive as-issued NOAA HRRR 2 m temperature at the nearest station grid cell."""
from __future__ import annotations

import argparse
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time, timedelta, timezone
from email.utils import parsedate_to_datetime
from functools import lru_cache
from hashlib import sha256
import json
import math
from pathlib import Path
import re
from threading import Lock
from urllib.request import Request, urlopen

from .archive import date_range
from .expansion_weather import STATIONS
from .history import _write
from .market import iso, parse_time

ORIGIN = "https://noaa-hrrr-bdp-pds.s3.amazonaws.com"
DOCS = "https://registry.opendata.aws/noaa-hrrr-pds/"
UTC = timezone.utc
FIELD = re.compile(r":TMP:2 m above ground:(\d+) hour fcst:")
# 05Z to 07Z the next day covers a complete local-standard day across the contiguous U.S.
LEADS = range(5, 32)
INTRADAY_LEADS = {12: range(6, 20), 18: range(4, 14)}
_GEOMETRY = OrderedDict()
_GEOMETRY_LOCK = Lock()
_GEOMETRY_LIMIT = 2048


class SourcePaused(Exception):
    """New public collection was stopped before a request."""


def object_url(issue, lead):
    run = parse_time(issue)
    return f"{ORIGIN}/hrrr.{run:%Y%m%d}/conus/hrrr.t{run:%H}z.wrfsfcf{lead:02d}.grib2"


def locate_temperature(index, lead):
    candidates = []
    lines = index.splitlines()
    for entry, following in zip(lines, lines[1:]):
        match = FIELD.search(entry)
        if match and int(match[1]) == lead:
            start = int(entry.split(":")[1])
            stop = int(following.split(":")[1])
            if not 0 <= start < stop <= start + 3 * 1024 * 1024:
                raise ValueError("Invalid NOAA HRRR temperature byte range")
            candidates.append((start, stop - 1))
    if len(candidates) != 1:
        raise ValueError(f"Expected one HRRR 2 m temperature field at lead {lead}")
    return candidates[0]


def _native_locations(handle, stations):
    from eccodes import codes_get, codes_grib_find_nearest

    # Section 3 identifies the full projection and scan order. Field values and
    # clocks are decoded from each original message, independently of this cache.
    grid = codes_get(handle, "md5Section3")
    locations = {}
    with _GEOMETRY_LOCK:
        for station, (lat, lon, _, _) in stations.items():
            key = grid, lat, lon
            if key not in _GEOMETRY:
                nearest, = codes_grib_find_nearest(handle, lat, lon, is_lsm=False, npoints=1)
                _GEOMETRY[key] = {
                    "station_lat": lat, "station_lon": lon, "grid_lat": nearest["lat"],
                    "grid_lon": ((nearest["lon"] + 180) % 360) - 180,
                    "grid_index": nearest["index"], "distance_km": nearest["distance"],
                }
                if len(_GEOMETRY) > _GEOMETRY_LIMIT:
                    _GEOMETRY.popitem(last=False)
            _GEOMETRY.move_to_end(key)
            locations[station] = _GEOMETRY[key]
    return locations


def decode_station_cells(body, issue, lead, *, stations=STATIONS):
    from eccodes import CodesInternalError, codes_get, codes_get_double_elements, codes_new_from_message, codes_release

    valid = issue + timedelta(hours=lead)
    expected = {
        "discipline": 0, "parameterCategory": 0, "parameterNumber": 0, "units": "K",
        "typeOfLevel": "heightAboveGround", "level": 2, "stepType": "instant",
        "startStep": lead, "endStep": lead, "gridType": "lambert", "Nx": 1799, "Ny": 1059,
        "DxInMetres": 3000.0, "DyInMetres": 3000.0,
        "dataDate": int(issue.strftime("%Y%m%d")), "dataTime": int(issue.strftime("%H%M")),
        "validityDate": int(valid.strftime("%Y%m%d")), "validityTime": int(valid.strftime("%H%M")),
    }
    handle = codes_new_from_message(body)
    try:
        for key, value in expected.items():
            if codes_get(handle, key) != value:
                raise ValueError(f"Unexpected NOAA HRRR GRIB2 {key}")
        locations = _native_locations(handle, stations)
        if not locations:
            return {}
        values = codes_get_double_elements(handle, "values", [cell["grid_index"] for cell in locations.values()])
        cells = {}
        for (station, cell), kelvin in zip(locations.items(), values, strict=True):
            if not math.isfinite(kelvin) or not 150 < kelvin < 350:
                raise ValueError(f"Unusable HRRR near-surface temperature for {station}")
            cells[station] = {
                **cell,
                "temp_k": kelvin, "temp_f": (kelvin - 273.15) * 1.8 + 32,
            }
        return cells
    except CodesInternalError as error:
        raise ValueError(f"Could not decode NOAA HRRR temperature: {error}") from error
    finally:
        codes_release(handle)


def frame_path(data, issue, lead):
    return Path(data) / "hrrr" / issue.strftime("%Y%m%d%H") / f"f{lead:02d}.json"


@lru_cache(maxsize=1024)
def _frame(path, version):
    return json.loads(path.read_bytes())


def _local_projection(path, digest):
    extra = path.with_suffix(".stations.json")
    try:
        row = _frame(extra, extra.stat().st_mtime_ns)
        return row["stations"] if row["sha256"] == digest else {}
    except (OSError, KeyError, TypeError, ValueError):
        return {}


def extend_station_projection(data, issue, lead, stations):
    """Project new stations from the already received GRIB field and retain its original evidence."""
    path = frame_path(data, issue, lead)
    frame = _frame(path, path.stat().st_mtime_ns)
    extra = _local_projection(path, frame["sha256"])
    missing = {code: STATIONS[code] for code in stations if code not in frame["stations"] and code not in extra}
    if not missing:
        return 0
    body = path.with_suffix(".grib2").read_bytes()
    if sha256(body).hexdigest() != frame["sha256"]:
        raise ValueError("Original HRRR field differs from its retained digest")
    projected = decode_station_cells(body, issue, lead, stations=missing)
    _write(path.with_suffix(".stations.json"), json.dumps({"sha256": frame["sha256"], "stations": {**extra, **projected}},
                                                         separators=(",", ":"), allow_nan=False).encode())
    return len(projected)


def station_trace(data, station, day, start, end, *, cycle=0, provenance=False):
    if station not in STATIONS:
        return []
    issue = datetime.combine(date.fromisoformat(day), time(cycle), UTC)
    first, last = parse_time(start), parse_time(end)
    points = []
    for lead in INTRADAY_LEADS.get(cycle, LEADS):
        valid = issue + timedelta(hours=lead)
        if not first <= valid < last:
            continue
        path = frame_path(data, issue, lead)
        try:
            field = _frame(path, path.stat().st_mtime_ns)
            cell = field["stations"].get(station) or _local_projection(path, field["sha256"]).get(station)
            if cell is None:
                continue
            assert_stamp = field["valid_at"]
            if parse_time(assert_stamp) != valid or parse_time(field["issued_at"]) != issue:
                continue
            if not issue <= parse_time(field["available_at"]) <= parse_time(field["ingested_at"]):
                continue
            keys = ("source", "issued_at", "available_at", "ingested_at", "valid_at")
            if provenance:
                keys += ("raw", "sha256", "source_etag", "byte_range", "lead_hours")
            points.append({**cell, **{key: field[key] for key in keys}})
        except (OSError, KeyError, TypeError, ValueError):
            continue
    return points


def complete_remaining_trace(data, station, day, at, close, *, local_receipt=False, cycles=None):
    """Choose one operational run with every remaining hour published at the decision."""
    stamp = parse_time(at)
    end = parse_time(close)
    first = stamp.replace(minute=0, second=0, microsecond=0)
    if first < stamp:
        first += timedelta(hours=1)
    hours = [first + timedelta(hours=index) for index in range(max(0, (end - first) // timedelta(hours=1)))]
    if not hours:
        return []
    for cycle in reversed(INTRADAY_LEADS) if cycles is None else cycles:
        points = station_trace(data, station, day, first, end, cycle=cycle, provenance=True)
        if ([parse_time(row["valid_at"]) for row in points] == hours
                and all(parse_time(row["available_at"]) <= stamp for row in points)
                and (not local_receipt or all(parse_time(row["ingested_at"]) <= stamp for row in points))):
            return points
    return []


def point(data, lat, lon, at, valid, *, local_receipt=False, pinned_issue=None):
    """Return one native grid cell from an eligible cached hourly forecast."""
    if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("Invalid globe probe coordinates")
    if not (20 <= lat <= 56 and -141 <= lon <= -52):
        return None
    cutoff = parse_time(at).astimezone(UTC)
    valid = parse_time(valid).astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    known_run = parse_time(pinned_issue).astimezone(UTC) if pinned_issue else None
    choices = []
    for day in (valid.date(), valid.date() - timedelta(days=1)):
        for hour, leads in {0: LEADS, **INTRADAY_LEADS}.items():
            issue = datetime.combine(day, time(hour), UTC)
            lead = int((valid - issue).total_seconds() // 3600)
            if lead not in leads or (known_run is not None and issue != known_run) or issue > cutoff:
                continue
            path = frame_path(data, issue, lead)
            try:
                field = _frame(path, path.stat().st_mtime_ns)
                published, receipt = parse_time(field["available_at"]), parse_time(field["ingested_at"])
                if (parse_time(field["issued_at"]) == issue and parse_time(field["valid_at"]) == valid
                        and issue <= published <= receipt and published <= cutoff
                        and (not local_receipt or receipt <= cutoff)):
                    choices.append((issue, lead, path, field))
            except (OSError, TypeError, KeyError, ValueError):
                continue
    if not choices:
        return None
    issue, lead, path, field = max(choices, key=lambda row: row[0])
    raw = path.with_suffix(".grib2").read_bytes()
    if sha256(raw).hexdigest() != field["sha256"]:
        raise ValueError("Original HRRR field differs from its retained digest")
    cell = decode_station_cells(raw, issue, lead, stations={"point": (lat, lon, None, None)})["point"]
    if cell["distance_km"] > 5:
        return None
    return {**cell, **{key: field[key] for key in ("source", "issued_at", "valid_at", "available_at", "ingested_at",
                                                   "sha256", "native_units", "resolution_m")}}


def _request(data, request, get):
    if (Path(data) / "collector" / "paused").exists():
        raise SourcePaused("Collection stopped")
    return get(request, timeout=20)


def read_range(data, issue, url, first, last, get):
    request = Request(url, headers={"Range": f"bytes={first}-{last}"})
    with _request(data, request, get) as reply:
        if reply.status != 206 or not reply.headers.get("Content-Range", "").startswith(f"bytes {first}-{last}/"):
            raise ValueError("NOAA did not return the requested HRRR GRIB2 range")
        published = parsedate_to_datetime(reply.headers["Last-Modified"]).astimezone(UTC)
        tag = reply.headers.get("ETag")
        payload = reply.read(last - first + 2)
    obtained = datetime.now(UTC)
    if len(payload) != last - first + 1 or not issue <= published <= obtained:
        raise ValueError("Incomplete HRRR range or conflicting publication clock")
    return payload, published, obtained, tag


def cache_frame(data, issue, lead, *, get=urlopen):
    path = frame_path(data, issue, lead)
    try:
        return json.loads(path.read_bytes())
    except FileNotFoundError:
        pass
    url = object_url(issue, lead)
    with _request(data, url + ".idx", get) as reply:
        index = reply.read(512 * 1024).decode("ascii")
    first, last = locate_temperature(index, lead)
    payload, published, obtained, tag = read_range(data, issue, url, first, last, get)
    digest = sha256(payload).hexdigest()
    raw = path.with_suffix(".grib2")
    cells = decode_station_cells(payload, issue, lead)
    frame = {
        "model": "NOAA HRRR CONUS",
        "field": "2 m temperature, instant forecast",
        "native_units": "K",
        "resolution_m": 3000,
        "issued_at": iso(issue),
        "available_at": iso(published),
        "ingested_at": iso(obtained),
        "valid_at": iso(issue + timedelta(hours=lead)),
        "lead_hours": lead,
        "source": url,
        "source_index": url + ".idx",
        "source_etag": tag,
        "byte_range": [first, last],
        "sha256": digest,
        "raw": str(raw.relative_to(data)),
        "stations": cells,
    }
    _write(raw, payload)
    _write(path, json.dumps(frame, separators=(",", ":"), allow_nan=False).encode())
    return frame


def backfill(data, start, end, workers=5, *, leads=LEADS, fetch=cache_frame, cycle=0):
    counts, errors = {}, {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for day in date_range(start, end):
            issue = datetime.combine(day, time(cycle), UTC)
            jobs = {pool.submit(fetch, data, issue, lead): lead for lead in leads}
            downloaded = 0
            for job in as_completed(jobs):
                try:
                    job.result()
                    downloaded += 1
                except SourcePaused:
                    for pending in jobs:
                        pending.cancel()
                    return counts, errors
                except (OSError, TypeError, ValueError, KeyError) as error:
                    errors[f"{day}/f{jobs[job]:02d}"] = str(error)
            counts[day.isoformat()] = downloaded
            print(f"{day} {downloaded}/{len(leads)} HRRR fields", flush=True)
    return counts, errors


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data", type=Path, default=Path(".data"))
    p.add_argument("--start", type=date.fromisoformat, required=True)
    p.add_argument("--end", type=date.fromisoformat)
    p.add_argument("--workers", type=int, choices=range(1, 9), default=5)
    p.add_argument("--cycle", type=int, choices=(0, 6, 12, 18), default=0)
    p.add_argument("--leads", nargs="+", type=int, default=LEADS)
    args = p.parse_args()
    if any(not 0 <= lead <= 48 for lead in args.leads):
        p.error("Choose native forecast leads from 0 to 48 hours")
    counts, errors = backfill(args.data, args.start, args.end or args.start, args.workers,
                              leads=tuple(dict.fromkeys(args.leads)), cycle=args.cycle)
    print(json.dumps({"days": counts, "issues": errors}, indent=2))
    return 0 if any(counts.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
