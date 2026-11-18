"""Freeze analysis inputs, verify deterministic runs, and attach later payout evidence."""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
from uuid import uuid4
import zlib

from . import paper_eligibility
from .market import decimal, iso, paper_trial, parse_time
from .poly_fee_policy import frozen_schedule

SCHEMA = 1
LEGACY_ENGINE = "book-recheck-v2"
ENGINE = "book-recheck-v3"
FEE_SCHEDULE = frozen_schedule()
RUN_ID = re.compile(r"[0-9a-f]{24}")
DEFAULT_DATA = Path(__file__).resolve().parents[1] / ".data"


def _json(payload):
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _id(inputs):
    return hashlib.sha256(_json(inputs)).hexdigest()[:24]


def _freeze(capture, as_of):
    frozen = {key: capture.get(key) for key in ("kind", "started_at", "captured_at")}
    frozen["captured_at"] = iso(as_of)
    frozen["events"] = []
    for event in capture.get("events", []):
        if event.get("weather_only"):
            continue
        own = dict(event)
        for key in ("observations", "forecasts", "research_observations", "official_reports"):
            if key in event or key in {"observations", "forecasts"}:
                own[key] = [row for row in event.get(key, []) if parse_time(row["available_at"]) <= as_of]
        own["buckets"] = []
        for bucket in event.get("buckets", []):
            clean = {key: value for key, value in bucket.items() if key not in {"books", "prices", "settlement"}}
            if "listing_revisions" in clean:
                clean["listing_revisions"] = [row for row in clean["listing_revisions"]
                                              if parse_time(row["received_at"]) <= as_of]
            clean["books"] = [book for book in bucket.get("books", []) if parse_time(book["at"]) <= as_of]
            settlement = bucket.get("settlement")
            if settlement and parse_time(settlement["available_at"]) <= as_of:
                clean["settlement"] = settlement
            own["buckets"].append(clean)
        frozen["events"].append(own)
    return frozen


def _fee_at(schedule):
    matched = sorted((parse_time(entry["from"]), decimal(entry["coefficient"])) for entry in schedule)

    def lookup(at):
        return next(rate for start, rate in reversed(matched) if start <= at)

    return lookup


def _make_result(inputs, predict):
    engine = inputs["engine"]
    if engine not in {LEGACY_ENGINE, ENGINE}:
        raise ValueError("The saved result is available. This software implements a different paper engine.")
    data = inputs["evidence"]
    settings = dict(inputs["settings"])
    if engine == ENGINE:
        settings["clock_basis"] = inputs["clock_basis"]
    if evaluation := inputs.get("evaluation"):
        from .strategy_rules import StrategyRule

        settings["rules"] = StrategyRule(evaluation["strategy"], evaluation["freshness"], 0,
                                         evaluation["max_exchange_age"])
        settings["audit_rejections"] = evaluation.get("audit_rejections", False)
    return paper_trial(data, predict, **settings, as_of=inputs["as_of"],
                       fee_at=_fee_at(inputs["fee_schedule"]))


def create_run(capture, predict, *, as_of=None, order_size=25, reaction_seconds=5, freshness=None,
               selection=None, bankroll=500, max_station_day=25, evaluation=None, engine=ENGINE,
               clock_basis=None, fee_schedule=None, source_run=None, evidence_gaps=None, max_recheck_lag=15):
    """Freeze local evidence and every probability the engine consulted."""
    as_of = min(parse_time(as_of or capture["captured_at"]), parse_time(capture["captured_at"]))
    probabilities = {}
    inputs = {
        "engine": engine, "evidence": deepcopy(_freeze(capture, as_of)), "as_of": iso(as_of),
        "settings": {"order_size": order_size, "reaction_seconds": reaction_seconds, "bankroll": bankroll,
                     "max_station_day": max_station_day, "max_recheck_lag": max_recheck_lag},
        "fee_schedule": deepcopy(fee_schedule) if fee_schedule is not None else frozen_schedule(),
        "probabilities": probabilities,
    }
    if freshness is not None:
        inputs["settings"]["freshness"] = freshness
    if selection is not None:
        inputs["settings"]["selection"] = selection
    if evaluation is not None:
        inputs["evaluation"] = deepcopy(evaluation)
    if engine == ENGINE:
        fixture = capture.get("kind") == "fixture"
        inputs.update(policy=paper_eligibility.POLICY, fee_policy=paper_eligibility.FEE_POLICY,
                      fee_lock_basis="signal decision time",
                      clock_basis=clock_basis or (paper_eligibility.RECEIPT_CLOCK if fixture else paper_eligibility.APP_CLOCK))
        inputs["settings"]["freshness"] = (evaluation or {}).get("freshness") or freshness or ("legacy" if fixture else "origin-confirmed")
        if evaluation is not None:
            inputs["evaluation"]["clock_basis"] = inputs["clock_basis"]
    if source_run is not None:
        inputs["source_run"] = source_run
        inputs["evidence_gaps"] = evidence_gaps

    def remember(event, when):
        by_time = probabilities.setdefault(event["id"], {})
        key = iso(when)
        if key not in by_time:
            by_time[key] = deepcopy(predict(event, when) or {})
        return by_time[key]

    result = _make_result(inputs, remember)
    return {"schema": SCHEMA, "id": _id(inputs), "saved_at": iso(datetime.now(timezone.utc)),
            "inputs": inputs, "result": result}


