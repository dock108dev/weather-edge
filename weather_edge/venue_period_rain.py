"""Test airport precipitation across native monthly and multi-month periods."""

import argparse
from calendar import monthrange
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import gzip
import json
from math import exp
from pathlib import Path
from statistics import mean
import sys

from . import research_model, strategy_twc, venue_archive, venue_ghcn, venue_storm
from .market import decimal, iso, parse_time
from .venue_contracts import _precipitation_dates
from .venue_sources import CollectionPaused

VERSION = "multi-month-airport-rain-v2"
EARLIER = "multi-month-airport-rain-v1-94c81b1965d6bca5"
TERMS = "https://assets.kalshi.com/contract_terms/RAINGLOBALPERIOD.pdf"
GHCND = "https://www.ncei.noaa.gov/pub/data/ghcn/daily/"
ISSUE_CONTEXT = "https://www.cpc.ncep.noaa.gov/products/predictions/90day/fxus05.html"
RONI_NAME = Path("roni-ascii-20261115.txt")
STATIONS = {"KAPC": "USW00093227", "KGTF": "USW00024143"}
PHASES = (0, 14, 29, 59)
FIT_END = {"KAPC": 2012, "KGTF": 2004}
ANALOG_WIDTH = 0.75
METHODS = {"climate": "Earlier fixed-period climate", "observed": "Observed accumulation and earlier remainder",
           "enso": "Observed accumulation and earlier ENSO analogs"}


def _gauge(data, station, collect):
    identity = STATIONS[station]
    path = Path(identity + ".dly")
    source = GHCND + "all/" + identity + ".dly"
    try:
        raw, original = (venue_storm._get(data, path, source, folder="period-rain") if collect
                         else venue_storm._existing(data, path, folder="period-rain"))
    except FileNotFoundError:
        return {}, None, Counter()
    rows, flags = {}, Counter()
    for day, value, measure, qual, source_flag in venue_ghcn.daily_entries(raw, identity, "PRCP"):
        if value < 0 or qual != " " or measure in {"P", "I"} or source_flag == " ":
            flags["missing or ambiguous"] += 1
            continue
        if measure == "A":
            flags["multi-day total excluded"] += 1
            continue
        if day in rows:
            raise ValueError(f"Duplicate station climate day {day}")
        rows[day] = {"tenths_mm": value, "trace": measure == "T", "measurement_flag": measure, "source_flag": source_flag}
    return rows, original, flags


def _enso(data):
    try:
        raw, metadata = venue_storm._existing(data, RONI_NAME, folder="enso")
    except FileNotFoundError:
        return {}, None
    values = {}
    for line in raw.decode("ascii").splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[1].isdigit():
            season, year, value = parts
            year, value = int(year), float(value)
            if abs(value) <= 5:
                values[(year, season)] = value
    return values, metadata


def _enso_id(start):
    return {3: (start.year - 1, "NDJ"), 5: (start.year, "JFM"), 10: (start.year, "JJA"),
            11: (start.year, "JAS"), 12: (start.year, "JAS")}[start.month]


def _retrospective(rows, first, last):
    days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    if any(day not in rows for day in days):
        return None
    totals = [rows[day]["tenths_mm"] for day in days]
    return {"total_tenths_mm": sum(totals), "total_inches": float(Decimal(sum(totals)) / 254),
            "observed_inches": {str(n): float(Decimal(sum(totals[:n])) / 254) for n in PHASES if n < len(totals)},
            "dated_trace_count": sum(rows[day]["trace"] for day in days), "days": len(days),
            "source_flags": dict(Counter(rows[day]["source_flag"] for day in days))}


def _twc_daily(row):
    if row is None or row["provider_status"] not in ("official", "revised"):
        return None
    if row["precipitation_in"] == "T":
        return Decimal(0)
    try:
        amount = decimal(row["precipitation_in"])
    except ValueError:
        return None
    return amount if amount >= 0 else None


