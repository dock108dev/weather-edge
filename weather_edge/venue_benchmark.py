"""Run the matched historical two-venue price and weather probability study."""

import argparse
from collections import Counter, defaultdict
from datetime import date, timedelta
from functools import cache
import gzip
import json
import math
from pathlib import Path
import random
from statistics import mean
import sys

from sklearn import __version__ as sklearn_version
from sklearn.linear_model import LogisticRegression

from . import expansion_weather, research_model, venue_archive, venue_contracts
from .market import iso, parse_time
from .model import _limits, _probability
from .venue_history import CANDLE_KIND, DISPLAY_KIND

VERSION = "venue-weather-benchmark-v1"
FIT_START, FIT_END = "2026-10-01", "2026-10-31"
TEST_START, TEST_END = "2026-11-03", "2026-11-14"
HOURS = (3, 9, 13, 17, 21)
MAX_WIDTH = 0.30
TIGHT_WIDTH = 0.10
DISPLAY_LOOKBACK = timedelta(minutes=2)
WAIT = timedelta(minutes=5)
POLY_PRICE_DOCS = "https://docs.polymarket.us/api-reference/price-history/get-price-history"
KALSHI_CANDLE_DOCS = "https://docs.kalshi.com/api-reference/market/get-market-candlesticks"
METHODS = {"polymarket": "Polymarket US display midpoint", "kalshi": "Kalshi candle midpoint",
           "average": "Simple midpoint average", "combiner": "Earlier-date logistic combiner",
           "noaa": "As-issued NOAA TXN + XND"}


def _source_rows(data, *, kalshi_kind=CANDLE_KIND, days=(FIT_START, TEST_END)):
    with venue_archive.connect(data) as db:
        return [dict(row) for row in db.execute(
            """SELECT venue,instrument_id,family,station,day,title,region,label,units,operator,
            lower,upper,climate_starts_at,climate_ends_at,yes_payout,settlement_received_at,
            settlement_at,settlement_source,resolution_source,report_policy,fallback,rules_primary,rules_secondary,
            rules_url,opened_at,closed_at FROM markets WHERE day BETWEEN ? AND ? AND station IS NOT NULL
            AND family IN ('daily-high','daily-low') AND (
                EXISTS (SELECT 1 FROM history AS h WHERE h.venue=markets.venue AND h.instrument_id=markets.instrument_id
                AND h.kind IN (?,?))
            )""", (*days, kalshi_kind, DISPLAY_KIND),
        )]


def _probability_proxy(raw, venue):
    if venue == "kalshi":
        try:
            bid = float(venue_archive.kalshi_dollar_close(raw["yes_bid"]))
            ask = float(venue_archive.kalshi_dollar_close(raw["yes_ask"]))
        except (TypeError, KeyError, ValueError, AttributeError):
            return None
    else:
        try:
            bid, ask = 1 - float(raw["shortPrice"]), float(raw["longPrice"])
        except (TypeError, KeyError, ValueError):
            return None
    spread = round(ask - bid, 10)
    if not 0 < bid <= ask + 1e-10 < 1 or not 0 <= spread <= MAX_WIDTH:
        return None
    return {"probability": (bid + ask) / 2, "bid": bid, "ask": ask, "width": spread}


def _history_point(db, market, stamp, *, kalshi_kind=CANDLE_KIND):
    venue = market["venue"]
    kind = kalshi_kind if venue == venue_archive.KALSHI else DISPLAY_KIND
    before = stamp if venue == venue_archive.KALSHI else stamp - DISPLAY_LOOKBACK
    picked = db.execute(
        """SELECT at,receipt_id,body FROM history WHERE venue=? AND instrument_id=? AND kind=? AND at>=? AND at<=?
        ORDER BY at DESC LIMIT 1""",
        (venue, market["instrument_id"], kind, iso(before), iso(stamp)),
    ).fetchone()
    if picked is None:
        return None, "missing evidence at scheduled clock"
    stored = json.loads(picked["body"])
    if (proxy := _probability_proxy(stored, venue)) is None:
        return None, "incoherent or unbounded displayed spread"
    return {**proxy, "source_at": picked["at"], "receipt_id": picked["receipt_id"], "raw": stored}, None


