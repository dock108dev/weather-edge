"""Score both venues and same-clock NOAA guidance on earlier calendar dates."""

import argparse
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from functools import cache
import gzip
from hashlib import sha256
import json
from pathlib import Path
import sys

from sklearn import __version__ as sklearn_version
from sklearn.linear_model import LogisticRegression

from . import expansion_nbm, expansion_replay, expansion_weather, research_model, strategy_twc
from . import venue_archive, venue_benchmark, venue_snow_data, venue_warmseason
from .market import decimal, iso, parse_time
from .model import _limits, _probability
from .nbm import climate_window
from .venue_contracts import STATION_OFFSETS

VERSION = "warm-season-venue-probabilities-v2"
EARLIER_VERSION = "warm-season-venue-probabilities-v1"
EARLIER = "warm-season-venue-probabilities-v1-db58bc7bba7a9f71"
METHODS = {**venue_benchmark.METHODS, "noaa_25pct": "75% venue average and 25% NOAA",
           "noaa_market_fit": "Earlier logistic on both venue midpoints and NOAA"}
FIT, TEST = ("2026-05-01", "2026-07-31"), ("2026-08-01", "2026-09-30")
REVISION_DAY = date(2026, 8, 29)
REVISION_PRODUCTS = ("202608300824-KMFL-CDUS42-CLIMIA", "202608300910-KMFL-CDUS42-CLIMIA")


def _labels(data, station):
    path = data / "venues" / "snow-climate" / station / "2026.json"
    receipt_path = path.with_suffix(".json.receipt.json")
    if not path.is_file() or not receipt_path.is_file():
        return {}, None
    raw, receipt = path.read_bytes(), json.loads(receipt_path.read_bytes())
    if sha256(raw).hexdigest() != receipt["sha256"]:
        raise ValueError(f"Climate report archive digest mismatch for {station}")
    rows = {}
    for item in json.loads(raw)["results"]:
        label = venue_snow_data._day_row(station, item, offset=STATION_OFFSETS[station])
        if label is None or not FIT[0] <= label["date"] <= TEST[1]:
            continue
        if label["date"] in rows:
            raise ValueError(f"Multiple annual CLI rows for {station} {label['date']}")
        value = label["high_f"]
        if not isinstance(value, (float, int)) or value % 1 or not -70 <= value <= 135:
            continue
        rows[label["date"]] = {"product": label["product"], "source": label["source"],
                               "publication_at": label["published_at"], "actual_f": int(value)}
    return rows, receipt


def _forecast(data, station, day, decision):
    target = date.fromisoformat(day)
    issue = datetime.combine(target, time(12), timezone.utc)
    card = expansion_nbm.load_card(data, station, issue)
    if not card or not parse_time(card["issued_at"]) <= parse_time(card["available_at"]) <= decision:
        return None
    forecast = expansion_replay._high_forecast(card, *climate_window(target, STATION_OFFSETS[station]))
    if forecast and forecast["high_sd_f"] and forecast["high_sd_f"] > 0:
        return {**forecast, "archive": str(expansion_nbm.card_path(data, station, issue).relative_to(data))}
    return None


def _revisions(data):
    path = data / "expansion" / "cli" / "KMIA"
    reports = [expansion_weather.raw_report(data, "KMIA", REVISION_DAY, product)
               for product in REVISION_PRODUCTS if (path / f"{product}.txt").is_file()]
    return {"NWS": [row for row in reports if row],
            "The Weather Company": strategy_twc.reports(data, station="KMIA", day=REVISION_DAY.isoformat())}


