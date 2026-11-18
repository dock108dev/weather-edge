"""Try as-issued extended NBM guidance in the open Cincinnati HDD quarter."""

from __future__ import annotations

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import gzip
import json
from pathlib import Path
from statistics import mean
import sys
from urllib.request import urlopen

import numpy as np

from . import expansion_live_hdd, expansion_nbe, research_model, research_model_hdd as hdd, research_model_hdd_curve as curve
from .collector import paused
from .nbm import iso, utc

VERSION = expansion_live_hdd.STUDY_VERSION
CALENDAR = ((10, 17), (10, 31), (11, 14), (11, 28), (12, 12))
CURRENT_ISSUE = date(2026, 11, 15)
CURRENT_CHECKS = (date(2026, 10, 17), date(2026, 10, 31), date(2026, 11, 7))
FIT_YEARS = range(2020, 2024)
LATER_YEARS = range(2024, 2027)
WEEK_LABELS = {"calendar": "Earlier same-date HDD climatology", "recent": "Most recent complete seven HDD days",
               "nbe": "Raw as-issued seven-day extended NOAA forecast", "earlier_bias": "Extended NOAA plus earlier forecast error adjustment"}


def _week(card, origin):
    points = {utc(row["valid_at"]): row for row in card["forecasts"]}
    assembled = []
    for ahead in range(1, 8):
        day = origin + timedelta(days=ahead)
        noon = datetime.combine(day, time(12), timezone.utc)
        low = points.get(noon)
        high = points.get(noon + timedelta(hours=12))
        if not low or not high or high["temp_f"] < low["temp_f"]:
            return None
        average = (Decimal(high["temp_f"] + low["temp_f"]) / 2).quantize(Decimal(1), rounding=ROUND_HALF_UP)
        assembled.append({"date": str(day), "high": high, "low": low, "rounded_mean_f": int(average), "hdd_65": float(hdd._hdd(average))})
    return assembled


def _origin(data, day):
    issue = datetime.combine(day, time(12), timezone.utc)
    cutoff = issue + timedelta(hours=3)
    card = expansion_nbe.load_card(data, issue)
    if card is None or not issue <= utc(card["available_at"]) <= cutoff:
        return None
    forecast = _week(card, day)
    if not forecast:
        return None
    return {"station": hdd.STATION, "date": str(day), "year": day.year, "decision_at": iso(cutoff),
            "daily": forecast, "forecast_hdd": sum(row["hdd_65"] for row in forecast),
            "forecast_source": {key: card[key] for key in ("source", "issued_at", "available_at", "received_at", "card_sha256",
                                                             "printed_model_version", "content_range", "etag")},
            "retained_card": research_model._source_entry(data, expansion_nbe.card_path(data, issue))}


def _observed(days, first, last):
    records = [days.get(str(day)) for day in hdd._span(first, last)]
    return [row["hdd_65"] for row in records] if all(row is not None for row in records) else None


def _historic_origins():
    return [*(date(year, month, day) for year in range(2020, 2026) for month, day in CALENDAR), *CURRENT_CHECKS]


def _collect(data, cutoff):
    def safely_get(request, **parameters):
        if paused(data):
            raise OSError("Live collection stopped")
        return urlopen(request, **parameters)

    def get_day(day):
        issue = datetime.combine(day, time(12), timezone.utc)
        if issue < cutoff:
            try:
                expansion_nbe.load_card(data, issue, fetch=True, get=safely_get)
            except (OSError, ValueError, TypeError, KeyError) as error:
                return {"date": str(day), "reason": str(error)}
        return None

    with ThreadPoolExecutor(max_workers=3) as pool:
        return [error for error in pool.map(get_day, (*_historic_origins(), CURRENT_ISSUE)) if error]


