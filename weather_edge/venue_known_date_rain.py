"""Replay Election Day and July Fourth rain against exact station-day evidence."""

import argparse
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
import gzip
import json
from pathlib import Path
from statistics import mean
import sys

from . import research_model, research_model_rain_windows, strategy_twc, venue_archive, venue_benchmark, venue_weekend_rain
from .market import iso, parse_time
from .nbm import climate_window
from .venue_contracts import STATION_OFFSETS

VERSION = "exact-election-and-july-fourth-rain-v1"
PERIODS = (("KXRAINHOLIDAY", "2026-07-04"), ("KXRAINELECTION", "2026-11-03"))
EXAMPLES = {"KXRAINELECTION-03NOV26-PHX", "KXRAINELECTION-03NOV26-DSM"}
MODEL = {"union6": "NOAA six-hour union under independence", "union12": "NOAA twelve-hour union under independence",
         "max6": "Largest individual NOAA six-hour probability", "market": "Kalshi coherent candle midpoint"}


def _weather(card, station, target, decision):
    if not card or station not in STATION_OFFSETS or not parse_time(card["available_at"]) <= decision:
        return None
    start, end = climate_window(target, STATION_OFFSETS[station])
    if not parse_time(card["issued_at"]) <= parse_time(card["available_at"]) <= decision < start:
        return None
    six = research_model_rain_windows._window(card, start, end, 6)
    twelve = research_model_rain_windows._window(card, start, end, 12)
    if six is None or twelve is None:
        return None
    return {"union6": six["probability"], "union12": twelve["probability"], "max6": six["peak_probability"],
            "source_at": iso(start), "end_at": iso(end), "forecast_at": card["issued_at"], "available_at": card["available_at"],
            "archive_received_at": card["received_at"], "source": card["source"], "card_sha256": card["card_sha256"],
            "six_hour_windows": six["intervals"], "twelve_hour_windows": twelve["intervals"]}


def _market_rows(data, series):
    with venue_archive.connect(data) as db:
        return [dict(row) for row in db.execute(f"SELECT {venue_archive.PUBLIC_SELECT} FROM markets WHERE venue=? AND series_id=? ORDER BY station,instrument_id",
                                               (venue_archive.KALSHI, series))]


def _native(data, markets, stamp):
    with venue_archive.connect(data) as db:
        result = {}
        for market in markets:
            if not parse_time(market["opened_at"]) <= stamp < parse_time(market["closed_at"]):
                continue
            quote, gap = venue_benchmark._history_point(db, market, stamp)
            result[market["instrument_id"]] = quote or {"unavailable": gap}
    return result


def gather(data, *, collect=False):
    evidence, weather_sources, missing = [], {}, Counter()
    for series, key in PERIODS:
        day = date.fromisoformat(key)
        markets = [row for row in _market_rows(data, series) if row["day"] == key]
        stations = {market["station"] for market in markets if market["station"]}
        twc = {row["station"]: row for row in strategy_twc.reports(data, day=key) if row["station"] in stations}
        decisions = (("Previous afternoon", datetime.combine(day-timedelta(days=1), time(12), timezone.utc), 6),
                     ("Before local midnight", datetime.combine(day, time(0), timezone.utc), 3))
        values = {row["instrument_id"]: {"market": row, "period": key, "resolution_report": twc.get(row["station"]),
                  "weather_rain": venue_weekend_rain._rain(twc.get(row["station"])), "decision_prices": {}, "forecasts": {}} for row in markets}
        for label, issue, lag in decisions:
            cards, receipt = venue_weekend_rain._cards(data, issue, stations, collect)
            weather_sources[iso(issue)] = receipt
            decision = issue + timedelta(hours=lag, minutes=5)
            native = _native(data, markets, decision - timedelta(minutes=5))
            for item in values.values():
                market = item["market"]
                forecast = _weather(cards.get(market["station"]), market["station"], day, decision)
                if not forecast:
                    missing[f"{key} / {label} / complete NOAA fixed-time window"] += 1
                else:
                    item["forecasts"][label] = forecast
                item["decision_prices"][label] = native.get(market["instrument_id"])
        evidence.extend(values.values())
    tapes = {}
    with venue_archive.connect(data) as db:
        for instrument in sorted(EXAMPLES):
            tapes[instrument] = [dict(row) | {"body": json.loads(row["body"])} for row in db.execute(
                """SELECT at,kind,receipt_id,native_key,body FROM history WHERE venue=? AND instrument_id=?
                AND at>='2026-11-02' AND kind IN ('trade','candle-60m') ORDER BY at,native_key""", (venue_archive.KALSHI, instrument),
            )]
    return {"version": VERSION, "evidence": evidence, "source_receipts": weather_sources, "missing": dict(missing), "native_tapes": tapes}