def _twc_window(rows, station, first, last, as_of):
    eligible = []
    for index in range((last - first).days + 1):
        day = first + timedelta(days=index)
        if day >= as_of.date():
            break
        report = rows.get((station, str(day)))
        if report is None or parse_time(report["available_at"]) > as_of or (amount := _twc_daily(report)) is None:
            break
        eligible.append({"day": str(day), "inches": str(amount), "original_raw": report})
    value = sum((decimal(row["inches"]) for row in eligible), Decimal(0))
    return {"days": len(eligible), "total_inches": str(value), "complete": len(eligible) == (last - first).days + 1,
            "daily_records": eligible}


def _market_rows(data):
    with venue_archive.connect(data) as db:
        return [venue_archive._market(row) for row in db.execute(
            f"SELECT {venue_archive.PUBLIC_SELECT} FROM markets WHERE venue=? AND family='period-rain' "
            "ORDER BY station,day,event_id,instrument_id", (venue_archive.KALSHI,))]


def _groups(data, cutoff):
    groups = {}
    for market in _market_rows(data):
        first, last = _precipitation_dates(market["rules_primary"], market["rules_secondary"])
        if not first or not last or market["station"] not in STATIONS:
            continue
        station = market["station"]
        handle = station, first, last
        current = groups.setdefault(handle, {"station": station, "first": str(first), "last": str(last), "markets": [], "depth": {}})
        current["markets"].append(market)
        books = venue_archive.books(data, venue_archive.KALSHI, market["instrument_id"], limit=10, end=cutoff)
        candidates = [book for book in books if book.get("depth_kind") == "displayed_ladder"]
        if candidates:
            book = candidates[0]
            current["depth"][market["instrument_id"]] = {key: book.get(key) for key in (
                "at", "source_at", "receipt_id", "source", "yes_bids", "yes_offers", "no_bids", "no_offers")}
    return groups


def gather(data, *, collect=False, as_of=None):
    as_of = as_of or datetime.now(timezone.utc)
    groups = _groups(data, as_of)
    raw_daily, raw_sources, gaps = {}, {}, {}
    for station in STATIONS:
        raw_daily[station], receipt, flags = _gauge(data, station, collect)
        raw_sources[station] = receipt
        gaps[station] = dict(flags)
    enso, enso_receipt = _enso(data)
    twc_rows = {(row["station"], row["label_date"]): row for row in strategy_twc.reports(
        data, start="2026-10-01", end=str(as_of.date()), as_of=as_of)}
    catalog, seasons, sampled_periods = [], [], set()
    for (station, first, last), current in groups.items():
        start_month, end_month = first.month, last.month
        handle = station, start_month, end_month
        for year in (() if handle in sampled_periods else range(1951, 2026)):
            days = raw_daily[station]
            start = date(year, start_month, 1)
            end_year = year + (end_month < start_month)
            end = date(end_year, end_month, monthrange(end_year, end_month)[1])
            if end >= date(2026, 5, 1):
                continue
            weather = _retrospective(days, start, end)
            if weather is None:
                continue
            key_year, season = _enso_id(start)
            seasons.append({"handle": [station, start_month, end_month], "start": str(start), "end": str(end),
                            "year": year, "enso_index": enso.get((key_year, season)),
                            "enso_period": f"{season} {key_year}", **weather})
        sampled_periods.add(handle)
        key_year, season = _enso_id(first)
        # The missing future feature stays missing, including all spring 2027
        # periods that require as-yet unobserved seasons.
        live_enso = enso.get((key_year, season)) if key_year <= as_of.year else None
        current["enso_index"] = live_enso
        current["enso_period"] = f"{season} {key_year}"
        current["source_days"] = _twc_window(twc_rows, station, first, last, as_of)
        if last < as_of.date():
            current["retrospective_noaa"] = _retrospective(raw_daily[station], first, last)
        catalog.append({**current, "handle": [station, start_month, end_month]})
    return {"version": VERSION, "as_of": iso(as_of), "catalog": catalog, "historical_seasons": seasons, "gauge_quality": gaps,
            "raw_receipts": raw_sources, "enso_receipt": enso_receipt}


def _prob(analog, crossing, weights=None):
    if weights is None:
        weights = [1.0] * len(analog)
    return (sum(weight for value, weight in zip(analog, weights, strict=True) if value > crossing) + 1) / (sum(weights) + 2)


