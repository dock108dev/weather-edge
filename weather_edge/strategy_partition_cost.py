"""Check whether a full native temperature partition clears displayed fees."""

import argparse
from bisect import bisect_left, bisect_right
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import gzip
import json
from pathlib import Path

from .history import _check_bucket_partition
from .market import _ladder, fee_coefficient, iso, parse_time, taker_fee
from .strategy_rules import FRESHNESS, book_status
from .strategy_runs import DEFAULT_DATA, digest, json_bytes, slice_tape, write_new


ENGINE = "temperature-partition-cost-v1"
MAX_LOCAL_AGE = timedelta(seconds=30)
MAX_LADDER_SKEW = timedelta(seconds=15)
REACTION = timedelta(seconds=30)
RECHECK = timedelta(seconds=15)
SIZE = (1, 10)
MINIMUM_DOLLARS = Decimal("0.25")


def leg(book, side, size, at):
    levels = []
    open_size = Decimal(size)
    for price, quantity in _ladder(book, side):
        take = min(open_size, quantity)
        levels.append((price, take))
        open_size -= take
        if open_size == 0:
            break
    if open_size > 0:
        return None
    notional = sum(price * amount for price, amount in levels)
    fee = taker_fee(levels, fee_coefficient(at))
    return {"quote_at": book["at"], "exchange_at": book["source_at"], "notional": str(notional), "fee": str(fee),
            "cost": str(notional + fee), "quantity": size, "levels": [[str(p), str(q)] for p, q in levels]}


def bundle(event, books, side, size, at):
    terms = {bucket["id"]: leg(book, side, size, at) for bucket, book in zip(event["buckets"], books, strict=True)}
    if any(value is None for value in terms.values()):
        return None
    payoff = Decimal(size if side == "YES" else size * (len(books) - 1))
    cash = sum(Decimal(row["cost"]) for row in terms.values())
    return {"side": side, "quantity_per_leg": size, "total_cost": str(cash), "ordinary_partition_payout": str(payoff),
            "after_fee_difference": str(payoff - cash), "legs": terms}


def vectors(event, policy):
    bases = []
    checked = set()
    for bucket in event["buckets"]:
        books = sorted(((parse_time(row["at"]), row) for row in bucket["books"]), key=lambda item: item[0])
        times = [at for at, _ in books]
        bases.append((times, [row for _, row in books]))
        checked.update(times)
    trade_closes = parse_time(event["ends_at"])
    for at in sorted(checked):
        if at >= trade_closes:
            continue
        latest = []
        for times, books in bases:
            index = bisect_right(times, at) - 1
            if index < 0 or at - times[index] > MAX_LOCAL_AGE or book_status(books[index], None, times[index], policy):
                break
            latest.append((times[index], books[index]))
        if len(latest) != len(bases):
            yield at, None, "missing or unsuitable latest constituent ladder"
            continue
        if max(t for t, _ in latest) - min(t for t, _ in latest) > MAX_LADDER_SKEW:
            yield at, None, "constituent receipt clocks exceed 15-second span"
            continue
        yield at, [row for _, row in latest], None


