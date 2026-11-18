"""Replay NASA global-temperature thresholds and calendar-year ranks on both venues."""

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP
import gzip
from io import StringIO
import json
from pathlib import Path
import re
from statistics import mean
import sys

from sklearn import __version__ as sklearn_version
from sklearn.linear_model import Ridge

from . import research_model, venue_archive, venue_history, venue_storm
from .market import decimal, iso, parse_time
from .venue_benchmark import _probability_proxy
from .venue_contracts import GISS_REFERENCE, GISS_SOURCE

VERSION = "global-temperature-rank-v2"
EARLIER_RUN = "global-temperature-rank-v1-d1815cdd3ca28ca5"
GISS = "https://data.giss.nasa.gov/gistemp/"
ISSUE_CLOCKS = ("2026-10-09T15:00:00Z", "2026-11-12T16:00:00Z")
YEARLY = GISS + "graphs/graph_data/Global_Mean_Estimates_based_on_Land_and_Ocean_Data/graph.txt"
MONTHLY = GISS + "tabledata_v4/GLB.Ts+dSST.csv"
SCHEDULE = GISS + "release_dates.html"
ARCHIVED = GISS + "history/output/loti_used.zip"
PRESS = {
    2020: ("1.02", "2021-01-14", "https://www.nasa.gov/news-release/2020-tied-for-warmest-year-on-record-nasa-analysis-shows/"),
    2022: ("0.89", "2023-01-12", "https://www.nasa.gov/news-release/nasa-says-2022-fifth-warmest-year-on-record-warming-trend-continues/"),
    2023: ("1.17", "2024-01-12", "https://www.nasa.gov/wp-content/uploads/2024/01/noaa-nasa-global-analysis-2023.pdf"),
    2024: ("1.28", "2025-01-10", "https://www.nasa.gov/news-release/temperatures-rising-nasa-confirms-2024-warmest-year-on-record/"),
    2025: ("1.19", "2026-01-14", "https://www.nasa.gov/news-release/nasa-releases-global-temperature-data/"),
}
SAMPLES = (8, 9, 10)
FIT, CALIBRATION, TEST = (1950, 1994), (1995, 2006), (2007, 2025)
METHODS = {"ytd": "Year-to-date mean and earlier residuals", "late_trend": "Latest three-month level for remaining months",
           "ridge": "Earlier Ridge correction"}


def _get(data, name, url):
    return venue_storm._get(data, name, url, folder="global-temperature")


def parse_annual(body):
    lines = body.decode().splitlines()
    columns = next((line.split() for line in lines if "No_Smoothing" in line), None)
    if columns != ["Year", "No_Smoothing", "Lowess(5)"]:
        raise ValueError("NASA annual unsmoothed table schema differs")
    values = {}
    for line in lines:
        if match := re.fullmatch(r"\s*(\d{4})\s+([+-]?\d\.\d{2})(?:\s+[-.\d]*)?\s*", line):
            year = int(match[1])
            if year in values:
                raise ValueError("NASA annual year repeated")
            values[year] = match[2]
    if sorted(values) != list(range(1880, max(values, default=1880) + 1)):
        raise ValueError("NASA annual year table has a gap")
    return values


def parse_monthly(body):
    columns = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    text = body.decode("utf-8-sig")
    first = next(i for i, line in enumerate(text.splitlines()) if line.startswith("Year,"))
    values = {}
    for row in csv.DictReader(StringIO("\n".join(text.splitlines()[first:]))):
        year = int(row["Year"])
        if year in values:
            raise ValueError("NASA monthly year repeated")
        series = [None if row[col].strip() == "***" else float(decimal(row[col])) for col in columns]
        first_missing = next((i for i, value in enumerate(series) if value is None), len(series))
        if any(value is not None for value in series[first_missing:]):
            raise ValueError("NASA monthly dates contain an internal gap")
        values[year] = series[:first_missing]
    return values


def _price(data, contract, stamp, *, after=False):
    venue, instrument = contract["venue"], contract["instrument_id"]
    if not contract["opened_at"] or not contract["closed_at"]:
        return None
    if not parse_time(contract["opened_at"]) <= stamp < parse_time(contract["closed_at"]):
        return None
    if venue == venue_archive.KALSHI:
        # Use fully closed UTC-day candles outside the public release day.
        day = stamp.replace(hour=0, minute=0, second=0, microsecond=0)
        end = day + timedelta(days=2) if after else day
        start = end - timedelta(days=2)
        kind = venue_storm.CANDLE_KIND
    else:
        start, end = (stamp + timedelta(minutes=10), stamp + timedelta(minutes=20)) if after else (
            stamp - timedelta(minutes=20), stamp - timedelta(minutes=10),
        )
        kind = venue_history.DISPLAY_KIND
    with venue_archive.connect(data) as db:
        found = db.execute(
            """SELECT at,receipt_id,body FROM history WHERE venue=? AND instrument_id=? AND kind=?
            AND at>=? AND at<=? ORDER BY at DESC LIMIT 1""", (venue, instrument, kind, iso(start), iso(end)),
        ).fetchone()
    if not found:
        return None
    raw = json.loads(found["body"])
    prop = _probability_proxy(raw, venue)
    return {**prop, "at": found["at"], "receipt_id": found["receipt_id"], "raw": raw, "kind": kind} if prop else None


