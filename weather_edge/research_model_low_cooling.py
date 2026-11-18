"""Test whether past cooling relative to earlier guidance predicts a colder late daily low."""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
import gzip
import json
from pathlib import Path
from statistics import mean
import sys

from sklearn import __version__ as sklearn_version
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from . import expansion_metar, expansion_replay, expansion_weather, history, research_model
from . import research_model_high_transfer as transfer, research_model_high_warm, research_model_low_cross_season as cross
from . import research_model_low_full_day as full_day, research_model_low_portability as portable, research_model_low_precision as precision
from . import research_model_low_winter
from .model import _outcome
from .nbm import climate_window, utc

VERSION = "daily-low-recent-cooling-anomaly-v1"
HOURS = (17, 21)
WINDOWS = {"winter fit": (date(2026, 2, 8), date(2026, 2, 21)), "warm fit": (date(2026, 8, 27), date(2026, 9, 8)),
           "early verification": (date(2026, 9, 9), date(2026, 9, 21)), "October verification": (date(2026, 10, 1), date(2026, 10, 14)),
           "autumn diagnostic": transfer.TEST}
METHODS = {"txn": "Earlier calibrated NOAA carried TXN", "guard": "Fixed station reading and remaining forecast guard",
           "ridge": "Earlier-fitted bias shift with the same station bound",
           "cooling": "Same fitted bias shift plus observed cooling anomaly"}


def _original_record(row, station, start, at):
    evidence = expansion_metar.metar_low_evidence(row, station, start, at, include_six_hour=False)
    if not evidence:
        return None
    return {**evidence[0], "measured_f": 32 + evidence[0]["reported_c"] * 9 / 5,
            "receipt_source": row["receipt_source"]}


def _cooling(observations, station, start, at, forecast_cards):
    known = expansion_weather.observations_as_of(observations, start, at, delay_minutes=None)
    temperatures = [read for row in known if (read := _original_record(row, station, start, at))]
    if not temperatures:
        return None
    last = max(temperatures, key=lambda row: row["report_at"])
    last_time = utc(last["report_at"])
    if at - last_time > timedelta(minutes=75):
        return None
    ideal = last_time - timedelta(hours=3)
    first = min(temperatures, key=lambda row: abs(utc(row["report_at"]) - ideal))
    first_time = utc(first["report_at"])
    if abs(first_time - ideal) > timedelta(minutes=45):
        return None
    for card in sorted(forecast_cards, key=lambda row: row["issued_at"], reverse=True):
        issued, published = (utc(card[key]) for key in ("issued_at", "available_at"))
        if not issued <= published <= first_time or first_time - issued > timedelta(hours=36):
            continue
        knots = [(utc(point["valid_at"]), point["TMP"]) for point in card["points"] if "TMP" in point]
        expected_first = full_day._interp(knots, first_time)
        expected_last = full_day._interp(knots, last_time)
        if expected_first is None or expected_last is None:
            continue
        scale = timedelta(hours=3) / (last_time - first_time)
        measured = (last["measured_f"] - first["measured_f"]) * scale
        expected = (expected_last - expected_first) * scale
        return {"first": first, "last": last, "observed_change_f_per_three_hours": measured,
                "forecast_change_f_per_three_hours": expected, "residual_change_f_per_three_hours": measured - expected,
                "forecast_expected_first_f": expected_first, "forecast_expected_last_f": expected_last,
                "forecast": {key: card[key] for key in ("source", "issued_at", "available_at", "card_sha256")}}
    return None


def _materialize(data, station, day, bundle, label):
    """Add a new exact replay when a research day is absent from the user archive."""
    projected = data / "expansion" / "replay" / str(day) / f"{station}.json"
    if projected.is_file():
        return projected
    event = expansion_replay._event(data, station, day, bundle, False, True, retained_reports=[label])
    expansion_replay._write_new(data / "archive" / str(day) / f"{station}.json", event)
    expansion_replay._write_new(projected, event)
    return projected


def _station(data, station, first, last, phase):
    bundle = expansion_replay._station_data(data, station, first, last + timedelta(days=1), madis_only=True)
    labels, _, issued = bundle
    issues, evidence = Counter(), []
    for day in research_model._days(first, last):
        expected = labels.get(str(day))
        if not expected:
            issues["no archived station climate label"] += 1
            continue
        cli = data / "expansion" / "cli" / station / f"{expected['product']}.txt"
        if not cli.is_file():
            issues["no retained raw complete CLI product"] += 1
            continue
        label = precision._scoped_labels(data, station, day, expected)
        if not label or label["low_f"] is None:
            issues["retained climate minimum incomplete"] += 1
            continue
        archive = data / "expansion" / "replay" / str(day) / f"{station}.json"
        if phase == "October verification" and not archive.is_file():
            archive = _materialize(data, station, day, bundle, label)
        if not archive.is_file():
            issues["no originally timed exact station reports"] += 1
            continue
        event = json.loads(archive.read_bytes())
        observations = [row for row in event["observations"] if row.get("receipt_source") and row.get("raw")]
        near = [card for issue, card in issued.items() if day - timedelta(days=1) <= issue.date() <= day + timedelta(days=1)]
        opens, _ = climate_window(day, expansion_weather.STATIONS[station][3])
        views = {row["hour"]: row for row in portable._views(station, day, near, observations, label, delay_minutes=None)}
        for hour in HOURS:
            row = views.get(hour)
            cooling = _cooling(observations, station, opens, opens + timedelta(hours=hour), near)
            if row and row["metar_upper_bound"] and cooling:
                evidence.append({**row, "cooling": cooling, "replay_source": research_model._source_entry(data, archive)})
            else:
                issues["exact current and earlier readings, original forecast or bound unavailable"] += 1
    return evidence, dict(issues)


