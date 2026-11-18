"""Check whether exact six-hour METAR maxima improve the remaining-day high."""

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
import gzip
import json
import math
from pathlib import Path
import re
import statistics
from uuid import uuid4

from . import collector, expansion_weather
from .expansion_metar import REPORT_START, _tenths, _witness, integer_asos_interval, metar_low_evidence
from .market import iso, parse_time
from .model import MIN_BAND_DAYS, MIN_TAIL_SUPPORT_DAYS, _benchmark_buckets, _limits, _outcome, _probability
from .nbm import climate_window
from .strategy_high_remaining import HOURS, STATIONS, day_context
from .strategy_runs import DEFAULT_DATA, POLYMARKET, digest, json_bytes, write_new


ENGINE = "remaining-high-six-hour-observation-v1"
SIX_HOUR_HIGH = re.compile(r"(?<!\S)1([01])(\d{3})(?=\s|$)")
METHODS = "Precision-aware point maximum", "Point plus six-hour maxima"
GUARD = "Precision guard with climate-report reserve"
RUNTIME_ENGINE = "metar-guarded-high-shadow-v1"
RUNTIME_FAMILY = "daily-high-precision"


def metar_high_evidence(row, station, climate_start, decision, *, include_six_hour=True):
    point = metar_low_evidence(row, station, climate_start, decision, include_six_hour=False)
    if not include_six_hour:
        return point
    raw = (row.get("raw_report") or row.get("report") or row.get("raw") or "").strip().rstrip("=")
    identifier = REPORT_START.match(raw)
    if not identifier or identifier[1] != station:
        return point
    stamp, available = parse_time(row["at"]), parse_time(row["available_at"])
    if not climate_start <= stamp <= available <= decision or stamp.strftime("%d%H%M") != identifier[2]:
        return point
    _, _, remarks = raw.partition(" RMK ")
    maximum = SIX_HOUR_HIGH.search(remarks)
    if maximum is None:
        return point
    nominal = (stamp + timedelta(minutes=30)).replace(minute=0, second=0, microsecond=0)
    period_start = min(stamp, nominal) - timedelta(hours=6)
    period_end = max(stamp, nominal)
    degrees = _tenths(maximum)
    interval = integer_asos_interval(degrees, 0.1)
    if (nominal.hour % 6 == 0 and abs(stamp - nominal) <= timedelta(minutes=15)
            and climate_start <= period_start <= period_end <= decision and interval):
        return [*point, _witness(interval, stamp, available, period_start, period_end,
                                  "six-hour high", raw, degrees, 0.1, maximum[0])]
    return point


def make_rows(source, obs):
    result = {}
    evidence = {}
    for cohort, rows in source.items():
        result[cohort] = []
        for row in rows:
            station, day = row["station"], row["date"]
            start, _ = climate_window(day, STATIONS[station])
            decision = parse_time(row["at"])
            key = station, day
            support = [w for observation in obs[key] for w in metar_high_evidence(observation, station, start, decision)]
            points = [w for w in support if w["kind"] == "point"]
            if not points:
                continue
            measured_point = max(points, key=lambda item: item["lower_f"])
            measured_all = max(support, key=lambda item: item["lower_f"])
            physical_point = max(measured_point["lower_f"], row["expected_remaining_max_f"])
            physical_all = max(measured_all["lower_f"], row["expected_remaining_max_f"])
            result[cohort].append({
                **row, "physical_point_f": physical_point, "physical_with_six_f": physical_all,
                "observed_point_bound": measured_point, "all_observations_bound": measured_all,
                "six_hour_witnesses": sum(w["kind"] == "six-hour high" for w in support),
            })
            if measured_all["lower_f"] > row["actual_f"]:
                evidence[key] = {"actual_f": row["actual_f"], "source": row["label"]["source"], "witness": measured_all}
    return result, {" / ".join(key): row for key, row in evidence.items()}


def disagreement_reserve(calibration):
    days = {row["date"] for row in calibration}
    contradicted = {row["date"] for row in calibration if row["all_observations_bound"]["lower_f"] > row["actual_f"]}
    count = len(days)
    if count == 0:
        raise ValueError("No calibration dates to assess the observation guard")
    p = len(contradicted) / count
    z = 1.2815515655446004
    width = z * math.sqrt(p * (1 - p) / count + z**2 / (4 * count**2))
    endpoint = (p + z**2 / (2 * count) + width) / (1 + z**2 / count)
    return {"upper_one_sided_wilson": endpoint, "calibration_dates": count, "dates_with_conflict": sorted(contradicted)}


