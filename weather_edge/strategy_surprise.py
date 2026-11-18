"""Measure market movement after a newly available exact-station reading."""

import argparse
from bisect import bisect_left
from collections import Counter
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import statistics

from . import collector, study
from .capture import load_captures
from .market import iso, parse_time
from .model import _hourly_at
from .paper_runs import _freeze
from .strategy_rules import OBSERVATION_MIN_DELTA, book_status, distinct_observations
from .strategy_runs import DEFAULT_DATA, POLYMARKET, RUN_SPLIT, digest, json_bytes, write_new


ENGINE = "exact-station-surprise-v1"
HORIZONS_SECONDS = (60, 300, 900)
RECEIPT_LOOKBACK_SECONDS = 3 * collector.BOOK_INTERVAL
AFTER_SLACK_SECONDS = 3 * collector.BOOK_INTERVAL
OBS_SOURCES = {"NWS", "Aviation Weather Center"}


def mids(bucket):
    points = []
    for book in bucket["books"]:
        at = parse_time(book["at"])
        offers, bids = book.get("offers"), book.get("bids")
        if not (offers and bids) or book_status(book, None, at, "origin-confirmed") is not None:
            continue
        bid, offer = float(bids[0][0]), float(offers[0][0])
        points.append((at, (bid + offer) / 2, book["source_at"]))
    return ([at for at, _, _ in points], points)


def _readings(event, first, cutoff, first_receipt):
    if first_receipt:
        return [row for row in distinct_observations(event, cutoff) if parse_time(row["available_at"]) > first]
    best = {}
    for reading in event["observations"]:
        weather_at, received = parse_time(reading["at"]), parse_time(reading["available_at"])
        if reading.get("source") not in OBS_SOURCES or not weather_at <= received <= cutoff or received <= first:
            continue
        key = (received, reading["source"] != "NWS")
        if weather_at not in best or key < best[weather_at][0]:
            best[weather_at] = key, reading
    return [item[1] for _, item in sorted(best.items())]


def attached_change(tape, artifact, split, *, first_receipt=False):
    cut = parse_time(tape["captured_at"])
    cards, excluded = [], Counter()
    exposures = {"development": set(), "forward": set()}
    for event in tape["events"]:
        if event["station"] not in artifact["stations"] or not event["buckets"]:
            continue
        times_by_bucket = {b["id"]: mids(b) for b in event["buckets"]}
        first = min((times[0] for times, _ in times_by_bucket.values() if times), default=None)
        if first is None:
            continue
        for reading in _readings(event, first, cut, first_receipt):
            observed, when = parse_time(reading["at"]), parse_time(reading["available_at"])
            phase = "development" if when < split else "forward"
            exposures[phase].add((event["station"], event["date"]))
            after = study._event_snapshot(event, when)
            earlier = [r for r in event["observations"] if parse_time(r["at"]) != observed
                       or (first_receipt and parse_time(r["available_at"]) < when)]
            before = study._event_snapshot({**event, "observations": earlier}, when)
            if not (before and after) or when < parse_time(artifact["evidence_cutoff"]):
                excluded["no same-clock NOAA/Ridge forecast"] += 1
                continue
            hour_points = sorted((parse_time(r["valid_at"]), r["temp_f"]) for r in before["forecast"].get("hourly", []))
            predicted = _hourly_at(hour_points, observed)
            if predicted is None:
                excluded["no hourly guidance at exact observation time"] += 1
                continue
            first_probabilities = study._bucket_estimates(artifact, before, "ridge", event["buckets"])
            second_probabilities = study._bucket_estimates(artifact, after, "ridge", event["buckets"])
            bucket_changes = [
                (b["probability"] - a["probability"], b["id"], a["probability"], b["probability"], b["uncertain"])
                for a, b in zip(first_probabilities["buckets"], second_probabilities["buckets"], strict=True)
            ]
            change, ident, before_p, after_p, uncertain = max(bucket_changes, key=lambda item: abs(item[0]))
            if abs(change) < OBSERVATION_MIN_DELTA:
                excluded["largest bucket forecast change below two points"] += 1
                continue
            if uncertain:
                excluded["largest bucket has thin calibration support"] += 1
                continue
            starts, prices = times_by_bucket[ident]
            start = bisect_left(starts, when) - 1
            if start < 0 or (when - starts[start]).total_seconds() > RECEIPT_LOOKBACK_SECONDS:
                excluded["no timely pre-reading book for most affected bucket"] += 1
                continue
            before_at, midpoint, source_clock = prices[start]
            horizons = []
            for delay in HORIZONS_SECONDS:
                target = when + timedelta(seconds=delay)
                index = bisect_left(starts, target)
                if index == len(starts):
                    reason = "follow-up window incomplete" if cut < target + timedelta(seconds=AFTER_SLACK_SECONDS) else "no eligible follow-up book"
                elif (starts[index] - target).total_seconds() > AFTER_SLACK_SECONDS:
                    reason = "no eligible follow-up book"
                elif phase == "development" and starts[index] >= split:
                    reason = "follow-up crosses forward boundary"
                else:
                    at, mid, exchange_clock = prices[index]
                    horizons.append({
                        "seconds": delay, "quote_at": iso(at), "exchange_at": exchange_clock,
                        "midpoint": mid, "midpoint_change": mid - midpoint,
                        "same_exchange_timestamp": source_clock == exchange_clock,
                        "moves_with_forecast": change * (mid - midpoint) > 0,
                    })
                    continue
                excluded[reason] += 1
                horizons.append({"seconds": delay, "gap": reason})
            bucket = next(b["label"] for b in event["buckets"] if b["id"] == ident)
            cards.append({
                "venue": POLYMARKET, "station": event["station"], "date": event["date"], "event_id": event["id"],
                "instrument_id": ident, "at": iso(when), "window": phase, "observation": reading,
                "title": f"{event['station']} {bucket} · newly received station reading",
                "detail": f"Station was {reading['temp_f'] - predicted:+.1f}°F versus prior hourly guidance. "
                          f"Ridge YES moved {(100 * change):+.1f} points.",
                "observation_forecast_f": predicted, "surprise_f": reading["temp_f"] - predicted,
                "forecast_issued_at": before["forecast"]["issued_at"],
                "forecast_available_at": before["forecast"]["available_at"],
                "probability_before": before_p, "probability_after": after_p, "probability_change": change,
                "pre_quote_at": iso(before_at), "pre_quote_exchange_at": source_clock, "pre_quote_midpoint": midpoint,
                "horizons": horizons,
            })
    return cards, dict(excluded), {phase: len(days) for phase, days in exposures.items()}


