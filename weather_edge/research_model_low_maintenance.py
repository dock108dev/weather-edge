"""Evaluate whether the native METAR maintenance marker changes low-bound support."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path
from statistics import mean
import sys

from . import expansion_metar, expansion_weather, research_model, research_model_low_bound_exceptions as all_receipts
from . import research_model_low_cross_season as seasons, research_model_low_precision as precision
from .model import _outcome
from .nbm import climate_window, utc

VERSION = "daily-low-metar-maintenance-screen-v1"
CODE_DOC = "https://www.weather.gov/media/wrh/mesowest/metar_decode_key.pdf"
METHODS = {"native": "Earlier guarded bound from all usable reports", "unflagged": "Same guard with maintenance-marked reports excluded"}


def maintenance_indicated(row):
    """Recognize the native system-level maintenance field, which has no sensor ID."""
    raw = row.get("raw_report") or row.get("raw") or row.get("report") or ""
    return "$" in raw.strip().rstrip("=").split()


def _load(data, record_path, version):
    report = json.loads(record_path.read_bytes())
    if report["version"] != version:
        raise ValueError(f"Expected the original {version} record")
    prior = gzip.decompress((data / report["provenance"]["frozen_input"]).read_bytes())
    if hashlib.sha256(prior).hexdigest() != report["provenance"]["frozen_input_sha256"]:
        raise ValueError("Earlier low evidence checksum disagrees")
    return json.loads(prior), research_model._source_entry(data, record_path)


def _all_rows(data, records):
    results = []
    for row in records:
        station, day = row["station"], row["date"]
        first, last = climate_window(day, expansion_weather.STATIONS[station][3])
        rows, _ = all_receipts._sources(data, station, day)
        if not rows:
            continue
        precise, flagged = [], []
        for item in rows:
            at = utc(item["available_at"])
            if first <= at < last and expansion_metar.metar_low_evidence(item, station, first, at):
                precise.append(item)
                if maintenance_indicated(item):
                    flagged.append(item)
        screened = all_receipts._witnesses([item for item in precise if not maintenance_indicated(item)], station, first, last)
        results.append({**row, "flagged_original_weather_reports": len(flagged), "unflagged_bound": screened["terminal_compatible_bound"]})
    return results


def _at_clocks(data, previous):
    references = {}
    evaluated = []
    for row in previous:
        station, day, at = row["station"], row["date"], utc(row["at"])
        key = station, day
        if key not in references:
            path = data / "expansion" / "replay" / day / f"{station}.json"
            value = json.loads(path.read_bytes()) if path.is_file() else {"observations": []}
            references[key] = [item for item in value["observations"] if item.get("receipt_source") and item.get("raw")]
        start, _ = climate_window(day, expansion_weather.STATIONS[station][3])
        known = expansion_weather.observations_as_of(references[key], start, at, delay_minutes=None)
        ordinary = expansion_metar.upper_bound(known, station, start, at)
        original = row["metar_upper_bound"]
        if not ordinary or ordinary["upper_f"] != original["upper_f"]:
            continue
        screened = expansion_metar.upper_bound([item for item in known if not maintenance_indicated(item)], station, start, at)
        evaluated.append({**row, "flagged_bound": maintenance_indicated(ordinary), "unflagged_bound": screened})
    return evaluated


def gather(data, audit, prior):
    entire, source = _load(data, audit, all_receipts.VERSION)
    earlier, forecast = _load(data, prior, seasons.VERSION)
    pairs = {phase: {"rows": _at_clocks(data, earlier["cohorts"][phase]["weather"]), "native": earlier["cohorts"][phase]["native"]}
             for phase in seasons.SEASONS}
    return {"version": VERSION, "events": _all_rows(data, entire["weather"]), "cohorts": pairs,
            "earlier": earlier["calibration"], "sources": [source, forecast]}


def _model_cases(rows, native, widths, reserve):
    models, payouts, anchors = [], [], {}
    for row in rows:
        case = row["station"], row["date"]
        buckets = anchors.setdefault(case, research_model._benchmark_buckets(round(row["means"]["txn"])))
        if (new := row["unflagged_bound"]) is None:
            continue
        key = row["station"], row["hour"]
        sd, weight = widths["precision_center", *key], reserve[key]["uncapped_weight"]
        raw = row["metar_upper_bound"]["upper_f"]
        candidates = {"native": raw, "unflagged": new["upper_f"]}

        def estimate(bound, buckets):
            point = min(row["means"]["remaining"], bound)
            return precision._bucket_probabilities(buckets, point, sd, bound, weight)

        truth = [_outcome(bucket, row["actual_f"]) for bucket in buckets]
        weather = {arm: mean((p - y) ** 2 for p, y in zip(estimate(bound, buckets), truth, strict=True)) for arm, bound in candidates.items()}
        secondary = {"station": row["station"], "date": row["date"], "at": row["at"], "bound_changed": raw != new["upper_f"]}
        models.append({**secondary, "errors": weather})
        for target in native.get(f"{row['station']}/{row['date']}/{row['hour']}", []):
            outcome = target["native_yes"]
            predicted = {arm: (estimate(bound, [target["bucket"]])[0] - outcome) ** 2 for arm, bound in candidates.items()}
            payouts.append({**secondary, "errors": predicted, "native": target})
    return models, payouts


def _metrics(rows, group):
    return [{"group": group, "method": arm, "label": label, **research_model._independent_counts(rows),
             "bucket_brier": mean(row["errors"][arm] for row in rows)} for arm, label in METHODS.items()] if rows else []


def _source_case(row):
    previous, adjusted = (row[field] for field in ("terminal_compatible_bound", "unflagged_bound"))
    available = f"an unflagged upper read of {adjusted['upper_f']}°F" if adjusted else "no eligible unflagged bound at this station"
    return {"station": row["station"], "date": row["date"], "at": previous["available_at"],
            "title": f"{row['station']} · An original maintenance-marked report",
            "detail": (f"The marked station report gave a {previous['upper_f']}°F bound. Excluding that station's marked measurements left {available}. "
                       f"The complete climate minimum was {row['official_last']['low_f']}°F."), "evidence": row}


def evaluate(data, inputs):
    if inputs["version"] != VERSION:
        raise ValueError("Unexpected METAR maintenance audit input")
    frozen, digest = research_model.freeze_inputs(data, inputs)
    events = inputs["events"]
    filtered = [row for row in events if row["unflagged_bound"]]
    flags = [row for row in events if row["flagged_original_weather_reports"]]
    raw_errors = [row for row in events if row["terminal_compatible_bound"] and row["official_last"]["low_f"] > row["terminal_compatible_bound"]["upper_f"]]
    filtered_errors = [row for row in filtered if row["official_last"]["low_f"] > row["unflagged_bound"]["upper_f"]]
    widths, reserve = seasons._fit_shared(inputs["earlier"])
    metrics, diagnostics, scored = [], {}, {}
    for phase in seasons.SEASONS:
        case = inputs["cohorts"][phase]
        weather, payouts = _model_cases(case["rows"], case["native"], widths, reserve)
        metrics.extend(_metrics(weather, phase + " · identical archived forecast cases"))
        metrics.extend(_metrics(payouts, phase + " · exact same native payout strikes"))
        changed = [row for row in weather if row["bound_changed"]]
        metrics.extend(_metrics(changed, phase + " · physically different bounds after screening"))
        diagnostics[phase] = {"originally_matched_clocks": len(case["rows"]), "same_case_forecast_clocks": len(weather),
                              "clocks_lost_by_filter": len(case["rows"]) - len(weather), "clocks_with_changed_bound": len(changed),
                              "native_same_case_payouts": len(payouts)}
        scored[phase] = {"weather": weather, "native": payouts}
    record = {"version": VERSION, "title": "What does a station maintenance marker do to a low-temperature bound?",
              "hypothesis": "The exact exception at a marked station suggests testing the coverage and probability consequences of screening the native maintenance marker.",
              "target": "Exact whole-degree METAR low bounds and original native threshold outcomes, with and without maintenance-marked reports",
              "venues": ["kalshi"], "date_window": {"original_report_audit": [min(row["date"] for row in events), max(row["date"] for row in events)],
                                                       **{key: list(map(str, dates)) for key, dates in seasons.SEASONS.items()}},
              "coverage": {**research_model._independent_counts(events), "flagged_station_days": len(flags),
                           "flagged_exact_reports": sum(row["flagged_original_weather_reports"] for row in flags),
                           "station_days_with_a_screened_bound": len(filtered), "weather_bound_exceptions_before_screen": len(raw_errors),
                           "weather_bound_exceptions_after_screen": len(filtered_errors), "same_clock_forecast_evidence": diagnostics},
              "metrics": metrics, "cases": [_source_case(row) for row in raw_errors],
              "limits": ["The trailing $ is a system maintenance indicator. It does not identify which measuring element needs maintenance.",
                         "This compares fixed earlier forecast shapes and reserve. Loss of all originally timed unmarked reports is recorded as a gap before scoring the same matched cases.",
                         "The motivating historical exception is the supplied diagnostic example. The later native payout comparisons do not establish a policy for all marked observations."],
              "provenance": {"frozen_input": str(frozen.relative_to(data)), "frozen_input_sha256": digest, "source_records": inputs["sources"],
                             "noaa_codes": CODE_DOC, "source_sha256": research_model.run_sources(sys.modules[__name__], all_receipts, seasons, expansion_metar)}}
    return research_model.write_record(data, record, {"weather_report_flags": events, "forecast_scores": scored})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--report-audit", type=Path)
    parser.add_argument("--season-study", type=Path)
    parser.add_argument("--input", type=Path)
    args = parser.parse_args()
    if args.input is None and (args.report_audit is None or args.season_study is None):
        parser.error("Give both completed bounds reports or the frozen study input")
    corpus = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else gather(args.data, args.report_audit, args.season_study)
    print(evaluate(args.data, corpus))


if __name__ == "__main__":
    main()
