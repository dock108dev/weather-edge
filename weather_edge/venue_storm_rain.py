"""Score as-issued accumulation forecasts for the exact New York and Boston rain periods."""

import argparse
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import gzip
from hashlib import file_digest
import json
import math
from pathlib import Path
from statistics import mean
import sys

from sklearn import __version__ as sklearn_version
from sklearn.linear_model import LogisticRegression

from . import expansion_nbm, expansion_weather, research_model, strategy_twc, venue_archive, venue_benchmark, venue_period_rain
from .market import decimal, iso, parse_time
from .venue_contracts import _precipitation_dates

VERSION = "northeast-storm-precipitation-totals-v1"
STATIONS = ("KNYC", "KBOS")
SERIES = ("KXRAINSNYC", "KXRAINSBOS", "KXRAINNYCW")
FIT = (date(2026, 6, 1), date(2026, 8, 31))
TEST = (date(2026, 9, 1), date(2026, 11, 14))
LEVELS = (.5, 1., 1.5, 2., 2.5, 3.)
PHASES = ("Prior day", "After first complete climate day")
METHODS = {"calendar": "Earlier station rainfall base rate", "point": "Raw NOAA Q06 as a binary forecast",
           "residual": "Q06 with earlier empirical forecast error", "logistic": "Earlier threshold logistic adjustment"}


def _qpf(data, station, start, last, issue, decision, *, offset):
    card = expansion_nbm.load_card(data, station, issue)
    if not card or not parse_time(card["issued_at"]) <= parse_time(card["available_at"]) <= decision:
        return None
    zone = timezone(timedelta(hours=offset))
    begin = datetime.combine(start, time.min, zone)
    end = datetime.combine(last + timedelta(days=1), time.min, zone)
    intervals = []
    for row in card["points"]:
        if row.get("Q06") is None or not 0 <= row["Q06"] <= 5000:
            continue
        right = parse_time(row["valid_at"])
        left = right - timedelta(hours=6)
        overlap = max(timedelta(0), min(end, right) - max(begin, left))
        if overlap:
            intervals.append({"at": iso(right), "overlap_hours": overlap.total_seconds()/3600, "qpf_in": row["Q06"]/100,
                              "p06": row.get("P06")})
    if math.isclose(sum(row["overlap_hours"] for row in intervals), (end-begin).total_seconds()/3600):
        return {"issued_at": card["issued_at"], "available_at": card["available_at"], "card_sha256": card["card_sha256"],
                "source": card["source"], "source_product": card["source_product"], "requested_offset_hours": offset,
                "source_path": str(expansion_nbm.card_path(data, station, issue).relative_to(data)),
                "parts": intervals, "inches": sum(row["qpf_in"] * row["overlap_hours"]/6 for row in intervals)}


def _weather_label(raw):
    if not raw:
        return None
    if raw["precip_as_reported"] == "T":
        return {**raw, "inches": 0.0, "trace": True}
    number = raw["precip_as_reported"]
    if isinstance(number, (int, float)) and 0 <= number <= 25:
        return {**raw, "inches": float(number), "trace": False}


def _decision(data, station, start, phase, labels):
    middle = start + timedelta(days=1)
    if phase == PHASES[0]:
        issue = datetime.combine(start - timedelta(days=1), time(12), timezone.utc)
        nominal, first, known = issue + timedelta(hours=6), start, 0.0
    else:
        issue = datetime.combine(middle, time(0), timezone.utc)
        nominal, first = issue + timedelta(hours=9), middle
        prior = _weather_label(labels.get(str(start)))
        if not prior or parse_time(prior["published_at"]) > nominal:
            return None
        known = prior["inches"]
    forecast = _qpf(data, station, first, middle, issue, nominal, offset=-5)
    if not forecast:
        return None
    civil = _qpf(data, station, first, middle, issue, nominal, offset=-4)
    return {"phase": phase, "nominal_at": iso(nominal), "at": iso(nominal + venue_benchmark.WAIT), "observed_in": known,
            "prior_report": _weather_label(labels.get(str(start))) if phase == PHASES[1] else None,
            "forecast": forecast, "civil_sensitivity": civil, "qpf_total": known + forecast["inches"]}


