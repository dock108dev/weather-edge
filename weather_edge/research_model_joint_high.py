"""Check same-date high-temperature payout dependence across distinct stations."""

from __future__ import annotations

import argparse
from collections import defaultdict
from itertools import product
from datetime import date
import gzip
import json
from pathlib import Path
from statistics import mean
import sys

import numpy as np

from . import expansion_weather, research_model, research_model_high, research_model_high_transfer as markets
from .nbm import climate_window, utc

VERSION = "daily-high-two-station-dependence-v1"
HOUR = 13
PAIRS = {"Newark and Trenton": ("KEWR", "KTTN"), "Austin and Dallas": ("KAUS", "KDFW"),
         "Seattle and Las Vegas": ("KSEA", "KLAS")}
STATIONS = tuple(sorted({station for names in PAIRS.values() for station in names}))
SPLITS = {"fit": (date(2026, 8, 27), date(2026, 9, 21)), "October verification": (date(2026, 10, 1), date(2026, 10, 14)),
          "Later autumn verification": markets.TEST}
METHODS = {"independent": "Independent draws with the same station marginals", "paired": "Use the same historical date at both stations",
           "half_shared": "Equal mixture of paired and independent draws"}


def _view(data, station, day):
    path = data / "expansion" / "replay" / str(day) / f"{station}.json"
    if not path.is_file():
        return None
    event = json.loads(path.read_bytes())
    _, close = climate_window(day, expansion_weather.STATIONS[station][3])
    possibilities = sorted((report for report in event["official_reports"] if report.get("high_f") is not None
                            and utc(report["published_at"]) >= close), key=lambda row: row["published_at"], reverse=True)
    verified = None
    for report in possibilities:
        original = data / "expansion" / "cli" / station / f"{report['product']}.txt"
        if original.is_file() and report.get("sha256") == research_model._digest(original):
            verified = report
            break
    if verified is None:
        return None
    observed = [row for row in event["observations"] if row.get("receipt_source") and row.get("raw")]
    forecasts = [row for row in event["forecasts"] if row.get("high_source") == "noaa-nbm-nbs-txn"]
    sampled = research_model_high._snapshot(station, day, HOUR, forecasts, observed, verified, observation_delay=None)
    if sampled and sampled["observed_max_f"] is not None and sampled["latest_observation_age_h"] <= 2:
        return {**sampled, "center_f": max(sampled["forecast"]["mean_f"], sampled["observed_max_f"]),
                "archive_source": research_model._source_entry(data, path)}
    return None


def gather(data):
    groups, missing = {}, {}
    for phase, dates in SPLITS.items():
        pairs = [(station, day) for station in STATIONS for day in research_model._days(*dates)]
        rows = [record for station, day in pairs if (record := _view(data, station, day))]
        if phase == "fit":
            results, audit, gaps = {}, [], {}
        else:
            results, audit, gaps = markets._market_rows(data, rows, stations=STATIONS, test=dates)
        groups[phase] = {"weather": rows, "native": results, "resolution_audit": audit}
        missing[phase] = {"station_days_lacking_as_issued_forecast_and_original_report": len(pairs) - len(rows), "native_match": gaps}
    return {"version": VERSION, "groups": groups, "gaps": missing}


def _pair_dates(rows, stations):
    values = defaultdict(dict)
    for row in rows:
        if row["station"] in stations:
            values[row["date"]][row["station"]] = row
    return [tuple(group[station] for station in stations) for _, group in sorted(values.items()) if all(station in group for station in stations)]


def _band(draws, bucket):
    lo, hi = bucket["lower_f"], bucket["upper_f"]
    return (np.ones(len(draws), dtype=bool) if lo is None else draws >= lo) & (True if hi is None else draws <= hi)


def _probabilities(first, second):
    p_a, p_b = (float(flags.mean()) for flags in (first, second))
    both = float((first & second).mean())
    independent = p_a * p_b

    def cells(joint):
        return [max(0.0, 1 - p_a - p_b + joint), p_b - joint, p_a - joint, joint]

    return {"independent": cells(independent), "paired": cells(both), "half_shared": cells((both + independent) / 2)}


def _native_for(view, source):
    return [strike for strike in source.get(f"{view['station']}/{view['date']}/{HOUR}", [])
            if strike["venue_high_f"] == view["actual_f"] and strike["market"]["settlement_receipt_id"]]


def _score(pairs, training, source):
    first_error, second_error = (np.array([pair[index]["actual_f"] - pair[index]["center_f"] for pair in training]) for index in (0, 1))
    result = []
    for a, b in pairs:
        if utc(a["at"]) != utc(b["at"]):
            continue
        values_a = np.floor(a["center_f"] + first_error + .5)
        values_b = np.floor(b["center_f"] + second_error + .5)
        native_a, native_b = (_native_for(row, source) for row in (a, b))
        for strike_a, strike_b in product(native_a, native_b):
            odds = _probabilities(_band(values_a, strike_a["bucket"]), _band(values_b, strike_b["bucket"]))
            state = 2 * strike_a["native_yes"] + strike_b["native_yes"]
            result.append({"date": a["date"], "at": a["at"], "station": a["station"], "related_station": b["station"],
                           "native_a": strike_a, "native_b": strike_b, "a": a, "b": b, "probabilities": odds, "observed_state": state})
    return result


def _loss(row, method, event):
    odds = row["probabilities"][method]
    truth = row["observed_state"]
    if event == "four_state_brier":
        return mean((probability - (truth == cell)) ** 2 for cell, probability in enumerate(odds))
    return (odds[0] - (truth == 0)) ** 2


