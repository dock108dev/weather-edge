"""Model Cincinnati's calendar-quarter heating degree days from archived station data."""

from __future__ import annotations

import argparse
import csv
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import gzip
from hashlib import sha256
from io import StringIO
import json
from math import sqrt
from pathlib import Path
from statistics import mean, NormalDist
import sys
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import numpy as np
from sklearn import __version__ as sklearn_version
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from . import research_model, strategy_twc, venue_archive
from .history import _write
from .nbm import iso, utc

VERSION = "cincinnati-heating-degree-days-v1"
STATION = "KCVG"
GHCN_STATION = "USW00093814"
SERIES = "KXHDDCVG"
LOCAL = ZoneInfo("America/New_York")
FIRST_LIVE_DAY = date(2026, 10, 1)
LAST_LIVE_DAY = date(2026, 12, 31)
AS_OF_DAY = (11, 14)
WINDOWS = {"fit": (1986, 2008), "calibration": (2009, 2015), "test": (2016, 2025)}
FILING = "https://www.cftc.gov/filings/ptc/ptc09182627902.pdf"
NCEI_DOCS = "https://www.ncei.noaa.gov/products/land-based-station/global-historical-climatology-network-daily"
NCEI_URL = "https://www.ncei.noaa.gov/access/services/data/v1?" + urlencode(
    {"dataset": "daily-summaries", "stations": GHCN_STATION, "startDate": "1986-10-01", "endDate": "2025-12-31",
     "dataTypes": "TMAX,TMIN", "units": "standard", "includeAttributes": "true", "format": "csv"}
)
METHODS = {"quarter": "Earlier full-season climatology", "observed": "Known accumulation plus earlier remaining seasons",
           "recent_climate": "Known accumulation plus recency-weighted earlier seasons", "ridge": "Known accumulation plus recent-temperature correction"}


def _ghcn(data):
    root = data / "expansion" / "hdd"
    metadata = root / "kcvg-ghcn-1986-2025.json"
    if metadata.is_file():
        receipt = json.loads(metadata.read_text())
        content = (data / receipt["raw_path"]).read_bytes()
        if sha256(content).hexdigest() != receipt["body_sha256"]:
            raise ValueError("Archived GHCN receipt digest mismatch")
        return content, receipt
    start = datetime.now(timezone.utc)
    with urlopen(Request(NCEI_URL, headers={"Accept": "text/csv", "User-Agent": "Weather Edge research"}), timeout=25) as source:
        content = source.read()
        received = datetime.now(timezone.utc)
        last_modified = source.headers.get("Last-Modified")
    if not content.startswith(b'"STATION","DATE"'):
        raise ValueError("Unexpected NOAA daily-summaries response")
    digest = sha256(content).hexdigest()
    path = root / f"kcvg-ghcn-1986-2025-{digest[:16]}.csv"
    receipt = {"source": NCEI_URL, "dataset": "GHCN-Daily station summaries", "station": GHCN_STATION, "units": "degrees Fahrenheit",
               "requested_at": iso(start), "received_at": iso(received), "last_modified": last_modified,
               "availability": "retrospective NOAA data; original daily publication times unavailable",
               "body_sha256": digest, "raw_path": str(path.relative_to(data))}
    _write(path, content)
    _write(metadata, json.dumps(receipt, indent=2).encode())
    return content, receipt