def _noaa_forecast(data, station, day, family, decision, archive):
    if family == "daily-high":
        if not archive:
            return None
        forecasts = [{**row, "mean_f": row["high_f"], "sd_f": row.get("high_sd_f")}
                     for row in archive.get("forecasts", []) if row.get("high_source") == "noaa-nbm-nbs-txn"]
    else:
        forecasts = research_model.day_forecasts(data, station, date.fromisoformat(day))
    eligible = [row for row in forecasts if row.get("sd_f") and row["sd_f"] > 0
                and parse_time(row["issued_at"]) <= parse_time(row["available_at"]) <= decision
                and decision - parse_time(row["issued_at"]) <= timedelta(hours=36)]
    return max(eligible, key=lambda row: row["issued_at"], default=None)


def _label(day, family, archive, annual):
    field = "high_f" if family == "daily-high" else "low_f"
    reports = archive.get("official_reports", []) if archive else []
    eligible = [report for report in reports if report.get(field) is not None]
    if eligible:
        latest = max(eligible, key=lambda report: report["available_at"])
        return {"actual_f": latest[field], "publication_at": latest["available_at"],
                "source": latest["source"], "product": latest["product"]}
    summary = annual.get(day)
    if summary and summary.get(field) is not None:
        return {"actual_f": summary[field], "publication_at": summary["published_at"],
                "source": summary["source"], "product": summary["product"]}
    return None


def _binary_result(market):
    payout = market["yes_payout"]
    if payout in ("0", "1"):
        return int(payout)
    return None


def gather(data, *, kalshi_kind=CANDLE_KIND, days=(FIT_START, TEST_END)):
    data = Path(data)
    groups = defaultdict(lambda: {venue: [] for venue in venue_archive.VENUES})
    for market in _source_rows(data, kalshi_kind=kalshi_kind, days=days):
        interval = venue_contracts.temperature_window(market)
        if interval:
            groups[(market["family"], market["station"], market["day"], *interval)][market["venue"]].append(market)

    @cache
    def archive(station, day):
        path = data / "archive" / day / f"{station}.json"
        return json.loads(path.read_text()) if path.is_file() else {}

    @cache
    def annual(station):
        if station in expansion_weather.STATIONS and expansion_weather.cli_file(data, station, 2026).is_file():
            return expansion_weather.daily_labels(data, station, date.fromisoformat(FIT_START), date.fromisoformat(TEST_END))
        return {}

    @cache
    def forecast(station, day, family, decision):
        return _noaa_forecast(data, station, day, family, decision, archive(station, day))

    missing = Counter()
    timeline = []
    with venue_archive.connect(data) as db:
        for (family, station, day, low, high), venues in groups.items():
            if any(len(rows) == 0 for rows in venues.values()):
                continue
            if FIT_END < day < TEST_START:
                continue
            if any(len(rows) != 1 for rows in venues.values()):
                missing["ambiguous native contract"] += len(HOURS)
                continue
            kalshi, poly = venues[venue_archive.KALSHI][0], venues[venue_archive.POLY][0]
            if not poly["climate_starts_at"]:
                missing["missing explicit Polymarket US climate clock"] += len(HOURS)
                continue
            label = _label(day, family, archive(station, day), annual(station))
            if label is None:
                missing["missing weather label"] += len(HOURS)
                continue
            value = label["actual_f"]
            if not float(value).is_integer():
                missing["nonintegral weather label"] += len(HOURS)
                continue
            outcome = int((low is None or value >= low) and (high is None or value <= high))
            for hour in HOURS:
                nominal = parse_time(poly["climate_starts_at"]) + timedelta(hours=hour)
                decision = nominal + WAIT
                if any(not market["opened_at"] or not market["closed_at"] or
                       not parse_time(market["opened_at"]) <= decision < parse_time(market["closed_at"])
                       for market in (kalshi, poly)):
                    missing["outside venue market clock"] += 1
                    continue
                left, left_gap = _history_point(db, poly, nominal)
                right, right_gap = _history_point(db, kalshi, nominal, kalshi_kind=kalshi_kind)
                if left_gap or right_gap:
                    for venue, gap in ((venue_archive.POLY, left_gap), (venue_archive.KALSHI, right_gap)):
                        if gap:
                            missing[f"{venue}: {gap}"] += 1
                    continue
                noaa = forecast(station, day, family, decision)
                noaa_prob = None if noaa is None else _probability(
                    _limits({"lower_f": low, "upper_f": high}), [noaa["mean_f"]], noaa["sd_f"],
                )
                timeline.append({
                    "family": family, "station": station, "date": day, "hour_lst": hour, "at": iso(decision),
                    "bounds": [low, high], "outcome": outcome, "label": label,
                    "polymarket": left["probability"], "kalshi": right["probability"],
                    "average": (left["probability"] + right["probability"]) / 2, "noaa": noaa_prob,
                    "polymarket_price": left, "kalshi_price": right, "forecast": noaa,
                    "polymarket_market": poly, "kalshi_market": kalshi,
                    "poly_payout": _binary_result(poly), "kalshi_payout": _binary_result(kalshi),
                })
    timeline.sort(key=lambda row: (row["date"], row["station"], row["family"], row["at"], row["polymarket_market"]["instrument_id"]))
    return {"version": VERSION, "timeline": timeline, "missing": dict(missing),
            "settings": {"fit": [FIT_START, FIT_END], "test": [TEST_START, TEST_END], "hours_local_standard": HOURS,
                         "max_spread": MAX_WIDTH, "tight_spread": TIGHT_WIDTH,
                         "polymarket_sample_lookback_seconds": DISPLAY_LOOKBACK.total_seconds(),
                         "study_decision_lag_seconds": WAIT.total_seconds()}}


