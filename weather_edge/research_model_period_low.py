"""Replay four-day minima and the actual Northeast Kalshi contract window."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import gzip
import json
from math import prod
from pathlib import Path
import re
from statistics import NormalDist, mean
import sys
from zoneinfo import ZoneInfo

import numpy as np

from . import expansion_nbm, expansion_weather, research_model, strategy_twc, venue_archive
from .model import _limits, _outcome
from .nbm import climate_window, iso, utc

VERSION = "northeast-four-day-low-v1"
SERIES = "KXLOWTPERIOD"
STATIONS = ("KNYC", "KBOS", "KPHL", "KEWR", "KPVD")
WINDOWS = {"calibration": (date(2026, 6, 5), date(2026, 9, 26)), "test": (date(2026, 10, 1), date(2026, 11, 14))}
TRADE_DAYS = (date(2026, 11, 12), date(2026, 11, 15))
LOCAL = ZoneInfo("America/New_York")
HOUR_LST = 9
METHODS = {"independent_normal": "Independent NOAA distributions", "independent_empirical": "Earlier residuals, independent days",
           "paired_empirical": "Earlier residuals, paired days"}


def _four_days(start):
    return [start + timedelta(days=i) for i in range(4)]


def _native(data):
    with venue_archive.connect(data) as db:
        rows = db.execute("""SELECT instrument_id,event_id,station,title,label,source,rules_primary,rules_secondary,rules_url,
            status,operator,upper,closed_at,last_received_at,yes_payout,settlement_at,expiration_value,settlement_receipt_id
            FROM markets WHERE venue=? AND series_id=? ORDER BY station,instrument_id""", ("kalshi", SERIES))
        return [dict(row) for row in rows]


def _low_forecast(data, station, day, decision):
    return research_model.available_forecast(research_model.day_forecasts(data, station, day), decision)


def _historical(data):
    cohorts = {key: [] for key in WINDOWS}
    absent = Counter()
    for station in STATIONS:
        labels = expansion_weather.daily_labels(data, station, WINDOWS["calibration"][0], WINDOWS["test"][1])
        for split, (begin, end) in WINDOWS.items():
            for start in research_model._days(begin, end - timedelta(days=3)):
                days = _four_days(start)
                reports = [labels.get(str(day)) for day in days]
                at = climate_window(days[2], -5)[0] + timedelta(hours=HOUR_LST)
                if not all(report and report["low_f"] is not None for report in reports):
                    absent[f"{split}: incomplete four physical labels"] += 1
                    continue
                if any(utc(report["published_at"]) > at for report in reports[:2]):
                    absent[f"{split}: a prior-day report version was published after decision"] += 1
                    continue
                futures = [_low_forecast(data, station, day, at) for day in days[2:]]
                if not all(futures):
                    absent[f"{split}: no eligible pair of NOAA daily minima"] += 1
                    continue
                week, day_within_week = divmod((start - WINDOWS["test"][0]).days, 7)
                cohorts[split].append({"station": station, "date": str(start), "end": str(days[-1]), "at": iso(at),
                                       "reports": reports, "future_forecasts": futures,
                                       "known_minimum_f": min(report["low_f"] for report in reports[:2]),
                                       "actual_f": min(report["low_f"] for report in reports), "week": week,
                                       "within_week": day_within_week <= 3})
    return cohorts, dict(absent)


def _case_identity(market):
    station = market.get("station")
    rule = market["rules_primary"]
    match = re.search(r"\((K[A-Z]{3})\) for ([A-Z][a-z]{2}) (\d{1,2}) to (\d{1,2}), (20\d{2})\s*"
                      r"\(inclusive, America/New_York local calendar dates\).*?less than (\d+)° Fahrenheit", rule or "")
    if not match or station != match[1] or station not in STATIONS or market["operator"] != "below":
        return None
    first = datetime.strptime(f"{match[2]} {match[3]} {match[5]}", "%b %d %Y").date()
    last = first.replace(day=int(match[4]))
    if ((first, last) != TRADE_DAYS or (last - first).days != 3 or "The Weather Company" not in rule
            or "lowest of the official daily minimum temperatures" not in market["rules_secondary"]
            or Decimal(match[6]) != Decimal(market["upper"])):
        return None
    close_at = datetime.combine(last, datetime.min.time(), LOCAL) + timedelta(hours=23, minutes=59)
    return (first, last) if utc(market["closed_at"]) == close_at else None


def _live(data, cutoff):
    earliest, latest = TRADE_DAYS
    provider = {(row["station"], row["label_date"]): row for row in strategy_twc.reports(data, start=str(earliest), end=str(latest),
                                                                                      as_of=cutoff)
                if row["station"] in STATIONS and row["timezone"] == str(LOCAL)
                and row["provider_status"] in {"official", "revised"} and row["temperature_low_f"] is not None}
    stations = defaultdict(list)
    for market in _native(data):
        if _case_identity(market) and utc(market["last_received_at"]) <= cutoff:
            stations[market["station"]].append(market)
    cases = []
    for station, markets in sorted(stations.items()):
        days = _four_days(earliest)
        daily = [provider.get((station, str(day))) for day in days]
        known = [row["temperature_low_f"] for row in daily if row]
        low = min(known, default=None)
        market = min(markets, key=lambda row: abs(float(row["upper"]) - (low if low is not None else 35)))
        match = [row["instrument_id"] for row in markets if low is not None and low < float(row["upper"])]
        remaining = []
        for day, report in zip(days, daily, strict=True):
            if report is None:
                forecast = _low_forecast(data, station, day, cutoff)
                if forecast:
                    issue = utc(forecast["issued_at"])
                    card = expansion_nbm.load_card(data, station, issue)
                    if card and utc(card["received_at"]) <= cutoff:
                        remaining.append({"date": str(day), **forecast, "actual_local_receipt": card["received_at"]})
        reported = sum(row is not None for row in daily)
        detail = (f"Weather Company evidence covers {reported}/4 complete days. "
                  f"The smallest available daily report is {low:g}°F. " if low is not None else "No complete daily source report is present. ")
        detail += f"The native strike is strictly below {float(market['upper']):g}°F."
        cases.append({"station": station, "date": str(latest), "at": iso(cutoff), "venue": "kalshi",
                      "instrument_id": market["instrument_id"], "title": f"{station} · Four-day low report status", "detail": detail,
                      "start": str(earliest), "end": str(latest), "provider_reports": daily, "native_markets": markets,
                      "available_complete_days": reported, "partial_min_f": low,
                      "complete_period_min_f": low if reported == 4 else None, "remaining_forecasts": remaining,
                      "native_thresholds_breached_by_available_completed_day": match})
    return cases


def gather(data, cutoff):
    periods, absent = _historical(data)
    return {"version": VERSION, "periods": periods, "absent": absent, "live": _live(data, cutoff), "cutoff": iso(cutoff)}


def _scale(forecast):
    return max(1, forecast["sd_f"] or 1)


def _measure(calibration):
    by_station = defaultdict(list)
    for row in calibration:
        residuals = [(report["low_f"] - forecast["mean_f"]) / _scale(forecast)
                     for report, forecast in zip(row["reports"][2:], row["future_forecasts"], strict=True)]
        by_station[row["station"]].append(residuals)
    return {station: np.asarray(rows) for station, rows in by_station.items()}


def _normal_cdf(threshold, observed, forecasts):
    if threshold >= observed:
        return 1.0
    return 1 - prod(1 - NormalDist(forecast["mean_f"], _scale(forecast)).cdf(threshold) for forecast in forecasts)


def _predictions(row, sample):
    known = row["known_minimum_f"]
    forecasts = row["future_forecasts"]
    anchors = research_model._benchmark_buckets(round(min(known, *(forecast["mean_f"] for forecast in forecasts))))
    truth = [_outcome(bucket, row["actual_f"]) for bucket in anchors]
    sample_future = sample * np.array([_scale(forecast) for forecast in forecasts]) + np.array([f["mean_f"] for f in forecasts])
    scenarios = np.floor(sample_future + 0.5)
    paired = np.minimum(known, np.min(scenarios, axis=1))
    independent = np.minimum(known, np.minimum(scenarios[:, 0, None], scenarios[None, :, 1])).ravel()
    probabilities = {method: [] for method in METHODS}
    for bucket in anchors:
        lower, upper = _limits(bucket)
        probabilities["independent_normal"].append(_normal_cdf(upper, known, forecasts) - _normal_cdf(lower, known, forecasts))
        for name, entries in (("paired_empirical", paired), ("independent_empirical", independent)):
            probabilities[name].append(float(np.mean((lower < entries) & (entries < upper))))
    estimates = {method: {"probabilities": values, "brier": mean((p - y) ** 2 for p, y in zip(values, truth, strict=True))}
                 for method, values in probabilities.items()}
    return dict(row, anchors=anchors, truth=truth, estimates=estimates)


def evaluate(data, corpus):
    frozen, digest = research_model.freeze_inputs(data, corpus)
    samples = _measure(corpus["periods"]["calibration"])
    if not samples:
        raise ValueError("No earlier paired NOAA and climate report evidence")
    scored = [_predictions(row, samples[row["station"]]) for row in corpus["periods"]["test"] if row["station"] in samples]
    metrics = []
    for station in (None, *STATIONS):
        selected = [row for row in scored if station in (None, row["station"])]
        if selected:
            metrics.extend({"group": station or "Held-out four-day windows", "method": method, "label": label,
                            "calendar_starts": len({row["date"] for row in selected}), "station_periods": len(selected),
                            "weeks": len({row["week"] for row in selected}),
                            "bucket_brier": mean(row["estimates"][method]["brier"] for row in selected)}
                           for method, label in METHODS.items())
    weekly = defaultdict(list)
    for row in (case for case in scored if case["within_week"]):
        weekly[row["week"]].append(row["estimates"]["paired_empirical"]["brier"] - row["estimates"]["independent_empirical"]["brier"])
    differences = [mean(items) for items in weekly.values()]
    bootstrap = np.random.default_rng(20261115).choice(differences, size=(2500, len(differences))).mean(axis=1)
    record = {"version": VERSION, "title": "Four-day lows and two-day forecast dependence",
              "hypothesis": "The two remaining nights share forecast errors. A paired earlier residual sample may improve minimum probabilities after two daily reports are known.",
              "target": "Four consecutive official station minimum labels and the native Northeast Kalshi series", "venues": ["kalshi"],
              "date_window": {**{key: list(map(str, interval)) for key, interval in WINDOWS.items()}, "actual_market": list(map(str, TRADE_DAYS))},
              "metrics": metrics, "cases": corpus["live"], "omitted": corpus["absent"],
              "coverage": {"earlier_pairs_by_station": {key: len(vals) for key, vals in samples.items()},
                           "earlier_error_correlation": {key: float(np.corrcoef(vals.T)[0, 1]) for key, vals in samples.items()},
                           "paired_minus_independent_week_bootstrap_ci_95": np.quantile(bootstrap, (0.025, 0.975)).tolist()},
              "limits": ["Historical forecasts use published issue and NOAA archive clocks. The NWS labels come from the IEM summary transport, with both already-known reports timestamped before the decision.",
                         "The rolling historical windows overlap. The uncertainty check uses only windows entirely contained in separate one-week blocks. There is one native contract period with real payout eligible only after its own terms are met.",
                         "The current-market panel uses original local provider receipt time. It records complete daily reports separately and withholds the period minimum until all four days are present."],
              "provenance": {"frozen_input": str(frozen.relative_to(data)), "frozen_input_sha256": digest, "cutoff": corpus["cutoff"],
                             "provider_source": strategy_twc.SITE,
                             "source_sha256": research_model.run_sources(sys.modules[__name__], expansion_nbm, expansion_weather)}}
    return research_model.write_record(data, record, {"historical_test": scored, "native_cases": corpus["live"]})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--input", type=Path)
    args = parser.parse_args()
    corpus = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else gather(args.data, datetime.now(timezone.utc))
    print(evaluate(args.data, corpus))


if __name__ == "__main__":
    main()
