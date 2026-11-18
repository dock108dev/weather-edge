"""Materialize recent weather-only station days without replacing saved replays."""

from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile

from . import archive, expansion_metar, expansion_nbm, expansion_weather, history, research_model
from .nbm import climate_window, iso, utc

STATION_NAMES = {
    "KNYC": "Central Park", "KMDW": "Chicago Midway Airport", "KPHL": "Philadelphia International Airport",
    "KDEN": "Denver International Airport", "KBOS": "Boston Logan Airport", "KORD": "Chicago O’Hare Airport",
    "KSEA": "Seattle–Tacoma International Airport", "KATL": "Hartsfield–Jackson Atlanta International Airport",
    "KDFW": "Dallas Fort Worth International Airport", "KAUS": "Austin–Bergstrom International Airport",
    "KDCA": "Ronald Reagan Washington National Airport", "KSFO": "San Francisco International Airport",
    "KLAX": "Los Angeles International Airport", "KMIA": "Miami International Airport",
    "KEWR": "Newark Liberty International Airport", "KLAS": "Harry Reid International Airport",
    "KMSY": "Louis Armstrong New Orleans International Airport", "KTTN": "Trenton–Mercer Airport",
    "KPVD": "Rhode Island T. F. Green International Airport", "KCVG": "Cincinnati/Northern Kentucky International Airport",
    "KPDX": "Portland International Airport", "KBUF": "Buffalo Niagara International Airport",
}
FIELDS = {"DPT": "dewpoint_f", "WDR": "wind_direction_tens_deg", "WSP": "wind_knots",
          "SKY": "sky_pct", "TSD": "sd_f"}


