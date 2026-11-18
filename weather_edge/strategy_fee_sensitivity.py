"""Reprice the same frozen paper tape under two documented fee coefficients."""

import argparse
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import gzip
import json
from pathlib import Path

from . import study
from .market import decimal, iso, paper_trial, parse_time
from .strategy_rules import StrategyRule
from .strategy_runs import (
    DEFAULT_DATA, FIXED, HIGH_PROBABILITIES, LOW_PROBABILITIES, Predictor, case_for, digest,
    frozen_predictors, json_bytes, measure, slice_tape, write_new,
)


ENGINE = "documented-taker-rate-sensitivity-v2"
RATES = (("standard Tier 1", "0.095"), ("documented reduced coefficient", "0.0695"))
RULES = ("two-sided", "mid-price", "high-no", "joint-high-low", "joint-high-guarded-low",
         "joint-native-noaa-high-guarded-low")
POLY_FEE_DOCS = "https://docs.polymarket.us/fees"


def analyze(snapshot, artifact):
    capture = snapshot["capture"]
    split = parse_time(snapshot["split"])
    phases = {"development": slice_tape(capture, parse_time(capture["started_at"]), split - timedelta(microseconds=1)),
              "forward": slice_tape(capture, split, parse_time(capture["captured_at"]))}
    low = frozen_predictors(snapshot, LOW_PROBABILITIES)
    shadows = frozen_predictors(snapshot, HIGH_PROBABILITIES)
    forecast = Predictor(artifact, low=low, high=shadows.pop("guarded", None), shadows=shadows)
    available = tuple(rule for rule in RULES if not rule.startswith("joint")
                      or (rule == "joint-high-low" and "primary" in low)
                      or (rule == "joint-high-guarded-low" and "guarded" in low)
                      or (rule == "joint-native-noaa-high-guarded-low" and "native-noaa" in shadows and "guarded" in low))
    metrics, ledgers, cases, changes = [], {}, [], []
    documented = set()
    for phase, tape in phases.items():
        for method in available:
            base_attempts, base_positions = None, {}
            rules = StrategyRule(method, "origin-confirmed", FIXED.minimum_dollars)
            for name, raw in RATES:
                rate = decimal(raw)
                trial = paper_trial(tape, forecast(method), reaction_seconds=FIXED.reaction_seconds, order_size=FIXED.order_size,
                                    bankroll=500, max_station_day=25, max_recheck_lag=15, fee_at=lambda _: rate, rules=rules)
                ledger = f"{phase}/{method}/{raw}"
                ledgers[ledger] = trial
                results = measure(trial, detailed=True, portfolio=bool(low))
                attempts = {(row["bucket_id"], row["side"], row["signal_at"]): row for row in trial["attempts"]}
                positions = {(row["bucket_id"], row["side"]): row for row in trial["positions"]}
                metrics.append({"window": phase, "strategy": method, "rate_scope": name, "taker_coefficient": raw,
                                **{key: value for key, value in results.items() if not isinstance(value, dict)}})
                if base_attempts is None:
                    base_attempts, base_positions = attempts, positions
                    continue
                additional = [attempts[key] for key in attempts if key not in base_attempts]
                common = positions.keys() & base_positions.keys()
                changes.append({"window": phase, "strategy": method, "new_signals_at_reduced_rate": len(additional),
                                "standard_rate_signals_absent_at_reduced_rate": len(base_attempts.keys() - attempts),
                                "new_position_sides": len(positions.keys() - base_positions),
                                "lost_position_sides": len(base_positions.keys() - positions),
                                "same_position_sides": len(common),
                                "shared_position_side_with_different_recheck_clock": sum(positions[key]["quote_at"] != base_positions[key]["quote_at"]
                                                                                         for key in common)})
                if phase == "forward" and additional:
                    witness = min(additional, key=lambda row: (row["result"] != "Paper filled", parse_time(row["signal_at"])))
                    observed = case_for(witness, trial, tape, phase, "origin-confirmed")
                    cases.append({**observed, "strategy": method, "taker_coefficient": raw})
                elif phase == "forward" and common - documented:
                    ident = max(sorted(common - documented), key=lambda key: abs(positions[key]["fee"] - base_positions[key]["fee"]))
                    documented.add(ident)
                    first, second = base_positions[ident], positions[ident]
                    witness = attempts[(*ident, second["signal_at"])]
                    observed = case_for(witness, trial, tape, phase, "origin-confirmed")
                    cases.append({**observed, "strategy": method, "taker_coefficient": raw,
                                  "title": f'{observed["station"]} {observed["bucket"]["label"]} · same position at both rates',
                                  "detail": f'The standard fee was ${first["fee"]:.2f} and the alternate was ${second["fee"]:.2f}.',
                                  "data": {"standard_rate_position": first, "reduced_rate_position": second}})
    return metrics, ledgers, cases, changes


def report(data, paper):
    original = json.loads(paper.read_bytes())
    provenance = original["provenance"]
    snapshot = json.loads(gzip.decompress((data / provenance["frozen_inputs"]).read_bytes()))
    if digest(snapshot) != provenance["tape_sha256"]:
        raise ValueError("Frozen paper inputs differ from the original portfolio hash")
    artifact = study.load_selected(data, vintage=snapshot["model_vintage"])
    if artifact is None:
        raise ValueError("Original NOAA model is unavailable")
    settings = {"engine": ENGINE, "original_paper": original["id"], "frozen_tape_sha256": provenance["tape_sha256"],
                "taker_rate_variants": RATES, "rule_variants": RULES, "fee_schedule": POLY_FEE_DOCS,
                "stake": FIXED.order_size, "reaction_seconds": FIXED.reaction_seconds, "minimum_net_dollars": FIXED.minimum_dollars}
    ident = "fee-sensitivity-" + digest(settings)[:20]
    path = data / "research" / "strategies" / f"{ident}.json"
    if path.is_file():
        return path
    metrics, ledgers, cases, changes = analyze(snapshot, artifact)
    evidence = data / "research" / "evidence" / "strategy-fees" / f"{ident}.json.gz"
    blob = gzip.compress(json_bytes({"settings": settings, "trials": ledgers}), mtime=0)
    write_new(evidence, blob)
    value = {"id": ident, "title": "Does the documented taker rate change paper allocation?",
             "hypothesis": "A lower documented Polymarket US fee coefficient changes which same-book signals clear the fixed after-fee floor.",
             "target": "Frozen native high and low depth on one $500 paper bankroll per comparison.",
             "venues": ["polymarket-us"], "created_at": iso(datetime.now(timezone.utc)), "date_window": original["date_window"],
             "metrics": metrics, "fee_differences": changes, "cases": cases,
             "limits": ["The default run uses the public Tier 1 coefficient. The lower coefficient is a documented sensitivity with no assumed account eligibility.",
                        "The engine conservatively prices summed exact fees rounded to cents. Displayed depth cannot identify the split across resting orders.",
                        "All rates share the same bankroll, calendar window, native books and delayed recheck. Forecast EV and realized payout remain distinct."],
             "provenance": {**settings, "replay_details_path": str(evidence.relative_to(data)), "replay_details_sha256": sha256(blob).hexdigest()}}
    write_new(path, json_bytes(value) + b"\n")
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("paper", type=Path)
    args = parser.parse_args()
    path = report(args.data, args.paper)
    program = json.loads(path.read_bytes())
    print(json.dumps({"path": str(path), "metrics": program["metrics"], "differences": program["fee_differences"]}, indent=2))


if __name__ == "__main__":
    main()