def gather(data, hours):
    labels, label_sources = {}, {}
    for station in venue_warmseason.STATIONS:
        labels[station], label_sources[station] = _labels(data, station)

    @cache
    def forecast(station, day, at):
        return _forecast(data, station, day, at)

    timeline, disagreements = [], []
    missing = Counter()
    all_pairs = venue_warmseason.pairs(data)
    with venue_archive.connect(data) as db:
        for pair in all_pairs:
            left, right = pair["polymarket"], pair["kalshi"]
            station, day = pair["station"], pair["day"]
            label = labels[station].get(day)
            if decimal(left["yes_payout"]) != decimal(right["yes_payout"]):
                disagreements.append({**pair, "label": label})
            if not label:
                missing["no official station daily high"] += len(hours)
                continue
            low, high = pair["bounds"]
            verified = int((low is None or label["actual_f"] >= low) and (high is None or label["actual_f"] <= high))
            for hour in hours:
                stamp = parse_time(left["climate_starts_at"]) + timedelta(hours=hour)
                at = stamp + venue_benchmark.WAIT
                if any(not parse_time(contract["opened_at"]) <= at < parse_time(contract["closed_at"]) for contract in (left, right)):
                    missing["one listing outside retained trading hours"] += 1
                    continue
                poly, unavailable_poly = venue_benchmark._history_point(db, left, stamp)
                kalshi, unavailable_kalshi = venue_benchmark._history_point(db, right, stamp)
                if not poly or not kalshi:
                    if unavailable_poly:
                        missing[f"Polymarket US {unavailable_poly}"] += 1
                    if unavailable_kalshi:
                        missing[f"Kalshi {unavailable_kalshi}"] += 1
                    continue
                guidance = forecast(station, day, at)
                probability = None if guidance is None else _probability(
                    _limits({"lower_f": low, "upper_f": high}), [guidance["high_f"]], guidance["high_sd_f"],
                )
                if guidance is None:
                    missing["compatible as-issued NOAA high/spread unavailable"] += 1
                timeline.append({"date": day, "station": station, "family": "daily-high", "hour_lst": hour,
                                 "at": iso(at), "bounds": [low, high], "label": label, "outcome": verified,
                                 "polymarket_market": left, "kalshi_market": right, "polymarket_price": poly, "kalshi_price": kalshi,
                                 "polymarket": poly["probability"], "kalshi": kalshi["probability"],
                                 "average": (poly["probability"] + kalshi["probability"]) / 2,
                                 "poly_payout": venue_benchmark._binary_result(left), "kalshi_payout": venue_benchmark._binary_result(right),
                                 "noaa": probability, "forecast": guidance})
    return {"version": EARLIER_VERSION if tuple(hours) == (13,) else VERSION,
            "timeline": timeline, "catalog_available_pairs": len(all_pairs),
            "catalog_station_days": len({(row["station"], row["day"]) for row in all_pairs}),
            "unmatched_historical_clocks": dict(missing), "payout_disagreements": disagreements,
            "actual_revision_sources": _revisions(data), "climate_receipts": label_sources, "hours": list(hours)}


def _fit(fit, later):
    if len({row["outcome"] for row in fit}) != 2:
        return None
    counts = Counter(row["date"] for row in fit)
    weights = [len(fit) / len(counts) / counts[row["date"]] for row in fit]
    model = LogisticRegression(C=1).fit([venue_benchmark._features(row) for row in fit],
                                       [row["outcome"] for row in fit], sample_weight=weights)
    if later:
        probabilities = model.predict_proba([venue_benchmark._features(row) for row in later])
        for row, (_, p) in zip(later, probabilities, strict=True):
            row["combiner"] = float(p)
    return {"intercept": model.intercept_.tolist(), "coefficients": model.coef_.tolist(), "regularization_C": 1}