def _metrics(rows, group, pair):
    return [{"group": group, "pair": pair, "method": method, "label": title, "matched_threshold_pairs": len(rows),
             "calendar_dates": len({row["date"] for row in rows}), "paired_station_days": len({row["date"] for row in rows}),
             "station_days": len({(station, row["date"]) for row in rows for station in (row["station"], row["related_station"])}),
             **{metric: mean(_loss(row, method, metric) for row in rows) for metric in ("four_state_brier", "both_no_brier")}}
            for method, title in METHODS.items()] if rows else []


def _compare(rows, group, pair, method):
    dates = defaultdict(list)
    for row in rows:
        dates[row["date"]].append(_loss(row, method, "four_state_brier") - _loss(row, "independent", "four_state_brier"))
    certainty = [mean(data) for data in dates.values()]
    sampled = np.random.default_rng(20261115).choice(certainty, size=(3000, len(certainty))).mean(axis=1)
    return {"group": group, "pair": pair, "method": method, "baseline": "independent", "calendar_dates": len(dates),
            "delta_four_state_brier": mean(certainty), "date_bootstrap_interval_95": np.quantile(sampled, (.025, .975)).tolist()}


def _case(row, pair):
    a, b = row["native_a"], row["native_b"]
    outcome_a, outcome_b = a["native_yes"], b["native_yes"]
    independent, shared = (row["probabilities"][method][0] for method in ("independent", "paired"))
    return {"station": row["station"], "date": row["date"], "at": row["at"], "venue": "kalshi", "related_venue": "kalshi",
            "instrument_id": a["market"]["instrument_id"], "related_instrument_id": b["market"]["instrument_id"],
            "title": f"{pair} · Same-day threshold exposure", "related_station": row["related_station"],
            "detail": (f"Independent earlier residuals give {independent:.1%} to both NO. Using the paired historical dates gives {shared:.1%} "
                       f"with identical individual probabilities. The two native YES sides paid ${outcome_a} and ${outcome_b}."),
            "evidence": row}


def evaluate(data, inputs):
    if inputs["version"] != VERSION:
        raise ValueError("Unexpected paired daily-high weather input")
    frozen, digest = research_model.freeze_inputs(data, inputs)
    fit = inputs["groups"]["fit"]["weather"]
    metrics, intervals, cases, details, coverage = [], [], [], {}, {}
    for name, stations in PAIRS.items():
        earliest = min(utc(row["at"]) for row in inputs["groups"]["October verification"]["weather"] if row["station"] in stations)
        anchor = _pair_dates([row for row in fit if utc(row["official_report"]["published_at"]) < earliest], stations)
        if len(anchor) < 12:
            raise ValueError(f"Too few earlier shared dates for {name}")
        errors = np.array([[row["actual_f"] - row["center_f"] for row in pair] for pair in anchor])
        coverage[name] = {"earlier_joint_dates": len(anchor), "earlier_residual_correlation": float(np.corrcoef(errors.T)[0, 1])}
        details[name] = {"earlier_weather_pairs": anchor}
        for phase in tuple(SPLITS)[1:]:
            source = inputs["groups"][phase]
            weather = _pair_dates(source["weather"], stations)
            matched = _score(weather, anchor, source["native"])
            coverage[name][phase] = {"exact_weather_pairs": len(weather), "native_threshold_pairs": len(matched),
                                    "complete_native_pair_dates": len({row["date"] for row in matched})}
            metrics.extend(_metrics(matched, phase, name))
            if matched:
                intervals.extend(_compare(matched, phase, name, method) for method in ("paired", "half_shared"))
                chosen = max(matched, key=lambda row: abs(row["probabilities"]["paired"][0] - row["probabilities"]["independent"][0]))
                cases.append(_case(chosen, name))
            details[name][phase] = matched
    record = {"version": VERSION, "title": "Do daily-high markets at nearby stations share forecast risk?",
              "hypothesis": "Earlier observed forecast errors on the same calendar day can capture dependence between neighboring stations without changing either marginal forecast.",
              "target": "Four-state and both-NO probability for paired exact native Kalshi daily-high payouts observed on the same date",
              "venues": ["kalshi"], "date_window": {key: list(map(str, value)) for key, value in SPLITS.items()}, "coverage": coverage,
              "metrics": metrics, "uncertainty": intervals, "cases": cases, "missing": inputs["gaps"],
              "limits": ["The 13 LST station center uses the greater of the originally published daily NOAA guidance and originally received observed high so far. Both arms use the same empirical marginal errors from the same earlier dates.",
                         "The joined time-zone pairs share a UTC decision clock. Each raw CLI must agree with its separately verified native source payout before the pair enters scoring.",
                         "Nearby stations share weather and the pairs repeat native thresholds on each date. The study resamples entire climate dates and counts observation groups independently of repeated strikes.",
                         "The estimates describe correlated weather outcomes. They do not assume concurrent native order depth or imply an approved portfolio policy."],
              "provenance": {"frozen_input": str(frozen.relative_to(data)), "frozen_input_sha256": digest,
                             "source_sha256": research_model.run_sources(sys.modules[__name__], markets, research_model_high, expansion_weather)}}
    return research_model.write_record(data, record, details)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--input", type=Path)
    args = parser.parse_args()
    inputs = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else gather(args.data)
    print(evaluate(args.data, inputs))


if __name__ == "__main__":
    main()