def _hdd(temperature):
    return max(Decimal(0), Decimal(65) - Decimal(str(temperature))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _span(first, last):
    return [first + timedelta(days=offset) for offset in range((last - first).days + 1)]


def _historical(content):
    daily = {}
    for row in csv.DictReader(StringIO(content.decode("utf-8"))):
        if row["STATION"] != GHCN_STATION:
            raise ValueError("Unexpected NOAA daily-summaries station")
        day = date.fromisoformat(row["DATE"])
        if day.month < 10 or not row["TMAX"] or not row["TMIN"]:
            continue
        attributes = [row[f"{field}_ATTRIBUTES"].split(",") for field in ("TMAX", "TMIN")]
        if any(len(parts) < 2 or parts[1] for parts in attributes):
            continue
        high, low = (Decimal(row[field]) for field in ("TMAX", "TMIN"))
        if high < low or not -150 <= low <= high <= 150:
            continue
        period_mean = ((high + low) / 2).quantize(Decimal(1), rounding=ROUND_HALF_UP)
        daily[day] = float(_hdd(period_mean))
    rows, skipped = [], []
    for year in range(WINDOWS["fit"][0], WINDOWS["test"][1] + 1):
        days = _span(date(year, 10, 1), date(year, 12, 31))
        values = [daily.get(day) for day in days]
        if any(value is None for value in values):
            skipped.append({"year": year, "missing": [str(day) for day in days if day not in daily]})
            continue
        observed = (date(year, *AS_OF_DAY) - days[0]).days + 1
        rows.append({"year": year, "through": str(days[observed - 1]), "daily_hdd": values,
                     "observed": sum(values[:observed]), "recent_14": sum(values[observed - 14:observed]),
                     "remaining": sum(values[observed:]), "total": sum(values)})
    return rows, skipped


def _market(data, cutoff):
    with venue_archive.connect(data) as db:
        rows = db.execute("""SELECT instrument_id,event_id,title,label,operator,lower,upper,rules_primary,rules_secondary,rules_url,
            source,status,opened_at,closed_at,last_received_at,yes_payout,settlement_at,settlement_receipt_id,expiration_value
            FROM markets WHERE venue=? AND series_id=? ORDER BY CAST(upper AS REAL),instrument_id""", ("kalshi", SERIES))
        markets = [dict(row) for row in rows]
    native = []
    for market in markets:
        primary = market["rules_primary"] or ""
        secondary = market["rules_secondary"] or ""
        if ("KCVG, WMO identifier 72421" in primary and "Oct 1, 2026 and Dec 31, 2026" in primary
                and "The Weather Company" in secondary and "HDD (65)" in secondary and "Hourly Mean(F)" in secondary
                and market["operator"] == "below" and market["upper"] is not None and utc(market["last_received_at"]) <= cutoff):
            native.append(market)
    return native


def _live(data, cutoff):
    through = date(FIRST_LIVE_DAY.year, *AS_OF_DAY)
    reports = strategy_twc.reports(data, station=STATION, start=str(FIRST_LIVE_DAY), end=str(LAST_LIVE_DAY), as_of=cutoff)
    approved = {row["label_date"]: row for row in reports if row["provider_status"] in {"official", "revised"} and row["timezone"] == str(LOCAL)}
    witnessed = []
    for day in _span(FIRST_LIVE_DAY, through):
        row = approved.get(str(day))
        if not row:
            continue
        try:
            value = Decimal(str(row["native"]["hdd"]))
        except (InvalidOperation, KeyError):
            continue
        if not value.is_finite() or value < 0:
            continue
        comparison = _hdd(row["hourly_average_f"]) if row["hourly_average_f"] is not None else None
        witnessed.append({"date": str(day), "hdd_65": str(value.quantize(Decimal("0.01"))),
                          "hourly_counterfactual_hdd": str(comparison) if comparison is not None else None,
                          "receipt_id": row["receipt_id"], "body_sha256": row["body_sha256"], "source": row["source_url"],
                          "provider_status": row["provider_status"], "available_at": row["available_at"],
                          "hours_reported": row["hours_reported"], "official_max_f": row["temperature_high_f"],
                          "official_min_f": row["temperature_low_f"], "hourly_average_f": row["hourly_average_f"]})
    total = sum(Decimal(row["hdd_65"]) for row in witnessed)
    same_case = [row for row in witnessed if row["hourly_counterfactual_hdd"] is not None]
    return {"through": str(through), "expected_days": (through - FIRST_LIVE_DAY).days + 1, "received_days": witnessed,
            "total": float(total), "recent_14": sum(float(row["hdd_65"]) for row in witnessed[-14:]),
            "same_case_direct": float(sum(Decimal(row["hdd_65"]) for row in same_case)),
            "same_case_hourly": float(sum(Decimal(row["hourly_counterfactual_hdd"]) for row in same_case)),
            "markets": _market(data, cutoff)}


def gather(data, cutoff):
    original, receipt = _ghcn(data)
    seasons, skipped = _historical(original)
    return {"version": VERSION, "cutoff": iso(cutoff), "history": seasons, "history_source": receipt, "skipped": skipped,
            "live": _live(data, cutoff)}


def _features(row):
    return [row["observed"], row["recent_14"]]


def _calibrate(seasons):
    sample = {name: [row for row in seasons if start <= row["year"] <= finish] for name, (start, finish) in WINDOWS.items()}
    training, calibration = (sample[key] for key in ("fit", "calibration"))
    if min(len(training), len(calibration), len(sample["test"])) < 2:
        raise ValueError("No independent seasonal calibration and verification evidence")
    model = make_pipeline(StandardScaler(), Ridge(alpha=10))
    model.fit([_features(row) for row in training], [row["remaining"] for row in training])
    biases = [row["remaining"] - estimate for row, estimate in zip(calibration, model.predict([_features(row) for row in calibration]), strict=True)]
    residual_bias = mean(biases)
    sd = max(1, sqrt(mean((value - residual_bias) ** 2 for value in biases)))
    return sample, model, (float(residual_bias), float(sd))


def _empirical(samples, thresholds, weights=None):
    weights = np.asarray(weights if weights is not None else np.ones(len(samples)))
    values = np.asarray(samples)
    return {"point_hdd": float(np.average(values, weights=weights)),
            "probabilities": [float(np.average(values < strike, weights=weights)) for strike in thresholds]}


def _prediction(row, train, model, residual, thresholds):
    remaining = [prior["remaining"] for prior in train]
    conditioned = [row["observed"] + value for value in remaining]
    weights = [0.5 ** ((train[-1]["year"] - prior["year"]) / 10) for prior in train]
    point = row["observed"] + float(model.predict([_features(row)])[0]) + residual[0]
    dist = NormalDist(point, residual[1])
    return {"quarter": _empirical([prior["total"] for prior in train], thresholds),
            "observed": _empirical(conditioned, thresholds), "recent_climate": _empirical(conditioned, thresholds, weights),
            "ridge": {"point_hdd": point, "probabilities": [dist.cdf(strike - 0.005) for strike in thresholds]}}


def _score(samples, model, residual, thresholds):
    output = []
    for row in samples["test"]:
        estimates = _prediction(row, samples["fit"], model, residual, thresholds)
        for method in estimates.values():
            method["mae_hdd"] = abs(method["point_hdd"] - row["total"])
            method["brier"] = mean((p - int(row["total"] < strike)) ** 2
                                   for p, strike in zip(method["probabilities"], thresholds, strict=True))
        output.append({**row, "estimates": estimates})
    return output


def evaluate(data, inputs):
    frozen, digest = research_model.freeze_inputs(data, inputs)
    sample, model, residual = _calibrate(inputs["history"])
    live = inputs["live"]
    markets = live["markets"]
    thresholds = sorted({float(row["upper"]) for row in markets})
    if not thresholds:
        raise ValueError("No verifiable native Cincinnati HDD strikes")
    test = _score(sample, model, residual, thresholds)
    metrics = [{"group": "Untouched seasonal weather proxy", "method": method, "label": name,
                "independent_seasons": len(test), "strike_cases": len(test) * len(thresholds),
                "mae_hdd": mean(row["estimates"][method]["mae_hdd"] for row in test),
                "bucket_brier": mean(row["estimates"][method]["brier"] for row in test)} for method, name in METHODS.items()]
    contrasts = []
    for method in ("observed", "recent_climate", "ridge"):
        errors = [row["estimates"][method]["brier"] - row["estimates"]["quarter"]["brier"] for row in test]
        simulation = np.random.default_rng(20261115).choice(errors, size=(2500, len(errors))).mean(axis=1)
        contrasts.append({"baseline": "quarter", "method": method, "mean_brier_difference": mean(errors),
                          "season_bootstrap_ci_95": np.quantile(simulation, (0.025, 0.975)).tolist()})
    estimates = {}
    if len(live["received_days"]) == live["expected_days"]:
        values = {"observed": live["total"], "recent_14": live["recent_14"]}
        estimates = _prediction(values, sample["fit"], model, residual, thresholds)
    chosen = min(markets, key=lambda row: abs(float(row["upper"]) - estimates.get("observed", {}).get("point_hdd", 1400)))
    seen = live["received_days"]
    detail = (f"{len(seen)}/{live['expected_days']} complete station days through November 14; "
              f"published HDD totals {live['total']:.2f}. The hourly mean calculation would give "
              f"{live['same_case_hourly']:.2f} instead of {live['same_case_direct']:.2f} on its common days.")
    cases = [{"date": live["through"], "at": inputs["cutoff"], "station": STATION, "venue": "kalshi", "instrument_id": chosen["instrument_id"],
              "title": "Cincinnati · Live quarter-to-date daily heating values", "detail": detail,
              "market": chosen, "available_days": seen, "thresholds": thresholds, "model_estimates": estimates}]
    record = {"version": VERSION, "title": "Cincinnati heating degree days through December",
              "hypothesis": "Replacing known elapsed days with their station accumulation improves on whole-quarter climatology. "
                            "Recent weather and recent climate are distinct prospective additions.",
              "target": "Calendar-quarter KCVG HDD(65), with native market and off-market historical climate proxy", "venues": ["kalshi"],
              "date_window": {**WINDOWS, "native_market": [str(FIRST_LIVE_DAY), str(LAST_LIVE_DAY)]},
              "coverage": {key: len(group) for key, group in sample.items()}, "metrics": metrics,
              "cases": cases, "uncertainty": contrasts, "omitted_seasons": inputs["skipped"],
              "limits": ["Historical seasons use GHCN station temperature extrema and a rounded daily mean. The earlier-year values are a climatological proxy without original publication clocks.",
                         "Current accumulated days use the locally received official provider HDD(65) column. The filed fallback first requires a missing direct value and a Final hourly mean.",
                         "There is one current native quarter in this series. Its December value and payout remain open in this snapshot. Weather model probabilities are exploratory."],
              "provenance": {"frozen_input": str(frozen.relative_to(data)), "frozen_input_sha256": digest,
                             "ncei_documentation": NCEI_DOCS, "original_history": inputs["history_source"], "market_filing": FILING,
                             "provider": strategy_twc.SITE, "scikit_learn": sklearn_version,
                             "source_sha256": research_model.run_sources(sys.modules[__name__])}}
    return research_model.write_record(data, record, {"test": test, "live": live, "live_estimates": estimates})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--input", type=Path)
    args = parser.parse_args()
    inputs = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else gather(args.data, datetime.now(timezone.utc))
    print(evaluate(args.data, inputs))


if __name__ == "__main__":
    main()
