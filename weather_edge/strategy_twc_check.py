"""Check report dates, thresholds, trace treatment, and actual weather-contract payouts."""

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import re

from . import strategy_twc, venue_archive
from .market import decimal, iso
from .strategy_rules import temperature_window
from .strategy_runs import DEFAULT_DATA, digest, json_bytes, write_new


ENGINE = "twc-daily-payout-audit-v1"
FAMILIES = ("daily-high", "daily-low", "daily-rain")
REPORT_FIELDS = (
    "receipt_id", "station", "label_date", "provider_status", "provider_issued_at",
    "city", "timezone", "source_class", "temperature_high_f", "temperature_low_f",
    "precipitation_in", "snowfall_in", "source_url", "available_at", "body_sha256",
)


def climate_indices(reports):
    by_station, by_cli = {}, defaultdict(list)
    for report in reports:
        day, station = report["label_date"], report["station"]
        by_station[station, day] = report
        cli = report["native"]["station"].get("cliId")
        if cli:
            by_cli[cli, day].append(report)
    return by_station, by_cli


def exact_report(market, by_station, by_cli):
    stated = re.findall(r"\bCLI([A-Z0-9]{3})\b", market.get("rules_primary") or "", re.I)
    if len(stated) == 1:
        candidates = by_cli.get((stated[0].upper(), market["day"]), [])
        if len(candidates) == 1:
            report, = candidates
            station = market["station"]
            if station and station != report["station"]:
                return None, "parsed station disagrees with stated CLI identifier"
            return report, None
        return None, "stated CLI identifier absent or ambiguous in provider data"
    if market["station"]:
        hit = by_station.get((market["station"], market["day"]))
        return (hit, None) if hit else (None, "stated station absent from provider snapshot")
    return None, "contract has no verified exact station"


def measured_payout(market, report):
    """Return source value and the binary threshold implied by this report."""
    if report["provider_status"] not in {"official", "revised"}:
        return None, "no official provider report"
    if market["family"] in {"daily-high", "daily-low"}:
        bounds = temperature_window(market)
        field = "temperature_high_f" if market["family"] == "daily-high" else "temperature_low_f"
        value = report[field]
        if bounds is None or value is None or value != int(value):
            return None, "unverified temperature bounds or provider integer"
        low, high = bounds
        return (value, int((low is None or value >= low) and (high is None or value <= high)), None), None
    rules = (market.get("rules_secondary") or "").lower().replace("“", '"').replace("”", '"')
    raw = report["precipitation_in"]
    missing_zero = "missing daily precipitation values are counted as 0 inches" in rules
    trace_zero = "`trace` amounts (t)" in rules or '"trace" amounts (t)' in rules
    if market["units"] != "in" or market["operator"] != "above" or market["lower"] is None:
        return None, "unsupported precipitation threshold"
    note = None
    if raw == "T" and trace_zero:
        raw, note = "0", "Reported trace treated as zero by this contract"
    elif raw is None and missing_zero:
        raw, note = "0", "Missing amount within an official report treated as zero by this contract"
    if raw is None or raw == "T":
        return None, "the contract does not establish treatment of this trace or missing amount"
    amount = decimal(raw)
    return (str(amount), int(amount > decimal(market["lower"])), note), None


def validated_rows(data, reports):
    first_day, last_day = min(r["label_date"] for r in reports), max(r["label_date"] for r in reports)
    station, cli = climate_indices(reports)
    full, gaps = [], Counter()
    for family in FAMILIES:
        for pair in venue_archive.export(data, venue=venue_archive.KALSHI, family=family, status="settled"):
            if (not pair["day"] or not first_day <= pair["day"] <= last_day
                    or pair["resolution_source"] != "The Weather Company"):
                continue
            report, reason = exact_report(pair, station, cli)
            if reason:
                gaps[family, reason] += 1
                continue
            if pair["yes_payout"] is None or decimal(pair["yes_payout"]) not in (0, 1):
                gaps[family, "missing or non-binary exchange payout"] += 1
                continue
            check, reason = measured_payout(pair, report)
            if reason:
                gaps[family, reason] += 1
                continue
            value, predicted, note = check
            observed = int(decimal(pair["yes_payout"]))
            full.append({
                "family": family, "date": pair["day"], "venue": pair["venue"], "station": report["station"],
                "title": pair["title"], "instrument_id": pair["instrument_id"], "rules_primary": pair["rules_primary"],
                "rules_secondary": pair["rules_secondary"], "at": pair["settlement_received_at"],
                "cutoffs": {key: pair[key] for key in ("opened_at", "closed_at", "settlement_at")},
                "provider_value": value, "provider_implied_yes_payout": predicted, "actual_yes_payout": observed,
                "agrees": predicted == observed, "treatment": note, "settlement_source": pair["settlement_source"],
                "weather_report": {key: report.get(key) for key in REPORT_FIELDS},
            })
    return full, gaps


