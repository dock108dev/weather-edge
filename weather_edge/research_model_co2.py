"""Use the NASA-displayed Mauna Loa monthly measurement for the native CO₂ thresholds."""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from email.utils import parsedate_to_datetime
import gzip
import hashlib
import json
from pathlib import Path
import re
from statistics import mean
import sys
from urllib.request import Request, urlopen

import numpy as np
from sklearn import __version__ as sklearn_version
from sklearn.linear_model import Ridge

from . import research_model, venue_archive
from .history import _write
from .nbm import iso, utc

VERSION = "mauna-loa-monthly-co2-v1"
NASA_PAGE = "https://science.nasa.gov/earth/explore/earth-indicators/carbon-dioxide/"
NASA_CHART = "https://assets.science.nasa.gov/content/dam/science/microapps/vital-signs/data/charts/co2.json"
NOAA_TABLE = "https://gml.noaa.gov/webdata/ccgg/trends/co2/co2_mm_mlo.txt"
CONTRACT = "https://assets.kalshi.com/contract_terms/CO2LEVEL.pdf"
FILING = "https://www.cftc.gov/filings/ptc/ptc07012525130.pdf"
SERIES = "KXCO2LEVEL"
BEFORE = date(2030, 1, 1)
CALIBRATION = (1976, 2003)
TEST = (2008, 2022)
HORIZON = 39
DISPLACED = (date(2022, 12, 1), date(2023, 7, 1))
METHODS = {"seasonal": "Prior observed monthly pattern",
           "linear": "Ten-year seasonal linear trend",
           "recent": "Five-year seasonal linear trend",
           "curve": "Fifteen-year seasonal trend with curvature"}


def _capture(data, name, source, *, refresh=False):
    root = data / "expansion" / "co2"
    pointer = root / f"{name}.receipt.json"
    if pointer.is_file() and not refresh:
        receipt = json.loads(pointer.read_bytes())
        raw = (data / receipt["path"]).read_bytes()
        if receipt["url"] != source or hashlib.sha256(raw).hexdigest() != receipt["sha256"]:
            raise ValueError("CO₂ source receipt identity changed")
        return raw, receipt
    requested = datetime.now(timezone.utc)
    with urlopen(Request(source, headers={"User-Agent": "Weather Edge public research"}), timeout=20) as response:
        raw = response.read(2_000_001)
        acquired = datetime.now(timezone.utc)
        headers = {key: response.headers.get(key) for key in ("Date", "Last-Modified", "ETag", "Content-Type")}
        final_url = response.geturl()
    if len(raw) > 2_000_000:
        raise ValueError("CO₂ source response exceeded the expected size")
    modified = iso(parsedate_to_datetime(headers["Last-Modified"])) if headers["Last-Modified"] else None
    sha = hashlib.sha256(raw).hexdigest()
    stored = root / f"{name}.{sha[:16]}.raw"
    if not stored.is_file():
        _write(stored, raw)
    receipt = {"url": source, "resolved_url": final_url, "requested_at": iso(requested), "received_at": iso(acquired),
               "response_last_modified_at": modified, "path": str(stored.relative_to(data)), "sha256": sha, "headers": headers}
    _write(pointer, research_model._json_bytes(receipt))
    return raw, receipt


def _nasa_table(body):
    decoded = json.loads(body, parse_float=Decimal)
    if (decoded["chart_id"] != "CO2_3784" or decoded["chart"]["source_url"] != NOAA_TABLE or decoded["chart"]["abbr_units"] != "ppm"
            or decoded["chart"]["category"] != "CO2" or "Mauna Loa Observatory" not in decoded["chart"]["subtitle"]):
        raise ValueError("Unexpected NASA chart identity")
    precision = decoded["tooltips"]["CO2"][1]
    if type(precision) is not int or not 0 <= precision <= 3:
        raise ValueError("Unknown NASA chart display precision")
    table = {}
    for row in decoded["items"]:
        if row["category_titles"] != ["CO2"] or row["day"] is not None or not row["status"]:
            continue
        month = date(row["year"], row["month"], 1)
        value = Decimal(row["y"])
        if month in table or not value.is_finite() or not 280 <= value <= 600:
            raise ValueError("Invalid Mauna Loa monthly chart value")
        table[month] = value
    if len(table) < 600:
        raise ValueError("Unexpected Mauna Loa chart coverage")
    return table, precision


