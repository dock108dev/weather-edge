"""Forecast monthly tornado count thresholds and audit the settling report definition."""

import argparse
from calendar import monthrange
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
import gzip
import json
import math
from pathlib import Path
import re
from statistics import mean
import sys
from urllib.parse import urlencode

from sklearn import __version__ as sklearn_version
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.feature_extraction import DictVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from threadpoolctl import threadpool_limits

from . import research_model, venue_archive, venue_storm, venue_snow_data
from .market import decimal, iso, parse_time

VERSION = "kalshi-tornado-count-v2"
EARLIER_RUN = "kalshi-tornado-count-v1-d93d8b7d011becfc"
SPC = "https://www.spc.noaa.gov/climo/summary/"
DIRECTIVE = "https://www.weather.gov/media/directives/010_pdfs/pd01005012curr.pdf"
TOTALS = "https://psl.noaa.gov/data/correlation/tornado.us.mon.data"
TERMS = "https://assets.kalshi.com/contract_terms/TORNADO.pdf"
AFOS = "https://mesonet.agron.iastate.edu/api/1/nws/afos/list.json?"
TEXT = "https://mesonet.agron.iastate.edu/api/1/nwstext/"
FIT, TEST = (2005, 2020), (2021, 2025)
THRESHOLDS = (25, 50, 75, 100, 150, 200, 275, 350, 450)
CLOCKS = (1, 8, 15, 22, 28)
MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
LABELS = {"climatology": "Earlier same-month count distribution",
          "remainder": "Known reports plus earlier same-month remainder",
          "logistic": "Earlier logistic correction with recent activity",
          "nonlinear": "Histogram gradient boosting with recent activity"}


def _get(data, path, source):
    return venue_storm._get(data, path, source, folder="tornado")


def _national(body, year):
    raw = json.loads(body)
    months = {}
    counts = {key: line["torn"] for key, line in raw["daily"].items()}
    if any(type(value) is not int or value < 0 for value in counts.values()):
        raise ValueError("Non-integral NOAA daily tornado count")
    for month in range(1, 13):
        last = monthrange(year, month)[1]
        days = [counts.get(f"{month:02d}{number:02d}") for number in range(1, last + 1)]
        total = raw["month"][str(month)]["torn"]
        displayed_days = sum(value for value in days if value is not None)
        months[str(month)] = {"year": year, "month": month, "total": total if None not in days else None,
                              "reports_to_date": total if any(value is not None for value in days) else None, "days": days,
                              "daily_tally": displayed_days, "daily_tally_matches_monthly": displayed_days == total}
    if sum(counts.values()) != raw["torn"]:
        raise ValueError("Daily tornado sum does not match the annual table")
    return months


def _psl(body):
    result = {}
    for line in body.decode().splitlines():
        values = line.split()
        if len(values) != 13 or not re.fullmatch(r"\d{4}", values[0]):
            continue
        result[values[0]] = {str(month): int(value) for month, value in enumerate(values[1:], 1) if int(value) >= 0}
    if len(result) < 50:
        raise ValueError("NOAA PSL monthly series missing")
    return result


def _report_sources(data, days):
    reports, problems = {}, {}
    for day in sorted(days):
        query = urlencode({"cccc": "KWNS", "pil": "STAMTS", "date": day})
        try:
            listing, receipt = venue_snow_data._obtain(data, Path("tornado") / f"stamts-{day}.json", AFOS + query)
            for item in json.loads(listing)["data"]:
                identifier = item["product_id"]
                if item["count"] != 1 or not re.fullmatch(r"\d{12}-KWNS-NWUS21-STAMTS", identifier):
                    continue
                raw, raw_receipt = venue_snow_data._obtain(data, Path("tornado") / f"{identifier}.txt", TEXT + identifier)
                text = raw.decode("utf-8", errors="replace")
                parsed = _stamts(text)
                if parsed:
                    reports[identifier] = {**parsed, "product": identifier, "published_at": iso(parse_time(item["entered"])),
                                           "receipt": raw_receipt, "listing_receipt": receipt}
        except (OSError, ValueError, TypeError, KeyError) as error:
            problems[day] = str(error)
    return reports, problems


def _stamts(text):
    if "STAMTS" not in text or "NWS STORM PREDICTION CENTER" not in text:
        return None
    target = re.search(r"TORNADO TOTALS AND RELATED DEATHS\.{3}THROUGH (\d{2} [A-Z]{3} 20\d{2})", text)
    header = re.search(r"\.\.(20\d{2})\.\..*?\n\s+PREL\s+ACT\b", text)
    if not target or not header:
        return None
    values = {}
    for index, month in enumerate(MONTHS, 1):
        if line := re.search(rf"^{month}\s+([\d-]+)\s+([\d^*-]+)(?=\s|$)", text, re.M):
            values[str(index)] = {"preliminary": int(line[1]) if line[1].isdigit() else None,
                                  "actual": int(line[2]) if line[2].isdigit() else None}
    return {"year": int(header[1]), "valid_through": datetime.strptime(target[1], "%d %b %Y").date().isoformat(),
            "months": values} if values else None


