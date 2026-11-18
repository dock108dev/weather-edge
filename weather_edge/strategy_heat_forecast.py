"""Backtest as-issued weekly temperature-streak forecasts against actual payouts."""

import argparse
from bisect import bisect_left
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone
from email.utils import parsedate_to_datetime
import gzip
from hashlib import sha256
import json
from pathlib import Path
import statistics
import time as monotonic_clock
from zoneinfo import ZoneInfo

from sklearn.feature_extraction import DictVectorizer
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline

from . import strategy_twc
from .capture import FORECAST_CAP, ReadOnlyHTTP
from .expansion_nbm import _station_text
from .market import iso, parse_time
from .nbm import AWS_NBM, _columns, _station_fields, nbm_url
from .strategy_periods import longest_streak
from .strategy_runs import DEFAULT_DATA, digest, json_bytes, write_new


ENGINE = "weekly-streak-nbm-v1"
FIT_END = "2026-09-06"
CALIBRATION_END = "2026-09-13"
TEST_BEGIN = "2026-09-21"
RIDGE_ALPHA = 10
PRODUCT_DOC = "https://vlab.noaa.gov/web/mdl/nbm-textcard-v5.0"


def issue_for(first_day):
    """Use yesterday's 18Z issue before each station's first local midnight."""
    station_date = date.fromisoformat(first_day)
    return datetime.combine(station_date - timedelta(days=1), time(18), timezone.utc)


def parsed_card(body, station, product):
    text, _ = _station_text(body, station, product.upper())
    return station_guidance(text, station, product)


def station_guidance(text, station, product):
    issued, fields = _station_fields(text, station, product)
    leads = [int(lead) for lead in fields["FHR"].replace("|", " ").split()]
    if not leads or leads != sorted(set(leads)):
        raise ValueError("Invalid NOAA valid-time sequence")
    if product == "nbe":
        # The extended product delimits local day groups with a pipe rather
        # than using the short product's three-character column layout.
        tokens = fields["TMP"].replace("|", " ").split()
        if len(tokens) != len(leads):
            raise ValueError("Extended temperature field count differs from its valid-time count")
        values = [int(token) for token in tokens]
    else:
        values = _columns(fields, "TMP", len(leads))
    return {
        "issued_at": iso(issued), "raw_card": text.decode("ascii"), "card_sha256": sha256(text).hexdigest(),
        "run": product, "point_step_hours": 3 if product == "nbs" else 12,
        "points": [{"at": iso(issued + timedelta(hours=lead)), "temp_f": amount}
                   for lead, amount in zip(leads, values, strict=True) if amount not in (None, -99)],
    }


def read_cards(data, issue, stations, product):
    root = data / "research" / "evidence" / "heat-guidance" / f"{issue:%Y%m%dT%H}" / product
    found = {}
    for station in stations:
        path = root / f"{station}.json"
        if path.is_file():
            saved = json.loads(path.read_text())
            found[station] = {**saved, **station_guidance(saved["raw_card"].encode("ascii"), station, product)}
    missing = stations - set(found)
    issues = {}
    if missing:
        deadline = monotonic_clock.monotonic() + 25
        get = ReadOnlyHTTP(deadline)
        source = (nbm_url(issue.date(), issue.hour, product) if product == "nbs"
                  else f"{AWS_NBM}/blend.{issue:%Y%m%d}/{issue:%H}/text/blend_nbetx.t{issue:%H}z")
        try:
            reply = get(source, FORECAST_CAP)
            original_stamp = reply.headers.get("last-modified")
            if original_stamp is None:
                raise ValueError("NOAA archive object publication time missing")
            published = max(issue, parsedate_to_datetime(original_stamp))
            shared = {"source": source, "object_last_modified": original_stamp, "available_at": iso(published),
                      "received_at": iso(reply.at), "object_sha256": sha256(reply.body).hexdigest()}
            for station in sorted(missing):
                try:
                    card = parsed_card(reply.body, station, product)
                    if parse_time(card["issued_at"]) != issue:
                        raise ValueError("Unexpected source issue clock")
                except (KeyError, ValueError) as error:
                    issues[station] = str(error)
                    continue
                found[station] = {**card, **shared}
                write_new(root / f"{station}.json", json_bytes(found[station]) + b"\n")
        except (OSError, ValueError) as error:
            issues.update({station: str(error) for station in missing})
    return found, issues


def previous_forecast(card_points, at):
    times, temps = card_points
    right = bisect_left(times, at)
    if right < len(times) and times[right] == at:
        return temps[right]
    if right == 0 or right == len(times):
        return None
    left = right - 1
    span = times[right] - times[left]
    if span > timedelta(hours=12):
        return None
    return temps[left] + ((at - times[left]) / span) * (temps[right] - temps[left])