def _write_new(path, event):
    """Publish a complete file if its path is still unoccupied."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", dir=path.parent, prefix=f".{path.name}-", delete=False) as handle:
        temporary = Path(handle.name)
        try:
            json.dump(event, handle, separators=(",", ":"), allow_nan=False)
            handle.write("\n")
            handle.flush()
            try:
                os.link(temporary, path)
            except FileExistsError:
                return False
        finally:
            temporary.unlink(missing_ok=True)
    return True


def _high_forecast(card, start, end):
    points = [point for point in card["points"] if start <= utc(point["valid_at"]) < end]
    daily = next((point for point in points if utc(point["valid_at"]).hour == 0 and "TXN" in point), None)
    if not daily:
        return None
    hourly = [{"valid_at": point["valid_at"], "temp_f": point["TMP"], **{
        label: point[key] for key, label in FIELDS.items() if key in point
    }} for point in points if "TMP" in point]
    return {"issued_at": card["issued_at"], "available_at": card["available_at"], "valid_at": daily["valid_at"],
            "high_f": daily["TXN"], "high_sd_f": daily.get("XND"), "high_source": "noaa-nbm-nbs-txn",
            "hourly": hourly, "point_interval_hours": 3, "source": card["source"], "card_sha256": card["card_sha256"]}


def _clocks(data, station, start, end, *, fetch=False):
    cache = Path(data) / "madis-metar"
    if fetch:
        issues = []
        result = history._observation_receipts(station, start, end, cache,
                                               on_error=lambda error: issues.append({"source": "NOAA MADIS", "reason": error}))
        return result, issues
    timestamps = [start + timedelta(hours=hour) for hour in range(25)]
    receipts = {}
    for hour in timestamps:
        if not (cache / hour.strftime("%Y%m%d_%H00.gz")).is_file():
            continue
        try:
            for key, stamp in history._madis_hour(hour, station, cache).items():
                receipts[key] = min(receipts.get(key, stamp), stamp)
        except (OSError, ValueError, KeyError, TypeError, AttributeError, EOFError):
            continue
    return receipts, []


def _observations(rows, receipts):
    points, proxies = [], []
    for row in rows:
        raw = row["raw_report"] or ""
        clocks = receipts.get((row["at"], history._report_key(raw)))
        point = {"at": row["at"], "available_at": clocks[0] if clocks else row["available_at"],
                 "temp_f": row["temp_f"], "source": f"{row['source']} / NOAA MADIS" if clocks else row["source"],
                 "source_url": row["source_url"], "source_kind": "station_observation", "raw": raw}
        for key in ("dewpoint_f", "wind_direction_deg", "wind_speed_knots", "hour_precip_in"):
            if row[key] is not None:
                label = {"wind_speed_knots": "wind_knots", "hour_precip_in": "precip_1h_in"}.get(key, key)
                point[label] = row[key]
        if clocks:
            points.append({**point, "receipt_source": clocks[1]})
        else:
            proxies.append({**point, "timing_proxy": "10 minutes after observation (historical archive assumption)"})
    return points, proxies


def _madis_observations(receipts, station, start, end):
    points = []
    for (at, report), (received, url) in sorted(receipts.items()):
        if not start <= utc(at) < end:
            continue
        point = {"at": at, "available_at": received, "source": "NOAA MADIS METAR",
                 "source_url": url, "receipt_source": url, "source_kind": "station_observation", "raw": report}
        evidence = expansion_metar.metar_low_evidence(point, station, start, utc(received), include_six_hour=False)
        if evidence:
            degrees = evidence[0]["reported_c"]
            points.append({**point, "temp_f": round(32 + degrees * 9 / 5, 1),
                           "temperature_precision_c": evidence[0]["temperature_precision_c"]})
    return points


def _station_data(data, station, start, end, *, madis_only=False):
    labels = expansion_weather.daily_labels(data, station, start, end)
    offset = timedelta(hours=expansion_weather.STATIONS[station][3])
    asos_start, asos_end = start - timedelta(days=1), end + timedelta(days=3)
    by_day = defaultdict(list)
    if not madis_only:
        archive = expansion_weather.observation_file(data, station, asos_start, asos_end).parent
        cached_ranges = []
        for path in archive.glob(f"{station}-????-??-??-????-??-??.csv"):
            cached_start, cached_end = date.fromisoformat(path.stem[5:15]), date.fromisoformat(path.stem[16:26])
            if cached_start <= asos_start and cached_end >= asos_end:
                cached_ranges.append((cached_start, cached_end))
        if cached_ranges:
            asos_start, asos_end = min(cached_ranges, key=lambda dates: dates[1] - dates[0])
        for obs in expansion_weather.observed_data(data, station, asos_start, asos_end):
            by_day[(utc(obs["at"]) + offset).date()].append(obs)
    cards = {}
    for day in research_model._days(start - timedelta(days=1), end):
        for cycle in research_model.NBS_CYCLES:
            issue = datetime.combine(day, time(cycle), timezone.utc)
            card = expansion_nbm.load_card(data, station, issue)
            if card:
                cards[issue] = card
    return labels, by_day, cards


def _event(data, station, day, bundle, verified, madis_only=False, *, retained_reports=None, retained_receipts=None):
    lat, lon, zone, offset = expansion_weather.STATIONS[station]
    label_rows, observations, cards = bundle
    start, end = climate_window(day, offset)
    legacy_name = STATION_NAMES[station]
    receipts, issues = (_clocks(data, station, start, end, fetch=verified)
                         if retained_receipts is None else (retained_receipts, []))
    if madis_only:
        station_rows, supplementary = _madis_observations(receipts, station, start, end), []
        if not station_rows:
            issues.append({"source": "NOAA MADIS METAR", "reason": "No decoded native station readings"})
    else:
        station_rows, supplementary = _observations(observations.get(day, []), receipts)
    # Without matched receipts the archive shows the measurements separately,
    # with the assumed plotting clock carried per row.
    if supplementary:
        issues.append({"source": "IEM ASOS / NOAA MADIS",
                       "reason": f"{len(supplementary)} reports lack matched original receipt times"})
    official = list(retained_reports or [])
    label = label_rows.get(str(day))
    if retained_reports is None and label is not None:
        try:
            official = expansion_weather.reports_for_day(data, station, day, label["product"])
        except (OSError, ValueError, KeyError, TypeError) as error:
            issues.append({"source": "NWS CLI archive", "reason": str(error)})
            try:
                raw = expansion_weather.raw_report(data, station, day, label["product"])
                if raw:
                    official = [raw]
            except (OSError, ValueError, KeyError, TypeError):
                pass
    if not official:
        issues.append({"source": "NWS CLI archive", "reason": "No verified complete climate report"})
    high = [point for issue, card in cards.items() if day - timedelta(days=1) <= issue.date() <= day
            and (point := _high_forecast(card, start, end))]
    low = research_model.day_forecasts(data, station, day, cards)
    if not high:
        issues.append({"source": "NOAA NBM NBS", "reason": "No station-card daily maximum"})
    return {
        "id": f"weather-{station.lower()}-{day}", "title": f"{legacy_name} weather",
        "station": station, "station_name": legacy_name, "lat": lat, "lon": lon, "date": str(day),
        "timezone": zone, "standard_utc_offset_hours": offset, "starts_at": iso(start), "ends_at": iso(end),
        "climate_starts_at": iso(start), "climate_ends_at": iso(end), "settles_at": None,
        "official_reports": official, "observations": station_rows, "research_observations": supplementary,
        "observations_missing_receipt": len(supplementary), "forecasts": high, "minimum_forecasts": low,
        "buckets": [], "weather_only": True, "issues": issues,
    }


def build(data, stations, start, end, *, verified=False, madis_only=False):
    data = Path(data)
    options = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = {station: pool.submit(_station_data, data, station, start, end, madis_only=madis_only) for station in stations}
        for station, job in jobs.items():
            options[station] = job.result()
    def pack(key):
        station, day = key
        event = _event(data, station, day, options[station], verified, madis_only)
        path = data / "archive" / str(day) / f"{station}.json"
        new = _write_new(path, event)
        supplement = data / "expansion" / "replay" / str(day) / f"{station}.json"
        # This separate projection can be refreshed while the original archive
        # stays identical. Originals take precedence for weather and markets.
        history._write(supplement, (json.dumps(event, separators=(",", ":"), allow_nan=False) + "\n").encode())
        return {"station": station, "date": str(day), "new_archive_file": new,
                "verified_readings": len(event["observations"]), "proxy_readings": len(event["research_observations"]),
                "high_forecasts": len(event["forecasts"]), "minimum_forecasts": len(event["minimum_forecasts"]),
                "verified_official_reports": len(event["official_reports"])}

    candidates = [(station, day) for station in stations for day in research_model._days(start, end)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        return list(pool.map(pack, candidates))


def _retained_observations(data, station, days):
    by_path, by_day = defaultdict(set), defaultdict(list)
    ranges = []
    for path in (data / "expansion" / "asos").glob(f"{station}-????-??-??-????-??-??.csv"):
        first, last = date.fromisoformat(path.stem[5:15]), date.fromisoformat(path.stem[16:26])
        ranges.append((last - first, first, last, path))
    for day in days:
        matching = [row for row in ranges if row[1] <= day and day + timedelta(days=1) < row[2]]
        if matching:
            by_path[min(matching)].add(day)
    offset = timedelta(hours=expansion_weather.STATIONS[station][3])
    for (_, first, last, path), selected in by_path.items():
        source = expansion_weather.observation_url(station, first, last)
        for row in expansion_weather.decode_observations(path.read_text(), station, source):
            day = (utc(row["at"]) + offset).date()
            if day in selected:
                by_day[day].append(row)
    return by_day


def _retained_reports(data, station, days):
    grouped = defaultdict(list)
    for path in sorted((data / "expansion" / "cli" / station).glob("*.txt")):
        raw = path.read_bytes()
        match = archive.CLI_DATE.search(raw.decode(errors="replace"))
        if match is None or not expansion_weather._valid_product(path.stem, station):
            continue
        month, day, year = match.groups()
        climate_day = date(int(year), archive.MONTHS[month], int(day))
        if climate_day in days and (report := expansion_weather.decode_raw_report(raw, station, climate_day, path.stem)):
            grouped[climate_day].append(report)
    return grouped


def _retained_clocks(data, targets):
    by_hour, grouped = defaultdict(set), defaultdict(dict)
    base = data / "madis-metar"
    for station, day in targets:
        start, end = climate_window(day, expansion_weather.STATIONS[station][3])
        for n in range((end - start) // timedelta(hours=1) + 1):
            by_hour[start + timedelta(hours=n)].add(station)

    def read(item):
        at, stations = item
        name = at.strftime("%Y%m%d_%H00.gz")
        path = base / name
        if not path.is_file():
            return {}, None
        url = f"{history.MADIS}/{at:%Y/%m/%d}/point/metar/netcdf/{name}"
        try:
            return history._decode_madis_hour(path.read_bytes(), stations, url), None
        except (OSError, ValueError, KeyError, TypeError, AttributeError, EOFError) as error:
            return {}, {"source": name, "reason": str(error)}

    issues = []
    with ThreadPoolExecutor(max_workers=4) as workers:
        for decoded, issue in workers.map(read, sorted(by_hour.items())):
            if issue:
                issues.append(issue)
            for station, keys in decoded.items():
                offset = timedelta(hours=expansion_weather.STATIONS[station][3])
                for key, receipt in keys.items():
                    day = (utc(key[0]) + offset).date()
                    if (station, day) in targets:
                        lane = grouped[station, day]
                        lane[key] = min(lane.get(key, receipt), receipt)
    return grouped, issues


def build_retained(data, stations, start, end, *, madis_only=False):
    """Publish missing station-days solely from original objects retained locally."""
    data = Path(data)
    wanted = {(station, day): data / "archive" / str(day) / f"{station}.json"
              for station in stations for day in archive.date_range(start, end)}
    targets = {key: path for key, path in wanted.items() if not path.exists()}
    receipts, issues = _retained_clocks(data, targets)
    published, empty = [], []
    for station in stations:
        days = {day for code, day in targets if code == station}
        if not days:
            continue
        observations = {} if madis_only else _retained_observations(data, station, days)
        official = _retained_reports(data, station, days)
        card_days = days | {day - timedelta(days=1) for day in days}
        cards = {}
        for day in sorted(card_days):
            for cycle in research_model.NBS_CYCLES:
                issue = datetime.combine(day, time(cycle), timezone.utc)
                if card := expansion_nbm.load_card(data, station, issue):
                    cards[issue] = card
        for day in sorted(days):
            original = receipts.get((station, day), {})
            native_only = madis_only or not observations.get(day)
            event = _event(data, station, day, ({}, observations, cards), False, native_only,
                           retained_reports=official.get(day, []), retained_receipts=original)
            key = {"station": station, "date": str(day)}
            if not any(event[field] for field in ("official_reports", "observations", "research_observations", "forecasts")):
                empty.append(key)
            elif _write_new(targets[station, day], event):
                published.append({**key, "verified_readings": len(event["observations"]),
                                  "proxy_readings": len(event["research_observations"]),
                                  "high_forecasts": len(event["forecasts"]),
                                  "verified_official_reports": len(event["official_reports"])})
    return {"kept_existing": len(wanted) - len(targets), "published": published, "empty": empty,
            "retained_file_issues": issues}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path(".data"))
    parser.add_argument("--stations", nargs="+", choices=STATION_NAMES, required=True)
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    parser.add_argument("--verify-receipts", action="store_true", help="Fetch missing public original MADIS receipts")
    parser.add_argument("--madis-only", action="store_true", help="Decode exact station temperatures directly from MADIS METARs")
    parser.add_argument("--retained-only", action="store_true", help="Use locally retained original files and publish missing replay days")
    args = parser.parse_args()
    if args.start > args.end:
        parser.error("Start date must precede or equal end date")
    if args.retained_only and args.verify_receipts:
        parser.error("Retained-only replay uses stored receipt files")
    report = (build_retained(args.data_dir, args.stations, args.start, args.end, madis_only=args.madis_only)
              if args.retained_only else
              build(args.data_dir, args.stations, args.start, args.end, verified=args.verify_receipts, madis_only=args.madis_only))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
