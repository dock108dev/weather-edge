"""Compare hourly Kalshi index forecasts with the same market's minute price evidence."""

import argparse
from collections import Counter, defaultdict
from datetime import timedelta
import gzip
from itertools import batched
import json
import math
from pathlib import Path
from statistics import mean
import sys

from sklearn import __version__ as sklearn_version
from sklearn.linear_model import LogisticRegression

from . import research_model, venue_archive, venue_index_model
from .market import iso, parse_time
from .venue_benchmark import _probability_proxy
from .venue_clock_study import MINUTE_CANDLE_KIND, _batch
from .venue_sources import PublicGET

VERSION = "hourly-index-market-clock-v1"
CLOCKS_AFTER = (5, 10)
MIN_SURPRISE = 0.5
MIN_DIVERGENCE = 0.05
METHODS = {"market": "Minute candle midpoint", "weather": "Earlier Ridge weather estimate",
           "blend": "75% market + 25% weather", "learned": "Earlier-date market and weather logistic"}


def _keep(corpus):
    windows = venue_index_model._partition(corpus["rows"])
    return [*windows["calibration"], *windows["test"]]


def backfill(data, corpus):
    clocks = defaultdict(set)
    for row in _keep(corpus):
        when = parse_time(row["at"])
        clocks[when].update(market["instrument_id"] for market in row["prices"])
    get = PublicGET(data)
    totals = Counter()
    for at, market_ids in sorted(clocks.items()):
        for number, group in enumerate(batched(sorted(market_ids), 40), 1):
            points = _batch(data, get, at, group, number, before=2, after=max(CLOCKS_AFTER), prefix="hourly-index/")
            totals["returned_candles"] += points
            totals["requests"] += 1
    return dict(totals)


def _match_price(db, instrument, clock, *, same_or_before=True):
    left = clock - timedelta(minutes=1) if same_or_before else clock
    rel = "<=" if same_or_before else "="
    row = db.execute(
        f"""SELECT at,receipt_id,body FROM history WHERE venue=? AND instrument_id=? AND kind=?
          AND at>=? AND at{rel}? ORDER BY at DESC LIMIT 1""",
        (venue_archive.KALSHI, instrument, MINUTE_CANDLE_KIND, iso(left), iso(clock)),
    ).fetchone()
    if row is None:
        return None, "no stored candle"
    raw = json.loads(row["body"])
    proxy = _probability_proxy(raw, venue_archive.KALSHI)
    if proxy is None:
        return None, "no coherent two-sided candle proxy"
    return {**proxy, "raw": raw, "source_at": row["at"], "receipt_id": row["receipt_id"]}, None


def gather(data, corpus):
    fits = venue_index_model._partition(corpus["rows"])
    models = venue_index_model._fit(fits["fit"])
    calibration = venue_index_model._calibrate(venue_index_model._predictions(fits["calibration"], models))
    cases = {"fit": [], "test": []}
    omitted = Counter()
    with venue_archive.connect(data) as db:
        for phase, study_group in (("fit", "calibration"), ("test", "test")):
            available = venue_index_model._predictions(fits[study_group], models)
            for row in available:
                decision = parse_time(row["at"])
                for market in row["prices"]:
                    if not market["opened_at"] or not market["closed_at"] or not (
                        parse_time(market["opened_at"]) <= decision - timedelta(minutes=1) < parse_time(market["closed_at"])
                    ):
                        omitted[phase + ": outside trading clock"] += 1
                        continue
                    symbol = market["instrument_id"]
                    baseline, gap = _match_price(db, symbol, decision - timedelta(minutes=1))
                    if gap:
                        omitted[phase + ": " + gap] += 1
                        continue
                    market_p = baseline["probability"]
                    weather_p = venue_index_model._prob(row, "ridge", market["threshold_f"], calibration)
                    after = {}
                    for horizon in CLOCKS_AFTER:
                        clock = decision + timedelta(minutes=horizon)
                        value, reason = _match_price(db, symbol, clock, same_or_before=False)
                        after[str(horizon)] = value if value else {"missing": reason}
                    cases[phase].append({
                        "city": row["city"], "date": row["date"], "at": row["at"], "target_at": row["target_at"],
                        "market": market_p, "weather": weather_p, "blend": market_p * 0.75 + weather_p * 0.25,
                        "outcome": market["native_payout_yes"], "delta_10_f": row["delta_10_f"],
                        "actual_f": row["actual_f"], "forecast_f": row["ridge"], "market_rule": market,
                        "original": baseline, "rechecks": after, "index_source": row["latest"]["source"],
                        "index_source_at": row["latest"]["at"], "index_receipt_id": row["latest"]["receipt_id"],
                    })
    return {"version": VERSION, "cases": cases, "missing": dict(omitted),
            "settings": {"later_minutes": CLOCKS_AFTER, "candidate_delta_min_f": MIN_SURPRISE,
                         "candidate_min_probability_difference": MIN_DIVERGENCE}}


def _logit(p):
    p = max(1e-6, min(1 - 1e-6, p))
    return math.log(p / (1 - p))