def _noaa_table(body):
    result = {}
    for line in body.decode().splitlines():
        fields = line.split()
        if line.startswith("#") or len(fields) != 8 or not fields[0].isdigit():
            continue
        year, month, _, amount, *_ = fields
        key = date(int(year), int(month), 1)
        value = Decimal(amount)
        if key in result:
            raise ValueError("Duplicate NOAA monthly value")
        if value.is_finite() and 280 <= value <= 600:
            result[key] = value
    if len(result) < 600:
        raise ValueError("Unexpected NOAA raw monthly table coverage")
    return result


def _whole_ppm_page(body, month):
    page = body.decode()
    match = re.search(r">\s*Latest Measurement\s*</[^>]+>\s*<p[^>]+>\s*(\d+)\s*ppm\s*</p>", page)
    if not match or month.strftime("%B %Y") not in page or NOAA_TABLE not in page:
        return None
    return int(match[1])


def _markets(data, cutoff):
    with venue_archive.connect(data) as db:
        raw = db.execute("""SELECT instrument_id, event_id, title, operator, lower, units, status, yes_payout,
                                  rules_primary, rules_url, opened_at, closed_at, last_received_at, source
                            FROM markets WHERE venue='kalshi' AND series_id=? ORDER BY CAST(lower AS REAL)""", (SERIES,)).fetchall()
    records = []
    for market in map(dict, raw):
        value = market["lower"]
        if (market["event_id"] != "KXCO2LEVEL-30" or market["rules_url"] != CONTRACT or market["operator"] != "at-least"
                or not value or f"at least {value} before Jan 2030" not in market["rules_primary"]
                or utc(market["last_received_at"]) > cutoff):
            continue
        details = venue_archive.detail(data, "kalshi", market["instrument_id"])
        upstream = details["raw_market"] if details else None
        quote = None
        if upstream:
            bid = Decimal(upstream.get("yes_bid_dollars") or "NaN")
            ask = Decimal(upstream.get("yes_ask_dollars") or "NaN")
            if bid.is_finite() and ask.is_finite() and 0 <= bid <= ask <= 1 and (bid, ask) != (0, 1):
                quote = {"display_bid": str(bid), "display_ask": str(ask), "display_midpoint": float((bid + ask) / 2),
                         "catalog_receipt_at": market["last_received_at"]}
        records.append({**market, "verified_threshold_ppm": str(Decimal(value)), "display": quote})
    if not records:
        raise ValueError("No exact native CO₂ threshold contracts")
    return records


def _month_index(month):
    return month.year * 12 + month.month - 1


def _month(index):
    year, offset = divmod(index, 12)
    return date(year, offset + 1, 1)


def gather(data, *, refresh=False):
    bodies, source_receipts = {}, {}
    for name, url in (("nasa-page", NASA_PAGE), ("nasa-chart", NASA_CHART), ("noaa-monthly", NOAA_TABLE), ("venue-terms", CONTRACT)):
        bodies[name], source_receipts[name] = _capture(data, name, url, refresh=refresh)
    nasa, precision = _nasa_table(bodies["nasa-chart"])
    source = _noaa_table(bodies["noaa-monthly"])
    cutoff = max(utc(row["received_at"]) for row in source_receipts.values())
    latest = max(nasa)
    return {"version": VERSION, "cutoff": iso(cutoff), "sources": source_receipts,
            "forecast_precision": precision, "headline_ppm": _whole_ppm_page(bodies["nasa-page"], latest),
            "history": [{"month": month.isoformat(), "nasa_ppm": str(value),
                         "noaa_ppm": str(source[month]) if month in source else None}
                        for month, value in sorted(nasa.items()) if month < cutoff.date().replace(day=1)],
            "missing_noaa_months": len(set(nasa) - set(source)), "markets": _markets(data, cutoff)}


def _design(indices, origin, *, curvature=False):
    trend = (np.asarray(indices) - origin) / 12.0
    months = np.eye(12)[np.asarray(indices) % 12]
    linear = (trend,) if not curvature else (trend, trend ** 2 / 10)
    return np.column_stack((*linear, months[:, 1:]))


def _project(monthly, origin, horizon):
    target = np.arange(origin + 1, origin + horizon + 1)
    # The last completed value for each calendar month is a simple no-trend forecast.
    latest = {index % 12: amount for index, amount in sorted(monthly.items()) if index <= origin}
    output = {"seasonal": [latest[index % 12] for index in target]}
    for method, years, curve in (("linear", 10, False), ("recent", 5, False), ("curve", 15, True)):
        known = sorted((index, amount) for index, amount in monthly.items() if origin - 12 * years < index <= origin)
        x, y = zip(*known, strict=True)
        trend = Ridge(alpha=0.3 if not curve else 4.0).fit(_design(x, origin, curvature=curve), y)
        output[method] = trend.predict(_design(target, origin, curvature=curve)).tolist()
    return {method: [{"month": _month(int(key)).isoformat(), "ppm": float(value)}
                     for key, value in zip(target, amounts, strict=True)] for method, amounts in output.items()}


