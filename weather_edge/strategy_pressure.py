"""Test original METAR sea-level pressure change as a daily-high feature."""

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from functools import lru_cache
from hashlib import sha256
import json
from pathlib import Path
import re
from statistics import mean

from sklearn.feature_extraction import DictVectorizer
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from . import collector, expansion_metar, expansion_weather, history, research_model, strategy_dewpoint, strategy_high_transfer, venue_archive
from .market import iso, parse_time
from .model import _benchmark_buckets, _limits, _outcome
from .nbm import climate_window
from .strategy_metar_high import disagreement_reserve, guarded_probability


VERSION = "original-metar-pressure-high-v1"
SOURCE = "https://www.weather.gov/media/asos/appen.pdf"
PRESSURE = re.compile(r"(?<!\S)SLP(\d{3})(?=\s|$)")
MAX_LATEST_AGE = timedelta(minutes=80)
SPAN = timedelta(hours=3)
SPAN_TOLERANCE = timedelta(minutes=20)
METHODS = ("Physical Ridge and dew point", "Physical Ridge, dew point and pressure change")


def pressure_point(row, station):
    raw = (row.get("raw_report") or "").strip().rstrip("=")
    ident = expansion_metar.REPORT_START.match(raw)
    stamp = parse_time(row["at"])
    if not ident or ident[1] != station or stamp.strftime("%d%H%M") != ident[2]:
        return None
    found = PRESSURE.search(raw.partition(" RMK ")[2])
    if not found:
        return None
    code = int(found[1])
    hpa = (10000 if code < 500 else 9000) + code
    return {"at": iso(stamp), "pressure_hpa": hpa / 10, "raw_report": raw, "source_url": row.get("source_url")}


def _nominal(stamp):
    return (stamp + timedelta(minutes=15)).replace(minute=0, second=0, microsecond=0)


def _retained_receipt(cache, point, station):
    if not point:
        return None
    stamp = parse_time(point["at"])
    observations = _hour_receipts(cache, _nominal(stamp), station)
    proof = observations.get((point["at"], history._report_key(point["raw_report"])))
    if not proof:
        return None
    received, source = proof
    return {**point, "available_at": received, "receipt_source": source}


@lru_cache(maxsize=1536)
def _hour_receipts(cache, hour, station):
    path = cache / hour.strftime("%Y%m%d_%H00.gz")
    return history._madis_hour(hour, station, cache) if path.is_file() else {}


def qualified_change(reported, station, start, at, cache):
    eligible = []
    for row in reported:
        stamp = parse_time(row["at"])
        if not max(start, at - MAX_LATEST_AGE - SPAN - SPAN_TOLERANCE) <= stamp <= at:
            continue
        point = _retained_receipt(cache, pressure_point(row, station), station)
        if point and parse_time(point["available_at"]) <= at:
            eligible.append((stamp, point))
    if not eligible:
        return None
    latest_at, latest = max(eligible, key=lambda point: (point[0], parse_time(point[1]["available_at"])))
    if at - latest_at > MAX_LATEST_AGE:
        return None
    earlier = [point for point in eligible if abs((latest_at - point[0]) - SPAN) <= SPAN_TOLERANCE]
    if not earlier:
        return None
    earlier_at, first = min(earlier, key=lambda point: (abs((latest_at - point[0]) - SPAN), point[0],
                                                       -parse_time(point[1]["available_at"]).timestamp()))
    hours = (latest_at - earlier_at).total_seconds() / 3600
    return {"current": latest, "prior": first, "elapsed_hours": hours,
            "trend_hpa_per_3h": (latest["pressure_hpa"] - first["pressure_hpa"]) * 3 / hours}


