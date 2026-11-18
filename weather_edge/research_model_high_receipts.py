"""Check the transferred daily-high forecast using originally timed station receipts."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, timedelta
import gzip
import json
from pathlib import Path
from statistics import mean
import sys

from . import research_model, research_model_high, research_model_high_transfer as transfer, research_model_high_warm
from .nbm import utc

VERSION = "daily-high-native-receipts-transfer-v1"
METHODS = {"noaa": "Raw as-issued NOAA", "proxy_ridge": "Ridge with ten-minute timing",
           "receipt_ridge": "Ridge with original receipts", "proxy_nonlinear": "Nonlinear with ten-minute timing",
           "receipt_nonlinear": "Nonlinear with original receipts"}


def _event_key(row):
    return row["station"], row["date"], row["at"]


def _recorded(data, rows):
    events = {}
    references = {}
    originals, planned = [], []
    omissions = Counter()
    for row in rows:
        station, stamp, _ = _event_key(row)
        clock = row["hour"]
        if clock < 0:
            continue
        key = station, stamp
        if key not in events:
            path = data / "expansion" / "replay" / stamp / f"{station}.json"
            event = json.loads(path.read_bytes()) if path.is_file() else None
            events[key] = [reading for reading in event["observations"] if reading.get("receipt_source") and reading.get("raw")] if event else []
            if event:
                references["/".join(key)] = research_model._source_entry(data, path)
        observed = events[key]
        if not observed:
            omissions["no originally timed station archive"] += 1
            continue
        merged = research_model_high._snapshot(station, date.fromisoformat(stamp), clock, [row["forecast"]], observed,
                                                row["official_report"], observation_delay=None)
        if not merged or merged["forecast"]["card_sha256"] != row["forecast"]["card_sha256"]:
            omissions["no comparable originally issued NBS card"] += 1
            continue
        if merged["observed_max_f"] is None or row["observed_max_f"] is None:
            omissions["no before-decision station observations in both arms"] += 1
            continue
        if utc(merged["latest_observation"]["available_at"]) > utc(row["at"]):
            raise ValueError("Station receipt occurred after the daily-high decision")
        originals.append(merged)
        planned.append(row)
    return originals, planned, references, dict(omissions)


def gather(data, original):
    content = json.loads(gzip.decompress(original.read_bytes()))
    if content["version"] != research_model_high_warm.VERSION:
        raise ValueError("Expected frozen high-transfer cross-season data")
    cohorts = {}
    for group, source in content["cohorts"].items():
        receipts, proxy, cites, gaps = _recorded(data, source["rows"]["test"])
        cohorts[group] = {"receipts": receipts, "proxy": proxy, "sources": cites, "gaps": gaps,
                          "markets": source["markets"], "strict_midday": source.get("strict_midday", {}),
                          "adapt": source["rows"]["adapt"]}
    return {"version": VERSION, "fit": content["baseline_rows"], "cohorts": cohorts,
            "cross_season_reference": research_model._source_entry(data, original)}


def _same_cases(inputs, source):
    def score(rows):
        response, _ = transfer._weather_probabilities({"baseline_rows": inputs["fit"], "rows": {"test": rows, "adapt": source["adapt"]}})
        return response

    approximate, recorded = map(score, (source["proxy"], source["receipts"]))
    results = []
    for pre, raw in zip(approximate, recorded, strict=True):
        if _event_key(pre) != _event_key(raw) or pre["buckets"] != raw["buckets"] or pre["truth"] != raw["truth"]:
            raise ValueError("Receipt replay does not use identical labels and thresholds")
        estimates = {"noaa": pre["estimates"]["noaa"]}
        for name, view in (("proxy", pre), ("receipt", raw)):
            estimates.update({f"{name}_{method}": view["estimates"][method] for method in ("ridge", "nonlinear")})
        results.append({**raw, "estimates": estimates, "lag_assumed_latest": pre["latest_observation"],
                        "lag_assumed_max_f": pre["observed_max_f"]})
    return results


def _evaluate_native(rows, source, group):
    points = transfer._market_probabilities(rows, source["markets"])
    selected = [row for row in points if row["display_price"] is not None]
    strict = transfer._market_probabilities(rows, source.get("strict_midday", {}))
    priced = {**METHODS, "market": "Earlier completed Kalshi midpoint"}
    metrics = []
    for label, paired, methods in (("native binary payouts", points, METHODS),
                                 ("same payouts and price evidence", selected, priced),
                                 ("13:05 LST exact candle with spread ≤30¢", strict, priced)):
        if paired:
            snapshots = {(row["station"], row["date"], row["at"]) for row in paired}
            counts = research_model._independent_counts(paired)
            counts["snapshots"] = len(snapshots)
            metrics.extend({"method": method, "label": name, "group": f"{group} · {label}", **counts, "bucket_cases": len(paired),
                            "bucket_brier": mean((row["probabilities"][method] - row["native_yes"]) ** 2 for row in paired)}
                           for method, name in methods.items())
    return points, metrics


def _case(row, points):
    chosen = next((point for point in points if _event_key(point) == _event_key(row) and point["native_yes"] == 1), None)
    observed = row["latest_observation"]
    simulated = row["lag_assumed_latest"]
    actual, approximate = (row["estimates"][key]["mean_f"] for key in ("receipt_nonlinear", "proxy_nonlinear"))
    return {"station": row["station"], "date": row["date"], "at": row["at"],
            **({"venue": "kalshi", "instrument_id": chosen["market"]["instrument_id"], "market": chosen} if chosen else {}),
            "title": f"{row['station']} · Original observation clock versus assumed delay",
            "detail": (f"The originally timed latest report was measured at {observed['at']} and entered MADIS at "
                       f"{observed['available_at']}. The ten-minute arm instead used the {simulated['at']} measurement. "
                       f"The two nonlinear estimates were {actual:.1f}°F and {approximate:.1f}°F, with a complete CLI high of {row['actual_f']}°F."),
            "weather": row}


def evaluate(data, inputs):
    path, digest = research_model.freeze_inputs(data, inputs)
    metrics, comparisons, coverage, cases, details = [], [], {}, [], {}
    for group, source in inputs["cohorts"].items():
        rows = _same_cases(inputs, source)
        if not rows:
            continue
        payouts, native_metrics = _evaluate_native(rows, source, group)
        metrics.extend(native_metrics)
        coverage[group] = {**research_model._independent_counts(rows),
                           "latest_measurement_changed": sum(row["latest_observation"]["at"] != row["lag_assumed_latest"]["at"] for row in rows),
                           "known_peak_changed": sum(row["observed_max_f"] != row["lag_assumed_max_f"] for row in rows),
                           "native_bucket_cases": len(payouts)}
        clock_groups = {f"{group} · all available clocks": rows}
        clock_groups[f"{group} · both reports within two hours"] = [
            row for row in rows if all(utc(row["at"]) - utc(observation["at"]) <= timedelta(hours=2)
                                       for observation in (row["latest_observation"], row["lag_assumed_latest"]))]
        for hour in (3, 9, 13, 17, 21):
            clock_groups[f"{group} · {hour:02}:00 LST"] = [row for row in rows if row["hour"] == hour]
        for phase, compared in clock_groups.items():
            metrics.extend(research_model.metrics(compared, phase, METHODS))
            if compared:
                for baseline, method in (("proxy_ridge", "receipt_ridge"), ("proxy_nonlinear", "receipt_nonlinear"),
                                         ("noaa", "receipt_nonlinear")):
                    comparisons.append({"group": phase, **research_model.date_bootstrap(compared, baseline, method, "brier")})
        ordered = sorted(rows, key=lambda row: row["estimates"]["proxy_nonlinear"]["brier"] -
                         row["estimates"]["receipt_nonlinear"]["brier"])
        cases.extend(_case(row, payouts) for row in (ordered[0], ordered[-1]))
        details[group] = {"weather": rows, "payouts": payouts, "gaps": source["gaps"]}
    record = {"version": VERSION, "title": "Daily-high transfer on genuinely timed archive reports",
              "hypothesis": "Replacing the ten-minute observation assumption with the exact earlier NOAA MADIS receipt can change which report the model saw and the resulting probability score.",
              "target": "Verified daily highs and exact Kalshi temperature buckets at nine independent stations",
              "venues": ["kalshi"], "date_window": {"late_summer": list(map(str, research_model_high_warm.TEST)),
                                                     "autumn": list(map(str, transfer.TEST))},
              "coverage": coverage, "metrics": metrics, "contrasts": comparisons, "cases": cases,
              "limits": ["The same earlier temperature model and calibration are used by both arms. Eligible forecast bulletins, station-days, thresholds and verified labels are identical.",
                         "The timing change includes the absence of ASOS reports that could not be matched to an original ingress receipt. Their weather measurements remain in the archive with a disclosed proxy.",
                         "MADIS received time is the archived provider clock. No original local computer download has been inferred from it.",
                         "Venue display price and payout comparisons provide forecast scores and no depth for historical execution."],
              "provenance": {"frozen_input": str(path.relative_to(data)), "frozen_input_sha256": digest,
                             "cross_season_input": inputs["cross_season_reference"],
                             "source_sha256": research_model.run_sources(sys.modules[__name__], research_model_high, transfer)}}
    return research_model.write_record(data, record, details)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    options = parser.add_mutually_exclusive_group(required=True)
    options.add_argument("--baseline", type=Path)
    options.add_argument("--input", type=Path)
    args = parser.parse_args()
    inputs = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else gather(args.data, args.baseline)
    print(evaluate(args.data, inputs))


if __name__ == "__main__":
    main()
