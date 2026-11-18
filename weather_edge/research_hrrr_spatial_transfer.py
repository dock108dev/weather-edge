"""Test whether transferable nearby-grid context explains an HRRR direction control."""
from __future__ import annotations

import argparse
import gzip
from hashlib import sha256
import json
from pathlib import Path
import sys

from . import research_hrrr_gradient, research_model
from .model import _benchmark_buckets

VERSION = "hrrr-spatial-gradient-transfer-v1"
METHODS = {**research_hrrr_gradient.METHODS, "background": "Local Ridge + average of both nearby cells",
           "direction": "Local Ridge + signed slope between cells"}


def _features(row, method):
    if method in research_hrrr_gradient.METHODS:
        return research_hrrr_gradient._features(row, method)
    base = research_hrrr_gradient._features(row, "nbm")
    upwind, downwind = (row["thermal_probes"][key]["temp_f"] for key in ("upwind", "downwind"))
    base["spatial_term_f"] = ((upwind + downwind) / 2 - row["hrrr_trace"][0]["temp_f"]
                              if method == "background" else downwind - upwind)
    return base


def _read(path):
    raw = gzip.decompress(path.read_bytes())
    data = json.loads(raw)
    if data["version"] != research_hrrr_gradient.VERSION:
        raise ValueError("Need original projected HRRR thermal gradient inputs")
    return data, sha256(raw).hexdigest()


def run(data, summer, autumn):
    fit, fit_sha = _read(summer)
    test, test_sha = _read(autumn)
    earlier = [*fit["calibration"], *fit["test"]]
    if max(row["date"] for row in earlier) >= min(row["date"] for row in test["calibration"]):
        raise ValueError("Summer fit must end before any autumn decision")
    metrics, comparisons, evidence = [], [], {}
    for key, label in (("calibration", "Independent early autumn transfer"),
                       ("test", "Later autumn follow-up")):
        decisions = [{**row, "buckets": _benchmark_buckets(round(row["raw_f"]))} for row in test[key]
                     if row["official_source_grade"] == "exact raw CLI"]
        cohorts, reserve = research_hrrr_gradient._evaluate({
            "calibration": earlier, "test": decisions, "payouts": test["payouts"],
        }, methods=METHODS, features=_features)
        evidence[label] = cohorts
        for cohort, results in sorted(cohorts.items()):
            group = f"{label} · {cohort}"
            metrics.extend(research_model.metrics(results, group, METHODS))
            comparisons.extend({"group": group, **research_model.date_bootstrap(results, "nbm", method, metric)}
                               for method in METHODS if method != "nbm" for metric in ("brier", "mae"))
    late = evidence["Later autumn follow-up"]
    cases = []
    for venue, rows in late.items():
        if venue == "official":
            continue
        best = max(rows, key=lambda r: abs(r["estimates"]["background"]["brier"] - r["estimates"]["nbm"]["brier"]))
        bucket = next(item for item in best["buckets"] if item["payout"] == 1)
        cases.append({"venue": venue, "station": best["station"], "date": best["date"], "at": best["at"],
                      "instrument_id": bucket["instrument_id"], "title": f"{best['station']} issued forecast and nearby cells",
                      "detail": (f"The local native cell gave {best['hrrr_trace'][0]['temp_f']:.1f}°F. The two cells were "
                                 f"{best['thermal_probes']['upwind']['temp_f']:.1f}°F upwind and "
                                 f"{best['thermal_probes']['downwind']['temp_f']:.1f}°F downwind. "
                                 f"The official daily high was {best['actual_f']:.0f}°F."),
                      "hrrr_probes": research_hrrr_gradient.probes(best),
                      "official_report": best["official_report"], "outcome": bucket})
    return research_model.write_record(data, {
        "version": VERSION, "title": "Can nearby HRRR temperature context transfer across seasons?",
        "hypothesis": "If nearby temperature context is useful independently of advection direction, a balanced spatial term should transfer on new season dates.",
        "target": "Exact NWS daily highs and corresponding verified native payout intervals.",
        "venues": sorted(test["payouts"]), "date_window": {"fit": [fit["source_dates"]["from"], fit["source_dates"]["through"]],
            "independent_transfer": test["source_dates"]["calibration"], "follow_up": test["source_dates"]["held"]},
        "metrics": metrics, "comparisons": comparisons, "cases": cases,
        "limits": ["All five candidates share one input subset, older warm-season fit, physical lower bound and prior residual distribution procedure.",
                   "The independent early autumn dates were used to train the prior autumn-only study but had never been its outcome test. The later autumn results are a follow-up on the earlier experiment's test dates.",
                   "All further features use the same two native 40 km points. The two-cell average is a symmetric along-wind sample and approximates local spatial background."],
        "provenance": {"fit_rows": len(earlier), "fit_frozen_path": str(summer.relative_to(data)), "fit_frozen_sha256": fit_sha,
                       "test_frozen_path": str(autumn.relative_to(data)), "test_frozen_sha256": test_sha,
                       "guard_reserve": reserve, "run_sources": research_model.run_sources(sys.modules[__name__], research_hrrr_gradient)},
    }, evidence)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--summer", type=Path, required=True)
    parser.add_argument("--autumn", type=Path, required=True)
    args = parser.parse_args()
    print(run(args.data, args.summer, args.autumn))


if __name__ == "__main__":
    main()
