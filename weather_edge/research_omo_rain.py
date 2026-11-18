"""Measure whether dense airport gauge receipts add timely evidence for measurable rain."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone
import gzip
from hashlib import sha256
import json
from pathlib import Path
import re
from statistics import mean, median
import sys

from . import archive, expansion_live_low, expansion_metar, expansion_weather, omo, omo_precipitation, research_model
from . import research_model_rain, research_model_rain_update, venue_archive
from .market import iso, parse_time
from .nbm import climate_window

VERSION = "madis-five-minute-measurable-rain-v1"
CLOCK_STUDY = "madis-measurable-rain-clock-sensitivity-v1"
EARLY = (date(2026, 10, 1), date(2026, 10, 18))
HELD = (date(2026, 10, 29), date(2026, 11, 13))
STATIONS = ("KPHL", "KEWR", "KORD", "KMDW", "KBOS", "KDEN", "KSEA", "KLAX", "KMIA", "KATL", "KDFW", "KSFO")
HOURS = (13, 17, 21)
P01 = re.compile(r"\bP(\d{4})\b")
METHODS = {"p06": "NOAA P06", "metar": "Contained routine and special METAR gauge",
           "public": "Add direct public file by publication", "proxy": "Add dense reading using receipt clock proxy"}


def _gauge_clocks(event, station, starts, cutoff):
    observations = [r for r in event.get("observations", []) if r.get("receipt_source") and not r.get("timing_proxy")
                    and starts <= parse_time(r["at"]) <= parse_time(r["available_at"]) <= cutoff]
    points = []
    for row in observations:
        raw = row.get("raw") or row.get("report") or row.get("raw_report") or ""
        line = expansion_metar.REPORT_START.match(raw)
        at, received = parse_time(row["at"]), parse_time(row["available_at"])
        remarks = raw.partition(" RMK ")[2]
        code = P01.search(remarks)
        if (line and line[1] == station and at.strftime("%d%H%M") == line[2] and starts + timedelta(hours=2) <= at <= received <= cutoff
                and code and int(code[1]) > 0 and "PNO" not in remarks.split()):
            points.append({"at": iso(at), "at_known": iso(received), "amount_hundredths_in": int(code[1]), "raw_report": raw,
                           "source": row["receipt_source"], "native_code": code[0]})
    if witness := research_model_rain_update.gauge_witness(observations, station, starts, cutoff):
        points.append({"at": witness["report_at"], "at_known": max(witness["available_at"], witness["period_ends_at"], key=parse_time),
                       "amount_hundredths_in": round(witness["amount_in"] * 100), **witness})
    return min(points, key=lambda r: parse_time(r["at_known"]), default=None), len(observations)


def _local_official(data, station, day):
    folder = data / "expansion" / "cli" / station
    choices = []
    for offset in (1, 2):
        for path in folder.glob(f"{day + timedelta(days=offset):%Y%m%d}*.txt"):
            try:
                row = expansion_weather.raw_report(data, station, day, path.stem)
            except (ValueError, OSError):
                continue
            if row and row["precip_in"] not in (None, "M", "MM"):
                choices.append(row)
    return max(choices, key=lambda row: row["published_at"], default=None)


def _market_rows(data):
    grouped = defaultdict(list)
    for row in venue_archive.export(data, family="daily-rain", status="settled", day_from=str(EARLY[0]), day_through=str(HELD[-1])):
        if row["station"] not in STATIONS or row["yes_payout"] not in ("0", "1"):
            continue
        strict = row["operator"] == "above" and row["lower"] == "0" and row["resolution_source"] == "The Weather Company"
        nws = row["operator"] == "at-least" and row["lower"] == "0.01" and row["resolution_source"] == "NWS Daily Climate Report"
        if strict or nws:
            grouped[row["station"], row["day"]].append({key: row[key] for key in (
                "venue", "instrument_id", "station", "day", "operator", "lower", "yes_payout", "resolution_source",
                "settlement_source", "settlement_received_at", "rules_primary", "climate_starts_at", "climate_ends_at",
            )})
    return grouped


def inputs(data, *, quarter_hour=False):
    records, sources, gaps = {"early": [], "held": []}, {}, Counter()
    symbols = _market_rows(data)
    with ThreadPoolExecutor(max_workers=3) as pool:
        for split, bounds in (("early", EARLY), ("held", HELD)):
            for day in archive.date_range(*bounds):
                at = datetime.combine(day, time(12), timezone.utc)
                source_files = [omo.load(data, at + timedelta(hours=n)) for n in range(12)]
                files = [frame for frame in source_files if frame]
                if len(files) < 6:
                    gaps[f"{split} · fewer than six archived files on a UTC date"] += 1
                    continue
                station_readings = defaultdict(list)
                for frame, readings in zip(files, pool.map(lambda row: omo_precipitation.load(data, row), files), strict=True):
                    path = frame["raw"]
                    sources[path] = {key: frame[key] for key in ("source", "sha256", "available_at", "ingested_at")}
                    for station in STATIONS:
                        station_readings[station].extend({**row, "archive": path, "source": frame["source"],
                                                           "public_file_at": frame["available_at"]} for row in readings.get(station, []))
                day_events = {event["station"]: event for event in archive.load_day(data, str(day))["events"]}
                for station, observed in station_readings.items():
                    start, _ = climate_window(day, expansion_weather.STATIONS[station][3])
                    prior = research_model_rain._weather(data, day, station)
                    official = _local_official(data, station, day)
                    if not prior or not official or not observed:
                        gaps[f"{split} · missing as-issued P06, original full report or native gauge"] += 1
                        continue
                    if split == "early" and parse_time(official["published_at"]) >= datetime.combine(HELD[0], time.min, timezone.utc):
                        continue
                    truth = int(official["precip_in"] != "T" and official["precip_in"] >= .01)
                    clocks = ([at + timedelta(minutes=minute) for minute in range(60, 12 * 60, 15)] if quarter_hour
                              else [start + timedelta(hours=hour) for hour in HOURS])
                    for clock in clocks:
                        hour = (clock - start) / timedelta(hours=1)
                        eligible = [p for p in observed if start + timedelta(hours=2) <= parse_time(p["at"]) <= clock
                                    and parse_time(p["madis_modified_at"]) + timedelta(minutes=10) <= clock]
                        if len({p["at"] for p in eligible}) < 12:
                            gaps[f"{split} · sparse source support by {hour} LST"] += 1
                            continue
                        regular, routine_count = _gauge_clocks(day_events.get(station, {}), station, start, clock)
                        if routine_count < (6 if quarter_hour else 12):
                            gaps[f"{split} · sparse original METAR receipt coverage"] += 1
                            continue
                        candidates = {"public": [], "proxy": []}
                        for point in eligible:
                            if not point["amount_hundredths_in"]:
                                continue
                            private = max(parse_time(point["madis_received_at"]), parse_time(point["madis_modified_at"]))
                            for name, available in (("public", max(private, parse_time(point["public_file_at"]))),
                                                    ("proxy", private + timedelta(minutes=10))):
                                if available <= clock:
                                    candidates[name].append({**point, "at_known": iso(available)})
                        positive = {name: min(rows, key=lambda p: p["at_known"], default=None) for name, rows in candidates.items()}
                        outcomes = [item for item in symbols[station, str(day)] if int(item["yes_payout"]) == truth]
                        records[split].append({"station": station, "date": str(day), "at": iso(clock), "hour": hour, "truth": truth,
                                               "p06": prior["forecast"]["p06_overlap_probability"], "forecast": prior,
                                               "observations": {"metar": regular, **positive}, "official_report": official, "payouts": outcomes})
    return {"version": CLOCK_STUDY if quarter_hour else VERSION, "records": records, "sources": sources,
            "gaps": dict(gaps), "quarter_hour": quarter_hour}


def _example(row):
    witness = row["observations"]["proxy"] or row["observations"]["public"]
    contract = row["payouts"][0] if row["payouts"] else {}
    return {"station": row["station"], "date": row["date"], "at": row["at"], "venue": contract.get("venue"),
            "instrument_id": contract.get("instrument_id"), "title": f"{row['station']} hourly gauge before official reporting",
            "detail": f"MADIS sampled {witness['amount_hundredths_in'] / 100:.2f} in at {witness['at']}. "
                      f"The public-object clock was {witness['public_file_at']}; the daily climate report printed {row['official_report']['precip_as_reported']} in.",
            "official_report": row["official_report"], "data": row}


def run(data, frozen, *, quarter_hour=False):
    if frozen is None:
        evidence = inputs(data, quarter_hour=quarter_hour)
        frozen, digest = research_model.freeze_inputs(data, evidence)
    else:
        raw = gzip.decompress(frozen.read_bytes())
        evidence, digest = json.loads(raw), sha256(raw).hexdigest()
    early, held = evidence["records"]["early"], evidence["records"]["held"]
    if evidence["version"] not in (VERSION, CLOCK_STUDY) or not early or not held:
        raise ValueError("Need two independent windows with matched forecasts and source receipts")
    reserves = {}
    for name in ("metar", "public", "proxy"):
        positive = {(r["station"], r["date"]): r["truth"] for r in early if r["observations"][name] or r["observations"]["metar"]}
        misses = sum(1 - value for value in positive.values())
        interval = expansion_live_low._wilson(misses, len(positive))[1] if positive else 1
        reserves[name] = {"support": len(positive), "misses": misses, "upper_error": interval}
    for row in held:
        row["estimates"] = {"p06": {"brier": (row["p06"] - row["truth"]) ** 2}}
        for name, support in reserves.items():
            p = row["p06"]
            if row["observations"][name] or row["observations"]["metar"]:
                p = 1 - support["upper_error"] * (1 - p)
            row["estimates"][name] = {"brier": (p - row["truth"]) ** 2}
    scopes = {"all": held}
    if evidence.get("quarter_hour"):
        scopes["Different available source signal"] = [r for r in held if bool(r["observations"]["metar"]) != bool(r["observations"]["proxy"])]
    scopes.update({str(hour): [r for r in held if r["hour"] == hour] for hour in HOURS})
    metrics, comparisons = [], []
    for group, rows in scopes.items():
        if rows:
            metrics.extend({"group": str(group), "method": name, "label": label, "snapshots": len(rows),
                            "calendar_dates": len({r["date"] for r in rows}), "station_days": len({(r["station"], r["date"]) for r in rows}),
                            "brier": mean(r["estimates"][name]["brier"] for r in rows)} for name, label in METHODS.items())
            comparisons.extend({"group": group, **research_model.date_bootstrap(rows, "metar", name, "brier")} for name in ("public", "proxy"))
    both = [r for r in held if r["observations"]["metar"] and r["observations"]["proxy"]]
    first = {(r["station"], r["date"]): (parse_time(r["observations"]["metar"]["at_known"]) - parse_time(r["observations"]["proxy"]["at_known"])).total_seconds() / 60
             for r in both}
    ranked = sorted((r for r in held if r["observations"]["proxy"] and r["payouts"]),
                    key=lambda row: (not row["observations"]["metar"], first.get((row["station"], row["date"]), 0), -row["p06"]), reverse=True)
    cases = {}
    for row in ranked:
        cases.setdefault((row["station"], row["date"]), row)
    result = {"version": evidence["version"], "title": ("Fifteen-minute airport rain evidence and report delays" if evidence.get("quarter_hour")
                                                        else "Dense airport precipitation and exact daily rain payouts"),
              "hypothesis": "An earlier, contained OMO gauge measurement can add evidence beyond the regular station reports.",
              "target": "Measurable precipitation in the published daily climate report where native payout is separately available.",
              "venues": sorted({s["venue"] for r in held for s in r["payouts"]}),
              "date_window": {"calibration": list(map(str, EARLY)), "held": list(map(str, HELD))},
              "metrics": metrics, "comparisons": comparisons, "cases": [_example(row) for row in list(cases.values())[:2]],
              "coverage": {"matches_with_both_positive_sources": len(first), "proxy_median_minutes_earlier": median(first.values()) if first else None,
                           "increments_with_dense_proxy_ahead_of_metar": sum(bool(r["observations"]["proxy"]) and not r["observations"]["metar"] for r in held),
                           "increments_with_direct_file_ahead_of_metar": sum(bool(r["observations"]["public"]) and not r["observations"]["metar"] for r in held),
                           "verified_payouts": len({s["instrument_id"] for r in held for s in r["payouts"]}), "gaps": evidence["gaps"]},
              "limits": ["MADIS accumulation is an overlapping report with a rolling clock near the hour. Each reading is used once as an affirmative witness. No overlapping amounts are added together.",
                         "Same-station weather and venue cash are independently verified; the venue cohort is included when it agrees with the completed station label.",
                         "The internal receipt plus 10 minutes is a sensitivity, and the explicit public file timestamp is scored separately. Earlier complete absence of precipitation does not establish a dry outcome."],
              "provenance": {"frozen_inputs": str(frozen.relative_to(data)), "frozen_input_sha256": digest, "reserve": reserves,
                             "source_docs": omo_precipitation.DOCS,
                             "source_sha256": research_model.run_sources(sys.modules[__name__], omo, omo_precipitation)}}
    return research_model.write_record(data, result, {"held": held, "source_files": evidence["sources"]})


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data", type=Path, default=Path(".data"))
    p.add_argument("--frozen", type=Path)
    p.add_argument("--quarter-hour", action="store_true")
    args = p.parse_args()
    print(run(args.data, args.frozen, quarter_hour=args.quarter_hour))


if __name__ == "__main__":
    main()