def _noaa_blend(fit, later):
    with_noaa = [row for row in fit if row["noaa"] is not None]
    if len({row["outcome"] for row in with_noaa}) != 2 or not later:
        return None
    features = lambda row: [*venue_benchmark._features(row), venue_benchmark._logit(row["noaa"])]
    counts = Counter(row["date"] for row in with_noaa)
    weights = [len(with_noaa) / len(counts) / counts[row["date"]] for row in with_noaa]
    model = LogisticRegression(C=1).fit([features(row) for row in with_noaa], [row["outcome"] for row in with_noaa],
                                       sample_weight=weights)
    for row, (_, estimate) in zip(later, model.predict_proba([features(row) for row in later]), strict=True):
        row["noaa_market_fit"] = float(estimate)
        row["noaa_25pct"] = 0.75 * row["average"] + 0.25 * row["noaa"]
    return {"intercept": model.intercept_.tolist(), "coefficients": model.coef_.tolist(), "regularization_C": 1}


def _disagreement_cases(corpus):
    result = []
    for row in corpus["payout_disagreements"]:
        left, right = row["polymarket"], row["kalshi"]
        evidence = corpus["actual_revision_sources"] if row["day"] == REVISION_DAY.isoformat() and row["station"] == "KMIA" else None
        detail = f"Kalshi YES paid ${right['yes_payout']}. Polymarket US YES paid ${left['yes_payout']}."
        if evidence and len(evidence["NWS"]) == 2:
            first, revision = evidence["NWS"]
            detail += f" Complete NWS reports changed from {first['high_f']}°F at 08:24 UTC to {revision['high_f']}°F at 09:10 UTC."
        if evidence and evidence["The Weather Company"]:
            source = evidence["The Weather Company"][0]
            if source["temperature_high_f"] is not None:
                detail += f" The retained Weather Company label was {source['temperature_high_f']:g}°F."
        result.append({"venue": venue_archive.KALSHI, "instrument_id": right["instrument_id"], "date": row["day"],
                       "station": row["station"], "at": right["settlement_at"],
                       "title": f"{row['station']} · {right['label']} · different native payouts", "detail": detail,
                       "data": {**row, "source_revisions": evidence}})
    return result


