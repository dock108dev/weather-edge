"""Compare wave forecast leads and coastline sampling against the same NDBC labels."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
import gzip
from hashlib import sha256
import json
from pathlib import Path
from statistics import mean
import sys

from . import archive, grids, research_gefs, research_model, research_waves, waves
from .market import iso, parse_time

VERSION = "noaa-wave-lead-coast-v1"
FIRST, SPLIT, FINISH = date(2026, 10, 5), date(2026, 10, 27), date(2026, 11, 14)
LEADS = (6, 24, 48)
THRESHOLD = 2.0
METHODS = {"six": "Native 6-hour lead", "day": "Native 24-hour lead", "two_days": "Native 48-hour lead",
           "bilinear": "6-hour complete wet-cell interpolation", "wet_only": "6-hour wet-weight interpolation"}


def field(data, target, lead):
    issue = target - timedelta(hours=lead)
    path = grids.frame_path(data / "waves", issue, lead)
    frame = waves._cached(path)
    if frame is None or not issue <= parse_time(frame["available_at"]) <= target:
        return None
    if parse_time(frame["issued_at"]) != issue or parse_time(frame["valid_at"]) != target:
        raise ValueError("GFS Wave forecast clocks disagree with the match")
    payload = (data / frame["raw"]).read_bytes()
    if sha256(payload).hexdigest() != frame["sha256"]:
        raise ValueError("GFS Wave archive was changed")
    grid = waves.native_grid(payload, issue, lead)
    return grid, {k: frame[k] for k in ("id", "issued_at", "available_at", "valid_at", "ingested_at", "source", "raw", "sha256")}


def inputs(data):
    locations = research_waves._locations(data)
    obs, receipts = {}, {}
    for station in locations:
        obs[station], receipts[station] = research_waves._get_observations(data, station)
    days, rows, gaps, manifests = archive.date_range(FIRST, FINISH), [], Counter(), {}
    for day in days:
        target = datetime.combine(day, time(18), timezone.utc)
        products = [field(data, target, lead) for lead in LEADS]
        if any(result is None for result in products):
            gaps["incomplete as-issued lead family"] += 1
            continue
        manifests[iso(target)] = {name: result[1] for name, result in zip(tuple(METHODS)[:3], products, strict=True)}
        for station, (lat, lon) in locations.items():
            observed = min((at for at in obs[station] if abs(at - target) <= timedelta(minutes=20)),
                           key=lambda at: (abs(at - target), at), default=None)
            if observed is None:
                gaps["no measured wave height near valid time"] += 1
                continue
            coastal = waves.interpolation_corners(products[0][0], lat, lon)
            wet = [item for item in coastal if item["height_m"] is not None]
            estimates = {name: waves.sample_grid(grid, lat, lon)[0] for name, (grid, _) in zip(tuple(METHODS)[:3], products, strict=True)}
            estimates["wet_only"] = (sum(row["weight"] * row["height_m"] for row in wet) / sum(row["weight"] for row in wet)) if wet else None
            estimates["bilinear"] = estimates["wet_only"] if len(wet) == len(coastal) else None
            rows.append({"station": station, "date": str(day), "target": iso(target), "observation_at": iso(observed),
                         "region": research_waves.BUOYS[station], "lat": lat, "lon": lon, "actual_m": obs[station][observed],
                         "estimates": estimates, "native_corners": coastal})
    return {"version": VERSION, "rows": rows, "frames": manifests, "buoys": receipts, "gaps": dict(gaps)}


def _scores(rows, fit, group, methods):
    output, scores = [], {}
    for method in methods:
        residuals = [r["actual_m"] - r["estimates"][method] for r in fit if r["estimates"][method] is not None]
        errors = [r["estimates"][method] - r["actual_m"] for r in rows]
        forecasts = [sum(r["estimates"][method] + err >= THRESHOLD for err in residuals) / len(residuals) for r in rows]
        dates = {r["date"] for r in rows}
        scores[method] = errors
        output.append({"group": group, "method": method, "label": METHODS[method], "calendar_dates": len(dates),
                       "buoy_days": len({(r["station"], r["date"]) for r in rows}), "observations": len(rows),
                       "n_buoys": len({r["station"] for r in rows}), "mae_m": mean(map(abs, errors)), "bias_m": mean(errors),
                       "two_meter_brier": mean((p - (r["actual_m"] >= THRESHOLD)) ** 2 for p, r in zip(forecasts, rows, strict=True)),
                       "earlier_residuals": len(residuals)})
    comparisons = []
    for method in methods[1:]:
        errors = defaultdict(list)
        for row, point, reference in zip(rows, scores[method], scores[methods[0]], strict=True):
            errors[row["date"]].append(abs(point) - abs(reference))
        comparisons.append({"group": group, "baseline": METHODS[methods[0]], "candidate": METHODS[method],
                            "paired_mae_difference": research_gefs._mean_interval(errors)})
    return output, comparisons


def run(data, frozen):
    if frozen is None:
        corpus = inputs(data)
        frozen, digest = research_model.freeze_inputs(data, corpus)
    else:
        raw = gzip.decompress(frozen.read_bytes())
        corpus, digest = json.loads(raw), sha256(raw).hexdigest()
    calibration = [r for r in corpus["rows"] if r["date"] < str(SPLIT)]
    held = [r for r in corpus["rows"] if r["date"] >= str(SPLIT)]
    if corpus["version"] != VERSION or not calibration or not held:
        raise ValueError("The wave comparison needs original matched forecasts in both calendar windows")
    metrics, paired, cases = [], [], []
    for name, methods in (("Forecast leads", tuple(METHODS)[:3]), ("Wet interpolation", ("six", "wet_only")),
                          ("All required neighbors wet", ("six", "bilinear"))):
        rows = [r for r in held if all(r["estimates"][key] is not None for key in methods)]
        if rows:
            scores, contrasts = _scores(rows, calibration, name, methods)
            metrics.extend(scores)
            paired.extend(contrasts)
    recovered = [r for r in held if r["estimates"]["six"] is None and r["estimates"]["wet_only"] is not None]
    aligned = [r for r in held if r["estimates"]["six"] is not None and r["estimates"]["two_days"] is not None]
    if aligned:
        largest = max(aligned, key=lambda r: abs(r["estimates"]["six"] - r["estimates"]["two_days"]))
        cases.append({"station": largest["station"], "title": f"Buoy {largest['station']} at a common forecast valid time",
                      "date": largest["date"], "at": largest["target"], "globe_station": "KNYC", "layers": "waves",
                      "probe_lat": largest["lat"], "probe_lon": largest["lon"],
                      "probe_buoy": largest["station"], "wave_frame": corpus["frames"][largest["target"]]["six"]["id"],
                      "wave_leads": [{"label": f"{hour}-hour forecast on globe", "frame": corpus["frames"][largest["target"]][name]["id"]}
                                     for name, hour in (("day", 24), ("two_days", 48))],
                      "detail": f"The buoy measured {largest['actual_m']:.2f} m. The 48-hour native forecast was "
                                f"{largest['estimates']['two_days']:.2f} m and the 6-hour forecast was {largest['estimates']['six']:.2f} m.",
                      "data": {**largest, "forecast": corpus["frames"][largest["target"]], "measurement": corpus["buoys"][largest["station"]]}})
    if recovered:
        best = min(recovered, key=lambda r: abs(r["actual_m"] - r["estimates"]["wet_only"]))
        cases.append({"station": best["station"], "title": f"Buoy {best['station']} at a modeled shoreline",
                      "date": best["date"], "at": best["target"], "globe_station": "KNYC", "layers": "waves",
                      "probe_lat": best["lat"], "probe_lon": best["lon"],
                      "probe_buoy": best["station"], "wave_frame": corpus["frames"][best["target"]]["six"]["id"],
                      "detail": f"The buoy measured {best['actual_m']:.2f} m. The nearest native forecast cell was dry. "
                                f"Wet-only interpolation estimated {best['estimates']['wet_only']:.2f} m from nearby ocean cells.",
                      "data": {**best, "forecast": corpus["frames"][best["target"]], "measurement": corpus["buoys"][best["station"]]}})
    return research_model.write_record(data, {"version": VERSION, "title": "Coastal wave forecasts at six, 24 and 48 hours",
              "hypothesis": "Shorter native forecast leads help across matched buoy reports, and wet-neighbor sampling may recover coastal holes.",
              "target": "NDBC significant wave height and the probability of reaching 2 m", "venues": [],
              "date_window": {"calibration": [str(FIRST), str(SPLIT - timedelta(days=1))], "held": [str(SPLIT), str(FINISH)]},
              "metrics": metrics, "comparisons": paired, "cases": cases,
              "coverage": {"source_gaps": corpus["gaps"], "held_buoy_days": len(held), "native_wet_recoveries": len(recovered),
                           "recovered_only_mae_m": mean(abs(r["actual_m"] - r["estimates"]["wet_only"]) for r in recovered) if recovered else None},
              "limits": ["Matched comparisons use the same valid clocks and buoy measurements, within 20 minutes of the original forecast valid time.",
                         "Two-meter probabilities use earlier pooled forecast errors. Coastal interpolation can mix different marine conditions across barriers. The raw nearest cell remains labeled independently."],
              "provenance": {"frozen_inputs": str(frozen.relative_to(data)), "frozen_input_sha256": digest,
                             "source_sha256": research_model.run_sources(sys.modules[__name__], waves, research_waves)}},
              {"held": held, "forecast_receipts": corpus["frames"], "buoy_receipts": corpus["buoys"]})


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data", type=Path, default=Path(".data"))
    p.add_argument("--frozen", type=Path)
    args = p.parse_args()
    print(run(args.data, args.frozen))


if __name__ == "__main__":
    main()
