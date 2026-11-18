"""Reconstruct the native Alabama track rule from original advisories and Census boundaries."""

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import gzip
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import re
import struct
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener
from zipfile import ZipFile

from . import research_model, venue_archive, venue_storm
from .capture import ImmutableOrigin
from .collector import paused
from .history import _write
from .market import decimal, iso, parse_time
from .venue_sources import CollectionPaused

VERSION = "isaias-alabama-native-track-v3"
EARLIER = "isaias-alabama-native-track-v2-12fcdef41cafeca8"
NATIVE = "KXHURPATHAL-31DEC26ISAIAS-YES"
STORM_INDEX = "https://www.nhc.noaa.gov/archive/2026/ISAIAS.shtml"
NHC = "https://www.nhc.noaa.gov"
CENSUS_DIR = "https://www2.census.gov/geo/tiger/GENZ2025/shp/"
LISTED_BOUNDARY = CENSUS_DIR + "cb2025usstate500k.zip"
NAMED_BOUNDARY = CENSUS_DIR + "cb_2025_us_state_500k.zip"
OFFICIAL_LANDFALL = NHC + "/archive/2026/al09/al092026.update.10100129.shtml"
HURRICANE_MPH = 74
ZONES = {"AST": -4, "EDT": -4, "CDT": -5, "EST": -5, "CST": -6}
ADVISORY_PATH = re.compile(rb"/archive/2026/al09/al092026\.(?:public(?:_a)?|fstadv)\.\d{3}\.shtml")
PUBLIC_TIME = re.compile(r"^(\d{1,2})(\d{2}) (AM|PM) (AST|EDT|CDT|EST|CST) [A-Z]{3} ([A-Z]{3}) (\d{1,2}) (20\d{2})$", re.M | re.I)
FORECAST_TIME = re.compile(r"^(\d{2})(\d{2}) UTC [A-Z]{3} ([A-Z]{3}) (\d{1,2}) (20\d{2})$", re.M | re.I)
PUBLIC_POSITION = re.compile(r"^LOCATION\.\.\.(\d+\.\d)([NS])\s+(\d+\.\d)([EW])$", re.M | re.I)
FORECAST_POSITION = re.compile(r"^(?:[A-Z -]+ )?CENTER LOCATED NEAR +"
                               r"(\d+\.\d)([NS])\s+(\d+\.\d)([EW]) AT +\d+/\d{4}Z$", re.M)
FORECAST_MPH_FACTOR = 1.150779448
EPSILON = 1e-10


def _shape_record(raw, target):
    if len(raw) < 100 or struct.unpack_from(">i", raw)[0] != 9994:
        raise ValueError("Census ZIP did not contain an ESRI shapefile")
    cursor = 100
    while cursor + 8 <= len(raw):
        record_id, words = struct.unpack_from(">ii", raw, cursor)
        end = cursor + 8 + 2 * words
        if end > len(raw):
            raise ValueError("Truncated Census shape record")
        if record_id == target:
            return raw[cursor + 8:end]
        cursor = end
    raise ValueError(f"Missing shapefile polygon for DBF record {target}")


def _dbf_record(data, expected):
    count, header_size, row_size = struct.unpack_from("<IHH", data, 4)
    positions = {}
    offset = 1
    for cursor in range(32, header_size - 1, 32):
        name = data[cursor:cursor + 11].split(bytes([0]), 1)[0].decode("ascii")
        length = data[cursor + 16]
        positions[name] = (offset, offset + length)
        offset += length
    for index in range(count):
        row = data[header_size + index * row_size:header_size + (index + 1) * row_size]
        if len(row) != row_size or row.startswith(b"*"):
            continue
        if all(row[positions[key][0]:positions[key][1]].decode("ascii").strip() == value for key, value in expected.items()):
            return index + 1
    raise ValueError("The named Census geographic feature is absent")