def guarded_probability(bounds, centers, scale, lower_f, reserve):
    floor = lower_f - 0.5
    lo, hi = bounds
    base = _probability(bounds, centers, scale)
    available = _probability((floor, math.inf), centers, scale)
    if available <= 1e-12:
        return base
    guarded = _probability((max(lo, floor), hi), centers, scale) / available if hi > floor else 0
    return reserve * base + (1 - reserve) * guarded


def evaluate(groups, exact=None):
    columns = dict(zip(METHODS, ("physical_point_f", "physical_with_six_f"), strict=True))
    historic = defaultdict(list)
    for row in groups["calibration"]:
        for method, field in columns.items():
            historic[method, row["station"], row["local_hour"]].append(row["actual_f"] - row[field])
    tested = []
    reserve = disagreement_reserve(groups["calibration"])
    for row in groups["test"]:
        if not all(historic[method, row["station"], row["local_hour"]] for method in METHODS):
            continue
        buckets = _benchmark_buckets(round(row["raw_f"])) if exact is None else exact.get((row["station"], row["date"]))
        if not buckets:
            continue
        outcome = [_outcome(bucket, row["actual_f"]) for bucket in buckets]
        losses = {}
        for method, field in columns.items():
            centers = [row[field] + error for error in historic[method, row["station"], row["local_hour"]]]
            p = [_probability(_limits(bucket), centers, 0.5) for bucket in buckets]
            losses[method] = {"brier": statistics.mean((value - label) ** 2 for value, label in zip(p, outcome, strict=True)),
                              "abs_error_f": abs(row[field] - row["actual_f"])}
        centers = [row["physical_with_six_f"] + error for error in historic[METHODS[1], row["station"], row["local_hour"]]]
        lower = row["all_observations_bound"]["lower_f"]
        precision = [guarded_probability(_limits(bucket), centers, 0.5, lower, reserve["upper_one_sided_wilson"])
                     for bucket in buckets]
        losses[GUARD] = {"brier": statistics.mean((value - label) ** 2 for value, label in zip(precision, outcome, strict=True)),
                         "abs_error_f": abs(row["physical_with_six_f"] - row["actual_f"])}
        tested.append({**row, "losses": losses, "buckets": buckets})
    return tested, reserve


def metrics(rows, label):
    if not rows:
        return []
    return [
        {"window": label, "method": name, "station_days": len({(r["station"], r["date"]) for r in rows}),
         "dates": len({r["date"] for r in rows}), "decision_rows": len(rows),
         "score_brier": statistics.mean(r["losses"][name]["brier"] for r in rows),
         "mae_f": statistics.mean(r["losses"][name]["abs_error_f"] for r in rows)}
        for name in (*METHODS, GUARD)
    ]


def read_covering_observations(data, station, first, last):
    location = data / "expansion" / "asos"
    selections = []
    for file in location.glob(f"{station}-*.csv"):
        match = re.fullmatch(rf"{station}-(\d{{4}}-\d{{2}}-\d{{2}})-(\d{{4}}-\d{{2}}-\d{{2}})\.csv", file.name)
        if match:
            begins, ends = map(date.fromisoformat, match.groups())
            if begins <= first and ends >= last:
                selections.append((ends - begins, begins, ends, file))
    if selections:
        _, first, last, _ = min(selections)
    source = expansion_weather.observation_file(data, station, first, last)
    if not source.is_file() and collector.paused(data):
        raise FileNotFoundError("Raw ASOS coverage is unavailable locally and collection is stopped")
    rows = expansion_weather.observed_data(data, station, first, last)
    return rows, str(source.relative_to(data))


