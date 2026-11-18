"""Test observed surface shortwave irradiance after the cloud-mask comparison."""

from __future__ import annotations

import argparse
from calendar import isleap
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import gzip
import json
from math import cos, pi, radians, sin
from pathlib import Path
from statistics import mean
import sys

from sklearn import __version__ as sklearn_version

from . import expansion_satellite, research_model, research_model_satellite
from .nbm import utc

VERSION = "goes-surface-shortwave-high-v1"
SOLAR_EQUATIONS = "https://gml.noaa.gov/grad/solcalc/solareqns.PDF"
METHODS = {"noaa": "NOAA TXN + XND", "bias": "Earlier station/time bias", "ridge": "Ridge with forecast sky and sun angle",
           "ridge_satellite": "Ridge + GOES cloud mask", "ridge_solar": "Ridge + GOES surface shortwave retrieval",
           "ridge_both": "Ridge + both GOES products"}


def solar_cosine(when, latitude, longitude):
    at = utc(when)
    hour = at.hour + at.minute / 60 + at.second / 3600
    gamma = 2 * pi / (366 if isleap(at.year) else 365) * (at.timetuple().tm_yday - 1 + (hour - 12) / 24)
    equation = 229.18 * (0.000075 + 0.001868 * cos(gamma) - 0.032077 * sin(gamma)
                         - 0.014615 * cos(2 * gamma) - 0.040849 * sin(2 * gamma))
    declination = (0.006918 - 0.399912 * cos(gamma) + 0.070257 * sin(gamma) - 0.006758 * cos(2 * gamma)
                   + 0.000907 * sin(2 * gamma) - 0.002697 * cos(3 * gamma) + 0.00148 * sin(3 * gamma))
    solar_minutes = (hour * 60 + equation + 4 * longitude) % 1440
    hour_angle = radians(solar_minutes / 4 - 180)
    lat = radians(latitude)
    return sin(lat) * sin(declination) + cos(lat) * cos(declination) * cos(hour_angle)


def _gather(data, source):
    content = json.loads(gzip.decompress(source.read_bytes()))
    if content["version"] != research_model_satellite.VERSION:
        raise ValueError("Expected frozen GOES cloud research inputs")
    dates = sorted({row["date"] for rows in content["rows"].values() for row in rows})
    gaps = Counter()

    def retrieve(day):
        try:
            return expansion_satellite.latest(data, day + "T18:00:00Z", product=expansion_satellite.IRRADIANCE)
        except (ValueError, OSError, KeyError):
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        frames = dict(zip(dates, pool.map(retrieve, dates), strict=True))
    groups = {phase: [] for phase in content["rows"]}
    for phase, rows in content["rows"].items():
        for row in rows:
            frame = frames[row["date"]]
            point = frame["points"].get(row["station"]) if frame else None
            if not point or point["center_w_m2"] is None or point["window_good_pixels"] < 20 or utc(frame["available_at"]) > utc(row["at"]):
                gaps[f"{phase}: missing eligible good-quality irradiance"] += 1
                continue
            cosine = solar_cosine(frame["valid_at"], point["lat"], point["lon"])
            if cosine <= 0:
                gaps[f"{phase}: sun below horizon"] += 1
                continue
            keys = ("source", "product", "satellite", "valid_at", "available_at", "received_at", "scan_starts_at",
                    "projection", "raw_path", "body_sha256")
            groups[phase].append({**row, "solar": {key: frame[key] for key in keys}, "irradiance": point,
                                  "solar_zenith_cosine": cosine, "angle_adjusted_irradiance": point["window_mean_w_m2"] / cosine})
    return {"version": VERSION, "windows": content["windows"], "rows": groups, "gaps": dict(gaps),
            "source": research_model._source_entry(data, source)}


def _features(row, method):
    source = "ridge_satellite" if method in ("ridge_satellite", "ridge_both") else "ridge"
    values = research_model_satellite._features(row, source)
    values["solar_zenith_cosine"] = row["solar_zenith_cosine"]
    if method in ("ridge_solar", "ridge_both"):
        values["angle_adjusted_surface_irradiance_w_m2"] = row["angle_adjusted_irradiance"]
    return values