def _forecast(earlier, phase, observed, value, strike):
    if len(earlier) < 8:
        return {}
    baseline = [row["total_inches"] for row in earlier]
    remainder = [(row, row["total_inches"] - row["observed_inches"][str(phase)]) for row in earlier
                 if str(phase) in row["observed_inches"]]
    analog = [tail for row, tail in remainder]
    out = {"climate": _prob(baseline, strike), "observed": _prob(analog, strike - observed)}
    if value is not None:
        valid = [(tail, exp(-0.5 * ((item["enso_index"] - value) / ANALOG_WIDTH) ** 2)) for item, tail in remainder
                 if item["enso_index"] is not None]
        if valid:
            outcomes, weights = zip(*valid, strict=True)
            # Normalize before smoothing so weak analog support does not
            # artificially pull the estimate toward 50%.
            weight_sum = sum(weights)
            if weight_sum > 0.01:
                out["enso"] = _prob(outcomes, strike - observed, [weight * len(valid) / weight_sum for weight in weights])
    return out


def _case(group, market, detail, *, book=None, projected=None):
    return {"venue": market["venue"], "instrument_id": market["instrument_id"], "station": market["station"], "date": group["first"],
            "at": (book or {}).get("at") or market["settlement_at"], "title": market["title"], "detail": detail,
            "data": {"native_contract": market, "source_days": group["source_days"], "noaa_weather": group.get("retrospective_noaa"),
                     "book": book, "projected": projected}}


