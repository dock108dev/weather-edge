"""Validate the native seven-day station outlook and expose weather-only forecasts."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
import gzip
import json
from pathlib import Path
from statistics import mean
import sys

import numpy as np

from . import expansion_nbe, expansion_nbm, expansion_replay, expansion_weather, research_model, venue_archive
from . import research_model_venue
from .nbm import climate_window, iso, utc

VERSION = "nbm-seven-day-station-temperature-v1"
REFERENCE_STATIONS = ("KNYC", "KMDW", "KORD", "KDEN")
OUTLOOK_STATIONS = (*REFERENCE_STATIONS, "KPDX", "KBUF")
ORIGINS = tuple(date(2026, 10, 4) + timedelta(days=7 * i) for i in range(6))
CURRENT = date(2026, 11, 15)
LEADS = ((1, 2), (3, 4), (5, 7))
TARGETS = {"minimum": ("low_f", "low_normal_f"), "maximum": ("high_f", "high_normal_f")}
LABELS = {"nbe": "Originally published extended NBM", "normal": "Same station and date climate normal", "nbs": "Same-issue shorter NBM"}


def _case_row(data, station, card, day, ground, source):
    label = ground.get(str(day))
    if label is None:
        return None
    raw_path = data / "expansion" / "cli" / station / f"{label['product']}.txt"
    if not raw_path.is_file():
        return None
    raw = expansion_weather.raw_report(data, station, day, label["product"])
    if not raw or any(raw[actual] is None or label[normal] is None for actual, normal in TARGETS.values()):
        return None
    issue = utc(card["issued_at"])
    decision = issue + timedelta(hours=3)
    short = expansion_nbm.load_card(data, station, issue)
    short_weather = {}
    if short and issue <= utc(short["available_at"]) <= decision:
        offset = expansion_weather.STATIONS[station][3]
        start, end = climate_window(day, offset)
        high = expansion_replay._high_forecast(short, start, end)
        low = research_model._forecast(short, day, offset)
        if high:
            short_weather["maximum"] = high
        if low:
            short_weather["minimum"] = low
    return {"station": station, "date": str(day), "at": iso(decision), "lead_day": source["lead_day"],
            "issue": {key: card[key] for key in ("source", "issued_at", "available_at", "received_at", "card_sha256", "printed_model_version")},
            "official_report": raw, "targets": {name: {"actual": raw[actual], "normal": label[normal], "nbe": source[name]["temp_f"],
                                                        "native_stddev_f": source[name]["sd_f"], "nbe_window": source[name],
                                                        "nbs": short_weather[name].get("mean_f", short_weather[name].get("high_f")) if name in short_weather else None,
                                                        "short_evidence": short_weather.get(name)}
                                      for name, (actual, normal) in TARGETS.items()}}


def _native_example(data, station, target, cutoff):
    day, forecast_low = target["target_date"], target["minimum"]["temp_f"]
    listed = venue_archive.query(data, family="daily-low", place=station, day=day, limit=30)["rows"]
    eligible = []
    for row in listed:
        bounds = research_model_venue.integer_bucket(row)
        if (not bounds or not row["opened_at"] or not row["closed_at"]
                or not utc(row["opened_at"]) <= cutoff < utc(row["closed_at"])):
            continue
        lower, upper = bounds
        if (lower is None or lower <= forecast_low) and (upper is None or forecast_low <= upper):
            eligible.append(row)
    return min(eligible, key=lambda row: (row["venue"] != venue_archive.POLY, row["instrument_id"]), default=None)


def gather(data, at):
    evidence, missing = [], Counter()
    for station in REFERENCE_STATIONS:
        daily = expansion_weather.daily_labels(data, station, ORIGINS[0] + timedelta(days=1), CURRENT - timedelta(days=1))
        for origin in ORIGINS:
            issue = datetime.combine(origin, time(12), timezone.utc)
            card = expansion_nbe.load_card(data, issue, station=station)
            if not card or not issue <= utc(card["available_at"]) <= issue + timedelta(hours=3):
                missing["no complete extended source at the fixed publication cutoff"] += 1
                continue
            for raw in expansion_nbe.daily_extrema(card):
                day = date.fromisoformat(raw["target_date"])
                row = _case_row(data, station, card, day, daily, raw)
                if row:
                    evidence.append(row)
                else:
                    missing["no raw complete observed extrema and matching daily normals"] += 1
    current = {station: view for station in OUTLOOK_STATIONS
               if (view := expansion_nbe.station_outlook(data, station, at)) and utc(view["issued_at"]).date() == CURRENT}
    native = {station: row for station, view in current.items()
              if (row := _native_example(data, station, view["daily_extrema"][0], at))}
    return {"version": VERSION, "at": iso(at), "samples": evidence, "current": current, "native_examples": native, "missing": dict(missing)}


def _metrics(rows, frame, kinds, *, fair_short=False):
    metrics = []
    for target in TARGETS:
        comparable = [row for row in rows if not fair_short or row["targets"][target]["nbs"] is not None]
        if not comparable:
            continue
        for model in kinds:
            fields = [row["targets"][target] for row in comparable]
            errors = [item[model] - item["actual"] for item in fields]
            stats = {"group": frame, "target": target, "method": model, "label": LABELS[model],
                     **research_model._independent_counts(comparable), "issue_blocks": len({row["issue"]["issued_at"] for row in comparable}),
                     "mae_f": mean(abs(value) for value in errors), "bias_f": mean(errors), "rmse_f": mean(value ** 2 for value in errors) ** .5}
            if model == "nbe":
                assessed = [(error, item["native_stddev_f"]) for error, item in zip(errors, fields, strict=True) if item["native_stddev_f"] is not None]
                if assessed:
                    stats["raw_nominal_80_range_coverage"] = mean(abs(error) <= width * 1.28155 for error, width in assessed)
                    stats["nominal_80_range_cases"] = len(assessed)
            metrics.append(stats)
    return metrics


def _compare(rows, frame, target):
    issues = defaultdict(list)
    for row in rows:
        item = row["targets"][target]
        issues[row["issue"]["issued_at"]].append(abs(item["nbe"] - item["actual"]) - abs(item["normal"] - item["actual"]))
    values = [mean(errors) for errors in issues.values()]
    draw = np.random.default_rng(20261115).choice(values, size=(3000, len(values))).mean(axis=1)
    return {"group": frame, "target": target, "baseline": "normal", "method": "nbe", "issue_blocks": len(issues),
            "delta_mae_f": mean(values), "weekly_issue_block_interval_95": np.quantile(draw, (.025, .975)).tolist()}


def globe_date(station, at):
    """Locate the decision in the station's fixed climate-day timeline."""
    return (utc(at) + timedelta(hours=expansion_weather.STATIONS[station][3])).date().isoformat()