def _contracts(data):
    with venue_archive.connect(data) as db:
        return [dict(row) for row in db.execute(
            f"SELECT {venue_archive.PUBLIC_SELECT} FROM markets WHERE venue=? AND family='tornado-count' "
            "ORDER BY day,instrument_id", (venue_archive.KALSHI,),
        )]


def _archive_price(data, month, contract, day):
    at = datetime(month["year"], month["month"], day, 18, tzinfo=timezone.utc)
    return venue_storm._pre_decision_price(data, contract, at)


def gather(data, *, direct):
    now = datetime.now(timezone.utc)
    sl, psl_receipt = _get(data, "psl-monthly.data", TOTALS)
    national = {}
    sources = {}
    for year in range(FIT[0], 2027):
        body, receipt = _get(data, f"spc-prelim-{year}.json", SPC + f"{year}/ruf/NAT/NAT.json")
        national[str(year)] = _national(body, year)
        sources[str(year)] = receipt
    _, script_receipt = _get(data, "spc-summary.js", SPC + "js/page.js")
    targets, direct_gaps = {}, {}
    if direct:
        issue_months = ((2024, 11), (2025, 1), (2025, 4), (2025, 11), (2025, 12),
                        (2026, 1), (2026, 2), (2026, 11))
        look_at = {date(year, month, day).isoformat() for year, month in issue_months for day in range(1, 11)}
        look_at.update(("2026-11-12", "2026-11-15"))
        targets, direct_gaps = _report_sources(data, look_at)
    images = {}
    for year, mon in ((2024, 10), (2024, 12), (2025, 3), (2026, 10), (2026, 11)):
        designation = MONTHS[mon - 1]
        path = f"{designation.lower()}-{year}-preliminary.png"
        url = SPC + f"{year}/ruf/NAT/NAT_{designation}_TORN.png"
        try:
            _, images[f"{year}-{mon:02d}"] = _get(data, path, url)
        except OSError as error:
            direct_gaps[path] = str(error)
    market_rows = []
    for contract in _contracts(data):
        day = contract["day"]
        month = national.get(day[:4], {}).get(str(int(day[5:7]))) if day else None
        market_rows.append({"contract": contract,
                            "price_clocks": {str(d): _archive_price(data, month, contract, d) for d in CLOCKS} if month else {}})
    return {"version": VERSION, "as_of": iso(now), "national": national, "physical_totals": _psl(sl),
            "sources": sources, "psl_receipt": psl_receipt, "supplementary_reports": targets,
            "direct_gaps": direct_gaps, "script_receipt": script_receipt, "images": images, "market_rows": market_rows}


def _snapshot(row, decision_day, threshold, references, *, hold=False):
    days_known = max(0, decision_day - 2)
    eligible = row["days"][:days_known]
    if None in eligible or not references:
        return None
    history = [ref for ref in references if not hold or ref["year"] != row["year"]]
    counts = [ref["total"] for ref in history]
    tails = [ref["total"] - sum(ref["days"][:days_known]) for ref in history]
    observed = sum(eligible)
    return {"year": row["year"], "month": row["month"], "date": date(row["year"], row["month"], decision_day).isoformat(),
            "decision_day": decision_day, "threshold": threshold, "reports_already_counted": observed,
            "days_known": days_known, "remaining_fraction": (len(row["days"]) - days_known) / 31,
            "recent_count": sum(eligible[-7:]), "recent_active_days": sum(num > 0 for num in eligible[-7:]),
            "climatology": (sum(count > threshold for count in counts) + .5) / (1 + len(counts)),
            "remainder": (sum(observed + count > threshold for count in tails) + .5) / (1 + len(tails)),
            "outcome": int(row["total"] > threshold) if row["total"] is not None else None}


def _features(row):
    probability = row["remainder"]
    return {"month": str(row["month"]), "distance_log": math.asinh((row["reports_already_counted"] - row["threshold"]) / 25),
            "forecast_logit": math.log(probability / (1 - probability)), "remaining_fraction": row["remaining_fraction"],
            "activity_log": math.log1p(row["recent_count"]), "last_week_active_days": row["recent_active_days"]}


