"""Diagnose incomplete native temperature quote vectors without imputing mids."""

import argparse
from bisect import bisect_right
from collections import Counter
from datetime import timedelta
from decimal import Decimal
import gzip
import json
from pathlib import Path
from statistics import median

from . import research_model
from .market import decimal, iso, parse_time
from .strategy_exposure import exclusive_partition
from .strategy_rules import book_status
from .strategy_runs import DEFAULT_DATA, digest


VERSION = "native-temperature-partial-quotes-v1"
MAX_LOCAL_AGE = timedelta(seconds=30)
MAX_EXCHANGE_AGES = (None, 120)
ZERO, ONE = Decimal(0), Decimal(1)


def feasible_intervals(ranges):
    minimum = sum((lower for lower, _ in ranges), ZERO)
    maximum = sum((upper for _, upper in ranges), ZERO)
    if not minimum <= ONE <= maximum:
        return None
    return [(max(lower, ONE - (maximum - upper)), min(upper, ONE - (minimum - lower))) for lower, upper in ranges]


def _latest(times, books, at, max_exchange):
    pos = bisect_right(times, at) - 1
    if pos < 0 or at - times[pos] > MAX_LOCAL_AGE:
        return None
    book = books[pos]
    if max_exchange is not None and not 0 <= (at - parse_time(book["source_at"])).total_seconds() <= max_exchange:
        return None
    return book


def _snapshot(buckets, lanes, clock, age):
    evidence = []
    for bucket, (times, books) in zip(buckets, lanes, strict=True):
        book = _latest(times, books, clock, age)
        native = book or {}
        bids, asks = native.get("bids"), native.get("offers")
        bid = max(decimal(row[0]) for row in bids) if bids else None
        ask = min(decimal(row[0]) for row in asks) if asks else None
        evidence.append({"instrument_id": bucket["id"], "bid": bid, "ask": ask, "receipt_at": native.get("at"),
                         "exchange_at": native.get("source_at"), "label": bucket["label"]})
    ranges = [(item["bid"] if item["bid"] is not None else ZERO, item["ask"] if item["ask"] is not None else ONE)
              for item in evidence]
    provided = sum(int(row["bid"] is not None) + int(row["ask"] is not None) for row in evidence)
    forecast_boxes = feasible_intervals(ranges)
    for item, interval in zip(evidence, forecast_boxes or [None] * len(evidence), strict=True):
        item["quote_consistent_interval"] = interval
    return evidence, provided, forecast_boxes


def examined(event, split, through, indexed_positions):
    blueprints, anchors = [], set()
    expiry = parse_time(event["ends_at"])
    for bucket in event["buckets"]:
        accepted = [(at, book) for book in bucket["books"]
                    if split <= (at := parse_time(book["at"])) <= through and at < expiry
                    and not book_status(book, None, at, "origin-confirmed")]
        accepted.sort(key=lambda item: item[0])
        anchors.update(at for at, _ in accepted)
        blueprints.append(([at for at, _ in accepted], [book for _, book in accepted]))
    metrics, cases = [], []
    for age in MAX_EXCHANGE_AGES:
        counts, all_widths = Counter(), []
        for at in sorted(anchors):
            proof, sides, result = _snapshot(event["buckets"], blueprints, at, age)
            counts["decision_clocks"] += 1
            counts["no_observed_quote_sides"] += sides == 0
            counts["complete_two_sided_quote_vectors"] += sides == 2 * len(proof) and result is not None
            if result is None:
                counts["incompatible_with_sum_to_one"] += 1
            elif sides:
                widths = [upper - lower for lower, upper in result]
                all_widths.extend(widths)
                counts["compatible_quote_vector"] += 1
                counts["partial_with_informative_ranges"] += sides < 2 * len(proof) and min(widths) < ONE
            for position in indexed_positions.get(iso(at), []):
                own = next(row for row in proof if row["instrument_id"] == position["bucket_id"])
                interval = own["quote_consistent_interval"]
                modeled = position["probability"]
                if position["side"] == "NO":
                    modeled = 1 - modeled
                bounds = f"{interval[0]:.1%}–{interval[1]:.1%}" if interval else "unavailable"
                cases.append({"station": event["station"], "date": event["date"], "event_id": event["id"], "at": iso(at),
                              "venue": event.get("venue", "polymarket-us"), "instrument_id": position["bucket_id"],
                              "title": f'{event["station"]} {own["label"]} · partial venue price evidence',
                              "detail": f'The weather estimate assigned {modeled:.1%} to YES. The quote-consistent YES range was {bounds}.',
                              "max_exchange_age_seconds": age, "data": {"position": position, "quote_evidence": proof}})
        metrics.append({"station": event["station"], "date": event["date"], "family": event.get("family", "daily-high"),
                        "event_id": event["id"], "max_exchange_age_seconds": age,
                        **dict(counts), "median_quote_consistent_width": median(all_widths) if all_widths else None})
    return metrics, cases


