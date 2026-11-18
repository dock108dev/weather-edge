"""Replay native state D4 thresholds against weekly U.S. Drought Monitor data."""

import argparse
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
import gzip
import json
import math
from pathlib import Path
from statistics import mean
import sys
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from sklearn import __version__ as sklearn_version
from sklearn.feature_extraction import DictVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline

from . import research_model, venue_archive, venue_benchmark, venue_storm
from .collector import paused
from .market import iso, parse_time
from .venue_sources import CollectionPaused

VERSION = "us-drought-monitor-native-d4-v2"
EARLIER_RUN = "us-drought-monitor-native-d4-v1-f10080caf0b761d3"
FOLDER = "usdm-drought"
SERVICE = "https://usdmdataservices.unl.edu/api/StateStatistics/"
SUMMARY = "https://droughtmonitor.unl.edu/services/data/summary/html/"
DOCUMENTATION = "https://droughtmonitor.unl.edu/DmData/DataDownload/WebServiceInfo.aspx"
RELEASE = "https://droughtmonitor.unl.edu/About/MediaKit.aspx"
FILING = "https://www.cftc.gov/sites/default/files/filings/ptc/25/07/ptc07252527447.pdf"
STATE_FIPS = {"AZ": "04", "CA": "06", "IA": "19", "IL": "17", "IN": "18", "KS": "20", "LA": "22", "MN": "27",
              "MO": "29", "NC": "37", "ND": "38", "OR": "41", "TN": "47", "VA": "51", "WA": "53"}
FIT_LAST, HOLD_LAST, NATIVE_YEAR = 2017, 2025, 2026
PHASES = (-2, 2, 5)
SERIES = "KXDROUGHTLEVEL"
EASTERN = ZoneInfo("America/New_York")
WITNESS = "KXDROUGHTLEVEL-26JULLD4-NC"
NARRATIVES = {"20260602": ("June 04, 2026", "North Carolina"), "20260609": ("June 11, 2026", "a new area of exceptional drought"),
              "20260616": ("June 18, 2026", "North Carolina")}
METHODS = {"calendar": "Earlier state and same-season D4 rate", "analogs": "Past D4 from comparable current drought category",
           "logistic": "Earlier logistic using drought area and changes", "market": "Native Kalshi candle midpoint"}


def _obtain(data, path, url, collect, *, json_body=False):
    if collect and paused(data):
        raise CollectionPaused
    raw, receipt = (venue_storm._get(data, path, url, folder=FOLDER, accept="application/json" if json_body else None)
                    if collect else venue_storm._existing(data, path, folder=FOLDER))
    if receipt["source"] != url:
        raise ValueError(f"Different original URL retained for {path}")
    return raw, receipt


def _usdm(data, state, area, collect):
    suffix = "area" if area else "percent"
    method = "GetDroughtSeverityStatisticsByArea" if area else "GetDroughtSeverityStatisticsByAreaPercent"
    first, last = ("5/1/2026", "8/4/2026") if area else ("1/1/2000", "11/15/2026")
    url = SERVICE + method + "?" + urlencode({"aoi": STATE_FIPS[state], "startdate": first, "enddate": last, "statisticsType": 1})
    raw, receipt = _obtain(data, state + "-" + suffix + ".json", url, collect, json_body=True)
    rows = {}
    for row in json.loads(raw):
        start = date.fromisoformat(row["validStart"][:10])
        stop = date.fromisoformat(row["validEnd"][:10])
        values = [float(row[f"d{level}"]) for level in range(5)]
        if (row["stateAbbreviation"] != state or row["statisticFormatID"] != 1 or start.weekday() != 1
                or row["mapDate"][:10] != start.isoformat() or stop - start != timedelta(days=6)
                or str(start) in rows or not all(math.isfinite(x) and 0 <= x <= (1e7 if area else 100) for x in values)
                or values != sorted(values, reverse=True)):
            raise ValueError(f"Unexpected USDM state row for {state} {start}")
        rows[str(start)] = {"valid_start": str(start), "valid_end": str(stop), "standard_release_at": iso(_release_at(start)),
                            **{f"d{level}": x for level, x in enumerate(values)}}
    if not rows:
        raise ValueError(f"No USDM state rows for {state}")
    return rows, receipt


def _release_at(tuesday):
    return datetime.combine(tuesday + timedelta(days=2), time(8, 30), EASTERN)


def _thursday(day):
    return day + timedelta(days=(3 - day.weekday()) % 7)