def _fit(rows):
    features, outcomes = [_features(row) for row in rows], [row["outcome"] for row in rows]
    models = {
        "logistic": make_pipeline(DictVectorizer(sparse=False), LogisticRegression(C=1, max_iter=350)),
        "nonlinear": make_pipeline(DictVectorizer(sparse=False), HistGradientBoostingClassifier(max_iter=90,
                                                                                              max_leaf_nodes=15,
                                                                                              l2_regularization=1,
                                                                                              random_state=1)),
    }
    with threadpool_limits(limits=2):
        for model in models.values():
            model.fit(features, outcomes)
    return models


def _predict(rows, models):
    if not rows:
        return
    features = [_features(row) for row in rows]
    with threadpool_limits(limits=2):
        for name, model in models.items():
            for row, pair in zip(rows, model.predict_proba(features), strict=True):
                row[name] = float(pair[1])


def _score(rows, label, outcome="outcome", methods=LABELS):
    stats = []
    for method in methods:
        groups = defaultdict(list)
        for row in rows:
            groups[(row["year"], row["month"])].append((row[method] - row[outcome]) ** 2)
        if groups:
            stats.append({"group": label, "method": method, "label": LABELS.get(method, "Kalshi candle midpoint"),
                          "bucket_cases": len(rows), "calendar_months": len(groups),
                          "bucket_brier": mean(mean(errors) for errors in groups.values())})
    return stats


def _native_count(contract):
    raw = contract["expiration_value"]
    return int(raw) if raw and re.fullmatch(r"\d+", raw) else None


def _result_for(count, contract):
    if count is None:
        return None
    order = contract["operator"]
    if order in ("above", "at-least") and contract["lower"] is not None:
        lower = decimal(contract["lower"])
        return int(count > lower if order == "above" else count >= lower)
    if order in ("below", "at-most") and contract["upper"] is not None:
        upper = decimal(contract["upper"])
        return int(count < upper if order == "below" else count <= upper)
    return None


def _matched_reports(corpus, contract, month):
    if not month:
        return []
    return sorted((report for report in corpus["supplementary_reports"].values() if report["year"] == month["year"]
                   and report["valid_through"][:7] >= contract["day"][:7]
                   and str(month["month"]) in report["months"]), key=lambda row: row["published_at"])


def _market_replay(corpus, references, models):
    records, forecasts = [], []
    for evidence in corpus["market_rows"]:
        market = evidence["contract"]
        day = market["day"]
        month = corpus["national"].get(day[:4], {}).get(str(int(day[5:7]))) if day else None
        payout = int(market["yes_payout"]) if market["yes_payout"] in ("0", "1") else None
        native = _native_count(market)
        current_preliminary, physical = None, None
        if month:
            current_preliminary = month["total"]
            physical = corpus["physical_totals"].get(str(month["year"]), {}).get(str(month["month"]))
        native_implied = _result_for(native, market)
        preliminary_implied = _result_for(current_preliminary, market)
        physical_implied = _result_for(physical, market)
        audit = {"native": native, "native_implied": native_implied, "native_payout": payout,
                 "recent_preliminary": current_preliminary, "recent_preliminary_implied": preliminary_implied,
                 "NOAA_PSL": physical, "NOAA_PSL_implied": physical_implied, "timeseries": month,
                 "comparison_reports": _matched_reports(corpus, market, month)}
        record = {**evidence, "audit": audit, "forecast": []}
        records.append(record)
        if not month or payout is None or market["operator"] != "above" or market["lower"] is None:
            continue
        for clock, price in evidence["price_clocks"].items():
            if not price:
                continue
            if forecast := _snapshot(month, int(clock), int(decimal(market["lower"])), references[month["month"]]):
                decorated = {**forecast, "native_payout": payout, "market": price["probability"],
                             "quote": price, "instrument_id": market["instrument_id"]}
                record["forecast"].append(decorated)
                forecasts.append(decorated)
    _predict(forecasts, models)
    return records, forecasts


def _outside_claims(records):
    numbers = Counter()
    dates = defaultdict(set)
    for record in records:
        market, row = record["contract"], record["audit"]
        if row["native_payout"] is None:
            continue
        for label, candidate in (("native published count", row["native_implied"]),
                                 ("later SPC map counts", row["recent_preliminary_implied"]),
                                 ("retrospective NOAA PSL counts", row["NOAA_PSL_implied"])):
            if candidate is not None:
                same = candidate == row["native_payout"]
                numbers[(label, "included")] += 1
                numbers[(label, "different")] += int(not same)
                dates[label].add(market["day"])
    return {label: {"instruments": numbers[(label, "included")], "calendar_months": len(dates[label]),
                    "binary_disagreements": numbers[(label, "different")]} for label in dates}


