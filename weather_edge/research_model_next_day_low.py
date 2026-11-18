"""Compare as-issued next-day low guidance and native marks before the climate day."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
import gzip
import hashlib
import json
from math import inf
from pathlib import Path
from statistics import mean
import sys

import numpy as np

from . import expansion_live_low, expansion_nbe, expansion_weather, research_model, research_model_extended_calibration as bands
from . import research_model_extended_temperatures as extended
from . import research_model_low_cross_venue_receipts as quote_archive, research_model_venue, strategy_forecast_innovation as timing, venue_archive
from .model import _outcome
from .nbm import climate_window, iso, utc

VERSION = "daily-low-before-climate-day-v1"
DATES = (date(2026, 10, 15), date(2026, 11, 14))
LATER_START = date(2026, 10, 29)
STATIONS = ("KNYC", "KMDW")
METHODS = {"native": "As-issued next-day NBE spread", "widened": "Earlier calibrated spread at the same forecast", "empirical": "Earlier empirical forecast-error distribution"}
QUOTE_METHODS = {"market": "Earlier native two-sided display midpoint", "blend": "Fixed 25% native-spread weather blend"}


def _input(path, data):
    record = json.loads(path.read_bytes())
    if record["version"] != bands.VERSION:
        raise ValueError("Expected earlier dated NBE uncertainty calibration")
    saved = gzip.decompress((data / record["provenance"]["frozen_input"]).read_bytes())
    if hashlib.sha256(saved).hexdigest() != record["provenance"]["frozen_input_sha256"]:
        raise ValueError("Original next-day calibration input digest disagrees")
    return json.loads(saved), research_model._source_entry(data, path)


def _payout_markets(data):
    grouped = defaultdict(list)
    with venue_archive.connect(data) as db:
        rows = db.execute("""SELECT venue, station, day, instrument_id, event_id, operator, lower, upper, label,
            rules_primary, rules_secondary, rules_url, resolution_source, climate_starts_at, climate_ends_at,
            opened_at, closed_at, yes_payout, settlement_at, settlement_receipt_id, expiration_value, source
            FROM markets WHERE family='daily-low' AND station IN (?,?) AND day BETWEEN ? AND ? AND status='settled'
              AND yes_payout IN ('0','1') AND settlement_receipt_id IS NOT NULL ORDER BY venue,station,day,instrument_id""",
                          (*STATIONS, *map(str, DATES)))
        for item in rows:
            market = dict(item)
            grouped[market["venue"], market["station"], market["day"]].append(market)
    return grouped


def _verify_payouts(markets, venue, station, day, actual, decision):
    if venue == venue_archive.POLY:
        event = {"buckets": [{"id": row["instrument_id"], "rules": row["rules_primary"]} for row in markets]}
        try:
            buckets = {item["id"]: (item["lower_f"], item["upper_f"])
                       for item in expansion_live_low._validated_buckets(event, day, station)}
        except (TypeError, KeyError, ValueError):
            return []
    else:
        buckets = {row["instrument_id"]: research_model_venue.integer_bucket(row) for row in markets}
    eligible = []
    for row in markets:
        if not row["opened_at"] or not row["closed_at"] or not utc(row["opened_at"]) <= decision < utc(row["closed_at"]):
            continue
        limits = buckets.get(row["instrument_id"])
        if limits is None:
            continue
        if venue == venue_archive.KALSHI:
            station_id = expansion_weather.CLI_OFFICE[station][1]
            reported = expansion_weather.parse_number(row["expiration_value"])
            rule = row["rules_primary"] or ""
            if (row["resolution_source"] != "The Weather Company" or f"({station_id})" not in rule
                    or "minimum" not in rule.lower() or reported != actual):
                continue
        bucket = {"lower_f": limits[0], "upper_f": limits[1]}
        result = int(row["yes_payout"])
        if result == _outcome(bucket, actual):
            eligible.append({**row, "bounds_f": limits, "payout_yes": result})
    return eligible


def _forecast(data, station, day, label):
    path = data / "expansion" / "cli" / station / f"{label['product']}.txt"
    if not path.is_file():
        return None
    official = expansion_weather.raw_report(data, station, day, label["product"])
    if not official or official["low_f"] is None:
        return None
    issue = datetime.combine(day - timedelta(days=1), time(12), timezone.utc)
    decision = issue + timedelta(hours=3, minutes=5)
    start, end = climate_window(day, expansion_weather.STATIONS[station][3])
    card = expansion_nbe.load_card(data, issue, station=station)
    if not card or not issue <= utc(card["available_at"]) < decision < start:
        return None
    match = next((row for row in card["forecasts"] if start <= utc(row["valid_at"]) < end
                  and utc(row["valid_at"]).hour == 12 and row.get("sd_f") is not None), None)
    if not match:
        return None
    forecast = {key: card[key] for key in ("issued_at", "available_at", "received_at", "card_sha256", "source", "printed_model_version")}
    return {"station": station, "date": str(day), "at": iso(decision), "climate_starts_at": iso(start),
            "target": "minimum", "lead_group": 1, "predicted_f": match["temp_f"], "sd_f": max(1., match["sd_f"]),
            "native_sample": match, "forecast": forecast, "official_report": official}


def _mark(db, row, at):
    trace = quote_archive._price_history(db, row["venue"], row, at - timedelta(minutes=4), at)
    value = timing.latest(*trace, at, timedelta(minutes=2))
    return value if value is not None and utc(row["opened_at"]) < utc(value["at"]) else None


def gather(data, reference):
    calibration, source = _input(reference, data)
    markets = _payout_markets(data)
    entries, omissions = [], Counter()
    with venue_archive.connect(data) as db:
        for station in STATIONS:
            daily = expansion_weather.daily_labels(data, station, *DATES)
            for day in research_model._days(*DATES):
                label = daily.get(str(day))
                view = _forecast(data, station, day, label) if label else None
                if view is None:
                    omissions["no verified post-close CLI low and pre-day complete native forecast"] += 1
                    continue
                decision = utc(view["at"])
                for venue in (venue_archive.POLY, venue_archive.KALSHI):
                    options = _verify_payouts(markets[venue, station, str(day)], venue, station, day, view["official_report"]["low_f"], decision)
                    if not options:
                        omissions[f"{venue} · no approved native payout match while open"] += 1
                    for market in options:
                        mark = _mark(db, market, decision)
                        if mark is None:
                            omissions[f"{venue} · no strictly timed coherent display mark"] += 1
                        entries.append({**view, "venue": venue, "market": market, "mark": mark, "truth": market["payout_yes"]})
    return {"version": VERSION, "earlier": calibration["rows"]["fit"], "source": source, "outcomes": entries,
            "calibration_before": calibration["selection_clock"], "omissions": dict(omissions)}


def _update(rows, prior):
    results = []
    for row in rows:
        arms = bands._arms(row, prior)
        if not arms:
            continue
        lower, upper = row["market"]["bounds_f"]
        left, right = (-inf if lower is None else lower - .5), (inf if upper is None else upper + .5)
        forecasts = {name: bands._cdf(centers, scale, right) - bands._cdf(centers, scale, left) for name, (centers, scale) in arms.items()}
        if row["mark"] is not None:
            displayed = row["mark"]["probability"]
            forecasts.update(market=displayed, blend=.75 * displayed + .25 * forecasts["native"])
        results.append({**row, "probabilities": forecasts})
    return results


def _metrics(rows, group, models):
    return [{"group": group, "method": key, "label": label, **research_model._independent_counts(rows),
             "bucket_brier": mean((row["probabilities"][key] - row["truth"]) ** 2 for row in rows)} for key, label in models.items()] if rows else []


def _paired(rows):
    buckets = defaultdict(dict)
    for row in rows:
        key = row["station"], row["date"], tuple(row["market"]["bounds_f"])
        buckets[key].setdefault(row["venue"], []).append(row)
    result = []
    for pair in buckets.values():
        if any(len(pair.get(venue, [])) != 1 for venue in (venue_archive.POLY, venue_archive.KALSHI)):
            continue
        first, second = (pair[venue][0] for venue in (venue_archive.POLY, venue_archive.KALSHI))
        if first["truth"] != second["truth"] or first["at"] != second["at"]:
            continue
        estimates = {"both_market_mean": mean(row["probabilities"]["market"] for row in (first, second)),
                     **{row["venue"]: row["probabilities"]["market"] for row in (first, second)},
                     **{key: first["probabilities"][key] for key in METHODS}}
        result.append({**first, "probabilities": estimates, "paired_market": second["market"], "paired_mark": second["mark"]})
    return result


def _interval(rows, name):
    grouped = defaultdict(list)
    for row in rows:
        raw, blend = (row["probabilities"][method] for method in ("market", "blend"))
        grouped[row["date"]].append((blend - row["truth"]) ** 2 - (raw - row["truth"]) ** 2)
    means = [mean(values) for values in grouped.values()]
    sampled = np.random.default_rng(20261115).choice(means, size=(3000, len(means))).mean(axis=1)
    return {"group": name, "method": "blend", "baseline": "market", "delta_bucket_brier": mean(means),
            "calendar_dates": len(means), "date_bootstrap_interval_95": np.quantile(sampled, (.025, .975)).tolist()}


def _case(row):
    if row.get("paired_market"):
        alternate = row["paired_market"]["instrument_id"]
        text = f"Polymarket US midpoint was {row['probabilities']['polymarket-us']:.1%}, Kalshi {row['probabilities']['kalshi']:.1%}. "
    else:
        alternate = None
        text = f"Native midpoint was {row['probabilities']['market']:.1%}. "
    return {"station": row["station"], "date": row["date"], "at": row["at"], "venue": row["venue"],
            "globe_date": extended.globe_date(row["station"], row["at"]),
            "instrument_id": row["market"]["instrument_id"], "related_instrument_id": alternate,
            "related_venue": "kalshi" if alternate else None, "title": f"{row['station']} · The market before the target climate day",
            "detail": (f"{text} Originally published extended NBM forecast {row['predicted_f']}°F with a native standard deviation of {row['sd_f']:g}°F. "
                       f"Its modeled YES probability was {row['probabilities']['native']:.1%}, the complete low was {row['official_report']['low_f']}°F, and verified YES paid ${row['truth']}."),
            "forecast_and_display": row}


def evaluate(data, inputs):
    if inputs["version"] != VERSION:
        raise ValueError("Unexpected pre-day low forecast inputs")
    frozen, digest = research_model.freeze_inputs(data, inputs)
    reserve = bands._fit(inputs["earlier"])
    scored = _update(inputs["outcomes"], reserve)
    metrics, intervals, cases, details, coverage = [], [], [], {}, {}
    groups = {"Earlier replay": [row for row in scored if date.fromisoformat(row["date"]) < LATER_START],
              "Later verification": [row for row in scored if date.fromisoformat(row["date"]) >= LATER_START]}
    for phase, rows in groups.items():
        quotes = [row for row in rows if row["mark"]]
        for venue in (venue_archive.POLY, venue_archive.KALSHI):
            marked = [row for row in quotes if row["venue"] == venue]
            watched = f"{phase} · {venue}"
            metrics.extend(_metrics([row for row in rows if row["venue"] == venue], watched + " · actual payouts", METHODS))
            metrics.extend(_metrics(marked, watched + " · coherent timed display", {**METHODS, **QUOTE_METHODS}))
            if marked:
                intervals.append(_interval(marked, watched))
        dual = _paired(quotes)
        metrics.extend(_metrics(dual, phase + " · same exact strike and payout at both venues",
                                {**METHODS, venue_archive.POLY: "Polymarket US prior display", venue_archive.KALSHI: "Kalshi prior display",
                                 "both_market_mean": "Mean of both exact display probabilities"}))
        if dual:
            cases.append(_case(max(dual, key=lambda row: abs(row["probabilities"][venue_archive.POLY] - row["probabilities"][venue_archive.KALSHI]))))
        elif quotes:
            strongest = max(quotes, key=lambda row: abs(row["probabilities"]["market"] - row["probabilities"]["native"]))
            cases.append(_case(strongest))
        coverage[phase] = {**research_model._independent_counts(rows), "coherent_displays": len(quotes), "two_venue_exact_pairs": len(dual),
                           "paired_dates": len({row["date"] for row in dual})}
        details[phase] = {"payouts": rows, "exact_both_venue": dual}
    record = {"version": VERSION, "title": "Next-day lows against both markets before the climate day begins",
              "hypothesis": "A next-day as-issued forecast provides a probability benchmark before the exact weather day. Earlier calibration or a fixed blend may improve on the initial displayed native quotes.",
              "target": "Exact settled Central Park and Midway daily-low ranges, at a fixed decision before either local climate day",
              "venues": [venue_archive.POLY, venue_archive.KALSHI], "date_window": {"verification": list(map(str, DATES)),
                 "untouched_later_start": str(LATER_START), "decision_before_target_day": "15:05 UTC", "earlier_weather_calibration_known_by": inputs["calibration_before"]},
              "metrics": metrics, "uncertainty": intervals, "cases": cases, "coverage": coverage, "missing": inputs["omissions"],
              "limits": ["Weather uses the prior UTC date's native 12Z NOAA NBE with verified original public posting. The printed forecast minimum and spread are scored against a separately retained post-close NWS climate-day minimum.",
                         "Polymarket US uses stored native display marks and Kalshi uses completed one-minute bid and ask candles. Both use unchanged 30-cent spread, one-minute publication allowance, and two-minute maximum age.",
                         "Two-venue scores require identical inclusive whole-degree bounds and actual verified identical payouts in the recorded case. Provider rules and report selection remain distinct.",
                         "The price comparisons are retrospective. They do not certify order size, original receipt time or execution. The blend fixes its 25% weather weight before the report."],
              "provenance": {"frozen_input": str(frozen.relative_to(data)), "frozen_input_sha256": digest, "earlier_model": inputs["source"],
                             "source_sha256": research_model.run_sources(sys.modules[__name__], bands, extended, expansion_nbe, quote_archive)}}
    return research_model.write_record(data, record, details)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--calibration-study", type=Path)
    source.add_argument("--input", type=Path)
    args = parser.parse_args()
    inputs = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else gather(args.data, args.calibration_study)
    print(evaluate(args.data, inputs))


if __name__ == "__main__":
    main()
