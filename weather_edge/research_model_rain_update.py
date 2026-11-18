"""Test as-of measurable rain updates using contained ASOS accumulation reports."""

from __future__ import annotations

import argparse
import gzip
import json
import re
import statistics
import sys
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

import numpy as np
from sklearn.feature_extraction import DictVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from . import expansion_contract, expansion_live_low, expansion_metar, expansion_weather, research_model
from . import research_model_low_full_day, research_model_low_precision, research_model_rain
from .nbm import climate_window, iso, utc

VERSION = "precipitation-gauge-update-v1"
WINDOWS = {"fit": research_model_low_full_day.WINDOWS["fit"], **research_model_low_precision.WINDOWS}
METHODS = {
    "pre_day": "Earlier NBM full-climate-day PoP",
    "remaining_only": "Most recent NBM remaining-window PoP",
    "gauge_guard": "Earlier PoP + contained measured accumulation",
    "learned": "Learned remaining guidance and observed gauge update",
}
ACCUMULATION = re.compile(r"(?<!\S)6(\d{4}|////)(?=\s|$)")


def gauge_witness(rows, station, opens, at):
    """Find the first known positive native report fully contained in this climate day."""
    qualifying = []
    for row in rows:
        raw = (row.get("raw_report") or row.get("report") or row.get("raw") or "").strip().rstrip("=")
        identity = expansion_metar.REPORT_START.match(raw)
        if not identity or identity[1] != station:
            continue
        stamp, receipt = utc(row["at"]), utc(row["available_at"])
        if not opens <= stamp <= receipt <= at or stamp.strftime("%d%H%M") != identity[2]:
            continue
        remarks = raw.partition(" RMK ")[2]
        field = ACCUMULATION.search(remarks)
        if (not field or field[1] in ("////", "0000") or "PNO" in remarks.split()):
            continue
        nominal = (stamp + timedelta(minutes=30)).replace(minute=0, second=0, microsecond=0)
        if nominal.hour % 3 or abs(nominal - stamp) > timedelta(minutes=15):
            continue
        hours = 3 if nominal.hour % 6 else 6
        start = min(stamp, nominal) - timedelta(hours=hours)
        end = max(stamp, nominal)
        if opens <= start <= end <= at:
            qualifying.append({"amount_in": int(field[1]) / 100, "report_at": iso(stamp), "available_at": iso(receipt),
                               "period_starts_at": iso(start), "period_ends_at": iso(end), "duration_hours": hours,
                               "native_code": field[0], "raw_report": raw})
    return min(qualifying, key=lambda row: (max(row["available_at"], row["period_ends_at"]), row["report_at"]), default=None)


def _remaining(cards, at, close):
    candidates = sorted((card for card in cards if utc(card["issued_at"]) <= utc(card["available_at"]) <= at
                         and at - utc(card["issued_at"]) <= timedelta(hours=36)), key=lambda row: row["issued_at"], reverse=True)
    return next((value for card in candidates if (value := research_model_rain.estimate_rain_window(card, at, close))), None)


def _case(data, station, day, reports, candidates, official):
    start, end = climate_window(day, expansion_weather.STATIONS[station][3])
    initial = research_model_rain._weather(data, day, station)
    truth = expansion_contract.rain_outcome(official["precip_as_reported"])
    if not initial or truth is None:
        return []
    results = []
    for hour in research_model.HOURS[1:]:
        at = start + timedelta(hours=hour)
        remaining = _remaining(candidates, at, end)
        if not remaining:
            continue
        observed = expansion_weather.observations_as_of(reports, start, at)
        witness = gauge_witness(observed, station, start, at)
        results.append({"date": str(day), "station": station, "at": iso(at), "hour": hour,
                        "truth": truth, "official_report": official, "gauge_witness": witness,
                        "pre_day": initial["forecast"], "remaining": remaining,
                        "seen_station_reports": len(observed)})
    return results


def make_inputs(data):
    rows, references, missing = {}, {}, []
    for stage, (first, last) in WINDOWS.items():
        rows[stage], references[stage] = [], {}
        for station in research_model.STATIONS:
            labels, reports, issued, source = research_model_low_full_day._station_inputs(data, station, first, last)
            references[stage][station] = source
            for day in research_model._days(first, last):
                known = labels.get(str(day))
                official = known if stage == "fit" else research_model_low_precision._scoped_labels(data, station, day, known)
                if not official:
                    missing.append({"stage": stage, "station": station, "date": str(day), "reason": "No complete station report"})
                    continue
                cards = [card for shift in (-1, 0, 1) for card in issued[day + timedelta(days=shift)]]
                evidence = _case(data, station, day, reports[day], cards, official)
                if len(evidence) != len(research_model.HOURS) - 1:
                    missing.append({"stage": stage, "station": station, "date": str(day),
                                    "decision_count": len(evidence), "reason": "Forecast unavailable at one or more decision times"})
                rows[stage].extend(evidence)
    return {"version": VERSION, "rows": rows, "source_files": references, "missing": missing,
            "windows": {name: [str(begin), str(end)] for name, (begin, end) in WINDOWS.items()}}


def _features(row):
    first = research_model_rain._features({"forecast": row["pre_day"]}, True)
    rest = research_model_rain._features({"forecast": row["remaining"]}, True)
    return {"station": row["station"], "hour": row["hour"], "preliminary_pop": first[0], "initial_qpf": first[1],
            "remaining_pop": rest[0], "remaining_qpf": rest[1],
            "gauge_confirmed": float(bool(row["gauge_witness"])), "readings": row["seen_station_reports"]}


