"""Compare the earlier and later HRRR issue at the identical late-day decision clock."""
from __future__ import annotations

import argparse
from collections import Counter
import gzip
from hashlib import sha256
import json
from pathlib import Path
import sys

from . import hrrr, research_hrrr_intraday, research_model, strategy_high_transfer
from .expansion_weather import STATIONS
from .nbm import climate_window

VERSION = "hrrr-fixed-cycle-high-v1"
METHODS = {"nbm": "Official hourly NBM remainder", "cycle12": "Complete 12Z HRRR", "cycle18": "Complete 18Z HRRR",
           "two_cycles": "Equal 12Z and 18Z forecast average"}


def inputs(data, source):
    record, proof = strategy_high_transfer._archived(data, source)
    pools, missing = {}, Counter()
    for split in ("calibration", "test"):
        pools[split] = []
        for row in proof[split]:
            if row["local_hour"] != 17:
                continue
            _, close = climate_window(row["date"], STATIONS[row["station"]][3])
            tracks = {f"cycle{hour}": hrrr.complete_remaining_trace(data, row["station"], row["date"], row["at"], close, cycles=(hour,))
                      for hour in (12, 18)}
            if not all(tracks.values()):
                for lane, points in tracks.items():
                    if not points:
                        missing[f"{split} · missing published complete {lane}"] += 1
                continue
            pools[split].append({**row, "tracks": tracks,
                                 "forecast_peaks": {name: max(p["temp_f"] for p in rows) for name, rows in tracks.items()}})
    return {"version": VERSION, **pools, "source_study": record["id"], "window": record["date_window"], "gaps": dict(missing),
            "source_sha256": record["provenance"]["replay_details_sha256"],
            "payouts": {venue: {f"{row['station']} {row['at']}": row["buckets"] for row in rows if row["local_hour"] == 17}
                        for venue, rows in proof["payout_cases"].items()}}


def centers(row):
    observed = row["all_observations_bound"]["lower_f"]
    peaks = row["forecast_peaks"]
    return {name: max(observed, peak) for name, peak in {
        "nbm": row["expected_remaining_max_f"], **peaks, "two_cycles": (peaks["cycle12"] + peaks["cycle18"]) / 2,
    }.items()}


def _example(venue, row):
    bucket = next(item for item in row["buckets"] if item["payout"] == 1)
    return {"station": row["station"], "date": row["date"], "at": row["at"], "venue": venue,
            "instrument_id": bucket["instrument_id"], "title": f"{row['station']} · two forecast cycles, one decision",
            "hrrr_issues": [row["tracks"][name][0]["issued_at"] for name in ("cycle12", "cycle18")],
            "detail": f"The complete 12Z trajectory peaked at {row['forecast_peaks']['cycle12']:.1f}°F and the 18Z trajectory at "
                      f"{row['forecast_peaks']['cycle18']:.1f}°F over the identical remaining hours. The observation floor was "
                      f"{row['all_observations_bound']['lower_f']}°F. The completed climate high was {row['actual_f']:.0f}°F.",
            "forecast": row["tracks"], "official_report": row["official_report"], "bucket": bucket}


def run(data, source, frozen):
    if frozen is None:
        files = inputs(data, source)
        frozen, digest = research_model.freeze_inputs(data, files)
    else:
        original = gzip.decompress(frozen.read_bytes())
        files, digest = json.loads(original), sha256(original).hexdigest()
    if files["version"] != VERSION or not files["calibration"] or not files["test"]:
        raise ValueError("Fixed cycle comparison requires matched earlier and later decisions")
    cohorts, reserve = research_hrrr_intraday._evaluate(files["calibration"], files["test"], files["payouts"], candidates=centers)
    metrics, cases, comparisons = [], [], []
    for name, rows in sorted(cohorts.items()):
        metrics.extend(research_model.metrics(rows, name, METHODS))
        comparisons.extend({"group": name, **research_model.date_bootstrap(rows, "cycle12", challenger, score)}
                           for challenger in ("cycle18", "two_cycles", "nbm") for score in ("brier", "mae"))
        if rows and name != "official":
            mixed_source = max(rows, key=lambda r: abs(r["forecast_peaks"]["cycle18"] - r["forecast_peaks"]["cycle12"]))
            case_shift = max(rows, key=lambda r: abs(r["estimates"]["cycle18"]["brier"] - r["estimates"]["cycle12"]["brier"]))
            cases.append(_example(name, mixed_source))
            if case_shift is not mixed_source:
                cases.append(_example(name, case_shift))
    return research_model.write_record(data, {"version": VERSION, "title": "Earlier and later HRRR afternoon issues, same late decision",
              "hypothesis": "The newer 18Z HRRR issue adds information when both trajectories can actually be used at 17:00 local standard time.",
              "target": "Official station-day highs and verified payout intervals with two complete native forecast cycles.",
              "date_window": files["window"], "venues": sorted(files["payouts"]), "metrics": metrics, "comparisons": comparisons,
              "coverage": {"calibration_rows": len(files["calibration"]), "missing": files["gaps"]}, "cases": cases,
              "limits": ["Both complete hourly trajectories must have been originally published by the decision. All variants share the same measured station floor and reserve.",
                         "Per-station residuals are calibrated only on earlier calendar dates. Scores describe exact outcomes and provide no price or fill comparison."],
              "provenance": {"frozen_inputs": str(frozen.relative_to(data)), "frozen_input_sha256": digest, "source": files["source_study"],
                             "source_sha256": files["source_sha256"], "guard_reserve": reserve,
                             "run_sources": research_model.run_sources(sys.modules[__name__], hrrr, research_hrrr_intraday, strategy_high_transfer)}}, cohorts)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data", type=Path, default=Path(".data"))
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--source", type=Path)
    source.add_argument("--frozen", type=Path)
    args = p.parse_args()
    print(run(args.data, args.source, args.frozen))


if __name__ == "__main__":
    main()
