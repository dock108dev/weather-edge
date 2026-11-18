"""Compare radar and gauge evidence for measurable-rain probabilities."""

from __future__ import annotations

import argparse
import gzip
import json
import math
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

from sklearn.feature_extraction import DictVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from . import expansion_contract, expansion_mrms, expansion_weather, research_model
from . import research_model_rain, research_model_rain_update
from .nbm import climate_window, iso, utc

VERSION = "rain-radar-signal-v1"
PRODUCT = "RadarOnly_QPE_03H"
REGION = "rain-study"
MM_PER_HUNDREDTH_INCH = 0.254
STAGES = ("calibration", "selection", "november_2026")
METHODS = {
    "pre_day": "Pre-day NOAA PoP",
    "base": "Logistic correction with gauge evidence",
    "radar": "Identical correction plus radar accumulation",
}


def _wanted(sets):
    jobs = set()
    for rows in sets.values():
        for row in rows:
            start, _ = climate_window(row["date"], expansion_weather.STATIONS[row["station"]][3])
            decision = utc(row["at"])
            valid = start + timedelta(hours=3)
            while valid < decision:
                jobs.add(valid)
                valid += timedelta(hours=3)
    return sorted(jobs)


def _acquire(data, times):
    frames, gaps = {}, []
    def frame(valid):
        try:
            return valid, expansion_mrms.get_frame(data, PRODUCT, valid, REGION), None
        except (OSError, KeyError, TypeError, ValueError) as error:
            return valid, None, expansion_contract.availability_error(error)

    with ThreadPoolExecutor(max_workers=2) as pool:
        for count, (time, result, gap) in enumerate(pool.map(frame, times), 1):
            if result:
                frames[time] = result
            else:
                gaps.append({"valid_at": iso(time), "error": gap})
            if count % 50 == 0:
                print(f"MRMS 3-hour station analysis {count}/{len(times)} frames checked", flush=True)
    return frames, gaps


def _match(rows, frames):
    matched = []
    for row in rows:
        start, _ = climate_window(row["date"], expansion_weather.STATIONS[row["station"]][3])
        decision = utc(row["at"])
        cursor = start + timedelta(hours=3)
        periods = []
        while cursor < decision:
            frame = frames.get(cursor)
            if not frame or utc(frame["available_at"]) > decision:
                break
            value = frame["points"][row["station"]]["amount_mm"]
            if value is None or utc(frame["accumulation_starts_at"]) != cursor - timedelta(hours=3):
                break
            periods.append({**frame, "station_amount_mm": value})
            cursor += timedelta(hours=3)
        if periods:
            matched.append({**row, "radar_periods": periods,
                            "radar_amount_mm": sum(period["station_amount_mm"] for period in periods)})
    return matched


def _features(row, radar):
    raw = research_model_rain._features({"forecast": row["pre_day"]}, True)
    remaining = research_model_rain._features({"forecast": row["remaining"]}, True)
    features = {"station": row["station"], "p06_pre_day_logit": raw[0], "q06_pre_day": raw[1],
                "p06_remaining_logit": remaining[0], "q06_remaining": remaining[1], "clock": row["hour"],
                "gauge_confirmed": float(bool(row["gauge_witness"]))}
    if radar:
        features["radar_precip_log"] = math.log1p(row["radar_amount_mm"] / MM_PER_HUNDREDTH_INCH)
    return features


def _predict(fit_rows, test, use_radar):
    estimates = make_pipeline(DictVectorizer(sparse=False), StandardScaler(), LogisticRegression(C=1, random_state=20261115))
    estimates.fit([_features(row, use_radar) for row in fit_rows], [row["truth"] for row in fit_rows])
    return [float(p) for _, p in estimates.predict_proba([_features(row, use_radar) for row in test])]


def _scores(rows, group):
    n = {"calendar_dates": len({row["date"] for row in rows}),
         "station_days": len({(row["station"], row["date"]) for row in rows}), "snapshots": len(rows)}
    return [{"method": method, "label": label, "group": group, **n,
             "brier": statistics.mean((row["truth"] - row["probabilities"][method]) ** 2 for row in rows)}
            for method, label in METHODS.items()] if rows else []