def gather(data, reference):
    core = json.loads(gzip.decompress(reference.read_bytes()))
    if core["version"] != precision.VERSION:
        raise ValueError("Expected earlier daily-low temperature-band calibration")
    groups, gaps = {}, {}
    for phase, dates in WINDOWS.items():
        stations = research_model_low_winter.STATIONS if phase == "winter fit" else transfer.STATIONS
        rows, missing = [], {}
        with ThreadPoolExecutor(max_workers=3) as pool:
            tasks = {station: pool.submit(_station, data, station, *dates, phase) for station in stations}
            for station, job in tasks.items():
                matched, missing[station] = job.result()
                rows.extend(matched)
        gaps[phase] = missing
        payouts, audit, excluded = ({}, [], []) if phase in ("winter fit", "warm fit") else transfer._market_rows(data, rows, test=dates, family="daily-low")
        priced = research_model_high_warm.strict_candles(data, rows, payouts, hours=HOURS)
        groups[phase] = {"weather": rows, "payouts": payouts, "strict_quotes": priced, "native_source_audit": audit,
                         "native_gaps": excluded}
    return {"version": VERSION, "groups": groups, "gaps": gaps, "earlier_bands": core["rows"]["calibration"],
            "calibration_source": research_model._source_entry(data, reference)}


def _features(row, add_cooling):
    means, upper = row["means"], row["metar_upper_bound"]["upper_f"]
    reading = row["cooling"]["last"]["measured_f"]
    features = [row["hour"], means["remaining"] - means["txn"], upper - means["remaining"], reading - upper]
    if add_cooling:
        features.append(row["cooling"]["residual_change_f_per_three_hours"])
    return features


def _fit(rows, add_cooling):
    model = make_pipeline(StandardScaler(), Ridge(alpha=2))
    model.fit([_features(row, add_cooling) for row in rows], [row["actual_f"] - precision._precision_mean(row) for row in rows])
    return model


def _score(rows, widths, reserves, plain, frontier):
    if not rows:
        return []
    chosen = {"ridge": plain.predict([_features(row, False) for row in rows]),
              "cooling": frontier.predict([_features(row, True) for row in rows])}
    output, anchors = [], {}
    for index, row in enumerate(rows):
        key = row["station"], row["hour"]
        upper = row["metar_upper_bound"]["upper_f"]
        starting = precision._precision_mean(row)
        config = {"txn": (row["means"]["txn"], widths["txn", *key], None, 0),
                  "guard": (starting, widths["precision_center", *key], upper, reserves[key]["uncapped_weight"])}
        config.update({name: (min(upper, starting + float(values[index])), *config["guard"][1:]) for name, values in chosen.items()})
        target = row["actual_f"]
        physical = anchors.setdefault((row["station"], row["date"]), research_model._benchmark_buckets(round(row["means"]["txn"])))
        actuals = [_outcome(bucket, target) for bucket in physical]
        estimates = {}
        for method, (center, width, bound, reserve) in config.items():
            odds = precision._bucket_probabilities(physical, center, width, bound, reserve)
            estimates[method] = {"mean_f": center, "error_f": center - target, "probabilities": odds,
                                 "brier": mean((p - y) ** 2 for p, y in zip(odds, actuals, strict=True))}
        output.append({**row, "estimates": estimates, "config": config, "buckets": physical, "truth": actuals})
    return output


def _payouts(rows, rules):
    output = []
    for row in rows:
        key = f"{row['station']}/{row['date']}/{row['hour']}"
        for outcome in rules.get(key, []):
            probabilities = {method: precision._bucket_probabilities([outcome["bucket"]], *settings)[0]
                             for method, settings in row["config"].items()}
            if price := outcome["display_price"]:
                probabilities["market"] = price["midpoint"]
            output.append({**row, **outcome, "probabilities": probabilities})
    return output


def _payout_metrics(rows, group, *, market=False):
    if not rows:
        return []
    labels = {**METHODS, **({"market": "Exact coherent native midpoint"} if market else {})}
    return [{"group": group, "method": method, "label": label, **research_model._independent_counts(rows),
             "bucket_brier": mean((row["probabilities"][method] - row["native_yes"]) ** 2 for row in rows)}
            for method, label in labels.items()]


