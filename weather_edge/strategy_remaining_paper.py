"""Compare raw and remaining-day high signals on the same frozen paper tape."""

import argparse
from datetime import datetime, timedelta, timezone
import gzip
import json
from pathlib import Path

from . import collector, strategy_high_remaining, strategy_metar_high, study
from .capture import load_captures
from .market import iso, parse_time
from .paper_runs import _freeze
from .strategy_runs import (
    DEFAULT_DATA, FIXED, POLYMARKET, RUN_SPLIT, Predictor, digest, freeze_book_probabilities,
    frozen_low_predictor, json_bytes, representative_cases, run_trial, slice_tape, write_new,
)


ENGINE = "remaining-high-portfolio-v1"
METHODS = ("no-trade", "two-sided", "remaining-high", "late-remaining-high")


def alternate_source(artifact, *, precision=False):
    computed = {}
    method = strategy_metar_high.predict_runtime if precision else strategy_high_remaining.predict_runtime

    def callback(event, at):
        local_start = parse_time(event["climate_starts_at"])
        offset = (at - local_start).total_seconds() / 3600
        hour = max((x for x in artifact["decision_hours"] if x <= offset), default=None)
        key = (event["id"], hour)
        if key not in computed:
            variant = method(artifact, event, at) if hour is not None else None
            computed[key] = {row["id"]: {
                **row, "model_version": variant["version"], "model_vintage": artifact["vintage"],
                "model_method": variant["method"], "local_hour": hour,
                "forecast_issued_at": variant["issued_at"], "forecast_available_at": variant["available_at"],
                "strategy_context": {**variant["context"], "knowledge_at": variant["knowledge_at"]},
            } for row in variant["buckets"]} if variant else {}
        return computed[key]

    return callback


