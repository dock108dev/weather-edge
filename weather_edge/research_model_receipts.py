"""Measure which station evidence became available first on the local live tape."""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from . import collector, expansion_metar, expansion_weather, history, research_model
from .nbm import climate_window, iso, utc

SOURCES = ("NWS", "Aviation Weather Center", "Iowa State MADIS HFMETAR")
VERSION = "live-station-receipts-v1"


def observations(data, cutoff):
    output = {}
    first_receipt = {}
    with closing(collector.connection(data)) as db:
        for station, day, source, payload in db.execute("""
            SELECT station, day, source, payload FROM weather
            WHERE family IN ('observation', 'research_observation')
            ORDER BY seen
        """):
            if source not in SOURCES:
                continue
            row = json.loads(payload)
            at = utc(row["at"])
            arrival = utc(row["available_at"])
            if not at <= arrival <= cutoff:
                continue
            station_source = source, station
            first_receipt[station_source] = min(first_receipt.get(station_source, arrival), arrival)
            message = history._report_key(row.get("report") or "")
            key = station, source, row["at"], message, row["temp_f"]
            if key not in output or arrival < utc(output[key]["available_at"]):
                output[key] = {
                    **row, "station": station, "date": day, "report_text_key": message,
                    "lag_seconds": (arrival - at).total_seconds(),
                }
    return list(output.values()), first_receipt


def _summary(latencies):
    ordered = sorted(latencies)
    return {"seconds_median": statistics.median(ordered), "seconds_p90": ordered[math.ceil(0.9 * len(ordered)) - 1],
            "seconds_min": ordered[0], "seconds_max": ordered[-1]}


def _paired(rows):
    unique = defaultdict(dict)
    for row in rows:
        source = row["source"]
        if source not in SOURCES[:2] or not row["report_text_key"]:
            continue
        key = row["station"], row["at"], row["report_text_key"]
        prior = unique[key].get(source)
        if prior is None or row["available_at"] < prior["available_at"]:
            unique[key][source] = row
    result = []
    for (station, obs, _), pair in unique.items():
        if len(pair) != 2:
            continue
        a, n = pair["Aviation Weather Center"], pair["NWS"]
        lead = (utc(n["available_at"]) - utc(a["available_at"])).total_seconds()
        result.append({"station": station, "at": obs, "date": a["date"], "awc_first_seconds": lead, "awc": a, "nws": n})
    return result


