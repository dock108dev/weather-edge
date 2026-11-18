"""Study NOAA seasonal RONI outlooks against exact Kalshi contract periods."""

import argparse
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
import gzip
import json
from pathlib import Path
import re
from statistics import mean
import sys
from urllib.parse import urlencode

from sklearn import __version__ as sklearn_version
from sklearn.linear_model import Ridge

from . import research_model, venue_archive, venue_storm
from .market import decimal, iso
from .venue_contracts import ENSO_PERIODS
from .venue_sources import PublicGET

VERSION = "kalshi-enso-roni-v2"
EARLIER_RUN = "kalshi-enso-roni-v1-286284ee67350b7d"
CPC = "https://www.cpc.ncep.noaa.gov/products/analysis_monitoring/enso/roni/"
TERMS = "https://assets.kalshi.com/contract_terms/ELNINOSTRENGTH.pdf"
PUBLICATION = "https://www.weather.gov/media/notification/pdf_2026/pns26-05_Relative_ONI.pdf"
NUMERIC = "https://www.cpc.ncep.noaa.gov/data/indices/RONI.ascii.txt"
ISSUES = {4: "2026-04-09", 5: "2026-05-14", 6: "2026-06-11", 7: "2026-07-09",
          8: "2026-08-13", 9: "2026-09-10", 10: "2026-10-08", 11: "2026-11-12"}
OFFICIAL_CASES = {10: {"SON": 0.54, "OND": 0.83, "NDJ": 0.70}, 11: {"OND": 0.60, "NDJ": 0.60}}
FIT, CALIBRATION, TEST = (1950, 1989), (1990, 1999), (2000, 2025)
THRESHOLDS = ("-1.5", "-0.5", "0.5", "1.0", "1.5", "2.0", "2.5")
CLOCK_SOURCES = {8: ("MAM", "FMA"), 11: ("JJA", "MJJ")}
METHODS = {"climatology": "Earlier NDJ climatology", "persistence": "Latest season and calibrated persistence residuals",
           "ridge": "Earlier Ridge with recent seasonal change"}
NUMBER = re.compile(r"[+-]?\d+(?:\.\d+)?")


def _get(data, path, source):
    return venue_storm._get(data, path, source, folder="enso")


def parse_history(body):
    """Use official visible one-decimal values for the contract label."""
    rows = {}
    for row in venue_storm.Page(body).rows:
        if not 2 <= len(row) <= 13 or not re.fullmatch("19[5-9][0-9]|20[0-9][0-9]", row[0]):
            continue
        year = int(row[0])
        values = {season: value for season, value in zip(ENSO_PERIODS, row[1:]) if NUMBER.fullmatch(value)}
        if year in rows or any(not -5 <= decimal(value) <= 5 for value in values.values()):
            raise ValueError("Unexpected NOAA RONI history identity or value")
        rows[year] = values
    if len(rows) < 70:
        raise ValueError("NOAA RONI history table not found")
    return rows


def parse_outlook(body, month, kind):
    page = venue_storm.Page(body)
    expected = date(2026, month, 1).strftime("Issued %B %Y")
    if expected not in page.text():
        raise ValueError(f"Archive does not identify {expected}")
    width = 10 if kind == "strengths" else 8
    points = {}
    for row in page.rows:
        if len(row) != width or not row[0]:
            continue
        season = row[0].split()[0]
        if season not in ENSO_PERIODS or any(not NUMBER.fullmatch(cell) for cell in row[1:]):
            continue
        if season in points:
            raise ValueError("Unexpected repeated CPC outlook season")
        values = [decimal(cell) for cell in row[1:]]
        if kind == "strengths" and (abs(sum(values) - 100) > 2 or any(not 0 <= value <= 100 for value in values)):
            raise ValueError("Unexpected CPC intensity probability row")
        if kind == "outlook" and values != sorted(values):
            raise ValueError("Nonmonotonic CPC percentile row")
        points[season] = [float(value) for value in values]
    if not points:
        raise ValueError("Expected per-season CPC outlook data")
    return points