def _rings(body, target):
    with ZipFile(BytesIO(body)) as archive:
        projection = next(name for name in archive.namelist() if name.endswith(".prj"))
        reference = archive.read(projection)
        if not reference.startswith(b'GEOGCS["GCS_North_American_1983"') or b'UNIT["Degree"' not in reference:
            raise ValueError("Unexpected Census coordinate representation")
        dbf = next(name for name in archive.namelist() if name.endswith(".dbf"))
        shp = next(name for name in archive.namelist() if name.endswith(".shp"))
        shape = _shape_record(archive.read(shp), _dbf_record(archive.read(dbf), target))
    if struct.unpack_from("<i", shape)[0] != 5:
        raise ValueError("Named Census shape is not a polygon")
    parts, count = struct.unpack_from("<ii", shape, 36)
    starts = struct.unpack_from(f"<{parts}i", shape, 44) + (count,)
    points = list(struct.iter_unpack("<dd", shape[44 + 4 * parts:]))
    if len(points) != count or not parts or starts[0] != 0 or tuple(sorted(starts)) != starts:
        raise ValueError("Named Census polygon boundaries are invalid")
    rings = [points[a:b] for a, b in zip(starts, starts[1:])]
    if any(len(ring) < 4 or ring[0] != ring[-1] for ring in rings):
        raise ValueError("Expected closed Census rings")
    return rings


