"""Compare station-day HRRR and GEFS precipitation at the same four stations."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date
import gzip
from hashlib import sha256
import json
import math
from pathlib import Path
import statistics
import sys

from sklearn import __version__ as sklearn_version
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from . import hrrr, hrrr_precipitation, research_gefs, research_gefs_precipitation as ensemble, research_model


VERSION = "hrrr-gefs-rain-v1"
METHODS = {
    "nbm": ensemble.METHODS["nbm"], "gefs": ensemble.METHODS["gefs"], "hrrr": "HRRR literal threshold",
    "nbm_logistic": ensemble.METHODS["nbm_logistic"], "hrrr_logistic": "Earlier-calibrated HRRR amount",
    "nbm_hrrr": "Earlier-calibrated NBM plus HRRR", "nbm_gefs": ensemble.METHODS["blend_logistic"],
    "all": "Earlier-calibrated NBM plus both forecast models",
}


def inputs(data, paths):
    cohorts = {}
    for path in paths:
        body = gzip.decompress(path.read_bytes())
        source = json.loads(body)
        season = "summer" if "summer" in source["version"] else "autumn"
        if season in cohorts:
            raise ValueError("Use one frozen GEFS selection per season")
        sets = {}
        missing = Counter()
        for split, rows in source["sets"].items():
            sets[split] = []
            for row in rows:
                forecast = hrrr_precipitation.day_amount(data, date.fromisoformat(row["date"]), row["station"])
                if forecast is None:
                    missing[split] += 1
                    continue
                sets[split].append({**row, "hrrr": forecast})
        cohorts[season] = {"sets": sets, "missing": dict(missing), "windows": source["windows"],
                           "source": str(path), "source_sha256": sha256(body).hexdigest()}
    return {"version": VERSION, "cohorts": cohorts}


def _features(row):
    frequency, _, native = ensemble._features(row)
    nbm = native["nbm_logistic"]
    gefs = native["gefs_logistic"]
    amount = row["hrrr"]["amount_mm"]
    fine = [math.log1p(amount / ensemble.THRESHOLD_MM)]
    return frequency, {"nbm_logistic": nbm, "hrrr_logistic": fine, "nbm_hrrr": nbm + fine,
                       "nbm_gefs": nbm + gefs, "all": nbm + gefs + fine}


def _score(sets):
    fit = sets["fit"]
    training = [_features(row)[1] for row in fit]
    models = {}
    for method in METHODS.keys() - {"nbm", "gefs", "hrrr"}:
        model = make_pipeline(StandardScaler(), LogisticRegression(C=1, random_state=ensemble.SEED))
        model.fit([row[method] for row in training], [row["truth"] for row in fit])
        models[method] = model
    scored = {}
    for split in ("calibration", "test"):
        scored[split] = []
        for row in sets[split]:
            member_fraction, features = _features(row)
            amount, allowance = row["hrrr"]["amount_mm"], row["hrrr"]["packing_error_mm"]
            exceedance = 0.5 if abs(amount - ensemble.THRESHOLD_MM) <= allowance else float(amount >= ensemble.THRESHOLD_MM)
            ps = {"nbm": row["forecast"]["p06_overlap_probability"], "gefs": member_fraction, "hrrr": exceedance}
            for method, model in models.items():
                ps[method] = float(model.predict_proba([features[method]])[0, 1])
            scored[split].append({**row, "probabilities": ps})
    return scored


def _metrics(cases, season, stage, *, threshold=None):
    return [{"group": season, "evaluation": stage, "method": method, "label": label, "station_days": len(cases),
             "calendar_dates": len({row["date"] for row in cases}), "yes_cases": sum(row["truth"] for row in cases),
             "brier": statistics.mean((row["probabilities"][method] - row["truth"]) ** 2 for row in cases),
             **({"gefs_member_frequency_minimum": threshold} if threshold is not None else {})}
            for method, label in METHODS.items()] if cases else []


def _contrast(rows, preferred):
    deltas = defaultdict(list)
    for row in rows:
        ps, label = row["probabilities"], row["truth"]
        deltas[row["date"]].append((ps[preferred] - label) ** 2 - (ps["nbm_logistic"] - label) ** 2)
    return {"mean": statistics.mean(d for day in deltas.values() for d in day),
            "date_bootstrap_interval_95": research_gefs._mean_interval(deltas), "separate_dates": len(deltas)}


def _case(row, season):
    truth, station, day = row["truth"], row["station"], row["date"]
    official = row["official_report"]
    probs = row["probabilities"]
    members_wet = round(len(row["members"]) * probs["gefs"])
    return {"title": f"{station} {day} · native short-range and ensemble precipitation", "station": station, "date": day,
            "at": row["at"], "detail": (f"3 km HRRR total {row['hrrr']['amount_mm']:.2f} mm. "
                                        f"{members_wet}/31 GEFS members crossed 0.254 mm. Raw report {official['precip_as_reported']} in. "
                                        f"The verified threshold labeled this {'wet' if truth else 'below measurable'}."),
            "forecast": {"hrrr": row["hrrr"], "gefs_member_total_mm": [p["liquid_mm"] for p in row["members"]],
                         "probabilities": probs, "nbm": row["forecast"]}, "official_report": official, "season": season}


def run(data, ge_inputs, frozen=None):
    if frozen is None:
        evidence = inputs(data, ge_inputs)
        frozen, digest = research_model.freeze_inputs(data, evidence)
    else:
        raw = gzip.decompress(frozen.read_bytes())
        evidence, digest = json.loads(raw), sha256(raw).hexdigest()
    metrics, cases, coverage, details = [], [], {}, {}
    for season, cohort in evidence["cohorts"].items():
        scored = _score(cohort["sets"])
        selection = _metrics(scored["calibration"], season, "Pre-test selection")
        tuned = [row for row in selection if row["method"] not in {"nbm", "gefs", "hrrr"}]
        best = min(tuned, key=lambda row: row["brier"])["method"]
        test = scored["test"]
        certain = [row for row in test if row["probabilities"]["gefs"] >= .95]
        metrics.extend(selection)
        metrics.extend(_metrics(test, season, "Held-out same station-days"))
        metrics.extend(_metrics(certain, season, "Held-out >=95% GEFS wet members", threshold=.95))
        coverage[season] = {"matched": {key: len(rows) for key, rows in cohort["sets"].items()}, "missing_hrrr": cohort["missing"],
                            "pretest_selected": best, "selected_minus_nbm_calibration": _contrast(test, best),
                            "nbm_hrrr_minus_nbm_calibration": _contrast(test, "nbm_hrrr"),
                            "all_models_minus_nbm_calibration": _contrast(test, "all")}
        ranked = sorted(test, key=lambda row: abs(row["probabilities"]["hrrr"] - row["probabilities"]["gefs"]), reverse=True)
        cases.extend(_case(row, season) for row in ranked[:2])
        ord_miss = next((row for row in test if row["station"] == "KORD" and row["date"] == "2026-11-13"), None)
        if ord_miss and ord_miss not in ranked[:2]:
            cases.append(_case(ord_miss, season))
        details[season] = test
    return research_model.write_record(data, {
        "version": VERSION, "title": "Does 3 km short-range guidance improve the measurable-rain decision?",
        "hypothesis": "Fine-grid HRRR resolves precipitation near the exact station that a half-degree GEFS ensemble misses.",
        "target": "Native weather forecast of measurable precipitation in the fixed UTC−06 climate day",
        "date_window": {name: cohort["windows"] for name, cohort in evidence["cohorts"].items()},
        "venues": [], "metrics": metrics, "cases": cases, "coverage": coverage,
        "limits": [
            "HRRR day total subtracts the native 00Z–06Z cumulative field from the native 00Z–30Z field on the same 3 km grid point. Negative differences beyond packed-field precision are ineligible.",
            "The archive clock requires both HRRR fields and all 31 GEFS paths to be known by 06Z. The eligibility intersection is identical across methods.",
            "The literal deterministic forecast is scored as 0 or 1 when packing precision supports that threshold, and 0.5 when the encoded range spans the threshold. It supplies an error benchmark for calibrated methods.",
            "Each season uses its own earlier fit and intervening selection. Brier differences use paired whole-date resamples. Exact native WFO gauge reports supply the official label.",
        ],
        "provenance": {"inputs": str(frozen), "input_sha256": digest, "registry": hrrr.DOCS, "scikit_learn": sklearn_version,
                       "earlier_gefs_inputs": {key: {"path": row["source"], "sha256": row["source_sha256"]}
                                                for key, row in evidence["cohorts"].items()},
                       "source_sha256": research_model.run_sources(sys.modules[__name__], hrrr_precipitation, ensemble),
                       "reproduce": f"python -m weather_edge.research_hrrr_precipitation --inputs {frozen}"},
    }, details)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--gefs-inputs", type=Path, nargs="+")
    parser.add_argument("--inputs", type=Path)
    args = parser.parse_args()
    if not args.inputs and not args.gefs_inputs:
        parser.error("Provide frozen GEFS inputs or the complete saved paired inputs")
    print(run(args.data, args.gefs_inputs, args.inputs))


if __name__ == "__main__":
    main()
