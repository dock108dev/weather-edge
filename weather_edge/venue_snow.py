"""Station-month snowfall probability model and archived Kalshi contract replay."""

import argparse
from collections import Counter, defaultdict
from datetime import date, timedelta
import gzip
import json
import math
from pathlib import Path
from statistics import mean
import sys

from sklearn import __version__ as sklearn_version
from sklearn.feature_extraction import DictVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline

from . import research_model, venue_archive, venue_snow_data
from .expansion_weather import STATIONS
from .market import decimal, iso, parse_time
from .nbm import climate_window
from .venue_benchmark import _probability_proxy
from .venue_history import CANDLE_KIND

VERSION = "kalshi-monthly-snow-v1"
FIT = (2006, 2017)
TEST = (2018, 2026)
THRESHOLDS = (2, 4, 6, 8, 10, 12)
CLOCKS = (1, 8, 15, 22, 29)
HOUR_LST = 18
KA_SERIES = ("KXNYCSNOWM", "KXCHISNOWM", "KXSEASNOWM", "KXDENSNOWM")
MEASUREMENT = "https://www.weather.gov/gsp/snow"
TERMS = "https://assets.kalshi.com/contract_terms/SNOWOVERTIME.pdf"
LABELS = {"climatology": "Station and calendar-month climatology",
          "remainder": "Observed accumulation + historical remaining snowfall",
          "logistic": "Earlier-year logistic model"}


def _clock(station, year, month, day):
    start, _ = climate_window(date(year, month, day), STATIONS[station][3])
    return start + timedelta(hours=HOUR_LST)


def _prefix(month, clock):
    """Stop at the latest consecutive daily report released by the decision."""
    count = 0
    running = decimal(0)
    latest = None
    for report in month["days"]:
        if report is None or parse_time(report["published_at"]) > clock or report["daily_inches"] is None:
            break
        count += 1
        running += decimal(report["daily_inches"])
        latest = report
    return count, running, latest


def _earlier(months):
    groups = defaultdict(list)
    for month in months:
        if FIT[0] <= month["year"] <= FIT[1] and month["official_total"] is not None:
            groups[(month["station"], month["month"])].append(month)
    return groups


def _smooth(wins, n):
    return (wins + 0.5) / (n + 1)


def _record(month, day, threshold, historical, *, remove_same_year=False):
    clock = _clock(month["station"], month["year"], month["month"], day)
    day_count, observed, latest = _prefix(month, clock)
    if day > 1 and day_count < day - 2:
        return None
    references = [row for row in historical if not remove_same_year or row["year"] != month["year"]]
    if not references:
        return None
    total = month["official_total"]
    label = None if total is None else int(decimal(total) > decimal(threshold))
    unconditional = [decimal(row["official_total"]) for row in references]
    # Compare equal calendar-day prefixes. Some reference months (leap years)
    # have one additional future day, which remains in their calendar-month total.
    destinations = [row for row in references if row["complete_daily"]
                    and decimal(row["daily_total"]) == decimal(row["official_total"])]
    future_total = [sum(decimal(item["daily_inches"]) for item in ref["days"][day_count:]) for ref in destinations]
    if not future_total:
        return None
    prior = _smooth(sum(value > decimal(threshold) for value in unconditional), len(unconditional))
    reached = observed > decimal(threshold)
    remainder = 1.0 if reached else _smooth(sum(observed + value > decimal(threshold) for value in future_total), len(future_total))
    return {
        "station": month["station"], "year": month["year"], "month": month["month"], "decision_day": day,
        "date": date(month["year"], month["month"], day).isoformat(), "at": iso(clock), "threshold_in": threshold,
        "observed_days": day_count, "observed_inches": float(observed), "latest_report": latest,
        "remaining_days": len(month["days"]) - day_count, "weather_threshold_crossed": reached,
        "history_months": len(unconditional), "remainder_history_months": len(future_total),
        "climatology": prior, "remainder": remainder, "outcome": label, "official_total": total,
    }


def _features(row):
    probability = row["remainder"]
    logit = 11 if probability == 1 else math.log(probability / (1 - probability))
    return {"station": row["station"], "month": str(row["month"]), "remainder_logit": logit,
            "above_threshold_so_far": row["observed_inches"] - row["threshold_in"],
            "remaining_fraction": row["remaining_days"] / 31}


def _fit(records):
    counts = Counter((row["year"], row["month"], row["station"]) for row in records)
    weights = [len(records) / len(counts) / counts[(row["year"], row["month"], row["station"])] for row in records]
    estimator = make_pipeline(DictVectorizer(sparse=False), LogisticRegression(C=1.0, max_iter=500))
    estimator.fit([_features(row) for row in records], [row["outcome"] for row in records],
                  logisticregression__sample_weight=weights)
    return estimator


def _predict(records, estimator):
    if records:
        for row, probabilities in zip(records, estimator.predict_proba([_features(row) for row in records]), strict=True):
            row["logistic"] = 1.0 if row["weather_threshold_crossed"] else float(probabilities[1])