def _live_case(station, view, market=None):
    first, final = view["daily_extrema"][0], view["daily_extrema"][-1]
    return {"station": station, "date": first["target_date"], "at": view["knowledge_at"], "mode": "live",
            "globe_date": globe_date(station, view["knowledge_at"]),
            "venue": market["venue"] if market else None, "instrument_id": market["instrument_id"] if market else None,
            "title": f"{station} · A real seven-day weather outlook", "detail": (
                f"The locally received bulletin forecast {first['minimum']['temp_f']}°F low and {first['maximum']['temp_f']}°F high "
                f"for {first['target_date']}, with complete forecast days through {final['target_date']}."),
            "weather_outlook": view, "market": market}


def _settled_case(row):
    label = row["targets"]
    return {"station": row["station"], "date": row["date"], "at": row["at"],
            "globe_date": globe_date(row["station"], row["at"]),
            "title": f"{row['station']} · A long-lead forecast and complete station report",
            "detail": (f"At lead day {row['lead_day']}, NBE forecast {label['minimum']['nbe']}°F and {label['maximum']['nbe']}°F. "
                       f"The complete report had {label['minimum']['actual']}°F and {label['maximum']['actual']}°F."),
            "original_forecast_and_report": row}


def evaluate(data, inputs):
    if inputs["version"] != VERSION:
        raise ValueError("Unexpected extended station forecast inputs")
    if not inputs["samples"]:
        raise ValueError("No originally timed extended forecast joined to raw station weather")
    path, digest = research_model.freeze_inputs(data, inputs)
    rows = inputs["samples"]
    metrics, bounds = [], []
    for first, last in LEADS:
        matched = [row for row in rows if first <= row["lead_day"] <= last]
        if matched:
            label = f"Lead days {first}–{last}"
            metrics.extend(_metrics(matched, label, ("nbe", "normal")))
            bounds.extend(_compare(matched, label, target) for target in TARGETS)
    metrics.extend(_metrics(rows, "Identical NBS and NBE publication clock and label", ("nbe", "nbs", "normal"), fair_short=True))
    selected = max((row for row in rows if row["lead_day"] >= 5),
                   key=lambda row: max(abs(item["nbe"] - item["actual"]) for item in row["targets"].values()), default=None)
    cases = [_live_case(station, inputs["current"][station], inputs["native_examples"].get(station))
             for station in ("KPDX", "KBUF", "KNYC", "KMDW") if station in inputs["current"]]
    if selected:
        cases.append(_settled_case(selected))
    record = {"version": VERSION, "title": "A seven-day view for exact weather stations",
              "hypothesis": "Original extended NOAA guidance improves on the same-date station normal as forecasts extend beyond active market coverage.",
              "target": "Exact station NBE temperature extrema and complete local climate reports, with a separate current forecast-only attachment",
              "venues": sorted({case["venue"] for case in cases if case.get("venue")}),
              "date_window": {"retained_historical_issues": [str(ORIGINS[0]), str(ORIGINS[-1])], "current_forecast_saved_at": inputs["at"]},
              "coverage": {**research_model._independent_counts(rows), "original_weekly_issues": len({row["issue"]["issued_at"] for row in rows}),
                           "current_stations": len(inputs["current"]), "current_forecast_days": sum(len(row["daily_extrema"]) for row in inputs["current"].values())},
              "metrics": metrics, "uncertainty": bounds, "cases": cases,
              "limits": ["Historical as-of clocks use originally published NOAA station text. Today's separate view requires each model file's actual retained local receipt.",
                         "The native 18-hour extrema windows overlap the fixed local climate day. A matching valid date is a weather guidance comparison. Reported climate and contract results remain distinct.",
                         "The raw 80% interval uses the native standard deviation and a Gaussian scale conversion. Its observed coverage is measured independently by lead.",
                         "Native markets open for only part of the seven-day forecast horizon. Some exact forecast stations have no listing in the retained catalogs."],
              "missing": inputs["missing"], "provenance": {"frozen_input": str(path.relative_to(data)), "frozen_input_sha256": digest,
                 "nbm_description": expansion_nbe.DOCUMENTATION, "source_sha256": research_model.run_sources(
                     sys.modules[__name__], expansion_nbe, expansion_nbm, expansion_weather, research_model_venue)}}
    return research_model.write_record(data, record, {"daily_validation": rows, "current_outlooks": inputs["current"]})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--input", type=Path)
    parser.add_argument("--as-of")
    args = parser.parse_args()
    if args.input and args.as_of:
        parser.error("A frozen input carries its original forecast cutoff")
    at = utc(args.as_of) if args.as_of else datetime.now(timezone.utc)
    inputs = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else gather(args.data, at)
    print(evaluate(args.data, inputs))


if __name__ == "__main__":
    main()