def _historical(data, labels):
    rows, gaps = [], Counter()
    for category, limits in (("fit", FIT), ("held", TEST)):
        for day in range(0, (limits[1]-limits[0]).days, 2):
            start = limits[0] + timedelta(days=day)
            for station in STATIONS:
                official = [_weather_label(labels[station].get(str(start + timedelta(days=offset)))) for offset in (0, 1)]
                if not all(official):
                    gaps[f"{category} / {station} / incomplete official period"] += 1
                    continue
                actual = round(sum(row["inches"] for row in official), 2)
                if category == "fit" and any(row["published_at"] >= f"{TEST[0]}T00:00:00Z" for row in official):
                    gaps["fit official period published after cutoff"] += 1
                    continue
                for phase in PHASES:
                    model = _decision(data, station, start, phase, labels[station])
                    if model:
                        rows.append({**model, "station": station, "start": str(start), "category": category, "amount_in": actual,
                                     "physical_daily_reports": official})
                    else:
                        gaps[f"{category} / {station} / {phase} / missing as-issued Q06 or prior report"] += 1
    return rows, dict(gaps)


def _native(data, labels):
    placeholders = ",".join("?" for _ in SERIES)
    groups, forecast = {}, {}
    twc = {(row["station"], row["label_date"]): row for row in strategy_twc.reports(data, start="2026-09-26", end="2026-10-04")
           if row["station"] in STATIONS}
    with venue_archive.connect(data) as db:
        db.execute("BEGIN")
        markets = [dict(row) for row in db.execute(f"SELECT {venue_archive.PUBLIC_SELECT} FROM markets WHERE venue=? AND series_id IN ({placeholders})",
                                                   (venue_archive.KALSHI, *SERIES))]
        for market in markets:
            station = market["station"]
            first, last = _precipitation_dates(market["rules_primary"], market["rules_secondary"])
            if station not in STATIONS or first is None or last is None:
                continue
            key = (station, first, last)
            offers = groups.get(key)
            if offers is None:
                days = [first + timedelta(days=n) for n in range((last-first).days+1)]
                reports = [twc.get((station, str(day))) for day in days]
                values = [venue_period_rain._twc_daily(row) for row in reports]
                total = str(sum(values, Decimal(0))) if all(value is not None for value in values) else None
                nce = [_weather_label(labels[station].get(str(day))) for day in days]
                offers = {"station": station, "first": str(first), "last": str(last), "twc_sum_in": total,
                          "twc_reports": reports, "nws_reports": nce, "contracts": []}
                groups[key] = offers
                forecast[key] = {phase: _decision(data, station, first, phase, labels[station]) for phase in PHASES} if len(days) == 2 else {}
            prices = {}
            for phase, point in forecast[key].items():
                if point and parse_time(market["opened_at"]) <= parse_time(point["at"]) < parse_time(market["closed_at"]):
                    content, gap = venue_benchmark._history_point(db, market, parse_time(point["nominal_at"]))
                    prices[phase] = content or {"unavailable": gap}
            tape = []
            if market["lower"] == "2.5":
                tape = [dict(row) | {"body": json.loads(row["body"])} for row in db.execute(
                    """SELECT at,native_key,body,kind,receipt_id FROM history WHERE venue=? AND instrument_id=?
                    AND kind IN ('trade','candle-60m') ORDER BY at,native_key""", (venue_archive.KALSHI, market["instrument_id"]),
                )]
            offers["contracts"].append({"market": market, "prices": prices, "native_2_5_tape": tape})
    return [{**rows, "official_forecasts": forecast[key]} for key, rows in sorted(groups.items())]


def gather(data):
    labels, sources = {}, {}
    for station in STATIONS:
        path = expansion_weather.cli_file(data, station, 2026)
        with path.open("rb") as body:
            sources[station] = {"path": str(path.relative_to(data)), "sha256": file_digest(body, "sha256").hexdigest()}
        labels[station] = expansion_weather.daily_labels(data, station, FIT[0], TEST[1])
    rows, gaps = _historical(data, labels)
    return {"version": VERSION, "observations": rows, "calendars": _native(data, labels), "source_receipts": sources, "missing": gaps}


