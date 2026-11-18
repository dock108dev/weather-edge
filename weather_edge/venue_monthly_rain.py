"""Forecast monthly precipitation thresholds and replay exact Kalshi market clocks."""

import argparse
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
import gzip
from hashlib import sha256
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

from . import research_model, venue_archive, venue_snow_data, venue_storm
from .archive import CLI_DATE, MONTHS
from .expansion_weather import CLI_OFFICE, STATIONS, TODAY_THROUGH_CLOSE, _valid_product
from .market import decimal, iso, parse_time
from .nbm import climate_window
from .venue_snow import _clock, _last_candle, _smooth

VERSION = "kalshi-monthly-rain-v4"
EARLIER_RUN = "kalshi-monthly-rain-v3-8cc4f3b0e3d99d63"
STATION_IDS = ("KNYC", "KMDW", "KORD", "KSEA")
SERIES = ("KXRAINNYCM", "KXRAINCHIM", "KXRAINSEAM")
FIT, TEST = (2006, 2017), (2018, 2026)
CLOCKS = (1, 8, 15, 22, 28)
THRESHOLDS = (1, 2, 3, 4, 5, 6)
TERM_URLS = ("https://assets.kalshi.com/contract_terms/RAINM.pdf", "https://assets.kalshi.com/contract_terms/RAINNYCM.pdf")
AFOS_URL = "https://mesonet.agron.iastate.edu/api/1/nws/afos/list.json?"
NCEI_URL = "https://www.ncei.noaa.gov/access/services/data/v1"
NCEI_DOCS = "https://www.ncei.noaa.gov/support/access-data-service-api-user-documentation"
NCEI_STATION = {"KSEA": "USW00024233", "KMDW": "USW00014819"}
METRICS = {"climatology": "Earlier station-month frequency",
           "remainder": "Published accumulation and empirical remainder",
           "logistic": "Earlier logistic correction",
           "wetness": "Logistic plus recent wetness",
           "nonlinear": "Histogram gradient boosting with recent wetness"}
PRECIP_SECTION = re.compile(r"^PRECIPITATION \(IN\)[^\n]*\n(?P<data>(?:[^\S\n]+[^\n]*\n)+)", re.M)
AMOUNT_LINE = re.compile(r"^\s*(?P<label>YESTERDAY|TODAY|MONTH TO DATE)\s+(?P<amount>\d+\.\d+|\d+|T|MM?)\b", re.M)


def load_weather(data):
    """Reuse the receipt-backed IEM annual CLI transport for both liquid and snow."""
    months = []
    sources = []
    missing = Counter()
    for station in STATION_IDS:
        for year in range(FIT[0], TEST[1] + 1):
            path = data / "venues" / "snow-climate" / station / f"{year}.json"
            meta = path.with_suffix(".json.receipt.json")
            if not path.is_file() or not meta.is_file():
                missing["missing archived station-year"] += 1
                continue
            body, receipt = path.read_bytes(), json.loads(meta.read_bytes())
            if sha256(body).hexdigest() != receipt["sha256"]:
                raise ValueError(f"Changed station-year source {path}")
            sources.append(receipt)
            grouped = defaultdict(dict)
            for raw in json.loads(body)["results"]:
                row = venue_snow_data._day_row(station, raw, offset=STATIONS[station][3])
                if not row:
                    continue
                stamp = date.fromisoformat(row["date"])
                if stamp > date(2026, 11, 14):
                    continue
                if stamp.day in grouped[stamp.month]:
                    raise ValueError(f"Ambiguous daily precipitation label {station} {stamp}")
                kind, daily = venue_snow_data._snow_value(raw.get("precip"))
                mtd_kind, cumulative = venue_snow_data._snow_value(raw.get("precip_month"))
                grouped[stamp.month][stamp.day] = {key: row[key] for key in ("date", "product", "published_at", "source")} | {
                    "daily_kind": kind, "daily_inches": daily, "month_kind": mtd_kind, "month_inches": cumulative,
                }
            for month, days in sorted(grouped.items()):
                first = date(year, month, 1)
                end = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
                day_count = (end - first).days
                last = days.get(day_count)
                months.append({"station": station, "year": year, "month": month, "first": first.isoformat(),
                               "day_count": day_count, "days": [days.get(day) for day in range(1, day_count + 1)],
                               "total": last["month_inches"] if last else None})
    return {"months": months, "weather_sources": sources, "missing": dict(missing)}