def _fresh_bounds(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[row["station"], row["date"]].append(row)
    updates = []
    for (station, day), readings in groups.items():
        geography = collector.WEATHER_STATIONS.get(station, expansion_weather.STATIONS.get(station))
        if geography is None:
            continue
        zone = geography[3]
        start, end = climate_window(day, zone)
        best, best_ordinary = None, None
        for row in sorted(readings, key=lambda item: item["available_at"]):
            arrived = utc(row["available_at"])
            if arrived >= end:
                continue
            raw = expansion_metar.metar_low_evidence(row, station, start, arrived)
            if not raw:
                continue
            evidence = min(raw, key=lambda item: item["upper_f"])
            value = evidence["upper_f"]
            extra = row["source"] == SOURCES[2]
            if not extra:
                best_ordinary = value if best_ordinary is None else min(value, best_ordinary)
            if best is None or value < best:
                prior = best
                best = value
                updates.append({
                    "station": station, "date": day, "at": row["available_at"], "source": row["source"],
                    "new_upper_f": value, "previous_any_source_upper_f": prior, "ordinary_sources_upper_f": best_ordinary,
                    "hf_only": extra and (best_ordinary is None or value < best_ordinary),
                    "evidence": evidence,
                })
    return updates


def study(data, cutoff=None):
    data = Path(data)
    cutoff = utc(cutoff or datetime.now(timezone.utc))
    frozen, starts = observations(data, cutoff)
    active = [row for row in frozen if utc(row["at"]) >= starts[row["source"], row["station"]]]
    pairs = _paired(active)
    updates = _fresh_bounds(frozen)
    metrics = []
    for source in SOURCES:
        available = [row for row in active if row["source"] == source]
        if not available:
            continue
        tags = {(row["station"], row["at"]) for row in available}
        earlier_other = {(row["station"], row["at"]) for row in active if row["source"] != source}
        metrics.append({
            "method": source, "label": source, "group": "live station samples", "station_times": len(tags),
            "unique_station_times_in_source": len(tags - earlier_other), "raw_report_count": len(available),
            "station_days": len({(row["station"], row["date"]) for row in available}),
            "earliest_measured": min(row["at"] for row in available),
            "latest_received": max(row["available_at"] for row in available),
            **_summary([row["lag_seconds"] for row in available]),
        })
        source_updates = [row for row in updates if row["source"] == source
                          and utc(row["at"]) >= starts[source, row["station"]]]
        metrics.append({"method": f"{source} bound updates", "label": source, "group": "precision-bound changes",
                        "station_days": len({(row["station"], row["date"]) for row in source_updates}),
                        "upper_bound_updates": len(source_updates), "hf_only_at_receipt": sum(row["hf_only"] for row in source_updates)})
    if pairs:
        metrics.append({"method": "metar_pair", "label": "Identical precise station reports", "group": "AWC before NWS",
                        "station_times": len(pairs), "station_days": len({(row["station"], row["date"]) for row in pairs}),
                        "awc_earlier": sum(row["awc_first_seconds"] > 0 for row in pairs),
                        "nws_earlier": sum(row["awc_first_seconds"] < 0 for row in pairs),
                        **_summary([row["awc_first_seconds"] for row in pairs])})
    cases = []
    used = set()
    for row in sorted(pairs, key=lambda pair: pair["awc_first_seconds"], reverse=True):
        if row["station"] in used:
            continue
        used.add(row["station"])
        a, n = row["awc"], row["nws"]
        text = (f"Aviation Weather Center received at {a['available_at']}. NWS received at {n['available_at']}. "
                f"Identical station report arrived {row['awc_first_seconds'] / 60:.1f} minutes sooner through AWC.")
        cases.append({"station": row["station"], "date": row["date"], "at": a["available_at"], "mode": "live",
                      "title": f"{row['station']} matched observation receipt", "detail": text,
                      "observation": a, "matched_nws_observation": n})
        if len(cases) == 4:
            break
    for row in [x for x in updates if x["hf_only"]][-2:]:
        cases.append({"station": row["station"], "date": row["date"], "at": row["at"], "mode": "live",
                      "title": f"{row['station']} supplementary minimum evidence",
                      "detail": (f"Five-minute HFMETAR lowered the then-known precise ceiling to "
                                 f"{row['new_upper_f']}°F. Its own local receipt determines the clock."),
                      "observation": row["evidence"], "bound_update": row})
    days = sorted({row["date"] for row in active})
    record = {
        "version": VERSION, "title": "Station data density and first local receipt",
        "hypothesis": "Faster or denser exact-station reports can substantively update the model before the separate NWS observation path.",
        "target": "Exact-station local observation receipt latency on the long live tape",
        "venues": [], "date_window": {"live": days}, "metrics": metrics, "cases": cases,
        "limits": [
            "Latency is local first receipt for a report at the controller's existing polling cadence. The provider's earliest possible publication time is unknown.",
            "Measurements before a source's first local receipt at the particular station are omitted from latency comparisons because they include startup history.",
            "Aviation Weather Center and NWS comparisons require matching normalized raw METAR text, station and measurement time. Corrections can produce different raw text at one timestamp.",
            "Five-minute supplementary observations keep provenance and can improve density while arriving later. This audit preserves their original display-only role.",
        ],
        "provenance": {"cutoff": iso(cutoff), "warmup_by_source": {
                           source: {station: iso(at) for (kind, station), at in starts.items() if source == kind} for source in SOURCES},
                       "source_sha256": research_model.run_sources(sys.modules[__name__], expansion_metar),
                       "source": "weather.sqlite3 exact local weather receipt rows"},
    }
    return research_model.write_record(data, record, {"observations": frozen, "pairs": pairs, "bound_updates": updates})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=Path(".data"), type=Path)
    parser.add_argument("--cutoff", type=utc)
    args = parser.parse_args()
    print(study(args.data_dir, args.cutoff))


if __name__ == "__main__":
    main()
