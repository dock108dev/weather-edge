"""Compare origin-confirmed quotes with explicit exchange-clock safeguards."""

import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import gzip
import json
from pathlib import Path

from . import study
from .market import iso, paper_trial, parse_time, source_time_key
from .paper_runs import _fee_at
from .strategy_rules import EXCHANGE_AGE_SECONDS, StrategyRule, book_status, exchange_clock_status
from .strategy_runs import (
    DEFAULT_DATA, FIXED, HIGH_PROBABILITIES, LOW_PROBABILITIES, SHADOW_RULES, Predictor,
    digest, frozen_predictors, json_bytes, measure, representative_cases, slice_tape, write_new,
)


ENGINE = "exchange-clock-sensitivity-v2"
SETTINGS = ((None, False), (EXCHANGE_AGE_SECONDS, False), (30, False), (None, True), (30, True))


@dataclass(frozen=True)
class ClockRule(StrategyRule):
    max_exchange_age: int | None = None
    require_advance: bool = False

    def depth_status(self, book, side, at):
        reason = super().depth_status(book, side, at)
        if reason or self.max_exchange_age is None:
            return reason
        if exchange_clock_status(book, at, self.max_exchange_age):
            return "Exchange clock outside configured age window"
        return None

    def recheck_status(self, signal, candidate):
        if self.require_advance and source_time_key(candidate["source_at"]) <= source_time_key(signal["source_at"]):
            return "Recheck exchange clock did not advance"
        return None


def origin_clock_distribution(tape):
    counts = Counter()
    for event in tape["events"]:
        for bucket in event["buckets"]:
            for book in bucket["books"]:
                at = parse_time(book["at"])
                if book_status(book, None, at, "origin-confirmed"):
                    continue
                clock = source_time_key(book["source_at"])
                if clock > source_time_key(iso(at)):
                    band = "clock ahead of receipt"
                else:
                    suffixes = ((30, "0 to 30 seconds"), (120, "30 to 120 seconds"), (900, "120 to 900 seconds"))
                    band = next((label for seconds, label in suffixes
                                 if clock >= source_time_key(iso(at - timedelta(seconds=seconds)))), "over 900 seconds")
                counts[band, book.get("source_time_progress", "unknown")] += 1
    return [{"exchange_clock_age": band, "archive_source_time_progress": progress, "receipts": count}
            for (band, progress), count in sorted(counts.items())]


def run_grid(snapshot, artifact, *, portfolio=False):
    original = snapshot["capture"]
    tape = original if portfolio else {**original, "events": [event for event in original["events"]
                                                             if event.get("family", "daily-high") == "daily-high"]}
    split = parse_time(snapshot["split"])
    phases = {
        "development": slice_tape(tape, parse_time(tape["started_at"]), split - timedelta(microseconds=1)),
        "forward": slice_tape(tape, split, parse_time(tape["captured_at"])),
    }
    names = ("two-sided",)
    low, high, shadows = None, None, {}
    if portfolio:
        low = frozen_predictors(snapshot, LOW_PROBABILITIES)
        names += ("joint-high-low", "joint-high-guarded-low")
        shadows = frozen_predictors(snapshot, HIGH_PROBABILITIES)
        high = shadows.pop("guarded", None)
        if high is not None:
            names += ("joint-late-guarded-high-low",)
        if "first-receipt" in low:
            names += ("joint-late-high-first-receipt-low",)
        names += tuple(SHADOW_RULES[key] for key in SHADOW_RULES if key in shadows)
    predict = Predictor(artifact, low=low, high=high, shadows=shadows)
    fees = _fee_at(snapshot["fee_schedule"])
    trials, metrics, examples, distribution = {}, [], [], []
    for phase, capture in phases.items():
        if portfolio:
            for family in ("daily-high", "daily-low"):
                deals = [event for event in capture["events"] if event.get("family", "daily-high") == family]
                distribution.extend({"window": phase, "family": family, **value}
                                    for value in origin_clock_distribution({**capture, "events": deals}))
        else:
            distribution.extend({"window": phase, **value} for value in origin_clock_distribution(capture))
        for name in names:
            results, cases, evidence = _clock_trials(capture, predict(name), fees, phase, name, portfolio)
            metrics.extend(results)
            examples.extend(cases)
            trials.update(evidence)
    return metrics, examples, trials, distribution


