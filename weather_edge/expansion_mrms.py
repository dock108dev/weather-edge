"""Archive NOAA one-hour MRMS precipitation estimates for weather replay."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.request import Request, urlopen

from . import expansion_contract, expansion_weather, research_model
from .history import _write
from .nbm import climate_window, iso, utc

AWS = "https://noaa-mrms-pds.s3.amazonaws.com"
GUIDE = "https://www.nssl.noaa.gov/projects/mrms/operational/tables.php"
VERSION = "mrms-precipitation-replay-v1"
PRODUCTS = {"RadarOnly_QPE_01H": (2, 1), "MultiSensor_QPE_01H_Pass1": (30, 1), "RadarOnly_QPE_03H": (3, 3)}
PRODUCT_LABELS = {"RadarOnly_QPE_01H": "Radar-only estimate", "MultiSensor_QPE_01H_Pass1": "Multisensor pass 1 estimate"}
POINTS = {
    "KNYC": (40.779, -73.9692, "Central Park"),
    "KPHL": (39.873, -75.227, "Philadelphia International Airport"),
    "hudson-valley": (41.225, -73.856, "Lower Hudson Valley"),
}
CHICAGO_POINTS = {
    "KMDW": (41.786, -87.752, "Chicago Midway Airport"),
    "KORD": (41.960, -87.930, "Chicago O’Hare Airport"),
    "southwest-chicago": (41.525, -88.081, "Southwest of Chicago"),
}
REGIONS = {"northeast": ("KNYC", "Northeast", POINTS), "chicago": ("KMDW", "Chicago area", CHICAGO_POINTS),
           "rain-study": ("KNYC", "Central Park and Chicago", {"KNYC": POINTS["KNYC"], "KMDW": CHICAGO_POINTS["KMDW"]})}


def object_url(product, end):
    return f"{AWS}/CONUS/{product}_00.00/{end:%Y%m%d}/MRMS_{product}_00.00_{end:%Y%m%d-%H%M%S}.grib2.gz"


def _sample(payload, product, end, points):
    from eccodes import CodesInternalError, codes_get, codes_grib_find_nearest_multiple, codes_new_from_message, codes_release

    expected = {
        "discipline": 209, "parameterCategory": 6, "parameterNumber": PRODUCTS[product][0],
        "gridType": "regular_ll", "Ni": 7000, "Nj": 3500,
        "iDirectionIncrementInDegrees": 0.01, "jDirectionIncrementInDegrees": 0.01,
        "iScansNegatively": 0, "jScansPositively": 0,
        "dataDate": int(end.strftime("%Y%m%d")), "dataTime": int(end.strftime("%H%M")),
        "validityDate": int(end.strftime("%Y%m%d")), "validityTime": int(end.strftime("%H%M")),
    }
    handle = codes_new_from_message(gzip.decompress(payload))
    try:
        for key, value in expected.items():
            if codes_get(handle, key) != value:
                raise ValueError(f"MRMS precipitation field has an unexpected {key}")
        coords = list(points.values())
        samples = codes_grib_find_nearest_multiple(handle, False, [row[0] for row in coords], [row[1] for row in coords])
        if len(samples) != len(coords):
            raise ValueError("MRMS nearest-point result has the wrong point count")
        results = {}
        for ident, sample in zip(points, samples, strict=True):
            lat, lon, label = points[ident]
            value = sample["value"]
            valid = math.isfinite(value) and value >= 0 and value != codes_get(handle, "missingValue")
            # MRMS encodes below-zero sentinel values for missing/outside coverage.
            results[ident] = {"label": label, "lat": lat, "lon": lon, "grid_lat": sample["lat"],
                              "grid_lon": (sample["lon"] + 180) % 360 - 180, "distance_km": sample["distance"],
                              "amount_mm": float(value) if valid else None, "native_value": float(value) if math.isfinite(value) else None}
        return results
    except CodesInternalError as error:
        raise ValueError(f"Could not decode MRMS field: {error}") from error
    finally:
        codes_release(handle)


def get_frame(data, product, end, region="northeast", *, points=None, get=urlopen):
    if product not in PRODUCTS or end.minute or end.second or end.microsecond or end.utcoffset() != timedelta(0):
        raise ValueError("Expected a supported MRMS product and exact UTC hour")
    source = object_url(product, end)
    raw = data / "expansion" / "mrms" / "raw" / product / f"{end:%Y%m%d-%H00}.grib2.gz"
    original = raw.with_suffix(".json")
    if points is None:
        points = REGIONS[region][2]
        path = original if region == "northeast" else raw.with_suffix(f".{region}.json")
    else:
        footprint = hashlib.sha256(research_model._json_bytes(points)).hexdigest()
        path = raw.with_suffix(f".sample-{footprint[:16]}.json")
    receipt = raw.with_suffix(".receipt.json")
    if path.is_file():
        return json.loads(path.read_text())
    existing = next((entry for entry in (receipt, original, *sorted(raw.parent.glob(f"{raw.stem}.*.json")))
                     if entry.exists()), None)
    if raw.exists() and existing:
        known = json.loads(existing.read_text())
        obj = raw.read_bytes()
        if hashlib.sha256(obj).hexdigest() != known["raw_sha256"]:
            raise ValueError("MRMS preserved raw file digest differs from its archive metadata")
        published, captured = utc(known["available_at"]), utc(known["received_at"])
        etag = known.get("source_etag")
    else:
        request = Request(source, headers={"User-Agent": "Weather Edge public research", "Accept-Encoding": "identity"})
        with get(request, timeout=20) as response:
            stamp = response.headers.get("Last-Modified")
            if not stamp:
                raise ValueError("MRMS source Last-Modified is missing")
            published = utc(parsedate_to_datetime(stamp))
            obj = response.read()
            captured = datetime.now(timezone.utc)
            etag = response.headers.get("ETag")
        _write(raw, obj)
    if not end <= published <= captured:
        raise ValueError("MRMS timing contradicts the observed accumulation window")
    duration = PRODUCTS[product][1]
    clocks = {"available_at": iso(published), "received_at": iso(captured), "source": source, "source_etag": etag,
              "raw_path": str(raw.relative_to(data)), "raw_sha256": hashlib.sha256(obj).hexdigest()}
    if not receipt.exists():
        _write(receipt, research_model._json_bytes(clocks))
    samples = _sample(obj, product, end, points)
    frame = {
        "product": product, "kind": "gridded precipitation estimate", "units": "mm", "resolution_degrees": 0.01,
        "accumulation_starts_at": iso(end - timedelta(hours=duration)), "valid_at": iso(end), "duration_hours": duration,
        **clocks, "points": samples,
    }
    _write(path, research_model._json_bytes(frame))
    return frame


def _round(value):
    return None if value is None else round(value, 3)


def make_day(data, day, region="northeast"):
    station, _, points = REGIONS[region]
    start, end = climate_window(day, expansion_weather.STATIONS[station][3])
    jobs = [(product, start + timedelta(hours=hour)) for hour in range(1, 25) for product in PRODUCT_LABELS]

    def take(job):
        product, stamp = job
        try:
            return get_frame(data, product, stamp, region), None
        except (OSError, TypeError, KeyError, ValueError) as error:
            return None, {"product": product, "valid_at": iso(stamp), "error": expansion_contract.availability_error(error)}

    rows, missing = [], []
    with ThreadPoolExecutor(max_workers=2) as pool:
        for frame, error in pool.map(take, jobs):
            if frame:
                rows.append(frame)
            else:
                missing.append(error)
    suffix = "" if region == "northeast" else f"-{region}"
    path = data / "expansion" / "mrms" / "days" / f"{day}{suffix}.json"
    by_position = {point_id: [row for row in rows if row["points"][point_id]["amount_mm"] is not None] for point_id in points}
    summary = []
    for point_id, samples in by_position.items():
        for product, label in PRODUCT_LABELS.items():
            product_rows = [row for row in samples if row["product"] == product]
            sums = [row["points"][point_id]["amount_mm"] for row in product_rows]
            delays = [(utc(row["available_at"]) - utc(row["valid_at"])).total_seconds() for row in product_rows]
            summary.append({
                "method": product, "label": label, "group": points[point_id][2], "hours": len(sums),
                "climate_day_total_mm": _round(sum(sums)) if len(sums) == 24 else None,
                "peak_hour_mm": _round(max(sums)) if sums else None,
                "median_publication_delay_seconds": statistics.median(delays) if delays else None,
            })
    day_data = {"station": station, "date": str(day), "climate_start": iso(start), "climate_end": iso(end),
                "frames": rows, "missing": missing, "metrics": summary}
    _write(path, research_model._json_bytes(day_data))
    return day_data, path


def study(data, day, region="northeast"):
    station, region_name, points = REGIONS[region]
    station_name = points[station][2]
    day_data, source_day = make_day(data, day, region)
    label = expansion_weather.daily_labels(data, station, day, day)[str(day)]
    contract = None
    payout_error = None
    try:
        contract = expansion_contract.rain_market(data, day, label, station=station)
    except (OSError, TypeError, KeyError, ValueError) as error:
        payout_error = expansion_contract.availability_error(error)
    official = contract["official_report"] if contract else label
    rows = day_data["frames"]
    center_samples = [row for row in rows if row["points"][station]["amount_mm"] is not None]
    significant = max(center_samples, key=lambda row: row["points"][station]["amount_mm"], default=None)
    metrics = day_data["metrics"]
    official_precip = expansion_weather.parse_number(official["precip_as_reported"])
    metrics.append({"method": "nws-cli", "label": "Governing official climate report", "group": station_name,
                    "climate_day_total_in": official_precip,
                    "reported_amount": official["precip_as_reported"],
                    "climate_day_total_mm": _round(official_precip * 25.4) if official_precip is not None else None})
    cases = []
    if significant:
        terms = contract["buckets"][0] if contract else None
        cases.append({"station": station, "date": str(day), "at": significant["available_at"],
                      "title": f"Hourly precipitation near {station_name}",
                      "detail": (f"{PRODUCT_LABELS[significant['product']]} reached "
                                 f"{significant['points'][station]['amount_mm']:.1f} mm through {significant['valid_at']}. "
                                 f"The CLI gave {official['precip_as_reported']} for the full climate day."),
                      "venue": "polymarket-us" if contract else None,
                      "instrument_id": terms["id"] if terms else None,
                      "official_report": official, "market": terms, "radar_frame": significant})
    record = {
        "version": VERSION, "title": f"{day:%b. %d} {region_name} precipitation on radar",
        "hypothesis": "Hourly radar estimates reveal within-day variation around a rain settlement station and nearby places.",
        "target": f"One-hour liquid-equivalent MRMS estimates during the exact {station_name} climate day",
        "venues": ["polymarket-us"] if contract else [], "date_window": {"replay": [str(day), str(day)]},
        "metrics": metrics, "cases": cases,
        "limits": [
            "MRMS is a gridded precipitation estimate. The original local station report determines the verified contract result.",
            "Each product retains separate accumulation, publication and local retrieval clocks. The listed publication lag uses the public archive Last-Modified.",
            "Climate-day estimates add 24 non-overlapping one-hour amounts only when every hour is present. Nearby values outside the market station provide regional context.",
        ],
        "provenance": {"source_day": research_model._source_entry(data, source_day), "noaa_product_guide": GUIDE,
                       "source_sha256": research_model.run_sources(sys.modules[__name__])},
        "missing": day_data["missing"], "payout_evidence_issue": payout_error,
    }
    return research_model.write_record(data, record, day_data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path(".data"))
    parser.add_argument("--day", type=date.fromisoformat, default=date(2026, 11, 12))
    parser.add_argument("--region", choices=REGIONS, default="northeast")
    args = parser.parse_args()
    print(study(args.data_dir, args.day, args.region))


if __name__ == "__main__":
    main()
