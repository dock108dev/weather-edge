"""Validate decoded daily-low bounds across nine additional stations and native payouts."""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import gzip
import json
from pathlib import Path
from statistics import mean
import sys

from . import expansion_metar, expansion_replay, expansion_weather, research_model
from . import research_model_high_transfer, research_model_low_full_day, research_model_low_precision as precision
from .nbm import climate_window, utc

VERSION = "daily-low-bound-portability-v1"
STATIONS, ADAPT, TEST = (research_model_high_transfer.STATIONS, research_model_high_transfer.ADAPT, research_model_high_transfer.TEST)
METHODS = precision.METHODS


def _views(station, day, forecasts, observations, label, *, delay_minutes):
    start, _ = climate_window(day, expansion_weather.STATIONS[station][3])
    rows = []
    for hour in research_model.HOURS[1:]:
        decision = start + timedelta(hours=hour)
        view = research_model_low_full_day._at(station, day, decision, forecasts, observations, label, delay_minutes=delay_minutes)
        if not view:
            continue
        known = expansion_weather.observations_as_of(observations, start, decision, delay_minutes=delay_minutes)
        evidence = expansion_metar.upper_bound(known, station, start, decision)
        point = expansion_metar.upper_bound(known, station, start, decision, include_six_hour=False)
        rows.append({**view, "metar_upper_bound": evidence, "point_upper_bound": point})
    return rows


def _station(data, station):
    labels, observations, cards = expansion_replay._station_data(data, station, ADAPT[0], TEST[1])
    results = {"adapt": [], "test": [], "recorded_station_receipts": []}
    omitted = Counter()
    for name, (first, last) in (("adapt", ADAPT), ("test", TEST)):
        for day in research_model._days(first, last):
            expected = labels.get(str(day))
            label = precision._scoped_labels(data, station, day, expected)
            if not label or label["low_f"] is None:
                omitted[f"{name}: incomplete raw CLI minimum"] += 1
                continue
            if name == "adapt" and utc(label["published_at"]) >= climate_window(TEST[0], expansion_weather.STATIONS[station][3])[0]:
                omitted[f"{name}: raw CLI minimum published after test started"] += 1
                continue
            forecasts = [card for issue, card in cards.items() if day - timedelta(days=2) <= issue.date() <= day + timedelta(days=1)]
            results[name].extend(_views(station, day, forecasts, observations[day], label, delay_minutes=10))
            if name == "test":
                path = data / "expansion" / "replay" / str(day) / f"{station}.json"
                if path.is_file():
                    archive = json.loads(path.read_bytes())
                    recorded = [row for row in archive["observations"] if row.get("receipt_source")]
                    results["recorded_station_receipts"].extend(_views(station, day, forecasts, recorded, label, delay_minutes=None))
    return results, dict(omitted)


