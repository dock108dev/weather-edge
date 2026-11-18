"""Test daily-high forecast transfer to nine exact-station Kalshi series."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import gzip
import json
import math
from pathlib import Path
from statistics import mean
import sys

from sklearn import __version__ as sklearn_version
from threadpoolctl import threadpool_limits

from . import (expansion_nbm, expansion_replay, expansion_weather, research_model, research_model_high,
               research_model_venue, venue_archive)
from .model import _limits, _outcome, _probability
from .nbm import climate_window, utc

VERSION = "daily-high-new-stations-v1"
STATIONS = ("KATL", "KDCA", "KEWR", "KLAS", "KMSY", "KTTN", "KAUS", "KDFW", "KSEA")
ADAPT = (date(2026, 10, 15), date(2026, 10, 27))
TEST = (date(2026, 10, 29), date(2026, 11, 14))
METHODS = {"noaa": "NOAA TXN + XND", "bias": "NOAA + local bias", "ridge": "Ridge transfer",
           "nonlinear": "Nonlinear transfer", "local_ridge": "Ridge + local bias", "local_nonlinear": "Nonlinear + local bias"}
TRAINED_METHODS = {key: METHODS[key] for key in ("noaa", "bias", "ridge", "nonlinear")}
LOCAL_STRENGTH = 10


def _features(row, method):
    features = research_model_high._features(row, method)
    del features["station"]
    return features


def _weather_rows(data, station, *, adapt=ADAPT, test=TEST):
    labels, observations, cards = expansion_replay._station_data(data, station, adapt[0], test[1])
    split_rows = {"adapt": [], "test": []}
    gaps = []
    offset = expansion_weather.STATIONS[station][3]
    test_starts_at = climate_window(test[0], offset)[0] + timedelta(hours=min(research_model.HOURS))
    for split, (first, last) in (("adapt", adapt), ("test", test)):
        for day in research_model._days(first, last):
            label = labels.get(str(day))
            report = None
            if label:
                try:
                    report = expansion_weather.raw_report(data, station, day, label["product"])
                except (OSError, ValueError, KeyError, TypeError) as error:
                    gaps.append({"station": station, "date": str(day), "reason": str(error)})
                    continue
            if not report or report["high_f"] is None or (split == "adapt" and utc(report["published_at"]) >= test_starts_at):
                gaps.append({"station": station, "date": str(day), "reason": "No timely, complete raw high report"})
                continue
            start, end = climate_window(day, offset)
            day_cards = [card for issue, card in cards.items() if day - timedelta(days=2) <= issue.date() <= day]
            forecasts = [forecast for card in day_cards if (forecast := expansion_replay._high_forecast(card, start, end))]
            for hour in research_model.HOURS:
                row = research_model_high._snapshot(station, day, hour, forecasts, observations[day], report)
                if row:
                    split_rows[split].append(row)
                else:
                    gaps.append({"station": station, "date": str(day), "hour_lst": hour, "reason": "No same-station eligible TXN"})
    return split_rows, gaps


def _market_rows(data, weather_cases, *, family="daily-high", stations=STATIONS, test=TEST):
    groups = defaultdict(list)
    for row in weather_cases:
        groups[row["station"], row["date"]].append(row)
    skipped = Counter()
    prices = defaultdict(list)
    audits = []
    cache = {}
    slots = ",".join("?" for _ in stations)
    label_key = "high_f" if family == "daily-high" else "low_f"
    query = f"""SELECT instrument_id,event_id,station,day,rules_primary,rules_secondary,rules_url,resolution_source,source,
        expiration_value,yes_payout,settlement_at,settlement_received_at,settlement_receipt_id,opened_at,closed_at,
        operator,lower,upper FROM markets WHERE venue=? AND family=? AND station IN ({slots})
        AND day BETWEEN ? AND ? AND status='settled' AND yes_payout IN ('0','1') ORDER BY station,day,instrument_id"""
    with venue_archive.connect(data) as db:
        source_rows = [dict(row) for row in db.execute(query, ("kalshi", family, *stations, *map(str, test)))]
    for market in source_rows:
        station, day = market["station"], market["day"]
        weather = groups.get((station, day))
        if not weather:
            skipped["no matching weather station-day"] += 1
            continue
        pil = expansion_weather.CLI_OFFICE[station][1]
        if market["resolution_source"] != "The Weather Company" or f"({pil})" not in market["rules_primary"]:
            skipped["different or unidentified named provider station"] += 1
            continue
        try:
            value = Decimal(market["expiration_value"])
        except (InvalidOperation, TypeError):
            skipped["non-numeric native expiration"] += 1
            continue
        bounds = research_model_venue.integer_bucket(market)
        if not bounds or not value.is_finite() or value != value.to_integral_value() or not -150 <= value <= 150:
            skipped["unsupported native integer settlement or threshold"] += 1
            continue
        bucket = {"lower_f": bounds[0], "upper_f": bounds[1]}
        payout = int(market["yes_payout"])
        if _outcome(bucket, int(value)) != payout:
            skipped["native payout disagrees with native expiration value"] += 1
            continue
        official = weather[0]["official_report"]
        if payout == 1:
            audits.append({"station": station, "date": day, "market": market, "official_report": official,
                           f"venue_{label_key}": int(value), f"physical_{label_key}": official[label_key],
                           "source_equal": int(value) == official[label_key]})
        if not market["opened_at"] or not market["closed_at"]:
            skipped["missing native trading window"] += 1
            continue
        opened, closed = utc(market["opened_at"]), utc(market["closed_at"])
        for row in weather:
            decision = utc(row["at"])
            if opened <= decision < closed:
                display = research_model_venue._midpoint(data, market, cache, decision)
                prices[f"{station}/{day}/{row['hour']}"].append({"market": market, "bucket": bucket, "native_yes": payout,
                                                               f"venue_{label_key}": int(value), "display_price": display})
    return dict(prices), audits, dict(skipped)


def gather(data, anchor):
    seed = json.loads(gzip.decompress(anchor.read_bytes()))
    if seed["version"] != research_model_high.VERSION or set(seed["stations"]) & set(STATIONS):
        raise ValueError("Base study must use independent training stations")
    rows = {split: [] for split in ("adapt", "test")}
    gaps = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        for evidence, missing in pool.map(lambda station: _weather_rows(data, station), STATIONS):
            for key in rows:
                rows[key].extend(evidence[key])
            gaps.extend(missing)
    markets, audits, skipped = _market_rows(data, rows["test"])
    return {"version": VERSION, "baseline_input": research_model._source_entry(data, anchor),
            "baseline_windows": seed["windows"], "baseline_rows": {key: seed["rows"][key] for key in ("fit", "calibration")},
            "rows": rows, "markets": markets, "audits": audits, "skipped": skipped, "gaps": gaps}


def _points(model, cases):
    output = {method: [row["forecast"]["mean_f"] for row in cases] for method in TRAINED_METHODS if method != "bias"}
    with threadpool_limits(limits=1):
        for method, estimator in model["models"].items():
            residuals = estimator.predict([_features(row, method) for row in cases])
            output[method] = [m + float(e) for m, e in zip(output[method], residuals, strict=True)]
    return output


def _residuals(group, points, by):
    errors = defaultdict(list)
    for method, estimates in points.items():
        for row, center in zip(group, estimates, strict=True):
            errors[(method, *by(row))].append(row["actual_f"] - center)
    return errors


def _fit(anchors):
    return research_model.fit(anchors["fit"], anchors["calibration"], methods=TRAINED_METHODS, feature_fn=_features)


def _weather_probabilities(inputs):
    anchors = inputs["baseline_rows"]
    test, adapt = (inputs["rows"][key] for key in ("test", "adapt"))
    fit = _fit(anchors)
    calibration = _residuals(anchors["calibration"], _points(fit, anchors["calibration"]), lambda row: (row["hour"],))
    local_bias = _residuals(adapt, _points(fit, adapt), lambda row: (row["station"], row["hour"]))
    widths = {key: max(0.75, math.sqrt(mean(value ** 2 for value in residuals))) for key, residuals in calibration.items()}
    adjustments = {key: sum(values) / (len(values) + LOCAL_STRENGTH) for key, values in local_bias.items()}
    points = _points(fit, test)
    scored = []
    for index, row in enumerate(test):
        hour, station = row["hour"], row["station"]
        estimates = {}
        direct = points["noaa"][index]
        partition = research_model._benchmark_buckets(round(direct))
        actual = row["actual_f"]
        outcomes = [_outcome(bucket, actual) for bucket in partition]
        native_width = row["forecast"]["sd_f"]
        for method in METHODS:
            standard = "noaa" if method == "bias" else method.removeprefix("local_")
            point = points[standard][index]
            if method == "bias" or method.startswith("local_"):
                point += adjustments.get((standard, station, hour), 0)
            width = native_width if standard == "noaa" and native_width and native_width > 0 else widths[standard, hour]
            probabilities = [_probability(_limits(bucket), [point], width) for bucket in partition]
            estimates[method] = {"mean_f": point, "scale_f": width, "error_f": point - actual, "probabilities": probabilities,
                                 "brier": mean((p - y) ** 2 for p, y in zip(probabilities, outcomes, strict=True))}
        scored.append(dict(row, buckets=partition, truth=outcomes, estimates=estimates))
    return scored, [{"method": method, "station": station, "clock_lst": hour, "adapt_days": len(local_bias[method, station, hour]),
                     "bias_f": value} for (method, station, hour), value in adjustments.items()]


def _market_probabilities(scored, markets):
    final = []
    for row in scored:
        key = f"{row['station']}/{row['date']}/{row['hour']}"
        for native in markets.get(key, []):
            probabilities = {method: _probability(_limits(native["bucket"]), [item["mean_f"]], item["scale_f"])
                             for method, item in row["estimates"].items()}
            if native["display_price"] is not None:
                probabilities["market"] = native["display_price"]["midpoint"]
            final.append({**native, "station": row["station"], "date": row["date"], "at": row["at"], "hour": row["hour"],
                          "forecast": row["forecast"], "official_report": row["official_report"], "probabilities": probabilities})
    return final


def _market_metrics(rows, group, include_market):
    metrics = []
    if rows:
        methods = {**METHODS, "market": "Earlier completed Kalshi hourly midpoint"} if include_market else METHODS
        for method, name in methods.items():
            by_date = defaultdict(list)
            for row in rows:
                by_date[row["date"]].append((row["probabilities"][method] - row["native_yes"]) ** 2)
            metrics.append({"method": method, "label": name, "group": group, "calendar_dates": len(by_date),
                            "station_days": len({(row["station"], row["date"]) for row in rows}),
                            "bucket_cases": len(rows), "bucket_brier": mean(mean(errors) for errors in by_date.values())})
    return metrics


def _case(row):
    market = row["market"]
    native = row["venue_high_f"]
    report = row["official_report"]["high_f"]
    return {"station": row["station"], "date": row["date"], "at": row["at"], "venue": "kalshi",
            "instrument_id": market["instrument_id"], "title": f"{row['station']} · Exact daily-high payout",
            "detail": f"NOAA TXN {row['forecast']['mean_f']}°F. Same-station CLI {report}°F. "
                      f"Native ruling {native}°F and YES paid ${row['native_yes']}.", "evidence": row}


def evaluate(data, inputs):
    frozen, digest = research_model.freeze_inputs(data, inputs)
    scores, local_bias = _weather_probabilities(inputs)
    quotes = _market_probabilities(scores, inputs["markets"])
    priced = [row for row in quotes if row["display_price"] is not None]
    metrics = research_model.metrics(scores, "New stations · physical daily high", METHODS)
    for station in STATIONS:
        metrics.extend(research_model.metrics([row for row in scores if row["station"] == station], station, METHODS))
    for hour in research_model.HOURS:
        metrics.extend(research_model.metrics([row for row in scores if row["hour"] == hour], f"{hour:02} local standard", METHODS))
    metrics += _market_metrics(quotes, "Verified native payouts", False)
    metrics += _market_metrics(priced, "Identical native payouts with price evidence", True)
    reference = (("noaa", "ridge"), ("noaa", "nonlinear"), ("noaa", "bias"), ("ridge", "local_ridge"), ("nonlinear", "local_nonlinear"))
    contrasts = [research_model.date_bootstrap(scores, before, after, metric)
                 for before, after in reference for metric in ("mae", "brier")]
    unusual = sorted(quotes, key=lambda row: (row["venue_high_f"] != row["official_report"]["high_f"],
                                             abs(row["probabilities"]["noaa"] - row["native_yes"])), reverse=True)
    cases = []
    seen = set()
    for row in unusual:
        if row["station"] in seen:
            continue
        cases.append(_case(row))
        seen.add(row["station"])
    audits = inputs["audits"]
    coverage = {"adapt": research_model._independent_counts(inputs["rows"]["adapt"]), "test": research_model._independent_counts(scores),
                "native_station_days": len({(row["station"], row["date"]) for row in quotes}), "native_payout_cases": len(quotes),
                "markets_with_prices": len(priced), "source_comparison_station_days": len(audits),
                "nws_versus_native_disagreements": sum(not audit["source_equal"] for audit in audits)}
    record = {"version": VERSION, "title": "Can a daily-high model transfer to nine new stations?",
              "hypothesis": "A no-site-identity correction learned on six stations can improve on NOAA at unseen stations. "
                            "An earlier 13-day local sample checks whether an inexpensive site correction changes the result.",
              "target": "Exact raw NWS daily highs and independently verified Kalshi payouts", "venues": ["kalshi"],
              "date_window": {**{key: inputs["baseline_windows"][key] for key in ("fit", "calibration")},
                              "local_bias": list(map(str, ADAPT)), "test": list(map(str, TEST))},
              "coverage": coverage, "metrics": metrics, "cases": cases, "contrasts": contrasts, "omitted": inputs["skipped"],
              "limits": ["Transferred Ridge and nonlinear models were fit on six separate stations. The local-bias arm uses only the specified earlier sample and shrinks corrections toward zero.",
                         "New-station physical validation uses complete archived native NWS CLI text. The earlier training data use IEM transported daily summaries.",
                         "The historical observation availability proxy is measurement time plus ten minutes. NOAA cards carry the original archive publication clock.",
                         "Native Weather Company ruling values, actual payouts and separate same-station NWS reports are compared independently. Hourly display proxies omit executable depth."],
              "provenance": {"frozen_input": str(frozen.relative_to(data)), "frozen_input_sha256": digest,
                             "forecast_docs": "https://vlab.noaa.gov/web/mdl/nbm-textcard-v5.0", "venue_source": "https://weather.com/kalshi",
                             "sklearn": sklearn_version,
                             "source_sha256": research_model.run_sources(sys.modules[__name__], expansion_weather, expansion_nbm, research_model_high)}}
    return research_model.write_record(data, record, {"weather": scores, "payouts": quotes, "source_audit": audits, "local_bias": local_bias})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--baseline-input", type=Path)
    source.add_argument("--input", type=Path)
    args = parser.parse_args()
    inputs = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else gather(args.data, args.baseline_input)
    print(evaluate(args.data, inputs))


if __name__ == "__main__":
    main()
