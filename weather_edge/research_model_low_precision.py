"""Bound a daily-low distribution with independently coded ASOS temperature evidence."""

from __future__ import annotations

import argparse
import gzip
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

from . import expansion_live_low, expansion_metar, expansion_weather, research_model, research_model_low_full_day
from .model import _benchmark_buckets, _limits, _outcome, _probability
from .nbm import climate_window

VERSION = "daily-low-precision-v1"
WINDOWS = {
    "calibration": research_model_low_full_day.WINDOWS["calibration"],
    "selection": research_model_low_full_day.WINDOWS["selection"],
    "december_2025": research_model_low_full_day.WINDOWS["test"],
    "january_2026": (date(2026, 1, 1), date(2026, 1, 31)),
    "april_2026": (date(2026, 4, 1), date(2026, 4, 30)),
    "november_2026": (date(2026, 10, 29), date(2026, 11, 14)),
}
METHODS = {
    "txn": "Carried NOAA TXN minimum",
    "combined": "Observed minimum plus remaining NOAA guidance",
    "precision_center": "Raw point and six-hour evidence + forecast",
    "hard_cap": "Raw evidence distribution cut at observed bound",
    "guarded_cap": "Bounded distribution with verified-exception allowance",
}


def _cohort_station(data, station, start, end):
    return research_model_low_full_day._station_inputs(data, station, start, end)


def _scoped_labels(data, station, day, expected):
    if expected is None:
        return None
    try:
        return expansion_weather.raw_report(data, station, day, expected["product"])
    except (OSError, KeyError, ValueError, TypeError):
        return None


def _collect(data, windows):
    rows, sources = {split: [] for split in windows}, {}
    gaps = []
    for split, (first, last) in windows.items():
        sources[split] = {}
        with ThreadPoolExecutor(max_workers=2) as pool:
            tasks = {station: pool.submit(_cohort_station, data, station, first, last) for station in research_model.STATIONS}
            for station, task in tasks.items():
                labels, observations, issued, refs = task.result()
                offset = expansion_weather.STATIONS[station][3]
                sources[split][station] = refs
                for day in research_model._days(first, last):
                    label = _scoped_labels(data, station, day, labels.get(str(day)))
                    if not label or label["low_f"] is None:
                        gaps.append({"split": split, "station": station, "date": str(day), "issue": "Raw complete CLI minimum unavailable"})
                        continue
                    opens, _ = climate_window(day, offset)
                    candidates = [card for delta in (-1, 0, 1) for card in issued[day + timedelta(days=delta)]]
                    for hour in research_model.HOURS:
                        at = opens + timedelta(hours=hour)
                        view = research_model_low_full_day._at(station, day, at, candidates, observations[day], label)
                        if not view:
                            gaps.append({"split": split, "station": station, "date": str(day), "hour": hour,
                                         "issue": "No exact remaining weather profile or carried TXN"})
                            continue
                        raw = expansion_weather.observations_as_of(observations[day], opens, at)
                        point = expansion_metar.upper_bound(raw, station, opens, at, include_six_hour=False)
                        extra = expansion_metar.upper_bound(raw, station, opens, at)
                        rows[split].append({**view, "metar_upper_bound": extra, "point_upper_bound": point})
    return {"version": VERSION, "rows": rows, "sources": sources, "missing": gaps,
            "windows": {split: [str(first), str(last)] for split, (first, last) in windows.items()}}


def _precision_mean(row):
    bound = row["metar_upper_bound"]
    return min(row["means"]["remaining"], bound["upper_f"]) if bound else row["means"]["combined"]


def _calibrate(rows):
    errors = defaultdict(list)
    outcomes = defaultdict(list)
    for row in rows:
        bucket = row["station"], row["hour"]
        for method, value in (("txn", row["means"]["txn"]), ("combined", row["means"]["combined"]),
                              ("precision_center", _precision_mean(row))):
            errors[(method, *bucket)].append((value - row["actual_f"]) ** 2)
        bound = row["metar_upper_bound"]
        if bound:
            outcomes[bucket].append(row["actual_f"] > bound["upper_f"])
    widths = {key: max(0.75, math.sqrt(statistics.mean(values))) for key, values in errors.items()}
    exceptions = {key: {"violation_count": sum(result), "calibration_days": len(result),
                        "uncapped_weight": min(1.0, expansion_live_low._wilson(sum(result), len(result))[1])}
                  for key, result in outcomes.items()}
    return widths, exceptions


