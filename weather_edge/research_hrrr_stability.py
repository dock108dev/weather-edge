"""Check whether afternoon HRRR progress survives removing dates or stations."""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date, timedelta
import gzip
from hashlib import sha256
import json
from pathlib import Path
from statistics import mean
import sys

from . import research_model

VERSION = "hrrr-afternoon-high-stability-v1"
BLOCK_DAYS = 3


def _read(data, source):
    report = json.loads(source.read_bytes())
    payload = (data / report["provenance"]["replay_details_path"]).read_bytes()
    checksum = sha256(payload).hexdigest()
    if report["version"] != "hrrr-intraday-high-v1" or checksum != report["provenance"]["replay_details_sha256"]:
        raise ValueError("This robustness comparison requires the unchanged intraday HRRR record")
    return report, checksum, json.loads(gzip.decompress(payload))["cohorts"]


def _leave_out(rows, key, method):
    groups = defaultdict(list)
    for row in rows:
        delta = row["estimates"][method]["brier"] - row["estimates"]["nbm"]["brier"]
        groups[key(row)].append(delta)
    return [{"omitted": group, "omitted_rows": len(groups[group]),
              "retained_brier_difference": mean(delta for name, values in groups.items() if name != group for delta in values)}
            for group in sorted(groups)] if len(groups) > 1 else []


def run(data, sources):
    metrics, evidence, provenance, examples, windows, venues = [], {}, {}, [], {}, set()
    for source in sources:
        record, checksum, cohorts = _read(data, source)
        first = date.fromisoformat(record["date_window"]["held"][0])
        label = "Held dates starting " + str(first)
        windows[label] = record["date_window"]["held"]
        provenance[record["id"]] = checksum
        venues.update(record["venues"])
        examples.extend(case for case in record["cases"][:2])

        def block(row):
            elapsed = (date.fromisoformat(row["date"]) - first).days
            return str(first + timedelta(days=(elapsed // BLOCK_DAYS) * BLOCK_DAYS))

        for cohort, rows in sorted(cohorts.items()):
            group = f"{label} · {cohort}"
            evidence[group] = {}
            for method in ("hrrr", "blend"):
                by_block = _leave_out(rows, block, method)
                by_station = _leave_out(rows, lambda row: row["station"], method)
                evidence[group][method] = {"three_day_group": by_block, "station": by_station}
                metrics.append({"group": group, "forecast": {"hrrr": "HRRR hourly", "blend": "Equal blend"}[method],
                                "decisions": len(rows), "calendar_dates": len({r["date"] for r in rows}),
                                "station_days": len({(r["station"], r["date"]) for r in rows}),
                                "three_day_blocks": len({block(r) for r in rows}),
                                "stations": len({r["station"] for r in rows}),
                                "worst_delta_without_one_block": max((r["retained_brier_difference"] for r in by_block), default=None),
                                "worst_delta_without_one_station": max((r["retained_brier_difference"] for r in by_station), default=None)})
    return research_model.write_record(data, {
        "version": VERSION, "title": "Do late-summer and autumn HRRR gains depend on one weather episode?",
        "hypothesis": "An afternoon probability advantage should survive removal of any one held station or short calendar block.",
        "target": "Paired bucket Brier difference from the same original NBM control, with earlier calibration fixed.",
        "date_window": windows, "metrics": metrics, "venues": sorted(venues), "cases": examples,
        "limits": ["Each omitted time group is one consecutive three-day calendar block measured from the first held date.",
                   "Only the evaluated records change. Calibration, the observation floor and original forecast clocks stay fixed.",
                   "A negative worst difference means every eligible omission still favored the candidate. A single site or block cannot support that omission check."],
        "provenance": {"original_evaluation_evidence_sha256": provenance,
                       "run_sources": research_model.run_sources(sys.modules[__name__])},
    }, evidence)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--source", type=Path, nargs="+", required=True)
    args = parser.parse_args()
    print(run(args.data, args.source))


if __name__ == "__main__":
    main()