def _ensure_hours(data, observations, groups, budget):
    cache = data / "madis-metar"
    tasks = {}
    for frames in groups.values():
        for row in frames:
            station, day = row["station"], row["date"]
            at = parse_time(row["at"])
            for report in observations[station, day]:
                reported = parse_time(report["at"])
                if at - MAX_LATEST_AGE - SPAN - SPAN_TOLERANCE <= reported <= at and pressure_point(report, station):
                    hour = _nominal(reported)
                    if not (cache / hour.strftime("%Y%m%d_%H00.gz")).is_file():
                        tasks.setdefault(hour, station)

    def fetch(entry):
        hour, station = entry
        if collector.paused(data):
            return iso(hour), "Collection paused locally"
        try:
            history._madis_hour(hour, station, cache)
        except (OSError, ValueError, KeyError, AttributeError, EOFError) as error:
            return iso(hour), str(error)
        return iso(hour), None if (cache / hour.strftime("%Y%m%d_%H00.gz")).is_file() else "Public file unavailable"

    selected = sorted(tasks.items())[:budget]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(fetch, selected))
    return {"nominal_hours_historically_missing": len(tasks), "nominal_hours_selected_for_fetch": len(selected),
            "nominal_hours_retained": sum(issue is None for _, issue in results),
            "nominal_hour_issues": {at: issue for at, issue in results if issue is not None}}


def _inputs(row, method):
    features = strategy_dewpoint.predictors(row, strategy_dewpoint.MODELS[2])
    if method == METHODS[1]:
        features["pressure_change_hpa_3h"] = row["pressure"]["trend_hpa_per_3h"]
    return features


def _model(cohorts):
    train, cal = cohorts["fit"], cohorts["calibration"]
    error = [row["actual_f"] - row["physical_with_six_f"] for row in train]
    models, residuals = {}, defaultdict(list)
    for method in METHODS:
        model = make_pipeline(DictVectorizer(sparse=False), StandardScaler(), Ridge(alpha=5))
        model.fit([_inputs(row, method) for row in train], error)
        models[method] = model
        for row, predicted in zip(cal, model.predict([_inputs(row, method) for row in cal]), strict=True):
            residuals[method, row["local_hour"]].append(float(row["actual_f"] - row["physical_with_six_f"] - predicted))
    return models, residuals


def _evaluate(rows, models, residuals, reserve, markets=None):
    output = []
    for row in rows:
        bounds = _benchmark_buckets(round(row["raw_f"])) if markets is None else markets.get((row["station"], row["date"]))
        if not bounds or markets is not None and any(_outcome(item, row["actual_f"]) != item["payout"] for item in bounds):
            continue
        lower = row["all_observations_bound"]["lower_f"]
        estimates = {}
        for method in METHODS:
            correction = float(models[method].predict([_inputs(row, method)])[0])
            center = row["physical_with_six_f"] + correction
            days = residuals[method, row["local_hour"]]
            if not days:
                break
            samples = [center + error for error in days]
            probs = [guarded_probability(_limits(item), samples, 0.5, lower, reserve) for item in bounds]
            point = max(lower, mean(samples))
            estimates[method] = {"high_f": point, "error_f": point - row["actual_f"], "probabilities": probs,
                                 "brier": mean((p - _outcome(item, row["actual_f"])) ** 2 for p, item in zip(probs, bounds, strict=True))}
        if len(estimates) == len(METHODS):
            output.append({**row, "buckets": bounds, "estimates": estimates})
    return output


