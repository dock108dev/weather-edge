"""Pin and apply an earlier-trained daily-low model to local receipt evidence."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import math
import re
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import joblib

from . import expansion_contract, expansion_nbm, expansion_weather, model_evidence, research_model
from .history import _check_bucket_partition, _write
from .market import _bucket_bounds
from .model import MIN_BAND_DAYS, MIN_TAIL_SUPPORT_DAYS, _limits, _probability
from .nbm import climate_window, iso, utc

SCHEMA = 1
VERSION = "daily-low-shadow-v1"
RUNTIME = Path("research/runtime/daily-low")


def freeze(data, input_path=None):
    """Fit only fit/calibration rows and choose the method on earlier selection dates."""
    data = Path(data)
    if input_path is None:
        inputs = research_model.make_inputs(data)
        input_path, inputs_digest = research_model.freeze_inputs(data, inputs)
    else:
        content = gzip.decompress(input_path.read_bytes())
        inputs = json.loads(content)
        inputs_digest = hashlib.sha256(content).hexdigest()
    by_stage, missing = research_model.snapshots(inputs, splits=("fit", "calibration", "selection"))
    fitted = research_model.fit(by_stage["fit"], by_stage["calibration"])
    selection = research_model.predictions(fitted, by_stage["selection"])
    method = min(research_model.metrics(selection), key=lambda row: row["bucket_brier"])["method"]
    widths = fitted["widths"][method]
    calibration = defaultdict(list)
    means = research_model.forecast_means(fitted, by_stage["calibration"])[method]
    for row, mean in zip(by_stage["calibration"], means, strict=True):
        calibration[(row["station"], row["hour"])].append((row["date"], row["actual_f"] - mean))
    cutoff = max(utc(row["label"]["published_at"]) for rows in by_stage.values() for row in rows)
    source = Path(__file__).with_name("research_model.py")
    artifact = {
        "schema": SCHEMA, "version": VERSION, "method": method, "widths": widths,
        "stations": tuple(research_model.STATIONS), "evidence_cutoff": iso(cutoff),
        "model": fitted["models"].get(method), "bias": fitted["bias"],
        "calibration": dict(calibration), "windows": {p: inputs["windows"][p] for p in by_stage},
        "input_sha256": inputs_digest, "input_name": input_path.name, "fit_source_sha256": research_model._digest(source),
        "runtime_source_sha256": research_model._digest(Path(__file__)),
        "split_coverage": {stage: research_model._independent_counts(rows) for stage, rows in by_stage.items()},
        "missing_train_evidence": missing,
    }
    blob = io.BytesIO()
    joblib.dump(artifact, blob)
    content = blob.getvalue()
    vintage = hashlib.sha256(content).hexdigest()
    root = data / RUNTIME
    saved = root / f"{vintage}.joblib"
    if not saved.exists():
        _write(saved, content)
    _write(root / "active.json", research_model._json_bytes({
        "vintage": vintage, "version": VERSION, "method": method, "evidence_cutoff": iso(cutoff),
        "input_sha256": inputs_digest, "path": str(saved.relative_to(data)), "coverage": artifact["split_coverage"],
    }))
    model_evidence.published(data, "daily_low", vintage, saved)
    return saved


def load(data, *, vintage=None):
    """Open an immutable hashed artifact from the trusted local research archive."""
    data = Path(data)
    root = data / RUNTIME
    if vintage is None:
        pointer = root / "active.json"
        if not pointer.is_file():
            return None
        vintage = json.loads(pointer.read_text())["vintage"]
    if not re.fullmatch(r"[a-f0-9]{64}", vintage or ""):
        raise ValueError("Invalid daily-low model vintage")
    path = root / f"{vintage}.joblib"
    if not path.is_file():
        return None
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != vintage:
        raise ValueError("Daily-low model digest does not match its vintage")
    artifact = joblib.load(io.BytesIO(content))
    if artifact["schema"] != SCHEMA or artifact["version"] != VERSION:
        raise ValueError("Unknown daily-low shadow artifact version")
    return {**artifact, "vintage": vintage, "data": data}


def _validated_buckets(event, day, station):
    expected_event = f"{expansion_contract.LOW_EVENTS[station]}-{day}"
    buckets = []
    for row in event.get("buckets", []):
        ident = row.get("id", "")
        if not isinstance(ident, str) or not ident.startswith(f"tc-{expected_event}-"):
            raise ValueError("Unexpected daily-low instrument identity")
        match = expansion_contract.LOW_RULE.search(row.get("rules") or row.get("description") or "")
        if not match or match[1].upper() != station or match[2] != str(day):
            raise ValueError("Daily-low instrument rule has no matching station, date and climate report")
        lo, hi = _bucket_bounds(match[3])
        buckets.append({"id": ident, "lower_f": lo, "upper_f": hi})
    buckets.sort(key=lambda row: row["lower_f"] if row["lower_f"] is not None else -math.inf)
    if len({row["id"] for row in buckets}) != len(buckets):
        raise ValueError("Duplicate daily-low instruments")
    _check_bucket_partition(buckets)
    return buckets


def _runtime_forecasts(artifact, event, station, day, at):
    start, end = climate_window(day, expansion_weather.STATIONS[station][3])
    estimates = []
    for row in event.get("forecasts", []):
        if row.get("low_f") is None or row.get("low_availability_basis") != "local_receipt":
            continue
        issued = utc(row["low_issued_at"])
        available = utc(row["low_available_at"])
        valid = utc(row["low_valid_at"])
        if not start <= valid < end or valid.hour != 12 or not issued <= available <= at:
            continue
        estimates.append({"issued_at": iso(issued), "available_at": iso(available), "valid_at": iso(valid),
                          "mean_f": row["low_f"], "sd_f": row.get("low_sd_f"), "source": row["low_source"],
                          "availability_basis": "local_receipt",
                          "lead_hours": (valid - issued) / timedelta(hours=1)})
    # Previously fetched public cards already have a real local receipt. Use it,
    # since their source archive posting is a different historical clock.
    data = artifact["data"]
    for day_offset in (-1, 0):
        for hour in research_model.NBS_CYCLES:
            issue = datetime.combine(day + timedelta(days=day_offset), time(hour), timezone.utc)
            card = expansion_nbm.load_card(data, station, issue)
            if not card:
                continue
            received = utc(card["received_at"])
            if not issue <= utc(card["available_at"]) <= received <= at:
                continue
            if forecast := research_model._forecast(card, day, expansion_weather.STATIONS[station][3]):
                estimates.append({**forecast, "source_published_at": forecast["available_at"],
                                  "available_at": iso(received), "availability_basis": "local_receipt"})
    return research_model.available_forecast(estimates, at)


def _runtime_observations(event, start, at):
    by_time = {}
    for row in event.get("observations", []):
        if row.get("timing_proxy") or row.get("source") not in {"NWS", "Aviation Weather Center"}:
            continue
        timestamp, receipt = utc(row["at"]), utc(row["available_at"])
        value = expansion_weather.parse_number(row.get("temp_f"))
        if value is None or not start <= timestamp <= receipt <= at:
            continue
        priority = (row["source"] == "NWS", receipt)
        if timestamp not in by_time or priority >= by_time[timestamp][0]:
            by_time[timestamp] = priority, row
    return [by_time[stamp][1] for stamp in sorted(by_time)]


def _wilson(successes, count):
    """90% Wilson interval for same-station clock calibration event support."""
    if not count:
        return 0.0, 1.0
    z = 1.6448536269514722
    rate = successes / count
    scale = 1 + z ** 2 / count
    center = rate + z ** 2 / (2 * count)
    width = z * math.sqrt((rate * (1 - rate) + z ** 2 / (4 * count)) / count)
    return (center - width) / scale, (center + width) / scale


def predict(artifact, event, as_of):
    """Return a shadow probability from the latest trained local-standard clock."""
    if artifact is None or event.get("family") != "daily-low":
        return None
    station = event.get("station")
    if station not in artifact["stations"]:
        return None
    try:
        day = date.fromisoformat(event["date"])
        at = utc(as_of)
        start, end = climate_window(day, expansion_weather.STATIONS[station][3])
        if not start - timedelta(hours=3) <= at < end:
            return None
        clocks = [hour for (place, hour) in artifact["widths"] if place == station and start + timedelta(hours=hour) <= at]
        if not clocks:
            return None
        hour = max(clocks)
        decision = start + timedelta(hours=hour)
        if decision < utc(artifact["evidence_cutoff"]):
            return None
        forecasts = _runtime_forecasts(artifact, event, station, day, decision)
        if forecasts is None:
            return None
        buckets = _validated_buckets(event, day, station)
        readings = _runtime_observations(event, start, decision)
    except (OSError, TypeError, KeyError, ValueError, AttributeError):
        return None
    if hour >= 3 and not readings:
        return None
    observed_min = min((row["temp_f"] for row in readings), default=None)
    latest = readings[-1] if readings else None
    point = {
        "station": station, "at": iso(decision), "hour": hour, "forecast": forecasts,
        "observed_min_f": observed_min, "latest_observation": latest,
        "dewpoint_f": latest.get("dewpoint_f") if latest else None,
    }
    method = artifact["method"]
    mean = forecasts["mean_f"]
    if method == "bias":
        mean += artifact["bias"][station, hour]
    elif artifact["model"] is not None:
        mean += float(artifact["model"].predict([research_model.features(point, method)])[0])
    width = artifact["widths"][station, hour]
    native_sd = forecasts.get("sd_f")
    if method == "noaa" and native_sd is not None and native_sd > 0:
        width = native_sd
    examples = artifact["calibration"].get((station, hour), [])
    modeled = []
    for bucket in buckets:
        bounds = lo, hi = _limits(bucket)
        p = _probability(bounds, [mean], width)
        successes = sum(lo <= mean + error < hi for _, error in examples)
        lower, upper = _wilson(successes, len(examples))
        failures = len(examples) - successes
        modeled.append({"id": bucket["id"], "probability": p, "no_probability": 1 - p,
                        "probability_low": min(p, lower), "probability_high": max(p, upper),
                        "yes_support": successes, "no_support": failures, "validation_days": len(examples),
                        "uncertain": len(examples) < MIN_BAND_DAYS or min(successes, failures) < MIN_TAIL_SUPPORT_DAYS})
    return {
        "method": method, "version": VERSION, "vintage": artifact["vintage"],
        "mode": "shadow", "target": "daily-low", "station": station, "date": str(day),
        "at": iso(at), "knowledge_at": iso(decision), "local_standard_decision_hour": hour,
        "issued_at": forecasts["issued_at"], "available_at": forecasts["available_at"], "valid_at": forecasts["valid_at"],
        "forecast_source": forecasts["source"], "model_low_f": mean, "observed_min_f": observed_min,
        "latest_observation": latest, "forecast": forecasts, "buckets": modeled,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=Path(".data"), type=Path)
    parser.add_argument("--from-input", type=Path)
    args = parser.parse_args()
    print(freeze(args.data_dir, args.from_input))


if __name__ == "__main__":
    main()