def _sample(months, groups):
    splits = {"fit": [], "test": []}
    missing = Counter()
    for month in months:
        if month["station"] not in venue_snow_data.CORE_STATIONS or month["official_total"] is None:
            continue
        group = "fit" if FIT[0] <= month["year"] <= FIT[1] else "test"
        references = groups[(month["station"], month["month"])]
        for day in CLOCKS:
            if day > len(month["days"]):
                continue
            for threshold in THRESHOLDS:
                row = _record(month, day, threshold, references, remove_same_year=group == "fit")
                if row is not None:
                    splits[group].append(row)
                else:
                    missing[f"{group}: insufficient published prefix or earlier reference"] += 1
    return splits, dict(missing)


def _coverage(rows):
    return {"bucket_cases": len(rows), "calendar_dates": len({row["date"] for row in rows}),
            "calendar_years": len({row["year"] for row in rows}),
            "station_months": len({(row["station"], row["year"], row["month"]) for row in rows}),
            "snapshots": len({(row["station"], row["date"]) for row in rows})}


def _score(rows, group, methods=tuple(LABELS)):
    result = []
    if not rows:
        return result
    for method in methods:
        cases = defaultdict(list)
        for row in rows:
            cases[(row["station"], row["year"], row["month"])].append((row[method] - row["outcome"]) ** 2)
        result.append({"group": group, "method": method, "label": LABELS[method],
                       "bucket_brier": mean(mean(errors) for errors in cases.values()), **_coverage(rows)})
    return result


def _target_contracts(data):
    rows = []
    for series in KA_SERIES:
        rows.extend(venue_archive.export(data, venue=venue_archive.KALSHI, family="snow", text=series))
    return [row for row in rows if row["series_id"] in KA_SERIES and row["day_basis"] == "contract period start"
            and row["operator"] == "above" and row["units"] == "in" and row["lower"] is not None]


def _last_candle(data, contract, at):
    symbol = contract["instrument_id"]
    with venue_archive.connect(data) as db:
        row = db.execute(
            """SELECT at,receipt_id,body FROM history
            WHERE venue=? AND instrument_id=? AND kind=? AND at>=? AND at<=?
            ORDER BY at DESC LIMIT 1""",
            (venue_archive.KALSHI, symbol, CANDLE_KIND, iso(at - timedelta(minutes=90)), iso(at - timedelta(minutes=1))),
        ).fetchone()
    if row is None:
        return None
    raw = json.loads(row["body"])
    proxy = _probability_proxy(raw, venue_archive.KALSHI)
    if proxy is None:
        return None
    return {**proxy, "source_at": row["at"], "receipt_id": row["receipt_id"], "raw": raw}


def _replay_rows(data, months, estimator, groups):
    saved = {}
    frozen_months = {(row["station"], row["year"], row["month"]): row for row in months}
    for contract in _target_contracts(data):
        first = date.fromisoformat(contract["day"])
        station = contract["station"]
        native_yes = contract["yes_payout"]
        native_source = contract["resolution_source"]
        symbol = contract["instrument_id"]
        observed = frozen_months.get((station, first.year, first.month))
        scalar = native_yes not in ("0", "1", None)
        if native_source not in ("National Weather Service", "NWS Daily Climate Report"):
            status = "Exact NWS settlement terms unqualified"
        elif scalar:
            status = "Scalar payout; physical threshold evidence unavailable"
        elif native_yes is None:
            status = "Missing native payout"
        elif observed is None or observed["official_total"] is None:
            status = "Missing complete official snowfall report"
        else:
            status = "Weather result and binary payout inspected"
        proof = None
        timeline = []
        if observed and observed["official_total"] is not None and native_source in ("National Weather Service", "NWS Daily Climate Report"):
            threshold = decimal(contract["lower"])
            truth = int(decimal(observed["official_total"]) > threshold)
            final = observed["days"][-1]
            expiration = contract["expiration_value"]
            consistent = final and truth == int(native_yes) if native_yes in ("0", "1") else None
            proof = {
                "product": final["product"], "source": final["source"], "published_at": final["published_at"],
                "official_total_inches": observed["official_total"], "sum_of_daily_inches": observed["daily_total"],
                "trace_days": observed["trace_days"], "native_expiration_value": expiration,
                "aligns_with_numeric_expiration_value": decimal(expiration) == decimal(observed["official_total"]) if expiration else None,
                "expected_ordinary_binary_yes": truth, "matches_native_payout": consistent,
            }
            if expiration and proof["aligns_with_numeric_expiration_value"] is False:
                status = "Native expiration differs from latest official report"
            elif consistent is False:
                status = "Native payout differs from latest official report"
            if station in venue_snow_data.CORE_STATIONS:
                for day in range(1, len(observed["days"]) + 1):
                    sample = _record(observed, day, float(threshold), groups[(station, first.month)])
                    if sample is None:
                        continue
                    time = parse_time(sample["at"])
                    if contract["opened_at"] and contract["closed_at"]:
                        live = parse_time(contract["opened_at"]) <= time < parse_time(contract["closed_at"])
                    else:
                        live = False
                    sample["trading_open"] = live
                    sample["market_midpoint_proxy"] = _last_candle(data, contract, time) if live else None
                    timeline.append(sample)
                _predict(timeline, estimator)
        saved[symbol] = {"contract": contract, "status": status, "official_weather": proof, "timeline": timeline}
    return saved