def _clock(issue):
    begun = date.fromisoformat(issue)
    return (datetime.combine(begun, datetime.min.time(), timezone.utc),
            datetime.combine(begun + timedelta(days=2), datetime.min.time(), timezone.utc))


def contracts(data):
    with venue_archive.connect(data) as db:
        return [dict(row) for row in db.execute(
            f"SELECT {venue_archive.PUBLIC_SELECT} FROM markets WHERE venue=? AND series_id='KXRONI' ORDER BY instrument_id",
            (venue_archive.KALSHI,),
        )]


def backfill_candles(data):
    get = PublicGET(data)
    markets = contracts(data)
    retained = sum(venue_storm._receive_candle(data, get, row) for row in markets)
    return len(markets), retained


def _forecast_probability(market, issue):
    if "NDJ 2026-27 season" not in market["rules_primary"]:
        return None, "Season peak across overlapping periods needs a joint outlook and definitive eligible periods"
    strengths = issue.get("strengths", {}).get("NDJ")
    band = (market["operator"], *[str(decimal(market[key]).normalize()) if market[key] is not None else None
                                  for key in ("lower", "upper")])
    if strengths:
        indices = {("below", None, "0.5"): slice(0, 5), ("between-inclusive", "0.5", "0.9"): slice(5, 6),
                   ("between-inclusive", "1", "1.4"): slice(6, 7), ("between-inclusive", "1.5", "1.9"): slice(7, 8),
                   ("at-least", "2", None): slice(8, 9)}
        if band in indices:
            return sum(strengths[indices[band]]) / 100, "Official NOAA NDJ RONI strength bins"
    if "historical_threshold" in issue and (at_least := issue["historical_threshold"].get("NDJ")) is not None:
        if band == ("at-least", "2.5", None):
            return at_least, "Official NOAA discussion's approximate NDJ ≥2.5°C probability"
        if band == ("between-inclusive", "2", "2.4") and strengths:
            return round(strengths[-1] / 100 - at_least, 4), "Approximate difference of published NDJ threshold probabilities"
    return None, "No exact NOAA probability for this native threshold and release"


def gather(data):
    now = iso(datetime.now(timezone.utc))
    page, history_receipt = _get(data, "roni-table-20261115.html", CPC)
    _, numeric_receipt = _get(data, "roni-ascii-20261115.txt", NUMERIC)
    rows, gaps = {}, {}
    for month, day in ISSUES.items():
        observed = {"issued_date": day, "receipts": {}}
        for kind in ("strengths", "outlook"):
            source = CPC + "archives/?" + urlencode({"type": kind, "month": f"{month:02d}", "year": 2026})
            try:
                body, proof = _get(data, f"outlooks/{kind}-{month:02d}.html", source)
                values = parse_outlook(body, month, kind)
            except (OSError, ValueError) as error:
                gaps[f"{day}/{kind}"] = str(error)
                continue
            observed[kind] = values
            observed["receipts"][kind] = proof
        if month in OFFICIAL_CASES:
            issue_url = f"https://www.cpc.ncep.noaa.gov/products/analysis_monitoring/enso_disc_{date.fromisoformat(day):%b}2026/ensodisc.pdf".lower()
            try:
                _, proof = _get(data, f"outlooks/discussion-{month:02d}.pdf", issue_url)
                observed["historical_threshold"] = OFFICIAL_CASES[month]
                observed["receipts"]["discussion"] = proof
            except OSError as error:
                gaps[f"{day}/discussion"] = str(error)
        rows[month] = observed
    market_records = []
    for contract in contracts(data):
        samples = []
        for issue in rows.values():
            before, after = _clock(issue["issued_date"])
            old_price = venue_storm._pre_decision_price(data, contract, before)
            new_price = venue_storm._pre_decision_price(data, contract, after)
            forecast, attribution = _forecast_probability(contract, issue)
            samples.append({"issue_date": issue["issued_date"], "before_at": iso(before), "after_at": iso(after),
                            "before_price": old_price, "after_price": new_price, "forecast": forecast,
                            "forecast_attribution": attribution, "available_at": iso(after - timedelta(days=1)),
                            "price_change": new_price["probability"] - old_price["probability"] if new_price and old_price else None})
        market_records.append({"contract": contract, "samples": samples})
    return {"version": VERSION, "as_of": now, "history": parse_history(page), "history_receipts": [history_receipt, numeric_receipt],
            "issue_gaps": gaps, "issue_archive": rows, "market_records": market_records}