def study(data, input_file):
    data = Path(data)
    inputs = json.loads(gzip.decompress(Path(input_file).read_bytes()))
    evidence = {stage: inputs["rows"][stage] for stage in STAGES}
    frames, gaps = _acquire(data, _wanted(evidence))
    matched = {stage: _match(rows, frames) for stage, rows in evidence.items()}
    earlier = matched["calibration"]
    metrics, cases, reports = [], [], {}
    for stage in STAGES[1:]:
        rows = matched[stage]
        base, radar = (_predict(earlier, rows, candidate) for candidate in (False, True))
        recs = [{**row, "probabilities": {"pre_day": row["pre_day"]["p06_overlap_probability"], "base": old, "radar": new}}
                for row, old, new in zip(rows, base, radar, strict=True)]
        reports[stage] = recs
        metrics.extend(_scores(recs, stage))
        for station in ("KNYC", "KMDW"):
            metrics.extend(_scores([row for row in recs if row["station"] == station], f"{stage} · {station}"))
        radar_positive = [row for row in recs if row["radar_amount_mm"] >= MM_PER_HUNDREDTH_INCH]
        false_station_days = {(row["station"], row["date"]) for row in radar_positive if row["truth"] == 0}
        gauge_positive = [row for row in recs if row["gauge_witness"]]
        before_gauge = [row for row in radar_positive if not row["gauge_witness"] and row["truth"]]
        metrics.append({"method": "source_audit", "label": "Radar versus official climate day", "group": stage,
                        "radar_qualifying_station_days": len({(row["station"], row["date"]) for row in radar_positive}),
                        "dry_cli_despite_qualifying_radar": len(false_station_days),
                        "radar_wet_before_confirming_gauge": len({(row["station"], row["date"]) for row in before_gauge}),
                        "confirmed_gauge_station_days": len({(row["station"], row["date"]) for row in gauge_positive})})
        for row in sorted((row for row in radar_positive if row["truth"] == 0),
                          key=lambda row: row["radar_amount_mm"], reverse=True)[:1]:
            cases.append({"title": f"{row['station']} radar estimate compared with CLI", "station": row["station"],
                          "date": row["date"], "at": row["at"],
                          "detail": (f"Known radar accumulation at the adjacent grid cell was {row['radar_amount_mm']:.2f} mm. "
                                     f"The complete station CLI reported {row['official_report']['precip_as_reported']}."),
                          "official_report": row["official_report"], "radar_frame": row["radar_periods"][-1]})
    record = {
        "version": VERSION, "title": "Radar as an early rainfall signal",
        "hypothesis": "NOAA’s published gridded estimate improves measurable station probability beyond an equal earlier model with exact station gauge evidence.",
        "target": "Official Central Park and Midway measurable precipitation",
        "venues": [], "date_window": {stage: inputs["windows"][stage] for stage in STAGES},
        "metrics": metrics, "cases": cases,
        "limits": [
            "Radar aggregates use complete contiguous three-hour periods from the start of each climate day and enter only after the original archive publication time.",
            "The same-case logistic candidates share earlier forecast, station gauge and time information. Only the radar point amount differs.",
            "A radar cell can estimate measurable rain while the climate gauge reports trace or dry. Native radar values are physical context for the station model.",
        ],
        "provenance": {"weather_inputs": research_model._source_entry(data, Path(input_file)),
                       "source_sha256": research_model.run_sources(sys.modules[__name__], expansion_mrms, research_model_rain_update),
                       "radar_source": expansion_mrms.GUIDE},
        "coverage": {stage: {"scored": len(rows), "requested": len(evidence[stage])} for stage, rows in matched.items()},
        "frames": {"received": len(frames), "missing": len(gaps)}, "missing": gaps,
    }
    return research_model.write_record(data, record, reports)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path(".data"))
    parser.add_argument("--from-input", type=Path, required=True)
    args = parser.parse_args()
    print(study(args.data_dir, args.from_input))


if __name__ == "__main__":
    main()
