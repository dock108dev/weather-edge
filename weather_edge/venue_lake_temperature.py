"""Compare NOAA's revised lake-temperature series with the original venue expiration."""

import argparse
import csv
from datetime import date, datetime, time, timedelta, timezone
import gzip
from io import StringIO
import json
from pathlib import Path
import re
from statistics import mean, NormalDist, pstdev
import sys

from sklearn import __version__ as sklearn_version
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from threadpoolctl import threadpool_limits

from . import research_model, venue_archive, venue_benchmark, venue_storm
from .collector import paused
from .market import parse_time
from .venue_sources import CollectionPaused

VERSION = "lake-michigan-source-versions-v1"
FOLDER = "great-lakes-sst"
FIRST, FIT_START, FIT_END, CALIBRATION_END, HOLD_END = 1995, 2001, 2008, 2011, 2025
LEADS, LAGS = (0, 3, 7, 10), (12, 2)
STRIKES = (19.0, 20.75, 21.0, 21.25, 23.0)
EXACT = date(2022, 8, 29)
BASE = "https://apps.glerl.noaa.gov/coastwatch/"
NOAA_METHOD = "https://coastwatch.glerl.noaa.gov/satellite-data-products/great-lakes-surface-environmental-analysis-glsea/"
LAKES = {"michigan": "m", "huron": "h", "superior": "s", "erie": "e", "ontario": "o"}
SOURCES = {f"noaa-{name}-all.csv": BASE + f"webdata/statistic/csv/all_year_glsea_avg_{code}_C.csv" for name, code in LAKES.items()} | {
    "series-contract.pdf": "https://assets.kalshi.com/regulatory/product-certifications/MICHTEMP.pdf",
    "noaa-2022-original.dat": BASE + "ftp/glsea/avgtemps/2022/glsea-temps2022_1024.dat",
    "noaa-2022-acs.dat": "https://apps.glerl.noaa.gov/erddap/files/glsea_avgtemps_3/2022/glsea-temps2022_1024_3.dat",
}
MODELS = {"calendar": "Earlier calendar climatology", "persistence": "Lagged lake temperature persistence",
          "anomaly": "Recent anomaly held against target climatology", "ridge": "Ridge with own-lake trend",
          "regional_ridge": "Ridge with own-lake and nearby lake histories",
          "gradient": "Histogram gradient boosting with regional histories"}


def _obtain(data, name, collect):
    if collect and paused(data):
        raise CollectionPaused
    source = SOURCES[name]
    raw, receipt = (venue_storm._get(data, name, source, folder=FOLDER) if collect
                    else venue_storm._existing(data, name, folder=FOLDER))
    if receipt["source"] != source:
        raise ValueError(f"Different original URL retained for {name}")
    return raw, receipt


def _parse(raw):
    result = {}
    for row in csv.DictReader(StringIO(raw.decode("utf-8-sig"))):
        ordinal = int(row.pop(""))
        if not 1 <= ordinal <= 366:
            raise ValueError("Unexpected daily lake source ordinal")
        for year, temp in row.items():
            if not temp or temp == "NaN":
                continue
            target = date(int(year), 1, 1) + timedelta(days=ordinal - 1)
            value = float(temp)
            if target.year != int(year) or not 0 <= value <= 36 or str(target) in result:
                raise ValueError(f"Unexpected daily lake value {target} {temp}")
            result[str(target)] = value
    return result


def _target_record(raw):
    if b"Surf. Water Temp. (degrees C)" not in raw or b"Mich." not in raw:
        raise ValueError("Unexpected NOAA lake table headings")
    label = f"{EXACT.year} {EXACT.timetuple().tm_yday:03d}".encode()
    exact = [line for line in raw.splitlines() if line.startswith(label)]
    if len(exact) != 1 or not re.fullmatch(rb"\d+\.\d+", exact[0].split()[3]):
        raise ValueError("Unverified NOAA Lake Michigan value")
    return {"source_date": str(EXACT), "table_line": exact[0].decode(), "temperature_c": float(exact[0].split()[3])}


def _market_rows(data):
    with venue_archive.connect(data) as db:
        db.execute("BEGIN")
        markets = [dict(row) for row in db.execute(f"SELECT {venue_archive.PUBLIC_SELECT} FROM markets WHERE venue=? AND series_id='KXMICHTEMP'",
                                                  (venue_archive.KALSHI,))]
        for market in markets:
            market["price_points"] = {}
            for lead in LEADS:
                stamp = datetime.combine(EXACT - timedelta(days=lead), time(18), timezone.utc)
                if parse_time(market["opened_at"]) <= stamp < parse_time(market["closed_at"]):
                    point, gap = venue_benchmark._history_point(db, market, stamp)
                    market["price_points"][str(lead)] = point or {"unavailable": gap}
            market["native_tape"] = [dict(item) | {"body": json.loads(item["body"])} for item in db.execute(
                """SELECT at,native_key,kind,receipt_id,body FROM history WHERE venue=? AND instrument_id=?
                AND kind IN ('trade','candle-60m') ORDER BY at,native_key""", (venue_archive.KALSHI, market["instrument_id"]),
            )]
    return sorted(markets, key=lambda row: row["instrument_id"])