def _historical(history):
    history = {int(year): values for year, values in history.items()}
    cases = []
    for month, (latest, older) in CLOCK_SOURCES.items():
        observations = []
        for year in range(FIT[0], TEST[1] + 1):
            row = history.get(year, {})
            if any(season not in row for season in (latest, older, "NDJ")):
                continue
            current, prior, future = (float(row[season]) for season in (latest, older, "NDJ"))
            observations.append({"calendar_year": year, "date": date(year, month, 15).isoformat(),
                                 "decision_month": month, "source_season": latest, "prior_season": older,
                                 "current": current, "features": [current, current - prior], "target": future})
        earlier = [row for row in observations if FIT[0] <= row["calendar_year"] <= FIT[1]]
        calibration = [row for row in observations if CALIBRATION[0] <= row["calendar_year"] <= CALIBRATION[1]]
        test = [row for row in observations if TEST[0] <= row["calendar_year"] <= TEST[1]]
        model = Ridge(alpha=1.0).fit([row["features"] for row in earlier], [row["target"] for row in earlier])
        dependence = model.predict([row["features"] for row in calibration])
        persistence_error = [row["target"] - row["current"] for row in calibration]
        ridge_error = [row["target"] - predicted for row, predicted in zip(calibration, dependence, strict=True)]
        for row, predicted in zip(test, model.predict([row["features"] for row in test]), strict=True):
            distributions = {"climatology": [past["target"] for past in earlier],
                             "persistence": [row["current"] + residual for residual in persistence_error],
                             "ridge": [predicted + residual for residual in ridge_error]}
            for bound in THRESHOLDS:
                floor = float(bound)
                estimates = {method: (0.5 + sum(value >= floor - 1e-9 for value in values)) / (1 + len(values))
                             for method, values in distributions.items()}
                cases.append({**row, "threshold_c": bound, "outcome": int(row["target"] >= floor), **estimates})
    return cases


def _score(rows, group):
    metrics = []
    for method, label in METHODS.items():
        score = defaultdict(list)
        for row in rows:
            score[row["calendar_year"]].append((row[method] - row["outcome"]) ** 2)
        if score:
            metrics.append({"method": method, "label": label, "group": group, "bucket_cases": len(rows),
                            "calendar_dates": len({row["date"] for row in rows}), "enso_seasons": len(score),
                            "bucket_brier": mean(mean(errors) for errors in score.values()),
                            "yes_outcomes": sum(row["outcome"] for row in rows)})
    return metrics


def _examples(records):
    examples = []
    for row in records:
        contract = row["contract"]
        symbol = contract["instrument_id"]
        if symbol not in ("KXRONI-27APR-T20", "KXRONI-27APR2-T25", "KXRONI-27AUG-B25"):
            continue
        last = row["samples"][-1]
        price = last["after_price"]
        detail = (f"November NOAA NDJ probability {last['forecast']:.0%}." if last["forecast"] is not None
                  else last["forecast_attribution"] + ".")
        if price:
            detail += f" Historical Kalshi candle midpoint {price['probability']:.1%}."
        detail += " Native payout pending."
        examples.append({"venue": venue_archive.KALSHI, "instrument_id": symbol, "date": last["issue_date"],
                         "at": last["after_at"], "title": contract["title"], "detail": detail, "data": row})
    return examples