def gather(data, cutoff):
    content, receipt = curve._acis(data)
    labels = curve._acis_days(content)
    years = range(1986, 2026)
    seasons = {}
    for year in years:
        values = _observed(labels, date(year, 10, 1), date(year, 12, 31))
        if values:
            seasons[str(year)] = values
    observations, missing = [], []
    for day in _historic_origins():
        captured = _origin(data, day)
        first, last = day + timedelta(days=1), day + timedelta(days=7)
        actual = _observed(labels, first, last)
        recent = _observed(labels, day - timedelta(days=7), day - timedelta(days=1))
        if captured is None or actual is None or recent is None:
            missing.append({"date": str(day), "reason": "No complete seven-day forecast/settled-label pair at the fixed cutoff"})
            continue
        older_normal = [sum(_observed(labels, first.replace(year=year), last.replace(year=year)))
                        for year in years if year < 2020 and str(year) in seasons]
        observations.append({**captured, "actual_daily_hdd": actual, "calendar": mean(older_normal), "recent": sum(recent),
                             "actual": sum(actual)})
    live = hdd._live(data, cutoff)
    forecast = _origin(data, CURRENT_ISSUE)
    if forecast is None or utc(forecast["forecast_source"]["received_at"]) > cutoff:
        raise ValueError("No real current KCVG extended bulletin received by the saved cutoff")
    return {"version": VERSION, "limited_acis_climate_seasons": seasons, "rows": observations, "live": live, "forecast": forecast,
            "as_of": iso(cutoff), "missing": missing, "climate_provenance": receipt}


def _weekly(rows, correction):
    return [{**row, "weekly_forecasts": {"calendar": row["calendar"], "recent": row["recent"],
                                        "nbe": row["forecast_hdd"], "earlier_bias": max(0, row["forecast_hdd"] + correction)}} for row in rows]


def _weekly_metrics(rows, name):
    if not rows:
        return []
    return [{"group": name, "method": method, "label": label, "forecast_origins": len(rows),
             "calendar_dates": len({row["date"] for row in rows}), "independent_seasons": len({row["year"] for row in rows}),
             "complete_hdd_days": sum(len(row["actual_daily_hdd"]) for row in rows),
             "mae_hdd": mean(abs(row["weekly_forecasts"][method] - row["actual"]) for row in rows),
             "mean_forecast_hdd": mean(row["weekly_forecasts"][method] for row in rows),
             "mean_actual_hdd": mean(row["actual"] for row in rows)} for method, label in WEEK_LABELS.items()]


def _weekly_compare(rows, group, method):
    years = defaultdict(list)
    for row in rows:
        candidate = abs(row["weekly_forecasts"][method] - row["actual"])
        normal = abs(row["weekly_forecasts"]["calendar"] - row["actual"])
        years[row["year"]].append(candidate - normal)
    values = [mean(weekly) for weekly in years.values()]
    if not values:
        return None
    intervals = np.random.default_rng(20261115).choice(values, size=(3000, len(values))).mean(axis=1)
    return {"group": group, "method": method, "baseline": "calendar", "independent_seasons": len(values),
            "mean_season_paired_mae_delta_hdd": mean(values), "season_bootstrap_interval_95": np.quantile(intervals, (.025, .975)).tolist()}


def _scenario(seasons, origin, through, known, forecast, residual, thresholds):
    through_index = (through - date(through.year, 10, 1)).days + 1
    first = (origin + timedelta(days=1) - date(origin.year, 10, 1)).days
    end = first + 7
    source = [values for year, values in sorted(seasons.items()) if int(year) < origin.year]
    if not source or not residual or not 0 <= through_index <= first < end <= len(source[0]):
        return None
    raw = [known + sum(values[through_index:]) for values in source]
    prefix_and_remaining = [known + sum(values[through_index:first]) + sum(values[end:]) for values in source]
    extended = [partial + max(0, forecast + error) for partial in prefix_and_remaining for error in residual]
    return {"historical_remainder": hdd._empirical(raw, thresholds), "extended_forecast": hdd._empirical(extended, thresholds),
            "independent_analog_seasons": len(source), "earlier_residual_origins": len(residual), "analog_through_index": through_index,
            "forecast_replaces_dates": [str(origin + timedelta(days=1)), str(origin + timedelta(days=7))],
            "forecast_hdd": forecast, "analog_same_dates_hdd": mean(sum(values[first:end]) for values in source),
            "known_hdd": known}


def _held_quarters(rows, seasons, residual, thresholds):
    results = []
    for row in rows:
        own = seasons.get(str(row["year"]))
        if own is None:
            continue
        day = date.fromisoformat(row["date"])
        through = day - timedelta(days=1)
        count = (through - date(day.year, 10, 1)).days + 1
        known = sum(own[:count])
        outcome = [int(sum(own) < strike) for strike in thresholds]
        estimates = _scenario(seasons, day, through, known, row["forecast_hdd"], residual, thresholds)
        if estimates is None:
            continue
        truth = sum(own)
        arms = {name: {**estimates[name], "mae_hdd": abs(estimates[name]["point_hdd"] - truth),
                      "brier": mean((p - answer) ** 2 for p, answer in zip(estimates[name]["probabilities"], outcome, strict=True))}
                for name in ("historical_remainder", "extended_forecast")}
        results.append({**row, "through": str(through), "quarter_outcome_hdd": truth, "estimates": arms, "scenarios": estimates})
    return results