def _fit(fit, test):
    if len({row["outcome"] for row in fit}) < 2 or not test:
        return None
    groups = Counter(row["date"] for row in fit)
    weights = [len(fit) / len(groups) / groups[row["date"]] for row in fit]
    model = LogisticRegression(C=1.0).fit(
        [[_logit(row["market"]), _logit(row["weather"])] for row in fit],
        [row["outcome"] for row in fit], sample_weight=weights,
    )
    outputs = model.predict_proba([[_logit(row["market"]), _logit(row["weather"])] for row in test])
    for row, (_, p) in zip(test, outputs, strict=True):
        row["learned"] = float(p)
    return {"intercept": model.intercept_.tolist(), "coefficients": model.coef_.tolist()}


def _coverage(rows):
    return {"bucket_cases": len(rows), "calendar_dates": len({row["date"] for row in rows}),
            "city_days": len({(row["date"], row["city"]) for row in rows}),
            "hourly_events": len({(row["target_at"], row["city"]) for row in rows})}


def _score(rows, group, methods):
    stats = []
    for method in methods:
        by_day = defaultdict(list)
        for row in rows:
            by_day[row["date"]].append((row[method] - row["outcome"]) ** 2)
        if by_day:
            stats.append({"group": group, "method": method, "label": METHODS[method], **_coverage(rows),
                          "bucket_brier": mean(mean(errors) for errors in by_day.values())})
    return stats


def _moves(rows):
    candidates = [row for row in rows if abs(row["delta_10_f"]) >= MIN_SURPRISE
                  and abs(row["market"] - row["weather"]) >= MIN_DIVERGENCE]
    movement = {}
    for horizon in CLOCKS_AFTER:
        valid = []
        missed = Counter()
        for row in candidates:
            newer = row["rechecks"][str(horizon)]
            if "missing" in newer:
                missed[newer["missing"]] += 1
                continue
            direction = 1 if row["weather"] > row["market"] else -1
            change = (newer["probability"] - row["market"]) * direction
            valid.append({**row, "toward_weather": change, "recheck": newer})
        movement[str(horizon)] = {
            "candidate_cases": len(candidates), "eligible_rechecks": _coverage(valid), "missing_rechecks": dict(missed),
            "mean_toward_weather": mean(row["toward_weather"] for row in valid) if valid else None,
            "station_hour_dependence": "Different thresholds within one hour share index and payout evidence",
            "moved_toward": sum(row["toward_weather"] > 0 for row in valid),
            "unchanged": sum(row["toward_weather"] == 0 for row in valid),
        }
    return movement


def evaluate(data, corpus):
    data = Path(data)
    frozen, digest = research_model.freeze_inputs(data, corpus)
    fit, test = corpus["cases"]["fit"], [dict(row) for row in corpus["cases"]["test"]]
    param = _fit(fit, test)
    methods = METHODS if param else tuple(method for method in METHODS if method != "learned")
    metrics = _score(test, "Identical native binary payout cases", methods)
    for city in sorted({row["city"] for row in test}):
        metrics += _score([row for row in test if row["city"] == city], city, methods)
    cases = []
    seen = set()
    for row in sorted(test, key=lambda item: abs(item["market"] - item["weather"]), reverse=True):
        if row["city"] in seen:
            continue
        native = row["market_rule"]
        cases.append({
            "title": f"{row['city']} · {native['label']} · {row['target_at']}", "date": row["date"], "at": row["at"],
            "venue": venue_archive.KALSHI, "instrument_id": native["instrument_id"], "data": row,
            "detail": f"Market proxy {row['market']:.0%}, weather {row['weather']:.0%}, native YES paid ${row['outcome']}.",
        })
        seen.add(row["city"])
    record = {"version": VERSION, "title": "Hourly index probabilities against historical prices",
              "hypothesis": "Earlier weather probabilities may improve contemporaneous market probabilities. "
                            "Larger as-of changes test whether later prices move toward the weather estimate.",
              "target": "Exact hourly Kalshi payout and later minute midpoint changes", "venues": ["Kalshi"],
              "date_window": {"fit": venue_index_model.WINDOWS["calibration"], "test": venue_index_model.WINDOWS["test"]},
              "coverage": {"fit": _coverage(fit), "test": _coverage(test)}, "metrics": metrics, "cases": cases,
              "missing": corpus["missing"], "price_movement": _moves(test),
              "limits": ["Historical candles provide price proxies and no qualifying timestamped book with executable size.",
                         "Observation clocks follow the filed +300 seconds. Historical original local index receipt is unknown.",
                         "The learned combination has three earlier calendar dates. Later market moves are descriptive and clustered by hour."],
              "provenance": {"frozen_input": str(frozen.relative_to(data)), "frozen_input_sha256": digest,
                             "parameters": param, "settings": corpus["settings"], "scikit_learn": sklearn_version,
                             "source_sha256": research_model.run_sources(sys.modules[__name__], venue_index_model)}}
    return research_model.write_record(data, record, {"payout_test": test, "price_movement": _moves(test)})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--download", action="store_true")
    args = parser.parse_args(argv)
    source = json.loads(gzip.decompress(args.input.read_bytes()))
    if source["version"] == VERSION:
        print(evaluate(args.data, source))
        return
    if args.download:
        print(json.dumps(backfill(args.data, source)), flush=True)
    print(evaluate(args.data, gather(args.data, source)))


if __name__ == "__main__":
    main()
