"""Measure the cost of a one-degree allowance above a METAR low bound."""

from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path
from statistics import mean
import sys

from . import expansion_live_precision, research_model, research_model_low_maintenance as maintenance
from . import research_model_low_cross_season as seasons, research_model_low_precision as precision
from .model import _outcome

VERSION = "daily-low-one-degree-allowance-v1"
METHODS = {"original": "Original exact-station bound", "plus_one": "Allow one additional whole degree",
           "marked_plus_one": "Allow one additional degree for a maintenance-marked report"}


def _allowance(marked):
    return {"original": 0, "plus_one": 1, "marked_plus_one": int(marked)}


def _model_cases(rows, native, widths, reserve):
    weather, payouts, anchors = [], [], {}
    for row in rows:
        station, day, hour, at = (row[field] for field in ("station", "date", "hour", "at"))
        common = {"station": station, "date": day, "at": at, "marked_bound": row["flagged_bound"]}
        witness = row["metar_upper_bound"]
        base = witness["upper_f"]
        candidates = {method: base + allowance for method, allowance in _allowance(row["flagged_bound"]).items()}
        width = widths["precision_center", station, hour]
        reserve_weight = reserve[station, hour]["uncapped_weight"]
        center = precision._precision_mean(row)
        buckets = anchors.setdefault((station, day), research_model._benchmark_buckets(round(row["means"]["txn"])))
        truth = [_outcome(bucket, row["actual_f"]) for bucket in buckets]
        estimates = {}
        for method, bound in candidates.items():
            probabilities = precision._bucket_probabilities(buckets, center, width, bound, reserve_weight)
            estimates[method] = {"brier": mean((p - y) ** 2 for p, y in zip(probabilities, truth, strict=True)),
                                 "climate_above_allowed_bound": row["actual_f"] > bound}
        weather.append({**common, "estimates": estimates})
        for outcome in native.get(f"{station}/{day}/{hour}", []):
            bucket, native_yes = outcome["bucket"], outcome["native_yes"]
            predictions = {}
            for method, bound in candidates.items():
                p, = precision._bucket_probabilities([bucket], center, width, bound, reserve_weight)
                side = expansion_live_precision.metar_implied_side(bucket, bound)
                predictions[method] = {"brier": (p - native_yes) ** 2, "probability": p, "bound_f": bound,
                                       "implied_side": side, "wrong_implied_side": side is not None and int(side == "YES") != native_yes}
            payouts.append({**common, "estimates": predictions, "native": outcome, "metar": witness,
                            "official_report": row["official_report"]})
    return weather, payouts


def _scored(rows, group, *, native=False):
    for method, label in METHODS.items():
        readings = [row["estimates"][method] for row in rows]
        metrics = {"bucket_brier": mean(row["brier"] for row in readings)}
        if native:
            metrics.update(
                implied_sides=sum(row["implied_side"] is not None for row in readings),
                wrong_implied_sides=sum(row["wrong_implied_side"] for row in readings),
                original_implied_sides_lost=sum(a["estimates"]["original"]["implied_side"] is not None and b["implied_side"] is None
                                               for a, b in zip(rows, readings, strict=True)),
            )
        else:
            metrics["climate_observations_above_allowed_bound"] = sum(row["climate_above_allowed_bound"] for row in readings)
        yield {"group": group, "method": method, "label": label, **research_model._independent_counts(rows), **metrics}


def _native_case(row, phase):
    native = row["native"]
    market = native["market"]
    raw, derived = (row["estimates"][method] for method in ("original", "marked_plus_one"))
    return {"station": row["station"], "date": row["date"], "at": row["at"], "venue": "kalshi",
            "instrument_id": market["instrument_id"], "title": f"{row['station']} · One degree removes a conditional side",
            "detail": (f"The original {raw['bound_f']}°F read implies {raw['implied_side']} conditionally. With {derived['bound_f']}°F allowed, "
                       f"the full compatible range gives no one side. Complete climate low {row['official_report']['low_f']}°F, "
                       f"native YES payout ${native['native_yes']}."), "cohort": phase, "evidence": row}


