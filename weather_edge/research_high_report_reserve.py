"""Evaluate the probability reserve used when a six-hour METAR conflicts with a daily high."""
from __future__ import annotations

import argparse
from collections import defaultdict
import gzip
from hashlib import sha256
import json
from pathlib import Path
from statistics import mean
import sys

from . import research_hrrr_intraday, research_model, strategy_metar_high
from .model import _limits, _outcome, _probability

VERSION = "high-temperature-source-reserve-v1"
METHODS = {"hard": "Hard qualified METAR floor", "current": "Same-center source reserve",
           "point": "Point-witness forecast inside reserve", "noaa": "Raw NOAA forecast inside reserve"}


def _center(row, kind):
    remaining = (row["hrrr_remaining_f"] + row["expected_remaining_max_f"]) / 2
    return max(row[kind]["lower_f"], remaining)


def _sources(data, paths):
    variants, receipts = {}, {}
    for path in paths:
        record = json.loads(path.read_bytes())
        provenance = record["provenance"]
        raw = gzip.decompress((data / provenance["frozen_inputs"]).read_bytes())
        if record["version"] != research_hrrr_intraday.VERSION or sha256(raw).hexdigest() != provenance["frozen_input_sha256"]:
            raise ValueError("Unrecognized or altered frozen matched high study")
        heading = record["date_window"]["held"][0]
        variants[heading] = json.loads(raw)
        receipts[heading] = {"id": record["id"], "holdout": record["date_window"]["held"], "digest": sha256(raw).hexdigest()}
    return {"version": VERSION, "variants": variants, "sources": receipts}


def _score(prior, held, payouts):
    reserve = strategy_metar_high.disagreement_reserve(prior)
    weight = reserve["upper_one_sided_wilson"]
    errors = defaultdict(list)
    fields = {"full": "all_observations_bound", "point": "observed_point_bound"}
    for row in prior:
        for name, field in fields.items():
            errors[row["station"], row["local_hour"], name].append(row["actual_f"] - _center(row, field))
    result = defaultdict(list)
    for row in held:
        station, hour = row["station"], row["local_hour"]
        distributions = {name: [_center(row, field) + err for err in errors[station, hour, name]] for name, field in fields.items()}
        if len(distributions["full"]) < 10:
            continue
        groups = {"official": row["buckets"], **{venue: samples[f"{station} {row['at']}"] for venue, samples in payouts.items()
                                                if f"{station} {row['at']}" in samples}}
        observed = row["all_observations_bound"]["lower_f"]
        for group, buckets in groups.items():
            targets = [b["payout"] if group != "official" else _outcome(b, row["actual_f"]) for b in buckets]
            vectors = defaultdict(list)
            for bucket in buckets:
                bounds = _limits(bucket)
                hard = strategy_metar_high.guarded_probability(bounds, distributions["full"], .5, observed, reserve=0)
                fallback = {"current": _probability(bounds, distributions["full"], .5),
                            "point": _probability(bounds, distributions["point"], .5),
                            "noaa": _probability(bounds, [row["raw_f"]], row["raw_sigma"])}
                vectors["hard"].append(hard)
                for name, baseline in fallback.items():
                    vectors[name].append((1 - weight) * hard + weight * baseline)
            estimates = {name: {"probabilities": probs, "brier": mean((p - y) ** 2 for p, y in zip(probs, targets, strict=True))}
                         for name, probs in vectors.items()}
            result[group].append({**row, "buckets": buckets, "targets": targets, "estimates": estimates,
                                  "conflict": observed > row["actual_f"], "reserve": weight})
    return dict(result), reserve


def _example(venue, row):
    i, bucket = next((i, value) for i, value in enumerate(row["buckets"]) if value["payout"] == 1)
    mass = {name: entry["probabilities"][i] for name, entry in row["estimates"].items()}
    return {"station": row["station"], "date": row["date"], "at": row["at"], "venue": venue,
            "instrument_id": bucket["instrument_id"], "title": f"{row['station']} reported high differs from its METAR maximum",
            "detail": f"The available METAR maximum was {row['all_observations_bound']['lower_f']}°F, the point witness was "
                      f"{row['observed_point_bound']['lower_f']}°F, and the completed CLI high was {row['actual_f']:.0f}°F. "
                      f"The winning interval received {mass['current']:.3%} with the shared reserve and {mass['point']:.3%} with the point alternative.",
            "official_report": row["official_report"], "data": {"witness": row["all_observations_bound"], "point": row["observed_point_bound"],
                                                                  "payout": bucket, "reserve": row["reserve"], "probability": mass}}


def run(data, paths, frozen):
    if frozen is None:
        inputs = _sources(data, paths)
        frozen, digest = research_model.freeze_inputs(data, inputs)
    else:
        payload = gzip.decompress(frozen.read_bytes())
        inputs, digest = json.loads(payload), sha256(payload).hexdigest()
    if inputs["version"] != VERSION or len(inputs["variants"]) < 2:
        raise ValueError("Two independent archived regimes are needed")
    metrics, paired, cases, coverage, records = [], [], [], {}, {}
    for first, study in sorted(inputs["variants"].items()):
        groups, reserve = _score(study["calibration"], study["test"], study["payouts"])
        records[first] = groups
        coverage[first] = {"earlier_reserve": reserve, "official_conflicts": sum(row["conflict"] for row in groups["official"]),
                           "conflict_station_days": len({(row["station"], row["date"]) for row in groups["official"] if row["conflict"]})}
        for venue, rows in groups.items():
            group = f"{first} · {venue}"
            metrics.extend({"group": group, "method": name, "label": label, "snapshots": len(rows),
                            "station_days": len({(r["station"], r["date"]) for r in rows}),
                            "calendar_dates": len({r["date"] for r in rows}), "bucket_brier": mean(r["estimates"][name]["brier"] for r in rows)}
                           for name, label in METHODS.items())
            paired.extend({"group": group, **research_model.date_bootstrap(rows, "current", method, "brier")}
                          for method in ("hard", "point", "noaa"))
            if venue != "official" and (conflicts := [r for r in rows if r["conflict"]]):
                cases.append(_example(venue, max(conflicts, key=lambda r: r["all_observations_bound"]["lower_f"] - r["actual_f"])))
    return research_model.write_record(data, {"version": VERSION, "title": "Daily highs when a METAR maximum and climate report disagree",
              "hypothesis": "When a daily high may differ from a six-hour station report, a reserve conditioned on the point-temperature witness may assign better probability to the exact payout.",
              "target": "Exact daily official high temperature and separately verified native payout on matched buckets.",
              "venues": ["kalshi", "polymarket-us"], "date_window": {k: v["holdout"] for k, v in inputs["sources"].items()},
              "metrics": metrics, "comparisons": paired, "coverage": coverage, "cases": cases,
              "limits": ["Each reserve is fixed on earlier independent calendar dates. The weighted portion keeps the full observation bound and equal HRRR/NBM remainder.",
                         "The source contradiction group is selected to illustrate exceptions. It does not establish why the daily climate report and original ASOS input differ.",
                         "Prediction probabilities were tested without a matched price or a simulated execution claim."],
              "provenance": {"frozen_inputs": str(frozen.relative_to(data)), "frozen_input_sha256": digest,
                             "source_studies": inputs["sources"], "code": research_model.run_sources(sys.modules[__name__], strategy_metar_high)}}, records)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data", type=Path, default=Path(".data"))
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--source", type=Path, nargs=2)
    group.add_argument("--frozen", type=Path)
    args = p.parse_args()
    print(run(args.data, args.source, args.frozen))


if __name__ == "__main__":
    main()