def _evaluation(items):
    comparisons = []
    for case in items:
        market = case["market"]
        payout = venue_benchmark._binary_result(market)
        if payout is None or case["weather_rain"] is None:
            continue
        for clock, forecast in case["forecasts"].items():
            quote = case["decision_prices"].get(clock)
            comparisons.append({"period": case["period"], "station": market["station"], "clock": clock, "native_payout": payout,
                                "observed_weather": int(case["weather_rain"]), **forecast, "market": quote.get("probability") if quote else None})
    scores = []
    for _, date_key in PERIODS:
        for clock in ("Previous afternoon", "Before local midnight"):
            cohort = [row for row in comparisons if row["period"] == date_key and row["clock"] == clock]
            for label, keep in (("Physical source on all covered stations", cohort),
                                ("Native cash on the same coherent quote clocks", [row for row in cohort if row["market"] is not None])):
                if not keep:
                    continue
                target = "native_payout" if label.startswith("Native") else "observed_weather"
                names = MODEL if target == "native_payout" else {key: MODEL[key] for key in ("union6", "union12", "max6")}
                scores += [{"group": f"{date_key} · {clock} · {label}", "model": name, "bucket_cases": len(keep),
                            "station_days": len({row["station"] for row in keep}), "calendar_dates": 1, "rain_cases": sum(row[target] for row in keep),
                            "brier": mean((row[key]-row[target])**2 for row in keep)} for key, name in names.items()]
    return scores, comparisons


def _examples(corpus):
    result = []
    for case in corpus["evidence"]:
        market = case["market"]
        ticker = market["instrument_id"]
        if ticker not in EXAMPLES:
            continue
        original = corpus["native_tapes"][ticker]
        trade = [{"at": row["at"], "yes": float(row["body"]["yes_price_dollars"])} for row in original
                 if row["kind"] == "trade" and row["body"].get("yes_price_dollars") is not None]
        hourly = [{"at": row["at"], "yes": float(price)} for row in original if row["kind"] == "candle-60m"
                  and (price := venue_archive.kalshi_dollar_close(row["body"].get("yes_ask"))) is not None]
        if ticker.endswith("-PHX"):
            title = "Phoenix's native scalar payout is separate from the missing weather result"
            detail = f"The retained Weather Company archive has no qualifying November 3 Phoenix rain amount. The exchange paid ${market['yes_payout']} for YES and ${market['no_payout']} for NO. The native record has no numeric expiration amount."
        else:
            title = "Des Moines reported measurable Election Day rain"
            amount = case["resolution_report"]["precipitation_in"] if case["resolution_report"] else "unavailable"
            detail = f"The exact station report shows {amount} inches. Native YES paid ${market['yes_payout']} for the strict positive amount."
        result.append({"venue": market["venue"], "instrument_id": ticker, "station": market["station"], "date": case["period"],
                       "at": market["settlement_at"], "title": title, "detail": detail,
                       "venue_price_timeline": {"series": [{"label": label, "kind": category, "points": points} for label, category, points in
                            (("Kalshi actual trades", "trade", trade), ("Kalshi hourly YES ask", "display", hourly)) if points],
                            "markers": [{"at": market["closed_at"], "label": "Native trading close", "kind": "close"}]},
                       "data": case | {"native_zone": "Provider-defined calendar reporting day", "tape": original}})
    return sorted(result, key=lambda item: item["instrument_id"] not in {"KXRAINELECTION-03NOV26-PHX"})


def evaluate(data, corpus):
    frozen, digest = research_model.freeze_inputs(data, corpus)
    items = corpus["evidence"]
    scores, comparisons = _evaluation(items)
    native_counts = {}
    for _, key in PERIODS:
        cases = [row for row in items if row["period"] == key]
        supported = [row for row in cases if row["weather_rain"] is not None and venue_benchmark._binary_result(row["market"]) is not None]
        native_counts[key] = {"instruments": len(cases), "source_and_binary_payout": len(supported),
                              "source_matches_binary_payout": sum(int(row["weather_rain"]) == venue_benchmark._binary_result(row["market"]) for row in supported),
                              "scalar_payouts": sum(row["market"]["yes_payout"] not in (None, "0", "1") for row in cases),
                              "explicit_weather_trace": sum((row["resolution_report"] or {}).get("precipitation_in") == "T" for row in cases)}
    result = {"version": VERSION, "title": "Measurable rain on Election Day and July Fourth",
              "hypothesis": "Same-date NOAA issuance improves exact station rain probabilities and the provider amount predicts binary native cash.",
              "target": "Strictly positive station-day precipitation and actual fixed-point cash payouts", "venues": ["Kalshi"],
              "date_window": {"native event weather dates": [date for _, date in PERIODS]},
              "coverage": {"by_weather_date": native_counts, "forecast_checks": len(comparisons)}, "missing": corpus["missing"],
              "metrics": scores, "cases": _examples(corpus),
              "limits": ["The two event dates have dependent station weather. Counts convey geographic coverage rather than independent weather years.",
                         "The event rules specify exact source dates without UTC endpoints. The forecast uses fixed local-standard-time intervals.",
                         "Native scalar cash is independent of modeled binary rain and of any missing-weather convention.",
                         "The source-issued marks and original observed local quote receipts are different clocks. Historical price rows do not provide a fill."],
              "provenance": {"forecast_source": venue_weekend_rain.SOURCE_DOC, "exact_source": venue_weekend_rain.TWC,
                             "frozen_input": str(frozen.relative_to(data)), "frozen_input_sha256": digest,
                             "source_sha256": research_model.run_sources(sys.modules[__name__], venue_weekend_rain)}}
    return research_model.write_record(data, result, {"comparisons": comparisons, "audited_settlements": items})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--collect", action="store_true")
    parser.add_argument("--input", type=Path)
    args = parser.parse_args(argv)
    corpus = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else gather(args.data, collect=args.collect)
    print(evaluate(args.data, corpus))


if __name__ == "__main__":
    main()