def freeze_runtime(data, source_study, rows, reserve):
    residuals = {}
    for row in rows:
        key = str(int(row["local_hour"]))
        residuals.setdefault(row["station"], {}).setdefault(key, []).append(row["actual_f"] - row["physical_with_six_f"])
    evidence = {"engine": RUNTIME_ENGINE, "source_study": source_study, "residuals": residuals,
                "reserve": reserve["upper_one_sided_wilson"], "published_before": max(row["label"]["available_at"] for row in rows),
                "decision_hours": HOURS}
    vintage = digest(evidence)
    root = data / "research" / "runtime" / RUNTIME_FAMILY
    write_new(root / f"{vintage}.json", json_bytes(evidence) + b"\n")
    staging = root / f".{uuid4().hex}.tmp"
    try:
        staging.write_bytes(json_bytes({"vintage": vintage}) + b"\n")
        staging.replace(root / "active.json")
    finally:
        staging.unlink(missing_ok=True)
    return vintage


def load_runtime(data, vintage=None):
    source = data / "research" / "runtime" / RUNTIME_FAMILY
    if vintage is None and (source / "active.json").is_file():
        vintage = json.loads((source / "active.json").read_text())["vintage"]
    if not isinstance(vintage, str) or re.fullmatch(r"[0-9a-f]{64}", vintage) is None:
        return None
    file = source / f"{vintage}.json"
    if not file.is_file():
        return None
    model = json.loads(file.read_text())
    if model.get("engine") != RUNTIME_ENGINE or digest(model) != vintage:
        raise ValueError("Unsupported or damaged high precision artifact")
    return {**model, "vintage": vintage}