def _case(data, row):
    original = research_model_satellite._case(data, row)
    sky = row["satellite"]["point"]["window_cloud_mask_fraction"]
    flux = row["irradiance"]["window_mean_w_m2"]
    before, after = (row["estimates"][method]["mean_f"] for method in ("ridge", "ridge_solar"))
    return {**original, "title": f"{row['station']} · Observed shortwave and the official high", "solar": row["solar"],
            "irradiance": row["irradiance"], "detail": (f"The eligible native retrieval gave {flux:.1f} W/m² across nearby quality-filtered pixels. "
                f"The cloud mask classified {sky:.0%} as cloudy or probably cloudy. Ridge {before:.1f}°F, "
                f"irradiance arm {after:.1f}°F, verified CLI {row['actual_f']}°F.")}


def evaluate(data, inputs):
    frozen, digest = research_model.freeze_inputs(data, inputs)
    rows = inputs["rows"]
    if not all(rows.values()):
        raise ValueError("Incomplete irradiance fit, calibration, selection, or holdout")
    model = research_model.fit(rows["fit"], rows["calibration"], methods=METHODS, feature_fn=_features)
    earlier = research_model.predictions(model, rows["selection"])
    test = research_model.predictions(model, rows["test"])
    metrics = research_model.metrics(test, "Same-case held-out station-days", METHODS)
    metrics.extend(research_model.metrics(earlier, "Earlier selection station-days", METHODS))
    ages = [(utc(row["solar"]["available_at"]) - utc(row["solar"]["valid_at"])).total_seconds() for row in test]
    decision_ages = [(utc(row["at"]) - utc(row["solar"]["valid_at"])).total_seconds() for row in test]
    metrics.append({"method": "irradiance_coverage", "label": "Native DSRF footprints and clocks", "group": "Held-out retrievals",
                    "native_scans": len({row["solar"]["body_sha256"] for row in test}),
                    "mean_publication_lag_seconds": mean(ages), "mean_decision_age_minutes": mean(decision_ages) / 60})
    for station in sorted({row["station"] for row in test}):
        metrics.extend(research_model.metrics([row for row in test if row["station"] == station], station, METHODS))
    contrasts = [research_model.date_bootstrap(test, base, added, "brier") for base, added in (
        ("ridge", "ridge_satellite"), ("ridge", "ridge_solar"), ("ridge_satellite", "ridge_both"))]
    by_error = sorted(test, key=lambda row: abs(row["estimates"]["ridge"]["error_f"])
                      - abs(row["estimates"]["ridge_solar"]["error_f"]), reverse=True)
    chosen = [by_error[0], by_error[-1]]
    for station in ("KNYC", "KPHL"):
        event = next((row for row in test if row["station"] == station and row["date"] == "2026-11-12"), None)
        if event and event not in chosen:
            chosen.append(event)
    record = {"version": VERSION, "title": "Does observed surface sunlight improve the daily high?",
              "hypothesis": "Surface shortwave irradiance is closer to the daytime heating mechanism than cloud classification alone. "
                            "Adding the retrieval to an otherwise matched model may reduce bucket errors.",
              "target": "Verified exact-station daily maximum temperature", "venues": [], "date_window": inputs["windows"],
              "coverage": {phase: research_model._independent_counts(phase_rows) for phase, phase_rows in rows.items()},
              "metrics": metrics, "contrasts": contrasts, "cases": [_case(data, event) for event in chosen], "missing": inputs["gaps"],
              "limits": ["Each date uses the latest native Full Disk scan published before 18:00Z. The fixed selection clock precedes every station decision.",
                         "Instantaneous surface shortwave is a satellite retrieval including direct and diffuse flux. The model divides by the solar zenith cosine to reduce geometry effects.",
                         "The cloud and irradiance scans can be different ages. Each clock and original local archive receipt is preserved with its own source.",
                         "Synthetic common buckets score weather predictions. Any native market link is case evidence."],
              "provenance": {"frozen_input": str(frozen.relative_to(data)), "frozen_input_sha256": digest,
                             "irradiance_docs": expansion_satellite.IRRADIANCE_DOCS, "solar_geometry": SOLAR_EQUATIONS,
                             "scikit_learn": sklearn_version,
                             "source_sha256": research_model.run_sources(sys.modules[__name__], expansion_satellite, research_model_satellite)}}
    return research_model.write_record(data, record, {"selection": earlier, "test": test})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    sources = parser.add_mutually_exclusive_group(required=True)
    sources.add_argument("--cloud-input", type=Path)
    sources.add_argument("--input", type=Path)
    args = parser.parse_args()
    corpus = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else _gather(args.data, args.cloud_input)
    print(evaluate(args.data, corpus))


if __name__ == "__main__":
    main()
