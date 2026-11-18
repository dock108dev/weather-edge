"""Compare actual GEFS six-hour precipitation support across the holiday climate day."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone
import gzip
from hashlib import sha256
import json
import math
from pathlib import Path
from urllib.request import urlopen
from zoneinfo import ZoneInfo

from . import collector, gefs, gefs_precipitation, research_model, strategy_thanksgiving, strategy_twc, venue_archive
from .market import iso, parse_time
from .strategy_runs import json_bytes, write_new


VERSION = "thanksgiving-gefs-exact-day-v1"
THRESHOLD_MM = 0.254
DOCS = "https://www.emc.ncep.noaa.gov/emc/pages/numerical_forecast_systems/gefs.php"
ROOT = strategy_thanksgiving.ROOT / "gefs"


def station_inputs(data, baseline):
    saved = json.loads(baseline.read_bytes())
    proof = saved["provenance"]
    raw = (data / proof["replay_details_path"]).read_bytes()
    if sha256(raw).hexdigest() != proof["replay_details_sha256"]:
        raise ValueError("Thanksgiving baseline evidence checksum mismatch")
    rows = json.loads(gzip.decompress(raw))["current"]
    weather = {row["station"]: row for row in strategy_twc.reports(data, day="2026-11-14")}
    matched = {}
    for row in rows:
        station = row["station"]
        report = weather.get(station)
        if report is None:
            continue
        zone = ZoneInfo(report["timezone"])
        target = date.fromisoformat(row["venue_market"]["day"])
        start = datetime.combine(target, time.min, zone).astimezone(timezone.utc)
        end = datetime.combine(target + timedelta(days=1), time.min, zone).astimezone(timezone.utc)
        source = row["source_site"]
        matched[station] = {**row, "source_coordinates": (source["lat"], source["lon"]), "start": iso(start), "end": iso(end),
                            "timezone_proof": {key: report[key] for key in ("timezone", "source_url", "receipt_id", "label_date")}}
    return saved, matched, sorted({row["station"] for row in rows} - set(matched))


def exact_leads(issue, sites):
    lower = min(parse_time(row["start"]) for row in sites.values())
    upper = max(parse_time(row["end"]) for row in sites.values())
    return [lead for lead in range(6, 385, 6)
            if issue + timedelta(hours=lead) > lower and issue + timedelta(hours=lead - 6) < upper]


def sample_frame(data, issue, lead, member, sites, fetch):
    directory = data / ROOT / issue.strftime("%Y%m%d%H")
    path = directory / f"f{lead:03d}-m{member:02d}.json"
    if path.is_file():
        saved = json.loads(path.read_bytes())
        if set(saved["stations"]) == set(sites):
            return saved
        raise ValueError("Recorded GEFS extraction used a different exact station set")
    if not fetch or collector.paused(data):
        raise FileNotFoundError("Local GEFS member range unavailable")
    origin = gefs.object_url(issue, lead, member)
    with gefs._get(data, origin + ".idx", urlopen) as reply:
        index = reply.read(512 * 1024)
        start, stop = gefs_precipitation.indexed_precipitation(index.decode("ascii"), lead)
    body, published, received, tag = gefs._range(data, origin, start, stop, issue, urlopen)
    values = gefs_precipitation._decode(body, issue, lead, member, coordinates=sites)
    native = path.with_suffix(".grib2")
    result = {"member": member, "lead_hours": lead, "issued_at": iso(issue), "available_at": iso(published),
              "received_at": iso(received), "starts_at": iso(issue + timedelta(hours=lead - 6)),
              "ends_at": iso(issue + timedelta(hours=lead)), "source": origin, "source_etag": tag,
              "byte_range": [start, stop - 1], "object_index_sha256": sha256(index).hexdigest(),
              "raw_sha256": sha256(body).hexdigest(), "raw": str(native.relative_to(data)), "stations": values}
    write_new(native, body)
    write_new(path, json_bytes(result) + b"\n")
    return result


def member_bounds(rows, station, climate_start, climate_end):
    selected = [row for row in rows if parse_time(row["ends_at"]) > climate_start and parse_time(row["starts_at"]) < climate_end]
    selected.sort(key=lambda row: parse_time(row["starts_at"]))
    if (not selected or parse_time(selected[0]["starts_at"]) > climate_start or parse_time(selected[-1]["ends_at"]) < climate_end
            or any(old["ends_at"] != new["starts_at"] for old, new in zip(selected, selected[1:]))):
        return None
    contained = [row for row in selected if climate_start <= parse_time(row["starts_at"]) < parse_time(row["ends_at"]) <= climate_end]
    return {"lower_mm": math.fsum(row["stations"][station]["liquid_mm"] for row in contained),
            "upper_mm": math.fsum(row["stations"][station]["liquid_mm"] for row in selected),
            "member": selected[0]["member"], "full_sixes": len(contained), "overlapping_sixes": len(selected),
            "source_available_at": max((row["available_at"] for row in selected), key=parse_time),
            "local_received_at": max((row["received_at"] for row in selected), key=parse_time)}


def _case_priority(row):
    exact = row["member_amounts"][0]["full_sixes"] == row["member_amounts"][0]["overlapping_sixes"]
    mark = row["midpoint_proxy"]
    disagreement = max(row["model_fraction_lower"] - mark, mark - row["model_fraction_upper"], 0) if mark is not None else 0
    return row["complete_members"], exact, disagreement


def report(data, baseline, issue, fetch):
    original, sites, missing_timezone = station_inputs(data, baseline)
    if not sites:
        raise ValueError("No exact native station-day time zone evidence")
    used = exact_leads(issue, sites)
    if not used:
        raise ValueError("Requested GEFS issue does not cover the native holiday")
    points = {key: row["source_coordinates"] for key, row in sites.items()}
    planned = [(member, lead) for member in gefs.MEMBERS for lead in used]

    def pull(item):
        member, lead = item
        try:
            return sample_frame(data, issue, lead, member, points, fetch), None
        except (OSError, ValueError, RuntimeError) as error:
            return None, {"member": member, "lead": lead, "reason": str(error)}

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(pull, planned))
    saved = [row for row, _ in responses if row]
    gaps = [issue for _, issue in responses if issue]
    by_member = {member: [row for row in saved if row["member"] == member] for member in gefs.MEMBERS}
    results, cases = [], []
    for station, target in sites.items():
        start, close = map(parse_time, (target["start"], target["end"]))
        rows = [value for track in by_member.values() if (value := member_bounds(track, station, start, close))]
        if not rows:
            continue
        lower = sum(row["lower_mm"] >= THRESHOLD_MM for row in rows)
        upper = sum(row["upper_mm"] >= THRESHOLD_MM for row in rows)
        results.append({**target, "complete_members": len(rows), "known_wet_model_members": lower,
                        "possibly_wet_model_members": upper, "model_fraction_lower": lower / len(rows),
                        "model_fraction_upper": upper / len(rows), "member_amounts": rows,
                        "source_available_at": max((row["source_available_at"] for row in rows), key=parse_time),
                        "local_received_at": max((row["local_received_at"] for row in rows), key=parse_time)})
    for row in sorted(results, key=_case_priority, reverse=True)[:10]:
        native = row["venue_market"]
        cases.append({"station": row["station"], "date": native["day"], "at": row["local_received_at"], "venue": native["venue"],
                      "instrument_id": native["instrument_id"], "title": f'{row["station"]} Thanksgiving GEFS liquid precipitation',
                      "detail": f'Of {row["complete_members"]} complete members, {row["known_wet_model_members"]} reach 0.01 inch using complete six-hour intervals and {row["possibly_wet_model_members"]} reach it when partially overlapping intervals are included.',
                      "data": row})
    complete = [r for r in results if r["complete_members"] == len(gefs.MEMBERS)]
    field_summary = [{"source": "NOAA GEFS 0.5° public range", "issue": iso(issue), "requested_member_periods": len(planned),
                      "retained_member_periods": len(saved), "native_stations": len(sites), "stations_with_all_members": len(complete),
                      "stations_with_exactly_contained_six_hour_blocks": sum(r["member_amounts"][0]["overlapping_sixes"] == r["member_amounts"][0]["full_sixes"] for r in complete),
                      "last_local_field_receipt": max((row["received_at"] for row in saved), key=parse_time, default=None),
                      "missing_exact_local_timezones": len(missing_timezone)}]
    metrics = [*field_summary, *[{key: row[key] for key in ("station", "start", "end", "complete_members", "known_wet_model_members",
                                 "possibly_wet_model_members", "model_fraction_lower", "model_fraction_upper", "midpoint_proxy")}
                                for row in results]]
    record = {"version": VERSION, "title": "Thanksgiving precipitation across the actual GEFS ensemble",
              "hypothesis": "Native six-hour GEFS members retain enough spatial and lead-time coverage to estimate a weather-only bracket for the exact holiday.",
              "target": "Liquid equivalent of at least 0.01 inches during each station’s native November 26 calendar day.",
              "venues": [venue_archive.KALSHI], "date_window": {"from": iso(issue), "through": str(strategy_thanksgiving.thanksgiving(2026))},
              "metrics": metrics, "cases": cases,
              "limits": ["A six-hour interval crossing a local day boundary contributes zero to the lower and its entire amount to the upper bracket.",
                         "These are uncalibrated ensemble fractions at the nearest model grid cell. The earlier station climatology and the current venue marks remain separate.",
                         "All November 26 outcomes and exact native payouts are pending. No historical performance or simulated fill was inferred from this prospective ensemble."],
              "provenance": {"engine": VERSION, "GEFS": DOCS, "baseline": original["id"], "issue": iso(issue),
                             "field": "Surface six-hour APCP in kg/m², numerically millimeters liquid equivalent",
                             "grid_degrees": 0.5, "member_period_gaps": gaps, "unmapped_timezones": missing_timezone}}
    return research_model.write_record(data, record, {"stations": results, "retained_member_periods": saved, "gaps": gaps})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=venue_archive.DEFAULT_DATA)
    parser.add_argument("--issue", default="2026-11-15T12:00:00Z", type=parse_time)
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("baseline", type=Path)
    args = parser.parse_args()
    path = report(args.data, args.baseline, args.issue, args.fetch)
    record = json.loads(path.read_bytes())
    print(json.dumps({"path": str(path), "metrics": record["metrics"][:3], "gaps": record["provenance"]["member_period_gaps"][:12]}, indent=2))


if __name__ == "__main__":
    main()
