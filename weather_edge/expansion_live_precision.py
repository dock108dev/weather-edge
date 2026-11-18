"""Apply a checksum-pinned physical daily-low shadow to exact local receipts."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from . import expansion_live_context, expansion_live_low, expansion_metar, expansion_weather
from . import model_evidence, research_model, research_model_low_precision
from .history import _write
from .model import MIN_BAND_DAYS, MIN_TAIL_SUPPORT_DAYS, _limits
from .nbm import climate_window, iso, utc

VERSION = "daily-low-precision-shadow-v1"
RUNTIME = Path("research/runtime/daily-low-precision")


def freeze(data, input_path):
    data = Path(data)
    content = gzip.decompress(Path(input_path).read_bytes())
    inputs = json.loads(content)
    if inputs["version"] != research_model_low_precision.VERSION:
        raise ValueError("Expected frozen precision-calibration inputs")
    calibration = inputs["rows"]["calibration"]
    selection = inputs["rows"]["selection"]
    widths, exceptions = research_model_low_precision._calibrate(calibration)
    by_group = defaultdict(list)
    for row in calibration:
        by_group[row["station"], row["hour"]].append(
            (row["date"], row["actual_f"] - research_model_low_precision._precision_mean(row))
        )
    groups = [{"station": station, "hour": hour, "width_f": widths["precision_center", station, hour],
               "residuals": residuals, "exceptions": exceptions.get((station, hour))}
              for (station, hour), residuals in sorted(by_group.items())]
    cutoff = max(utc(row["official_report"]["published_at"]) for row in [*calibration, *selection])
    evidence = {
        "version": VERSION, "model": "guarded_cap", "evidence_cutoff": iso(cutoff), "groups": groups,
        "calibration_window": inputs["windows"]["calibration"], "selection_window": inputs["windows"]["selection"],
        "input_sha256": hashlib.sha256(content).hexdigest(),
        "source_sha256": research_model.run_sources(research_model_low_precision, expansion_metar, expansion_live_context),
        "runtime_source_sha256": research_model._digest(Path(__file__)),
    }
    raw = research_model._json_bytes(evidence)
    vintage = hashlib.sha256(raw).hexdigest()
    root = data / RUNTIME
    saved = root / f"{vintage}.json"
    if not saved.exists():
        _write(saved, raw)
    _write(root / "active.json", research_model._json_bytes({"vintage": vintage, "path": str(saved.relative_to(data))}))
    model_evidence.published(data, "daily_low_precision", vintage, saved)
    return saved


def load(data, *, vintage=None):
    data = Path(data)
    directory = data / RUNTIME
    if vintage is None:
        pointer = directory / "active.json"
        if not pointer.exists():
            return None
        vintage = json.loads(pointer.read_text())["vintage"]
    if not isinstance(vintage, str) or len(vintage) != 64 or any(letter not in "0123456789abcdef" for letter in vintage):
        raise ValueError("Invalid daily-low precision model vintage")
    path = directory / f"{vintage}.json"
    if not path.exists():
        return None
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != vintage:
        raise ValueError("Daily-low precision model digest does not match its vintage")
    artifact = json.loads(raw)
    if artifact["version"] != VERSION:
        raise ValueError("Unknown low precision forecast artifact version")
    return {**artifact, "vintage": vintage, "data": data}


def metar_implied_side(bucket, upper_f):
    """Return the side that would win across all whole-degree lows through this reading."""
    if upper_f is None:
        return None
    lower, upper = bucket["lower_f"], bucket["upper_f"]
    if lower is None and (upper is None or upper >= upper_f):
        return "YES"
    if lower is not None and lower > upper_f:
        return "NO"
    return None


def _bucket_estimates(buckets, center, upper_f, config):
    width = config["width_f"]
    weight = config["exceptions"]["uncapped_weight"]
    probabilities = research_model_low_precision._bucket_probabilities(buckets, center, width, upper_f, weight)
    counts = len(config["residuals"])
    modeled = []
    for bucket, p in zip(buckets, probabilities, strict=True):
        lo, hi = _limits(bucket)
        yes = sum(lo <= center + residual < hi for _, residual in config["residuals"])
        no = counts - yes
        pl, ph = expansion_live_low._wilson(yes, counts)
        modeled.append({"id": bucket["id"], "probability": p, "no_probability": 1 - p,
                        "probability_low": min(p, pl), "probability_high": max(p, ph),
                        "yes_support": yes, "no_support": no, "validation_days": counts,
                        "metar_implied_side": metar_implied_side(bucket, upper_f),
                        "uncertain": counts < MIN_BAND_DAYS or min(yes, no) < MIN_TAIL_SUPPORT_DAYS})
    return modeled


def predict(artifact, event, as_of):
    """Shadow odds for the most recent qualified trained decision time."""
    if artifact is None or event.get("family") != "daily-low":
        return None
    station = event.get("station")
    if station not in research_model.STATIONS:
        return None
    try:
        day = date.fromisoformat(event["date"])
        now = utc(as_of)
        start, end = climate_window(day, expansion_weather.STATIONS[station][3])
        if not start <= now < end:
            return None
        groups = [row for row in artifact["groups"] if row["station"] == station and row["exceptions"] is not None
                  and start + timedelta(hours=row["hour"]) <= now]
        if not groups:
            return None
        config = max(groups, key=lambda row: row["hour"])
        decision = start + timedelta(hours=config["hour"])
        if decision < utc(artifact["evidence_cutoff"]):
            return None
        physics = expansion_live_context.physical_context(artifact["data"], event, decision)
        rows = expansion_live_low._runtime_observations(event, start, decision)
        witness = expansion_metar.upper_bound(rows, station, start, decision)
        if physics is None or witness is None:
            return None
        buckets = expansion_live_low._validated_buckets(event, day, station)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    center = min(witness["upper_f"], physics["remaining_low_f"])
    width = config["width_f"]
    modeled = _bucket_estimates(buckets, center, witness["upper_f"], config)
    forecast = {**physics["forecast"], "valid_at": physics["forecast"]["covered_to"],
                "mean_f": center, "sd_f": width}
    return {
        "version": VERSION, "vintage": artifact["vintage"], "method": "guarded_cap", "mode": "shadow",
        "target": "daily-low", "station": station, "date": str(day), "at": iso(now), "knowledge_at": iso(decision),
        "local_standard_decision_hour": config["hour"], "issued_at": forecast["issued_at"], "valid_at": forecast["valid_at"],
        "available_at": forecast["available_at"], "forecast": forecast, "forecast_source": forecast["source"],
        "model_low_f": center, "observed_min_f": physics["observed_low_f"], "metar_upper_bound_f": witness["upper_f"],
        "latest_observation": rows[-1], "metar_evidence": witness, "remaining_context": physics,
        "report_exception_allowance": config["exceptions"], "buckets": modeled,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path(".data"))
    parser.add_argument("--from-input", type=Path, required=True)
    args = parser.parse_args()
    print(freeze(args.data_dir, args.from_input))


if __name__ == "__main__":
    main()
