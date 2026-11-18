"""Inspect confidence across exact settled buckets, decision clocks, and stations."""

import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import gzip
from hashlib import sha256
import json
from pathlib import Path
import statistics

from .market import iso, parse_time
from .expansion_weather import STATIONS as EXPANDED_STATIONS
from .strategy_runs import DEFAULT_DATA, POLYMARKET, digest, json_bytes, write_new
from .study import STATIONS


ENGINE = "exact-bucket-calibration-v1"
BANDS = (0.5, 0.8, 0.9, 0.97, 0.99, 1.0)
METHODS = (("noaa", "NOAA physical forecast"), ("market", "Venue probability proxy"))


def flatten(venue, panels):
    for row in panels["later test"]:
        for bucket in row["buckets"]:
            for field, method in METHODS:
                yes = bucket[field]
                if yes is None or not 0 <= yes <= 1:
                    continue
                strongest = yes >= 0.5
                yield {
                    "venue": venue, "date": row["date"], "station": row["station"], "at": row["at"],
                    "decision_hour_local_standard": (
                        parse_time(row["at"]) + timedelta(hours=(STATIONS[row["station"]] if row["station"] in STATIONS
                                                              else EXPANDED_STATIONS[row["station"]][3]))
                    ).hour,
                    "instrument_id": bucket["instrument_id"], "label": bucket.get("title", bucket.get("label")), "method": method,
                    "side": "YES" if strongest else "NO", "probability": yes if strongest else 1 - yes,
                    "paid_one": int(bucket["outcome"] == (1 if strongest else 0)),
                }


def station_day_score(rows):
    by_day = defaultdict(list)
    for row in rows:
        by_day[row["date"], row["station"]].append(row)
    if not by_day:
        return {"observed_hit_rate": None, "mean_confidence": None, "bucket_brier": None}
    return {
        "observed_hit_rate": statistics.mean(statistics.mean(row["paid_one"] for row in group) for group in by_day.values()),
        "mean_confidence": statistics.mean(statistics.mean(row["probability"] for row in group) for group in by_day.values()),
        "bucket_brier": statistics.mean(statistics.mean((row["probability"] - row["paid_one"]) ** 2
                                                        for row in group) for group in by_day.values()),
    }


def metrics(rows, group):
    failed = [r for r in rows if not r["paid_one"]]
    return {
        "group": group, "rows": len(rows), "miss_rows": len(failed),
        "distinct_dates": len({r["date"] for r in rows}),
        "distinct_station_days": len({(r["station"], r["date"]) for r in rows}),
        "miss_station_days": len({(r["station"], r["date"]) for r in failed}),
        "miss_dates": len({r["date"] for r in failed}),
        **station_day_score(rows),
    }


def display_cases(rows):
    failures = [r for r in rows if not r["paid_one"]]
    misses = sorted(failures, key=lambda r: (-r["probability"], r["date"], r["station"], r["at"]))
    seen, chosen = set(), []
    for row in misses:
        key = (row["venue"], row["method"], row["station"], row["date"])
        if key not in seen:
            seen.add(key)
            chosen.append({**row, "title": f"{row['station']} {row['label']} {row['side']} · calibration miss",
                           "detail": f"{row['method']} estimated {row['probability']:.1%} on the side that paid zero."})
        if len(chosen) == 8:
            break
    return chosen


def read_panel(data, path):
    raw = path.read_bytes()
    obj = json.loads(gzip.decompress(raw) if raw.startswith(b"\x1f\x8b") else raw)
    if "panels" in obj:
        return obj["panels"]
    pointer = obj["provenance"]
    archived = (data / pointer["replay_details_path"]).read_bytes()
    if sha256(archived).hexdigest() != pointer["replay_details_sha256"]:
        raise ValueError(f"Frozen validation input checksum mismatch for {path}")
    return json.loads(gzip.decompress(archived))["panels"]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", type=Path, default=DEFAULT_DATA)
    ap.add_argument("--poly", type=Path, required=True, help="Polymarket exact-bucket anchor report")
    ap.add_argument("--kalshi", type=Path, required=True, help="Kalshi exact-bucket frozen input panel")
    args = ap.parse_args()
    data = args.data
    panels = {POLYMARKET: read_panel(data, args.poly), "kalshi": read_panel(data, args.kalshi)}
    items = [row for venue, source in panels.items() for row in flatten(venue, source)]
    settings = {
        "engine": ENGINE, "source_files": {"polymarket-us": str(args.poly.relative_to(data)) if args.poly.is_relative_to(data) else str(args.poly),
                                            "kalshi": str(args.kalshi.relative_to(data)) if args.kalshi.is_relative_to(data) else str(args.kalshi)},
        "source_panel_hash": {venue: digest(source) for venue, source in panels.items()},
        "confidence_bands": BANDS, "weighting": "same weight per station-day, then per bucket/snapshot row",
    }
    table, temporal = [], []
    for venue in panels:
        for _, method in METHODS:
            base = [r for r in items if r["venue"] == venue and r["method"] == method]
            for lo, hi in zip(BANDS, BANDS[1:]):
                subset = [r for r in base if lo <= r["probability"] < hi or hi == 1 and r["probability"] == 1]
                table.append({"venue": venue, "method": method, "confidence_lower": lo, "confidence_upper": hi,
                              **metrics(subset, f"{lo:.0%}–{hi:.0%}")})
            for station in sorted({x["station"] for x in base}):
                for hour in sorted({x["decision_hour_local_standard"] for x in base if x["station"] == station}):
                    group = [r for r in base if r["station"] == station and r["decision_hour_local_standard"] == hour]
                    temporal.append({"venue": venue, "method": method, "station": station, "local_standard_hour": hour,
                                     **metrics(group, "all confidence"),
                                     "rows_99_plus": sum(r["probability"] >= 0.99 for r in group),
                                     "misses_99_plus": sum(r["probability"] >= 0.99 and not r["paid_one"] for r in group)})
    ident = "calibration-" + digest(settings)[:20]
    result = {
        "id": ident, "title": "How much evidence supports near-certain daily-high odds?",
        "hypothesis": "Near-99% weather or venue probabilities remain reliable across distinct settled station-days and decision hours.",
        "target": "Daily high temperature bucket",
        "venues": list(panels), "created_at": iso(datetime.now(timezone.utc)),
        "date_window": {"from": min((r["date"] for r in items), default=None), "through": max((r["date"] for r in items), default=None)},
        "metrics": table,
        "cases": display_cases(items),
        "station_and_hours": temporal,
        "limits": [
            "Bucket and clock rows share one underlying settlement for a station-day.",
            "A perfect observed rate on a handful of dates cannot validate near-certain probabilities.",
            "Each venue is scored on its own payouts. Model and venue proxy samples are paired within each venue.",
        ],
        "provenance": settings,
    }
    path = data / "research" / "models" / f"{ident}.json"
    write_new(path, json_bytes(result) + b"\n")
    print(json.dumps({"path": str(path), "extremes": [row for row in table if row["confidence_lower"] >= 0.97],
                      "misses": result["cases"][:4]}, indent=2))


if __name__ == "__main__":
    main()