def _bucket_probabilities(buckets, mean, width, cap=None, exception_weight=0):
    if cap is None:
        return [_probability(_limits(bucket), [mean], width) for bucket in buckets]
    cap = cap + 0.5  # Integer report values imply a half-degree continuous boundary.
    mass = _probability((-math.inf, cap), [mean], width)
    result = []
    for bucket in buckets:
        lo, hi = _limits(bucket)
        raw = _probability((lo, hi), [mean], width)
        retained = _probability((lo, min(hi, cap)), [mean], width) if lo < min(hi, cap) else 0.0
        p = retained / mass if mass else raw
        result.append((1 - exception_weight) * p + exception_weight * raw)
    return result


def _eval(rows, widths, exceptions):
    output = []
    anchors = {}
    for row in rows:
        case = row["station"], row["date"]
        group = row["station"], row["hour"]
        buckets = anchors.setdefault(case, _benchmark_buckets(round(row["means"]["txn"])))
        truth = [_outcome(bucket, row["actual_f"]) for bucket in buckets]
        precision = row["metar_upper_bound"]
        cap = precision["upper_f"] if precision else None
        weight = exceptions.get(group, {}).get("uncapped_weight", 1.0)
        center = _precision_mean(row)
        cfg = {
            "txn": (row["means"]["txn"], widths["txn", *group], None, 0),
            "combined": (row["means"]["combined"], widths["combined", *group], None, 0),
            "precision_center": (center, widths["precision_center", *group], None, 0),
            "hard_cap": (center, widths["precision_center", *group], cap, 0),
            "guarded_cap": (center, widths["precision_center", *group], cap, weight),
        }
        estimates = {}
        for method, (point, spread, upper, w) in cfg.items():
            probabilities = _bucket_probabilities(buckets, point, spread, upper, w)
            estimates[method] = {"mean_f": point, "error_f": point - row["actual_f"], "probabilities": probabilities,
                                 "brier": statistics.mean((p - y) ** 2 for p, y in zip(probabilities, truth, strict=True))}
        output.append({**row, "estimates": estimates, "buckets": buckets, "truth": truth,
                       "cap_violated": cap is not None and row["actual_f"] > cap, "uncapped_weight": weight})
    return output


def _case(witness, split):
    bound = witness["metar_upper_bound"]
    label = witness["official_report"]
    day, station = witness["date"], witness["station"]
    return {"station": station, "date": day, "at": witness["at"], "title": f"{station} {day} · {bound['kind']}",
            "detail": (f"Raw {bound['temperature_code']} corresponds to an upper reading of {bound['upper_f']}°F. "
                       f"Full-day CLI minimum was {label['low_f']}°F. Remaining forecast "
                       f"was {witness['means']['remaining']:.1f}°F."),
            "cohort": split, "metar_evidence": bound, "official_report": label, "forecast": witness["forecast"]}