def _logit(probability):
    p = min(1 - 1e-6, max(1e-6, probability))
    return math.log(p / (1 - p))


def _features(row):
    return [_logit(row["polymarket"]), _logit(row["kalshi"])]


def _date_errors(rows, method, target):
    by_day = defaultdict(list)
    for row in rows:
        by_day[row["date"]].append((row[method] - row[target]) ** 2)
    return {day: mean(values) for day, values in by_day.items()}


def _stats(rows):
    return {"bucket_cases": len(rows), "calendar_dates": len({row["date"] for row in rows}),
            "station_days": len({(row["station"], row["date"]) for row in rows}),
            "snapshots": len({(row["station"], row["date"], row["hour_lst"], row["family"]) for row in rows})}


def _scores(rows, methods, group, *, native=False, labels=METHODS):
    if not rows:
        return []
    targets = {"polymarket": "poly_payout", "kalshi": "kalshi_payout"} if native else {}
    return [{"group": group, "method": method, "label": labels[method],
             "bucket_brier": mean(_date_errors(rows, method, targets.get(method, "outcome")).values()), **_stats(rows)}
            for method in methods]


def _paired_interval(rows):
    left = _date_errors(rows, "polymarket", "outcome")
    right = _date_errors(rows, "kalshi", "outcome")
    differences = [left[day] - right[day] for day in sorted(left)]
    if not differences:
        return None
    rng = random.Random(401)
    bootstraps = sorted(mean(rng.choices(differences, k=len(differences))) for _ in range(2000))
    return {"calendar_dates": len(differences), "polymarket_minus_kalshi": mean(differences),
            "descriptive_95_percent_interval": [bootstraps[50], bootstraps[1949]]}


def _tails(rows, methods):
    output = []
    for method in methods:
        for side, transform in (("YES", lambda p, y: (p, y)), ("NO", lambda p, y: (1-p, 1-y))):
            for cutoff in (0.95, 0.99):
                observed = [(row, *transform(row[method], row["outcome"])) for row in rows]
                selection = [record for record in observed if record[1] >= cutoff]
                output.append({"method": method, "side": side, "cutoff": cutoff,
                               **_stats([row for row, _, _ in selection]),
                               "mean_forecast": mean(p for _, p, _ in selection) if selection else None,
                               "observed_rate": mean(y for _, _, y in selection) if selection else None})
    return output


def _case(row):
    left, right = row["polymarket_market"], row["kalshi_market"]
    family = "Daily high" if row["family"] == "daily-high" else "Daily low"
    payouts = ""
    if row["poly_payout"] is not None and row["kalshi_payout"] is not None:
        payouts = f" Public YES payouts were ${row['poly_payout']} and ${row['kalshi_payout']}, respectively."
    return {"venue": venue_archive.POLY, "instrument_id": left["instrument_id"],
            "related_instrument_id": right["instrument_id"], "station": row["station"], "date": row["date"],
            "at": row["at"], "title": f"{family} · {row['station']} · {left['label']}",
            "detail": (f"Polymarket US {row['polymarket']:.0%} and Kalshi {row['kalshi']:.0%} midpoint proxies. "
                       f"The NWS report measured {row['label']['actual_f']:.0f}°F." + payouts),
            "data": row}


