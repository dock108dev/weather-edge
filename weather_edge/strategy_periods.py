"""Reconstruct actual consecutive-heat and ranged-low market observations."""

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import re

from . import strategy_twc, venue_archive
from .market import decimal, iso, parse_time
from .strategy_runs import DEFAULT_DATA, digest, json_bytes, write_new


ENGINE = "twc-period-replay-v1"
MONTH = r"[A-Za-z]+"
DAY = r"(?:[1-9]|[12]\d|3[01])"
WEEKLY = re.compile(
    rf"If there is at least (\d+)(?: or more)? consecutive days within ({MONTH} {DAY}, \d{{4}}) "
    rf"through ({MONTH} {DAY}, \d{{4}}) on which the daily average temperature at ([A-Z]{{4}}) in .+?"
    r" is strictly greater than (-?\d+) degrees Fahrenheit, then the market resolves to Yes\.",
)
PERIOD_LOW = re.compile(
    rf"If the minimum temperature recorded at .+? \(([A-Z]{{4}})\) for ({MONTH}) ({DAY}) to ({DAY}), "
    r"(\d{4}) \(inclusive, ([A-Za-z_]+/[A-Za-z_]+) local calendar dates\), is less than (-?\d+)° Fahrenheit "
    r"according to The Weather Company, then the market resolves to Yes\.",
)


def period_days(first, last):
    if first > last:
        raise ValueError("Period ends before it starts")
    return [str(first + timedelta(days=offset)) for offset in range((last - first).days + 1)]


def named_date(value):
    try:
        return datetime.strptime(value, "%B %d, %Y").date()
    except ValueError:
        return datetime.strptime(value, "%b %d, %Y").date()


def decode(market):
    primary = market.get("rules_primary") or ""
    secondary = (market.get("rules_secondary") or "").lower()
    if match := WEEKLY.fullmatch(primary):
        required, first, last, station, threshold = match.groups()
        if (
            "arithmetic mean of the hourly temperature values reported by the weather company" not in secondary
            or "rounded to the nearest whole degree" not in secondary
            or "fewer than 18 reported hourly values" not in secondary
            or "data as published at the expiration time" not in secondary
        ):
            return None
        first, last = named_date(first), named_date(last)
        if not first <= last <= first + timedelta(days=6):
            return None
        return {"family": "weekly heat streak", "station": station, "start": first, "end": last,
                "temperature_threshold": int(threshold), "minimum_days": int(required)}
    if match := PERIOD_LOW.fullmatch(primary):
        station, month, first, last, year, label_zone, threshold = match.groups()
        if "lowest of the official daily minimum temperatures" not in secondary:
            return None
        return {"family": "period minimum", "station": station, "start": named_date(f"{month} {first}, {year}"),
                "end": named_date(f"{month} {last}, {year}"), "temperature_threshold": int(threshold), "tz": label_zone}
    return None


def classify_heat_day(row, threshold):
    """A published tenth at the whole-degree rounding boundary remains uncertain."""
    if not row:
        return None, "No provider row"
    if row["hours_reported"] is None or row["hourly_average_f"] is None:
        return None, "Missing reported hourly mean or count"
    if row["hours_reported"] < 18:
        return False, "Rule's fewer-than-18-hours day breaks the streak"
    midpoint = decimal(row["hourly_average_f"])
    cut = decimal(threshold) + Decimal("0.5")
    if midpoint == cut:
        return None, "Published precision is ambiguous at the whole-degree rounding threshold"
    return midpoint > cut, "Reported hourly mean and count"


def longest_streak(days):
    best = count = 0
    for matched in days:
        count = count + 1 if matched else 0
        best = max(best, count)
    return best


def thermal_result(terms, reports):
    station = terms["station"]
    threshold = terms["temperature_threshold"]
    days = []
    for day in period_days(terms["start"], terms["end"]):
        report = reports.get((station, day))
        if terms["family"] == "weekly heat streak":
            qualifies, reason = classify_heat_day(report, threshold)
            value = report["hourly_average_f"] if report else None
            hours = report["hours_reported"] if report else None
        else:
            complete = report and report["timezone"] == terms["tz"] and report["provider_status"] in {"official", "revised"}
            value = report["temperature_low_f"] if complete else None
            hours = None
            qualifies = value < threshold if value is not None else None
            reason = "Official daily minimum" if value is not None else "Required official daily report incomplete"
        days.append({
            "date": day, "qualifies": qualifies, "observed_f": value, "hour_count": hours, "reason": reason,
            "available_at": report["available_at"] if report else None, "source": report["source_url"] if report else None,
            "body_sha256": report["body_sha256"] if report else None, "provider_status": report["provider_status"] if report else None,
        })
    if terms["family"] == "weekly heat streak":
        lower = longest_streak([d["qualifies"] is True for d in days])
        upper = longest_streak([d["qualifies"] is not False for d in days])
        need = terms["minimum_days"]
        scope = {"minimum_possible_hot_streak": lower, "maximum_possible_hot_streak": upper}
        implied = 1 if lower >= need else 0 if upper < need else None
    else:
        complete_values = [d["observed_f"] for d in days if d["observed_f"] is not None]
        scope = {"minimum_of_complete_reported_days": min(complete_values) if complete_values else None}
        implied = int(any(d["qualifies"] for d in days)) if len(complete_values) == len(days) else None
    return days, implied, scope