def gather(data):
    started = datetime.now(timezone.utc)
    annual, annual_receipt = _get(data, "yearly-20261115.txt", YEARLY)
    monthly, monthly_receipt = _get(data, "monthly-20261115.csv", MONTHLY)
    _, calendar_receipt = _get(data, "calendar-20261115.html", SCHEDULE)
    _, older_receipt = _get(data, "archived-vintages.zip", ARCHIVED)
    annual_values, monthly_values = parse_annual(annual), parse_monthly(monthly)
    publications, omitted = {}, {}
    for year, (amount, published, url) in PRESS.items():
        extension = "pdf" if url.endswith(".pdf") else "html"
        try:
            raw, receipt = _get(data, f"{year}-first-nasa.{extension}", url)
            if extension == "html" and amount not in venue_storm.Page(raw).text():
                raise ValueError("NASA release does not contain inspected amount")
            publications[year] = {"amount_c": amount, "day": published, "receipt": receipt}
        except (OSError, ValueError) as error:
            omitted[year] = str(error)
    with venue_archive.connect(data) as db:
        contracts = [dict(row) for row in db.execute(
            f"""SELECT {venue_archive.PUBLIC_SELECT} FROM markets WHERE family IN ('annual-temperature','annual-temperature-rank')
            AND resolution_source=? ORDER BY day,venue,instrument_id""", (GISS_SOURCE,),
        )]
    clocks = []
    for contract in contracts:
        if contract["status"] != "open":
            continue
        timeline = []
        for clock in ISSUE_CLOCKS:
            stamp = parse_time(clock)
            timeline.append({"release_clock": clock, "before": _price(data, contract, stamp),
                             "after": _price(data, contract, stamp, after=True)})
        clocks.append({"contract": contract["instrument_id"], "venue": contract["venue"], "quotes": timeline})
    return {"version": VERSION, "as_of": iso(started), "annual": annual_values, "monthly": monthly_values,
            "source_receipts": [annual_receipt, monthly_receipt, calendar_receipt, older_receipt],
            "dated_annual_releases": publications, "missing_dated_releases": omitted,
            "contracts": contracts, "market_price_clocks": clocks}


def _rank(annual, target):
    return 1 + sum(value > target for value in annual)


def _rounded(value):
    return decimal(str(value)).quantize(decimal("0.01"), rounding=ROUND_HALF_UP)


def _weather_row(annual, monthly, year, count):
    observed = monthly.get(year, [])[:count]
    if len(observed) < count:
        return None
    values = [float(annual[prior]) for prior in range(year - 10, year)]
    ytd, recent = mean(observed), mean(observed[-3:])
    features = [recent - ytd, ytd - mean(values), recent - mean(observed[-6:-3])]
    return {"year": year, "months_read": count, "observed_months": observed, "ytd": ytd,
            "late_trend": (sum(observed) + (12 - count) * recent) / 12,
            "features": features, "official": float(annual[year]) if year in annual else None}


def _calibrate(annual, monthly, n):
    cases = [_weather_row(annual, monthly, y, n) for y in range(FIT[0], TEST[1] + 1)]
    earlier = [row for row in cases if row and FIT[0] <= row["year"] <= FIT[1]]
    model = Ridge(alpha=0.4).fit([row["features"] for row in earlier], [row["official"] - row["ytd"] for row in earlier])
    for row in cases:
        if row:
            row["ridge"] = row["ytd"] + float(model.predict([row["features"]])[0])
    calibration = [row for row in cases if row and CALIBRATION[0] <= row["year"] <= CALIBRATION[1]]
    residuals = {method: [row["official"] - row[method] for row in calibration] for method in METHODS}
    current = _weather_row(annual, monthly, 2026, n)
    if current:
        current["ridge"] = current["ytd"] + float(model.predict([current["features"]])[0])
    return [row for row in cases if row and row["year"] >= TEST[0]], current, residuals