def evaluate(data, inputs):
    path, digest = research_model.freeze_inputs(data, inputs)
    rows = inputs["timeline"]
    test = [dict(row) for row in rows if TEST_START <= row["date"] <= TEST_END]
    boundary = min((row["at"] for row in test), default=TEST_START + "T00:00:00Z")
    fit = [row for row in rows if FIT_START <= row["date"] <= FIT_END and row["label"]["publication_at"] < boundary]
    choices = ["polymarket", "kalshi", "average"]
    params = None
    if len({row["outcome"] for row in fit}) == 2:
        daily_counts = Counter(row["date"] for row in fit)
        weights = [len(fit) / len(daily_counts) / daily_counts[row["date"]] for row in fit]
        model = LogisticRegression(C=1.0).fit([_features(row) for row in fit],
                                            [row["outcome"] for row in fit], sample_weight=weights)
        params = {"intercept": model.intercept_.tolist(), "coefficient": model.coef_.tolist(), "C": 1.0}
        if test:
            for row, probabilities in zip(test, model.predict_proba([_features(row) for row in test]), strict=True):
                row["combiner"] = float(probabilities[1])
            choices.append("combiner")
    native_rows = [row for row in test if row["poly_payout"] is not None and row["kalshi_payout"] is not None]
    native_disagreements = [row for row in native_rows if row["poly_payout"] != row["kalshi_payout"]]
    noaa_rows = [row for row in test if row["noaa"] is not None]
    tight_rows = [row for row in test if max(row[venue]["width"] for venue in ("kalshi_price", "polymarket_price")) <= TIGHT_WIDTH]
    metrics = _scores(test, choices, "Common NWS label")
    metrics += _scores(native_rows, ["polymarket", "kalshi"], "Each venue's actual payout", native=True)
    metrics += _scores(noaa_rows, [*choices, "noaa"], "Common NWS label with as-issued NOAA")
    metrics += _scores(tight_rows, choices, "Common NWS label with ≤10¢ widths")
    for hour in HOURS:
        metrics += _scores([row for row in test if row["hour_lst"] == hour], choices, f"{hour:02}:05 local standard time")
    for station in sorted({row["station"] for row in test}):
        metrics += _scores([row for row in test if row["station"] == station], choices, station)
    selected = {}
    ranked = sorted(test, key=lambda row: abs(row["polymarket"] - row["kalshi"]), reverse=True)
    for row in [*native_disagreements, *ranked]:
        key = row["station"], row["date"]
        selected.setdefault(key, _case(row))
        if len(selected) == 7:
            break

    result = {
        "version": VERSION, "title": "Two-venue historical market probabilities",
        "hypothesis": "Aligned prices for exact station, day and whole-degree threshold predict the same official weather. "
                      "Combining both venues may improve the best individual market proxy.",
        "target": "Official NWS daily high or low at matched thresholds",
        "venues": ["Polymarket US", "Kalshi"], "date_window": {"fit": [FIT_START, FIT_END], "test": [TEST_START, TEST_END]},
        "coverage": {"fit": _stats(fit), "test": _stats(test), "native binary payouts": _stats(native_rows),
                     "payout disagreements": _stats(native_disagreements), "with as-issued NOAA": _stats(noaa_rows),
                     "≤10¢ widths": _stats(tight_rows)},
        "limits": ["Provider timestamps describe historical price evidence. Executable size and original local availability are unknown.",
                   "Kalshi terms may use The Weather Company, different revisions, and a fair-price fallback. "
                   "The NWS benchmark scores a common weather target.",
                   "Earlier-date overlap and later independent dates limit calibration support."],
        "metrics": metrics, "missing": inputs["missing"], "cases": list(selected.values()),
        "calibration": _tails(test, choices), "paired_date_bootstrap": _paired_interval(test),
        "provenance": {"frozen_input": str(path.relative_to(data)), "frozen_input_sha256": digest,
                       "settings": inputs["settings"], "combiner": params, "scikit_learn": sklearn_version,
                       "polymarket_docs": POLY_PRICE_DOCS, "kalshi_docs": KALSHI_CANDLE_DOCS,
                       "source_sha256": research_model.run_sources(venue_archive, venue_contracts, sys.modules[__name__])},
    }
    return research_model.write_record(data, result, {"fit": fit, "test": test})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=venue_archive.DEFAULT_DATA)
    parser.add_argument("--inputs", type=Path)
    args = parser.parse_args(argv)
    inputs = json.loads(gzip.decompress(args.inputs.read_bytes())) if args.inputs else gather(args.data)
    result = evaluate(args.data, inputs)
    print(result)


if __name__ == "__main__":
    main()