def _live_case(inputs, result, thresholds):
    if not result:
        return []
    live, origin = inputs["live"], inputs["forecast"]
    open_markets = [row for row in live["markets"] if row["status"] == "open" and row["yes_payout"] is None]
    if not open_markets:
        return []
    chosen = min(open_markets, key=lambda row: abs(float(row["upper"]) - result["extended_forecast"]["point_hdd"]))
    index = thresholds.index(float(chosen["upper"]))
    earlier, changed = (result[name]["probabilities"][index] for name in ("historical_remainder", "extended_forecast"))
    return [{"station": hdd.STATION, "date": live["through"], "at": inputs["as_of"], "venue": "kalshi",
             "instrument_id": chosen["instrument_id"], "title": "Cincinnati · November forecast and the open heating quarter",
             "detail": (f"Official daily fields total {live['total']:.0f} HDD so far. The next seven complete forecast days sum to "
                        f"{origin['forecast_hdd']:.0f} HDD, against {result['analog_same_dates_hdd']:.0f} from earlier same dates. "
                        f"The modeled chance below {float(chosen['upper']):g} HDD changes from {earlier:.1%} to {changed:.1%}. The contract remains open."),
             "market": chosen, "weekly_forecast": origin, "scenario_comparison": result}]


def _market_attachment(inputs, result, strikes):
    if not result:
        return None
    live, source = inputs["live"], inputs["forecast"]
    bulletin = source["forecast_source"]
    scenarios = {row["instrument_id"]: {"threshold_hdd": float(row["upper"]), "operator": "below",
                                      "analog_yes_probability": result["historical_remainder"]["probabilities"][strikes.index(float(row["upper"]))],
                                      "forecast_yes_probability": result["extended_forecast"]["probabilities"][strikes.index(float(row["upper"]))]}
                 for row in live["markets"] if row["event_id"] == expansion_live_hdd.EVENT_ID and row["status"] == "open"}
    return {"schema": expansion_live_hdd.SCHEMA, "event_id": expansion_live_hdd.EVENT_ID, "station": hdd.STATION,
            "contract_period": expansion_live_hdd.PERIOD, "known_at": inputs["as_of"],
            "accrual": {"through_date": live["through"], "hdd_65": live["total"], "complete_days": len(live["received_days"]),
                        "last_source_available_at": iso(max(utc(row["available_at"]) for row in live["received_days"]))},
            "forecast": {"dates": result["forecast_replaces_dates"], "hdd_65": source["forecast_hdd"],
                         "analog_same_dates_hdd_65": result["analog_same_dates_hdd"], "daily": source["daily"],
                         "source_published_at": bulletin["available_at"],
                         **{key: bulletin[key] for key in ("issued_at", "received_at", "source", "card_sha256")}},
            "thresholds": scenarios}