def gather(data):
    rows = {name: [] for name in ("adapt", "test", "recorded_station_receipts")}
    omissions = {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        for station, (evidence, gaps) in zip(STATIONS, pool.map(lambda station: _station(data, station), STATIONS), strict=True):
            for phase in rows:
                rows[phase].extend(evidence[phase])
            omissions[station] = gaps
    native, source_audit, unavailable = research_model_high_transfer._market_rows(data, rows["test"], family="daily-low")
    return {"version": VERSION, "rows": rows, "native": native, "source_audit": source_audit,
            "omissions": omissions, "native_omissions": unavailable}


def _prices(rows, native, widths):
    output = []
    for row in rows:
        station, day, hour = row["station"], row["date"], row["hour"]
        for target in native.get(f"{station}/{day}/{hour}", []):
            probabilities = {}
            bound = row["metar_upper_bound"]["upper_f"] if row["metar_upper_bound"] else None
            for method, estimate in row["estimates"].items():
                width = widths[method if method in {"txn", "combined"} else "precision_center", station, hour]
                cap = bound if method in {"hard_cap", "guarded_cap"} else None
                probability, = precision._bucket_probabilities([target["bucket"]], estimate["mean_f"], width, cap,
                                                                  row["uncapped_weight"] if method == "guarded_cap" else 0)
                probabilities[method] = probability
            if display := target["display_price"]:
                probabilities["market"] = display["midpoint"]
            output.append({**target, "station": station, "date": day, "at": row["at"], "probabilities": probabilities,
                           "official_report": row["official_report"], "metar_evidence": row["metar_upper_bound"], "forecast": row["forecast"]})
    return output


def _payout_metrics(rows, label, include_market=False):
    if not rows:
        return []
    names = {**METHODS, "market": "Earlier Kalshi display midpoint"} if include_market else METHODS
    return [{"group": label, "method": method, "label": name, "bucket_cases": len(rows),
             "calendar_dates": len({row["date"] for row in rows}), "station_days": len({(row["station"], row["date"]) for row in rows}),
             "bucket_brier": mean((row["probabilities"][method] - row["native_yes"]) ** 2 for row in rows)}
            for method, name in names.items()]


def _case(row, settled):
    raw = precision._case(row, "new station")
    if settled:
        market = settled["market"]
        raw.update(venue="kalshi", instrument_id=market["instrument_id"], market=market,
                   native_payout=settled["native_yes"], payout_probability=settled["probabilities"]["guarded_cap"])
    return raw


def evaluate(data, corpus):
    frozen, digest = research_model.freeze_inputs(data, corpus)
    rows = corpus["rows"]
    widths, exceptions = precision._calibrate(rows["adapt"])
    eligible = {phase: [row for row in rows[phase] if ("txn", row["station"], row["hour"]) in widths and row["metar_upper_bound"]]
                for phase in ("test", "recorded_station_receipts")}
    scored = {phase: precision._eval(sample, widths, exceptions) for phase, sample in eligible.items()}
    groups = {"test": "Archive-lag model comparison", "recorded_station_receipts": "Matched original MADIS station receipts"}
    metrics, coverage, uncertainty = [], {}, []
    for phase, test in scored.items():
        metrics.extend(research_model.metrics(test, groups[phase], METHODS))
        violations = [row for row in test if row["cap_violated"]]
        coverage[phase] = {**research_model._independent_counts(test), "bound_exceptions": len({(row["station"], row["date"]) for row in violations}),
                           "bound_exception_decisions": len(violations), "six_hour_tightening": sum(
                               row["point_upper_bound"] is not None and row["metar_upper_bound"]["upper_f"] < row["point_upper_bound"]["upper_f"]
                               for row in test)}
        if test:
            uncertainty.append({"group": groups[phase], **research_model.date_bootstrap(test, "combined", "guarded_cap", "brier")})
    for station in STATIONS:
        metrics.extend(research_model.metrics([row for row in scored["test"] if row["station"] == station], station, METHODS))
    payout = _prices(scored["test"], corpus["native"], widths)
    matched = [row for row in payout if row["display_price"]]
    metrics.extend(_payout_metrics(payout, "Verified native daily-low payouts"))
    metrics.extend(_payout_metrics(matched, "Same native payouts with display prices", include_market=True))
    audit = corpus["source_audit"]
    coverage["actual_venue"] = {"station_days": len(audit), "raw_cli_differs_from_native": sum(not row["source_equal"] for row in audit),
                                "payout_cases": len(payout), "priced_cases": len(matched)}
    p_by_clock = {(row["station"], row["date"], row["at"]): row for row in payout if row["native_yes"] == 1}
    examples = sorted(scored["test"], key=lambda row: (row["cap_violated"],
                       row["estimates"]["combined"]["brier"] - row["estimates"]["guarded_cap"]["brier"]), reverse=True)
    cases = []
    seen = set()
    for row in examples:
        if row["station"] in seen:
            continue
        cases.append(_case(row, p_by_clock.get((row["station"], row["date"], row["at"]))))
        seen.add(row["station"])
    record = {"version": VERSION, "title": "Do daily-low station bounds transfer to new airports?",
              "hypothesis": "Raw local temperature precision limits physically plausible minima at new sites. "
                            "Original MADIS receipts test whether the gain survives a verified station clock.",
              "target": "Raw official daily lows with separately verified native Kalshi daily-low payouts", "venues": ["kalshi"],
              "date_window": {"adapt": list(map(str, ADAPT)), "test": list(map(str, TEST))},
              "metrics": metrics, "cases": cases, "uncertainty": uncertainty, "coverage": coverage,
              "missing": corpus["omissions"], "payout_missing": corpus["native_omissions"],
              "limits": ["Earlier 13 calendar dates calibrate each station and clock. Later 17 dates remain untouched by fitting.",
                         "The first comparison assigns the historical ten-minute archive allowance. The separate original-receipt panel requires a NOAA MADIS match.",
                         "Archived forecast availability is the original NOAA object publication. An observed exception allowance keeps empirical forecast mass above the bound.",
                         "Physical raw report values and native exchange expiration are retained independently. The archive has display prices for only a subset."],
              "provenance": {"frozen_input": str(frozen.relative_to(data)), "frozen_input_sha256": digest,
                             "raw_temperature_source": expansion_metar.ASOS_FAQ,
                             "source_sha256": research_model.run_sources(sys.modules[__name__], precision, expansion_metar)}}
    return research_model.write_record(data, record, {"scored": scored, "payouts": payout, "source_audit": audit})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--input", type=Path)
    args = parser.parse_args()
    corpus = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else gather(args.data)
    print(evaluate(args.data, corpus))


if __name__ == "__main__":
    main()