def study(data, source=None):
    data = Path(data)
    inputs = json.loads(gzip.decompress(source.read_bytes())) if source else _collect(data, WINDOWS)
    frozen, digest = research_model.freeze_inputs(data, inputs)
    samples = inputs["rows"]
    widths, exceptions = _calibrate(samples["calibration"])
    selected = None
    metrics, chosen_examples = [], []
    diagnostics, evaluation = {}, {}
    for split, rows in samples.items():
        if split == "calibration":
            continue
        scored = _eval(rows, widths, exceptions)
        evaluation[split] = scored
        comparable = [row for row in scored if row["metar_upper_bound"]]
        comparisons = research_model.metrics(comparable, split, METHODS)
        if split == "selection":
            selected = min(comparisons, key=lambda row: row["bucket_brier"])["method"]
        metrics.extend(comparisons)
        for hour in (3, 9, 13, 17, 21):
            metrics.extend(research_model.metrics([row for row in comparable if row["hour"] == hour],
                                                   f"{split} · {hour:02}:00 LST", METHODS))
        sources = Counter(row["metar_upper_bound"]["kind"] for row in comparable)
        unique_exceptions = {(row["station"], row["date"]) for row in comparable if row["cap_violated"]}
        unique_six = {(row["station"], row["date"]) for row in comparable
                      if row["metar_upper_bound"]["kind"] == "six-hour low"}
        six_improvements = [row for row in comparable if row["metar_upper_bound"]["kind"] == "six-hour low"
                            and (not row["point_upper_bound"]
                                 or row["metar_upper_bound"]["upper_f"] < row["point_upper_bound"]["upper_f"])]
        diagnostics[split] = {
            "scored_decisions_with_raw_metar": len(comparable), "all_eligible_forecasts": len(scored),
            "observed_bound_exceptions": len(unique_exceptions), "bound_exception_decisions": sum(row["cap_violated"] for row in comparable),
            "witness_kind": dict(sources), "six_hour_station_days": len(unique_six),
            "six_hour_bound_improvements": len(six_improvements),
            "cohort_contrasts": [research_model.date_bootstrap(comparable, "combined", candidate, "brier")
                                 for candidate in ("precision_center", "hard_cap", "guarded_cap")] if comparable else [],
        }
        if comparable and split != "selection":
            pool = [row for row in comparable if row["cap_violated"]] or six_improvements or comparable
            candidate = max(pool, key=lambda row: row["means"]["combined"] - row["metar_upper_bound"]["upper_f"])
            chosen_examples.append(_case(candidate, split))
        metrics.append({"method": "metar_bound_audit", "label": "Recorded upper bound audit", "group": split,
                        "calendar_dates": len({row["date"] for row in comparable}),
                        "station_days": len({(row["station"], row["date"]) for row in comparable}),
                        "bound_exceptions": len(unique_exceptions), "six_hour_improvements": len(six_improvements)})
    prefit = [{"station": station, "decision_hour": hour, **entry} for (station, hour), entry in sorted(exceptions.items())]
    record = {
        "version": VERSION, "title": "Daily-low bounds from raw station temperature code",
        "hypothesis": "Recovering the degree-Fahrenheit precision of raw station data improves probability forecasts for a daily minimum, while verified exceptions justify reserve probability.",
        "target": "Whole-degree Central Park and Midway NWS CLI minimum",
        "venues": [], "date_window": inputs["windows"], "metrics": metrics, "cases": chosen_examples,
        "limits": [
            "Hourly and special METAR tenths Celsius and the whole-Celsius group imply different sets of internally stored whole-degree ASOS values.",
            "Six-hour minima enter only when report-time and nominal-hour bounds both fit completely inside the exact climate day and the report is available by the decision.",
            "Weather replays use an assumed ten-minute ASOS receipt lag. Actual daily CLI corrections and sensor quality checks can defeat a reported reading bound.",
            "The guarded cutoff reserves probability using the upper end of the earlier station/time Wilson interval for observed bound violations. Hard truncation is scored as a separate diagnostic.",
            "The four held-out cohorts certify original station-day CLI text and use a fixed earlier calibration. Raw-source completeness varies by date.",
        ],
        "provenance": {"input_path": str(frozen.relative_to(data)), "input_sha256": digest, "pretest_selected": selected,
                       "source_sha256": research_model.run_sources(sys.modules[__name__], expansion_metar, research_model_low_full_day),
                       "source_definitions": [expansion_metar.ASOS_FAQ, expansion_metar.METAR_DOCS, expansion_metar.ASOS_USER_GUIDE]},
        "diagnostics": diagnostics, "calibration_bound_exceptions": prefit, "missing": inputs["missing"],
    }
    return research_model.write_record(data, record, evaluation)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path(".data"))
    parser.add_argument("--from-input", type=Path)
    args = parser.parse_args()
    print(study(args.data_dir, args.from_input))


if __name__ == "__main__":
    main()
