"""Test first-receipt METAR pressure change as a feature for daily rain."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date, timedelta
import gzip
from hashlib import sha256
import json
import math
from pathlib import Path
import re
import statistics
import sys

from sklearn import __version__ as sklearn_version
from sklearn.feature_extraction import DictVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from . import archive, expansion_weather, research_gefs, research_model, research_model_rain, research_model_rain_update
from .nbm import climate_window, iso, utc


VERSION = "metar-pressure-daily-rain-v1"
STATIONS = ("KNYC", "KMDW", "KPHL", "KMIA", "KSFO", "KLAX")
WINDOWS = {
    "fit": (date(2026, 10, 1), date(2026, 10, 17)),
    "calibration": (date(2026, 10, 19), date(2026, 10, 26)),
    "test": (date(2026, 10, 29), date(2026, 11, 14)),
}
HOURS = (9, 15)
GUIDE = "https://www.weather.gov/media/tbw/Aviation/TAF_Decoder.pdf"
REMARKS = "https://www.weather.gov/media/wrh/mesowest/metar_decode_key.pdf"
ALTIMETER = re.compile(r"(?:^|\s)A(\d{4})(?=\s|=|$)")
TENDENCY = re.compile(r"(?:^|\s)5([0-8])(\d{3})(?=\s|=|$)")


def pressure_readings(observations, station, at):
    points = {}
    for row in sorted(observations, key=lambda reading: utc(reading["available_at"])):
        if row.get("timing_proxy"):
            continue
        observed, received = utc(row["at"]), utc(row["available_at"])
        if not observed <= received <= at or at - observed > timedelta(hours=5):
            continue
        raw = row.get("report") or row.get("raw") or row.get("raw_report") or ""
        parts = raw.split()
        if parts and parts[0] in ("METAR", "SPECI"):
            parts.pop(0)
        boundary, _, remarks = raw.partition(" RMK ")
        code = ALTIMETER.search(boundary)
        if not code or not parts or parts[0] != station:
            continue
        inches = int(code[1]) / 100
        if not 26 <= inches <= 33:
            continue
        current = points.get(observed)
        # A repeat of the same raw measurements keeps its first receipt.
        if current and current["report"].rstrip("= ") == raw.rstrip("= "):
            continue
        tender = TENDENCY.search(remarks)
        native = None
        if tender:
            kind, size = int(tender[1]), int(tender[2]) / 10
            native = {"shape": kind, "magnitude_hpa": size,
                      "signed_hpa": size if kind < 4 else 0.0 if kind == 4 else -size}
        points[observed] = {"observed_at": iso(observed), "available_at": iso(received),
                            "altimeter_inhg": inches, "report": raw, "native_tendency": native,
                            "source": row.get("receipt_source") or row.get("source_url") or row.get("source")}
    return points


def pressure_change(observations, station, at):
    points = pressure_readings(observations, station, at)
    if not points:
        return None
    latest = max(points)
    if at - latest > timedelta(minutes=90):
        return None
    target = latest - timedelta(hours=3)
    earlier = min((clock for clock in points if abs(clock - target) <= timedelta(minutes=50)),
                  key=lambda clock: (abs(clock - target), clock), default=None)
    if earlier is None:
        return None
    first, last = points[earlier], points[latest]
    hours = (latest - earlier).total_seconds() / 3600
    difference = (last["altimeter_inhg"] - first["altimeter_inhg"]) * 33.8638867
    return {"pressure_change_3h_hpa": difference * 3 / hours, "measured_hours": hours, "earlier": first, "latest": last}


def _supplement(data, event, station, day):
    path = data / "expansion" / "replay" / str(day) / f"{station}.json"
    if not path.is_file():
        return event
    extra = archive._read_event(path, day, station)
    if any(event.get(key) and utc(event[key]) != utc(extra[key]) for key in ("climate_starts_at", "climate_ends_at")):
        return event
    return {**event, "observations": [*event["observations"], *extra["observations"]],
            "official_reports": [*event.get("official_reports", []), *extra.get("official_reports", [])]}


def inputs(data):
    sets = {label: [] for label in WINDOWS}
    gaps = Counter()
    locations = {station: expansion_weather.STATIONS[station][3] for station in STATIONS}
    for split, (first, last) in WINDOWS.items():
        for day in archive.date_range(first, last):
            records = {event["station"]: event for event in archive.load_day(data, str(day))["events"]}
            for station, offset in locations.items():
                event = records.get(station)
                if event is None:
                    gaps[split, "no station-day archive"] += 1
                    continue
                event = _supplement(data, event, station, day)
                reports = [r for r in event.get("official_reports", []) if r.get("product") and r.get("sha256")]
                official = max(reports, key=lambda row: utc(row["available_at"]), default=None)
                native = official.get("precip_as_reported", official.get("precip_in")) if official else None
                liquid = 0.0 if native == "T" else expansion_weather.parse_number(native)
                if liquid is None:
                    gaps[split, "no complete original precipitation report"] += 1
                    continue
                base = research_model_rain._weather(data, day, station)
                if base is None:
                    gaps[split, "no complete prior NOAA P06/Q06"] += 1
                    continue
                start, _ = climate_window(day, offset)
                for hour in HOURS:
                    decision = start + timedelta(hours=hour)
                    observed = event["observations"]
                    if research_model_rain_update.gauge_witness(observed, station, start, decision):
                        gaps[split, "measurable rain already witnessed"] += 1
                        continue
                    change = pressure_change(observed, station, decision)
                    if change is None:
                        gaps[split, "missing timed altimeter pair"] += 1
                        continue
                    sets[split].append({**base, "at": iso(decision), "hour": hour, "truth": int(liquid >= .01),
                                        "pressure": change, "official_report": official, "source_amount": native})
    ordered = tuple(WINDOWS)
    for earlier, later in zip(ordered, ordered[1:]):
        if sets[later]:
            freeze = min(utc(row["at"]) for row in sets[later])
            qualified = [row for row in sets[earlier] if utc(row["official_report"]["available_at"]) < freeze]
            gaps[earlier, "label after next window"] += len(sets[earlier]) - len(qualified)
            sets[earlier] = qualified
    return {"version": VERSION, "sets": sets,
            "gaps": [{"split": split, "reason": why, "cases": count} for (split, why), count in sorted(gaps.items()) if count]}


def _features(row, extra):
    logit, amount = research_model_rain._features(row, qpf=True)
    result = {"station": row["station"], "hour": str(row["hour"]), "logit_p06": logit, "log_q06": amount}
    if extra:
        result["pressure_change_3h_hpa"] = row["pressure"]["pressure_change_3h_hpa"]
    return result


def _score(train, rows):
    models = {}
    for name, extra in (("calibrated-nbm", False), ("plus-pressure", True)):
        model = make_pipeline(DictVectorizer(sparse=False), StandardScaler(), LogisticRegression(C=1, random_state=20261115))
        model.fit([_features(row, extra) for row in train], [row["truth"] for row in train])
        models[name] = extra, model
    probabilities = {key: model.predict_proba([_features(row, extra) for row in rows])[:, 1]
                     for key, (extra, model) in models.items()}
    return [{**row, "probabilities": {"raw-nbm": row["forecast"]["p06_overlap_probability"],
                                     **{key: float(values[index]) for key, values in probabilities.items()}}}
            for index, row in enumerate(rows)]


def _metrics(rows, group):
    if not rows:
        return []
    return [{"group": group, "method": key, "decisions": len(rows),
             "station_days": len({(row["station"], row["date"]) for row in rows}),
             "calendar_dates": len({row["date"] for row in rows}), "yes_cases": sum(row["truth"] for row in rows),
             "brier": statistics.mean((row["probabilities"][key] - row["truth"])**2 for row in rows)}
            for key in rows[0]["probabilities"]]


def _case(row, rank):
    ps, reading = row["probabilities"], row["pressure"]
    return {"station": row["station"], "date": row["date"], "at": row["at"],
            "title": f"{row['station']} {row['date']} · pressure trend {rank}",
            "detail": f"Altimeter change {reading['pressure_change_3h_hpa']:+.1f} hPa in three hours. "
                      f"Earlier-calibrated NOAA {ps['calibrated-nbm']:.1%}; with pressure {ps['plus-pressure']:.1%}. "
                      f"Official daily precipitation {row['source_amount']} inches.",
            "forecast": {"periods": row["forecast"], "probabilities": ps}, "official_report": row["official_report"],
            "evidence": reading}


def run(data, frozen=None, *, supersedes=None):
    if frozen is None:
        evidence = inputs(data)
        frozen, digest = research_model.freeze_inputs(data, evidence)
    else:
        body = gzip.decompress(frozen.read_bytes())
        evidence, digest = json.loads(body), sha256(body).hexdigest()
    sets = evidence["sets"]
    train, calibration, test = (sets[key] for key in WINDOWS)
    validation, evaluated = _score(train, calibration), _score(train, test)
    metrics = [*_metrics(validation, "Earlier check"), *_metrics(evaluated, "Held-out matched pressure reports")]
    for hour in HOURS:
        metrics.extend(_metrics([row for row in evaluated if row["hour"] == hour], f"Held out at {hour}:00 local standard"))
    comparisons = defaultdict(list)
    def improvement(row):
        y, p = row["truth"], row["probabilities"]
        return (p["plus-pressure"] - y)**2 - (p["calibrated-nbm"] - y)**2
    for row in evaluated:
        comparisons[row["date"]].append(improvement(row))
    ranked = sorted(evaluated, key=improvement)
    native = [row["pressure"] for row in evaluated if row["pressure"]["latest"]["native_tendency"]]
    native_no_near_zero = [row for row in native if abs(row["pressure_change_3h_hpa"]) >= .7
                          and abs(row["latest"]["native_tendency"]["signed_hpa"]) >= .7]
    disagree = sum(math.copysign(1, row["pressure_change_3h_hpa"]) !=
                   math.copysign(1, row["latest"]["native_tendency"]["signed_hpa"]) for row in native_no_near_zero)
    return research_model.write_record(data, {
        "version": VERSION, "title": "Does a falling METAR altimeter improve daily rain probabilities?",
        **({"supersedes": supersedes} if supersedes else {}),
        "hypothesis": "Three-hour pressure change between exact-station METAR receipts may add information to the as-issued NOAA daily rain proxy before local measurable precipitation has been observed.",
        "target": "Official station precipitation of at least 0.01 inches", "venues": [],
        "date_window": {key: list(map(str, dates)) for key, dates in WINDOWS.items()}, "metrics": metrics,
        "cases": [_case(ranked[0], "helped"), _case(ranked[-1], "hurt")],
        "coverage": {"frozen_by_stage": {key: len(rows) for key, rows in sets.items()}, "gaps": evidence["gaps"],
                     "later_native_tendency_overlap": len(native), "both_magnitudes_at_least_point_seven": len(native_no_near_zero),
                     "sign_disagreements_on_overlap": disagree},
        "uncertainty": {"pressure_minus_baseline_brier": statistics.mean(improvement(row) for row in evaluated),
                        "calendar_block_interval_95": research_gefs._mean_interval(comparisons)},
        "limits": [
            "Uses exact station METAR altimeter settings with their retained first receipt clocks, a latest reading within 90 minutes and an earlier reading centered three hours before it. The change between reported measurements is rescaled to three hours.",
            "All cases use the same prior NOAA P06/Q06 and no observed gauge witness of measurable rain at decision. An absent gauge witness leaves the preceding accumulation uncertain.",
            "The METAR A setting is different from sea-level pressure. The native 5appp station-pressure tendency, where present, is a separate cross-check.",
            "Only earlier dates train both logistic models. The sole added predictor is the same-station pressure change. The outcome combines official liquid equivalent, including frozen forms, across the climate day.",
        ],
        "provenance": {"frozen_input": str(frozen.relative_to(data)), "input_sha256": digest, "sklearn": sklearn_version,
                       "NWS_METAR": GUIDE, "NWS_METAR_remarks": REMARKS,
                       "source_sha256": research_model.run_sources(sys.modules[__name__], research_model_rain, research_model_rain_update),
                       "reproduce": (f"python -m weather_edge.research_pressure_rain --inputs {frozen}"
                                     + (f" --supersedes {supersedes}" if supersedes else ""))},
    }, {"earlier_calibration": validation, "later_test": evaluated})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--inputs", type=Path)
    parser.add_argument("--supersedes")
    args = parser.parse_args()
    print(run(args.data, args.inputs, supersedes=args.supersedes))


if __name__ == "__main__":
    main()