def _recompute(inputs):
    probabilities = inputs["probabilities"]

    def frozen(event, at):
        return probabilities[event["id"]][iso(at)]

    return _make_result(inputs, frozen)


def evaluate_old(run):
    """Apply the shared rules to frozen inputs, retaining gaps and the original run."""
    verify_run(run)
    inputs = run["inputs"]
    if inputs["engine"] != LEGACY_ENGINE:
        raise ValueError("Choose a book-recheck-v2 run to evaluate under the new rules")
    tape = inputs["evidence"]
    settings = dict(inputs["settings"])
    lag = settings.pop("max_recheck_lag", 15)
    settings["freshness"] = "legacy" if tape["kind"] == "fixture" else "origin-confirmed"
    evaluation = deepcopy(inputs.get("evaluation"))
    if evaluation is not None:
        evaluation.update(freshness=settings["freshness"])
        evaluation["max_exchange_age"] = None if tape["kind"] == "fixture" else 120
    missing = {
        "books_without_first_app_availability": 0,
        "instruments_without_retained_listing_revisions": [],
        "probability_lookups_without_frozen_clock": [],
    }
    if tape["kind"] != "fixture":
        for event in tape["events"]:
            for bucket in event["buckets"]:
                books = bucket.get("books", [])
                missing["books_without_first_app_availability"] += sum(not b.get("available_at") for b in books)
                if books and not bucket.get("listing_revisions"):
                    missing["instruments_without_retained_listing_revisions"].append(bucket["id"])

    def predict(event, at):
        key = iso(at)
        values = inputs["probabilities"].get(event["id"], {})
        if key not in values:
            missing["probability_lookups_without_frozen_clock"].append({"event_id": event["id"], "at": key})
        return values.get(key, {})

    result = create_run(tape, predict, **settings, as_of=inputs["as_of"], evaluation=evaluation,
                        max_recheck_lag=lag, source_run=run["id"], evidence_gaps=missing)
    old, new = run["result"], result["result"]
    def key(row):
        return row["event_id"], row["bucket_id"], row["side"], row.get("quote_at") or row.get("book_at")
    decisions = {key(row): row["result"] for row in new["attempts"]}
    reasons = {key(row): row["reason"] for row in new["signal_rejections"]}
    result["inputs"]["comparison"] = {
        "signals": [old["signals"], new["signals"]], "fills": [old["fills"], new["fills"]],
        "changed_decisions": [
            {"event_id": row["event_id"], "instrument": row["bucket_id"], "side": row["side"],
             "book_at": row["quote_at"], "original": row["result"], "current": current}
            for row in old["attempts"]
            if (current := decisions.get(key(row), reasons.get(key(row), "No signal at the new decision clock"))) != row["result"]
        ],
    }
    result["id"] = _id(result["inputs"])
    return result


def verify_run(run):
    if run["id"] != _id(run["inputs"]):
        raise ValueError("Saved inputs do not match the run identifier")
    reproduced = _recompute(run["inputs"])
    if _json(reproduced) != _json(run["result"]):
        raise ValueError("The saved paper result could not be reproduced")
    return reproduced