def _clock_trials(capture, predict, fees, phase, name, portfolio):
    base_fills = None
    metrics, examples, trials = [], [], {}
    for age, advance in SETTINGS:
        rules = ClockRule(name, "origin-confirmed", FIXED.minimum_dollars, age, advance)
        trial = paper_trial(capture, predict, reaction_seconds=FIXED.reaction_seconds, order_size=FIXED.order_size,
                            bankroll=500, max_station_day=25, max_recheck_lag=15, fee_at=fees, rules=rules)
        group = f"{phase}/{name}" if portfolio else phase
        key = f"{group}/{age or 'full'}/{advance}"
        trials[key] = trial
        positions = {(row["venue"], row["bucket_id"], row["side"]): row for row in trial["positions"]}
        if base_fills is None:
            base_fills = positions
        retained = set(positions) & set(base_fills)
        metrics.append({"window": phase, **({"strategy": name} if portfolio else {}), "max_exchange_clock_age_seconds": age,
                        "require_exchange_clock_advance": advance, "base_fill_sides_retained": len(retained),
                        "retained_sides_filled_at_same_receipt": sum(positions[k]["quote_at"] == base_fills[k]["quote_at"] for k in retained),
                        "base_fill_sides_removed": len(base_fills) - len(retained),
                        **measure(trial, detailed=True, portfolio=portfolio)})
        if phase == "forward":
            for case in representative_cases(trial, capture, phase, "origin-confirmed"):
                examples.append({**case, "variant": key})
    return metrics, examples, trials


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--portfolio", action="store_true", help="Keep the frozen daily-high and daily-low arms on one bankroll.")
    parser.add_argument("evidence", type=Path)
    args = parser.parse_args()
    frozen = json.loads(gzip.decompress(args.evidence.read_bytes()))
    identity = digest(frozen)
    if identity != args.evidence.name.removesuffix(".json.gz"):
        parser.error("Frozen paper evidence checksum mismatch")
    artifact = study.load_selected(args.data, vintage=frozen["model_vintage"])
    if artifact is None:
        parser.error("Original model vintage unavailable")
    if args.portfolio and not {"low_probabilities", "guarded_low_probabilities"} <= frozen.keys():
        parser.error("A frozen high/low portfolio containing both low model variants is required")
    metrics, cases, trials, distribution = run_grid(frozen, artifact, portfolio=args.portfolio)
    settings = {"engine": ENGINE, "frozen_paper_input": identity, "fixed_variants": SETTINGS,
                "bankroll": 500, "station_day_cap": 25, "strategy": "two-sided", "http_policy": "origin-confirmed"}
    if args.portfolio:
        settings["strategy"] = "matched high and low portfolio"
    id_ = "clock-sensitivity-" + digest(settings)[:20]
    payload = args.data / "research" / "evidence" / "clock-sensitivity" / f"{id_}.json.gz"
    write_new(payload, gzip.compress(json_bytes({"settings": settings, "trials": trials, "clock_distribution": distribution}), mtime=0))
    capture = frozen["capture"]
    report = {"id": id_, "title": "How much did older exchange clocks affect paper fills?",
              "hypothesis": "Affirmative origin responses preserve simulated opportunities after bounding exchange timestamps and requiring progress between signal and recheck.",
              "target": "Polymarket US daily-high and daily-low books on one paper bankroll." if args.portfolio else
                        "Polymarket US daily-high books and exact two-sided simulation.",
              "venues": ["polymarket-us"], "created_at": iso(datetime.now(timezone.utc)),
              "date_window": {"from": capture["started_at"], "split": frozen["split"], "through": capture["captured_at"]},
              "metrics": metrics, "cases": cases[:12], "clock_distribution": distribution,
              "limits": ["The venue timestamp can remain steady when displayed depth is unchanged. The cap and progression rules are explicit sensitivity settings.",
                         "The same prespecified $25, 30-second, and $0.25 settings and $500 cash are replayed for each arm.",
                         "An advance is a later exchange timestamp for the same instrument. Freshness limitations remain separate from no profitable price.",
                         "The development and forward windows share the same one climate date."],
              "provenance": {**settings, "frozen_trials": str(payload.relative_to(args.data)),
                             "replay_details_path": str(payload.relative_to(args.data))}}
    path = args.data / "research" / "strategies" / f"{id_}.json"
    write_new(path, json_bytes(report) + b"\n")
    print(json.dumps({"path": str(path), "metrics": metrics, "clock_distribution": distribution}, indent=2))


if __name__ == "__main__":
    main()
