"""Audit archived climate values against the complete native NWS daily reports."""
from __future__ import annotations

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date
import gzip
from hashlib import sha256
import json
from pathlib import Path
import sys

from . import expansion_weather, research_model, venue_archive

VERSION = "cli-publication-audit-v1"


def _cases(frozen):
    raw = gzip.decompress(Path(frozen).read_bytes())
    value = json.loads(raw)
    by_case = {}
    for split, rows in value["sets"].items():
        for row in rows:
            label = row.get("label") or row["official_report"]
            key = (row["station"], row["date"], label["product"])
            old = by_case.setdefault(key, {"split": split, "label": label, "targets": set()})
            old["targets"].add(row["target"])
    return by_case, sha256(raw).hexdigest()


def _verify(data, key):
    station, day, product = key
    cached = data / "expansion" / "cli" / station / f"{product}.txt"
    if not cached.is_file() and (data / "collector" / "paused").exists():
        return None, "Collection stopped before reading this original report"
    try:
        value = expansion_weather.raw_report(data, station, date.fromisoformat(day), product)
    except (KeyError, TypeError, ValueError, OSError) as error:
        return None, str(error)
    if value is None:
        return None, "No station-matched complete value in this NWS report"
    return value, None


def _payout_case(data, station, day, target, report):
    rows = venue_archive.query(data, venue="polymarket-us", family=f"daily-{target}", place=station,
                               day=day, status="settled", limit=25)["rows"]
    settled = next((row for row in rows if row["yes_payout"] == "1" and row["station"] == station
                    and row["resolution_source"] == "NWS Daily Climate Report"), None)
    if settled:
        return {"instrument_id": settled["instrument_id"], "venue": settled["venue"], "market": settled}
    return {}


def audit(data, paths):
    data = Path(data)
    studies = {}
    for path in paths:
        rows, digest = _cases(path)
        studies[str(path)] = rows, digest
    keys = sorted({key for rows, _ in studies.values() for key in rows})
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = dict(zip(keys, pool.map(lambda key: _verify(data, key), keys), strict=True))
    measurements = defaultdict(lambda: {"station_days": 0, "raw_values": 0, "agree": 0, "different": 0, "missing": 0})
    checks = []
    for path, (cases, digest) in studies.items():
        for key, case in cases.items():
            station, day, _ = key
            report, error = results[key]
            for target in sorted(case["targets"]):
                name = f"{target}_f"
                normalized = case["label"].get(name)
                actual = report.get(name) if report else None
                group = measurements[Path(path).name.split("-v1", 1)[0], case["split"], target]
                group["station_days"] += 1
                group["raw_values"] += int(actual is not None)
                group["missing"] += int(actual is None)
                if actual is not None:
                    group["agree"] += int(actual == normalized)
                    group["different"] += int(actual != normalized)
                checks.append({
                    "study": Path(path).name,
                    "split": case["split"], "station": station, "date": day, "target": target,
                    "annual_value_f": normalized, "raw_value_f": actual, "report": report, "error": error,
                    "input_sha256": digest,
                })
    witnesses = []
    differences = [row for row in checks if row["raw_value_f"] is not None and row["raw_value_f"] != row["annual_value_f"]]
    for row in differences[:12]:
        station, day, target, report = (row[key] for key in ("station", "date", "target", "report"))
        witnesses.append({
            "station": station, "date": day, "at": report["published_at"],
            "title": f"{station} {day} · different {target} values",
            "detail": (f"Annual transport {row['annual_value_f']}°F. "
                       f"Complete NWS climate report {row['raw_value_f']}°F."),
            "official_report": report, **_payout_case(data, station, day, target, report),
        })
    if not witnesses:
        for key in keys[:3]:
            station, day, _ = key
            report, error = results[key]
            if not report:
                continue
            witnesses.append({
                "station": station, "date": day, "at": report["published_at"],
                "title": f"{station} {day} · verified NWS report",
                "detail": f"NWS high {report.get('high_f')}°F and low {report.get('low_f')}°F.",
                "official_report": report,
            })
    return research_model.write_record(data, {
        "version": VERSION,
        "title": "Annual climate summaries checked against original daily reports",
        "hypothesis": "The annual archive values used to score HRRR and GEFS agree with the complete station and date report identified by the archive.",
        "target": "NWS daily high and low",
        "date_window": {"first_date": min(key[1] for key in keys), "last_date": max(key[1] for key in keys)},
        "metrics": [{"study": name, "window": split, "target": target, **counts}
                    for (name, split, target), counts in sorted(measurements.items())],
        "cases": witnesses, "venues": ["polymarket-us"],
        "limits": [
            "This compares the specific complete NWS product linked from the archived annual transport.",
            "The audit preserves native product issue time. Separate venue-specific publication, revisions, and fallback rules still govern payouts.",
        ],
        "provenance": {
            "input_sha256": {path: digest for path, (_, digest) in studies.items()},
            "distinct_reports_requested": len(keys),
            "distinct_reports_verified": sum(report is not None for report, _ in results.values()),
            "source_code": research_model.run_sources(sys.modules[__name__], expansion_weather),
            "reproduce": "python -m weather_edge.research_cli_audit " + " ".join(f"--inputs {path}" for path in paths),
        },
    }, details=checks)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data", type=Path, default=Path(".data"))
    p.add_argument("--inputs", type=Path, action="append", required=True)
    args = p.parse_args()
    print(audit(args.data, args.inputs))


if __name__ == "__main__":
    main()