def evaluate(data, inputs):
    if inputs["version"] != VERSION:
        raise ValueError("Unexpected Cincinnati extended guidance inputs")
    frozen, digest = research_model.freeze_inputs(data, inputs)
    earlier = [row for row in inputs["rows"] if row["year"] in FIT_YEARS]
    late = [row for row in inputs["rows"] if row["year"] in LATER_YEARS]
    residual = [row["actual"] - row["forecast_hdd"] for row in earlier]
    if len({row["year"] for row in earlier}) < 3 or not late:
        raise ValueError("Extended forecast evaluation needs independent earlier and later seasons")
    correction = mean(residual)
    verified = _weekly(late, correction)
    cohorts = {"Later independent weekly forecast blocks": verified,
               "2024–2025 earlier-model holdout": [row for row in verified if row["year"] < 2026],
               "2026 verified weather only": [row for row in verified if row["year"] == 2026]}
    metrics, interval = [], []
    for name, rows in cohorts.items():
        metrics.extend(_weekly_metrics(rows, name))
        if len({row["year"] for row in rows}) >= 2:
            interval.extend(_weekly_compare(rows, name, method) for method in ("nbe", "earlier_bias"))
    live = inputs["live"]
    strikes = sorted({float(row["upper"]) for row in live["markets"]})
    if not strikes:
        raise ValueError("No exact native Cincinnati HDD quarter threshold")
    quarters = _held_quarters(verified, inputs["limited_acis_climate_seasons"], residual, strikes)
    if quarters:
        for name in ("historical_remainder", "extended_forecast"):
            metrics.append({"group": "Retrospective completed quarters", "method": name, "independent_seasons": len({row["year"] for row in quarters}),
                            "forecast_origins": len(quarters), "mae_hdd": mean(row["estimates"][name]["mae_hdd"] for row in quarters),
                            "bucket_brier": mean(row["estimates"][name]["brier"] for row in quarters)})
    live_projection = None
    if len(live["received_days"]) == live["expected_days"]:
        live_projection = _scenario(inputs["limited_acis_climate_seasons"], CURRENT_ISSUE, date.fromisoformat(live["through"]),
                                    live["total"], inputs["forecast"]["forecast_hdd"], residual, strikes)
    record = {"version": VERSION, "title": "A real seven-day forecast for Cincinnati's open HDD quarter",
              "hypothesis": "An extended NOAA forecast for seven upcoming days can improve on historical daily HDD norms before projecting the rest of the native quarter.",
              "target": "KCVG 65°F heating degree days and the exact strict-below Kalshi quarter thresholds",
              "venues": ["kalshi"], "date_window": {"fit_years": [min(FIT_YEARS), max(FIT_YEARS)], "later_years": [min(LATER_YEARS), max(LATER_YEARS)],
                 "current_as_of": inputs["as_of"], "current_instrument_period": [str(hdd.FIRST_LIVE_DAY), str(hdd.LAST_LIVE_DAY)]},
              "coverage": {"earlier_fit_origins": len(earlier), "later_complete_week_origins": len(verified), "later_independent_years": len({row["year"] for row in verified}),
                           "completed_quarter_origins": len(quarters), "completed_quarter_years": len({row["year"] for row in quarters}),
                           "current_official_hdd_days": len(live["received_days"]), "open_native_strikes": sum(row["status"] == "open" for row in live["markets"])},
              "metrics": metrics, "uncertainty": interval, "cases": _live_case(inputs, live_projection, strikes), "missing": inputs["missing"],
              "market_projection": _market_attachment(inputs, live_projection, strikes),
              "limits": ["The 12Z NBE guidance had to be published by 15Z. Historical product clocks use the original public object timestamp. The live forecast additionally needs its actual retained local receipt by the study cutoff.",
                         "The forecast is a proxy made from native 18-hour high/low guidance and whole Fahrenheit half-up mean. These 18-hour intervals overlap the fixed climate day. The governing current values are the Weather Company daily HDD field.",
                         "Retrospective truth uses quality-screened exact-station ACIS HDD. Original historical label publication times and old season payouts are unavailable. Repeated forecast blocks within one quarter share the same eventual total.",
                         "Joining earlier forecast errors to past rest-of-quarter paths creates weighted scenarios, with independent support bounded by the number of prior seasons and forecast blocks. The current quarter is unresolved."],
              "provenance": {"frozen_input": str(frozen.relative_to(data)), "frozen_input_sha256": digest, "source_documents": [hdd.FILING, expansion_nbe.DOCUMENTATION, curve.ACIS_DOCS],
                             "source_sha256": research_model.run_sources(sys.modules[__name__], expansion_live_hdd, expansion_nbe, hdd, curve)}}
    return research_model.write_record(data, record, {"later_weekly_forecasts": verified, "quarter_projections": quarters,
                                                      "current_projection": live_projection, "earlier_forecast_week_bias_hdd": correction})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--as-of")
    parser.add_argument("--input", type=Path)
    parser.add_argument("--fetch", action="store_true", help="Collect only the listed KCVG extended station issues")
    args = parser.parse_args()
    if args.input and (args.as_of or args.fetch):
        parser.error("A frozen input has its own cutoff")
    cutoff = utc(args.as_of) if args.as_of else datetime.now(timezone.utc)
    if args.fetch:
        for gap in _collect(args.data, cutoff):
            print(json.dumps(gap), file=sys.stderr)
        if args.as_of is None:
            cutoff = datetime.now(timezone.utc)
    inputs = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else gather(args.data, cutoff)
    print(evaluate(args.data, inputs))


if __name__ == "__main__":
    main()