def hourly_day(cases, labels, nbs, nbe):
    first = cases[0]
    station = first["station"]
    zone = ZoneInfo(labels[station, first["weather_path"][0]["date"]]["timezone"])
    first_day = date.fromisoformat(first["terms"]["start"])
    decision = datetime.combine(first_day, time.min, zone).astimezone(timezone.utc)
    cards = [nbe, nbs]
    if any(not parse_time(card["issued_at"]) <= parse_time(card["available_at"]) <= decision for card in cards):
        return None, "Bulletin source publication later than first local day"
    points = {parse_time(point["at"]): point["temp_f"] for card in cards for point in card["points"]}
    times = sorted(points)
    track = times, [points[at] for at in times]
    rows = []
    path = first["weather_path"]
    for idx, actual in enumerate(path):
        day = date.fromisoformat(actual["date"])
        start = datetime.combine(day, time.min, zone).astimezone(timezone.utc)
        stop = datetime.combine(day + timedelta(days=1), time.min, zone).astimezone(timezone.utc)
        if (stop - start) != timedelta(hours=24) or not 18 <= (actual["hour_count"] or 0) <= 24 or actual["observed_f"] is None:
            return None, "Daily actual has below the market's 18 reports or local day has a clock transition"
        interpolated = [previous_forecast(track, start + timedelta(hours=hour)) for hour in range(24)]
        if any(value is None for value in interpolated):
            return None, "Forecast knots do not cover all 24 hours on each contract date"
        rows.append({"station": station, "date": str(day), "lead_day": idx, "raw_f": statistics.mean(interpolated),
                     "actual_f": actual["observed_f"], "actual_source": actual["source"],
                     "actual_hour_count": actual["hour_count"]})
    return {
        "station": station, "event_id": first["event_id"], "date": first["date"], "start": first["terms"]["start"],
        "forecast_knowledge_at": iso(decision), "body": rows, "contracts": cases,
        "guidance": [{key: card[key] for key in ("run", "source", "available_at", "received_at",
                                                  "issued_at", "card_sha256", "object_sha256", "point_step_hours")}
                     for card in cards],
    }, None