def gather(data, *, collect=False):
    tables, receipts, target = {}, {}, {}
    for name in LAKES:
        raw, receipts[name] = _obtain(data, f"noaa-{name}-all.csv", collect)
        tables[name] = _parse(raw)
    for version in ("original", "acs"):
        raw, receipts[f"2022 {version}"] = _obtain(data, f"noaa-2022-{version}.dat", collect)
        target[version] = _target_record(raw)
    _, receipts["filed rules"] = _obtain(data, "series-contract.pdf", collect)
    return {"version": VERSION, "series": tables, "receipts": receipts, "2022_target": target, "market_rows": _market_rows(data)}


def _climatology(series, stamp):
    samples = []
    for year in range(FIRST, min(FIT_END, stamp.year - 1) + 1):
        center = stamp.replace(year=year)
        samples.extend(series[key] for delta in range(-7, 8) if (key := str(center + timedelta(days=delta))) in series)
    return mean(samples)


def _rows(corpus, lag):
    tables = corpus["series"]
    michigan = tables["michigan"]
    rows = []
    for key, actual in sorted(michigan.items()):
        target = date.fromisoformat(key)
        if not (FIT_START <= target.year <= HOLD_END and (6, 15) <= (target.month, target.day) <= (9, 15)):
            continue
        thermal_calendar = _climatology(michigan, target)
        for lead in LEADS:
            anchor = target - timedelta(days=lead + lag)
            earlier = anchor - timedelta(days=7)
            last, old = str(anchor), str(earlier)
            if last not in michigan or old not in michigan or any(last not in by_day or old not in by_day for by_day in tables.values()):
                continue
            own = [thermal_calendar, michigan[last], michigan[last]-michigan[old], lead]
            peers = [value for name in LAKES if name != "michigan"
                     for value in (tables[name][last], tables[name][last]-tables[name][old])]
            rows.append({"target": key, "year": target.year, "lead_days": lead, "newest_feature_source_day": last,
                         "actual_c": actual, "calendar": thermal_calendar, "persistence": michigan[last],
                         "anomaly": thermal_calendar + michigan[last] - _climatology(michigan, anchor),
                         "own_features": own, "regional_features": own + peers})
    return rows


def _predictions(rows):
    fit = [row for row in rows if row["year"] <= FIT_END]
    calibration = [row for row in rows if FIT_END < row["year"] <= CALIBRATION_END]
    hold = [row for row in rows if CALIBRATION_END < row["year"] <= HOLD_END]
    actual = [row["actual_c"] for row in fit]
    candidates = {"ridge": ("own_features", make_pipeline(StandardScaler(), Ridge(alpha=10))),
                  "regional_ridge": ("regional_features", make_pipeline(StandardScaler(), Ridge(alpha=10))),
                  "gradient": ("regional_features", HistGradientBoostingRegressor(max_iter=130, max_leaf_nodes=12, random_state=9))}
    with threadpool_limits(limits=2):
        for key, (features, model) in candidates.items():
            model.fit([row[features] for row in fit], actual)
            predicted = model.predict([row[features] for row in calibration + hold])
            for row, estimate in zip(calibration + hold, predicted, strict=True):
                row[key] = float(estimate)
    residual_sd = {lead: {name: max(.1, pstdev(row["actual_c"] - row[name] for row in calibration if row["lead_days"] == lead))
                          for name in MODELS} for lead in LEADS}
    return hold, residual_sd


def _probability(row, model, strike, deviations):
    return 1 - NormalDist(row[model], deviations[row["lead_days"]][model]).cdf(strike)


def _metrics(hold, deviations, group):
    days = len({row["target"] for row in hold})
    return [{"group": group, "model": label, "bucket_cases": len(hold) * len(STRIKES), "calendar_dates": days,
             "calendar_years": len({row["year"] for row in hold}), "mae_c": mean(abs(row["actual_c"] - row[key]) for row in hold),
             "brier": mean((_probability(row, key, threshold, deviations) - int(row["actual_c"] > threshold)) ** 2
                           for row in hold for threshold in STRIKES)} for key, label in MODELS.items()]


