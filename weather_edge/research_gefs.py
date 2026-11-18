"""Test whether native GEFS spread improves station-day temperature probabilities."""
from __future__ import annotations

import argparse
from collections import defaultdict
import gzip
from hashlib import sha256
import json
import math
from pathlib import Path
import random
import statistics
import sys

from sklearn.linear_model import LinearRegression

from . import expansion_weather, gefs, research_hrrr, research_model
from .archive import date_range
from .nbm import climate_window, iso, utc

VERSION = "gefs-daily-extrema-v1"
WINDOWS = research_hrrr.WINDOWS
METHODS = (
    "nbm-calibrated", "gefs-member-count", "gefs-constant-spread", "gefs-calibrated-spread",
)


def inputs(data):
    data = Path(data)
    labels = {station: expansion_weather.daily_labels(data, station, WINDOWS[0][1], WINDOWS[-1][2])
              for station in gefs.CANDIDATES}
    sets = {kind: [] for kind, _, _ in WINDOWS}
    gaps = defaultdict(int)
    sources = {}
    for station in gefs.CANDIDATES:
        path = expansion_weather.cli_file(data, station, WINDOWS[0][1].year)
        sources[station] = {"path": str(path.relative_to(data)), "sha256": sha256(path.read_bytes()).hexdigest()}
    for i, (split, first, last) in enumerate(WINDOWS):
        for day in date_range(first, last):
            for station in gefs.CANDIDATES:
                offset = gefs.STATIONS[station][3]
                start, close = climate_window(day, offset)
                next_start = climate_window(WINDOWS[i + 1][1], offset)[0] if i + 1 < len(WINDOWS) else None
                label = labels[station].get(str(day))
                if label is None or (next_start and utc(label["published_at"]) >= next_start):
                    gaps[split, "complete CLI published by next window"] += 1
                    continue
                members = gefs.day_members(data, day, station)
                if len(members) != len(gefs.MEMBERS):
                    gaps[split, "complete native ensemble published before climate day"] += 1
                    continue
                for target in ("high", "low"):
                    nbm = research_hrrr._nbm(data, station, day, target, start, close)
                    actual = label[f"{target}_f"]
                    if nbm is None or actual is None:
                        gaps[split, f"{target} NBM/CLI"] += 1
                        continue
                    samples = [row[f"{target}_f"] for row in members]
                    buckets = (research_hrrr._verified_buckets(data, station, day, target, actual, start, close)
                               if split == "test" else [])
                    sets[split].append({
                        "target": target, "station": station, "date": str(day),
                        "at": iso(start), "actual_f": actual, "nbm": nbm,
                        "ensemble_mean_f": statistics.mean(samples), "ensemble_spread_f": statistics.pstdev(samples),
                        "members": members, "official_report": label, "buckets": buckets,
                    })
    return {"version": VERSION, "sets": sets, "official_source_digests": sources,
            "gaps": [{"split": split, "reason": reason, "station_days": count}
                     for (split, reason), count in sorted(gaps.items())]}


def _case(row, summary):
    winner = next((r for r in row["buckets"] if r["yes_payout"]), None)
    return {
        "station": row["station"], "date": row["date"], "at": row["at"],
        "title": f"{row['station']} {row['date']} · GEFS {row['target']} ensemble",
        "venue": "polymarket-us" if winner else None, "instrument_id": winner["id"] if winner else None,
        "detail": (f"Official {row['target']} {row['actual_f']:.1f}°F. NBM {row['nbm']['mean_f']}°F. "
                   f"GEFS mean {row['ensemble_mean_f']:.1f}°F and raw spread {row['ensemble_spread_f']:.1f}°F. "
                   f"Calibrated spread forecast uses a {summary['spread_sd_f']:.1f}°F standard deviation."),
        "official_report": row["official_report"], "bucket": winner,
        "forecast": {
            "member_count": len(row["members"]), "ensemble_mean_f": row["ensemble_mean_f"],
            "ensemble_spread_f": row["ensemble_spread_f"], "calibrated": summary, "nbm": row["nbm"],
            "member_extrema_f": [r[f"{row['target']}_f"] for r in row["members"]],
        },
    }


def _member_probability(bucket, values, bucket_count):
    lower, upper = bucket["low_f"], bucket["high_f"]
    integers = [math.floor(value + .5) for value in values]
    hits = sum((lower is None or value >= lower) and (upper is None or value <= upper) for value in integers)
    return (hits + .5) / (len(values) + .5 * bucket_count)


def _mean_interval(groups):
    dates = sorted(groups)
    if len(dates) < 2:
        return None
    rng = random.Random(20261115)
    draws = []
    for _ in range(2000):
        batch = [delta for day in rng.choices(dates, k=len(dates)) for delta in groups[day]]
        draws.append(statistics.mean(batch))
    lower, *_, upper = statistics.quantiles(draws, n=40)
    return [lower, upper]