def fit_forcing(events):
    fit = [event for event in events if event["date"] <= FIT_END]
    calibration = [event for event in events if FIT_END < event["date"] <= CALIBRATION_END]
    test = [event for event in events if event["start"] >= TEST_BEGIN]
    if not fit or not calibration or not test:
        raise ValueError("No forecast-complete cases for one of the fixed calendar phases.")
    model = make_pipeline(DictVectorizer(), Ridge(alpha=RIDGE_ALPHA))
    rows = [row for event in fit for row in event["body"]]

    def features(row):
        return {"station": row["station"], "lead_day": row["lead_day"], "raw_f": row["raw_f"]}

    model.fit([features(row) for row in rows], [row["actual_f"] - row["raw_f"] for row in rows])

    def means(event, use_ridge):
        items = [row["raw_f"] for row in event["body"]]
        if not use_ridge:
            return items
        shifts = model.predict([features(row) for row in event["body"]])
        return [float(original + update) for original, update in zip(items, shifts, strict=True)]

    outputs = []
    for correct, method in ((False, "Raw NBM + earlier residual paths"), (True, "Ridge + earlier residual paths")):
        residuals = defaultdict(list)
        for event in calibration:
            points = means(event, correct)
            errors = [row["actual_f"] - value for row, value in zip(event["body"], points, strict=True)]
            residuals[len(errors)].append(errors)
        for event in test:
            anchors = means(event, correct)
            trajectories = residuals[len(anchors)]
            if not trajectories:
                continue
            for contract in event["contracts"]:
                if contract["payout_yes"] not in (0, 1):
                    continue
                threshold = contract["terms"]["temperature_threshold"] + 0.5
                needed = contract["terms"]["minimum_days"]
                paths = [longest_streak([value + error > threshold for value, error in zip(anchors, path, strict=True)])
                         for path in trajectories]
                probability = (sum(path >= needed for path in paths) + 0.5) / (len(paths) + 1)
                outputs.append({
                    "name": method, "station": event["station"], "date": event["date"], "venue": "kalshi",
                    "title": contract["title"], "detail": f"{method} assigned {probability:.1%}. YES paid {contract['payout_yes']:.0f}.",
                    "instrument_id": contract["instrument_id"], "at": event["forecast_knowledge_at"],
                    "probability": probability, "outcome": contract["payout_yes"],
                    "weather_days": [{**row, "model_daily_mean_f": expected}
                                     for row, expected in zip(event["body"], anchors, strict=True)],
                    "guidance": event["guidance"], "period": contract["terms"],
                })
    return outputs, {"fit": fit, "calibration": calibration, "later test": test}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", type=Path, default=DEFAULT_DATA)
    ap.add_argument("period_replay", type=Path, help="Frozen period-case JSON under research/evidence.")
    args = ap.parse_args()
    data = args.data
    raw = args.period_replay.read_bytes()
    periods = json.loads(raw)["cases"]
    selected = defaultdict(list)
    for case in periods:
        if case["terms"]["family"] == "weekly heat streak" and case["payout_yes"] in (0, 1):
            selected[case["event_id"]].append(case)
    grouped = defaultdict(set)
    for group in selected.values():
        case = group[0]
        grouped[issue_for(case["terms"]["start"])].add(case["station"])

    def pull(job):
        issue, sites = job
        first, gaps = read_cards(data, issue, sites, "nbs")
        second, extensions = read_cards(data, issue, sites, "nbe")
        return issue, first, second, {**gaps, **extensions}

    with ThreadPoolExecutor(max_workers=2) as pool:
        archive = {issue: (nbs, nbe, issues) for issue, nbs, nbe, issues in pool.map(pull, sorted(grouped.items()))}
    observed = {(r["station"], r["label_date"]): r for r in strategy_twc.reports(data)}
    gaps, events = Counter(), []
    for group in selected.values():
        first = group[0]
        station = first["station"]
        issue = issue_for(first["terms"]["start"])
        nbs, nbe, source_gaps = archive[issue]
        if station not in nbs or station not in nbe:
            gaps[source_gaps.get(station, "Missing NBS or NBE station guidance")] += 1
            continue
        event, reason = hourly_day(group, observed, nbs[station], nbe[station])
        if reason:
            gaps[reason] += 1
        else:
            events.append(event)
    outputs, windows = fit_forcing(events)
    props = {
        "engine": ENGINE, "period_case_sha256": sha256(raw).hexdigest(), "fit_end": FIT_END,
        "calibration_end": CALIBRATION_END, "test_begin": TEST_BEGIN, "ridge_alpha": RIDGE_ALPHA,
        "bulletin_documentation": PRODUCT_DOC,
    }
    identified = {"settings": props, "forecast_cases": events, "held_probabilities": outputs, "gaps": dict(gaps)}
    ident = "heat-forecast-" + digest(identified)[:20]
    snapshot = data / "research" / "evidence" / "heat-hindcast" / f"{ident}.json.gz"
    write_new(snapshot, gzip.compress(json_bytes(identified), mtime=0))
    scores = []
    for method in sorted({row["name"] for row in outputs}):
        matching = [row for row in outputs if row["name"] == method]
        scores.append({
            "method": method, "bucket_brier": statistics.mean((r["probability"] - r["outcome"]) ** 2 for r in matching),
            "contracts": len(matching), "actual_yes_payouts": sum(r["outcome"] == 1 for r in matching),
            "distinct_station_weeks": len({(r["station"], r["date"]) for r in matching}),
            "independent_period_end_dates": len({r["date"] for r in matching}),
        })
    cases = sorted(outputs, key=lambda row: (row["outcome"] != 1, -abs(row["probability"] - row["outcome"])))[:5]
    report = {
        "id": ident, "title": "Seven-day heat streaks from as-issued NOAA forecasts",
        "hypothesis": "A Ridge correction to NBM daily means improves probabilities of a week with a specified number of consecutive hot days.",
        "target": "Kalshi weekly hot streak",
        "venues": ["kalshi"], "created_at": iso(datetime.now(timezone.utc)),
        "date_window": {"from": min(c["start"] for c in events), "through": max(c["date"] for c in events)},
        "metrics": scores, "cases": cases,
        "limits": [
            "The earlier residuals are whole-week weather errors, which retains within-week dependence. Nearby stations and the same ending week are correlated.",
            "The NOAA cards were issued and posted before the first required date. Current provider records supply archived labels without archived original publication times.",
            "Late week knots are 12 hours apart. Linear interpolation supplies a model daily mean over a full local calendar day.",
            "Actual exchange payouts are test labels. Price and orderbook evidence are outside this probability replay.",
        ],
        "evidence": {"gaps": dict(gaps), "windows": {key: {"events": len(rows), "dates": len({r["date"] for r in rows})}
                                                   for key, rows in windows.items()}},
        "provenance": {**props, "frozen_inputs": str(snapshot.relative_to(data))},
    }
    path = data / "research" / "models" / f"{ident}.json"
    write_new(path, json_bytes(report) + b"\n")
    print(json.dumps({"path": str(path), **{key: report[key] for key in ("metrics", "evidence")}}, indent=2))


if __name__ == "__main__":
    main()