def predict_runtime(model, event, as_of):
    station = event["station"]
    if event.get("family", "daily-high") != "daily-high" or station not in model["residuals"]:
        return None
    at = parse_time(as_of)
    start, end = climate_window(event["date"], STATIONS[station])
    hours = [hour for hour in model["decision_hours"] if start + timedelta(hours=hour) <= at < end]
    if not hours:
        return None
    hour = max(hours)
    decision = start + timedelta(hours=hour)
    if decision < parse_time(model["published_before"]):
        return None
    native = [row for row in event["observations"]
              if row.get("source") in {"NWS", "Aviation Weather Center"} and not row.get("timing_proxy")]
    witness = max((w for row in native for w in metar_high_evidence(row, station, start, decision)),
                  key=lambda w: (w["lower_f"], -parse_time(w["available_at"]).timestamp()), default=None)
    context, _ = day_context(station, event["date"], event["forecasts"], native, decision)
    residuals = model["residuals"][station].get(str(hour), [])
    if not context or not witness or not residuals:
        return None
    maximum = max(context["expected_remaining_max_f"], witness["lower_f"])
    centers = [maximum + offset for offset in residuals]
    estimates = []
    for bucket in event["buckets"]:
        bounds = lo, hi = _limits(bucket)
        successes = sum(lo <= value < hi for value in centers)
        misses = len(centers) - successes
        estimates.append({
            "id": bucket["id"], "probability": guarded_probability(bounds, centers, 0.5, witness["lower_f"], model["reserve"]),
            "uncertain": len(centers) < MIN_BAND_DAYS or min(successes, misses) < MIN_TAIL_SUPPORT_DAYS,
            "validation_days": len(centers), "validation_residuals": len(centers), "yes_tail_support": successes,
            "no_tail_support": misses,
        })
    return {
        "vintage": model["vintage"], "version": RUNTIME_ENGINE, "method": "metar-precision-guarded-high",
        "target": "daily-high", "at": iso(at), "knowledge_at": iso(decision), "local_standard_decision_hour": hour,
        "model_high_f": maximum, "available_at": context["forecast"]["remaining"]["available_at"],
        "issued_at": context["forecast"]["remaining"]["issued_at"], "buckets": estimates,
        "context": {**context, "temperature_witness": witness, "climate_report_disagreement_reserve": model["reserve"]},
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", type=Path, default=DEFAULT_DATA)
    ap.add_argument("high_evidence", type=Path)
    args = ap.parse_args()
    data = args.data
    old = json.loads(gzip.decompress(args.high_evidence.read_bytes()))
    inputs = old["fit_and_split_inputs"]
    chosen = {phase: inputs[phase] for phase in ("calibration", "test")}
    first = date.fromisoformat(min(row["date"] for rows in chosen.values() for row in rows))
    last = date.fromisoformat(max(row["date"] for rows in chosen.values() for row in rows))
    obs = defaultdict(list)
    files, gaps = {}, {}

    def get(station):
        try:
            items, path = read_covering_observations(data, station, first, last + timedelta(days=2))
            return station, items, path, None
        except (OSError, ValueError) as error:
            return station, [], None, str(error)

    with ThreadPoolExecutor(max_workers=3) as pool:
        for station, readings, path, gap in pool.map(get, sorted(STATIONS)):
            if gap:
                gaps[station] = gap
                continue
            files[station] = path
            for row in readings:
                day = (parse_time(row["at"]) + timedelta(hours=STATIONS[station])).date()
                obs[station, str(day)].append(row)
    replay, conflicting_bounds = make_rows(chosen, obs)
    tested, reserve = evaluate(replay)
    exact_buckets = {(row["station"], row["date"]): row["buckets"] for row in old["exact_payout_predictions"]}
    exact_test, _ = evaluate(replay, exact_buckets)
    changed = [r for r in tested if r["physical_point_f"] != r["physical_with_six_f"]]
    measurements = [*metrics(tested, "later date test"), *metrics(exact_test, "exact Polymarket US payout buckets"),
                    *metrics(changed, "later decision changed by six-hour report")]
    examples = []
    for row in sorted(changed, key=lambda r: -(r["physical_with_six_f"] - r["physical_point_f"]))[:6]:
        examples.append({
            **row, "venue": POLYMARKET, "instrument_id": None,
            "title": f"{row['station']} six-hour maximum",
            "detail": f"Point-based high {row['physical_point_f']:.0f}°F, six-hour-aware {row['physical_with_six_f']:.0f}°F, "
                      f"official climate high {row['actual_f']:.0f}°F.",
        })
    for row in tested:
        key = f"{row['station']} / {row['date']}"
        if key in conflicting_bounds:
            option = next((z for z in exact_buckets.get((row["station"], row["date"]), []) if _outcome(z, row["actual_f"]) == 1), {})
            examples.insert(0, {**row, "venue": POLYMARKET, "instrument_id": option.get("id"),
                                "title": f"{row['station']} measured and official high differed",
                                "detail": f"Decoded report bound {row['all_observations_bound']['lower_f']}°F, official climate high {row['actual_f']}°F."})
            break
    setup = {"engine": ENGINE, "parent_evidence": digest(old), "original_date_range": [str(first), str(last)],
             "source_files": files, "guard_reserve": reserve,
             "observation_policy": "IEM archived raw METAR with fixed 10-minute receipt proxy"}
    ident = "six-hour-high-" + digest({"setup": setup, "tested": tested, "exact": exact_test})[:20]
    destination = data / "research" / "evidence" / "high-physical" / f"{ident}.json.gz"
    write_new(destination, gzip.compress(json_bytes({"setup": setup, "decisions": tested, "exact": exact_test,
                                                    "contradictions": conflicting_bounds}), mtime=0))
    runtime = freeze_runtime(data, ident, replay["calibration"], reserve)
    report = {
        "id": ident, "title": "Can a six-hour observation catch a missed daytime high?",
        "hypothesis": "A six-hour maximum wholly inside the climate day improves a physical high estimate on later dates.",
        "target": "Daily high temperature", "venues": [POLYMARKET],
        "created_at": iso(datetime.now(timezone.utc)), "date_window": {"from": str(first), "through": str(last)},
        "metrics": measurements, "cases": examples,
        "limits": [
            "Raw report and nominal reference windows must both fit inside the exact climate day.",
            "Historical observation visibility uses a fixed receipt lag. A live decision requires its actual receipt.",
            "Calibration and scoring use distinct calendar cohorts. A raw temperature bound does not replace official climate or exchange outcomes.",
        ],
        "evidence": {"changed_forward_decisions": len(changed), "source_bound_conflicts": conflicting_bounds,
                     "source_gaps": gaps, "guard_reserve": reserve},
        "provenance": {**setup, "frozen_evidence": str(destination.relative_to(data)), "shadow_runtime_vintage": runtime},
    }
    path = data / "research" / "models" / f"{ident}.json"
    write_new(path, json_bytes(report) + b"\n")
    print(json.dumps({"path": str(path), **{key: report[key] for key in ("metrics", "evidence")}}, indent=2))


if __name__ == "__main__":
    main()