def _quantized(value, places):
    return Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def _samples(monthly):
    series = {}
    for label, (first, last) in (("calibration", CALIBRATION), ("test", TEST)):
        output = []
        for year in range(first, last + 1):
            origin = _month_index(date(year, 9, 1))
            if origin not in monthly:
                continue
            awaited = [monthly.get(index) for index in range(origin + 1, origin + HORIZON + 1)]
            known = [monthly.get(index) for index in range(origin - 14, origin + 1)]
            if any(value is None for value in (*known, *awaited)):
                continue
            estimates = _project(monthly, origin, HORIZON)
            known_max = max(known)
            output.append({"origin_year": year, "actual_max_ppm": max(*awaited, known_max), "known_max_ppm": known_max,
                           "target_contains_temporary_substitute": date(year, 9, 1) < DISPLACED[1] and date(year + 3, 12, 1) >= DISPLACED[0],
                           "predict": {key: max(row["ppm"] for row in paths) for key, paths in estimates.items()}})
        series[label] = output
    return series


def _binary(target, prediction, residuals, precision, observed):
    successes = sum(_quantized(max(observed, prediction + residual), precision) >= target for residual in residuals)
    return {"shadow_probability": (successes + 0.5) / (len(residuals) + 1), "supporting_older_windows": successes,
            "earlier_windows": len(residuals), "thin_support": min(successes, len(residuals) - successes) < 4}


def _score(sample, residuals, precision):
    output = []
    for row in sample:
        known = row["known_max_ppm"]
        thresholds = [_quantized(known, 0) + step for step in (4, 7, 10, 13, 16)]
        outcome = [int(_quantized(row["actual_max_ppm"], precision) >= level) for level in thresholds]
        estimates = {}
        for method, peak in row["predict"].items():
            probabilities = [_binary(level, peak, residuals[method], precision, known)["shadow_probability"] for level in thresholds]
            forecasts = max(peak + mean(residuals[method]), known)
            estimates[method] = {"point_max_ppm": forecasts, "abs_error_ppm": abs(forecasts - row["actual_max_ppm"]),
                                 "brier": mean((p - y) ** 2 for p, y in zip(probabilities, outcome, strict=True)),
                                 "probabilities": probabilities}
        output.append({**row, "thresholds_relative_to_visible_max_ppm": [str(value) for value in thresholds],
                       "outcomes": outcome, "estimates": estimates})
    return output


