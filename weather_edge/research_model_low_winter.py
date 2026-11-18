"""Validate the exact-station low guard during a distinct winter weather period."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal, InvalidOperation
import gzip
import json
from pathlib import Path
import sys

from . import expansion_weather, research_model, research_model_high_warm, research_model_low_cross_season as shared, research_model_venue, venue_archive
from . import research_model_low_portability as portable, research_model_low_precision as precision
from .model import _outcome
from .nbm import utc

VERSION = "daily-low-winter-native-guard-v1"
DATES = (date(2026, 2, 8), date(2026, 2, 21))
STATIONS = ("KATL", "KAUS", "KDCA", "KDFW", "KLAS", "KMSY", "KSEA")


def _austin(data, weather):
    groups = defaultdict(list)
    for row in weather:
        if row["station"] == "KAUS":
            groups[row["date"]].append(row)
    with venue_archive.connect(data) as db:
        rows = [dict(row) for row in db.execute("""SELECT instrument_id,event_id,series_id,day,station,operator,lower,upper,
                                                        rules_primary,rules_secondary,rules_url,expiration_value,yes_payout,
                                                        opened_at,closed_at,settlement_at,settlement_receipt_id,source
                                                 FROM markets WHERE venue='kalshi' AND station='KAUS' AND family='daily-low'
                                                 AND resolution_source='NWS Daily Climate Report' AND day BETWEEN ? AND ?
                                                 AND status='settled' AND yes_payout IN ('0','1') ORDER BY day,instrument_id""",
                                              tuple(map(str, DATES)))]
    matches, rejected, audit, cache = defaultdict(list), Counter(), [], {}
    for market in rows:
        day = market["day"]
        rules = market["rules_secondary"] or ""
        weather_day = groups.get(day)
        if not weather_day:
            rejected["no ground observation and remaining guidance"] += 1
            continue
        if ("minimum temperature recorded at Austin" not in market["rules_primary"] or "CLIAUS" not in rules
                or "Austin Bergstrom" not in rules or market["settlement_receipt_id"] is None):
            rejected["source, station or resolved primary target not explicit"] += 1
            continue
        raw = weather_day[0]["official_report"]
        bounds = research_model_venue.integer_bucket(market)
        try:
            amount = Decimal(market["expiration_value"])
        except (TypeError, InvalidOperation):
            rejected["no explicit native integer ruling"] += 1
            continue
        if not bounds or not amount.is_finite() or int(amount) != amount:
            rejected["incompatible native temperature rule"] += 1
            continue
        target = {"lower_f": bounds[0], "upper_f": bounds[1]}
        yes = int(market["yes_payout"])
        if _outcome(target, int(amount)) != yes:
            rejected["native ruling mismatches binary payout"] += 1
            continue
        if yes == 1:
            audit.append({"date": day, "market": market, "verified_cli": raw, "native_low_f": int(amount),
                          "raw_cli_agrees": int(amount) == raw["low_f"],
                          "secondary_temperature_wording_conflict": "highest temperature" in rules})
        if not market["opened_at"] or not market["closed_at"]:
            continue
        for view in weather_day:
            at = utc(view["at"])
            if utc(market["opened_at"]) <= at < utc(market["closed_at"]):
                quote = research_model_venue._midpoint(data, market, cache, at)
                matches[f"KAUS/{day}/{view['hour']}"].append({"market": market, "bucket": target, "native_yes": yes,
                               "venue_low_f": int(amount), "display_price": quote})
    return dict(matches), audit, dict(rejected)


def gather(data, reference):
    earlier = json.loads(gzip.decompress(reference.read_bytes()))
    if earlier["version"] != precision.VERSION:
        raise ValueError("Expected earlier 2025 station precision calibration")
    weather, gaps, sources = [], {}, {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        for station, (rows, missing, files) in zip(STATIONS, pool.map(lambda station: shared._station(data, station, DATES), STATIONS), strict=True):
            weather.extend(rows)
            gaps[station], sources[station] = missing, files
    payouts, audit, omitted = _austin(data, weather)
    return {"version": VERSION, "calibration": earlier["rows"]["calibration"], "calibration_window": earlier["windows"]["calibration"],
            "weather": weather, "gaps": gaps, "sources": sources, "native": payouts, "native_audit": audit,
            "strict_prices": research_model_high_warm.strict_candles(data, weather, payouts, hours=(9, 13, 17, 21)),
            "native_gaps": omitted, "earlier_source": research_model._source_entry(data, reference)}


def evaluate(data, inputs):
    path, digest = research_model.freeze_inputs(data, inputs)
    widths, exceptions = shared._fit_shared(inputs["calibration"])
    matched = [row for row in inputs["weather"] if (row["station"], row["hour"]) in exceptions]
    scored = precision._eval(matched, widths, exceptions)
    native = portable._prices(scored, inputs["native"], widths)
    marked = [row for row in native if row["display_price"]]
    strict = portable._prices(scored, inputs.get("strict_prices", {}), widths)
    metrics = research_model.metrics(scored, "Later full winter weather sample", shared.METHODS)
    for station in STATIONS:
        metrics.extend(research_model.metrics([row for row in scored if row["station"] == station], station, shared.METHODS))
    metrics.extend(portable._payout_metrics(native, "Exact verified winter Austin payouts"))
    metrics.extend(portable._payout_metrics(marked, "Winter Austin payouts with earlier prices", include_market=True))
    metrics.extend(portable._payout_metrics(strict, "Winter Austin payouts with exact two-sided candle", include_market=True))
    contrasts = [research_model.date_bootstrap(scored, base, challenger, "brier")
                 for base, challenger in (("txn", "combined"), ("combined", "guarded_cap"), ("precision_center", "guarded_cap"))]
    selected = shared._examples(scored, native)
    extreme = min((row for row in scored if row["station"] != "KAUS"), key=lambda row: row["actual_f"], default=None)
    if extreme:
        selected.append(precision._case(extreme, "winter weather-only"))
    raw_conflicts = {(row["station"], row["date"]) for row in scored if row["cap_violated"]}
    audits = inputs["native_audit"]
    record = {"version": VERSION, "title": "A winter check for the exact daily-low station bound",
              "hypothesis": "A physically bounded minimum learned from earlier stations can retain its benefit in winter weather and on an older native daily-low rule.",
              "target": "Raw fixed climate-day minima at seven other stations and verified native Austin payouts",
              "venues": ["kalshi"] if native else [], "date_window": {"reference_calibration": inputs["calibration_window"],
                                  "winter_verification": list(map(str, DATES))},
              "coverage": {**research_model._independent_counts(scored), "native_payouts": len(native), "quoted_payouts": len(marked),
                           "strict_candle_payouts": len(strict),
                           "matched_station_report_native_days": len(audits),
                           "season_bound_disagreement_days": len(raw_conflicts),
                           "native_source_disagreements": sum(not row["raw_cli_agrees"] for row in audits),
                           "native_secondary_rule_wording_conflicts": sum(row["secondary_temperature_wording_conflict"] for row in audits)},
              "metrics": metrics, "contrasts": contrasts, "cases": selected, "missing": inputs["gaps"],
              "limits": ["The model keeps the earlier pooled 2025 station reserve and widths. Each winter forecast uses original NOAA issue and MADIS observation receipt clocks.",
                         "The exact older native Austin rules identify the CLIAUS report and a minimum in the primary clause. The generic secondary wording says highest; the native payout and raw numeric ruling are checked separately.",
                         "Winter weather at other sites is scored independently of any later daily-low listings.",
                         "The winter source uses an earlier NBM bulletin release. Strict two-sided display candles use 09, 13, 17 and 21 LST with a five-minute allowance. Native payouts and historical display evidence remain separate from execution."],
              "provenance": {"frozen_input": str(path.relative_to(data)), "frozen_input_sha256": digest,
                             "reference": inputs["earlier_source"], "source_sha256": research_model.run_sources(
                                 sys.modules[__name__], shared, expansion_weather, precision, research_model_high_warm)}}
    return research_model.write_record(data, record, {"weather": scored, "payouts": native, "strict_prices": strict, "settlement_audit": audits,
                                                      "native_gaps": inputs["native_gaps"]})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--earlier", type=Path)
    source.add_argument("--input", type=Path)
    args = parser.parse_args()
    inputs = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else gather(args.data, args.earlier)
    print(evaluate(args.data, inputs))


if __name__ == "__main__":
    main()