def _reporting_weeks(year, *, overlap=False):
    first, last = date(year, 6, 4), date(year, 7, 30)
    tuesday = first - timedelta(days=(first.weekday() - 1) % 7)
    if not overlap and tuesday < first:
        tuesday += timedelta(days=7)
    return [str(tuesday + timedelta(days=7 * i)) for i in range(1 + (last - tuesday).days // 7)]


def _phases(year):
    anchor = _thursday(date(year, 6, 4))
    return {phase: datetime.combine(anchor + timedelta(weeks=phase), time(18), timezone.utc) for phase in PHASES}


def _price_targets(data):
    args = (venue_archive.KALSHI, SERIES)
    with venue_archive.connect(data) as db:
        db.execute("BEGIN")
        markets = [dict(row) for row in db.execute(f"SELECT {venue_archive.PUBLIC_SELECT} FROM markets WHERE venue=? AND series_id=?", args)]
        for market in markets:
            prices = {}
            for phase, nominal in _phases(NATIVE_YEAR).items():
                if not parse_time(market["opened_at"]) <= nominal + venue_benchmark.WAIT < parse_time(market["closed_at"]):
                    continue
                quote, missing = venue_benchmark._history_point(db, market, nominal)
                prices[str(phase)] = quote or {"unavailable": missing}
            market["decision_prices"] = prices
        native = [dict(row) | {"body": json.loads(row["body"])} for row in db.execute(
            """SELECT kind,native_key,at,receipt_id,body FROM history WHERE venue=? AND instrument_id=?
            AND kind IN ('trade','candle-60m') AND at>='2026-06-02' AND at<'2026-06-19' ORDER BY at,native_key""",
            (venue_archive.KALSHI, WITNESS),
        )]
    return sorted(markets, key=lambda row: row["instrument_id"]), native


def gather(data, *, collect=False):
    reports, area_reports, receipts, errors, narrative = {}, {}, {}, {}, {}
    for state in STATE_FIPS:
        for area, collection in ((False, reports), (True, area_reports)):
            key = f"{state} {'area' if area else 'percent'}"
            try:
                collection[state], receipts[key] = _usdm(data, state, area, collect)
            except (OSError, ValueError, KeyError) as error:
                errors[key] = str(error)
    for code, required in NARRATIVES.items():
        try:
            raw, receipt = _obtain(data, f"{code}.html", SUMMARY + f"usdm_summary_{code}.html", collect)
            text = raw.decode("utf-8", "replace")
            if not all(phrase in text for phrase in required):
                raise ValueError("Dated official USDM narrative markers were not present")
            narrative[code] = receipt
        except (OSError, ValueError, KeyError) as error:
            errors[f"narrative {code}"] = str(error)
    markets, native = _price_targets(data)
    return {"version": VERSION, "state_percent": reports, "state_area_2026": area_reports, "source_receipts": receipts,
            "narrative_receipts": narrative, "source_errors": errors, "markets": markets, "native_timeline": native}


def _stage(row):
    return max((level for level in range(5) if row[f"d{level}"] > 0), default=-1)


def _outcome(weekly, year, *, overlap=False):
    dates = _reporting_weeks(year, overlap=overlap)
    return int(any(weekly[day]["d4"] > 0 for day in dates)) if all(day in weekly for day in dates) else None


def _rows(corpus):
    rows, missing = [], Counter()
    for year in range(2000, NATIVE_YEAR + 1):
        dates = _reporting_weeks(year)
        for state, by_week in sorted(corpus["state_percent"].items()):
            outcome = _outcome(by_week, year)
            for phase, nominal in _phases(year).items():
                eligible = [stamp for stamp in by_week if by_week[stamp]["standard_release_at"] <= iso(nominal)]
                if outcome is None or not eligible:
                    missing["incomplete summer source series or no prior weekly report"] += 1
                    continue
                latest = max(eligible)
                if date.fromisoformat(latest) + timedelta(days=10) < nominal.date():
                    missing["stale latest weekly source"] += 1
                    continue
                previous = str(date.fromisoformat(latest) - timedelta(weeks=4))
                if previous not in by_week:
                    missing["four-week earlier report unavailable"] += 1
                    continue
                item, old = by_week[latest], by_week[previous]
                rows.append({"year": year, "state": state, "phase": phase, "at": iso(nominal + venue_benchmark.WAIT),
                             "latest": item, "four_weeks_prior": old, "outcome": outcome,
                             "settled_physically": any(by_week[day]["d4"] > 0 for day in dates if day <= latest),
                             "stage": _stage(item)})
    return rows, dict(missing)


def _features(row):
    latest, prior = row["latest"], row["four_weeks_prior"]
    return {"state": row["state"], "phase": str(row["phase"]), "max_category": row["stage"],
            "current_d2": math.log1p(latest["d2"]), "current_d3": math.log1p(latest["d3"]), "current_d4": math.log1p(latest["d4"]),
            "change_d3": math.log1p(latest["d3"]) - math.log1p(prior["d3"]),
            "change_d4": math.log1p(latest["d4"]) - math.log1p(prior["d4"])}


def _probabilities(earlier, later):
    unresolved = [row for row in earlier if not row["settled_physically"]]
    model = make_pipeline(DictVectorizer(), LogisticRegression(C=1, max_iter=500))
    model.fit([_features(row) for row in unresolved], [row["outcome"] for row in unresolved])
    learned = model.predict_proba([_features(row) for row in later])
    for row, (_, p) in zip(later, learned, strict=True):
        basic = [item["outcome"] for item in earlier if item["state"] == row["state"] and item["phase"] == row["phase"]]
        stage = [item["outcome"] for item in unresolved if item["phase"] == row["phase"] and item["stage"] == row["stage"]]
        row["calendar"] = (sum(basic) + 1) / (len(basic) + 2)
        row["analogs"] = 1.0 if row["settled_physically"] else (sum(stage) + 1) / (len(stage) + 2)
        row["logistic"] = 1.0 if row["settled_physically"] else float(p)
    linear = model[-1]
    return {"C": 1, "intercept": linear.intercept_.tolist(), "coefficients": dict(zip(model[0].get_feature_names_out(), linear.coef_[0], strict=True))}


def _scores(rows, group, methods, target="outcome"):
    if not rows:
        return []
    return [{"group": group, "model": METHODS[key], "brier": mean((row[key] - row[target]) ** 2 for row in rows),
             "bucket_cases": len(rows), "state_seasons": len({(row["state"], row["year"]) for row in rows}),
             "calendar_years": len({row["year"] for row in rows}), "positive_outcomes": sum(row[target] for row in rows)}
            for key in methods]


def _audit(corpus):
    result = []
    for market in corpus["markets"]:
        state = market["instrument_id"].rsplit("-", 1)[-1]
        weather = corpus["state_percent"].get(state, {})
        area = corpus["state_area_2026"].get(state, {})
        official = _outcome(weather, NATIVE_YEAR)
        raw_result = int(market["yes_payout"]) if market["yes_payout"] in ("0", "1") else None
        window = _reporting_weeks(NATIVE_YEAR)
        result.append({"market": market, "state": state, "native_payout": raw_result, "reporting_week_percentage_result": official,
                       "overlap_area_result": _outcome(area, NATIVE_YEAR, overlap=True), "period_start_only_area_result": _outcome(area, NATIVE_YEAR),
                       "overlap_boundary_week": weather.get(_reporting_weeks(NATIVE_YEAR, overlap=True)[0]),
                       "first_positive_week": next((weather[day] for day in window if day in weather and weather[day]["d4"] > 0), None),
                       "d4_weekly": [weather[day] for day in window if day in weather]})
    return result


def _witness(corpus, audits):
    case = next((item for item in audits if item["market"]["instrument_id"] == WITNESS and item["first_positive_week"]), None)
    if case is None:
        return []
    market = case["market"]
    first = case["first_positive_week"]
    trades, asks = [], []
    for point in corpus["native_timeline"]:
        raw = point["body"]
        if point["kind"] == "trade" and raw.get("yes_price_dollars") is not None:
            trades.append({"at": point["at"], "yes": float(raw["yes_price_dollars"])})
        if point["kind"] == "candle-60m" and (price := venue_archive.kalshi_dollar_close(raw.get("yes_ask"))) is not None:
            asks.append({"at": point["at"], "yes": float(price)})
    markers = [{"at": market["closed_at"], "label": "Observed Kalshi trading close", "kind": "close"}]
    if "20260609" in corpus["narrative_receipts"]:
        markers.insert(0, {"at": first["standard_release_at"], "label": "June 9 USDM map · standard release", "kind": "report"})
    cases = [{"venue": venue_archive.KALSHI, "instrument_id": WITNESS, "date": "2026-06-18", "at": market["settlement_at"],
             "title": "North Carolina returned to D4 before the contract closed",
             "detail": f"The {first['valid_start']} map classified {first['d4']:.2f}% of the state as exceptional drought. "
                       f"The dated June 11 narrative corroborates a new area of D4. Native YES paid ${case['native_payout']} on June 18.",
             "venue_price_timeline": {"series": [{"label": label, "kind": kind, "points": points}
                 for label, kind, points in (("Kalshi actual prints", "trade", trades), ("Kalshi hourly YES ask", "display", asks)) if points], "markers": markers},
             "data": {**case, "official_narratives": corpus["narrative_receipts"], "native_tape": corpus["native_timeline"]}}]
    missouri = next((row for row in audits if row["state"] == "MO" and row["overlap_boundary_week"]), None)
    if missouri:
        old = missouri["overlap_boundary_week"]
        cases.append({"venue": venue_archive.KALSHI, "instrument_id": missouri["market"]["instrument_id"], "date": "2026-06-04",
                      "at": old["standard_release_at"], "title": "Missouri's June 4 publication describes a June 2 reference week",
                      "detail": f"The June 2 source map reported {old['d4']:.2f}% of Missouri in D4, and had the June 4 publication date. "
                                f"Every reference week beginning June 4–July 30 was at zero. Native YES paid ${missouri['native_payout']}.",
                      "data": {**missouri, "current_official_statistics": corpus["source_receipts"].get("MO percent"),
                               "original_dated_narrative": corpus["narrative_receipts"].get("20260602")}})
    return cases


def evaluate(data, corpus):
    frozen, digest = research_model.freeze_inputs(data, corpus)
    rows, missing = _rows(corpus)
    earlier = [row for row in rows if row["year"] <= FIT_LAST]
    held = [row for row in rows if FIT_LAST < row["year"] <= HOLD_LAST]
    actual = [row for row in rows if row["year"] == NATIVE_YEAR]
    fit = _probabilities(earlier, held + actual)
    audit = _audit(corpus)
    market_by_state = {row["state"]: row for row in audit}
    matched = []
    for row in actual:
        native = market_by_state.get(row["state"])
        if not native or native["native_payout"] is None:
            continue
        point = native["market"]["decision_prices"].get(str(row["phase"]))
        if point and "probability" in point:
            matched.append({**row, "native_payout": native["native_payout"], "market": point["probability"], "native_price": point})
    unresolved = [row for row in held if not row["settled_physically"]]
    checked = [row for row in audit if row["period_start_only_area_result"] is not None and row["native_payout"] is not None]
    record = {"version": VERSION, "supersedes": EARLIER_RUN,
              "title": "Exceptional drought by state and the date that governs settlement",
              "hypothesis": "Earlier state drought coverage improves the probability of any D4 before the end of the reporting window.",
              "target": "Any part of each exact state in USDM D4 during an inclusive reporting period, alongside independently verified native cash",
              "date_window": {"fit June 4 to July 30 seasons": [2000, FIT_LAST], "held seasons": [FIT_LAST+1, HOLD_LAST],
                              "native reporting interval": ["2026-06-04", "2026-07-30"]}, "venues": ["Kalshi"],
              "coverage": {"source_weekly_state_reports": sum(map(len, corpus["state_percent"].values())), "states": len(corpus["state_percent"]),
                           "fit_rows": len(earlier), "held_rows": len(held), "held_unreached_rows": len(unresolved), "native_instruments": len(audit),
                           "native_payouts_with_full_official_area_series": len(checked),
                           "native_payouts_matched_by_in_window_reference_date": sum(row["native_payout"] == row["period_start_only_area_result"] for row in checked),
                           "native_payouts_matched_by_overlapping_validity": sum(row["native_payout"] == row["overlap_area_result"] for row in checked),
                           "native_date_boundary_sensitivity": sum(row["period_start_only_area_result"] != row["overlap_area_result"] for row in checked),
                           "native_exact_price_and_payout_clocks": len(matched)},
              "missing": {**missing, **corpus["source_errors"]}, "cases": _witness(corpus, audit), "fit": fit,
              "metrics": _scores(held, "Untouched physical calendar seasons", ("calendar", "analogs", "logistic"))
                         + _scores(unresolved, "D4 still unreached in untouched physical seasons", ("calendar", "analogs", "logistic"))
                         + _scores(matched, "2026 same native price and cash clocks", tuple(METHODS), target="native_payout"),
              "limits": ["The bulk state archive is retrospective. Use the documented Thursday release schedule to approximate historical availability and separately retain original dated narratives.",
                         "Any named part of a state can qualify. Absolute-area and percentage series are checked separately because printed percentages round.",
                         "Weekly validity and vendor close are independent. Both the overlapping valid-week and week-start-only interpretations are audited.",
                         "The order of two provider feeds cannot establish earliest local availability or historical book depth."],
              "provenance": {"USDM documentation": DOCUMENTATION, "standard publication schedule": RELEASE, "CFTC filing": FILING,
                             "source_sha256": research_model.run_sources(sys.modules[__name__], venue_benchmark, venue_storm),
                             "frozen_input": str(frozen.relative_to(data)), "frozen_input_sha256": digest, "scikit_learn": sklearn_version}}
    return research_model.write_record(data, record, {"native_audit": audit, "held_weather": held, "native_price_replay": matched})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--collect", action="store_true")
    parser.add_argument("--input", type=Path)
    args = parser.parse_args(argv)
    try:
        corpus = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else gather(args.data, collect=args.collect)
        print(evaluate(args.data, corpus))
    except CollectionPaused:
        print("Collection is stopped. Retained receipts are available.")
        return 2


if __name__ == "__main__":
    sys.exit(main())
