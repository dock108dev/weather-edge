"""Test an earlier fixed daily-low observation guard at nine independent stations."""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta, timezone
import gzip
import json
from pathlib import Path
import sys

from . import expansion_metar, expansion_nbm, expansion_weather, research_model
from . import research_model_high_transfer as transfer, research_model_high_warm, research_model_low_portability as portable
from . import research_model_low_precision as precision
from .nbm import climate_window, utc

VERSION = "daily-low-cross-season-native-guard-v1"
SEASONS = {"late summer": research_model_high_warm.TEST, "autumn": transfer.TEST}
METHODS = precision.METHODS


def _station(data, station, dates):
    recorded, missing, source = [], Counter(), {}
    forecasts = {}
    for day in research_model._days(dates[0] - timedelta(days=1), dates[1] + timedelta(days=1)):
        forecasts[day] = [card for hour in research_model.NBS_CYCLES
                          if (card := expansion_nbm.load_card(data, station, datetime.combine(day, time(hour), timezone.utc)))]
    for day in research_model._days(*dates):
        path = data / "expansion" / "replay" / str(day) / f"{station}.json"
        if not path.is_file():
            missing["no archived full climate day"] += 1
            continue
        archived = json.loads(path.read_bytes())
        _, close = climate_window(day, expansion_weather.STATIONS[station][3])
        labels = [report for report in archived["official_reports"] if report["low_f"] is not None and
                  utc(report["published_at"]) >= close]
        if not labels:
            missing["no verified complete raw CLI minimum"] += 1
            continue
        official = max(labels, key=lambda report: report["published_at"])
        observed = [row for row in archived["observations"] if row.get("receipt_source") and row.get("raw")]
        cards = [card for delta in (-1, 0, 1) for card in forecasts[day + timedelta(days=delta)]]
        evidence = [row for row in portable._views(station, day, cards, observed, official, delay_minutes=None)
                    if row["metar_upper_bound"]]
        if not evidence:
            missing["no originally timed exact-station METAR plus full guidance"] += 1
            continue
        source[str(day)] = research_model._source_entry(data, path)
        recorded.extend(evidence)
    return recorded, dict(missing), source


def gather(data, earlier):
    prior = json.loads(gzip.decompress(earlier.read_bytes()))
    if prior["version"] != precision.VERSION:
        raise ValueError("Expected the earlier original-station precision calibration")
    groups = {}
    for name, dates in SEASONS.items():
        rows, gaps, sources = [], {}, {}
        with ThreadPoolExecutor(max_workers=3) as pool:
            tasks = {station: pool.submit(_station, data, station, dates) for station in transfer.STATIONS}
            for station, task in tasks.items():
                snapshots, missing, paths = task.result()
                rows.extend(snapshots)
                gaps[station], sources[station] = missing, paths
        native, audit, omissions = transfer._market_rows(data, rows, test=dates, family="daily-low")
        strict = research_model_high_warm.strict_candles(data, rows, native, hours=(9, 13, 17, 21))
        groups[name] = {"weather": rows, "gaps": gaps, "sources": sources, "native": native, "audit": audit,
                        "strict_prices": strict, "native_gaps": omissions}
    return {"version": VERSION, "calibration": prior["rows"]["calibration"], "calibration_window": prior["windows"]["calibration"],
            "reference": research_model._source_entry(data, earlier), "cohorts": groups}


def _fit_shared(calibration):
    pooled, reserves = precision._calibrate([{**row, "station": "joint earlier calibration"} for row in calibration])
    widths = {(method, station, hour): width for (method, _, hour), width in pooled.items() for station in transfer.STATIONS}
    exceptions = {(station, hour): rule for (_, hour), rule in reserves.items() for station in transfer.STATIONS}
    return widths, exceptions


def _examples(weather, payouts):
    paid = {(row["station"], row["date"], row["at"]): row for row in payouts if row["native_yes"]}
    ordered = sorted((row for row in weather if (row["station"], row["date"], row["at"]) in paid),
                     key=lambda row: (row["cap_violated"], row["estimates"]["combined"]["brier"] -
                                      row["estimates"]["guarded_cap"]["brier"]), reverse=True)
    shown, seen = [], set()
    for row in ordered:
        if len(shown) >= 2:
            break
        if row["station"] not in seen:
            shown.append(portable._case(row, paid[row["station"], row["date"], row["at"]]))
            seen.add(row["station"])
    return shown