def _block_interval(scored, baseline, method):
    blocks = defaultdict(list)
    for row in scored:
        blocks[(row["origin_year"] - TEST[0]) // 4].append(row["estimates"][method]["brier"] - row["estimates"][baseline]["brier"])
    means = [mean(rows) for rows in blocks.values()]
    simulated = np.random.default_rng(20261115).choice(means, size=(3000, len(means))).mean(axis=1)
    return {"method": method, "baseline": baseline, "calendar_blocks": len(blocks), "delta": mean(means),
            "four_year_block_interval_95": np.quantile(simulated, (.025, .975)).tolist()}


def evaluate(data, inputs):
    frozen, digest = research_model.freeze_inputs(data, inputs)
    history = inputs["history"]
    monthly = {_month_index(date.fromisoformat(row["month"])): float(row["nasa_ppm"]) for row in history}
    precision = inputs["forecast_precision"]
    groups = _samples(monthly)
    calibration = groups["calibration"]
    if len(calibration) < 10:
        raise ValueError("Insufficient independent earlier years for CO₂ calibration")
    residuals = {method: [row["actual_max_ppm"] - row["predict"][method] for row in calibration] for method in METHODS}
    scored = _score(groups["test"], residuals, precision)
    metrics = []
    for group, rows in (("Historical later origins", scored), ("Exact-station target months", [
            row for row in scored if not row["target_contains_temporary_substitute"]])):
        if rows:
            metrics.extend({"method": method, "label": label, "group": group, "origin_years": len(rows),
                            "calendar_blocks": len({(row["origin_year"] - TEST[0]) // 4 for row in rows}), "threshold_cases": len(rows) * 5,
                            "bucket_brier": mean(row["estimates"][method]["brier"] for row in rows),
                            "mae_ppm": mean(row["estimates"][method]["abs_error_ppm"] for row in rows)}
                           for method, label in METHODS.items())
    observed_at = inputs["cutoff"]
    latest = history[-1]
    observed = [(row["month"], Decimal(row["nasa_ppm"])) for row in history if row["month"] >= "2025-07-01"]
    peak_month, peak = max(observed, key=lambda row: row[1])
    origin = max(monthly)
    projected = _project(monthly, origin, _month_index(BEFORE) - origin - 1)
    future_max = {method: max(rows, key=lambda row: row["ppm"]) for method, rows in projected.items()}
    live = []
    for market in inputs["markets"]:
        strike = Decimal(market["verified_threshold_ppm"])
        forecast = {method: {**_binary(strike, projected_peak["ppm"], residuals[method], precision, float(peak)),
                             "unadjusted_peak": projected_peak,
                             "whole_ppm_sensitivity": _binary(strike, projected_peak["ppm"], residuals[method], 0, float(peak))}
                    for method, projected_peak in future_max.items()}
        live.append({"strike_ppm": str(strike), "market": market, "model": forecast,
                     "current_completed_months_reach_threshold": _quantized(peak, precision) >= strike})
    keep = min(live, key=lambda row: Decimal(row["strike_ppm"]))
    comparison = [row for row in history if row["noaa_ppm"] is not None]
    differences = [row for row in comparison if Decimal(row["nasa_ppm"]) != Decimal(row["noaa_ppm"])]
    record = {"version": VERSION, "title": "Mauna Loa monthly CO₂ and the 2030 threshold contracts",
              "hypothesis": "The month-by-month Mauna Loa series and an earlier trend fit are needed to model whether any completed month can cross the listed thresholds.",
              "target": "Any NASA-displayed complete Mauna Loa monthly average meeting the native threshold before January 2030",
              "venues": ["kalshi"], "date_window": {"calibration_origins": CALIBRATION, "later_origins": TEST,
                 "contract_observation_months": ["2025-07", "2029-12"], "latest_complete_nasa_month": latest["month"]},
              "coverage": {"nasa_months": len(history), "matching_raw_noaa_months": len(comparison) - len(differences),
                 "different_nasa_noaa_months": len(differences), "observed_contract_months": len(observed),
                 "open_native_thresholds": sum(row["market"]["status"] == "open" for row in live)},
              "metrics": metrics, "uncertainty": [_block_interval(scored, "linear", method) for method in ("recent", "curve")],
              "cases": [{"region": "Mauna Loa Observatory", "date": latest["month"], "at": observed_at, "venue": "kalshi",
                         "instrument_id": keep["market"]["instrument_id"], "title": "Mauna Loa · Native monthly CO₂ threshold",
                         "detail": (f"NASA and NOAA give {latest['nasa_ppm']} ppm for the latest completed month. "
                                    f"The highest NASA-displayed monthly value since issuance was {peak} ppm in {peak_month[:7]}. "
                                    "The threshold outcomes remain open."),
                         "headline_ppm": inputs["headline_ppm"], "nasa_chart_decimals": precision, "market_thresholds": live,
                         "latest_official_display": latest, "highest_completed_month": {"month": peak_month, "ppm": str(peak)}}],
              "limits": ["The current NASA historical chart and linked NOAA file are preserved as a fixed snapshot. Earlier exact chart publication clocks are unavailable.",
                         "Retrospective forecasts use only earlier displayed months at each origin. Thresholds are relative to that origin's known maximum, and no prior native payouts are available.",
                         "Neighboring origins overlap in calendar duration. Four-year blocks, rather than thresholds or months, determine the later uncertainty sample.",
                         "The sensitivity removes target windows that include the late 2022–mid 2023 Maunakea substitution. Those months precede the live market.",
                         "The contract specifies completed monthly data and the precision displayed by NASA. The chart tooltip gives hundredths while the headline rounds whole ppm.",
                         "These model estimates are exploratory and extreme historical support is thin. Catalog prices, when present, carry neither live depth nor a verified execution recheck."],
              "provenance": {"filing": FILING, "raw_sources": inputs["sources"], "frozen_input": str(frozen.relative_to(data)),
                 "frozen_input_sha256": digest, "scikit_learn": sklearn_version,
                 "source_sha256": research_model.run_sources(sys.modules[__name__])}}
    return research_model.write_record(data, record, {"calibration": calibration, "scored_origins": scored,
               "nasa_noaa_differences": differences, "projected_months": projected, "live_thresholds": live})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--input", type=Path)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    inputs = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else gather(args.data, refresh=args.refresh)
    print(evaluate(args.data, inputs))


if __name__ == "__main__":
    main()