def evaluate(data, corpus):
    frozen, digest = research_model.freeze_inputs(data, corpus)
    historical = _historical(corpus["history"])
    metrics = _score(historical, "Identical later NDJ thresholds")
    for month in CLOCK_SOURCES:
        label = "August retrospective clock" if month == 8 else "November retrospective clock"
        metrics += _score([row for row in historical if row["decision_month"] == month], label)
    by_release = {}
    for issue in corpus["issue_archive"].values():
        day = issue["issued_date"]
        samples = [row["samples"] for row in corpus["market_records"]]
        eligible = [item for parts in samples for item in parts if item["issue_date"] == day]
        joint = [row for row in eligible if row["price_change"] is not None]
        by_release[day] = {"matched_contracts": len(eligible), "coherent_before_and_after": len(joint),
                           "official_single_season_probabilities": sum(row["forecast"] is not None for row in eligible),
                           "with_coherent_price_and_official_forecast": sum(row["forecast"] is not None for row in joint)}
    observations = Counter()
    for row in historical:
        if row["decision_month"] == 11 and row["outcome"]:
            observations[row["threshold_c"]] += 1
    current = corpus["history"].get(2026, corpus["history"].get("2026")) or {}
    result = {"version": VERSION, "supersedes": EARLIER_RUN, "title": "NOAA El Niño strength and live RONI contract definitions",
              "hypothesis": "Earlier available-equivalent ocean anomalies improve on unconditional NDJ climatology. "
                            "A separate panel compares genuine as-issued 2026 NOAA forecasts with selected venue prices.",
              "target": "Climate Prediction Center November–January RONI at published 0.1°C precision", "venues": ["Kalshi"],
              "date_window": {"fit seasons": list(FIT), "residual calibration seasons": list(CALIBRATION),
                              "untouched seasons": list(TEST), "as-issued forecast releases": [ISSUES[4], ISSUES[11]]},
              "coverage": {"official history_years": len(corpus["history"]), "market_contracts": len(corpus["market_records"]),
                           "latest NOAA table row": {"year": 2026, "visible periods": current, "NDJ observation available": "NDJ" in current},
                           "verified native payouts": sum(row["contract"]["yes_payout"] is not None for row in corpus["market_records"]),
                           "price_clocks": by_release,
                           "held-out NDJ exceedances": {bound: observations[bound] for bound in THRESHOLDS}},
              "missing": corpus["issue_gaps"], "metrics": metrics, "cases": _examples(corpus["market_records"]),
              "limits": ["The 1950–2025 ERSSTv6 RONI table is a modern reconstruction. Historical inputs use a conservative nominal "
                         "finalization lag and current historical observations, so the score is retrospective weather skill.",
                         "Original 2026 NOAA outlooks are transcribed from dated operational archives. A conservative next UTC day "
                         "stands in for exact first web availability. Different releases forecast the same active NDJ season.",
                         "Each seasonal episode peak depends on the joint distribution of overlapping periods and the exchange's eligible "
                         "periods. Official single-period NOAA probabilities remain separate from those episode-wide strikes.",
                         "Current RONI rows can change for up to two months after initial publication. Active 2026–27 native payouts "
                         "are unavailable. Price proxies contain no historical executable quantities."],
              "provenance": {"frozen_input": str(frozen.relative_to(data)), "frozen_input_sha256": digest,
                             "scikit_learn": sklearn_version, "source_table": CPC, "terms": TERMS, "operational_transition": PUBLICATION,
                             "decision_dates": "August 15 using MAM and FMA; November 15 using JJA and MJJ",
                             "source_sha256": research_model.run_sources(sys.modules[__name__], venue_storm)}}
    return research_model.write_record(data, result, {"historical_weather": historical, "as_issued_replays": corpus["market_records"],
                                                      "official_issue_evidence": corpus["issue_archive"]})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--input", type=Path)
    parser.add_argument("--candles", action="store_true")
    args = parser.parse_args(argv)
    if args.candles:
        print(backfill_candles(args.data))
    corpus = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else gather(args.data)
    print(evaluate(args.data, corpus))


if __name__ == "__main__":
    main()