def evaluate(data, corpus):
    frozen, digest = research_model.freeze_inputs(data, corpus)
    histories = defaultdict(list)
    for row in corpus["historical_seasons"]:
        histories[tuple(row["handle"])].append(row)
    predictions, cases, current, payout_checks, tested = [], [], [], [], set()
    for group in corpus["catalog"]:
        station = group["station"]
        handle = tuple(group["handle"])
        historical = histories[handle]
        fit = [row for row in historical if row["year"] <= FIT_END[station]]
        hold = [row for row in historical if row["year"] > FIT_END[station]]
        for market in group["markets"]:
            threshold = float(decimal(market["lower"]))
            for frame in (() if (handle, threshold) in tested else hold):
                for phase in PHASES:
                    obs = frame["observed_inches"].get(str(phase))
                    if obs is None:
                        continue
                    chance = _forecast(fit, phase, obs, frame["enso_index"], threshold)
                    if len(chance) == len(METHODS):
                        predictions.append({"period": handle, "start": frame["start"], "year": frame["year"],
                                            "phase": phase, "threshold": threshold, "case_total_inches": frame["total_inches"],
                                            "outcome": frame["total_inches"] > threshold, "predictions": chance,
                                            "fit_periods": len(fit), "id": market["instrument_id"]})
            tested.add((handle, threshold))
            cash = market["yes_payout"]
            weather = group["source_days"]
            if cash in ("0", "1") and weather["complete"]:
                implied = int(decimal(weather["total_inches"]) > decimal(market["lower"]))
                payout_checks.append({"instrument_id": market["instrument_id"], "station": station, "start": group["first"], "end": group["last"],
                                      "actual": int(cash), "complete_daily_source_implies": implied,
                                      "source_total_inches": weather["total_inches"], "native_expiration_value": market["expiration_value"]})
                if threshold == 1 or implied != int(cash):
                    cases.append(_case(group, market, f"All {weather['days']} official source days sum to {weather['total_inches']} inches. "
                                       f"Actual native YES paid ${cash}. The native expiration label is {market['expiration_value']}."))
            elif cash is None and market["status"] == "open":
                phase = max(n for n in PHASES if n <= weather["days"])
                early_history = [row for row in historical if row["year"] < date.fromisoformat(group["first"]).year]
                observed = (float(decimal(weather["total_inches"])) if phase == weather["days"]
                            else sum(float(decimal(row["inches"])) for row in weather["daily_records"][:phase]))
                projected = _forecast(early_history, phase, observed, group["enso_index"], threshold)
                book = group["depth"].get(market["instrument_id"])
                current.append({"station": station, "period": handle, "instrument_id": market["instrument_id"],
                                "prior_reported_inches": weather["total_inches"], "days_received": weather["days"],
                                "calibration_phase_days": phase, "forecasts": projected, "book_reference": book})
                if market["instrument_id"] in ("KXRAINNAPAM-01NOV26-31MAR27-T25", "KXRAINGTF-01MAY27-30JUN27-T5"):
                    cases.append(_case(group, market, f"{weather['days']} completed provider reporting days had {weather['total_inches']} inches. "
                                       "The forecast uses historical airport totals. Native payout is pending.", book=book, projected=projected))
    metrics = []
    for label, subset in (("All date-held airport periods", predictions),
                          ("Preperiod, before observed accumulation", [row for row in predictions if row["phase"] == 0]),
                          ("At least two weeks of observed accumulation", [row for row in predictions if row["phase"] >= 14])):
        outcomes = defaultdict(list)
        for row in subset:
            outcomes[tuple(row["period"]), row["start"]].append(row)
        for method, name in METHODS.items():
            if subset:
                brier = mean(mean((row["predictions"][method] - row["outcome"]) ** 2 for row in rows) for rows in outcomes.values())
                metrics.append({"group": label, "model": name, "brier": brier, "threshold_cases": len(subset),
                                "station_periods": len(outcomes), "calendar_years": len({row["year"] for row in subset})})
    coverage = {"native_instruments": sum(len(group["markets"]) for group in corpus["catalog"]),
                "native_periods": len(corpus["catalog"]), "native_binary_checks": len(payout_checks),
                "native_cash_matches": sum(row["actual"] == row["complete_daily_source_implies"] for row in payout_checks),
                "historic_weather_by_period": {"/".join(map(str, key)): len(rows) for key, rows in histories.items()},
                "forecastable_live": sum(bool(row["forecasts"]) for row in current),
                "live_with_native_order_book": sum(row["book_reference"] is not None for row in current)}
    record = {"version": VERSION, "supersedes": EARLIER, "title": "Napa and Great Falls precipitation periods",
              "hypothesis": "An early accumulation adjustment and ENSO-matched past seasons can improve later airport total thresholds.",
              "target": "Past complete calendar-period total liquid-equivalent precipitation and separate native governing daily receipts",
              "date_window": {"Napa fit through": FIT_END["KAPC"], "Great Falls fit through": FIT_END["KGTF"], "archive held through": "2025-2026",
                              "native snapshot": corpus["as_of"]}, "venues": ["Kalshi"], "coverage": coverage, "metrics": metrics,
              "cases": cases, "missing": corpus["gauge_quality"],
              "limits": ["Historical GHCN daily measurements are revisable. Stage timing conservatively allows two days after the last included day and uses no later exposures.",
                         "ENSO is a modern retrospective RONI reconstruction for ended seasons. Later historical revisions were unavailable as vintages.",
                         "The native rule uses the direct governing total if published. A complete daily sum does not establish that the exchange used it.",
                         "The public Kalshi image clock is absent. Saved depth supports inspecting price and size without assuming execution."],
              "provenance": {"GHCN daily source documentation": GHCND + "readme.txt", "native terms": TERMS,
                             "current CPC outlook context": ISSUE_CONTEXT,
                             "frozen_input": str(frozen.relative_to(data)), "frozen_input_sha256": digest,
                             "source_sha256": research_model.run_sources(sys.modules[__name__], venue_ghcn, venue_storm)}}
    return research_model.write_record(data, record, {"native_payout_checks": payout_checks, "active": current, "test": predictions})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--input", type=Path)
    parser.add_argument("--as-of")
    parser.add_argument("--collect", action="store_true")
    args = parser.parse_args(argv)
    try:
        corpus = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else gather(
            args.data, collect=args.collect, as_of=parse_time(args.as_of) if args.as_of else None)
        print(evaluate(args.data, corpus))
    except CollectionPaused:
        print("Collection is stopped. Retained receipts are available.")
        return 2


if __name__ == "__main__":
    sys.exit(main())
