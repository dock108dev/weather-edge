"""Apply the four-station venue probability models to two untouched stations."""

import argparse
from collections import Counter, defaultdict
from datetime import timedelta
from functools import cache
import gzip
from hashlib import sha256
import json
from pathlib import Path
from statistics import mean
import sys

from . import research_model, venue_archive, venue_benchmark, venue_snow_data, venue_warmseason, venue_warmseason_study
from .market import decimal, iso, parse_time
from .model import _limits, _probability
from .venue_contracts import STATION_OFFSETS

VERSION = "held-venue-transfer-new-stations-v1"
FIT_INPUT = Path("research/model-inputs/warm-season-venue-probabilities-v2-87e3128ee749e95d51408561975672d911c943bc98254ff5345de1a3cb455910.json.gz")
TEST = {"KSFO": ("2026-08-01", "2026-09-30"), "KPHL": ("2026-10-15", "2026-11-14")}
HOURS = (9, 13)


def _station_labels(data, station):
    start, end = TEST[station]
    path = data / "venues" / "snow-climate" / station / "2026.json"
    raw, receipt = path.read_bytes(), json.loads(path.with_suffix(".json.receipt.json").read_bytes())
    if sha256(raw).hexdigest() != receipt["sha256"]:
        raise ValueError(f"Retained CLI changed for {station}")
    labels = {}
    for item in json.loads(raw)["results"]:
        if not start <= item["valid"] <= end:
            continue
        parsed = venue_snow_data._day_row(station, item, offset=STATION_OFFSETS[station])
        if not parsed or type(parsed["high_f"]) not in (int, float) or parsed["high_f"] % 1:
            continue
        day = parsed["date"]
        if day in labels:
            raise ValueError(f"Multiple complete CLI rows for {station} on {day}")
        labels[day] = {"actual_f": int(parsed["high_f"]), "product": parsed["product"], "source": parsed["source"],
                       "publication_at": parsed["published_at"]}
    return labels, receipt


def gather(data):
    fit_raw = (data / FIT_INPUT).read_bytes()
    original = json.loads(gzip.decompress(fit_raw))
    fit = [row for row in original["timeline"] if row["hour_lst"] in HOURS
           and venue_warmseason_study.FIT[0] <= row["date"] <= venue_warmseason_study.FIT[1]
           and row["label"]["publication_at"] < "2026-08-01T16:00:00Z"]
    labels, reports = {}, {}
    for station in TEST:
        labels[station], reports[station] = _station_labels(data, station)
    candidates = [row for station, dates in TEST.items() for row in venue_warmseason.pairs(data, stations=(station,), window=dates)]
    if {row["station"] for row in candidates} & {row["station"] for row in fit}:
        raise ValueError("New-station test accidentally overlaps the fitting stations")
    forecast = cache(venue_warmseason_study._forecast)
    rows, changed, missing = [], [], Counter()
    with venue_archive.connect(data) as db:
        for pair in candidates:
            station, day = pair["station"], pair["day"]
            left, right = pair["polymarket"], pair["kalshi"]
            label = labels[station].get(day)
            if decimal(left["yes_payout"]) != decimal(right["yes_payout"]):
                changed.append({**pair, "label": label})
            if label is None:
                missing[station + " exact CLI unavailable"] += len(HOURS)
                continue
            low, high = pair["bounds"]
            actual = label["actual_f"]
            target = int((low is None or low <= actual) and (high is None or actual <= high))
            for clock in HOURS:
                stamp = parse_time(left["climate_starts_at"]) + timedelta(hours=clock)
                at = stamp + venue_benchmark.WAIT
                if any(not parse_time(m["opened_at"]) <= at < parse_time(m["closed_at"]) for m in (left, right)):
                    missing[station + " a venue outside listed trading hours"] += 1
                    continue
                p, reason_p = venue_benchmark._history_point(db, left, stamp)
                k, reason_k = venue_benchmark._history_point(db, right, stamp)
                if not p or not k:
                    if not p:
                        missing[f"{station} Polymarket US {reason_p}"] += 1
                    if not k:
                        missing[f"{station} Kalshi {reason_k}"] += 1
                    continue
                guide = forecast(data, station, day, at)
                weather = None
                if guide:
                    weather = _probability(_limits({"lower_f": low, "upper_f": high}), [guide["high_f"]], guide["high_sd_f"])
                else:
                    missing[station + " as-issued NOAA high or spread unavailable"] += 1
                rows.append({"station": station, "date": day, "hour_lst": clock, "at": iso(at), "bounds": [low, high],
                             "label": label, "outcome": target, "polymarket_market": left, "kalshi_market": right,
                             "polymarket_price": p, "kalshi_price": k, "forecast": guide, "noaa": weather,
                             "polymarket": p["probability"], "kalshi": k["probability"], "average": (p["probability"] + k["probability"]) / 2,
                             "poly_payout": venue_benchmark._binary_result(left), "kalshi_payout": venue_benchmark._binary_result(right)})
    return {"version": VERSION, "fit": fit, "test": rows, "catalog_count": dict(Counter(row["station"] for row in candidates)),
            "missing": dict(missing), "cash_disagreements": changed, "stations_and_reports": reports, "fit_input_sha256": sha256(fit_raw).hexdigest()}