def _features(row, cutoff):
    return [math.log1p(cutoff), math.log1p(row["forecast"]["inches"]), row["observed_in"],
            math.log1p(row["qpf_total"])/(cutoff+.25), float(row["station"] == "KBOS")]


def _fit(corpus):
    earlier = {phase: [row for row in corpus["observations"] if row["category"] == "fit" and row["phase"] == phase] for phase in PHASES}
    models = {}
    for phase in PHASES:
        paired = [(row, level) for row in earlier[phase] for level in LEVELS]
        model = LogisticRegression(C=1, max_iter=500).fit([_features(row, level) for row, level in paired],
                                                          [int(row["amount_in"] > level) for row, level in paired])
        models[phase] = model
    return earlier, models


def _probabilities(row, cutoff, earlier, models):
    prior = earlier[row["phase"]]
    same_station = [item for item in prior if item["station"] == row["station"]]
    residuals = [item["amount_in"] - item["qpf_total"] for item in prior
                 if (item["forecast"]["inches"] >= .35) == (row["forecast"]["inches"] >= .35)]
    calendar = (sum(item["amount_in"] > cutoff for item in same_station)+1)/(len(same_station)+2)
    if row["observed_in"] > cutoff:
        return {"calendar": calendar, "point": 1., "residual": 1., "logistic": 1.}
    residual = (1+sum(max(row["observed_in"], row["qpf_total"] + diff) > cutoff for diff in residuals))/(len(residuals)+2)
    return {"calendar": calendar, "point": float(row["qpf_total"] > cutoff), "residual": residual,
            "logistic": float(models[row["phase"]].predict_proba([_features(row, cutoff)])[0][1])}


def _cases(corpus, earlier, models):
    cases, comparisons = [], []
    for period in corpus["calendars"]:
        station, value = period["station"], period["twc_sum_in"]
        target = "2.5" if period["first"] == "2026-09-26" else "0.5"
        for contract in period["contracts"]:
            market = contract["market"]
            if market["lower"] is None or value is None or market["operator"] != "above":
                continue
            level, native = decimal(market["lower"]), venue_benchmark._binary_result(market)
            actual = int(decimal(value) > level)
            replay = {phase: _probabilities({**row, "station": station}, float(level), earlier, models)
                      for phase, row in period["official_forecasts"].items() if row}
            comparisons.append({"station": station, "date": period["first"], "native_payout": native, "official_result": actual,
                                "twc_total": value, "replay": replay, "native_market": market, "price_points": contract["prices"]})
            if market["lower"] != target:
                continue
            tape = contract["native_2_5_tape"]
            trades = [{"at": row["at"], "yes": float(row["body"]["yes_price_dollars"])} for row in tape
                      if row["kind"] == "trade" and row["body"].get("yes_price_dollars") is not None]
            asks = [{"at": row["at"], "yes": float(price)} for row in tape if row["kind"] == "candle-60m"
                    and (price := venue_archive.kalshi_dollar_close(row["body"].get("yes_ask"))) is not None]
            cases.append({"venue": market["venue"], "instrument_id": market["instrument_id"], "station": station, "date": period["first"],
                          "at": market["settlement_at"], "title": f"{station} accumulated {value} inches across the native reporting days",
                          "detail": f"The strict condition was more than {level} inches. Exact TWC daily amounts sum to {value}. "
                                    f"The native expiration amount was {market['expiration_value']} and YES paid ${market['yes_payout']}.",
                          "venue_price_timeline": {"series": [{"label": label, "kind": kind, "points": points} for label, kind, points in
                              (("Kalshi actual prints", "trade", trades), ("Kalshi hourly YES ask", "display", asks)) if points],
                              "markers": [{"at": market["closed_at"], "label": "Native trading close", "kind": "close"}]},
                          "data": {"native": contract, "twc_reports": period["twc_reports"], "nws_reports": period["nws_reports"],
                                   "forecasts": period["official_forecasts"], "probability_replay": replay}})
    return cases, comparisons