def contracts(data):
    with venue_archive.connect(data) as db:
        result = db.execute("""SELECT """ + venue_archive.PUBLIC_SELECT + """
            FROM markets WHERE venue='kalshi' AND (series_id LIKE 'KXAVGT%' OR series_id='KXLOWTPERIOD')
            ORDER BY opened_at,instrument_id""")
        return [dict(row) for row in result]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", type=Path, default=DEFAULT_DATA)
    ap.add_argument("--as-of", help="Cut source and exchange receipts at this wall-clock time.")
    args = ap.parse_args()
    data = args.data
    cutoff = parse_time(args.as_of) if args.as_of else datetime.now(timezone.utc)
    reports = {(row["station"], row["label_date"]): row for row in strategy_twc.reports(data, as_of=cutoff)}
    if not reports:
        ap.error("No Weather Company receipts available at the selected time.")
    cases, gaps = [], Counter()
    for row in contracts(data):
        if parse_time(row["first_received_at"]) > cutoff:
            continue
        terms = decode(row)
        if not terms:
            gaps["Rule outside the validated streak and period-minimum patterns"] += 1
            continue
        days, implied, scope = thermal_result(terms, reports)
        if not any(day["available_at"] for day in days):
            gaps["No overlapping provider receipts"] += 1
            continue
        payout_at = row["settlement_received_at"]
        payout = row["yes_payout"] if payout_at and parse_time(payout_at) <= cutoff else None
        payout = float(decimal(payout)) if payout is not None else None
        family = terms["family"]
        cases.append({
            "station": terms["station"], "date": str(terms["end"]), "venue": venue_archive.KALSHI,
            "instrument_id": row["instrument_id"], "event_id": row["event_id"], "at": payout_at if payout is not None else iso(cutoff),
            "title": f"{terms['station']} {terms['end']} · {family}", "detail": row["rules_primary"],
            "market_status": row["status"], "payout_yes": payout, "provider_implied_yes": implied,
            "matches_payout": implied == payout if implied is not None and payout is not None else None,
            "rules": row["rules_primary"], "report_policy": row["rules_secondary"],
            "terms": {**terms, "start": str(terms["start"]), "end": str(terms["end"])},
            "settlement_source": row["settlement_source"], "weather_path": days,
            **scope,
        })
    metrics = []
    for family in ("weekly heat streak", "period minimum"):
        selected = [case for case in cases if case["terms"]["family"] == family]
        verified = [case for case in selected if case["matches_payout"] is not None]
        metrics.append({
            "family": family, "contracts_with_any_weather": len(selected),
            "distinct_event_windows": len({case["event_id"] for case in selected}),
            "independent_end_dates": len({case["date"] for case in selected}),
            "complete_or_determined": sum(case["provider_implied_yes"] is not None for case in selected),
            "verified_payout_comparisons": len(verified),
            "matched": sum(case["matches_payout"] for case in verified),
            "mismatched": sum(not case["matches_payout"] for case in verified),
            "verified_yes_payouts": sum(case["payout_yes"] == 1 for case in verified),
        })
    config = {"engine": ENGINE, "cutoff": iso(cutoff), "cases": cases}
    ident = "period-replay-" + digest(config)[:20]
    candidates = [
        next((c for c in cases if c["matches_payout"] is False), None),
        next((c for c in cases if c["payout_yes"] == 1 and c["matches_payout"]), None),
        next((c for c in cases if c["terms"]["family"] == "period minimum" and c["station"] == "KEWR"), None),
    ]
    shown = [case for case in candidates if case]
    snapshot = data / "research" / "evidence" / "period-cases" / f"{ident}.json"
    write_new(snapshot, json_bytes(config) + b"\n")
    result = {
        "id": ident, "title": "Real heat-streak and multi-day minimum rules",
        "hypothesis": "Exact provider hourly counts and daily values reproduce actual consecutive-heat payouts and track open minimum windows.",
        "target": "Consecutive hot days and multi-day minimum temperature",
        "venues": [venue_archive.KALSHI], "created_at": iso(datetime.now(timezone.utc)),
        "date_window": {"from": min((c["terms"]["start"] for c in cases), default=None),
                        "through": max((c["terms"]["end"] for c in cases), default=None)},
        "metrics": metrics, "cases": shown,
        "limits": [
            "An incomplete required daily-minimum window stays pending.",
            "When displayed hourly means are ambiguous around whole-degree rounding, the replay retains a range of possible streak lengths.",
            "Current official reports may incorporate revisions after historical contract expiration. Exchange payouts retain their own evidence.",
        ],
        "evidence": {"sources": [strategy_twc.SITE], "gaps": dict(gaps)},
        "provenance": {"engine": ENGINE, "frozen_inputs": str(snapshot.relative_to(data))},
    }
    path = data / "research" / "models" / f"{ident}.json"
    write_new(path, json_bytes(result) + b"\n")
    print(json.dumps({"path": str(path), "metrics": metrics, "gaps": gaps, "cases": [
        {k: c[k] for k in ("title", "instrument_id", "market_status", "payout_yes", "provider_implied_yes")} for c in shown
    ]}, indent=2))


if __name__ == "__main__":
    main()