def evaluate(data, inputs):
    frozen, digest = research_model.freeze_inputs(data, inputs)
    widths, exceptions = _fit_shared(inputs["calibration"])
    metrics, comparisons, cases, coverage, details = [], [], [], {}, {}
    for group in SEASONS:
        source = inputs["cohorts"][group]
        eligible = [row for row in source["weather"] if (row["station"], row["hour"]) in exceptions]
        scored = precision._eval(eligible, widths, exceptions)
        payout = portable._prices(scored, source["native"], widths)
        priced = [row for row in payout if row["display_price"]]
        strict = portable._prices(scored, source.get("strict_prices", {}), widths)
        metrics.extend(research_model.metrics(scored, group + " · original station receipts and raw CLI", METHODS))
        metrics.extend(portable._payout_metrics(payout, group + " · independently paid Kalshi buckets"))
        metrics.extend(portable._payout_metrics(priced, group + " · matched prices and payouts", include_market=True))
        metrics.extend(portable._payout_metrics(strict, group + " · exact four-hour choices with spread ≤30¢", include_market=True))
        if scored:
            comparisons.extend({"group": group, **research_model.date_bootstrap(scored, baseline, challenger, "brier")}
                               for baseline, challenger in (("txn", "combined"), ("combined", "guarded_cap"), ("precision_center", "guarded_cap")))
        violations = [row for row in scored if row["cap_violated"]]
        bound_disagreements = {(row["station"], row["date"]) for row in violations}
        native_exceptions = {(row["station"], row["date"]) for row in payout if row["venue_low_f"] > row["metar_evidence"]["upper_f"]}
        coverage[group] = {**research_model._independent_counts(scored), "raw_cli_disagreement_days": len(bound_disagreements),
                           "raw_cli_disagreement_clocks": len(violations), "market_bound_disagreement_days": len(native_exceptions),
                           "physical_and_provider_labels": len(source["audit"]),
                           "provider_label_disagreements": sum(not row["source_equal"] for row in source["audit"]),
                           "native_payouts": len(payout), "priced_payouts": len(priced), "strict_prices": len(strict)}
        details[group] = {"predictions": scored, "payouts": payout, "strict_prices": strict, "weather_gaps": source["gaps"],
                          "native_gaps": source["native_gaps"],
                          "source_comparison": source["audit"]}
        cases.extend(_examples(scored, payout))
    record = {"version": VERSION, "title": "An exact-station minimum bound across warmer and cooler seasons",
              "hypothesis": "The same physically defined station upper bound and earlier exception reserve can improve daily-low predictions without site-specific fitting.",
              "target": "Exact raw daily minima and separately verified Kalshi payouts at nine unseen stations",
              "venues": ["kalshi"], "date_window": {"earlier_two_station_calibration": inputs["calibration_window"],
                 **{key: list(map(str, value)) for key, value in SEASONS.items()}},
              "coverage": {"calibration": research_model._independent_counts(inputs["calibration"]), **coverage},
              "metrics": metrics, "contrasts": comparisons, "cases": cases,
              "limits": ["Calibration pools only the earlier Central Park and Midway residuals and bound disagreement allowance. No new-site weather or market result fits these settings.",
                         "All later station samples need their original matched MADIS receipt. Measurements without source receipt remain in the archive as separate context.",
                         "The station bound uses whole Fahrenheit compatibility and climate-contained station reports. Its reserve admits uncertainty about station precision and report differences.",
                         "The stricter priced comparison uses exact 09:00, 13:00, 17:00 or 21:00 candles and a five-minute availability allowance. Two-sided display spread must be at most 30 cents.",
                         "Native provider values and verified payout evidence identify market disagreements separately from the physical raw report."],
              "provenance": {"frozen_input": str(frozen.relative_to(data)), "frozen_input_sha256": digest,
                             "earlier_calibration": inputs["reference"], "metar_documentation": expansion_metar.ASOS_FAQ,
                             "source_sha256": research_model.run_sources(sys.modules[__name__], portable, precision, expansion_metar,
                                                                                 expansion_weather)}}
    return research_model.write_record(data, record, details)


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
