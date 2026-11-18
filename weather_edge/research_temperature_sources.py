"""Compare native daily-high expiration values with retained NWS report editions."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
import gzip
from hashlib import sha256
import json
from pathlib import Path
import statistics
import sys

from . import archive, expansion_weather, research_model, venue_archive, venue_contracts
from .nbm import climate_window, utc


VERSION = "daily-high-source-editions-v1"
SOURCES = ("The Weather Company", "NWS Daily Climate Report")


def _valid_reports(event, data):
    station, day = event["station"], date.fromisoformat(event["date"])
    if station not in expansion_weather.CLI_OFFICE:
        return []
    reports = list(event.get("official_reports", []))
    supplement = data / "expansion" / "replay" / str(day) / f"{station}.json"
    if supplement.is_file():
        extra = archive._read_event(supplement, day, station)
        reports.extend(extra.get("official_reports", []))
    editions = {}
    for report in reports:
        product, digest = report.get("product"), report.get("sha256")
        if not product or not digest or report.get("high_f") is None:
            continue
        raw = data / "expansion" / "cli" / station / f"{product}.txt"
        if not raw.is_file() or sha256(raw.read_bytes()).hexdigest() != digest:
            continue
        verified = expansion_weather.raw_report(data, station, day, product)
        if verified and verified["high_f"] == report["high_f"]:
            editions[product] = verified
    return sorted(editions.values(), key=lambda item: (utc(item["published_at"]), item["product"]))


def _expiration(value):
    try:
        native = Decimal(value)
    except (TypeError, ValueError, InvalidOperation):
        return None
    return native if native.is_finite() and -120 <= native <= 140 else None


def _win(bounds, value):
    lower, upper = bounds
    return int((lower is None or value >= lower) and (upper is None or value <= upper))


def inputs(data):
    archived = {}
    for day in archive.catalog(data)["days"]:
        for event in archive.load_day(data, day["date"])["events"]:
            if rows := _valid_reports(event, data):
                archived[event["station"], event["date"]] = rows
    candidates = defaultdict(list)
    second = defaultdict(dict)
    gaps = Counter()
    for market in venue_archive.export(data, family="daily-high", status="settled"):
        key = market["station"], market["day"]
        if key not in archived or market["units"] != "°F" or market["yes_payout"] not in {"0", "1"}:
            continue
        bounds = venue_contracts.temperature_window(market)
        if bounds is None:
            gaps["verified payout with unsupported interval"] += 1
            continue
        if market["venue"] == venue_archive.POLY:
            second[key][bounds] = market
            continue
        if market["resolution_source"] not in SOURCES:
            gaps["other resolution source"] += 1
            continue
        native = _expiration(market["expiration_value"])
        if not market["settlement_at"]:
            gaps["missing exact native settlement clock"] += 1
            continue
        if native is None:
            gaps["binary payout retained with no numeric native result"] += 1
        elif _win(bounds, native) != int(market["yes_payout"]):
            gaps["native expiration does not reproduce observed payout"] += 1
            native = None
        candidates[(key, market["resolution_source"])].append((market, bounds, native))
    results = []
    for ((station, day), source), cases in sorted(candidates.items()):
        reports = archived[station, day]
        cutoff = min(utc(market["settlement_at"]) for market, *_ in cases)
        eligible = [row for row in reports if utc(row["published_at"]) <= cutoff]
        if not eligible:
            gaps["no retained complete NWS report before native settlement"] += 1
            continue
        values = {native for _, _, native in cases if native is not None}
        if len(values) > 1:
            gaps["multiple native physical values for one station-day and source"] += 1
        latest = eligible[-1]
        first = eligible[0]
        first_value, last_value = first["high_f"], latest["high_f"]
        rule_start, rule_end = climate_window(date.fromisoformat(day), expansion_weather.STATIONS[station][3])
        exact = 0
        outcomes = []
        for market, bounds, native in cases:
            actual = int(market["yes_payout"])
            related = second[station, day].get(bounds)
            window = market["climate_starts_at"], market["climate_ends_at"]
            same_window = bool(all(window) and tuple(map(utc, window)) == (rule_start, rule_end))
            exact += same_window
            outcomes.append({"market": market, "bounds_f": bounds, "first_report_would_pay": _win(bounds, first_value),
                             "last_report_would_pay": _win(bounds, last_value), "native_yes": actual,
                             "joined_second_venue": related, "exact_reporting_window": same_window})
        results.append({"station": station, "date": day, "native_source": source,
                        "native_f": float(next(iter(values))) if len(values) == 1 else None,
                        "native_settled_by": cutoff.isoformat(), "first_report": first, "latest_report": latest,
                        "official_reports": reports, "exact_report_windows": exact, "outcomes": outcomes})
    return {"version": VERSION, "rows": results, "omitted": dict(gaps), "qualified_nws_station_days": len(archived)}


def _metrics(rows, source):
    grouped = [item for row in rows for item in row["outcomes"]]
    dual = [item for item in grouped if item["joined_second_venue"]]
    measured = [row for row in rows if row["native_f"] is not None]
    field = lambda which: sum(row["native_f"] != row[which]["high_f"] for row in measured)
    return {"source": source, "station_days": len(rows), "calendar_dates": len({row["date"] for row in rows}),
            "days_with_numeric_native_result": len(measured),
            "binary_native_payouts": len(grouped), "native_reporting_window_verified": sum(row["exact_report_windows"] for row in rows),
            "f_days_different_from_first_report": field("first_report"), "f_days_different_from_latest_report": field("latest_report"),
            "retained_revisions_to_native_high_f": sum(row["first_report"]["high_f"] != row["latest_report"]["high_f"] for row in rows),
            "payouts_different_under_first_report": sum(item["native_yes"] != item["first_report_would_pay"] for item in grouped),
            "payouts_different_under_latest_report": sum(item["native_yes"] != item["last_report_would_pay"] for item in grouped),
            "observed_both_venue_payouts": len(dual),
            "observed_both_venue_disagreements": sum(item["native_yes"] != int(item["joined_second_venue"]["yes_payout"]) for item in dual),
            "mae_f_vs_latest_report": statistics.mean(abs(row["native_f"] - row["latest_report"]["high_f"]) for row in measured) if measured else None}


def _witness(row):
    matchup = next((item for item in row["outcomes"]
                    if item["joined_second_venue"] and item["native_yes"] != int(item["joined_second_venue"]["yes_payout"])), None)
    if matchup is None:
        matchup = next((item for item in row["outcomes"] if item["native_yes"] != item["last_report_would_pay"]), row["outcomes"][0])
    market = matchup["market"]
    other = matchup["joined_second_venue"]
    native = f"{row['native_source']} value {row['native_f']:g}°F." if row["native_f"] is not None else "The native payout report omits a numeric high."
    text = (f"{native} NWS first edition {row['first_report']['high_f']}°F "
            f"and last eligible retained edition {row['latest_report']['high_f']}°F by the native settlement clock.")
    if other:
        text += f" Kalshi YES paid ${matchup['native_yes']}. Polymarket US YES paid ${other['yes_payout']}."
    return {"title": f"{row['station']} {row['date']} · Source editions at settlement", "venue": "kalshi",
            "station": row["station"], "date": row["date"], "instrument_id": market["instrument_id"],
            "related_instrument_id": other["instrument_id"] if other else None,
            "related_venue": other["venue"] if other else None, "at": market["settlement_at"],
            "detail": text, "official_report": row["latest_report"], "market": market,
            "evidence": {"editions": row["official_reports"], "native_value_f": row["native_f"], "same_case": matchup}}


def run(data, frozen=None, *, supersedes=None):
    if frozen is None:
        evidence = inputs(data)
        frozen, digest = research_model.freeze_inputs(data, evidence)
    else:
        raw = gzip.decompress(frozen.read_bytes())
        evidence, digest = json.loads(raw), sha256(raw).hexdigest()
    rows = evidence["rows"]
    regimes = {source: [row for row in rows if row["native_source"] == source] for source in SOURCES}
    metrics = [_metrics(cases, source) for source, cases in regimes.items() if cases]
    varied = [row for row in rows if (row["native_f"] is not None and row["native_f"] != row["latest_report"]["high_f"])
              or any(item["native_yes"] != item["last_report_would_pay"] for item in row["outcomes"])]
    varied.sort(key=lambda row: (sum(item["native_yes"] != item["last_report_would_pay"] for item in row["outcomes"]),
                                 abs(row["native_f"] - row["latest_report"]["high_f"]) if row["native_f"] is not None else 0), reverse=True)
    witness = varied[:7]
    miami = next((row for row in varied if (row["station"], row["date"]) == ("KMIA", "2026-08-29")), None)
    if miami and miami not in witness:
        witness.append(miami)
    return research_model.write_record(data, {
        "version": VERSION, "title": "When do native high-temperature payouts and official reports diverge?",
        **({"supersedes": supersedes} if supersedes else {}),
        "hypothesis": "Native numeric high values can differ from the last retained complete NWS edition, even within the same station and nominal date. Aggregating independent station-days distinguishes physical-value discrepancies from repeated strikes.",
        "target": "Native exchange daily highs and original NWS daily reports", "venues": ["kalshi", "polymarket-us"],
        "date_window": {"retained_station_days": [min(row["date"] for row in rows), max(row["date"] for row in rows)]},
        "metrics": metrics, "cases": [_witness(row) for row in witness], "coverage": {
            "qualified_raw_weather_station_days": evidence["qualified_nws_station_days"], "missing": evidence["omitted"]},
        "limits": [
            "The same station and date is the join key. Native resolution source and reporting window are retained on each contract. Many The Weather Company instruments have no verified reporting-window match.",
            "Original WFO report files are checked against their retained digest. Only editions published by the first native settlement can enter the first-versus-latest comparison. Other editions may exist outside the local sample.",
            "The binary calculation substitutes the NWS reported high into the same physical interval. Actual native payouts and equal-threshold payouts from both sources are counted independently.",
            "A numeric match to a particular NWS edition is compatible with several provider paths. The archive requires its own timestamped provider records to establish which edition actually reached The Weather Company.",
        ],
        "provenance": {"frozen_input": str(frozen.relative_to(data)), "input_sha256": digest,
                       "source_sha256": research_model.run_sources(sys.modules[__name__], expansion_weather, venue_contracts),
                       "reproduce": (f"python -m weather_edge.research_temperature_sources --inputs {frozen}"
                                     + (f" --supersedes {supersedes}" if supersedes else ""))},
    }, rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--inputs", type=Path)
    parser.add_argument("--supersedes")
    args = parser.parse_args()
    print(run(args.data, args.inputs, supersedes=args.supersedes))


if __name__ == "__main__":
    main()