def _write_cases(replays):
    priority = ("KXCHISNOWM-25DEC-8.0", "KXCHISNOWM-25DECB-8.0",
                "KXNYCSNOWM-26JAN-6.0", "KXSEASNOWM-26MAR-6.0", "KXNYCSNOWM-26DEC-8")
    out = []
    for symbol in priority:
        case = replays.get(symbol)
        if case is None:
            continue
        contract = case["contract"]
        official = case["official_weather"] or {}
        timeline = case["timeline"]
        sample = next((row for row in timeline if row["decision_day"] == 8), timeline[0] if timeline else None)
        measurement = f" Official month-end total {official['official_total_inches']} in." if official else ""
        payout = f" Native YES payout ${contract['yes_payout']}." if contract["yes_payout"] is not None else ""
        out.append({
            "venue": venue_archive.KALSHI, "instrument_id": symbol, "station": contract["station"],
            "date": contract["day"], "at": sample["at"] if sample else contract["settlement_at"],
            "title": f"{contract['region'] or contract['station']} · {contract['day'][:7]} snowfall >{contract['lower']} in",
            "detail": case["status"] + "." + measurement + payout,
            "data": case,
        })
    return out


def evaluate(data, corpus):
    data = Path(data)
    frozen_path, frozen_sha = research_model.freeze_inputs(data, corpus)
    groups = _earlier(corpus["months"])
    splits, missing = _sample(corpus["months"], groups)
    estimator = _fit(splits["fit"])
    test = splits["test"]
    _predict(test, estimator)
    replays = _replay_rows(data, corpus["months"], estimator, groups)
    evidence = Counter(replay["status"] for replay in replays.values())
    for replay in replays.values():
        evidence["held-out venue clock pairs"] += sum(bool(row["market_midpoint_proxy"]) for row in replay["timeline"])
    metrics = _score(test, "Held-out official station months")
    for day in CLOCKS:
        metrics += _score([row for row in test if row["decision_day"] == day], f"Day {day}, 18:00 local standard time")
    for station in venue_snow_data.CORE_STATIONS:
        metrics += _score([row for row in test if row["station"] == station], station)
    cases = _write_cases(replays)
    result = {
        "version": VERSION, "title": "Monthly snowfall thresholds and an actual scalar payout",
        "hypothesis": "Published snowfall already accumulated plus historical snowfall in the remaining calendar days "
                      "improves on station-month climatology on later years. A simple logistic model tests for residual gain.",
        "target": "NWS total monthly snowfall in inches and exact strict-above thresholds",
        "venues": ["Kalshi"], "date_window": {"fit years": [*FIT], "test years": [*TEST]},
        "coverage": {"fit": _coverage(splits["fit"]), "test": _coverage(test),
                     "archive_sources": len(corpus["sources"]), "contract_instruments": len(replays),
                     "replay_evidence": dict(evidence)},
        "metrics": metrics, "missing": {**corpus["missing"], **missing}, "cases": cases,
        "limits": [
            "Trace is kept separately and contributes zero inches to strictly positive numeric thresholds. Missing daily "
            "reports terminate the as-of accumulation. New Snowfall and Snow Depth are different source fields.",
            "Monthly weather labels are the final reported NWS monthly totals in the daily product. Some archived annual "
            "rows contain later corrections. They contribute to features only after the retained product publication timestamp.",
            "Native weather results, binary payout, scalar payout and displayed candle clocks are kept separately. "
            "Current Weather Company terms require their own exact station and report verification before a payout model.",
            "The calculation uses no future weather forecast. It estimates snowfall still remaining from earlier months.",
        ],
        "provenance": {"frozen_input": str(frozen_path.relative_to(data)), "frozen_input_sha256": frozen_sha,
                       "scope": {"stations": venue_snow_data.CORE_STATIONS, "months": venue_snow_data.SNOW_MONTHS,
                                 "thresholds_inches": THRESHOLDS, "clock_days": CLOCKS, "hour_local_standard": HOUR_LST},
                       "calendar_fit": "Exclude the same year from each fitting row's historical empirical inputs",
                       "scikit_learn": sklearn_version, "measurement": MEASUREMENT, "contract_terms": TERMS,
                       "source_sha256": research_model.run_sources(sys.modules[__name__], venue_snow_data)},
    }
    return research_model.write_record(data, result, {"forecast_test": test, "market_replays": replays})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--input", type=Path)
    args = parser.parse_args(argv)
    corpus = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else venue_snow_data.load_corpus(args.data)
    print(evaluate(args.data, corpus))


if __name__ == "__main__":
    main()