def evaluate(data, corpus):
    frozen, digest = research_model.freeze_inputs(data, corpus)
    earlier, models = _fit(corpus)
    held = [{"outcome": int(row["amount_in"] > level), "threshold_in": level, **row, **_probabilities(row, level, earlier, models)}
            for row in corpus["observations"] if row["category"] == "held" for level in LEVELS]
    scores = []
    for phase in PHASES:
        for label, rows in ((phase, [row for row in held if row["phase"] == phase]),
                            (f"{phase} · above 2.5 inches", [row for row in held if row["phase"] == phase and row["threshold_in"] == 2.5])):
            if rows:
                scores += [{"group": label, "model": name, "bucket_cases": len(rows), "calendar_dates": len({row["start"] for row in rows}),
                            "station_periods": len({(row["start"], row["station"]) for row in rows}),
                            "positive_outcomes": sum(row["outcome"] for row in rows),
                            "brier": mean((row[key] - row["outcome"])**2 for row in rows)} for key, name in METHODS.items()]
    cases, matches = _cases(corpus, earlier, models)
    priced = [(row, phase, point["probability"]) for row in matches for phase, point in row["price_points"].items()
              if row["native_payout"] is not None and phase in row["replay"] and "probability" in point]
    if priced:
        group = "Same verified native cash and displayed price, one storm"
        counts = {"group": group, "bucket_cases": len(priced), "calendar_dates": len({row["date"] for row, _, _ in priced}),
                  "station_periods": len({(row["station"], row["date"]) for row, _, _ in priced})}
        scores += [{**counts, "model": name, "brier": mean((row["replay"][phase][key] - row["native_payout"])**2
                    for row, phase, _ in priced)} for key, name in METHODS.items()]
        scores.append({**counts, "model": "Kalshi coherent candle midpoint", "brier": mean((p-row["native_payout"])**2 for row, _, p in priced)})
    record = {"version": VERSION, "title": "New York and Boston storm-total thresholds",
              "hypothesis": "As-issued Q06 totals with earlier residual calibration improve exact storm-accumulation probabilities.",
              "target": "Strict accumulation thresholds over named reporting days and independently verified native payouts", "venues": ["Kalshi"],
              "date_window": {"earlier calendar dates": list(map(str,FIT)), "untouched calendar dates": list(map(str,TEST))},
              "coverage": {"fit_station_periods_by_phase": {phase: len(rows) for phase, rows in earlier.items()}, "native_accumulation_thresholds": len(matches),
                           "native_exact_periods": len(corpus["calendars"]), "verified_native_payouts": sum(row["native_payout"] is not None for row in matches),
                           "twc_daily_sums_matching_payout": sum(row["native_payout"] == row["official_result"] for row in matches),
                           "native_price_clocks": len(priced)},
              "missing": corpus["missing"], "metrics": scores, "cases": cases,
              "limits": ["The archived station NBM forecasts were issued as dated. The original local receipt is unavailable for retrospective decisions.",
                         "Published NWS gauges supply the forecast target and first-day feature. TWC official reports and native payouts are audited separately.",
                         "The reporting-day boundary is implicit in market terms. Q06 over fixed local-standard time uses overlap weights and retains a civil-day sensitivity.",
                         "A directly published TWC period total would supersede a daily sum. The public dated daily reports support the sole retained numerical proof here.",
                         "The two-day model is evaluated only on exact two-day cohorts. The seven-day contract has an independent weather-to-cash audit."],
              "provenance": {"NWS daily archives": corpus["source_receipts"], "NBM text products": "https://vlab.noaa.gov/web/mdl/nbm-text-products",
                             "thresholds_inches": LEVELS, "sklearn": sklearn_version,"frozen_input": str(frozen.relative_to(data)),
                             "frozen_input_sha256": digest,"source_sha256": research_model.run_sources(sys.modules[__name__], expansion_nbm, venue_period_rain)}}
    return research_model.write_record(data, record, {"native_replay": matches, "held_forecast_replay": held})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--input", type=Path)
    args = parser.parse_args(argv)
    corpus = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else gather(args.data)
    print(evaluate(args.data, corpus))


if __name__ == "__main__":
    main()