def _distributions(row, residuals, annual):
    prior = [value for year, value in annual.items() if year < row["year"]]
    output = {}
    for method, errors in residuals.items():
        draws = [_rounded(row[method] + residual) for residual in errors]
        ranks = Counter(min(_rank(prior, draw), 5) for draw in draws)
        alpha = decimal("0.25")
        count = len(draws) + 5 * alpha
        probabilities = {str(rank): float((ranks[rank] + alpha) / count) for rank in range(1, 6)}
        output[method] = {"rank_probabilities": probabilities, "mean_anomaly": mean(float(draw) for draw in draws),
                          "forecast_cent_values": [str(value) for value in draws], "strict_above_all_observed": sum(draw > max(prior) for draw in draws),
                          "equal_to_hottest_observed": sum(draw == max(prior) for draw in draws)}
    return output


def _historical(annual, monthly):
    cases = []
    current = {}
    for n in SAMPLES:
        rows, partial, residuals = _calibrate(annual, monthly, n)
        for row in rows:
            year, value = row["year"], annual[row["year"]]
            rank = _rank([v for other, v in annual.items() if other < year], value)
            cases.append({**row, "rank": min(rank, 5), "distribution": _distributions(row, residuals, annual)})
        if partial:
            current[str(n)] = {**partial, "distribution": _distributions(partial, residuals, annual)}
    return cases, current


def _score(rows, label):
    metrics = []
    for method, name in METHODS.items():
        scores = defaultdict(list)
        errors = []
        for row in rows:
            replay = row["distribution"][method]
            errors.append(abs(replay["mean_anomaly"] - row["official"]))
            for rank, probability in replay["rank_probabilities"].items():
                scores[row["year"]].append((probability - int(int(rank) == row["rank"])) ** 2)
        metrics.append({"group": label, "method": method, "label": name, "bucket_cases": 5 * len(rows),
                        "calendar_years": len(scores), "bucket_brier": mean(mean(points) for points in scores.values()),
                        "annual_anomaly_mae_c": mean(errors)})
    return metrics


def _comparison(contract, temperature, past):
    if contract["venue"] == venue_archive.POLY:
        rank = _rank(past.values(), temperature)
        lower = int(contract["lower"])
        return rank >= lower if contract["operator"] == "at-least" else rank == lower
    lower = decimal(contract["lower"]) if contract["lower"] is not None else None
    upper = decimal(contract["upper"]) if contract["upper"] is not None else None
    op = contract["operator"]
    if op == "above" and lower is not None:
        return temperature > lower
    if op == "below" and upper is not None:
        return temperature < upper
    if op == "between-inclusive" and lower is not None and upper is not None:
        return lower <= temperature <= upper
    rule = contract["rules_primary"]
    if contract["series_id"] == "KXGTEMP" and op == "above-threshold-and-reference":
        reference = GISS_REFERENCE.search(rule)
        if lower is not None and reference and int(reference[1]) in past:
            return temperature > max(lower, past[int(reference[1])])
    return None


def _native_checks(corpus, annual):
    originals = {int(year): row for year, row in corpus["dated_annual_releases"].items()}
    cases = []
    for contract in corpus["contracts"]:
        if contract["yes_payout"] not in ("0", "1"):
            continue
        year = int(contract["day"][:4])
        source = originals.get(year)
        if not source:
            continue
        past = {previous: decimal(row["amount_c"]) for previous, row in originals.items() if previous < year}
        amount = decimal(source["amount_c"])
        expected = _comparison(contract, amount, past)
        cases.append({"contract": contract, "source": source, "latest_amount_c": str(annual[year]),
                      "same_value_as_latest": annual[year] == amount, "from_release_rule": expected,
                      "verified_native_yes": int(contract["yes_payout"]),
                      "same_outcome": expected is not None and int(expected) == int(contract["yes_payout"])})
    return cases


def _contingency(corpus, annual):
    contracts = {row["instrument_id"]: row for row in corpus["contracts"] if row["day"] == "2026-01-01"}
    ranked = [value for value in annual.values()]
    best = max(ranked)
    values = []
    for amount in (best - decimal("0.01"), best, best + decimal("0.01")):
        numbers = {instrument: _comparison(contract, amount, annual) for instrument, contract in contracts.items()}
        values.append({"first_release_scenario_c": str(amount), "computed_rank": _rank(ranked, amount),
                       "eligible_payouts_if_hypothesis_holds": numbers})
    return values