def analyze(snapshot, baseline):
    tape = snapshot["capture"]
    split = parse_time(snapshot["split"])
    windows = {
        "development": slice_tape(tape, parse_time(tape["started_at"]), split - timedelta(microseconds=1)),
        "forward": slice_tape(tape, split, parse_time(tape["captured_at"])),
    }
    forecaster = Predictor(baseline)
    archived = frozen_low_predictor(snapshot["remaining_probabilities"])
    precision = frozen_low_predictor(snapshot["precision_probabilities"]) if "precision_probabilities" in snapshot else None

    def rule_predictor(method):
        ordinary = forecaster(method if method in {"no-trade", "two-sided"} else "two-sided")
        if method in {"no-trade", "two-sided"}:
            return ordinary

        def apply(event, at):
            chooser = precision if method == "precision-high" else archived
            result = chooser(event, at)
            if result and (method != "late-remaining-high" or next(iter(result.values()))["local_hour"] >= 17):
                return result
            return ordinary(event, at)

        return apply

    metrics, cases, trials = [], [], {}
    methods = (*METHODS, "precision-high") if precision else METHODS
    for phase, current in windows.items():
        for method in methods:
            measured, result = run_trial(current, rule_predictor, method, FIXED, "origin-confirmed", detailed=True)
            physical_models = {"observed-plus-remaining-nbh", "metar-precision-guarded-high"}
            signals = [a for a in result["attempts"] if a["model_method"] in physical_models]
            entries = [p for p in result["positions"] if p["model_method"] in physical_models]
            metrics.append({"window": phase, "strategy": method, **measured, "physical_signals": len(signals),
                            "physical_fills": len(entries)})
            examples = representative_cases(result, current, phase, "origin-confirmed")
            cases.extend({**case, "strategy": method, "window": phase} for case in examples)
            trials[f"{phase}/{method}"] = result
    return metrics, cases, trials


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", type=Path, default=DEFAULT_DATA)
    ap.add_argument("--through")
    ap.add_argument("--rerun", type=Path)
    ap.add_argument("--precision", action="store_true", help="Compare the exact METAR high-guard variant as well.")
    args = ap.parse_args()
    data = args.data
    if args.rerun:
        snapshot = json.loads(gzip.decompress(args.rerun.read_bytes()))
        ident = "paper-remaining-" + digest(snapshot)[:20]
        saved = json.loads((data / "research" / "strategies" / f"{ident}.json").read_text())
        baseline = study.load_selected(data, vintage=snapshot["baseline_vintage"])
        metrics, cases, _ = analyze(snapshot, baseline)
        if metrics != saved["metrics"] or cases != saved["cases"]:
            ap.error("Remaining-day paper reproduction differs.")
        print(json.dumps({"verified": ident}, indent=2))
        return
    baseline = study.load_selected(data)
    challenger = strategy_high_remaining.load_runtime(data)
    capture = load_captures(data / "captures", through=args.through)
    if baseline is None or challenger is None or not capture:
        ap.error("Daily-high baseline, remaining-day artifact, and live book tape are required.")
    capture["events"] = [event for event in capture["events"] if event.get("family", "daily-high") == "daily-high"]
    cutoff = parse_time(capture["captured_at"])
    tape = _freeze(collector.enrich_capture(data, capture), cutoff)
    start = parse_time(capture["started_at"])
    split = parse_time(RUN_SPLIT)
    if not start < split < cutoff:
        ap.error("A complete development and forward tape is required.")
    snapshot = {
        "engine": ENGINE, "capture": tape, "split": RUN_SPLIT, "baseline_vintage": baseline["vintage"],
        "alternate_vintage": challenger["vintage"], "source_study": challenger["source_study"],
        "remaining_probabilities": freeze_book_probabilities(tape, alternate_source(challenger), family="daily-high"),
    }
    if args.precision:
        guard = strategy_metar_high.load_runtime(data)
        if guard is None:
            ap.error("Missing physically guarded high model. Run strategy_metar_high first.")
        snapshot["precision_vintage"] = guard["vintage"]
        snapshot["precision_probabilities"] = freeze_book_probabilities(tape, alternate_source(guard, precision=True), family="daily-high")
    ident = "paper-remaining-" + digest(snapshot)[:20]
    metrics, cases, results = analyze(snapshot, baseline)
    root = data / "research" / "evidence" / "remaining-paper"
    frozen = root / f"{ident}.json.gz"
    write_new(frozen, gzip.compress(json_bytes(snapshot), mtime=0))
    write_new(root / f"{ident}-decision-results.json.gz", gzip.compress(json_bytes(results), mtime=0))
    report = {
        "id": ident, "title": "Paper entries under raw and remaining-day highs",
        "hypothesis": "A measurement-aware late high changes after-fee entries at the same prices, size, and reaction delay.",
        "target": "Daily high temperature bucket", "venues": [POLYMARKET], "created_at": iso(datetime.now(timezone.utc)),
        "date_window": {"from": tape["started_at"], "split": RUN_SPLIT, "through": tape["captured_at"]},
        "metrics": metrics, "cases": cases,
        "limits": [
            "Each arm uses a distinct $500 ledger and an origin-confirmed, delayed recheck under the same settings.",
            "The late variant uses the physical alternative for known 17:00 and 21:00 local-standard decision clocks and falls back to NOAA otherwise.",
            "No order or quantity sizing is learned from the forward fold.",
            *( ["Precision guard uses an earlier-calibrated disagreement reserve and retains original measurement evidence."]
               if args.precision else []),
        ],
        "provenance": {"engine": ENGINE, "native_tape": str(frozen.relative_to(data)),
                       "prior_model_study": challenger["source_study"], "model_vintages": [baseline["vintage"], challenger["vintage"],
                                                                                        *([guard["vintage"]] if args.precision else [])]},
    }
    path = data / "research" / "strategies" / f"{ident}.json"
    write_new(path, json_bytes(report) + b"\n")
    print(json.dumps({"path": str(path), "metrics": metrics}, indent=2))


if __name__ == "__main__":
    main()