def follow_payouts(run, latest):
    """Create a child run for new verified payouts with the original entries and probabilities."""
    verify_run(run)
    inputs = deepcopy(run["inputs"])
    previous = parse_time(inputs["as_of"])
    through = parse_time(latest["captured_at"])
    proof = {
        (bucket["id"], bucket.get("market_id")): bucket["settlement"]
        for event in latest.get("events", []) for bucket in event.get("buckets", [])
        if bucket.get("settlement") and previous < parse_time(bucket["settlement"]["available_at"]) <= through
    }
    pending = {position["bucket_id"] for position in run["result"]["positions"] if not position["settled"]}
    updated = 0
    for event in inputs["evidence"]["events"]:
        for bucket in event["buckets"]:
            settlement = proof.get((bucket["id"], bucket.get("market_id")))
            if bucket["id"] in pending and settlement and "settlement" not in bucket:
                bucket["settlement"] = settlement
                updated += 1
    if not updated:
        raise ValueError("No later verified payout for this run's open positions")
    inputs["as_of"] = iso(through)
    inputs["parent_run"] = run["id"]
    result = _recompute(inputs)
    payout_fields = {"settlement_available_at", "side_payout", "settled", "payout"}
    entries = lambda records: [{k: v for k, v in p.items() if k not in payout_fields} for p in records]
    if entries(result["positions"]) != entries(run["result"]["positions"]):
        raise ValueError("Payout follow-up would change historical entries")
    return {"schema": SCHEMA, "id": _id(inputs), "saved_at": iso(datetime.now(timezone.utc)),
            "inputs": inputs, "result": result}


def _directory(data_dir):
    return Path(data_dir) / "paper-runs"


def _path(data_dir, run_id):
    if not RUN_ID.fullmatch(run_id):
        raise ValueError("Invalid paper run ID")
    return _directory(data_dir) / f"{run_id}.json.gz"