def _case(row, phase):
    cooling, estimates = row["cooling"], row["estimates"]
    return {"station": row["station"], "date": row["date"], "at": row["at"], "venue": "kalshi",
            "instrument_id": row["market"]["instrument_id"], "title": f"{row['station']} · Exact station cooling before the daily low settled",
            "detail": (f"Observed three-hour change {cooling['observed_change_f_per_three_hours']:+.1f}°F and earlier forecast "
                       f"{cooling['forecast_change_f_per_three_hours']:+.1f}°F. Estimated full-day lows "
                       f"{estimates['ridge']['mean_f']:.1f}°F before and {estimates['cooling']['mean_f']:.1f}°F with the feature. "
                       f"The complete climate report gave {row['actual_f']}°F and native YES paid ${row['native_yes']}."),
            "cohort": phase, "station_and_market": row}


def evaluate(data, inputs):
    if inputs["version"] != VERSION:
        raise ValueError("Unexpected cooling-anomaly corpus")
    frozen, digest = research_model.freeze_inputs(data, inputs)
    widths, reserves = cross._fit_shared(inputs["earlier_bands"])
    stages = tuple(WINDOWS)
    cutoff = min(utc(row["at"]) for row in inputs["groups"]["early verification"]["weather"])
    training = [row for phase in stages[:2] for row in inputs["groups"][phase]["weather"] if utc(row["official_report"]["published_at"]) < cutoff]
    plain, with_cooling = _fit(training, False), _fit(training, True)
    metrics, comparisons, cases, details = [], [], [], {}
    for phase in stages[2:]:
        evidence = inputs["groups"][phase]
        original = [row for row in evidence["weather"] if (row["station"], row["hour"]) in reserves]
        weather = _score(original, widths, reserves, plain, with_cooling)
        native = _payouts(weather, evidence["payouts"])
        quoted = _payouts(weather, evidence["strict_quotes"])
        metrics.extend(research_model.metrics(weather, phase, METHODS))
        rapid = [row for row in weather if row["cooling"]["residual_change_f_per_three_hours"] <= -4]
        metrics.extend(research_model.metrics(rapid, phase + " · unexpected cooling of 4°F or more", METHODS))
        metrics.extend(_payout_metrics(native, phase + " · exact verified native thresholds"))
        metrics.extend(_payout_metrics(quoted, phase + " · same-clock strict native display", market=True))
        if weather:
            comparisons.extend({"group": phase, **research_model.date_bootstrap(weather, "ridge", "cooling", measure)}
                               for measure in ("brier", "mae"))
        winners = [row for row in native if row["native_yes"] and row["cooling"]["residual_change_f_per_three_hours"] <= -4]
        if winners:
            example = max(winners, key=lambda row: row["estimates"]["ridge"]["brier"] - row["estimates"]["cooling"]["brier"])
            cases.append(_case(example, phase))
        details[phase] = {"weather": weather, "payouts": native, "strict_quotes": quoted}
    after = with_cooling.named_steps["ridge"].coef_[-1] / with_cooling.named_steps["standardscaler"].scale_[-1]
    record = {"version": VERSION, "title": "Can unexpectedly fast station cooling improve the late daily-low estimate?",
              "hypothesis": "Cooling faster than an earlier visible forecast may reveal colder conditions that the fixed station and remaining forecast guard misses near the end of the climate day.",
              "target": "Complete exact CLI lows and independently verified native Kalshi daily-low payout thresholds at 17 and 21 LST",
              "venues": ["kalshi"], "date_window": {key: list(map(str, dates)) for key, dates in WINDOWS.items()},
              "coverage": {"training": research_model._independent_counts(training),
                           **{phase: {**research_model._independent_counts(items["weather"]), "payouts": len(items["payouts"]),
                                      "strict_native_displays": len(items["strict_quotes"])} for phase, items in details.items()}},
              "recent_cooling_coefficient_f_per_f": float(after), "metrics": metrics, "uncertainty": comparisons, "cases": cases,
              "limits": ["The exact weather slope uses two raw station temperatures and original NOAA MADIS clocks. The same NOAA weather card used for both interpolated slope edges must have been published before the first measurement.",
                         "The older calendar groups train the two Ridge models. They share the same features apart from the rate of cooling beyond the earlier forecast. Both use the same fitted width, temperature bound and reserve.",
                         "The October 1–14 group was newly assembled for this comparison. The late autumn sample is a follow-up diagnostic motivated by earlier extreme daily-low misses.",
                         "The market comparison uses exact complete 17/21 local-standard candles and a five-minute allowance. Historical native prices and verified payout evidence remain separate from execution."],
              "missing": inputs["gaps"],
              "provenance": {"frozen_input": str(frozen.relative_to(data)), "frozen_input_sha256": digest,
                             "earlier_band_calibration": inputs["calibration_source"], "sklearn": sklearn_version,
                             "source_sha256": research_model.run_sources(sys.modules[__name__], full_day, expansion_replay, history,
                                                                           expansion_metar, research_model_high_warm, transfer)}}
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