def _published(month, clock):
    report = [(day, row) for day, row in enumerate(month["days"], 1) if row and parse_time(row["published_at"]) <= clock]
    latest = next(((day, row) for day, row in reversed(report) if row["month_inches"] is not None), (0, None))
    day, row = latest
    recent = [decimal(reading["daily_inches"]) for number, reading in report
              if day - 7 < number <= day and reading["daily_inches"] is not None]
    return day, row, len(recent), float(sum(recent)), sum(value >= decimal("0.01") for value in recent)


def _snapshot(month, day, threshold, references, *, remove_year=False):
    clock = _clock(month["station"], month["year"], month["month"], day)
    day_observed, latest, recent_count, recent_inches, wet_days = _published(month, clock)
    if day > 1 and day_observed < day - 2:
        return None
    observed = decimal(latest["month_inches"]) if latest else decimal(0)
    earlier = [ref for ref in references if not remove_year or ref["year"] != month["year"]]
    totals = [decimal(ref["total"]) for ref in earlier]
    tails = []
    for ref in earlier:
        report = ref["days"][day_observed - 1] if day_observed else None
        if day_observed and (not report or report["month_inches"] is None):
            continue
        # Earlier-month final values permit negative changes after an as-of report correction.
        at = _clock(ref["station"], ref["year"], ref["month"], day)
        if report and parse_time(report["published_at"]) > at:
            continue
        tails.append(decimal(ref["total"]) - (decimal(report["month_inches"]) if report else 0))
    if not tails:
        return None
    lower = decimal(threshold)
    return {"station": month["station"], "year": month["year"], "month": month["month"], "decision_day": day,
            "date": date(month["year"], month["month"], day).isoformat(), "at": iso(clock), "threshold": str(lower),
            "observed_days": day_observed, "remaining_days": month["day_count"] - day_observed,
            "observed_inches": str(observed), "latest_report": latest, "recent_inches": recent_inches,
            "wet_days": wet_days, "recent_days": recent_count, "references": len(tails),
            "climatology": _smooth(sum(value > lower for value in totals), len(totals)),
            "remainder": _smooth(sum(observed + value > lower for value in tails), len(tails)),
            "outcome": int(decimal(month["total"]) > lower) if month["total"] is not None else None}


def _features(row, *, wetness=False):
    probability = row["remainder"]
    base = {"station": row["station"], "month": str(row["month"]), "logit_remainder": math.log(probability / (1 - probability)),
            "above_threshold_so_far": float(row["observed_inches"]) - float(row["threshold"]),
            "days_remaining": row["remaining_days"] / 31}
    if wetness:
        base.update(wet_fraction=row["wet_days"] / 7, recent_inches=row["recent_inches"], coverage=row["recent_days"] / 7)
    return base


def _fit(rows):
    answers = [row["outcome"] for row in rows]
    by_month = Counter((row["station"], row["year"], row["month"]) for row in rows)
    weights = [len(rows) / len(by_month) / by_month[(row["station"], row["year"], row["month"])] for row in rows]
    parameters = (("logistic", False, LogisticRegression(C=1.0, max_iter=500)),
                  ("wetness", True, LogisticRegression(C=1.0, max_iter=500)),
                  ("nonlinear", True, HistGradientBoostingClassifier(max_iter=100, max_leaf_nodes=15,
                                                                    learning_rate=0.07, l2_regularization=1.0,
                                                                    random_state=0)))
    models = {}
    with threadpool_limits(limits=2):
        for name, add_wetness, estimator in parameters:
            model = make_pipeline(DictVectorizer(sparse=False), estimator)
            model.fit([_features(row, wetness=add_wetness) for row in rows], answers,
                      **{model.steps[-1][0] + "__sample_weight": weights})
            models[name] = (add_wetness, model)
    return models