def report(data, original):
    saved = json.loads(original.read_bytes())
    provenance = saved["provenance"]
    snapshot = json.loads(gzip.decompress((data / provenance["frozen_inputs"]).read_bytes()))
    if digest(snapshot) != provenance["tape_sha256"]:
        raise ValueError("Frozen paper input checksum mismatch")
    trials = saved["records"]
    metrics, examples, indexed = [], [], {}
    for rule in ("two-sided", "joint-high-low"):
        alt = trials.get(f"forward/{rule}/origin-confirmed", {})
        for position in alt.get("positions", []):
            event = position["event_id"]
            clock = iso(position["quote_at"])
            scoped = indexed.setdefault(event, {}).setdefault(clock, {})
            scoped[position["bucket_id"], position["side"]] = position
    tape = snapshot["capture"]
    split, end = map(parse_time, (snapshot["split"], tape["captured_at"]))
    for event in tape["events"]:
        if exclusive_partition(event):
            positions = {at: list(values.values()) for at, values in indexed.get(event["id"], {}).items()}
            summary, cases = examined(event, split, end, positions)
            metrics.extend(summary)
            examples.extend(cases)
    examples.sort(key=lambda row: (-row["data"]["position"]["ev_dollars"], row["max_exchange_age_seconds"] is not None))
    record = {"version": VERSION, "title": "What can partial temperature quotes establish?",
              "hypothesis": "A partially observed native quote vector can put a narrower range on an exclusive partition without inventing missing bids or asks.",
              "target": "Same-event displayed bid and ask range arithmetic after the fixed paper split.",
              "venues": saved["venues"], "date_window": saved["date_window"], "metrics": metrics, "cases": examples[:12],
              "limits": ["Missing bids and asks contribute unconstrained sides in [0, 1]. An exclusive native payout partition adds a sum-to-one constraint.",
                         "These ranges describe arithmetic compatibility if fair probabilities lie between displayed prices. They do not estimate payout probability or executable basket prices.",
                         "The latest origin-confirmed quote for each side must be locally received within 30 seconds of the decision. Exchange-clock age is compared separately."],
              "provenance": {"engine": VERSION, "paper_snapshot": saved["id"], "frozen_inputs": provenance["frozen_inputs"],
                             "frozen_input_sha256": provenance["tape_sha256"], "max_local_age_seconds": 30, "max_exchange_ages": MAX_EXCHANGE_AGES}}
    encoded = json.loads(json.dumps({"record": record, "responses": examples}, default=float, allow_nan=False))
    return research_model.write_record(data, encoded["record"], encoded["responses"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("paper", type=Path)
    args = parser.parse_args()
    path = report(args.data, args.paper)
    record = json.loads(path.read_bytes())
    print(json.dumps({"path": str(path), "metrics": record["metrics"]}, indent=2))


if __name__ == "__main__":
    main()