def case_for(row):
    currency = "in" if row["family"] == "daily-rain" else "°F"
    label = "reported value matched payout" if row["agrees"] else "current report and actual payout differ"
    return {**row, "title": f"{row['station']} {row['date']} · {row['family'].replace('-', ' ')} {label}",
            "detail": f"The Weather Company archived value was {row['provider_value']} {currency}. "
                      f"Threshold implies YES pays {row['provider_implied_yes_payout']}. "
                      f"The exchange paid {row['actual_yes_payout']}. "
                      + (row["treatment"] or "")}


def summarize(rows, *, group):
    mismatches = [row for row in rows if not row["agrees"]]
    return {
        "market_family": group, "contracts": len(rows), "calendar_dates": len({row["date"] for row in rows}),
        "station_days": len({(row["station"], row["date"]) for row in rows}),
        "stations": len({row["station"] for row in rows}), "agreeing_payouts": len(rows) - len(mismatches),
        "disagreeing_payouts": len(mismatches),
        "disagreeing_station_days": len({(row["station"], row["date"]) for row in mismatches}),
        "trace_or_missing_treatment": sum(row["treatment"] is not None for row in rows),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", type=Path, default=DEFAULT_DATA)
    ap.add_argument("--start")
    ap.add_argument("--end")
    args = ap.parse_args()
    data = args.data
    reports = strategy_twc.reports(data, start=args.start, end=args.end)
    if not reports:
        ap.error("No Weather Company archive. Run strategy_twc first.")
    rows, gaps = validated_rows(data, reports)
    rows.sort(key=lambda row: (not row["agrees"], row["treatment"] is not None, row["date"], row["instrument_id"]))
    metrics = [summarize([row for row in rows if row["family"] == family], group=family) for family in FAMILIES]
    source_keys = sorted({(row["label_date"], row["available_at"], row["body_sha256"]) for row in reports})
    details = {
        "engine": ENGINE, "reports": source_keys, "matches": rows,
        "gaps": [{"family": family, "reason": reason, "markets": value} for (family, reason), value in sorted(gaps.items())],
        "payout_clock": "Exchange's actual final payout and local archive receipt",
    }
    ident = "twc-payout-" + digest(details)[:20]
    snapshot = data / "research" / "evidence" / "twc" / f"{ident}.json"
    write_new(snapshot, json_bytes(details) + b"\n")
    cases = [case_for(row) for row in rows if not row["agrees"]][:6]
    for family in FAMILIES:
        positives = [row for row in rows if row["family"] == family and row["agrees"] and row["actual_yes_payout"] == 1]
        if positives:
            cases.append(case_for(positives[-1]))
    traces = [row for row in rows if row["family"] == "daily-rain" and row["treatment"] and row["agrees"]]
    if traces:
        cases.append(case_for(traces[-1]))
    report = {
        "id": ident, "title": "Weather Company reports versus actual Kalshi payouts",
        "hypothesis": "The provider's exact station-day readings reproduce payouts when the contract's own threshold and trace treatment are applied.",
        "target": "Daily high, daily low, and precipitation",
        "venues": [venue_archive.KALSHI], "created_at": iso(datetime.now(timezone.utc)),
        "date_window": {"from": min(row["label_date"] for row in reports),
                        "through": max(row["label_date"] for row in reports)},
        "metrics": metrics, "cases": cases,
        "limits": [
            "The provider download can occur after exchange settlement and may reflect later revisions.",
            "An unknown original issue time remains unknown. Replays use the actual received time for newly shown source evidence.",
            "Each contract uses exact station, date, threshold and settlement treatment. A missing whole report is an evidence gap.",
            "Observed payout agreements do not establish equivalent venue contracts.",
        ],
        "evidence": {"basis": strategy_twc.SITE, "station_rows": len(reports),
                     "stations": len({row["station"] for row in reports}),
                     "provider_statuses": dict(Counter(row["provider_status"] for row in reports)),
                     "provider_issue_clocks": sum(bool(row["provider_issued_at"]) for row in reports),
                     "gaps": details["gaps"]},
        "provenance": {"engine": ENGINE, "frozen_inputs": str(snapshot.relative_to(data)), "catalog": "venues.sqlite3",
                       "provider_receipts": strategy_twc.FILENAME},
    }
    path = data / "research" / "models" / f"{ident}.json"
    write_new(path, json_bytes(report) + b"\n")
    print(json.dumps({"path": str(path), "metrics": metrics, "evidence": report["evidence"], "cases": [
        {key: case[key] for key in ("title", "detail", "date", "station", "instrument_id")} for case in cases
    ]}, indent=2))


if __name__ == "__main__":
    main()