def evaluate(capture, window, policy):
    counts = Counter()
    candidates, rankings = [], {}
    station_days = set()
    cutoff = parse_time(capture["captured_at"])
    for event in capture["events"]:
        try:
            _check_bucket_partition(event["buckets"])
        except ValueError:
            counts["nonexhaustive native partition"] += 1
            continue
        if any("nws" not in (b.get("rules") or "").lower() and "national weather service" not in (b.get("rules") or "").lower()
               for b in event["buckets"]):
            counts["exact report rule not established"] += 1
            continue
        coherent, opportunities = [], []
        for at, point, issue in vectors(event, policy):
            counts["scanned_times"] += 1
            if issue:
                counts[issue] += 1
                continue
            counts["coherent_vectors"] += 1
            coherent.append((at, point))
            station_days.add((event["station"], event["date"]))
            for side in ("YES", "NO"):
                for size in SIZE:
                    if not (cost := bundle(event, point, side, size, at)):
                        counts["missing depth for fixed constituent size"] += 1
                        continue
                    counts["priced fixed-size bundles"] += 1
                    advantage = Decimal(cost["after_fee_difference"])
                    witness = {**cost, "event_id": event["id"], "station": event["station"], "at": iso(at),
                               "family": event.get("family", "daily-high"), "date": event["date"],
                               "venue": "polymarket-us", "instrument_id": event["buckets"][0]["id"]}
                    key = side, size
                    if key not in rankings or advantage > Decimal(rankings[key]["after_fee_difference"]):
                        rankings[key] = witness
                    if Decimal(0) < advantage:
                        counts["positive displayed bundle"] += 1
                    if advantage >= MINIMUM_DOLLARS and Decimal(cost["total_cost"]) <= 25:
                        opportunities.append(witness)
        times = [t for t, _ in coherent]
        counts["qualifying bundle signals"] += len(opportunities)
        for witness in opportunities:
            target = parse_time(witness["at"]) + REACTION
            upper = min(target + RECHECK, parse_time(event["ends_at"]))
            index = bisect_left(times, target)
            if index >= len(coherent) or times[index] > upper:
                outcome = "Missing completed recheck" if upper <= cutoff else "Awaiting full recheck window"
                later = None
            else:
                then, book = coherent[index]
                later = bundle(event, book, witness["side"], witness["quantity_per_leg"], then)
                outcome = ("Recheck lost constituent size" if later is None else
                           "Recheck costs removed theoretical edge" if Decimal(later["after_fee_difference"]) < MINIMUM_DOLLARS
                           or Decimal(later["total_cost"]) > 25 else "Theoretical edge remained")
            candidates.append({**witness, "recheck": later, "recheck_outcome": outcome})
            counts[outcome] += 1
    metrics = {"window": window, "freshness": policy, "coherent_station_days": len(station_days), **dict(counts),
               "paper_fills": 0}
    return metrics, candidates, list(rankings.values())


def report(data, evidence):
    frozen = json.loads(gzip.decompress(evidence.read_bytes()))
    original = digest(frozen)
    if original != evidence.name.removesuffix(".json.gz"):
        raise ValueError("Frozen paper evidence checksum mismatch")
    tape = frozen["capture"]
    split = parse_time(frozen["split"])
    windows = {"development": slice_tape(tape, parse_time(tape["started_at"]), split - timedelta(microseconds=1)),
               "forward": slice_tape(tape, split, parse_time(tape["captured_at"]))}
    metrics, cases, details = [], [], {}
    for scope, sample in windows.items():
        for policy in FRESHNESS:
            row, signals, closest = evaluate(sample, scope, policy)
            metrics.append(row)
            details[f"{scope}/{policy}"] = {"metrics": row, "candidates": signals, "best_prices": closest}
            for point in (*signals[:2], *closest[:2]):
                title = f"{point['station']} all-{point['side']} basket"
                detail = f"{point['quantity_per_leg']} per bucket. Displayed after-fee cost ${point['total_cost']}. Conditional payoff ${point['ordinary_partition_payout']}."
                cases.append({**point, "title": title, "detail": detail, "window": scope, "freshness": policy})
    settings = {"engine": ENGINE, "input_hash": original, "maximum_quote_age_seconds": 30, "maximum_clock_span_seconds": 15,
                "quantity_per_constituent": SIZE, "minimum_conditional_edge_dollars": 0.25, "reaction_seconds": 30, "gap_seconds": 15}
    ident = "partition-cost-" + digest(settings)[:20]
    detail_path = data / "research" / "evidence" / "partition-cost" / f"{ident}.json.gz"
    write_new(detail_path, gzip.compress(json_bytes(details), mtime=0))
    result = {"id": ident, "title": "Does a complete temperature basket clear after fees?",
              "hypothesis": "Same-event exhaustive ladders occasionally sum to a conditional payout above their all-in displayed cost.",
              "target": "Exact Polymarket US temperature partitions, origin evidence and delayed receipt comparisons.",
              "created_at": iso(datetime.now(timezone.utc)), "venues": ["polymarket-us"],
              "date_window": {"from": tape["started_at"], "through": tape["captured_at"], "split": frozen["split"]},
              "metrics": metrics, "cases": cases,
              "limits": ["Ordinary single-winner settlement sets bundle payout. All individual positions must satisfy exact venue terms.",
                         "A complete vector includes the latest receipt for each constituent. Receipt simultaneity and saved ladders do not establish atomic execution.",
                         "The test prices quantity through displayed levels and recorded fees and books no paper fills."],
              "provenance": {**settings, "frozen_evidence": str(detail_path.relative_to(data))}}
    path = data / "research" / "strategies" / f"{ident}.json"
    write_new(path, json_bytes(result) + b"\n")
    return path, metrics


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("evidence", type=Path)
    args = parser.parse_args()
    path, metrics = report(args.data, args.evidence)
    print(json.dumps({"path": str(path), "metrics": metrics}, indent=2))


if __name__ == "__main__":
    main()
