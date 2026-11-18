"""Compare wave lead errors conditional on an earlier native wind/swell forecast."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import timedelta
import gzip
from hashlib import sha256
import json
from math import sqrt
from pathlib import Path
from statistics import mean
import sys

from . import research_model, research_wave_leads, wave_components, waves

VERSION = "wave-wind-sea-swell-v1"
LABELS = {"wind": "Wind sea at least as high as primary swell",
          "swell": "Primary swell higher than wind sea"}


def _dominance(cells):
    wind, swell = cells["wind_sea_m"], cells["primary_swell_m"]
    if wind is None or swell is None:
        return None
    return "wind" if wind >= swell else "swell"


def _freeze(data, source):
    raw = gzip.decompress(source.read_bytes())
    parent = json.loads(raw)
    if parent["version"] != research_wave_leads.VERSION:
        raise ValueError("This comparison requires the frozen native wave lead cohort")
    manifest, rows, gaps = {}, [], Counter()
    for row in parent["rows"]:
        target = row["target"]
        forecasts = parent["frames"][target]
        if target not in manifest:
            manifest[target] = {}
            for method in ("two_days", "six"):
                height = waves._cached(data / "waves" / (forecasts[method]["id"] + ".json"))
                if height and (meta := wave_components.metadata(data, height)):
                    manifest[target][method] = meta
        pieces = {method: {name: waves.sample_grid(field, row["lat"], row["lon"])[0]
                           for name, field in wave_components.fields(data, meta).items()}
                  for method, meta in manifest[target].items()}
        if "two_days" not in pieces:
            gaps["No exact 48-hour component file"] += 1
            continue
        group = _dominance(pieces["two_days"])
        if group is None:
            gaps["48-hour wind-sea or primary-swell cell was masked"] += 1
        rows.append({**row, "dominance": group, "components": pieces})
    return {"version": VERSION, "parent_sha256": sha256(raw).hexdigest(), "rows": rows, "buoys": parent["buoys"],
            "height_forecasts": parent["frames"], "component_forecasts": manifest, "gaps": dict(gaps)}


def _closure(rows):
    errors = []
    comparable = [row for row in rows if row["estimates"]["six"] is not None and "six" in row["components"]]
    for row in comparable:
        fields = row["components"]["six"]
        if all(value is not None for value in fields.values()):
            errors.append(sqrt(sum(value ** 2 for value in fields.values())) - row["estimates"]["six"])
    return {"samples_with_height": len(comparable), "all_four_components": len(errors),
            "mean_derived_minus_native_m": mean(errors) if errors else None,
            "absolute_difference_m": mean(map(abs, errors)) if errors else None}


def _example(row, corpus):
    at = row["target"]
    lane = row["components"]["six"]
    component_text = ", ".join(f"{name.removesuffix('_m').replace('_', ' ')} {lane[name]:.2f} m"
                               for name in wave_components.COMPONENTS if lane[name] is not None)
    return {"station": row["station"], "date": row["date"], "at": at,
            "title": f"Buoy {row['station']} and original wave components",
            "globe_station": "KNYC", "layers": "waves", "probe_lat": row["lat"], "probe_lon": row["lon"],
            "probe_buoy": row["station"], "wave_frame": corpus["height_forecasts"][at]["six"]["id"],
            "wave_leads": [{"label": "48-hour forecast and components on globe",
                            "frame": corpus["height_forecasts"][at]["two_days"]["id"]}],
            "detail": (f"The buoy reported {row['actual_m']:.2f} m. The original 6-hour total was "
                       f"{row['estimates']['six']:.2f} m, with {component_text} at the same native cell."),
            "data": {"matched": row, "forecast": corpus["component_forecasts"][at],
                     "measurement_receipts": corpus["buoys"][row["station"]]}}


def run(data, *, source=None, frozen=None):
    if frozen is None:
        corpus = _freeze(data, source)
        frozen, digest = research_model.freeze_inputs(data, corpus)
    else:
        raw = gzip.decompress(frozen.read_bytes())
        corpus, digest = json.loads(raw), sha256(raw).hexdigest()
    if corpus["version"] != VERSION:
        raise ValueError("Unexpected native wave component input version")
    split = str(research_wave_leads.SPLIT)
    methods = ("six", "day", "two_days")
    matched = [row for row in corpus["rows"] if all(row["estimates"][method] is not None for method in methods)]
    fit, held = ([row for row in matched if row["date"] < split],
                 [row for row in matched if row["date"] >= split])
    if not fit or not held:
        raise ValueError("Wave component comparison has no matching earlier and later dates")
    metrics, comparisons, cases = [], [], []
    for key, label in LABELS.items():
        earlier, later = ([row for row in values if row["dominance"] == key] for values in (fit, held))
        if not earlier or not later:
            continue
        scores, paired = research_wave_leads._scores(later, earlier, label, methods)
        metrics.extend(scores)
        comparisons.extend(paired)
        visible = [row for row in later if "six" in row["components"]]
        if visible:
            cases.append(_example(max(visible, key=lambda row: row["actual_m"]), corpus))
    dates = research_wave_leads
    return research_model.write_record(data, {
        "version": VERSION, "title": "GFS Wave forecast leads by wind sea and swell",
        "hypothesis": "An earlier native wind-sea and primary-swell forecast identifies regimes with different wave-height lead errors.",
        "target": "Independent NOAA buoy significant wave height near the native forecast valid hour.",
        "date_window": {"fit": [str(dates.FIRST), str(dates.SPLIT - timedelta(days=1))], "held": [split, str(dates.FINISH)]},
        "venues": [], "metrics": metrics, "comparisons": comparisons, "cases": cases,
        "coverage": {"earlier_matched": len(fit), "later_matched": len(held), "gaps": corpus["gaps"],
                     "full_component_closure_check": _closure(held)},
        "limits": ["The wind/swell group uses the 48-hour run already published before the 6-hour forecast. Each group compares all three leads on the same buoys and dates.",
                   "A masked component stays unknown. The quadrature check includes only cells with all four components and does not reconstruct the displayed NOAA combined total.",
                   "The buoy verification compares significant wave height. It does not infer buoy partition heights from the model."],
        "provenance": {"frozen_inputs": str(frozen.relative_to(data)), "frozen_input_sha256": digest,
                       "parent_sha256": corpus["parent_sha256"], "field_docs": wave_components.DOCS,
                       "run_sources": research_model.run_sources(sys.modules[__name__], waves, wave_components, research_wave_leads)},
    }, corpus)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--source", type=Path)
    inputs.add_argument("--frozen", type=Path)
    args = parser.parse_args()
    print(run(args.data, source=args.source, frozen=args.frozen))


if __name__ == "__main__":
    main()
