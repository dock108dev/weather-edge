"""Check whether the same later-date high-temperature correction transfers across seasons."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
import gzip
import json
from pathlib import Path
import sys

from sklearn import __version__ as sklearn_version

from . import expansion_nbm, expansion_weather, research_model, research_model_high, research_model_high_transfer as transfer
from . import venue_archive, venue_benchmark
from .nbm import iso, utc

VERSION = "daily-high-cross-season-transfer-v1"
FIT = (date(2026, 6, 1), date(2026, 7, 31))
CALIBRATION = (date(2026, 8, 2), date(2026, 8, 15))
ADAPT = (date(2026, 8, 17), date(2026, 8, 24))
TEST = (date(2026, 8, 27), date(2026, 9, 21))
COHORT_ORDER = ("late summer", "autumn")
METHODS = transfer.METHODS


def _load(path):
    return json.loads(gzip.decompress(path.read_bytes()))


def _earlier(seed):
    if seed["version"] != research_model_high.VERSION or set(seed["stations"]) & set(transfer.STATIONS):
        raise ValueError("The earlier training and test stations must be disjoint")
    safe = {}
    for key, (first, last), successor in (("fit", FIT, CALIBRATION[0]), ("calibration", CALIBRATION, ADAPT[0])):
        cutoff = datetime.combine(successor, datetime.min.time(), timezone.utc)
        safe[key] = [row for row in seed["rows"]["fit"] if first <= date.fromisoformat(row["date"]) <= last
                     and utc(row["official_report"]["published_at"]) < cutoff]
        if not safe[key]:
            raise ValueError(f"No earlier chronologically eligible {key} rows")
    return safe


def strict_candles(data, weather, markets, *, hours=(13,)):
    """Use the complete exact-scheduled candle after a fixed five-minute allowance."""
    pairs = {}
    with venue_archive.connect(data) as db:
        for row in weather:
            if row["hour"] not in hours:
                continue
            key = f"{row['station']}/{row['date']}/{row['hour']}"
            observed = utc(row["at"])
            comparison_at = observed + timedelta(minutes=5)
            for strike in markets.get(key, []):
                market = strike["market"]
                if comparison_at >= utc(market["closed_at"]):
                    continue
                mark, _ = venue_benchmark._history_point(db, {**market, "venue": "kalshi"}, observed)
                if mark:
                    price = {**mark, "midpoint": mark["probability"], "comparison_at": iso(comparison_at)}
                    pairs.setdefault(key, []).append({**strike, "display_price": price})
    return pairs


def gather(data, baseline, autumn):
    earlier = _earlier(_load(baseline))
    later = _load(autumn)
    if later["version"] != transfer.VERSION or any(date.fromisoformat(row["date"]) < transfer.TEST[0] for row in later["rows"]["test"]):
        raise ValueError("Expected frozen autumn new-station transfer data")
    warm, gaps = {"adapt": [], "test": []}, []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(transfer._weather_rows, data, station, adapt=ADAPT, test=TEST) for station in transfer.STATIONS]
        for task in futures:
            split, omitted = task.result()
            gaps.extend(omitted)
            for key in warm:
                warm[key].extend(split[key])
    markets, audits, skipped = transfer._market_rows(data, warm["test"], test=TEST)
    cohorts = {"late summer": {"rows": warm, "gaps": gaps, "markets": markets, "audits": audits, "skipped": skipped},
               "autumn": {key: later[key] for key in ("rows", "gaps", "markets", "audits", "skipped")}}
    for evidence in cohorts.values():
        evidence["strict_midday"] = strict_candles(data, evidence["rows"]["test"], evidence["markets"])
    return {"version": VERSION, "baseline_rows": earlier,
            "reference_paths": [research_model._source_entry(data, path) for path in (baseline, autumn)],
            "cohorts": cohorts}


def _cohort_metrics(name, rows, payouts, priced):
    metrics = research_model.metrics(rows, name + " · same weather cases", METHODS)
    for hour in (3, 9, 13, 17):
        metrics.extend(research_model.metrics([row for row in rows if row["hour"] == hour], f"{name} · {hour:02}:00 LST", METHODS))
    metrics.extend(transfer._market_metrics(payouts, name + " · native payouts", False))
    metrics.extend(transfer._market_metrics(priced, name + " · native payouts and prices", True))
    return metrics


def _useful_cases(rows, group):
    graded = sorted(rows, key=lambda row: (row["venue_high_f"] != row["official_report"]["high_f"],
                                          abs(row["probabilities"]["noaa"] - row["native_yes"])), reverse=True)
    selected = []
    seen = set()
    for row in graded:
        if len(selected) >= 3:
            break
        if row["station"] not in seen:
            seen.add(row["station"])
            selected.append({**transfer._case(row), "season": group})
    return selected


def evaluate(data, inputs):
    path, digest = research_model.freeze_inputs(data, inputs)
    metrics, contrasts, cases, details, coverage = [], [], [], {}, {}
    for group in COHORT_ORDER:
        source = inputs["cohorts"][group]
        rows, local_bias = transfer._weather_probabilities({**source, "baseline_rows": inputs["baseline_rows"]})
        if not rows:
            continue
        payouts = transfer._market_probabilities(rows, source["markets"])
        priced = [row for row in payouts if row["display_price"] is not None]
        strict = transfer._market_probabilities(rows, source.get("strict_midday", {}))
        metrics.extend(_cohort_metrics(group, rows, payouts, priced))
        metrics.extend(transfer._market_metrics(strict, f"{group} · 13:05 LST exact candle with spread ≤30¢", True))
        contrasts.extend({"group": group, **research_model.date_bootstrap(rows, base, challenger, metric)}
                         for base, challenger in (("noaa", "ridge"), ("noaa", "nonlinear"), ("noaa", "bias"),
                                                  ("ridge", "local_ridge"), ("nonlinear", "local_nonlinear"))
                         for metric in ("mae", "brier"))
        cases.extend(_useful_cases(payouts, group))
        source_audits = source["audits"]
        coverage[group] = {**research_model._independent_counts(rows), "native_bucket_cases": len(payouts),
                           "priced_bucket_cases": len(priced), "native_source_station_days": len(source_audits),
                           "strict_midday_cases": len(strict),
                           "weather_provider_disagreements": sum(not row["source_equal"] for row in source_audits)}
        details[group] = {"predictions": rows, "exact_payouts": payouts, "strict_midday": strict,
                          "source_audit": source_audits, "local_bias": local_bias,
                          "gaps": source["gaps"], "missing_native": source["skipped"]}
    record = {"version": VERSION, "title": "A daily-high correction across late summer and autumn",
              "hypothesis": "The same correction trained at six other stations before mid-August can improve on NOAA at nine later stations in both warmer and cooler cases.",
              "target": "Exact raw NWS daily highs, native Weather Company rulings and independent Kalshi payouts",
              "venues": ["kalshi"], "date_window": {"fit": list(map(str, FIT)), "calibration": list(map(str, CALIBRATION)),
                 "warm_adapt": list(map(str, ADAPT)), "warm_validated": list(map(str, TEST)), "autumn_validated": list(map(str, transfer.TEST))},
              "coverage": {"fit": research_model._independent_counts(inputs["baseline_rows"]["fit"]),
                           "calibration": research_model._independent_counts(inputs["baseline_rows"]["calibration"]), **coverage},
              "metrics": metrics, "contrasts": contrasts, "cases": cases,
              "limits": ["Global correction, width calibration, and weather feature settings are the same in both later periods. Local-site adaptation has an earlier sample within its own season.",
                         "Training labels are IEM's archived summaries, and verification labels come from the exact complete raw CLI report. Native rulings and weather labels are compared independently.",
                         "As-issued NOAA guidance uses public archive publication clocks. Historical station measurements use the original study's ten-minute timing assumption for matched weather tests.",
                         "The strict price subset uses weather by 13:00 LST and an exact 13:00 two-sided candle with a five-minute decision allowance and at most 30 cents of spread.",
                         "Hourly price observations and native payouts support probability scoring. They do not establish historical fill evidence."],
              "provenance": {"same_fit": inputs["reference_paths"], "frozen_input": str(path.relative_to(data)), "frozen_input_sha256": digest,
                             "sklearn": sklearn_version, "source_sha256": research_model.run_sources(sys.modules[__name__], transfer,
                                                                                                   expansion_nbm, expansion_weather, venue_benchmark)}}
    return research_model.write_record(data, record, details)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--autumn", type=Path)
    parser.add_argument("--input", type=Path)
    args = parser.parse_args()
    if args.input is None and (args.baseline is None or args.autumn is None):
        parser.error("Provide both frozen reference inputs or the completed input")
    source = _load(args.input) if args.input else gather(args.data, args.baseline, args.autumn)
    print(evaluate(args.data, source))


if __name__ == "__main__":
    main()