def _cases(corpus, verified, scenarios):
    lookup = {row["instrument_id"]: row for row in corpus["contracts"]}
    prices = {row["contract"]: row["quotes"] for row in corpus["market_price_clocks"]}
    cases = []
    for instrument in ("KXGTEMP-26-P0", "trc-yr-httst-rnk-glbl-2026-1st", "KXGTEMP-27-P0"):
        if instrument not in lookup:
            continue
        row = lookup[instrument]
        detail = ("The ticker names 2027 but the instrument rule settles on calendar-year 2026."
                  if instrument == "KXGTEMP-27-P0" else
                  "A tie at the current NASA record would qualify as rank one under Polymarket US rules. "
                  "Kalshi requires a value strictly above 1.28°C and above the 2025 value.")
        cases.append({"venue": row["venue"], "instrument_id": instrument, "date": row["day"], "at": corpus["as_of"],
                      "title": row["title"], "detail": detail,
                      "data": {"contract": row, "known_price_clocks": prices.get(instrument), "threshold_scenarios": scenarios}})
    for row in verified:
        contract = row["contract"]
        if contract["instrument_id"] not in ("GTEMP-23-P1.02", "GTEMP-24-1.02/23", "KXGTEMP-25-P0"):
            continue
        payout = row["verified_native_yes"]
        cases.append({"venue": contract["venue"], "instrument_id": contract["instrument_id"], "date": contract["day"],
                      "at": contract["settlement_at"], "title": contract["title"],
                      "detail": f"NASA release printed {row['source']['amount_c']}°C. Verified native YES paid ${payout}.",
                      "data": row})
    return cases


def evaluate(data, corpus):
    annual = {int(year): decimal(value) for year, value in corpus["annual"].items()}
    monthly = {int(year): values for year, values in corpus["monthly"].items()}
    history, current = _historical(annual, monthly)
    metrics = _score(history, "All identical held-out yearly ranks")
    for month in SAMPLES:
        metrics += _score([row for row in history if row["months_read"] == month], f"First {month} months available")
    verified = _native_checks(corpus, annual)
    scenarios = _contingency(corpus, annual)
    counts = Counter()
    for row in corpus["market_price_clocks"]:
        for outcome in row["quotes"]:
            counts[f"{row['venue']}: coherent prior point"] += outcome["before"] is not None
            counts[f"{row['venue']}: coherent later point"] += outcome["after"] is not None
            counts[f"{row['venue']}: coherent pair"] += outcome["before"] is not None and outcome["after"] is not None
    revised = {year: str(annual[int(year)]) for year, row in corpus["dated_annual_releases"].items()
               if annual[int(year)] != decimal(row["amount_c"])}
    frozen, digest = research_model.freeze_inputs(data, corpus)
    result = {"version": VERSION, "supersedes": EARLIER_RUN, "title": "Same NASA report, different annual temperature payouts",
              "hypothesis": "Late-year monthly temperature levels may improve historical rank probabilities. The current annual "
                            "first-place market and strict record threshold give different outcomes at an exact tie.",
              "target": "NASA GISS calendar-year anomaly and exact rank at the published 0.01°C precision",
              "venues": ["Kalshi", "Polymarket US"],
              "date_window": {"fit calendar years": list(FIT), "residual calibration years": list(CALIBRATION),
                              "untouched years": list(TEST), "forecast examples": 2026},
              "coverage": {"official annual rows": len(annual), "held-out years": len({row['year'] for row in history}),
                           "current year months": len(monthly.get(2026, [])), "contract instruments": len(corpus["contracts"]),
                           "actual archived Kalshi payouts with dated NASA release": len(verified),
                           "per-instrument historical outcomes reproduced": sum(row["same_outcome"] for row in verified),
                           "active native payouts": 0, "historical quote clocks": dict(counts),
                           "published historical revisions": revised},
              "forecast_current": current, "threshold_illustrations": scenarios,
              "missing": corpus["missing_dated_releases"], "metrics": metrics, "cases": _cases(corpus, verified, scenarios),
              "limits": ["The historical forecast uses the currently published monthly table truncated after each calendar cutoff. "
                         "NASA can revise monthly values. Dated annual announcements support specific payout checks.",
                         "NASA's statistical descriptions of an effective tie use estimation uncertainty. The Polymarket US table rank "
                         "uses exact equal published values and counts only strictly warmer historical years ahead.",
                         "Calendar-year 2026 is incomplete. The tie illustration holds the known historical table fixed and is not an observed payout.",
                         "Polymarket US comparison uses a fixed intraday window and Kalshi uses complete daily candles. The two price clock windows "
                         "describe the source records separately and do not support synchronous execution."],
              "provenance": {"frozen_input": str(frozen.relative_to(data)), "frozen_input_sha256": digest, "scikit_learn": sklearn_version,
                             "governing_graph_table": YEARLY, "monthly_table": MONTHLY, "release_schedule": SCHEDULE,
                             "source_sha256": research_model.run_sources(sys.modules[__name__], venue_storm, venue_history)}}
    return research_model.write_record(data, result, {"held_out_years": history, "native_checks": verified,
                                                      "market_price_clocks": corpus["market_price_clocks"]})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--input", type=Path)
    args = parser.parse_args(argv)
    corpus = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else gather(args.data)
    print(evaluate(args.data, corpus))


if __name__ == "__main__":
    main()
