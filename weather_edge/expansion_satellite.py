"""Archive NOAA GOES-19 cloud-mask evidence with its native projection and clocks."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone
from email.utils import parsedate_to_datetime
from functools import lru_cache, partial
from hashlib import sha256
import json
from math import asin, atan2, cos, radians, sin, sqrt
from pathlib import Path
import re
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from xml.etree import ElementTree

import numpy as np

from . import expansion_weather, history
from .nbm import climate_window, iso, utc

NOAA = "https://noaa-goes19.s3.amazonaws.com"
PRODUCT = "ABI-L2-ACMC"
IRRADIANCE = "ABI-L2-DSRF"
SKIN = "ABI-L2-LSTC"
DOCS = "https://www.ncei.noaa.gov/access/metadata/landing-page/bin/iso?id=gov.noaa.ncdc%3AC01503"
IRRADIANCE_DOCS = "https://www.ncei.noaa.gov/access/metadata/landing-page/bin/iso?id=gov.noaa.ncdc%3AC01524"
SKIN_DOCS = "https://www.ncei.noaa.gov/access/metadata/landing-page/bin/iso?id=gov.noaa.ncdc%3AC01521"
PROJECTION_DOCS = "https://goes-r.noaa.gov/users/docs/PUG-GRB-vol4.pdf"
REGISTRY = "https://registry.opendata.aws/noaa-goes/"
FILENAME = re.compile(r"OR_(ABI-L2-(?:ACMC|DSRF|LSTC))-M\d+_G19_s(\d{14})_e(\d{14})_c(\d{14})\.nc")
CLOUD_CODES = {0: "clear", 1: "probably clear", 2: "probably cloudy", 3: "cloudy"}
CORE_STATIONS = ("KNYC", "KPHL", "KBOS", "KMDW", "KORD", "KDEN", "KATL", "KEWR", "KCVG", "KPVD")
PRODUCTS = {PRODUCT: {"field": "ACM", "kind": "satellite cloud classification",
                      "maximum_age": timedelta(minutes=20), "max_bytes": 12_000_000},
            IRRADIANCE: {"field": "DSR", "kind": "satellite retrieval of surface shortwave irradiance",
                         "maximum_age": timedelta(minutes=35), "max_bytes": 70_000_000},
            SKIN: {"field": "LST", "kind": "satellite retrieval of land skin temperature",
                   "maximum_age": timedelta(minutes=75), "max_bytes": 12_000_000}}


def _filename_time(value):
    return datetime.strptime(value[:-1], "%Y%j%H%M%S").replace(tzinfo=timezone.utc, microsecond=int(value[-1]) * 100000)


def _record(path, payload):
    history._write(path, (json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode())


def _request(url, get, max_bytes=12_000_000):
    request = Request(url, headers={"User-Agent": "Weather Edge public satellite research", "Accept-Encoding": "identity"})
    requested = datetime.now(timezone.utc)
    with get(request, timeout=30) as response:
        body = response.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise ValueError("GOES receipt exceeds native size envelope")
        received = datetime.now(timezone.utc)
        return body, {"source": url, "requested_at": iso(requested), "received_at": iso(received),
                      "last_modified": response.headers.get("Last-Modified"), "etag": response.headers.get("ETag")}


def _catalog(data, hour, get, product):
    prefix = f"{product}/{hour:%Y/%j/%H}/"
    root = data / "expansion" / "goes" / "inventory"
    if product != PRODUCT:
        root /= product
    root /= hour.strftime("%Y%m%d-%H")
    reusable = root / "complete.json"
    if reusable.is_file():
        return json.loads(reusable.read_bytes())
    url = f"{NOAA}/?{urlencode({'list-type': '2', 'prefix': prefix})}"
    body, receipt = _request(url, get)
    listing = ElementTree.fromstring(body)
    truncated = listing.find("{*}IsTruncated")
    if truncated is None or truncated.text != "false":
        raise ValueError("Incomplete GOES hourly inventory")
    items = []
    for row in listing.findall("{*}Contents"):
        key_node = row.find("{*}Key")
        key = key_node.text if key_node is not None else ""
        match = FILENAME.fullmatch(key.removeprefix(prefix)) if key.startswith(prefix) else None
        if match is None or match[1] != product:
            continue
        start, end, created = map(_filename_time, match.groups()[1:])
        modified = utc(row.find("{*}LastModified").text)
        if not start < end <= created <= modified:
            continue
        items.append({"key": key, "scan_starts_at": iso(start), "scan_ends_at": iso(end), "created_at": iso(created),
                      "listed_available_at": iso(modified), "size": int(row.find("{*}Size").text), "etag": row.find("{*}ETag").text})
    digest = sha256(body).hexdigest()
    path = root / f"{digest}.xml"
    if not path.is_file():
        history._write(path, body)
    result = {**receipt, "records": items, "body_sha256": digest, "raw_path": str(path.relative_to(data))}
    if items and utc(receipt["received_at"]) > hour + timedelta(hours=2):
        _record(reusable, result)
    return result


def project(lat, lon, projection):
    """Map a GRS80 surface point to NOAA fixed-grid scan angles in radians."""
    phi, delta = radians(lat), radians(lon - projection["longitude_of_projection_origin"])
    major, minor = projection["semi_major_axis"], projection["semi_minor_axis"]
    height = projection["perspective_point_height"] + major
    eccentricity_sq = 1 - (minor / major) ** 2
    geocentric = atan2((minor / major) ** 2 * sin(phi), cos(phi))
    radius = minor / sqrt(1 - eccentricity_sq * cos(geocentric) ** 2)
    sx = height - radius * cos(geocentric) * cos(delta)
    sy = -radius * cos(geocentric) * sin(delta)
    sz = radius * sin(geocentric)
    if height * (height - sx) < sy ** 2 + (major / minor) ** 2 * sz ** 2:
        return None
    return asin(-sy / sqrt(sx ** 2 + sy ** 2 + sz ** 2)), atan2(sz, sx)


def _decoded(attribute):
    return bytes(attribute).decode("ascii") if isinstance(attribute, bytes) else str(attribute)


def _scale(dataset, key):
    return float(np.asarray(dataset.attrs[key]).item())


def _coordinates(dataset):
    return dataset[:].astype(float) * _scale(dataset, "scale_factor") + _scale(dataset, "add_offset")


def _inside(values, target):
    half_pixel = abs(values[1] - values[0]) / 2
    return min(values[0], values[-1]) - half_pixel <= target <= max(values[0], values[-1]) + half_pixel


def _locate(xs, ys, projection, lat, lon):
    point = project(lat, lon, projection)
    if point is None or not (_inside(xs, point[0]) and _inside(ys, point[1])):
        return None
    col, row = (int(np.argmin(np.abs(centers - seek))) for centers, seek in zip((xs, ys), point, strict=True))
    xslice = slice(max(0, col - 2), min(len(xs), col + 3))
    yslice = slice(max(0, row - 2), min(len(ys), row + 3))
    return row, col, yslice, xslice


def _footprint(mask, quality, xs, ys, projection, station, lat, lon):
    indices = _locate(xs, ys, projection, lat, lon)
    if indices is None:
        return None
    row, col, yslice, xslice = indices
    local_mask, local_qf = mask[yslice, xslice], quality[yslice, xslice]
    valid = (local_mask <= 3) & (local_qf == 0)
    center = int(mask[row, col]) if quality[row, col] == 0 and mask[row, col] <= 3 else None
    count = int(valid.sum())
    return {"station": station, "lat": lat, "lon": lon, "row": row, "column": col,
            "grid_scan_x_rad": float(xs[col]), "grid_scan_y_rad": float(ys[row]),
            "center_class_code": center, "center_class": CLOUD_CODES.get(center), "center_dqf": int(quality[row, col]),
            "window_class_counts": {label: int(np.sum(local_mask[valid] == code)) for code, label in CLOUD_CODES.items()},
            "window_cloud_mask_fraction": float(np.mean(local_mask[valid] >= 2)) if count else None,
            "window_good_pixels": count, "window_total_pixels": int(valid.size)}


def _irradiance_point(field, quality, xs, ys, projection, station, lat, lon):
    indices = _locate(xs, ys, projection, lat, lon)
    if indices is None:
        return None
    row, col, yslice, xslice = indices
    packed_fill = int(np.asarray(field.attrs["_FillValue"]).item())
    scale, offset = (_scale(field, key) for key in ("scale_factor", "add_offset"))
    local = field[yslice, xslice]
    watts = local * scale + offset
    valid = (local != packed_fill) & (quality[yslice, xslice] == 0) & (0 <= watts) & (watts <= 1500)
    center_raw = int(field[row, col])
    center = float(center_raw * scale + offset)
    return {"station": station, "lat": lat, "lon": lon, "row": row, "column": col,
            "grid_scan_x_rad": float(xs[col]), "grid_scan_y_rad": float(ys[row]),
            "center_dqf": int(quality[row, col]), "center_raw": center_raw,
            "center_w_m2": center if center_raw != packed_fill and quality[row, col] == 0 and 0 <= center <= 1500 else None,
            "window_mean_w_m2": float(np.mean(watts[valid])) if valid.any() else None,
            "window_good_pixels": int(valid.sum()), "window_total_pixels": int(valid.size)}


def _skin_point(field, quality, xs, ys, projection, station, lat, lon, *, native_flags):
    indices = _locate(xs, ys, projection, lat, lon)
    if indices is None:
        return None
    row, col, yslice, xslice = indices
    minimum, maximum = map(int, field.attrs["valid_range"])
    local = field[yslice, xslice]
    flags = native_flags[yslice, xslice]
    kelvin = local * _scale(field, "scale_factor") + _scale(field, "add_offset")
    valid = (minimum <= local) & (local <= maximum) & (quality[yslice, xslice] == 0) & ((flags & 192) == 0)
    row_index, col_index = row - yslice.start, col - xslice.start
    center = float(kelvin[row_index, col_index]) if valid[row_index, col_index] else None
    footprint = float(np.mean(kelvin[valid])) if valid.any() else None
    return {"station": station, "lat": lat, "lon": lon, "row": row, "column": col,
            "grid_scan_x_rad": float(xs[col]), "grid_scan_y_rad": float(ys[row]),
            "center_dqf": int(quality[row, col]), "center_pqi": int(native_flags[row, col]),
            "center_kelvin": center, "window_mean_kelvin": footprint,
            "center_f": (center - 273.15) * 1.8 + 32 if center is not None else None,
            "window_mean_f": (footprint - 273.15) * 1.8 + 32 if footprint is not None else None,
            "window_good_pixels": int(valid.sum()), "window_total_pixels": int(valid.size)}


def _parse(body, match_key, product):
    try:
        import h5py
    except ImportError as error:
        raise RuntimeError("GOES download decoding needs the optional h5py package. See docs/weather-expansion.md.") from error
    with h5py.File.in_memory(body) as dataset:
        native = {key: _decoded(dataset.attrs[key]) for key in ("time_coverage_start", "time_coverage_end", "date_created",
                                                                 "dataset_name", "spatial_resolution")}
        if native["dataset_name"] != match_key.rsplit("/", 1)[-1]:
            raise ValueError("GOES file identity mismatch")
        grid = dataset["goes_imager_projection"]
        if _decoded(grid.attrs["grid_mapping_name"]) != "geostationary" or _decoded(grid.attrs["sweep_angle_axis"]) != "x":
            raise ValueError("Unrecognized ABI fixed grid projection")
        projection = {key: _scale(grid, key) for key in ("longitude_of_projection_origin", "semi_major_axis", "semi_minor_axis",
                                                        "perspective_point_height")}
        xs, ys = (_coordinates(dataset[key]) for key in ("x", "y"))
        field, quality = dataset[PRODUCTS[product]["field"]], dataset["DQF"]
        if field.shape != (len(ys), len(xs)) or quality.shape != field.shape:
            raise ValueError("Unexpected ABI product geometry")
        if product == PRODUCT:
            if not np.array_equal(field.attrs["flag_values"], (0, 1, 2, 3)):
                raise ValueError("Unexpected cloud mask categories")
            sample = _footprint
        elif product == IRRADIANCE:
            if _decoded(field.attrs["units"]) != "W m-2" or not np.array_equal(quality.attrs["flag_values"], (0, 1)):
                raise ValueError("Unexpected irradiance product units or quality flags")
            sample = _irradiance_point
        else:
            flags = dataset["PQI"]
            if (_decoded(field.attrs["units"]) != "K" or not np.array_equal(quality.attrs["flag_values"], (0, 1, 2, 3))
                    or flags.shape != field.shape or "surface_type_land_qf" not in _decoded(flags.attrs["flag_meanings"])):
                raise ValueError("Unexpected land skin temperature units or quality flags")
            meanings = _decoded(flags.attrs["flag_meanings"]).split()
            land = meanings.index("surface_type_land_qf")
            if int(flags.attrs["flag_masks"][land]) != 192 or int(flags.attrs["flag_values"][land]) != 0:
                raise ValueError("Unexpected land surface quality bit encoding")
            native.update(units="K", scale_factor=_scale(field, "scale_factor"), add_offset=_scale(field, "add_offset"),
                          packed_valid_range=[int(value) for value in field.attrs["valid_range"]],
                          quality_flags=_decoded(quality.attrs["flag_meanings"]))
            sample = partial(_skin_point, native_flags=flags)
        points = {}
        for station in CORE_STATIONS:
            lat, lon, *_ = expansion_weather.STATIONS[station]
            if point := sample(field, quality, xs, ys, projection, station, lat, lon):
                points[station] = point
        return {"native": native, "h5py": h5py.__version__, "points": points,
                "projection": {**projection, "width": len(xs), "height": len(ys), "sweep_angle_axis": "x",
                               "scan_x_step_rad": float(xs[1] - xs[0]), "scan_y_step_rad": float(ys[1] - ys[0])}}


def _frame(data, selected, inventory, get, product):
    root = data / "expansion" / "goes" / "frames"
    ident = sha256(selected["key"].encode()).hexdigest()[:24]
    path = root / f"{ident}.json"
    if path.is_file():
        return json.loads(path.read_bytes())
    source = f"{NOAA}/{quote(selected['key'], safe='/')}"
    payload, receipt = _request(source, get, PRODUCTS[product]["max_bytes"])
    if len(payload) != selected["size"] or receipt["etag"] != selected["etag"] or not receipt["last_modified"]:
        raise ValueError("GOES object does not match its listed identity")
    decoded = _parse(payload, selected["key"], product)
    start, end, created = (utc(decoded["native"][key]) for key in ("time_coverage_start", "time_coverage_end", "date_created"))
    if not start < end <= created or any(abs(actual - utc(selected[key])) > timedelta(seconds=1)
                                         for actual, key in ((start, "scan_starts_at"), (end, "scan_ends_at"), (created, "created_at"))):
        raise ValueError("GOES native scan clock disagrees with its object key")
    original = max(created, utc(selected["listed_available_at"]), utc(parsedate_to_datetime(receipt["last_modified"])))
    body_sha = sha256(payload).hexdigest()
    raw_path = data / "expansion" / "goes" / "raw" / (selected["key"].rsplit("/", 1)[-1])
    if not raw_path.is_file():
        history._write(raw_path, payload)
    result = {**receipt, **decoded, "satellite": "GOES-19", "product": product, "kind": PRODUCTS[product]["kind"],
              "scan_starts_at": iso(start), "scan_ends_at": iso(end), "created_at": iso(created), "available_at": iso(original),
              "valid_at": iso(end), "raw_path": str(raw_path.relative_to(data)), "body_sha256": body_sha,
              "inventory_source": inventory["source"], "inventory_sha256": inventory["body_sha256"],
              "inventory_raw_path": inventory["raw_path"]}
    _record(path, result)
    return result


def latest(data, as_of, *, product=PRODUCT, get=urlopen):
    """Find a completed scan actually published by a retrospective decision."""
    data, at = Path(data), utc(as_of)
    current_hour = at.replace(minute=0, second=0, microsecond=0)
    hours = (current_hour - timedelta(hours=1), current_hour) if at.minute else (current_hour - timedelta(hours=1),)
    candidates = []
    for hour in hours:
        inventory = _catalog(data, hour, get, product)
        candidates.extend((entry, inventory) for entry in inventory["records"]
                          if utc(entry["listed_available_at"]) <= at
                          and at - PRODUCTS[product]["maximum_age"] <= utc(entry["scan_ends_at"]) <= at)
    for selected, inventory in sorted(candidates, key=lambda item: item[0]["scan_ends_at"], reverse=True):
        result = _frame(data, selected, inventory, get, product)
        if utc(result["available_at"]) <= at:
            return result
    return None


@lru_cache(maxsize=4)
def _stored_frames(folder, directory_mtime):
    frames = []
    for path in folder.glob("*.json"):
        try:
            entry = json.loads(path.read_bytes())
            frames.append((utc(entry["valid_at"]), entry))
        except (OSError, ValueError, KeyError):
            continue
    return sorted(frames, key=lambda item: (item[0], item[1]["product"]))


def observed_station(data, station, day, *, as_of=None, original_receipts=False, products=(PRODUCT, IRRADIANCE)):
    """Read cached station footprints. Playback can apply publication or local receipt clocks."""
    if station not in CORE_STATIONS:
        return []
    path = Path(data).resolve() / "expansion" / "goes" / "frames"
    if not path.is_dir():
        return []
    start, end = climate_window(day, expansion_weather.STATIONS[station][3])
    clock = utc(as_of) if as_of is not None else None
    availability_key = "received_at" if original_receipts else "available_at"
    keys = ("satellite", "product", "kind", "valid_at", "scan_starts_at", "scan_ends_at", "created_at", "available_at",
            "received_at", "source", "body_sha256", "raw_path")
    output = []
    for valid, frame in _stored_frames(path, path.stat().st_mtime_ns):
        if not start <= valid < end or station not in frame["points"] or frame["product"] not in products:
            continue
        if clock is not None and utc(frame[availability_key]) > clock:
            continue
        output.append({**{key: frame[key] for key in keys}, "point": frame["points"][station],
                       "native_resolution": frame["native"]["spatial_resolution"], "availability_basis": availability_key})
    return output


def backfill(data, first, last, hours, workers, *, product=PRODUCT):
    clocks = [datetime.combine(first + timedelta(days=offset), time(hour), timezone.utc)
              for offset in range((last - first).days + 1) for hour in hours]

    def capture(clock):
        try:
            frame = latest(data, clock, product=product)
            return {"at": iso(clock), "frame": frame["body_sha256"]} if frame else {"at": iso(clock), "reason": "No eligible scan"}
        except (ValueError, OSError, KeyError, ElementTree.ParseError) as error:
            return {"at": iso(clock), "reason": str(error)}

    attempted, missed = 0, []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for attempted, outcome in enumerate(pool.map(capture, clocks), start=1):
            if "reason" in outcome:
                missed.append(outcome)
            if attempted % 50 == 0:
                print(f"GOES-19 {attempted}/{len(clocks)} scans checked", flush=True)
    return {"attempted": attempted, "available": attempted - len(missed), "missing": missed}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--product", choices=tuple(PRODUCTS), default=PRODUCT)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--as-of", type=utc)
    modes.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--end", type=date.fromisoformat)
    parser.add_argument("--hours", nargs="+", type=int, default=[18, 19, 20])
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()
    if args.as_of:
        print(json.dumps(latest(args.data, args.as_of, product=args.product), indent=2))
    else:
        if args.end is None or args.end < args.start or args.workers < 1 or any(not 0 <= hour <= 23 for hour in args.hours):
            parser.error("Invalid date range, worker count or UTC hours")
        print(json.dumps(backfill(args.data, args.start, args.end, sorted(set(args.hours)), args.workers, product=args.product), indent=2))


if __name__ == "__main__":
    main()