def evaluate(data, inputs):
    if inputs["version"] != VERSION:
        raise ValueError("Unexpected daily-low allowance input")
    frozen, digest = research_model.freeze_inputs(data, inputs)
    prior = inputs["maintenance"]
    widths, reserve = seasons._fit_shared(prior["earlier"])
    metrics, comparisons, details, cases = [], [], {}, []
    audit = prior["events"]
    for method, label in METHODS.items():
        failures = 0
        for row in audit:
            witness = row["terminal_compatible_bound"]
            if witness:
                padding = _allowance(maintenance.maintenance_indicated(witness))[method]
                failures += row["official_last"]["low_f"] > witness["upper_f"] + padding
        metrics.append({"group": "Diagnostic sample · close-time station evidence", "label": label, "method": method,
                        **research_model._independent_counts(audit), "official_lows_above_allowed_bound": failures})

    for phase in seasons.SEASONS:
        sample = prior["cohorts"][phase]
        weather, payouts = _model_cases(sample["rows"], sample["native"], widths, reserve)
        for rows, group, priced in ((weather, phase + " · exact same weather cases", False),
                                    (payouts, phase + " · verified native payout strikes", True)):
            if rows:
                metrics.extend(_scored(rows, group, native=priced))
                comparisons.extend({"group": group, **research_model.date_bootstrap(rows, "original", method, "brier")}
                                   for method in ("plus_one", "marked_plus_one"))
        lost = [row for row in payouts if row["estimates"]["original"]["implied_side"] and
                not row["estimates"]["marked_plus_one"]["implied_side"]]
        if lost:
            witness = max(lost, key=lambda row: abs(row["estimates"]["original"]["probability"] -
                                                   row["estimates"]["marked_plus_one"]["probability"]))
            cases.append(_native_case(witness, phase))
        details[phase] = {"weather": weather, "native": payouts}

    record = {"version": VERSION, "title": "How much native low evidence is lost by allowing one more degree?",
              "hypothesis": "A one-degree allowance can cover the observed climate-report discrepancy without sacrificing the useful probability information in the original low bound.",
              "target": "Exact original station bounds, forecast probabilities and separately verified native payout thresholds",
              "venues": ["kalshi"], "date_window": {phase: list(map(str, window)) for phase, window in seasons.SEASONS.items()},
              "metrics": metrics, "contrasts": comparisons, "cases": cases, "limits": [
                  "The one-degree discrepancy motivated the fixed sensitivity. Covering that same diagnostic example is not independent test evidence.",
                  "All methods keep the earlier fitted forecast mean, spread and disagreement reserve. Only the truncation point and conditional side change.",
                  "The maintenance indicator does not identify a measuring component. The audit does not estimate a distinct rate for an individual instrument.",
                  "Conditional sides require the governed low to stay within the stated upper value. Payouts score the same independently verified paid strikes.",
              ], "provenance": {"frozen_input": str(frozen.relative_to(data)), "frozen_input_sha256": digest,
                                "maintenance_screen": inputs["reference"], "noaa_codes": maintenance.CODE_DOC,
                                "source_sha256": research_model.run_sources(sys.modules[__name__], maintenance, seasons,
                                                                            precision, expansion_live_precision)}}
    return research_model.write_record(data, record, details)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--maintenance-study", type=Path)
    source.add_argument("--input", type=Path)
    args = parser.parse_args()
    if args.input:
        inputs = json.loads(gzip.decompress(args.input.read_bytes()))
    else:
        sample, reference = maintenance._load(args.data, args.maintenance_study, maintenance.VERSION)
        inputs = {"version": VERSION, "maintenance": sample, "reference": reference}
    print(evaluate(args.data, inputs))


if __name__ == "__main__":
    main()