def _evaluate(all_rows):
    training, earlier = all_rows["fit"], all_rows["calibration"]
    pipeline = make_pipeline(DictVectorizer(sparse=False), StandardScaler(), LogisticRegression(C=1, random_state=20261115))
    pipeline.fit([_features(row) for row in training], [row["truth"] for row in training])
    known_station_days = {(row["station"], row["date"]): row["truth"] for row in earlier if row["gauge_witness"]}
    count = len(known_station_days)
    failures = sum(1 - outcome for outcome in known_station_days.values())
    reserve = expansion_live_low._wilson(failures, count)[1]
    scores = {}
    for phase, rows in all_rows.items():
        if phase in ("fit", "calibration"):
            continue
        scores[phase] = []
        estimates = pipeline.predict_proba([_features(row) for row in rows]) if rows else []
        for row, (_, p) in zip(rows, estimates, strict=True):
            before = row["pre_day"]["p06_overlap_probability"]
            remaining = row["remaining"]["p06_overlap_probability"]
            guarded = (1 - reserve) + reserve * before if row["gauge_witness"] else before
            scores[phase].append({**row, "probabilities": {"pre_day": before, "remaining_only": remaining,
                                                           "gauge_guard": guarded, "learned": float(p)}})
    return scores, {"prior_confirmed_station_days": count, "prior_contradictions": failures, "uncapped_error_allowance": reserve}


def _metrics(rows, group):
    return [{"method": method, "label": name, "group": group,
             "calendar_dates": len({row["date"] for row in rows}),
             "station_days": len({(row["station"], row["date"]) for row in rows}),
             "snapshots": len(rows), "brier": statistics.mean((row["truth"] - row["probabilities"][method]) ** 2 for row in rows)}
            for method, name in METHODS.items()] if rows else []


def _contrast(rows, method):
    daily = defaultdict(list)
    for row in rows:
        truth = row["truth"]
        daily[row["date"]].append((row["probabilities"][method] - truth) ** 2 - (row["probabilities"]["pre_day"] - truth) ** 2)
    values = np.array([statistics.mean(items) for items in daily.values()])
    sample = np.random.default_rng(20261115).choice(values, size=(2500, len(values))).mean(axis=1)
    return {"method": method, "calendar_dates": len(values), "delta_brier": float(values.mean()),
            "interval_95": [float(bound) for bound in np.quantile(sample, [0.025, 0.975])]}


def _cases(results):
    result = []
    for phase, rows in results.items():
        if phase == "selection" or not rows:
            continue
        evidence = [row for row in rows if row["gauge_witness"]]
        if evidence:
            selected = min(evidence, key=lambda row: row["pre_day"]["p06_overlap_probability"])
            witness = selected["gauge_witness"]
            detail = (f"Earlier forecast {selected['probabilities']['pre_day']:.1%}. "
                      f"Raw {witness['native_code']} reported {witness['amount_in']:.2f} inches. "
                      f"Whole-day CLI printed {selected['official_report']['precip_as_reported']}.")
            result.append({"title": f"{selected['station']} contained precipitation reading", "detail": detail,
                           "station": selected["station"], "date": selected["date"], "at": selected["at"],
                           "official_report": selected["official_report"], "observation": witness,
                           "model_evidence": selected["probabilities"]})
    return result


def study(data, from_input=None):
    data = Path(data)
    inputs = json.loads(gzip.decompress(from_input.read_bytes())) if from_input else make_inputs(data)
    frozen, digest = research_model.freeze_inputs(data, inputs)
    by_phase, calibration = _evaluate(inputs["rows"])
    metrics, diagnostics = [], {}
    for stage, rows in by_phase.items():
        seen = [row for row in rows if row["gauge_witness"]]
        contradicted = {(row["station"], row["date"]) for row in seen if row["truth"] != 1}
        active = {(row["station"], row["date"]) for row in seen}
        metrics.extend(_metrics(rows, stage))
        metrics.extend(_metrics(seen, f"{stage} · early recognized wet cases"))
        diagnostics[stage] = {"confirmed_station_days": len(active), "contradicting_climates": len(contradicted),
                              "affirmative_reading_decisions": len(seen),
                              "contrasts": [_contrast(rows, method) for method in ("remaining_only", "gauge_guard", "learned")]}
    selected = min((row for row in metrics if row["group"] == "selection"), key=lambda row: row["brier"])["method"]
    record = {
        "version": VERSION, "title": "Rain updates from an exact station gauge",
        "hypothesis": "Contained station accumulation and updated remaining-day guidance improve measurable precipitation odds after the climate day begins.",
        "target": "Official liquid-equivalent precipitation of ≥0.01 inches at Central Park and Midway",
        "venues": [], "date_window": inputs["windows"], "metrics": metrics, "cases": _cases(by_phase),
        "limits": [
            "Forecasts use the latest published six-hour quantities whose overlapping intervals cover all remaining climate hours.",
            "The raw METAR 3-/6-hour QPE is limited to report-time and nominal-hour windows entirely inside the exact station climate day. Trace and missing gauge groups supply separate evidence.",
            "Archived station reports enter with a documented ten-minute clock assumption. Gauge evidence is tested against original complete CLI reports.",
            "The guarded gauge update reserves probability for source or reporting revisions. A common date is shared by both station cases.",
        ],
        "provenance": {"input_path": str(frozen.relative_to(data)), "input_sha256": digest,
                       "early_score_selection": selected, "fit_sources": research_model.run_sources(
                           sys.modules[__name__], expansion_metar, research_model_rain),
                       "definition": expansion_metar.ASOS_USER_GUIDE},
        "diagnostics": diagnostics, "calibration": calibration, "missing": inputs["missing"],
    }
    return research_model.write_record(data, record, {"scored": by_phase})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path(".data"))
    parser.add_argument("--from-input", type=Path)
    args = parser.parse_args()
    print(study(args.data_dir, args.from_input))


if __name__ == "__main__":
    main()