def _cases(corpus, hold, deviations):
    original = corpus["2022_target"]["original"]["temperature_c"]
    developed = corpus["2022_target"]["acs"]["temperature_c"]
    lookup = {row["lead_days"]: row for row in hold if row["target"] == str(EXACT)}
    cases = []
    for market in corpus["market_rows"]:
        if market["operator"] != "above" or market["lower"] is None:
            continue
        strike = float(market["lower"])
        forecasts = {lead: {name: _probability(row, name, strike, deviations) for name in MODELS} for lead, row in lookup.items()}
        trades = [{"at": row["at"], "yes": float(row["body"]["yes_price_dollars"])} for row in market["native_tape"]
                  if row["kind"] == "trade" and row["body"].get("yes_price_dollars") is not None]
        asks = [{"at": row["at"], "yes": float(price)} for row in market["native_tape"] if row["kind"] == "candle-60m"
                and (price := venue_archive.kalshi_dollar_close(row["body"].get("yes_ask"))) is not None]
        cases.append({"venue": market["venue"], "instrument_id": market["instrument_id"], "date": str(EXACT), "at": market["settlement_at"],
                      "title": f"Lake Michigan's surface was above {strike:.2f}°C on August 29, 2022",
                      "detail": f"Native YES paid ${market['yes_payout']} at {market['expiration_value']}°C. "
                                f"NOAA's retained legacy source row is {original:.2f}°C and the ACSPO reanalysis is {developed:.2f}°C. "
                                "All three values clear this strike.",
                      "venue_price_timeline": {"series": [{"label": label, "kind": kind, "points": points} for label, kind, points in
                          (("Kalshi actual prints", "trade", trades), ("Kalshi hourly YES ask", "display", asks)) if points],
                          "markers": [{"at": market["closed_at"], "label": "Native venue trading close", "kind": "close"}]},
                      "data": {"market": market, "NOAA_retained_2022_sources": corpus["2022_target"], "lead_forecasts": forecasts,
                               "native_fixed_clock_prices": market["price_points"]}})
    return cases


def evaluate(data, corpus):
    frozen, digest = research_model.freeze_inputs(data, corpus)
    results = {lag: _predictions(_rows(corpus, lag)) for lag in LAGS}
    hold, uncertainty = results[LAGS[0]]
    metrics = _metrics(hold, uncertainty, "All held summer dates using at least twelve days of feature lag")
    for lead in LEADS:
        for lag, (rows, spread) in results.items():
            group = f"{lead} days before target · {lag}-day source lag" + (" · hindsight sensitivity" if lag != LAGS[0] else "")
            metrics += _metrics([row for row in rows if row["lead_days"] == lead], spread, group)
    cases = _cases(corpus, hold, uncertainty)
    source_counts = {name: len(rows) for name, rows in corpus["series"].items()}
    record = {"version": VERSION, "title": "Lake Michigan surface temperature, source revisions and usable lags",
              "hypothesis": "An earlier same-lake anomaly helps predict later lake-wide temperatures. Nearby lakes may add regional information.",
              "target": "GLSEA daily whole-lake Michigan surface temperature and separately documented native Kalshi payouts",
              "date_window": {"initial climatology years": [FIRST, FIT_START - 1], "fit calendar years": [FIT_START, FIT_END],
                              "calibration years": [FIT_END + 1, CALIBRATION_END], "held calendar years": [CALIBRATION_END + 1, HOLD_END]},
              "venues": ["Kalshi"],
              "coverage": {"retained_daily_estimates": source_counts, "native_settled_instruments": len(cases),
                           "native_calendar_days": len({row["date"] for row in cases}), "native_price_and_cash_clocks": sum(
                               "probability" in point for market in corpus["market_rows"] for point in market["price_points"].values())},
              "metrics": metrics, "cases": cases, "missing": {"Original 2022 publication timestamp and revision chain": "Unavailable in latest NOAA tables"},
              "limits": ["NOAA combines satellite imagery within a window extending up to ten days past the source date. A twelve-day lag is used for the primary replay.",
                         "A separate two-day source-lag sensitivity demonstrates how a less conservative clock can affect a retrospective estimate.",
                         "NOAA's bulk lake summaries are current retrospective estimates. They do not prove any old local receipt or first value.",
                         "Historical trades and hourly quotes cover a single native day. Public trade tape has no executable depth."],
              "provenance": {"analysis method": NOAA_METHOD, "filed native terms": SOURCES["series-contract.pdf"],
                             "sources": corpus["receipts"], "scikit_learn": sklearn_version, "lead_days": LEADS,
                             "thresholds_c": STRIKES, "frozen_input": str(frozen.relative_to(data)), "frozen_input_sha256": digest,
                             "source_sha256": research_model.run_sources(sys.modules[__name__], venue_storm, venue_benchmark)}}
    return research_model.write_record(data, record, {"held_primary": hold, "calibrated_residual_sd_c": uncertainty,
                                                      "2022_NOAA_target": corpus["2022_target"], "native_tape": corpus["market_rows"]})


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
