"""Keep original NOAA MADIS files and supplemental high-frequency station readings."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone
from email.utils import parsedate_to_datetime
from functools import lru_cache
import gzip
from hashlib import sha256
import io
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import numpy as np

from . import expansion_weather
from .archive import date_range
from .history import _write
from .market import iso, parse_time

ORIGIN = "https://madis-data.ncep.noaa.gov/madisPublic1/data/"
DOCS = "https://madis.ncep.noaa.gov/madis_OMO.shtml"
NAME = "NOAA MADIS high-frequency ASOS"
UTC = timezone.utc
HOUR = timedelta(hours=1)
LIMIT = 5 * 1024 * 1024


def source(hour, *, live=False):
    prefix = "" if live else f"archive/{hour:%Y/%m/%d}/"
    return f"{ORIGIN}{prefix}LDAD/hfmetar/netCDF/{hour:%Y%m%d_%H00}.gz"


def path_for(data, hour, *, live=False):
    base = Path(data) / "omo" / f"{hour:%Y%m%d%H}"
    return base / ("live.json" if live else "archive.json")


def _station_samples(fields, mask, first, last):
    times = fields["observationTime"].data
    received = fields["receivedTime"].data
    modified = fields["modifyTime"].data
    provider_time = fields["reportTime"].data
    temperature = fields["temperature"].data
    valid = (mask & (times >= first) & (times < last) & (times <= received)
             & (received <= modified) & (modified < last + 3 * 86400)
             & (provider_time >= times) & (provider_time <= modified)
             & (temperature >= 213.15) & (temperature <= 333.15) & (fields["temperatureStatus"].data == 0))
    observations = []
    for index in np.flatnonzero(valid):
        number = float(temperature[index])
        c = number - 273.15
        # The native message encodes a whole Celsius temperature. Keep the netCDF value as well.
        if abs(c - round(c)) > .001:
            continue
        row = {"at": iso(datetime.fromtimestamp(times[index], UTC)),
               "madis_received_at": iso(datetime.fromtimestamp(received[index], UTC)),
               "provider_reported_at": iso(datetime.fromtimestamp(provider_time[index], UTC)),
               "madis_modified_at": iso(datetime.fromtimestamp(modified[index], UTC)),
               "temp_f": round(32 + round(c) * 1.8, 1), "temp_c_whole": round(c), "temp_native_k": number,
               "qc_descriptor": fields["temperatureDD"].data[index].decode("ascii"),
               "raw_report": fields["rawMessage"].data[index].tobytes().strip(b"\x00").decode("ascii")}
        observations.append(row)
    return sorted(observations, key=lambda r: (r["at"], r["madis_received_at"], r["madis_modified_at"]))


def decode(body, hour, *, sampler=_station_samples):
    from scipy.io import netcdf_file

    with netcdf_file(io.BytesIO(gzip.decompress(body)), "r", mmap=False) as dataset:
        fields = dataset.variables
        if (dataset.title != b"MADIS - Meteorological Surface - High frequency METAR"
                or parse_time(dataset.startTime.decode()) != hour or fields["temperature"].units != b"kelvin"):
            raise ValueError("Unexpected MADIS ASOS product, units or observation hour")
        station = fields["stationId"].data
        station_codes = station.view(f"S{station.shape[1]}").ravel()
        group = fields["dataProvider"].data
        provider = group.view(f"S{group.shape[1]}").ravel()
        native = (provider == b"HF-METAR") & (fields["metarType"].data == 1)
        rows = {}
        for code, (lat, lon, _, _) in expansion_weather.STATIONS.items():
            mask = (native & (station_codes == code.encode()) & (np.abs(fields["latitude"].data - lat) <= .04)
                    & (np.abs(fields["longitude"].data - lon) <= .04))
            if mask.any():
                points = sampler(fields, mask, hour.timestamp(), (hour + HOUR).timestamp())
                if points:
                    rows[code] = points
        return rows


def retain(data, hour, receipt, body, *, live=False):
    data = Path(data)
    pointer = path_for(data, hour, live=live)
    digest = sha256(body).hexdigest()
    if receipt.get("sha256", digest) != digest:
        raise ValueError("MADIS high-frequency bytes do not match the receipt")
    raw = pointer.with_name(digest + ".gz")
    leaves = decode(body, hour)
    if not raw.is_file():
        _write(raw, body)
    suffix = f"-{parse_time(receipt['ingested_at']):%Y%m%d%H%M%S%f}-live.json" if live else "-archive.json"
    record_path = pointer.with_name(digest + suffix)
    record = {**receipt, "sha256": digest, "raw": str(raw.relative_to(data)), "stations": leaves,
              "hour": iso(hour), "native_product": NAME, "native_units": "K from whole degrees Celsius"}
    if not record_path.is_file():
        _write(record_path, _encoded(record))
    _write(pointer, _encoded({"path": record_path.name, "sha256": digest}))
    return record


def _encoded(value):
    return json.dumps(value, allow_nan=False, separators=(",", ":")).encode() + b"\n"


@lru_cache(maxsize=384)
def _read_record(path):
    return json.loads(path.read_bytes())


def load(data, hour, *, live=False):
    pointer = path_for(data, hour, live=live)
    try:
        keys = json.loads(pointer.read_bytes())
        return _read_record(pointer.with_name(keys["path"]))
    except FileNotFoundError:
        return None


def fetch(data, hour, *, live=False):
    previous = load(data, hour, live=live)
    if previous and not live:
        return previous
    if (Path(data) / "collector" / "paused").exists():
        raise OSError("Collection stopped locally")
    headers = {"Accept-Encoding": "identity", "User-Agent": "Weather Edge public NOAA station archive"}
    if previous and previous.get("source_etag"):
        headers["If-None-Match"] = previous["source_etag"]
    url = source(hour, live=live)
    requested = datetime.now(UTC)
    try:
        with urlopen(Request(url, headers=headers), timeout=15) as response:
            body = response.read(LIMIT + 1)
            published = parsedate_to_datetime(response.headers["Last-Modified"]).astimezone(UTC)
            tag = response.headers.get("ETag")
    except HTTPError as error:
        if error.code == 304 and previous:
            return previous
        raise
    received = datetime.now(UTC)
    if len(body) > LIMIT or not hour <= published <= received:
        raise ValueError("Unexpected MADIS high-frequency receipt size or file clock")
    if previous and sha256(body).hexdigest() == previous["sha256"]:
        return previous
    receipt = {"source": url, "requested_at": iso(requested), "ingested_at": iso(received),
               "available_at": iso(published), "source_etag": tag}
    return retain(data, hour, receipt, body, live=live)


def editions(data, hour, *, original_receipts=False):
    if original_receipts:
        directory = path_for(data, hour, live=True).parent
        return sorted((_read_record(p) for p in directory.glob("*-live.json")), key=lambda row: row["ingested_at"])
    frame = load(data, hour)
    return [frame] if frame else []


def station_trace(data, station, start, end, *, original_receipts=False):
    start, end = parse_time(start), parse_time(end)
    if station not in expansion_weather.STATIONS:
        return []
    points, seen = [], {}
    hour = start.replace(minute=0, second=0, microsecond=0)
    while hour < end:
        for edition in editions(data, hour, original_receipts=original_receipts):
            basis = edition["ingested_at"] if original_receipts else edition["available_at"]
            clock = parse_time(basis)
            for row in edition["stations"].get(station, []):
                stamp = row["at"]
                if (not start <= parse_time(stamp) < end or parse_time(row["madis_modified_at"]) > clock
                        or seen.get(stamp) == row["temp_c_whole"]):
                    continue
                seen[stamp] = row["temp_c_whole"]
                points.append({**row, "available_at": basis, "source_at": edition["available_at"],
                               "ingested_at": edition["ingested_at"], "source": edition["source"],
                               "source_sha256": edition["sha256"], "display_only": True})
        hour += HOUR
    return sorted(points, key=lambda row: (row["at"], row["available_at"]))


def backfill(data, first, last, hours, *, workers=4):
    issues = {}
    def obtain(hour):
        try:
            value = fetch(data, hour)
            return hour, sum(map(len, value["stations"].values())), None
        except (OSError, ValueError, KeyError, TypeError, EOFError) as issue:
            return hour, 0, str(issue)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for day in date_range(first, last):
            if (Path(data) / "collector" / "paused").exists():
                break
            results = list(pool.map(obtain, (datetime.combine(day, time(hour), UTC) for hour in hours)))
            for hour, _, issue in results:
                if issue:
                    issues[iso(hour)] = issue
            print(f"{day} · {sum(issue is None for _, _, issue in results)}/{len(hours)} files, "
                  f"{sum(count for _, count, _ in results)} exact station rows", flush=True)
    return issues


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data", type=Path, default=Path(".data"))
    p.add_argument("--first", type=date.fromisoformat, required=True)
    p.add_argument("--last", type=date.fromisoformat)
    p.add_argument("--hours", type=int, nargs="+", choices=range(24), default=list(range(12, 24)))
    p.add_argument("--workers", type=int, choices=range(1, 7), default=4)
    args = p.parse_args()
    print(json.dumps({"issues": backfill(args.data, args.first, args.last or args.first,
                                         sorted(set(args.hours)), workers=args.workers)}, indent=2))


if __name__ == "__main__":
    main()