def _predict(rows, models):
    if not rows:
        return
    with threadpool_limits(limits=2):
        for name, (add_wetness, model) in models.items():
            probabilities = model.predict_proba([_features(row, wetness=add_wetness) for row in rows])
            for row, (_, p) in zip(rows, probabilities, strict=True):
                row[name] = float(p)


def _measure(rows):
    return {"bucket_cases": len(rows), "calendar_dates": len({row["date"] for row in rows}),
            "calendar_months": len({(row["year"], row["month"]) for row in rows}),
            "station_months": len({(row["station"], row["year"], row["month"]) for row in rows})}


def _score(rows, group, target="outcome", methods=METRICS):
    values = []
    for method in methods:
        months = defaultdict(list)
        for row in rows:
            months[(row["station"], row["year"], row["month"])].append((row[method] - row[target]) ** 2)
        if months:
            values.append({"method": method, "label": METRICS.get(method, "Kalshi candle midpoint"), "group": group,
                           "bucket_brier": mean(mean(errors) for errors in months.values()), **_measure(rows)})
    return values


def _parse_cli(body, station, day, product):
    label = CLI_DATE.search(body)
    pil = CLI_OFFICE[station][1]
    if not label or not re.search(rf"^{pil}\s*$", body, re.M):
        return None
    month, day_number, year = label.groups()
    if date(int(year), MONTHS[month], int(day_number)) != day:
        return None
    published = datetime.strptime(product[:12], "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
    _, ends = climate_window(day, STATIONS[station][3])
    if published < ends:
        return None
    match = PRECIP_SECTION.search(body)
    if not match:
        return None
    amounts = {line["label"]: line["amount"] for line in AMOUNT_LINE.finditer(match["data"])}
    day_amount = amounts.get("YESTERDAY")
    if day_amount is None and TODAY_THROUGH_CLOSE.search(body):
        day_amount = amounts.get("TODAY")
    monthly = amounts.get("MONTH TO DATE")
    if day_amount is None or monthly is None:
        return None
    return {"product": product, "published_at": iso(published), "climate_day": day.isoformat(),
            "daily_raw": day_amount, "monthly_raw": monthly}


def _afos(data, station, day):
    _, ends = climate_window(day, STATIONS[station][3])
    cccc, pil = CLI_OFFICE[station]
    stamp = ends.date()
    url = AFOS_URL + urlencode({"cccc": cccc, "pil": pil, "date": stamp})
    listing, receipt = venue_snow_data._obtain(data, Path(station) / f"afos-{stamp}.json", url)
    products = sorted({row["product_id"] for row in json.loads(listing)["data"]
                       if row["cccc"] == cccc and row["pil"] == pil and _valid_product(row["product_id"], station)})
    qualified = []
    for product in products:
        clock = datetime.strptime(product[:12], "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
        if not ends <= clock < ends + timedelta(hours=14):
            continue
        body, evidence = venue_snow_data.obtain_product(data, station, product)
        if row := _parse_cli(body, station, day, product):
            qualified.append({**row, "receipt": evidence})
    return {"reports": qualified, "listing_receipt": receipt}


def _contracts(data):
    with venue_archive.connect(data) as db:
        return [dict(row) for row in db.execute(
            f"SELECT {venue_archive.PUBLIC_SELECT} FROM markets WHERE venue=? AND series_id IN (?,?,?) "
            "AND family='monthly-rain' AND day IS NOT NULL AND station IS NOT NULL AND operator='above' "
            "AND units='in' AND lower IS NOT NULL ORDER BY day,instrument_id", (venue_archive.KALSHI, *SERIES),
        )]


def _daily_summaries(data, station, start, end, rule):
    native_id = NCEI_STATION.get(station)
    declared = re.search(r"GHCND:([A-Z0-9]{11})", rule)
    if not native_id or declared and declared[1] != native_id:
        return None
    url = NCEI_URL + "?" + urlencode({
        "dataset": "daily-summaries", "dataTypes": "PRCP", "stations": native_id, "startDate": start.isoformat(),
        "endDate": end.isoformat(), "format": "json", "units": "standard", "includeAttributes": "true",
    })
    path = Path("ncei-prcp") / station / f"{start}-{end}.json"
    raw, receipt = venue_storm._get(data, path, url, folder="monthly-rain")
    rows = {}
    for record in json.loads(raw):
        day = date.fromisoformat(record["DATE"])
        if record["STATION"] != native_id or not start <= day <= end or day in rows:
            raise ValueError("Daily Summaries identity or day mismatch")
        if amount := record.get("PRCP"):
            measurement, quality, *_ = record.get("PRCP_ATTRIBUTES", ",,").split(",")
            if quality:
                continue
            rows[day] = {"date": day.isoformat(), "inches": str(decimal(amount)), "trace": measurement == "T",
                         "codes": record.get("PRCP_ATTRIBUTES")}
    complete = len(rows) == (end - start).days + 1
    total = sum(decimal(row["inches"]) for row in rows.values()) if complete else None
    return {"source": receipt, "station_id": native_id, "daily": list(rows.values()),
            "total": str(total) if total is not None else None, "trace_days": sum(row["trace"] for row in rows.values())}


def _market_input(data, observed, *, direct):
    months = {(month["station"], month["first"]): month for month in observed["months"]}
    retained = []
    clock = datetime.now(timezone.utc)
    direct_cache, summary_cache, issues = {}, {}, {}
    for contract in _contracts(data):
        month = months.get((contract["station"], contract["day"]))
        if month is None:
            retained.append({"contract": contract, "prices": {}, "direct": None, "daily_summaries": None, "climate_day": None})
            continue
        prices = {}
        for day in CLOCKS:
            at = _clock(month["station"], month["year"], month["month"], day)
            if at > clock or not contract["opened_at"] or not contract["closed_at"]:
                continue
            if parse_time(contract["opened_at"]) <= at < parse_time(contract["closed_at"]):
                prices[str(day)] = _last_candle(data, contract, at)
        first = date.fromisoformat(month["first"])
        last = first.replace(day=month["day_count"])
        examined = last
        published = contract["settlement_at"]
        if published:
            published = parse_time(published)
            local_date = (published + timedelta(hours=STATIONS[month["station"]][3])).date()
            examined = min(last, local_date - timedelta(days=1))
        if direct and published and examined >= first and contract["resolution_source"] == "NWS Daily Climate Report":
            key = (month["station"], examined.isoformat())
            if key not in direct_cache and key not in issues:
                try:
                    direct_cache[key] = _afos(data, month["station"], examined)
                except (OSError, ValueError, KeyError) as error:
                    issues[key] = str(error)
        summary_key = (month["station"], "ncei", examined.isoformat())
        if (direct and published and examined >= first and contract["resolution_source"] == "NOAA NCEI Daily Summaries"
                and summary_key not in summary_cache and summary_key not in issues):
            try:
                summary_cache[summary_key] = _daily_summaries(data, month["station"], first, examined,
                                                            contract["rules_secondary"] or "")
            except (OSError, ValueError, KeyError) as error:
                issues[summary_key] = str(error)
        direct_evidence = direct_cache.get((month["station"], examined.isoformat()))
        retained.append({"contract": contract, "prices": prices, "direct": direct_evidence,
                         "daily_summaries": summary_cache.get(summary_key),
                         "climate_day": examined.isoformat(), "weather_month_end": last.isoformat()})
    return {**observed, "version": VERSION, "as_of": iso(clock), "market_records": retained,
            "direct_http_gaps": {"/".join(key): reason for key, reason in issues.items()}}


def _numeric_expiration(raw):
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)(?:\s+inches?)?\s*", raw or "", re.I)
    return decimal(match[1]) if match else None


def _proof(entry):
    contract = entry["contract"]
    payout = contract["yes_payout"]
    if payout is None:
        return {"status": "Native payout unavailable"}
    if payout not in ("0", "1"):
        return {"status": "Scalar payout", "native_yes": payout}
    amount = _numeric_expiration(contract["expiration_value"])
    result = {"native_yes": int(payout), "native_amount": str(amount) if amount is not None else None,
              "numeric_native_matches": int(amount > decimal(contract["lower"])) == int(payout) if amount is not None else None}
    early = entry["climate_day"] != entry.get("weather_month_end")
    summary = entry.get("daily_summaries")
    if summary and summary["total"] is not None:
        value = decimal(summary["total"])
        agrees = int(value > decimal(contract["lower"])) == int(payout)
        return {**result, "official_total_at_source_inches": str(value), "source_agrees_with_yes": agrees, "early": early,
                "numeric_source_matches": amount == value if amount is not None else None,
                "status": "Retrospective NCEI daily sum agrees with binary payout" if agrees
                else "Retrospective NCEI daily sum differs from binary payout"}
    if not entry["direct"]:
        return {**result, "status": "No qualifying direct NWS report"}
    candidates = [row for row in entry["direct"]["reports"]
                  if row["published_at"] <= contract["settlement_at"] and row["monthly_raw"] not in ("M", "MM")]
    if not candidates:
        return {**result, "status": "No qualifying direct NWS report published before native payout"}
    # RAINM uses the first complete month-end reading. NYC separately permits
    # a close once the daily amounts cross a strike and material review.
    latest = contract["series_id"] == "KXRAINNYCM" or (contract["report_policy"] or "").startswith("Instrument directs")
    selection = candidates[-1] if latest else candidates[0]
    value = decimal(0 if selection["monthly_raw"] == "T" else selection["monthly_raw"])
    result.update(direct_report=selection, eligible_reports=candidates, official_total_at_source_inches=str(value),
                  numeric_source_matches=amount == value if amount is not None else None,
                  source_agrees_with_yes=int(value > decimal(contract["lower"])) == int(payout), early=early)
    if early and (payout == "0" or contract["series_id"] != "KXRAINNYCM"):
        status = "Early resolution requires separate rule review"
    elif result["source_agrees_with_yes"] and result["numeric_source_matches"] is not False:
        status = "Published direct NWS report corroborates binary payout"
    elif result["source_agrees_with_yes"]:
        status = "Direct NWS amount differs and gives same binary threshold"
    else:
        status = "Direct NWS report and native binary payout differ"
    return {**result, "status": status}


def _replay(corpus, groups, models):
    rows, summaries = [], []
    months = {(month["station"], month["first"]): month for month in corpus["months"]}
    for entry in corpus["market_records"]:
        market = entry["contract"]
        month = months.get((market["station"], market["day"]))
        proof = _proof(entry)
        summary = {**entry, "proof": proof, "forecast": []}
        summaries.append(summary)
        if not month:
            continue
        for day in CLOCKS:
            sample = _snapshot(month, day, market["lower"], groups[(month["station"], month["month"])])
            if sample is None or sample["at"] > corpus["as_of"]:
                continue
            ran = str(day) in entry["prices"]
            price = entry["prices"].get(str(day))
            binary = market["yes_payout"] in ("0", "1")
            row = {**sample, "market_symbol": market["instrument_id"], "market_price": price, "market_open": ran,
                   "native_payout": int(market["yes_payout"]) if binary else None,
                   "market": price["probability"] if price else None}
            summary["forecast"].append(row)
            rows.append(row)
    _predict(rows, models)
    return rows, summaries


def _examples(summaries):
    priority = ("KXRAINNYCM-26NOV-1", "KXRAINNYCM-26NOV-3", "KXRAINCHIM-25DEC-1",
                "KXRAINCHIM-26OCT-3", "KXRAINSEAM-26NOV-1")
    selected = {row["contract"]["instrument_id"]: row for row in summaries}
    cases = []
    for symbol in priority:
        if symbol not in selected:
            continue
        row = selected[symbol]
        rule, proof = row["contract"], row["proof"]
        sample = next((item for item in row["forecast"] if item["decision_day"] == 8), None)
        yes = f" Native YES paid ${rule['yes_payout']}." if rule["yes_payout"] is not None else ""
        value = (f" The complete report through {row['climate_day']} recorded "
                 f"{proof['official_total_at_source_inches']} inches month to date.") if "official_total_at_source_inches" in proof else ""
        cases.append({"venue": venue_archive.KALSHI, "instrument_id": symbol, "station": rule["station"], "date": rule["day"],
                      "at": sample["at"] if sample else rule["settlement_at"],
                      "title": f"{rule['station']} · {rule['day'][:7]} rainfall >{rule['lower']} in",
                      "detail": proof["status"] + "." + yes + value, "data": row})
    return cases


def evaluate(data, corpus):
    frozen, digest = research_model.freeze_inputs(data, corpus)
    groups = defaultdict(list)
    for month in corpus["months"]:
        if FIT[0] <= month["year"] <= FIT[1] and month["total"] is not None:
            groups[(month["station"], month["month"])].append(month)
    splits = {"fit": [], "test": []}
    missing = Counter(corpus["missing"])
    for month in corpus["months"]:
        if month["total"] is None:
            missing["complete month-end amount unavailable"] += 1
            continue
        fitting = FIT[0] <= month["year"] <= FIT[1]
        key = "fit" if fitting else "test"
        for day in CLOCKS:
            for threshold in THRESHOLDS:
                row = _snapshot(month, day, threshold, groups[(month["station"], month["month"])], remove_year=fitting)
                if row:
                    splits[key].append(row)
                else:
                    missing[f"{key} insufficient published accumulation or earlier comparables"] += 1
    models = _fit(splits["fit"])
    _predict(splits["test"], models)
    matched, summaries = _replay(corpus, groups, models)
    market_test = [row for row in matched if row["market"] is not None and row["native_payout"] is not None]
    metrics = _score(splits["test"], "Held-out official weather thresholds")
    for day in CLOCKS:
        metrics += _score([row for row in splits["test"] if row["decision_day"] == day], f"Day {day}, 18:00 local standard time")
    metrics += _score(market_test, "Same verified native payouts and market clocks", "native_payout", (*METRICS, "market"))
    proof = dict(Counter(row["proof"]["status"] for row in summaries))
    result = {"version": VERSION, "supersedes": EARLIER_RUN, "title": "Monthly rainfall totals, recent wetness and early settlement",
              "hypothesis": "Published accumulation plus earlier calendar-month rainfall still remaining improves on climatology. "
                            "A same-case ablation tests whether a recent wet week adds independent forecast value.",
              "target": "Official monthly liquid-equivalent precipitation and native Kalshi strict-above payouts", "venues": ["Kalshi"],
              "date_window": {"fit calendar years": list(FIT), "untouched calendar years": list(TEST)},
              "coverage": {"fit": _measure(splits["fit"]), "test": _measure(splits["test"]), "market_price_payout": _measure(market_test),
                           "archive_station_years": len(corpus["weather_sources"]), "market_contracts": len(summaries), "direct_payout_audit": proof},
              "missing": {**missing, "direct report days with request errors": len(corpus["direct_http_gaps"])},
              "metrics": metrics, "cases": _examples(summaries),
              "limits": ["Annual transported CLI summaries may contain later revisions. A weather feature becomes eligible after its retained publication clock.",
                         "The weather total remains a distinct label when a contract settles early or selects a different authoritative report.",
                         "Retrospective NOAA NCEI daily sums expose present-day source values. The API supplies no original first publication clocks.",
                         "Trace is retained as a report status and contributes zero to an accumulation threshold. Missing amount stays unavailable.",
                         "Native candles support the market price proxy. There is no executable historical size or simulated fill in this study."],
              "provenance": {"frozen_input": str(frozen.relative_to(data)), "frozen_input_sha256": digest, "scikit_learn": sklearn_version,
                             "terms": TERM_URLS, "ncei_daily_summaries": NCEI_DOCS, "sample_as_of": corpus["as_of"],
                             "scope": {"stations": STATION_IDS, "decision_days": CLOCKS, "thresholds_inches": THRESHOLDS},
                             "source_sha256": research_model.run_sources(sys.modules[__name__], venue_snow_data, venue_storm)}}
    return research_model.write_record(data, result, {"weather_test": splits["test"], "native_replays": summaries,
                                                      "direct_http_gaps": corpus["direct_http_gaps"]})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--input", type=Path)
    parser.add_argument("--direct", action="store_true")
    args = parser.parse_args(argv)
    corpus = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else _market_input(
        args.data, load_weather(args.data), direct=args.direct,
    )
    print(evaluate(args.data, corpus))


if __name__ == "__main__":
    main()