def report(data, source, warm, fetch):
    phases = strategy_dewpoint.WARM_PHASES if warm else strategy_dewpoint.PHASES
    earlier, cohorts, previous_withheld = strategy_dewpoint.phase_rows(data, source, phases)
    _, raw_evidence = strategy_high_transfer._archived(data, source)
    observations = strategy_dewpoint.load_observations(data, raw_evidence["sources"])
    acquisition = _ensure_hours(data, observations, cohorts, fetch)
    _hour_receipts.cache_clear()
    accepted, missing = defaultdict(list), Counter()
    for phase, rows in cohorts.items():
        for row in rows:
            station, day = row["station"], row["date"]
            start, _ = climate_window(day, expansion_weather.STATIONS[station][3])
            change = qualified_change(observations[station, day], station, start, parse_time(row["at"]), data / "madis-metar")
            if change:
                accepted[phase].append({**row, "pressure": change})
            else:
                missing[phase] += 1
    if any(not accepted[phase] for phase in phases):
        raise ValueError("The matched earlier and later cohorts require original MADIS pressure receipt evidence")
    models, residuals = _model(accepted)
    reserve = disagreement_reserve(accepted["calibration"])["upper_one_sided_wilson"]
    later = accepted["later test"]
    splits = {"Official daily high": _evaluate(later, models, residuals, reserve)}
    payouts = strategy_high_transfer.native_buckets(data, later, window=tuple(map(date.fromisoformat, phases["later test"])))
    for venue in venue_archive.VENUES:
        market = {(station, day): buckets for (source_venue, station, day), buckets in payouts.items() if source_venue == venue}
        if market:
            splits[venue] = _evaluate(later, models, residuals, reserve, market)
    metrics, cases, intervals = [], [], {}
    for scope, rows in splits.items():
        if not rows:
            continue
        intervals[scope] = [research_model.date_bootstrap(rows, *METHODS, value) for value in ("brier", "mae")]
        for hour in (None, *strategy_high_transfer.HOURS):
            matched = [row for row in rows if hour is None or row["local_hour"] == hour]
            if matched:
                for result in research_model.metrics(matched, methods={method: method for method in METHODS}):
                    metrics.append({**result, "target": scope, "local_standard_hour": hour or "all"})
        if scope not in venue_archive.VENUES:
            continue
        for sign in (1, -1):
            row = min(rows, key=lambda r: sign * (r["estimates"][METHODS[1]]["brier"] - r["estimates"][METHODS[0]]["brier"]))
            bucket = next(item for item in row["buckets"] if item["payout"])
            base, new = (row["estimates"][method]["high_f"] for method in METHODS)
            cases.append({"station": row["station"], "date": row["date"], "at": row["at"], "venue": scope,
                          "instrument_id": bucket["instrument_id"], "title": f'{row["station"]} pressure tendency and the native high',
                          "detail": f'Pressure changed {row["pressure"]["trend_hpa_per_3h"]:+.1f} hPa per three hours. The baseline high was {base:.1f}°F, and the added-pressure estimate was {new:.1f}°F. Official high {row["actual_f"]:.0f}°F.',
                          "data": row})
    record = {"version": VERSION, "title": "Can a pressure trend improve the daily high?",
              "hypothesis": "An original same-station METAR sea-level pressure change adds information beyond the physical high forecast and observed humidity.",
              "target": "Exact high from the original CLI, with matched native bucket payouts where available.",
              "venues": list(venue_archive.VENUES), "date_window": {"from": phases["fit"][0], "through": phases["later test"][1], **phases},
              "metrics": metrics, "cases": cases, "confidence": intervals,
              "limits": ["The pressure pair uses exact retained original MADIS receipt clocks. Earlier physical and dew-point inputs retain the prior study’s as-of assumptions.",
                         "The pressure feature approximates a three-hour change with a 20-minute tolerance. The two models use identical dates, forecasts and labels.",
                         "Venue scores require agreement between the exact archived payout and official temperature threshold. No historical market fill was inferred."],
              "provenance": {"engine": VERSION, "source_format": SOURCE, "source_model": earlier["id"], "source_implementation_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
                             "pressure_native_unit": "hPa", "max_latest_age_minutes": 80, "pressure_prior_span_minutes": 180,
                             "matched_frames_by_phase": {key: len(value) for key, value in accepted.items()}, "missing_original_pressure": dict(missing),
                             "prior_weather_withheld": previous_withheld, "acquisition": acquisition, "ridge_alpha": 5, "guard_reserve": reserve}}
    return research_model.write_record(data, record, {"training_inputs": accepted["fit"], "calibration_inputs": accepted["calibration"], "test": splits})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=venue_archive.DEFAULT_DATA)
    parser.add_argument("--warm", action="store_true")
    parser.add_argument("--fetch", type=int, default=0, metavar="MAX_HOURLY_FILES")
    parser.add_argument("source", type=Path)
    args = parser.parse_args()
    if args.fetch < 0:
        parser.error("Provide a non-negative hourly file budget")
    saved = report(args.data, args.source, args.warm, args.fetch)
    obj = json.loads(saved.read_bytes())
    print(json.dumps({"path": str(saved), "metrics": obj["metrics"], "confidence": obj["confidence"], "provenance": obj["provenance"]}, indent=2))


if __name__ == "__main__":
    main()