def target_results(training, calibration, evaluation):
    biases = defaultdict(list)
    for row in training:
        biases[row["station"]].append(row["actual_f"] - row["ensemble_mean_f"])
    station_bias = {station: statistics.mean(errors) for station, errors in biases.items()}
    default_bias = statistics.mean(error for errors in biases.values() for error in errors)
    def center(row):
        return row["ensemble_mean_f"] + station_bias.get(row["station"], default_bias)

    nbm_variance = max(.25, statistics.mean((row["actual_f"] - row["nbm"]["mean_f"]) ** 2 for row in calibration))
    residual = [(row["actual_f"] - center(row)) ** 2 for row in calibration]
    var_constant = max(.25, statistics.mean(residual))
    estimator = LinearRegression(positive=True).fit([[row["ensemble_spread_f"] ** 2] for row in calibration], residual)
    slopes = float(estimator.coef_[0]), float(estimator.intercept_)

    errors = defaultdict(list)
    scores = defaultdict(list)
    spread_deltas = defaultdict(list)
    witnesses = []
    for row in evaluation:
        adjusted = center(row)
        spread_sd = math.sqrt(max(.25, slopes[1] + slopes[0] * row["ensemble_spread_f"] ** 2))
        means = {
            "nbm-calibrated": row["nbm"]["mean_f"], "gefs-member-count": row["ensemble_mean_f"],
            "gefs-constant-spread": adjusted, "gefs-calibrated-spread": adjusted,
        }
        sds = {"nbm-calibrated": math.sqrt(nbm_variance), "gefs-constant-spread": math.sqrt(var_constant),
               "gefs-calibrated-spread": spread_sd}
        members = [member[f"{row['target']}_f"] for member in row["members"]]
        for method, mean in means.items():
            errors[method].append(row["actual_f"] - mean)
        station_scores = []
        for bucket in row["buckets"]:
            outcomes = {}
            for method, mean in means.items():
                p = (_member_probability(bucket, members, len(row["buckets"])) if method == "gefs-member-count"
                     else research_hrrr._probability(bucket, mean, sds[method]))
                loss = (p - bucket["yes_payout"]) ** 2
                scores[method].append(loss)
                outcomes[method] = loss
            diff = outcomes["gefs-calibrated-spread"] - outcomes["gefs-constant-spread"]
            spread_deltas[row["date"]].append(diff)
            station_scores.append(diff)
        witnesses.append((row, {
            "adjusted_mean_f": adjusted, "nbm_sd_f": sds["nbm-calibrated"], "spread_sd_f": spread_sd,
            "constant_sd_f": sds["gefs-constant-spread"],
            "spread_minus_constant_brier": statistics.mean(station_scores) if station_scores else None,
        }))
    metrics = [
        {"target": evaluation[0]["target"], "method": method, "station_days": len(evaluation),
         "calendar_dates": len({row["date"] for row in evaluation}),
         "mae_f": statistics.mean(abs(err) for err in errors[method]), "bucket_cases": len(scores[method]),
         "bucket_station_days": sum(bool(row["buckets"]) for row in evaluation),
         "brier": statistics.mean(scores[method]) if scores[method] else None}
        for method in METHODS
    ]
    coverage = {"fit_station_days": len(training), "calibration_station_days": len(calibration),
                "station_bias_f": station_bias, "constant_sd_f": math.sqrt(var_constant),
                "spread_variance_slope": slopes[0], "spread_variance_intercept": slopes[1],
                "spread_minus_constant_brier_date_interval_95": _mean_interval(spread_deltas)}
    ranked = sorted((case for case in witnesses if case[1]["spread_minus_constant_brier"] is not None),
                    key=lambda case: case[1]["spread_minus_constant_brier"])
    if not ranked:
        ranked = sorted(witnesses, key=lambda pair: abs(pair[0]["actual_f"] - pair[0]["ensemble_mean_f"]))
    cases = [_case(*case) for case in ([ranked[0], ranked[-1]] if len(ranked) > 1 else ranked)]
    return metrics, cases, coverage


def run(data, frozen=None):
    if frozen is None:
        evidence = inputs(data)
        frozen, digest = research_model.freeze_inputs(Path(data), evidence)
    else:
        body = gzip.decompress(Path(frozen).read_bytes())
        evidence = json.loads(body)
        digest = sha256(body).hexdigest()
    metrics, cases, coverage = [], [], {}
    for target in ("high", "low"):
        subsets = [[row for row in evidence["sets"][split] if row["target"] == target] for split, *_ in WINDOWS]
        if not all(subsets):
            continue
        rows, examples, parameters = target_results(*subsets)
        metrics.extend(rows)
        cases.extend(examples)
        coverage[target] = parameters
    return research_model.write_record(data, {
        "version": VERSION,
        "title": "Does native GEFS spread improve high and low probability estimates?",
        "hypothesis": "The dispersion of the 31 native forecast members predicts later forecast error and improves exact-bucket probability calibration over a constant residual variance.",
        "target": "central standard time daily high and low",
        "venues": ["polymarket-us"],
        "date_window": {split: [str(first), str(last)] for split, first, last in WINDOWS},
        "metrics": metrics, "cases": cases,
        "limits": [
            "The 00Z 0.5° ensemble has four native six-hour windows that exactly tile the UTC−06 climate day. Other offsets are outside this comparison.",
            "GEFS uses the nearest gridded forecast cell. The official NWS climate report supplies the point-station outcome.",
            "Four observations from the same model member are combined by max or min. The study keeps the physical dependency across its windows.",
            "Only complete binary Polymarket US bucket partitions with verified rule window and matching payout support Brier scoring. Native sample counts use a half-count per bucket.",
            "The paired confidence interval resamples climate dates, including all stations on each drawn date.",
        ],
        "provenance": {
            "input": str(frozen), "input_sha256": digest, "model_grid": gefs.DOCS,
            "gaps": evidence["gaps"], "fitted": coverage, "stations": gefs.CANDIDATES,
            "source_code": research_model.run_sources(sys.modules[__name__], gefs, research_hrrr),
            "reproduce": f"python -m weather_edge.research_gefs --inputs {frozen}",
        },
    })


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data", type=Path, default=Path(".data"))
    p.add_argument("--inputs", type=Path)
    args = p.parse_args()
    print(run(args.data, args.inputs))


if __name__ == "__main__":
    main()
