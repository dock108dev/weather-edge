"""Compare the Weather Index filing clock with original local collection receipts."""

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import gzip
import json
from pathlib import Path
from statistics import mean, median
import sys

from . import research_model, venue_archive, venue_index, venue_index_model
from .market import decimal, iso, parse_time

VERSION = "weather-index-original-receipts-v2"
EARLIER_RUN = "weather-index-original-receipts-v1-f3cc87d3c93e23b1"
START = "2026-11-15T18:40:00Z"
METHODS = {"rule_clock": "Filed clock and historical forecast", "actual_local": "Original local receipt and same forecast"}


def gather(data, first, cutoff):
    with venue_archive.connect(data) as db:
        may_use = db.execute(
            """SELECT w.city,w.at,w.status,w.temperature_f,w.contributors,w.receipt_id,
               r.requested_at,r.received_at,r.source,r.headers
            FROM weather_indices AS w JOIN receipts AS r ON w.receipt_id=r.id
            WHERE w.at>=? AND w.at<? AND r.received_at<=? AND r.requested_at>=?
            ORDER BY w.city,w.at,r.received_at,w.receipt_id""", (iso(first), iso(cutoff), iso(cutoff), iso(first)),
        ).fetchall()
    versions = defaultdict(list)
    for row in may_use:
        if row["city"] in venue_index.ACTIVE_CITIES:
            versions[(row["city"], row["at"])].append({**dict(row), "headers": json.loads(row["headers"])})
    keep = []
    details = Counter()
    for (city, minute), records in versions.items():
        target = parse_time(minute)
        deadline = target + venue_index_model.PUBLICATION_DEADLINE
        if deadline >= cutoff:
            continue
        initials = [row for row in records if row["status"] in venue_index.CANONICAL and row["temperature_f"] is not None]
        first_canonical = initials[0] if initials else None
        if first_canonical is None:
            details[city + " minutes with no numeric at cutoff"] += 1
        first_available = parse_time(first_canonical["received_at"]) if first_canonical else None
        keep.append({"city": city, "event_minute": minute, "filing_deadline": iso(deadline), "first": first_canonical,
                     "delay_seconds": (first_available - deadline).total_seconds() if first_available else None,
                     "all_versions": records, "changed_numeric_value": len({decimal(row["temperature_f"]) for row in initials}) > 1,
                     "first_version_state": records[0]["status"], "final_version_state": records[-1]["status"]})
    day = first.date().isoformat()
    bounds = {"start": day, "end": cutoff.date().isoformat(), "cutoff": cutoff, "original_receipts_from": first}
    strict = venue_index_model.gather(data, strict_local=True, **bounds)
    rule = venue_index_model.gather(data, strict_local=False, **bounds)
    first_iso = iso(first)
    strict["rows"] = [row for row in strict["rows"] if row["at"] >= first_iso]
    rule["rows"] = [row for row in rule["rows"] if row["at"] >= first_iso]
    return {"first": first_iso, "cutoff": iso(cutoff), "clock_rows": keep, "clock_missing": dict(details),
            "strict": strict, "deadline": rule}


def _describe(clock_rows):
    output = {}
    for city in venue_index.ACTIVE_CITIES:
        rows = [row for row in clock_rows if row["city"] == city]
        point = [row["delay_seconds"] for row in rows if row["delay_seconds"] is not None]
        request = [parse_time(row["first"]["received_at"]) - parse_time(row["first"]["requested_at"]) for row in rows if row["first"]]
        output[city] = {"observed_event_minutes": len(rows), "first_numeric_receipts": len(point),
                        "numeric_value_revisions": sum(row["changed_numeric_value"] for row in rows),
                        "noncanonical_then_numeric": sum(row["first_version_state"] not in venue_index.CANONICAL and bool(row["first"]) for row in rows),
                        "numeric_before_filed_deadline": sum(delta < 0 for delta in point),
                        "median_seconds_after_deadline": median(point) if point else None,
                        "max_seconds_after_deadline": max(point) if point else None,
                        "median_request_to_receipt_ms": round(median(stamp.total_seconds() for stamp in request) * 1000, 1) if request else None}
    return output