def _cross(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _on_segment(a, b, c):
    return abs(_cross(a, b, c)) < EPSILON and all(min(a[i], b[i]) - EPSILON <= c[i] <= max(a[i], b[i]) + EPSILON for i in (0, 1))


def _intersect(a, b, c, d):
    if _on_segment(a, b, c) or _on_segment(a, b, d) or _on_segment(c, d, a) or _on_segment(c, d, b):
        return True
    return (_cross(a, b, c) * _cross(a, b, d) < 0 and _cross(c, d, a) * _cross(c, d, b) < 0)


def _inside(point, rings):
    x, y = point
    entered = False
    for ring in rings:
        for a, b in zip(ring, ring[1:]):
            if _on_segment(a, b, point):
                return True
            if (a[1] > y) != (b[1] > y) and x < (b[0] - a[0]) * (y - a[1]) / (b[1] - a[1]) + a[0]:
                entered = not entered
    return entered


def _intersects_region(a, b, rings):
    return _inside(a, rings) or _inside(b, rings) or any(_intersect(a, b, p, q) for ring in rings for p, q in zip(ring, ring[1:]))


def _boundary(data, collect):
    folder = data / "venues" / "isaias-path"
    original = folder / "census-2025.zip"
    metadata = folder / "census-2025-receipt.json"
    if original.is_file() and metadata.is_file():
        info = json.loads(metadata.read_bytes())
        raw = original.read_bytes()
        if sha256(raw).hexdigest() != info["sha256"]:
            raise ValueError("Changed archived Census boundary ZIP")
        return info, raw
    if not collect:
        raise FileNotFoundError(original)
    opener = build_opener(ImmutableOrigin())
    failures = {}
    for link in (LISTED_BOUNDARY, NAMED_BOUNDARY):
        if paused(data):
            raise CollectionPaused
        requested_at = iso(datetime.now(timezone.utc))
        try:
            with opener.open(Request(link, headers={"User-Agent": "Weather Edge public boundary reference"}), timeout=15) as response:
                body = response.read(8_000_001)
                if len(body) > 8_000_000:
                    raise ValueError("Census ZIP exceeded expected size")
                info = {"source": link, "requested_at": requested_at, "received_at": iso(datetime.now(timezone.utc)),
                        "sha256": sha256(body).hexdigest(), "status": response.status, "matches_requested_feature": True,
                        "path": str(original.relative_to(data)), "as_printed_request_errors": failures}
        except HTTPError as error:
            failures[link] = {"status": error.code, "requested_at": requested_at, "received_at": iso(datetime.now(timezone.utc))}
            continue
        _write(original, body)
        _write(metadata, (json.dumps(info, separators=(",", ":")) + "\n").encode())
        return info, body
    raise ValueError("Census boundary archive unavailable")


def _obtain(data, path, source, collect):
    return (venue_storm._get(data, path, source, folder="isaias-path") if collect
            else venue_storm._existing(data, path, folder="isaias-path"))


def _advisory(raw, name, metadata):
    # The historical archive keeps the original dated bulletin inside PRE.
    blocks = re.findall(rb"<pre[^>]*>(.*?)</pre>", raw, re.S | re.I)
    candidates = [item for item in blocks if b"AL092026" in item]
    if not candidates:
        return None
    text = candidates[0].decode("ascii", errors="replace").replace("\r", "").replace("&nbsp;", " ")
    advisory_type = name.split(".")[1]
    if advisory_type == "fstadv":
        stamp, where = FORECAST_TIME.search(text), FORECAST_POSITION.search(text)
        wind = re.search(r"^MAX SUSTAINED WINDS +(?:\.\.\.)?(\d+) KT", text, re.M)
        if not (stamp and where and wind):
            return None
        hour, minute, month, day, year = stamp.groups()
        tz = timezone.utc
        original_units = "kt"
        speed_mph = int(wind[1]) * FORECAST_MPH_FACTOR
    else:
        stamp, where = PUBLIC_TIME.search(text), PUBLIC_POSITION.search(text)
        wind = re.search(r"^MAXIMUM SUSTAINED WINDS\.\.\.(\d+) MPH", text, re.M)
        if not (stamp and where and wind):
            return None
        hour, minute, marker, abbreviation, month, day, year = stamp.groups()
        hour = str(int(hour) % 12 + (12 if marker == "PM" else 0))
        tz = timezone(timedelta(hours=ZONES[abbreviation]))
        original_units = "mph"
        speed_mph = int(wind[1])
    timestamp = datetime.strptime(f"{year} {month} {day} {int(hour):02}{minute}", "%Y %b %d %H%M").replace(tzinfo=tz)
    lat, north, lon, east = where.groups()
    return {"at": iso(timestamp), "document": name, "kind": advisory_type, "storm_id": "AL092026",
            "longitude": float(lon) * (1 if east == "E" else -1), "latitude": float(lat) * (1 if north == "N" else -1),
            "source_wind": int(wind[1]), "source_wind_units": original_units, "sustained_mph": speed_mph,
            "threshold_met": speed_mph >= HURRICANE_MPH, "receipt": metadata}


def _positions(advisories, *, include_intermediate):
    by_clock = defaultdict(list)
    for row in advisories:
        if include_intermediate or row["kind"] != "public_a":
            by_clock[row["at"]].append(row)
    output, conflicts = [], []
    for clock, records in sorted(by_clock.items()):
        signatures = {(row["longitude"], row["latitude"], row["threshold_met"]) for row in records}
        if len(signatures) != 1:
            conflicts.append({"at": clock, "records": records})
            # Keep a barrier so a conflicting issuance cannot be bridged by
            # a segment that the governing consecutive-doc rule did not permit.
            output.append(None)
            continue
        witness = min(records, key=lambda row: {"public": 0, "public_a": 1, "fstadv": 2}[row["kind"]])
        output.append({**witness, "confirming_docs": [row["document"] for row in records]})
    return output, conflicts


def _segments(positions, polygon, start, finish):
    pairs = []
    for a, b in zip(positions, positions[1:]):
        if a is None or b is None:
            continue
        ta, tb = parse_time(a["at"]), parse_time(b["at"])
        p, q = (a["longitude"], a["latitude"]), (b["longitude"], b["latitude"])
        intersects = _intersects_region(p, q, polygon)
        winds = a["threshold_met"] and b["threshold_met"]
        short = timedelta(0) < tb - ta <= timedelta(hours=12)
        eligible = start < ta < tb < finish
        pairs.append({"a": a, "b": b, "intersects_alabama": intersects, "both_winds_qualify": winds, "valid_time_gap": short,
                      "inside_native_dates": eligible, "qualifies": intersects and winds and short and eligible,
                      "a_inside_alabama": _inside(p, polygon), "b_inside_alabama": _inside(q, polygon),
                      "gap_hours": (tb - ta).total_seconds() / 3600})
    return pairs


def _prices(data):
    with venue_archive.connect(data) as db:
        return [dict(row) | {"body": json.loads(row["body"])} for row in db.execute(
            """SELECT at,native_key,kind,receipt_id,body FROM history WHERE venue=? AND instrument_id=?
            AND at >= '2026-10-10T01:00:00Z' AND at <= '2026-10-10T10:00:00Z'
            AND kind IN ('trade','candle-60m') ORDER BY at,native_key""", (venue_archive.KALSHI, NATIVE))]


def _price_timeline(rows, crossing, landfall):
    trades, asks = [], []
    for row in rows:
        kind = row["kind"]
        raw = (row["body"].get("yes_price_dollars") if kind == "trade"
               else venue_archive.kalshi_dollar_close(row["body"].get("yes_ask")))
        try:
            yes = decimal(raw)
        except ValueError:
            continue
        if 0 <= yes <= 1:
            (trades if kind == "trade" else asks).append({"at": row["at"], "yes": float(yes)})
    before = [point for point in trades if parse_time(point["at"]) < parse_time(crossing["b"]["at"])]
    after = [point for point in trades if parse_time(point["at"]) >= parse_time(crossing["b"]["at"])]
    markers = [{"at": crossing[key]["at"], "label": f"{crossing[key]['sustained_mph']:.0f} mph {state} Alabama", "kind": "report"}
               for key, state in (("a", "outside"), ("b", "inside"))]
    if landfall:
        markers.insert(0, {"at": "2026-10-10T01:30:00Z", "label": "NHC Florida landfall", "kind": "report"})
    return {"series": [{"label": name, "kind": kind, "points": points}
                       for name, kind, points in (("Kalshi trade prints", "trade", trades), ("Kalshi hour-end YES ask", "display", asks)) if points],
            "markers": markers}, {"before": before[-1] if before else None, "after": after[0] if after else None}


def gather(data, *, collect=False):
    if collect and paused(data):
        raise CollectionPaused
    boundary, body = _boundary(data, collect)
    polygon = _rings(body, {"STATEFP": "01", "STUSPS": "AL"})
    index, source = _obtain(data, Path("nhc-index.html"), STORM_INDEX, collect)
    expect = sorted(set(path.decode("ascii") for path in ADVISORY_PATH.findall(index)))
    if not expect:
        raise ValueError("Original NHC index lacks Isaias advisory links")
    native = venue_archive.detail(data, venue_archive.KALSHI, NATIVE)
    if not native or native["family"] != "storm-path":
        raise ValueError("Missing exact normalized native Isaias instrument")
    failure, advisories = [], []
    for path in expect:
        if collect and paused(data):
            raise CollectionPaused
        name = path.rsplit("/", 1)[-1]
        url = NHC + path
        try:
            raw, receipt = _obtain(data, Path(name), url, collect)
            parsed = _advisory(raw, name, receipt)
        except (ValueError, OSError, URLError) as error:
            failure.append({"source": url, "detail": str(error)})
            continue
        if parsed:
            advisories.append(parsed)
        else:
            failure.append({"source": url, "detail": "Advisory position, issuance or wind identifier unavailable"})
    _, landfall = _obtain(data, Path("official-destin-landfall.html"), OFFICIAL_LANDFALL, collect)
    begin = parse_time(native["raw_market"]["created_time"])
    finish = parse_time(native["raw_market"]["expected_expiration_time"])
    replays = {}
    for label, includes in (("complete published sequence", True), ("regular advisory cadence", False)):
        positions, conflicts = _positions(advisories, include_intermediate=includes)
        replays[label] = {"segments": _segments(positions, polygon, begin, finish), "same_clock_conflicts": conflicts,
                          "positions": [row for row in positions if row is not None]}
    return {"version": VERSION, "native": native, "boundary": boundary, "geometry": polygon, "expected_advisories": len(expect),
            "archive_index": source, "operational_landfall": landfall, "advisories": advisories, "missed": failure,
            "replays": replays, "clock": [iso(begin), iso(finish)], "native_prices": _prices(data)}


def evaluate(data, corpus):
    frozen, digest = research_model.freeze_inputs(data, {**corpus, "version": VERSION})
    native = corpus["native"]
    replays, cases = {}, []
    for clock, entry in corpus["replays"].items():
        segments = entry["segments"]
        diagnostics = Counter((row["intersects_alabama"], row["both_winds_qualify"]) for row in segments)
        intersects = [row for row in segments if row["intersects_alabama"]]
        replays[clock] = {"clock_positions": len(entry["positions"]), "adjacent_segments": len(segments),
                          "crossing_segments": len(intersects), "crossing_with_qualifying_winds": diagnostics[(True, True)],
                          "qualifying_segments": sum(row["qualifies"] for row in segments),
                          "same_clock_conflicts": len(entry["same_clock_conflicts"])}
        first = next((row for row in intersects if not row["a_inside_alabama"] and row["b_inside_alabama"]), None)
        if first and clock == "complete published sequence":
            a, b = first["a"], first["b"]
            timeline, around = _price_timeline(corpus["native_prices"], first, corpus["operational_landfall"])
            position_fields = ("at", "longitude", "latitude", "sustained_mph")
            track = [{key: row[key] for key in position_fields} for row in entry["positions"]]
            cases.append({"venue": venue_archive.KALSHI, "instrument_id": NATIVE, "date": b["at"][:10], "at": b["at"],
                          "title": "Isaias crossed the Alabama boundary below the rule's wind threshold",
                          "detail": f"The center was outside at {a['at'][11:16]}Z with {a['sustained_mph']:.0f} mph. "
                                    f"The next retained point was inside at {b['at'][11:16]}Z with {b['sustained_mph']:.0f} mph. "
                                    f"Both needed at least 74 mph. Native YES paid ${native['yes_payout']}.",
                          "venue_price_timeline": timeline,
                          "storm_track": {"positions": track, "boundary_rings": corpus["geometry"], "threshold_mph": HURRICANE_MPH,
                                          "crossing_after_at": a["at"], "crossing_observed_inside_at": b["at"], "region": "Alabama"},
                          "data": {"crossing": first, "native_rule": {key: native[key] for key in (
                              "rules_primary", "rules_secondary", "settlement_receipt", "settlement_at", "yes_payout")},
                              "official_landfall": corpus["operational_landfall"], "state_source": corpus["boundary"],
                              "nearest_native_trades": around,
                              "track": [{key: row[key] for key in ("at", "longitude", "latitude", "sustained_mph", "threshold_met")}
                                        for row in entry["positions"]]}})
    historical = {"archived_advisories": len(corpus["advisories"]), "listed_advisories": corpus["expected_advisories"],
                  "original_boundaries": len(corpus["geometry"]), "native_yes_paid": native["yes_payout"], "rule_checks": replays}
    metrics = [{"group": key, "label": "Native geographic and wind criteria", **item} for key, item in replays.items()]
    record = {"version": VERSION, "supersedes": EARLIER, "title": "Isaias crossed Alabama after losing hurricane-strength winds",
              "hypothesis": "An observed storm track inside the state is insufficient when consecutive advisories must both meet a wind cutoff.",
              "target": "Raw official advisory-center segments against the exact named Census boundary and the native verified payout",
              "date_window": {"native observation terms": corpus["clock"]}, "venues": ["Kalshi"], "coverage": historical,
              "metrics": metrics, "cases": cases, "missing": corpus["missed"],
              "limits": ["The native URL returned HTTP 404. The replacement is the Census 2025 same-named national 1:500,000 ZIP with both required feature IDs.",
                         "Regular and intermediate positions are audited independently of the native payout. Forecast-converted wind retains the original units.",
                         "The native API paid the listed binary amount. It does not identify the internal settlement decision witness.",
                         "Original venue trade timestamps and NHC bulletin issue times cover separate feeds. Display and trade history give no executable book size.",
                         "This is a single observed storm. It does not estimate market frequency or executed strategy returns."],
              "provenance": {"NHC archive": STORM_INDEX, "Original Census rule URL": LISTED_BOUNDARY,
                             "Documented federal boundary ZIP": NAMED_BOUNDARY, "frozen_input": str(frozen.relative_to(data)),
                             "frozen_input_sha256": digest, "source_sha256": research_model.run_sources(sys.modules[__name__], venue_storm)}}
    return research_model.write_record(data, record, {"clock": corpus["clock"], "replays": corpus["replays"]})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--collect", action="store_true")
    parser.add_argument("--input", type=Path)
    args = parser.parse_args(argv)
    try:
        corpus = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else gather(args.data, collect=args.collect)
        print(evaluate(args.data, corpus))
    except CollectionPaused:
        print("Collection is stopped. Retained receipts are available.")
        return 2


if __name__ == "__main__":
    sys.exit(main())
