"""Compare Central Park and as-issued airport forecasts against the native NYC index."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_FLOOR
from functools import lru_cache
import gzip
import json
from pathlib import Path
from statistics import NormalDist, mean, stdev
import sys
from zoneinfo import ZoneInfo

from sklearn import __version__ as sklearn_version
from sklearn.feature_extraction import DictVectorizer
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline

from . import expansion_nbh, research_model, venue_archive, venue_index, venue_index_model
from .nbm import iso, utc
from .venue_index_market import _match_price

VERSION = "nyc-airport-forecast-v1"
SERIES = "KXTEMPNYCHS"
METHOD = "https://assets.kalshi.com/contract_terms/NYCWINDEX.pdf"
LOCAL = ZoneInfo("America/New_York")
FORECAST_WINDOWS = {"fit": ("2026-09-10", "2026-10-09"), "calibration": ("2026-10-11", "2026-10-23"),
                    "test": ("2026-10-25", "2026-11-14")}
INDEX_WINDOWS = venue_index_model.WINDOWS
LEADS = (15, 180)
LABELS = {"park": "NBM Central Park", "airports": "NBM eight-airport mean", "airport_ridge": "Airport forecast correction",
          "persistence": "Most recent canonical index", "index": "Index-only nowcast", "hybrid": "Index nowcast plus airport forecast",
          "market": "Earlier minute midpoint"}


def _market_events(data, cutoff):
    markets = defaultdict(list)
    with venue_archive.connect(data) as db:
        rows = db.execute("""SELECT instrument_id,event_id,title,label,rules_primary,rules_url,source,opened_at,closed_at,
            status,settlement_at,settlement_source,settlement_received_at,settlement_receipt_id,yes_payout,
            expiration_value,operator,lower FROM markets WHERE venue=? AND series_id=? AND closed_at>=?
            ORDER BY closed_at,instrument_id""", (venue_archive.KALSHI, SERIES, FORECAST_WINDOWS["fit"][0]))
        for row in rows:
            markets[row["event_id"]].append(dict(row))
    omitted = Counter()
    events = []
    for event_id, group in markets.items():
        verified = [row for row in group if row["status"] == "settled" and row["settlement_at"]
                    and utc(row["settlement_at"]) <= cutoff and row["yes_payout"] in ("0", "1")]
        if not verified:
            omitted["no native payout by the collection cutoff"] += 1
            continue
        clocks = {venue_index_model._target_time(row["rules_primary"]) for row in verified}
        if len(clocks) != 1 or None in clocks:
            omitted["missing or conflicting rule clocks"] += 1
            continue
        target = clocks.pop()
        if any(row["rules_url"] != METHOD or "Synoptic Data" not in row["rules_primary"]
               or not row["closed_at"] or utc(row["closed_at"]) != target for row in verified):
            omitted["native rule or market-close mismatch"] += 1
            continue
        values = {Decimal(row["expiration_value"]) for row in verified if row["expiration_value"]}
        if len(values) != 1:
            omitted["missing or conflicting native expiration value"] += 1
            continue
        value = values.pop()
        if not value.is_finite() or not -150 <= value <= 150:
            omitted["native expiration value unavailable"] += 1
            continue
        prices = []
        for row in verified:
            if row["operator"] != "above" or row["lower"] is None:
                omitted["unsupported native threshold"] += 1
                continue
            if int(row["yes_payout"]) != (value > Decimal(row["lower"])):
                omitted["native outcome differs from nominal numeric strike"] += 1
                continue
            prices.append(row)
        if prices:
            events.append({"event_id": event_id, "target_at": iso(target), "actual_f": float(value),
                           "settled_at": iso(max(utc(row["settlement_at"]) for row in prices)), "prices": prices,
                           "date": target.astimezone(LOCAL).date().isoformat()})
    return events, omitted


def _equal_configuration(configs, decision, target):
    prior = [item for item in configs if item["at"] <= iso(target) and item["published_at"] <= iso(decision)]
    if not prior:
        return None
    config = prior[-1]
    weights = {item["station_id"]: item["weight"] for item in config["stations"]}
    expected = {station + "1M": 0.125 for station in expansion_nbh.NYC_MEMBERS}
    return config["config_version"] if weights == expected else None


def forecast_snapshot(cards, decision, target):
    cycle = decision.replace(hour=6 * (decision.hour // 6), minute=0, second=0, microsecond=0)
    for offset in range(5):
        issue = cycle - timedelta(hours=6 * offset)
        selected = {}
        for station in expansion_nbh.NYC_AND_MEMBERS:
            card = cards(station, issue)
            if not card or utc(card["available_at"]) > decision:
                break
            point = next((item for item in card["hourly"] if item["valid_at"] == iso(target)), None)
            if point is None:
                break
            selected[station] = {"temp_f": point["temp_f"], "card_sha256": card["card_sha256"],
                                 "source": card["source"], "available_at": card["available_at"], "received_at": card["received_at"]}
        if len(selected) == len(expansion_nbh.NYC_AND_MEMBERS):
            members = [selected[station]["temp_f"] for station in expansion_nbh.NYC_MEMBERS]
            return {"issued_at": iso(issue), "available_at": max(item["available_at"] for item in selected.values()),
                    "members": selected, "park": selected[expansion_nbh.CENTRAL_PARK]["temp_f"], "airports": mean(members),
                    "spread_f": max(members) - min(members), "forecast_hours": (target - issue).total_seconds() / 3600}
    return None


def gather(data, cutoff):
    events, omitted = _market_events(data, cutoff)
    calibration = venue_index.calibrations(data, "nyc")
    configs = [{**item, "at": iso(venue_index.index_time(item["effective_at_ms"])),
                "published_at": iso(venue_index.index_time(item["published_at_ms"]))} for item in calibration["value"]]

    @lru_cache(maxsize=300)
    def cards(station, issue):
        return expansion_nbh.load(data, station, issue)

    @lru_cache(maxsize=3)
    def index(day):
        start = datetime.combine(day, datetime.min.time(), timezone.utc)
        return venue_index_model._canonical_series(data, "nyc", start=start - timedelta(hours=2), end=start + timedelta(days=1))

    cases = []
    for event in events:
        target = utc(event["target_at"])
        for lead in LEADS:
            decision = target - timedelta(minutes=lead)
            version = _equal_configuration(configs, decision, target)
            forecast = forecast_snapshot(cards, decision, target) if version else None
            if not forecast:
                omitted[f"{lead} min: missing same-cycle forecast or equal-weight configuration"] += 1
                continue
            context = None
            if lead == 15 and INDEX_WINDOWS["fit"][0] <= event["date"]:
                context = venue_index_model._snapshot("nyc", target, index(target.date()), str(LOCAL))
            cases.append({**event, **forecast, **venue_index_model._seasoning(target, str(LOCAL)), "at": iso(decision),
                          "lead_minutes": lead, "config_version": version, "index_context": context})
    return {"version": VERSION, "cases": cases, "omitted": dict(omitted), "calibration_receipt": calibration["receipt_id"],
            "cutoff": iso(cutoff)}


def _partition(cases, windows):
    samples = {split: [row for row in cases if first <= row["date"] <= last] for split, (first, last) in windows.items()}
    for left, right in (("fit", "calibration"), ("calibration", "test")):
        if samples[right]:
            first_decision = min(utc(row["at"]) for row in samples[right])
            samples[left] = [row for row in samples[left] if utc(row["settled_at"]) < first_decision]
    return samples


def _airport_features(row):
    return {key: row[key] for key in ("airports", "spread_f", "forecast_hours", "diurnal_sin", "diurnal_cos")}


def _index_features(row):
    return venue_index_model._features(row["index_context"], include_spread=True)


def _hybrid_features(row):
    context = row["index_context"]
    return {**_index_features(row), **_airport_features(row), "airport_departure_f": row["airports"] - context["temperature_f"]}


def _fit(cases, features, base):
    model = make_pipeline(DictVectorizer(sparse=False), Ridge(alpha=10))
    model.fit([features(row) for row in cases], [row["actual_f"] - base(row) for row in cases])
    return model


def _suite(cases, windows, *, hybrid=False):
    samples = _partition(cases, windows)
    if not all(samples.values()):
        return None

    def airport(row):
        return row["airports"]

    def persistence(row):
        return row["index_context"]["temperature_f"]

    base = {"park": lambda row: row["park"], "airports": airport, "airport_ridge": airport}
    features = {"airport_ridge": _airport_features}
    if hybrid:
        base.update({method: persistence for method in ("persistence", "index", "hybrid")})
        features.update(index=_index_features, hybrid=_hybrid_features)
    models = {method: _fit(samples["fit"], extra, base[method]) for method, extra in features.items()}

    def predictions(rows):
        estimates = {method: [point(row) for row in rows] for method, point in base.items()}
        for method, model in models.items():
            residuals = model.predict([features[method](row) for row in rows])
            estimates[method] = [point + float(delta) for point, delta in zip(estimates[method], residuals, strict=True)]
        return [dict(row, point_f={method: estimate[i] for method, estimate in estimates.items()}) for i, row in enumerate(rows)]

    calibration = {}
    calibration_cases = predictions(samples["calibration"])
    for method in base:
        errors = [row["actual_f"] - row["point_f"][method] for row in calibration_cases]
        calibration[method] = {"bias_f": mean(errors), "sd_f": max(0.35, stdev(errors))}
    return predictions(samples["test"]), calibration, {key: _counts(items) for key, items in samples.items()}


def _cutpoint(threshold):
    cents = (Decimal(threshold) * 100).to_integral_value(rounding=ROUND_FLOOR)
    return float((cents + Decimal("0.5")) / 100)


def _score(row, calibration):
    truths = [int(market["yes_payout"]) for market in row["prices"]]
    estimates = {}
    for method, point in row["point_f"].items():
        center = point + calibration[method]["bias_f"]
        normal = NormalDist(center, calibration[method]["sd_f"])
        p = [1 - normal.cdf(_cutpoint(market["lower"])) for market in row["prices"]]
        estimates[method] = {"brier": mean((a - b) ** 2 for a, b in zip(p, truths, strict=True)),
                             "probabilities": p, "error_f": point - row["actual_f"], "calibrated_error_f": center - row["actual_f"]}
    return dict(row, estimates=estimates)


def _counts(rows):
    return {"calendar_dates": len({row["date"] for row in rows}), "hourly_events": len(rows),
            "threshold_cases": sum(len(row["prices"]) for row in rows)}


def _metrics(rows, group):
    stats = []
    for method in rows[0]["estimates"]:
        days = defaultdict(list)
        for row in rows:
            days[row["date"]].append(row["estimates"][method])
        stats.append({"group": group, "method": method, "label": LABELS[method], **_counts(rows),
                      "bucket_brier": mean(mean(item["brier"] for item in entries) for entries in days.values()),
                      **({"mae_f": mean(mean(abs(item["error_f"]) for item in entries) for entries in days.values()),
                          "calibrated_mae_f": mean(mean(abs(item["calibrated_error_f"]) for item in entries)
                                                    for entries in days.values())} if method != "market" else {})})
    return stats


def _price_subset(data, rows):
    matched = []
    with venue_archive.connect(data) as db:
        for row in rows:
            when = utc(row["at"]) - timedelta(minutes=1)
            indices = []
            quotes = []
            for i, market in enumerate(row["prices"]):
                if not market["opened_at"] or utc(market["opened_at"]) > when:
                    continue
                quote, _ = _match_price(db, market["instrument_id"], when)
                if quote is not None:
                    indices.append(i)
                    quotes.append(quote)
            if not indices:
                continue
            prices = [row["prices"][i] for i in indices]
            truth = [int(market["yes_payout"]) for market in prices]
            estimates = {method: dict(estimate, probabilities=[estimate["probabilities"][i] for i in indices])
                         for method, estimate in row["estimates"].items()}
            estimates["market"] = {"probabilities": [quote["probability"] for quote in quotes]}
            for value in estimates.values():
                value["brier"] = mean((p - y) ** 2 for p, y in zip(value["probabilities"], truth, strict=True))
            matched.append(dict(row, prices=prices, estimates=estimates, market_quotes=quotes))
    return matched


def _case(row, index):
    market = row["prices"][index]
    forecast = row["point_f"]
    return {"title": f"NYC Weather Index · Above {market['lower']}°F", "date": row["date"], "at": row["at"],
            "venue": "kalshi", "instrument_id": market["instrument_id"], "region": "New York City Weather Index",
            "detail": f"Central Park forecast {forecast['park']:.1f}°F. Eight-airport forecast {forecast['airports']:.1f}°F. "
                      f"Native index {row['actual_f']:.2f}°F and YES paid ${market['yes_payout']}.",
            "target_at": row["target_at"], "issued_at": row["issued_at"], "available_at": row["available_at"],
            "config_version": row["config_version"], "members": row["members"], "market": market, "actual_f": row["actual_f"],
            "point_f": forecast, "probabilities": {key: value["probabilities"][index] for key, value in row["estimates"].items()}}


def evaluate(data, corpus):
    frozen, digest = research_model.freeze_inputs(data, corpus)
    metrics, coverage, adjustments, uncertainty, cases, details = [], {}, {}, [], [], {}
    suites = [(f"{lead} minutes ahead · NOAA forecast", lead, False, FORECAST_WINDOWS) for lead in LEADS]
    suites.append(("15 minutes ahead · index update", 15, True, INDEX_WINDOWS))
    for group, lead, hybrid, window in suites:
        subset = [row for row in corpus["cases"] if row["lead_minutes"] == lead and (not hybrid or row["index_context"])]
        fit = _suite(subset, window, hybrid=hybrid)
        if not fit:
            continue
        test, calibration, counts = fit
        coverage[group] = counts
        adjustments[group] = calibration
        scored = [_score(row, calibration) for row in test]
        details[group] = scored
        metrics.extend(_metrics(scored, group))
        pairs = [("park", "airports"), ("airports", "airport_ridge")] if not hybrid else [("persistence", "index"), ("index", "hybrid")]
        for baseline, candidate in pairs:
            for metric in ("mae", "brier"):
                uncertainty.append({"group": group, **research_model.date_bootstrap(scored, baseline, candidate, metric)})
        if hybrid:
            matched = _price_subset(data, scored)
            if matched:
                matched_group = "15 minutes ahead · exact archived price subset"
                metrics.extend(_metrics(matched, matched_group))
                details[matched_group] = matched
            continue
        ranked = sorted(scored, key=lambda row: abs(row["estimates"]["park"]["error_f"])
                        - abs(row["estimates"]["airports"]["error_f"]), reverse=True)
        for row in (ranked[0], ranked[-1]):
            index = min(range(len(row["prices"])), key=lambda i: abs(float(row["prices"][i]["lower"]) - row["actual_f"]))
            cases.append(_case(row, index))
    if len(coverage) < len(suites):
        raise ValueError("Incomplete chronological forecast or index context cohorts")
    record = {"version": VERSION, "title": "Which temperatures forecast the NYC Weather Index?",
              "hypothesis": "As-issued forecasts at the eight listed airports may track the composite index more closely than Central Park. "
                            "A second test looks for forecast value once a recent canonical index is already known.",
              "target": "Native NYC hourly Weather Index and verified strict-above Kalshi payouts", "venues": ["kalshi"],
              "date_window": {"forecast": FORECAST_WINDOWS, "index_update": INDEX_WINDOWS}, "coverage": coverage,
              "metrics": metrics, "uncertainty": uncertainty, "cases": cases, "omitted": corpus["omitted"],
              "limits": ["All nine forecasts share a NOAA issue and exact valid hour. Retrospective availability uses the original NOAA object clock.",
                         "Eight-airport averaging assumes full coverage. The actual index can omit a member and use quality control and fallbacks.",
                         "Index input clocks use the filed five-minute publication convention. Original local historical index receipts are unavailable.",
                         "Displayed price metrics use the same stored one-minute candle cases. They do not measure execution or fill rates."],
              "provenance": {"frozen_input": str(frozen.relative_to(data)), "frozen_input_sha256": digest, "membership_rule": METHOD,
                             "index_api": venue_index.DOCS, "calibration_receipt": corpus["calibration_receipt"],
                             "earlier_residual_calibration": adjustments, "sklearn": sklearn_version,
                             "source_sha256": research_model.run_sources(sys.modules[__name__], expansion_nbh, venue_index_model)}}
    return research_model.write_record(data, record, details)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--input", type=Path)
    parser.add_argument("--cutoff", type=utc, default=datetime.now(timezone.utc))
    args = parser.parse_args()
    corpus = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else gather(args.data, args.cutoff)
    print(evaluate(args.data, corpus))


if __name__ == "__main__":
    main()
