"""Check for repeated weather triggers on late copies of the same METAR."""

import argparse
from datetime import datetime, timedelta, timezone
import gzip
import json
from pathlib import Path

from . import study
from .market import iso, parse_time
from .strategy_runs import (
    DEFAULT_DATA, FIXED, POLYMARKET, Predictor, digest, json_bytes, representative_cases,
    run_trial, slice_tape, write_new,
)


ENGINE = "first-weather-observation-receipt-v1"


def analyze(tape, model, split):
    start, end = parse_time(tape["started_at"]), parse_time(tape["captured_at"])
    stages = {"development": slice_tape(tape, start, split - timedelta(microseconds=1)),
              "forward": slice_tape(tape, split, end)}
    metrics, records, cases = [], {}, []
    for new_policy in (False, True):
        label = "first distinct local receipt" if new_policy else "legacy available copy"
        forecast = Predictor(model, first_observation_receipt=new_policy)
        for phase, capture in stages.items():
            measured, result = run_trial(capture, forecast, "observation", FIXED, "origin-confirmed", detailed=True)
            metrics.append({"phase": phase, "trigger": label, **measured})
            records[f"{phase}/{label}"] = result
            examples = representative_cases(result, capture, phase, "origin-confirmed")
            cases.extend({**case, "trigger": label, "window": phase} for case in examples)
    return metrics, records, cases


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", type=Path, default=DEFAULT_DATA)
    ap.add_argument("frozen", type=Path, help="A frozen paper evidence gzip preserving one older high tape.")
    args = ap.parse_args()
    data = args.data
    inputs = json.loads(gzip.decompress(args.frozen.read_bytes()))
    original_hash = digest(inputs)
    if original_hash != args.frozen.name.removesuffix(".json.gz"):
        ap.error("Frozen evidence hash differs.")
    capture, split = inputs["capture"], parse_time(inputs["split"])
    artifact = study.load_selected(data, vintage=inputs["model_vintage"])
    if artifact is None:
        ap.error("The original NOAA/Ridge vintage is required.")
    metrics, records, cases = analyze(capture, artifact, split)
    selected = {
        "engine": ENGINE, "original_run_evidence": original_hash, "model": artifact["vintage"],
        "matched_receipt_treatment": "Normalize identical original METAR/SPECI formatting. Preserve changed content and temperatures.",
    }
    ident = "observation-first-" + digest(selected)[:20]
    snapshot = data / "research" / "evidence" / "observation-receipts" / f"{ident}.json.gz"
    write_new(snapshot, gzip.compress(json_bytes({"inputs": selected, "results": records}), mtime=0))
    result = {
        "id": ident, "title": "Did two sources trigger the same observation twice?",
        "hypothesis": "First-delivery triggers preserve genuine weather updates and remove delayed copies of the same report.",
        "target": "Polymarket US daily-high observation-triggered replay",
        "venues": [POLYMARKET], "created_at": iso(datetime.now(timezone.utc)),
        "date_window": {"from": capture["started_at"], "split": inputs["split"], "through": capture["captured_at"]},
        "metrics": metrics, "cases": cases,
        "limits": [
            "Quotes, calibration, allocations, and reaction settings are shared across the two variants.",
            "One observation timestamp can have a real corrected temperature or original report. A correction is compared with the prior observed state.",
            "Receipt chance and quote cadence still affect the first usable decision.",
        ],
        "provenance": {**selected, "frozen_results": str(snapshot.relative_to(data))},
    }
    path = data / "research" / "strategies" / f"{ident}.json"
    write_new(path, json_bytes(result) + b"\n")
    print(json.dumps({"path": str(path), "metrics": metrics}, indent=2))


if __name__ == "__main__":
    main()