def _sync_path(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def store_run(data_dir, run, *, durable=False):
    file = _path(data_dir, run["id"])
    file.parent.mkdir(parents=True, exist_ok=True)
    if file.is_file():
        if durable:
            _sync_path(file)
            _sync_path(file.parent)
            _sync_path(file.parent.parent)
        return load_run(data_dir, run["id"])
    temp = file.parent / f".{uuid4().hex}.tmp"
    try:
        temp.write_bytes(gzip.compress(_json(run), mtime=0))
        if durable:
            _sync_path(temp)
        try:
            file.hardlink_to(temp)
        except FileExistsError:
            pass
    finally:
        temp.unlink(missing_ok=True)
    if durable:
        _sync_path(file)
        _sync_path(file.parent)
        _sync_path(file.parent.parent)
    return load_run(data_dir, run["id"])


def load_run(data_dir, run_id):
    file = _path(data_dir, run_id)
    reference = f"{file.parent.name}/{file.name}"
    try:
        run = json.loads(gzip.decompress(file.read_bytes()))
    except FileNotFoundError as exc:
        raise ValueError(f"Paper run {run_id} is missing; restore {reference} from backup") from exc
    except (EOFError, OSError, ValueError, zlib.error) as exc:
        raise ValueError(f"Cannot read paper run {run_id} ({reference}): {type(exc).__name__}") from exc
    if not isinstance(run, dict) or run.get("schema") != SCHEMA or run.get("id") != run_id:
        raise ValueError(f"Unsupported or invalid paper run {run_id} ({reference})")
    return run


def describe_run(run, include_result=True):
    inputs = run["inputs"]
    model_ids = {item.get("model_version") for times in inputs["probabilities"].values()
                 for guesses in times.values() for item in guesses.values() if isinstance(item, dict)}
    model_vintages = {item.get("model_vintage") for times in inputs["probabilities"].values()
                      for guesses in times.values() for item in guesses.values() if isinstance(item, dict)}
    result = run["result"]
    return {
        "id": run["id"], "saved_at": run["saved_at"], "as_of": inputs["as_of"],
        "kind": inputs["evidence"]["kind"], "settings": inputs["settings"],
        "engine": inputs["engine"], "fee_schedule": inputs["fee_schedule"], "parent_run": inputs.get("parent_run"),
        **{key: inputs[key] for key in ("policy", "fee_policy", "clock_basis", "fee_lock_basis", "source_run",
                                       "comparison", "evidence_gaps") if key in inputs},
        "models": sorted(model_ids - {None}), "model_vintages": sorted(model_vintages - {None}), "coverage": result["coverage"],
        "signals": result["signals"], "fills": result["fills"], "realized_pnl": result["realized_pnl"],
        **({"result": result} if include_result else {}),
    }


def list_runs(data_dir, kind):
    runs = []
    for path in _directory(data_dir).glob("*.json.gz"):
        try:
            run = load_run(data_dir, path.name.removesuffix(".json.gz"))
            if run["inputs"]["evidence"]["kind"] == kind:
                runs.append(describe_run(run, include_result=False))
        except (AttributeError, EOFError, KeyError, OSError, TypeError, ValueError):
            continue
    return sorted(runs, key=lambda run: run["saved_at"], reverse=True)


def model_availability_witnesses(data_dir):
    """Find exact artifacts already attested by an earlier frozen trial."""
    winners = {}
    for path in _directory(data_dir).glob("*.json.gz"):
        try:
            run = load_run(data_dir, path.name.removesuffix(".json.gz"))
            inputs = run["inputs"]
            if inputs["evidence"]["kind"] != "live" or _id(inputs) != run["id"]:
                continue
            saved = parse_time(run["saved_at"])
            if saved > datetime.now(timezone.utc):
                continue
            seen = {(info.get("model_version"), info.get("model_vintage"))
                    for frames in inputs["probabilities"].values() for estimates in frames.values()
                    for info in estimates.values() if isinstance(info, dict)}
            for model, vintage in seen:
                if not isinstance(model, str) or not isinstance(vintage, str):
                    continue
                if model.startswith("daily-high-"):
                    family = "study"
                elif model.startswith("daily-low-precision-"):
                    family = "daily_low_precision"
                elif model.startswith("daily-low-"):
                    family = "daily_low"
                elif model.startswith("knyc-ridge-"):
                    family = "legacy"
                else:
                    continue
                key = (family, vintage)
                if key not in winners or saved < winners[key][0]:
                    winners[key] = saved, {"run_id": run["id"], "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        except (EOFError, OSError, TypeError, ValueError, KeyError):
            continue
    return winners


def main():
    from . import server, strategy_native_capture

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA, help="Local evidence directory")
    sub = parser.add_subparsers(dest="action", required=True)
    save = sub.add_parser("save", help="Freeze a fresh trial through the latest local capture")
    save.add_argument("--fixture", action="store_true")
    save.add_argument("--order-size", type=float, default=25)
    save.add_argument("--reaction-seconds", type=int, default=5)
    save.add_argument("--freshness", choices=("legacy", "origin-confirmed"))
    save.add_argument("--clock-basis", choices=(paper_eligibility.APP_CLOCK, paper_eligibility.RECEIPT_CLOCK))
    sub.add_parser("rerun", help="Reproduce and verify a frozen paper run").add_argument("id")
    sub.add_parser("evaluate", help="Apply the new rules to a frozen v2 run and save a distinct result").add_argument("id")
    sub.add_parser("follow-payouts", help="Add later verified payout receipts").add_argument("id")
    args = parser.parse_args()
    server.DATA = args.data
    root = args.data
    if args.action == "save":
        capture = server.current_capture(args.fixture, include_low=True)
        if not capture:
            parser.error("No local capture is ready")
        if args.fixture:
            lookup = server.trial_probability_lookup(server.active_model())
        else:
            server.observe_model_artifacts()
            lookup = server.backtest_probability_lookup()
            capture = strategy_native_capture.with_listing_evidence(root, capture, capture["captured_at"])
        run = create_run(capture, lookup,
                         order_size=args.order_size, reaction_seconds=args.reaction_seconds,
                         freshness=args.freshness, clock_basis=args.clock_basis)
        run = store_run(root, run)
    else:
        for candidate in (root, root / "backtests" / "live", root / "backtests" / "fixture", root / "trade-paper"):
            if _path(candidate, args.id).is_file():
                root = candidate
                break
        run = load_run(root, args.id)
        if args.action == "follow-payouts":
            latest = server.current_capture(run["inputs"]["evidence"]["kind"] == "fixture", include_low=True)
            if not latest:
                parser.error("No local capture is ready")
            run = store_run(root, follow_payouts(run, latest))
        elif args.action == "evaluate":
            run = store_run(root, evaluate_old(run))
        else:
            verify_run(run)
    print(json.dumps(describe_run(run, include_result=False), indent=2))


if __name__ == "__main__":
    main()