def evaluate(data, corpus):
    fit = [row for row in corpus["timeline"] if FIT[0] <= row["date"] <= FIT[1]
           and row["label"]["publication_at"] < TEST[0] + "T00:00:00Z"]
    test = [dict(row) for row in corpus["timeline"] if TEST[0] <= row["date"] <= TEST[1]]
    params = _fit(fit, test)
    methods = ["polymarket", "kalshi", "average"] + (["combiner"] if params else [])
    noaa = [row for row in test if row["noaa"] is not None]
    blended = _noaa_blend(fit, noaa) if corpus["version"] == VERSION else None
    supported = [*methods, "noaa"] + (["noaa_25pct", "noaa_market_fit"] if blended else [])
    tight = [row for row in test if max(row[source]["width"] for source in ("polymarket_price", "kalshi_price")) <= .10]
    native = [row for row in test if row["poly_payout"] is not None and row["kalshi_payout"] is not None]
    ranked = sorted(test, key=lambda row: abs(row["polymarket"] - row["kalshi"]), reverse=True)
    metric = venue_benchmark._scores
    metrics = metric(test, methods, "Same later NWS labels") + metric(native, methods[:2], "Each venue's native payout", native=True)
    metrics += metric(noaa, supported, "Same later NWS labels with as-issued NOAA", labels=METHODS)
    metrics += metric(tight, methods, "Same later NWS labels with at most 10¢ widths")
    for source in sorted({row["kalshi_market"]["resolution_source"] or "unspecified" for row in test}):
        matched = [row for row in test if (row["kalshi_market"]["resolution_source"] or "unspecified") == source]
        metrics += metric(matched, methods, source + " Kalshi terms")
    clock_coverage, same_threshold_coverage = {}, {}
    if corpus["version"] == VERSION:
        by_instrument = defaultdict(set)
        for row in noaa:
            by_instrument[row["polymarket_market"]["instrument_id"]].add(row["hour_lst"])
        common = {instrument for instrument, clocks in by_instrument.items() if clocks == set(corpus["hours"])}
        for hour in corpus["hours"]:
            group = [row for row in noaa if row["hour_lst"] == hour]
            both = [row for row in group if row["polymarket_market"]["instrument_id"] in common]
            clock_coverage[hour] = {**venue_benchmark._stats(group), "paired_venue_bootstrap": venue_benchmark._paired_interval(group)}
            same_threshold_coverage[hour] = venue_benchmark._stats(both)
            metrics += metric(group, supported, f"{hour:02}:05 local standard time with NOAA", labels=METHODS)
            metrics += metric(both, supported, f"Same threshold at every clock, {hour:02}:05 local standard time", labels=METHODS)
    seen = set()
    examples = _disagreement_cases(corpus)
    for row in ranked:
        key = (row["station"], row["date"])
        if key not in seen:
            examples.append(venue_benchmark._case(row))
            seen.add(key)
        if len(seen) == 4:
            break
    frozen, digest = research_model.freeze_inputs(data, corpus)
    result = {"version": corpus["version"], "title": "Warm-season daily high prices and a real reporting revision",
              "hypothesis": "November's small venue difference may change across months. Earlier prices and as-issued NOAA "
                            "are tested on identical later station-day thresholds.",
              "target": "Whole-degree NWS daily high and separate native results", "venues": ["Polymarket US", "Kalshi"],
              "date_window": {"fit": FIT, "untouched": TEST},
              "coverage": {"full catalog matched pairs": corpus["catalog_available_pairs"],
                           "full catalog station-days": corpus["catalog_station_days"], "fit": venue_benchmark._stats(fit),
                           "test": venue_benchmark._stats(test), "with as-issued NOAA": venue_benchmark._stats(noaa),
                           "narrow-spread cases": venue_benchmark._stats(tight),
                           "all catalog verified payout differences": len(corpus["payout_disagreements"]),
                           "matched payout differences": sum(row["poly_payout"] != row["kalshi_payout"] for row in native)},
              "metrics": metrics, "calibration": venue_benchmark._tails(test, methods),
              "paired_date_bootstrap": venue_benchmark._paired_interval(test), "cases": examples,
              "missing": corpus["unmatched_historical_clocks"],
              "limits": ["The exchange clocks are provider candle and display clocks. Original book receipts and executable sizes are unavailable.",
                         "The serialized NWS annual label can reflect a revision. The two native payouts and dated source reports remain separate.",
                         "NOAA NBS is the single earlier 12 UTC issuance with published Last-Modified and issue. Score comparisons use the same supported rows.",
                         "Nearby observations and weather-model skill do not make the native temperature contracts interchangeable."],
              "provenance": {"frozen_input": str(frozen.relative_to(data)), "frozen_input_sha256": digest,
                             "price_clocks_local_standard": corpus["hours"], "decision_clock_minutes_later": 5,
                             "station_climate_sources": corpus["climate_receipts"], "combiner_parameters": params,
                             "scikit_learn": sklearn_version,
                             "source_sha256": research_model.run_sources(sys.modules[__name__], venue_warmseason, venue_benchmark)}}
    if corpus["version"] == VERSION:
        result["supersedes"] = EARLIER
        result["coverage"].update(clock_support=clock_coverage, same_threshold_at_all_clocks=same_threshold_coverage)
        result["provenance"]["weather_blend_parameters"] = blended
        result["limits"].append("The 12 UTC forecast remains fixed across earlier and later price clocks. Later agency issues could change attribution.")
    return research_model.write_record(data, result, {"market_weather_fit": fit, "market_weather_test": test,
                                                      "cash_payout_disagreements": corpus["payout_disagreements"],
                                                      "same_day_source_revisions": corpus["actual_revision_sources"]})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--input", type=Path)
    parser.add_argument("--hour", type=int, action="append", choices=venue_warmseason.CLOCKS)
    args = parser.parse_args(argv)
    corpus = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else gather(args.data, args.hour or venue_warmseason.CLOCKS)
    print(evaluate(args.data, corpus))


if __name__ == "__main__":
    main()
