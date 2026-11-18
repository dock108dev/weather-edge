"""Explore the active multi-day low with completed reports and a remaining-day forecast."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
import json
from pathlib import Path
from statistics import NormalDist
import time as monotonic_clock
from zoneinfo import ZoneInfo

from . import strategy_twc
from .capture import FORECAST_CAP, ReadOnlyHTTP, _nws_obs
from .market import iso, parse_time
from .nbm import nbm_url, parse_nbh
from .strategy_periods import contracts, decode, thermal_result
from .strategy_runs import DEFAULT_DATA, digest, json_bytes, write_new


ENGINE = "period-minimum-active-guidance-v1"
QUANTILES = 101
GUIDANCE_PRODUCTS = "https://vlab.noaa.gov/web/mdl/nbm-textcard-v5.0"


def read_current_forecasts(stations, *, now):
    issues = []
    for hours_ago in range(1, 5):
        cycle = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=hours_ago)
        source = nbm_url(cycle.date(), cycle.hour, "nbh")
        try:
            reply = ReadOnlyHTTP(monotonic_clock.monotonic() + 20)(source, FORECAST_CAP)
            modified = parsedate_to_datetime(reply.headers["last-modified"])
            if not cycle <= modified <= reply.at:
                raise ValueError("Unexpected NOAA object publication clock")
        except (KeyError, OSError, ValueError) as error:
            issues.append({"source": source, "reason": str(error)})
            continue
        cards = {}
        for place in stations:
            try:
                card = parse_nbh(reply.body, place)
                if parse_time(card["issued_at"]) != cycle:
                    raise ValueError("Forecast station issue mismatch")
                cards[place] = {**card, "source": source, "source_published_at": iso(modified),
                                "available_at": iso(reply.at), "object_sha256": sha256(reply.body).hexdigest(),
                                "model": "NOAA NBM NBH", "time_step_hours": 1}
            except (KeyError, OSError, ValueError) as error:
                issues.append({"station": place, "source": source, "reason": str(error)})
        if cards:
            return cards, issues
    return {}, issues


def proxy_path(current_min, prior_min, guidance):
    """Translate all remaining hours by one quantile, keeping correlated temperature errors."""
    points = [row for row in guidance if row.get("sd_f") is not None and row["sd_f"] > 0]
    if len(points) != len(guidance) or not points:
        return None
    warm_or_cool = NormalDist()
    path = []
    for index in range(QUANTILES):
        z = warm_or_cool.inv_cdf((index + 0.5) / QUANTILES)
        highs_and_lows = [prior_min, current_min, min(row["temp_f"] + z * row["sd_f"] for row in guidance)]
        path.append(min(highs_and_lows))
    return path


def current_observations(station):
    try:
        getter = ReadOnlyHTTP(monotonic_clock.monotonic() + 15)
        return station, _nws_obs(getter, station, limit=72), None
    except (KeyError, OSError, ValueError) as error:
        return station, [], {"station": station, "reason": str(error)}


def check_group(terms, reports, forecast, readings, as_of):
    days, official, _ = thermal_result(terms, reports)
    tz = ZoneInfo(terms["tz"])
    local_day = as_of.astimezone(tz).date()
    if local_day != terms["end"]:
        return None, "The active forecast case is built for the final local date"
    if not forecast or parse_time(forecast["available_at"]) > as_of:
        return None, "No receipt-confirmed NOAA NBH station forecast"
    start = datetime.combine(local_day, time.min, tz).astimezone(timezone.utc)
    end = datetime.combine(local_day + timedelta(days=1), time.min, tz).astimezone(timezone.utc)
    observed = [row for row in readings if start <= parse_time(row["at"]) <= parse_time(row["available_at"]) <= as_of]
    if not observed:
        return None, "No exact-station current-date reading was received"
    prior = [row["observed_f"] for row in days if row["date"] != str(local_day)]
    if not prior or any(value is None for value in prior):
        return None, "One or more earlier daily minimum reports are unavailable"
    first_valid = as_of.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    future_count = int((end - first_valid).total_seconds() // 3600)
    by_hour = {parse_time(row["valid_at"]): row for row in forecast["hourly"]}
    future = [by_hour.get(first_valid + timedelta(hours=i)) for i in range(future_count)]
    if not future or any(row is None for row in future):
        return None, "NBH remaining hours do not cover the station's whole local date"
    observed_min = min(row["temp_f"] for row in observed)
    prior_min = min(prior)
    forecast_min = min(row["temp_f"] for row in future)
    examples = proxy_path(observed_min, prior_min, future)
    nominal = min(prior_min, observed_min, forecast_min)
    threshold = terms["temperature_threshold"]
    return {
        "daily_report_path": days, "prior_official_min_f": prior_min, "within_current_day_observed_min_f": observed_min,
        "remaining_day_nbh_min_f": forecast_min, "nominal_period_min_f": nominal, "already_verified_period_min": official is not None,
        "observation_count": len(observed), "forecast_hours": len(future),
        "prior_threshold_already_observed": prior_min < threshold,
        "projected_yes_probability": sum(value < threshold - 0.5 for value in examples) / len(examples) if examples else None,
        "forecast_guidance": {key: value for key, value in forecast.items() if key != "hourly"},
        "forecast_points": future, "same_date_observations": observed,
    }, None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    args = parser.parse_args()
    data = args.data
    rows = [(market, terms) for market in contracts(data) if market["status"] == "open"
            and (terms := decode(market)) and terms["family"] == "period minimum"]
    if not rows:
        parser.error("No supported active ranged-minimum weather contracts.")
    stations = sorted({terms["station"] for _, terms in rows})
    start = datetime.now(timezone.utc)
    forecasts, issues = read_current_forecasts(stations, now=start)
    with ThreadPoolExecutor(max_workers=3) as pool:
        observed = {}
        for station, reading, issue in pool.map(current_observations, stations):
            observed[station] = reading
            if issue:
                issues.append(issue)
    as_of = datetime.now(timezone.utc)
    reports = {(r["station"], r["label_date"]): r for r in strategy_twc.reports(data, as_of=as_of)}
    predictions = []
    for market, terms in rows:
        station = terms["station"]
        data_from_weather, error = check_group(terms, reports, forecasts.get(station), observed[station], as_of)
        if error:
            issues.append({"instrument": market["instrument_id"], "station": station, "reason": error})
            continue
        signal = data_from_weather["projected_yes_probability"]
        predictions.append({
            "station": station, "date": str(terms["end"]), "venue": "kalshi", "at": iso(as_of),
            "instrument_id": market["instrument_id"], "event_id": market["event_id"],
            "title": f"{station} multi-day minimum under {terms['temperature_threshold']}°F",
            "detail": f"Earlier complete reports reached {data_from_weather['prior_official_min_f']:.0f}°F. "
                      f"Current station logs and remaining NBH show a conditional period minimum of {data_from_weather['nominal_period_min_f']:.1f}°F.",
            "terms": {**terms, "start": str(terms["start"]), "end": str(terms["end"])},
            "rules": market["rules_primary"], "report_policy": market["rules_secondary"],
            "latest_labeled_price": {key: market[key] for key in ("yes_bid", "yes_ask", "last_price", "last_received_at")},
            **data_from_weather, "probability_definition": "Shared normal forecast error with published point spread" if signal is not None else None,
        })
    source = {"engine": ENGINE, "as_of": iso(as_of), "quote_ready_cases": predictions, "source_issues": issues}
    ident = "period-low-live-" + digest(source)[:20]
    snapshot = data / "research" / "evidence" / "period-cases" / f"{ident}.json"
    write_new(snapshot, json_bytes(source) + b"\n")
    favored = sorted(predictions, key=lambda r: (r["station"] != "KEWR", abs((r["projected_yes_probability"] or 0) - 0.5)))
    result = {
        "id": ident, "title": "Live forecast for listed multi-day lows",
        "hypothesis": "Completed official dates, current exact station readings and remaining hourly guidance produce a conditional minimum for the listed period.",
        "target": "Kalshi ranged minimum temperature",
        "venues": ["kalshi"], "created_at": iso(as_of),
        "date_window": {"from": str(min(terms["start"] for _, terms in rows)),
                        "through": str(max(terms["end"] for _, terms in rows))},
        "metrics": [{"listed_instruments": len(rows), "forecasts": len(predictions),
                     "station_periods": len({r["station"] for r in predictions}),
                     "settled_payouts": 0, "paper_fills": 0}],
        "cases": favored[:10],
        "limits": [
            "The source's complete report for the final required day remains pending.",
            "Current station measurements and interpolated forecast spread create a provisional weather scenario.",
            "The probability calculation is an uncalibrated sensitivity, with one correlated temperature deviation across remaining hours.",
            "Shown display prices and source timestamps are market metadata snapshots.",
        ],
        "evidence": {"issues": issues},
        "provenance": {"engine": ENGINE, "frozen_inputs": str(snapshot.relative_to(data)), "guidance_docs": GUIDANCE_PRODUCTS},
    }
    path = data / "research" / "models" / f"{ident}.json"
    write_new(path, json_bytes(result) + b"\n")
    print(json.dumps({"path": str(path), "metrics": result["metrics"], "issues": issues,
                      "cases": [{k: r[k] for k in ("station", "instrument_id", "nominal_period_min_f", "projected_yes_probability")}
                                for r in favored[:5]]}, indent=2))


if __name__ == "__main__":
    main()
