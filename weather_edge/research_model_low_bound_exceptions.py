"""Check exact raw low-temperature bounds beyond the scheduled decision clocks."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import gzip
import json
from pathlib import Path
import sys

from . import expansion_live_low, expansion_live_precision, expansion_metar, expansion_weather, research_model
from . import research_model_low_editions as editions
from .nbm import climate_window, utc

VERSION = "daily-low-full-receipt-bound-audit-v1"


def _sources(data, station, day):
    paths = (data / "archive" / day / f"{station}.json", data / "expansion" / "replay" / day / f"{station}.json")
    receipts, evidence = {}, []
    for path in paths:
        if not path.is_file():
            continue
        try:
            event = json.loads(path.read_bytes())
        except (ValueError, OSError):
            continue
        if (event.get("station"), event.get("date")) != (station, day):
            continue
        for row in event.get("observations", []):
            raw = row.get("raw") or row.get("raw_report") or row.get("report")
            if row.get("receipt_source") and raw and not row.get("timing_proxy"):
                try:
                    key, known = (row["at"], raw), utc(row["available_at"])
                    if key not in receipts or known < utc(receipts[key]["available_at"]):
                        receipts[key] = row
                except (TypeError, ValueError, KeyError, AttributeError):
                    continue
        evidence.append(research_model._source_entry(data, path))
    return list(receipts.values()), evidence


def _compatible(entries):
    return max(row["lower_f"] for row in entries) <= min(row["upper_f"] for row in entries)


def _bound(rows):
    return min(rows, key=lambda row: (row["upper_f"], row["temperature_precision_c"], row["available_at"]), default=None)


def _witnesses(readings, station, start, end):
    groups, known = defaultdict(list), 0
    for row in readings:
        try:
            measurement, receipt = (utc(row[key]) for key in ("at", "available_at"))
            if not start <= measurement <= receipt < end:
                continue
            decoded = expansion_metar.metar_low_evidence(row, station, start, receipt)
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
        if not decoded:
            continue
        known += 1
        for witness in decoded:
            groups[witness["report_at"], witness["kind"]].append({**witness, "receipt_source": row["receipt_source"]})
    first, terminal, conflicts = [], [], []
    for bucket in groups.values():
        first_clock = min(row["available_at"] for row in bucket)
        contemporaneous = [row for row in bucket if row["available_at"] == first_clock]
        if _compatible(contemporaneous):
            first.append(_bound(contemporaneous))
        if _compatible(bucket):
            terminal.append(_bound(bucket))
        else:
            conflicts.append(sorted(bucket, key=lambda row: row["available_at"]))
    return {"known_reports": known, "raw_groups": len(groups), "first_receipt_bound": _bound(first),
            "terminal_compatible_bound": _bound(terminal), "same_station_time_conflicts": conflicts}


def gather(data):
    weather, no_reports = editions._weather(data)
    verification, missing = {}, Counter()
    for (station, day), forecast in weather.items():
        actual, refs = _sources(data, station, day)
        if not refs:
            missing["raw station report without local replay weather source"] += 1
            continue
        start, end = climate_window(day, expansion_weather.STATIONS[station][3])
        values = _witnesses(actual, station, start, end)
        if not values["first_receipt_bound"]:
            missing["no decoded originally timed in-day report"] += 1
            continue
        verification[station, day] = {**values, "station": station, "date": day, "official_first": forecast["first_complete"],
                                      "official_last": forecast["last_complete"], "observed_source": refs}
    native, gaps = editions._native(data, {key: weather[key] for key in verification})
    return {"version": VERSION, "weather": list(verification.values()), "native": native, "missing": {"weather": dict(missing),
                                                                                                    "cli": dict(no_reports), "native": dict(gaps)}}


def _replays(rows, field):
    events = [row for row in rows if row[field] is not None]
    before = [row for row in events if row["official_first"]["low_f"] > row[field]["upper_f"]]
    after = [row for row in events if row["official_last"]["low_f"] > row[field]["upper_f"]]
    days = len({row["date"] for row in events})
    failed = len({row["date"] for row in after})
    return {"group": "Exact originally received station temperature bounds", "method": field,
            **research_model._independent_counts(events), "raw_reports": sum(row["known_reports"] for row in events),
            "dates_with_later_official_low_above_bound": failed, "station_days_with_first_complete_low_above_bound": len(before),
            "station_days_with_last_complete_low_above_bound": len(after),
            "date_level_exception_rate_upper_one_sided_95": expansion_live_low._wilson(failed, days)[1]}


def _payout_metrics(rows, weather, field):
    outcomes = defaultdict(list)
    for row in rows:
        station = weather[row["station"], row["date"]]
        if (earlier := station[field]) is None:
            continue
        mark = utc(earlier["available_at"])
        market = row["market"]
        if not market["opened_at"] or not market["closed_at"] or not utc(market["opened_at"]) <= mark < utc(market["closed_at"]):
            continue
        lower, upper = row["bounds_f"]
        bucket = {"lower_f": lower, "upper_f": upper}
        side = expansion_live_precision.metar_implied_side(bucket, earlier["upper_f"])
        if side:
            outcomes[row["venue"]].append({**row, "implied_side": side, "failed": row["native_yes"] != int(side == "YES"), "witness": earlier})
    metrics = [{"group": "Native threshold resolved after a bound-implied side", "method": field, "venue": venue,
                **research_model._independent_counts(native), "verified_threshold_cases": len(native),
                "wrong_bound_implied_sides": sum(row["failed"] for row in native),
                "wrong_station_days": len({(row["station"], row["date"]) for row in native if row["failed"]})}
               for venue, native in outcomes.items()]
    return metrics, [row for entries in outcomes.values() for row in entries]


def _case(row, witness, native=None):
    low = row["official_last"]["low_f"]
    market = native["market"] if native else None
    opinion = f" The side implied by the bound was {native['implied_side']}, while native YES paid ${native['native_yes']}." if native else ""
    return {"station": row["station"], "date": row["date"], "at": witness["available_at"],
            "venue": market["venue"] if market else None, "instrument_id": market["instrument_id"] if market else None,
            "title": f"{row['station']} · An original station bound and the complete low",
            "detail": (f"The original {witness['kind']} group {witness['temperature_code']} implied an upper reading of "
                       f"{witness['upper_f']}°F. The complete climate report showed {low}°F.{opinion}"),
            "exact_station_evidence": witness, "complete_report": row["official_last"], "native_comparison": native}


def evaluate(data, inputs):
    if inputs["version"] != VERSION:
        raise ValueError("Unexpected all-receipt station low audit inputs")
    frozen, digest = research_model.freeze_inputs(data, inputs)
    records = inputs["weather"]
    keyed = {(row["station"], row["date"]): row for row in records}
    metrics, results, examples, seen = [], {}, [], set()
    for field in ("first_receipt_bound", "terminal_compatible_bound"):
        metrics.append(_replays(records, field))
        scored, payout_evidence = _payout_metrics(inputs["native"], keyed, field)
        metrics.extend(scored)
        results[field] = payout_evidence
        exceptions = {(row["station"], row["date"]): row for row in payout_evidence if row["failed"]}
        for row in records:
            pair = row["station"], row["date"]
            witness = row[field]
            if (pair not in seen and witness is not None and
                    (row["official_last"]["low_f"] > witness["upper_f"] or pair in exceptions)):
                examples.append(_case(row, witness, exceptions.get(pair)))
                seen.add(pair)
    if not examples:
        terminal = results["terminal_compatible_bound"]
        for place in ("KNYC", "KMDW"):
            candidate = max((row for row in terminal if row["station"] == place and row["witness"]["kind"] == "six-hour low"),
                            key=lambda row: row["witness"]["available_at"], default=None)
            if candidate:
                examples.append(_case(keyed[place, candidate["date"]], candidate["witness"], candidate))
    dates = [row["date"] for row in records]
    if not dates:
        raise ValueError("No original station reports and complete low targets")
    record = {"version": VERSION, "title": "Do originally timed METAR low bounds ever contradict the paid weather?",
              "hypothesis": "All original within-day readings can reveal rare official lows above a measured upper bound, especially after the last scheduled forecast update or around report corrections.",
              "target": "All retained date-correct originally received point and six-hour minimum reports, separately compared with raw CLI and native payout evidence",
              "venues": sorted({row["venue"] for row in inputs["native"]}), "date_window": {"observed_original_clock_days": [min(dates), max(dates)]},
              "coverage": {**research_model._independent_counts(records), "stations": len({row["station"] for row in records}),
                           "raw_report_groups": sum(row["raw_groups"] for row in records),
                           "station_days_with_a_conflicting_same_time_report": sum(bool(row["same_station_time_conflicts"]) for row in records)},
              "metrics": metrics, "cases": examples[:10], "limits": ["Only exact station reports with a retained original receipt no later than climate-day close are eligible. A six-hour group also needs its full clock interval inside the climate day and already elapsed at its receipt.",
                                                                     "The first-receipt arm uses the earliest compatible evidence of each observed-time group. The terminal arm omits groups whose retained original measurements conflict by close.",
                                                                     "Station day payout comparisons use actual verified native binaries and require the witness while the native contract is open. The conditional side follows the reported upper temperature and can fail if the governed official reading is higher.",
                                                                     "The confidence upper value uses dates with any recorded exception, rather than treating all strikes or raw readings as independent."],
              "missing": inputs["missing"], "provenance": {"frozen_input": str(frozen.relative_to(data)), "frozen_input_sha256": digest,
                   "source_sha256": research_model.run_sources(sys.modules[__name__], editions, expansion_metar, expansion_live_precision, expansion_weather)}}
    return research_model.write_record(data, record, {"all_stations": records, "native_comparison": results})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--input", type=Path)
    args = parser.parse_args()
    inputs = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else gather(args.data)
    print(evaluate(args.data, inputs))


if __name__ == "__main__":
    main()