def _forecast_support(historical, received):
    phase = venue_index_model._partition(historical["rows"])
    models = venue_index_model._fit(phase["fit"])
    calibration = venue_index_model._calibrate(venue_index_model._predictions(phase["calibration"], models))
    local = venue_index_model._predictions(received["strict"]["rows"], models)
    deadline = {row["event_id"]: row for row in venue_index_model._predictions(received["deadline"]["rows"], models)}
    trials = []
    for actual in local:
        reference = deadline.get(actual["event_id"])
        if not reference:
            continue
        live_at = parse_time(actual["at"])
        at_loc, at_rule = parse_time(actual["latest"]["at"]), parse_time(reference["latest"]["at"])
        this_event = {"city": actual["city"], "date": actual["date"], "event_id": actual["event_id"], "at": actual["at"],
                      "actual_f": actual["actual_f"], "target_at": actual["target_at"],
                      "local": actual["latest"], "deadline": reference["latest"],
                      "filed_point_arrived_after_decision": parse_time(reference["latest"]["received_at"]) > live_at,
                      "local_index_older_by_sec": (at_rule - at_loc).total_seconds()}
        for market in actual["prices"]:
            threshold = market["threshold_f"]
            trials.append({**this_event, "contract": market, "outcome": market["native_payout_yes"],
                           "actual_local": venue_index_model._prob(actual, "nonlinear", threshold, calibration),
                           "rule_clock": venue_index_model._prob(reference, "nonlinear", threshold, calibration)})
    return trials


def evaluate(data, historical, received, *, supersedes=EARLIER_RUN):
    corpus = {"version": VERSION, "historical": historical, "received": received, "supersedes": supersedes}
    frozen, digest = research_model.freeze_inputs(data, corpus)
    trials = _forecast_support(historical, received)
    events = {row["event_id"]: row for row in trials}
    counts = {"bucket_cases": len(trials), "hourly_events": len(events),
              "changed_input_hours": sum(row["local_index_older_by_sec"] != 0 for row in events.values()),
              "filed_proxy_used_point_received_later": sum(row["filed_point_arrived_after_decision"] for row in events.values())}
    metrics = []
    if trials:
        for method, label in METHODS.items():
            groups = defaultdict(list)
            for row in trials:
                groups[row["event_id"]].append((row[method] - row["outcome"]) ** 2)
            metrics.append({"group": "Same exact live hourly payouts", "method": method, "label": label,
                            **counts, "bucket_brier": mean(mean(errors) for errors in groups.values())})
    cases = []
    selected = sorted(trials, key=lambda row: (abs(row["actual_local"] - row["rule_clock"]), row["at"]), reverse=True)
    seen = set()
    for row in selected:
        if row["city"] in seen:
            continue
        market = row["contract"]
        cases.append({"venue": venue_archive.KALSHI, "instrument_id": market["instrument_id"], "date": row["date"], "at": row["at"],
                      "title": market["title"], "data": row,
                      "detail": f"Last known index {row['local']['temperature_f']}°F at {row['local']['at']}. "
                                f"Local replay {row['actual_local']:.0%}, filed-clock replay {row['rule_clock']:.0%}. Native YES paid ${row['outcome']}."})
        seen.add(row["city"])
    result = {"version": VERSION, "title": "Original receipts show when the hourly index was usable",
              "hypothesis": "The documented deadline alone may give the replay a reading the local collector had not yet received.",
              "target": "First retained numeric Weather Index receipt and exact later native hourly payouts", "venues": ["Kalshi"],
              "date_window": {"original receipts": [received["first"], received["cutoff"]]}, "supersedes": supersedes,
              "coverage": {"receipt_timing": _describe(received["clock_rows"]), "paired_forecast": counts}, "metrics": metrics,
              "cases": cases, "missing": {**received["clock_missing"], **received["strict"]["missing"]},
              "limits": ["First retained receipt reflects polling and transport. The provider may publish earlier than first local receipt.",
                         "Exposure to live hours changes during the session. Forecast comparison uses the same events and immutable previously fitted models.",
                         "Public Kalshi book payloads omit the source image timestamp. This weather receipt clock does not make a book executable."],
              "provenance": {"frozen_input": str(frozen.relative_to(data)), "frozen_input_sha256": digest,
                             "filing": venue_index_model.EXCHANGE_METHOD, "source_sha256": research_model.run_sources(sys.modules[__name__], venue_index_model)}}
    return research_model.write_record(data, result, {"first_original_receipts": received["clock_rows"], "prediction_trials": trials})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--fit-input", type=Path)
    parser.add_argument("--case-input", type=Path)
    parser.add_argument("--from", dest="start", default=START)
    parser.add_argument("--as-of")
    parser.add_argument("--supersedes")
    args = parser.parse_args(argv)
    if args.case_input:
        corpus = json.loads(gzip.decompress(args.case_input.read_bytes()))
        historical, received = corpus["historical"], corpus["received"]
        supersedes = corpus.get("supersedes", EARLIER_RUN)
    elif args.fit_input:
        historical = json.loads(gzip.decompress(args.fit_input.read_bytes()))
        cutoff = parse_time(args.as_of) if args.as_of else datetime.now(timezone.utc)
        received = gather(args.data, parse_time(args.start), cutoff)
        supersedes = args.supersedes or EARLIER_RUN
    else:
        parser.error("Supply --fit-input or an earlier --case-input")
    print(evaluate(args.data, historical, received, supersedes=supersedes))


if __name__ == "__main__":
    main()