def _cases(corpus, rows):
    priority = ("KXTORNADO-24DEC-100", "TORNADO-24OCT-50", "KXTORNADO-26OCT-50", "KXTORNADO-26NOV-50")
    by_id = {row["contract"]["instrument_id"]: row for row in rows}
    picked = []
    for symbol in priority:
        if symbol not in by_id:
            continue
        got = by_id[symbol]
        market, audit = got["contract"], got["audit"]
        archive = f"Latest SPC total {audit['recent_preliminary']}" if audit["recent_preliminary"] is not None else "Current month still open"
        status = f"Native count {audit['native']} and YES paid ${audit['native_payout']}." if audit["native"] is not None else "Native payout pending."
        if audit["NOAA_PSL"] is not None:
            archive += f" and NOAA PSL total {audit['NOAA_PSL']}"
        picked.append({"venue": market["venue"], "instrument_id": symbol, "date": market["day"],
                       "at": market["settlement_at"] or corpus["as_of"], "title": market["title"], "detail": status + " " + archive + ".",
                       "data": {**got, "latest_map": corpus["images"].get(market["day"][:7])}})
    return picked


def evaluate(data, corpus):
    frozen, digest = research_model.freeze_inputs(data, corpus)
    reference = defaultdict(list)
    for season in corpus["national"].values():
        for row in season.values():
            if FIT[0] <= row["year"] <= FIT[1] and row["total"] is not None:
                reference[row["month"]].append(row)
    splits = {"fit": [], "test": []}
    gaps = Counter()
    for season in corpus["national"].values():
        for row in season.values():
            if not row["daily_tally_matches_monthly"]:
                gaps["daily and monthly tally differ"] += 1
            if row["total"] is None:
                gaps["incomplete month"] += 1
                continue
            if row["year"] > TEST[1]:
                continue
            fitting = FIT[0] <= row["year"] <= FIT[1]
            cohort = splits["fit" if fitting else "test"]
            for day in CLOCKS:
                for threshold in THRESHOLDS:
                    if item := _snapshot(row, day, threshold, reference[row["month"]], hold=fitting):
                        cohort.append(item)
                    else:
                        gaps["unavailable clock"] += 1
    fit = _fit(splits["fit"])
    _predict(splits["test"], fit)
    replays, quotes = _market_replay(corpus, reference, fit)
    metrics = _score(splits["test"], "Identical held-out monthly count thresholds")
    for day in CLOCKS:
        metrics += _score([row for row in splits["test"] if row["decision_day"] == day], f"Day {day}, 18:00 UTC")
    metrics += _score(quotes, "Same payout trials with coherent daily prices", "native_payout", (*LABELS, "market"))
    result = {"version": VERSION, "supersedes": EARLIER_RUN, "title": "Tornado reports before settlement and after revisions",
              "hypothesis": "Current-month report pace and recent activity improve on long-term month-specific counts. "
                            "A separate audit tests whether later NOAA count products preserve the original venue outcomes.",
              "target": "United States monthly tornado report counts and strict strike thresholds", "venues": ["Kalshi"],
              "date_window": {"fit calendar years": list(FIT), "untouched calendar years": list(TEST)},
              "coverage": {"calendar_years_ingested": len(corpus["national"]), "market_instruments": len(replays),
                           "matching_quote_cases": len(quotes), "contemporaneous_STAMTS_releases": len(corpus["supplementary_reports"]),
                           "payout_audit": _outside_claims(replays)},
              "missing": {**gaps, "public source errors": len(corpus["direct_gaps"])},
              "metrics": metrics, "cases": _cases(corpus, replays),
              "limits": ["The historical SPC daily features use a documented daily report schedule as a conservative nominal lag. "
                         "The served archive may reflect subsequent revisions and does not establish its original receipt.",
                         "The daily tally and reported monthly total can differ. The empirical remainder preserves the printed monthly total.",
                         "STAMTS is a separate, contemporaneous NWS issuance with its own count and revision cutoff.",
                         "NOAA PSL monthly aggregates and the dashboard's currently revised map can use information absent at the native expiration.",
                         "Candle bid and ask closing values are price proxies with no historical executable quantity."],
              "provenance": {"frozen_input": str(frozen.relative_to(data)), "frozen_input_sha256": digest,
                             "source": SPC, "psl_monthly": TOTALS, "nws_directive": DIRECTIVE, "terms": TERMS,
                             "scikit_learn": sklearn_version,
                             "source_sha256": research_model.run_sources(sys.modules[__name__], venue_storm)}}
    return research_model.write_record(data, result, {"historical_forecasts": splits["test"], "native_replays": replays,
                                                      "independent_report_sources": corpus["supplementary_reports"],
                                                      "source_errors": corpus["direct_gaps"]})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--input", type=Path)
    parser.add_argument("--direct", action="store_true")
    args = parser.parse_args(argv)
    corpus = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else gather(args.data, direct=args.direct)
    print(evaluate(args.data, corpus))


if __name__ == "__main__":
    main()