def outcome_rows(cases):
    result = []
    for phase in ("development", "forward"):
        phase_rows = [case for case in cases if case["window"] == phase]
        for horizon in HORIZONS_SECONDS:
            analysis = [(case, hit) for case in phase_rows for hit in case["horizons"]
                        if hit["seconds"] == horizon and "gap" not in hit]
            result.append({
                "window": phase,
                "delay_minutes": horizon // 60,
                "observations_with_pre_book": len(phase_rows),
                "paired_follow_ups": len(analysis),
                "paired_follow_up_station_days": len({(case["station"], case["date"]) for case, _ in analysis}),
                "paired_follow_up_calendar_dates": len({case["date"] for case, _ in analysis}),
                "same_direction": sum(row["moves_with_forecast"] for _, row in analysis),
                "unchanged_quote": sum(row["midpoint_change"] == 0 for _, row in analysis),
                "unchanged_exchange_timestamp": sum(row["same_exchange_timestamp"] for _, row in analysis),
                "median_signed_move_points": 100 * statistics.median(
                    row["midpoint_change"] * (1 if case["probability_change"] > 0 else -1) for case, row in analysis
                ) if analysis else None,
            })
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", type=Path, default=DEFAULT_DATA)
    ap.add_argument("--through")
    ap.add_argument("--split", default=RUN_SPLIT)
    ap.add_argument("--first-observation-receipt", action="store_true")
    args = ap.parse_args()
    captured = load_captures(args.data / "captures", through=args.through)
    artifact = study.load_selected(args.data)
    if captured is None or artifact is None:
        ap.error("Local captured Polymarket books and weather model are required.")
    tape = _freeze(collector.enrich_capture(args.data, captured), parse_time(captured["captured_at"]))
    split = parse_time(args.split)
    settings = {
        "engine": ENGINE, "model_vintage": artifact["vintage"], "evidence_sha256": sha256(json_bytes(tape)).hexdigest(),
        "split": iso(split), "fixed_horizons_seconds": HORIZONS_SECONDS,
        "pre_book_lookback_seconds": RECEIPT_LOOKBACK_SECONDS, "after_window_slack_seconds": AFTER_SLACK_SECONDS,
        "minimum_yes_delta": OBSERVATION_MIN_DELTA,
    }
    if args.first_observation_receipt:
        settings["observation_trigger"] = "Earliest original delivery with changed reports kept as separate revisions"
    cards, gaps, exposures = attached_change(tape, artifact, split, first_receipt=args.first_observation_receipt)
    ident = "surprise-" + digest(settings)[:20]
    snapshot = args.data / "research" / "evidence" / "surprises" / f"{ident}.json"
    write_new(snapshot, json_bytes({"settings": settings, "cases": cards, "gaps": gaps}) + b"\n")
    record = {
        "id": ident,
        "title": "Did books react after station temperature surprises?",
        "hypothesis": "The most affected supported Ridge bucket sees a same-direction market midpoint move after each exact-station reading is available.",
        "target": "Daily high temperature bucket",
        "venues": [POLYMARKET],
        "created_at": iso(datetime.now(timezone.utc)),
        "date_window": {"from": tape["started_at"], "split": iso(split), "through": tape["captured_at"]},
        "metrics": outcome_rows(cards),
        "cases": sorted(cards, key=lambda c: (c["window"] == "development", -abs(c["probability_change"]), c["at"]))[:8],
        "limits": [
            "Forward and development share station-days. This is a timing and candidate-frequency check.",
            "One bucket per reading is fixed by the same-clock forecast change, with exchange moves measured at fixed later windows.",
            "Market access to an observation may precede our local receipt timestamp. Price movement does not isolate the effect of the report.",
            "Midpoint movement supplies no execution or verified payout evidence.",
        ],
        "evidence": {"exclusions": gaps, "station_days_with_source_observations": exposures},
        "provenance": {**settings, "frozen_samples": str(snapshot.relative_to(args.data))},
    }
    path = args.data / "research" / "models" / f"{ident}.json"
    write_new(path, json_bytes(record) + b"\n")
    print(json.dumps({"path": str(path), "metrics": record["metrics"], "exclusions": gaps}, indent=2))


if __name__ == "__main__":
    main()