def _metrics(rows, group, target, methods):
    result = []
    for model in methods:
        errors = defaultdict(list)
        for row in rows:
            if row.get(model) is not None and row.get(target) is not None:
                errors[row["date"]].append((row[model] - row[target]) ** 2)
        if errors:
            used = [row for row in rows if row.get(model) is not None and row.get(target) is not None]
            result.append({"group": group, "model": venue_warmseason_study.METHODS[model], "target": target,
                           "brier": mean(mean(values) for values in errors.values()), "bucket_cases": len(used),
                           "calendar_dates": len(errors), "station_days": len({(row["station"], row["date"]) for row in used})})
    return result


def _case(row):
    left, right = row["polymarket_market"], row["kalshi_market"]
    return {"venue": left["venue"], "instrument_id": left["instrument_id"], "related_venue": right["venue"],
            "related_instrument_id": right["instrument_id"], "station": row["station"], "date": row["date"], "at": row["at"],
            "title": left["title"], "detail": f"Published physical high {row['label']['actual_f']}°F. "
               f"The two display midpoint proxies were {row['polymarket']:.0%} and {row['kalshi']:.0%}. "
               f"Actual YES paid ${row['poly_payout']} and ${row['kalshi_payout']} respectively.",
            "data": row}


def evaluate(data, corpus):
    frozen, digest = research_model.freeze_inputs(data, corpus)
    test = corpus["test"]
    with_weather = [row for row in test if row["noaa"] is not None]
    fit = venue_warmseason_study._fit(corpus["fit"], test)
    blended = venue_warmseason_study._noaa_blend(corpus["fit"], with_weather)
    groups = {"Both held new stations": test, **{key: [row for row in test if row["station"] == key] for key in TEST}}
    quote_methods = ("polymarket", "kalshi", "average", "combiner")
    full_methods = (*quote_methods, "noaa", "noaa_25pct", "noaa_market_fit")
    metrics = []
    for name, rows in groups.items():
        common = [row for row in rows if row["noaa"] is not None]
        metrics.extend(_metrics(rows, f"{name} / same two venue prices", "outcome", quote_methods))
        metrics.extend(_metrics(common, f"{name} / same NOAA and both prices", "outcome", full_methods))
        for target in ("poly_payout", "kalshi_payout"):
            metrics.extend(_metrics(common, f"{name} / each verified native payout", target, full_methods))
    coverage = {"fitted_bucket_cases": len(corpus["fit"]), "test_catalog_pairs": corpus["catalog_count"],
                "held_both_venue_clocks": dict(Counter(row["station"] for row in test)),
                "held_same_noaa": dict(Counter(row["station"] for row in with_weather)),
                "native_cash_disagreements": len(corpus["cash_disagreements"]),
                "matched_clock_cash_disagreements": sum(row["poly_payout"] != row["kalshi_payout"] for row in test),
                "held_independent_dates": len({row["date"] for row in test})}
    cases = []
    for station in TEST:
        visible = [row for row in with_weather if row["station"] == station]
        if visible:
            strongest = max(visible, key=lambda row: abs(row["polymarket"] - row["kalshi"]))
            cases.append(_case(strongest))
    record = {"version": VERSION, "title": "Do the two-venue temperature models transfer to new airports?",
              "hypothesis": "A venue-only or weather-anchored combiner fitted on other monitors can improve calibration at SFO and Philadelphia.",
              "target": "Same station-day official maximum, and separate native cash results",
              "venues": ["Kalshi", "Polymarket US"],
              "date_window": {"four earlier fit stations": venue_warmseason_study.FIT, **TEST},
              "coverage": coverage, "metrics": metrics, "cases": cases, "missing": corpus["missing"],
              "limits": ["Both venue prices must be coherent at their shared local-standard decision. Historical transport does not establish standing execution size.",
                         "Neither test station is in the fitting set. The two held date ranges differ because Philadelphia listings start later.",
                         "Official daily maxima and actual venue cash are evaluated independently to preserve report-selection differences."],
              "fit": {"market_only": fit, "market_and_weather": blended},
              "provenance": {"frozen_input": str(frozen.relative_to(data)), "frozen_input_sha256": digest, "first_fit_input": str(FIT_INPUT),
                             "first_fit_input_sha256": corpus["fit_input_sha256"],
                             "source_sha256": research_model.run_sources(sys.modules[__name__], venue_warmseason_study, venue_benchmark)}}
    return research_model.write_record(data, record, {"held": test, "actual_cash_differences": corpus["cash_disagreements"]})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--input", type=Path)
    args = parser.parse_args(argv)
    corpus = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else gather(args.data)
    print(evaluate(args.data, corpus))


if __name__ == "__main__":
    main()
