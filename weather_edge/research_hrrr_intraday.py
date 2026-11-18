"""Compare intraday NOAA HRRR and NBM using the same observed temperature floor."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date
import gzip
from hashlib import sha256
import json
from pathlib import Path
from statistics import mean
import sys

from . import hrrr, research_model, strategy_high_transfer
from .expansion_weather import STATIONS
from .model import _limits, _outcome
from .nbm import climate_window
from .strategy_metar_high import disagreement_reserve, guarded_probability

VERSION = "hrrr-intraday-high-v1"
CANDIDATES = {"nbm": "NBM hourly remainder", "hrrr": "HRRR hourly remainder", "blend": "Equal forecast blend"}


def _key(row):
    return row["station"], row["at"]


def _freeze(data, source):
    record, evidence = strategy_high_transfer._archived(data, source)
    result = {"version": VERSION, "source_record": record["id"], "source_dates": record["date_window"],
              "source_evidence_sha256": record["provenance"]["replay_details_sha256"], "payouts": {}}
    skipped = Counter()
    for split in ("calibration", "test"):
        result[split] = []
        for row in evidence[split]:
            _, close = climate_window(date.fromisoformat(row["date"]), STATIONS[row["station"]][3])
            track = hrrr.complete_remaining_trace(data, row["station"], row["date"], row["at"], close)
            if not track:
                skipped[f"{split} {row['local_hour']:02d} LST: incomplete original-publication coverage"] += 1
                continue
            result[split].append({**row, "hrrr_trace": track, "hrrr_remaining_f": max(x["temp_f"] for x in track)})
    result["missing"] = dict(skipped)
    for venue, rows in evidence["payout_cases"].items():
        result["payouts"][venue] = {f"{row['station']} {row['at']}": row["buckets"] for row in rows}
    return result


def _centers(row):
    floor = row["all_observations_bound"]["lower_f"]
    nbm, native = row["expected_remaining_max_f"], row["hrrr_remaining_f"]
    return {name: max(floor, guidance) for name, guidance in (
        ("nbm", nbm), ("hrrr", native), ("blend", (nbm + native) * 0.5),
    )}


def _evaluate(calibration, test, payout_sets, *, candidates=_centers):
    residuals = defaultdict(list)
    reserve = disagreement_reserve(calibration)
    for row in calibration:
        for name, value in candidates(row).items():
            residuals[row["station"], row["local_hour"], name].append(row["actual_f"] - value)

    results = defaultdict(list)
    for row in test:
        station, hour, bound = row["station"], row["local_hour"], row["all_observations_bound"]["lower_f"]
        if len(residuals[station, hour, "nbm"]) < 10:
            continue
        cohorts = {"official": row["buckets"]}
        cohorts.update({venue: matches[f"{station} {row['at']}"] for venue, matches in payout_sets.items()
                        if f"{station} {row['at']}" in matches})
        for cohort, buckets in cohorts.items():
            truth = [bucket["payout"] if cohort != "official" else _outcome(bucket, row["actual_f"]) for bucket in buckets]
            estimates = {}
            for name, raw_center in candidates(row).items():
                errors = residuals[station, hour, name]
                centers = [raw_center + delta for delta in errors]
                predictions = [guarded_probability(_limits(bucket), centers, 0.5, bound,
                                                   reserve["upper_one_sided_wilson"]) for bucket in buckets]
                estimates[name] = {"center_f": raw_center, "error_f": raw_center + mean(errors) - row["actual_f"],
                                   "probabilities": predictions,
                                   "brier": mean((p - y) ** 2 for p, y in zip(predictions, truth, strict=True))}
            results[cohort].append({**row, "buckets": buckets, "truth": truth, "estimates": estimates})
    return dict(results), reserve


def _example(venue, row):
    bucket = next(b for b in row["buckets"] if b["payout"])
    i = row["buckets"].index(bucket)
    track = row["hrrr_trace"]
    p = {method: estimate["probabilities"][i] for method, estimate in row["estimates"].items()}
    observed = row["all_observations_bound"]["lower_f"]
    return {"station": row["station"], "date": row["date"], "at": row["at"], "venue": venue,
            "instrument_id": bucket["instrument_id"], "title": f"{row['station']} at {row['local_hour']}:00 local standard",
            "detail": (f"Weather reports had established a {observed}°F lower bound. "
                       f"The HRRR {track[0]['issued_at'][11:13]}Z remainder peaked at {row['hrrr_remaining_f']:.1f}°F, "
                       f"NBM at {row['expected_remaining_max_f']:.0f}°F, and the verified high was {row['actual_f']:.0f}°F. "
                       f"The winning contract received {p['nbm']:.1%} from NBM and {p['blend']:.1%} from the blend."),
            "official_report": row["official_report"], "actual_payout": bucket, "probabilities": p,
            "source_forecast": track}


def run(data, *, source=None, frozen=None):
    if frozen is None:
        inputs = _freeze(data, source)
        frozen, digest = research_model.freeze_inputs(data, inputs)
    else:
        payload = gzip.decompress(frozen.read_bytes())
        digest, inputs = sha256(payload).hexdigest(), json.loads(payload)
    if inputs["version"] != VERSION or not inputs["calibration"] or not inputs["test"]:
        raise ValueError("The HRRR intraday comparison needs independent complete earlier and later forecast windows")
    cohorts, reserve = _evaluate(inputs["calibration"], inputs["test"], inputs["payouts"])
    comparisons, metrics, cases = [], [], []
    for cohort, results in sorted(cohorts.items()):
        for group in ("all", 13, 17):
            rows = results if group == "all" else [row for row in results if row["local_hour"] == group]
            if not rows:
                continue
            metrics.extend({**item, "bucket_decisions": sum(len(row["buckets"]) for row in rows)}
                           for item in research_model.metrics(rows, f"{cohort} · {group}", CANDIDATES))
            comparisons.extend({"group": f"{cohort} · {group}", **research_model.date_bootstrap(rows, "nbm", name, measure)}
                               for name in ("hrrr", "blend") for measure in ("brier", "mae"))
        if cohort != "official":
            ordered = sorted(results, key=lambda row: row["estimates"]["blend"]["brier"] - row["estimates"]["nbm"]["brier"])
            cases.extend(_example(cohort, row) for row in (ordered[0], ordered[-1]))
    frames = {track["raw"]: track["sha256"] for split in ("calibration", "test")
              for row in inputs[split] for track in row["hrrr_trace"]}
    record = {"version": VERSION, "title": "As-issued HRRR afternoons for exact daily-high contracts",
              "hypothesis": "A complete native 12Z or 18Z HRRR trajectory improves estimates of the remaining climate-day maximum.",
              "target": "Published daily station maxima and exact paid high-temperature intervals at 13 and 17 local standard time.",
              "venues": sorted(inputs["payouts"]), "date_window": inputs["source_dates"], "metrics": metrics,
              "comparisons": comparisons, "cases": cases,
              "limits": ["Only complete hourly trajectories from one native forecast cycle qualify. Original NOAA object publication sets the historical knowledge clock.",
                         "All forecast variants share the same observed floor and empirically calibrated source disagreement reserve. Original ASOS receipt clocks use the parent study’s ten-minute proxy.",
                         "Station/determination-hour residuals use earlier observations only. The equal blend is fixed. Exact payout cohorts require matching the published station-day's winning interval.",
                         "Payout scoring measures probabilities. It provides no executable order or strategy return evidence."],
              "provenance": {"frozen_inputs": str(frozen.relative_to(data)), "frozen_input_sha256": digest,
                             "source_record": inputs["source_record"], "source_evidence_sha256": inputs["source_evidence_sha256"],
                             "native_grib_fields": len(frames), "calibration_rows": len(inputs["calibration"]),
                             "calendar_hold_gaps": inputs["missing"], "guard_reserve": reserve,
                             "run_sources": research_model.run_sources(sys.modules[__name__], hrrr, strategy_high_transfer)}}
    return research_model.write_record(data, record, {"native_grib_manifest": frames, "cohorts": cohorts})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--source", type=Path)
    source.add_argument("--frozen", type=Path)
    args = parser.parse_args()
    print(run(args.data, source=args.source, frozen=args.frozen))


if __name__ == "__main__":
    main()
