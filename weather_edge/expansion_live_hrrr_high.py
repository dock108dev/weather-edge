"""Evaluate the frozen HRRR and NBM comparison on locally received live inputs."""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import timedelta
import gzip
from hashlib import sha256
import json
from pathlib import Path
import re

from . import hrrr, research_hrrr_intraday, strategy_high_transfer
from .expansion_weather import STATIONS
from .history import _write
from .market import iso, parse_time
from .model import MIN_BAND_DAYS, MIN_TAIL_SUPPORT_DAYS, _limits
from .nbm import climate_window
from .strategy_metar_high import disagreement_reserve, guarded_probability, metar_high_evidence
from .strategy_runs import digest, json_bytes, write_new

VERSION = "daily-high-hrrr-shadow-v1"
ROOT = Path("research/runtime") / VERSION


def freeze(data, source):
    record = json.loads(source.read_bytes())
    meta = record["provenance"]
    original = gzip.decompress((data / meta["frozen_inputs"]).read_bytes())
    if record["version"] != research_hrrr_intraday.VERSION or sha256(original).hexdigest() != meta["frozen_input_sha256"]:
        raise ValueError("Intraday high qualification input checksum mismatch")
    rows = json.loads(original)["calibration"]
    earlier = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for row in rows:
        for name, center in research_hrrr_intraday._centers(row).items():
            earlier[row["station"]][str(row["local_hour"])][name].append(row["actual_f"] - center)
    artifact = {"version": VERSION, "study_id": record["id"], "qualified_at": record["created_at"],
                "fit_dates": record["date_window"]["calibration"], "hours": [13, 17], "residuals": earlier,
                "disagreement_reserve": disagreement_reserve(rows)["upper_one_sided_wilson"],
                "fit_input_sha256": meta["frozen_input_sha256"]}
    vintage = digest(artifact)
    path = data / ROOT / (vintage + ".json")
    write_new(path, json_bytes(artifact) + b"\n")
    _write(data / ROOT / "active.json", json_bytes({"vintage": vintage}))
    return vintage


def load(data, *, vintage=None):
    try:
        if vintage is None:
            vintage = json.loads((data / ROOT / "active.json").read_bytes())["vintage"]
        if not isinstance(vintage, str) or not re.fullmatch(r"[0-9a-f]{64}", vintage):
            raise ValueError("Invalid live HRRR evidence version")
        artifact = json.loads((data / ROOT / f"{vintage}.json").read_bytes())
    except FileNotFoundError:
        return None
    if artifact["version"] != VERSION or digest(artifact) != vintage:
        raise ValueError("Live HRRR high model checksum mismatch")
    return {**artifact, "vintage": vintage, "data": Path(data)}


def timeline(artifact, event, through):
    if (artifact is None or event.get("family", "daily-high") != "daily-high" or not event.get("buckets")
            or event.get("venue", "polymarket-us") != "polymarket-us"):
        return []
    station = event["station"]
    lane = artifact["residuals"].get(station)
    if not lane:
        return []
    through = parse_time(through)
    start, close = climate_window(event["date"], STATIONS[station][3])
    if (not event.get("climate_starts_at") or not event.get("climate_ends_at")
            or (start, close) != (parse_time(event["climate_starts_at"]), parse_time(event["climate_ends_at"]))):
        return []
    observations = [row for row in event.get("observations", []) if row.get("source") in {"NWS", "Aviation Weather Center"}
                    and not row.get("timing_proxy")]
    result = []
    for hour in artifact["hours"]:
        at = start + timedelta(hours=hour)
        if not parse_time(artifact["qualified_at"]) <= at <= through or str(hour) not in lane:
            continue
        forecast = hrrr.complete_remaining_trace(artifact["data"], station, event["date"], at, close, local_receipt=True)
        nbm = strategy_high_transfer._live_remaining(artifact, event, at, close)
        witness = max((w for row in observations for w in metar_high_evidence(row, station, start, at)),
                      key=lambda row: (row["lower_f"], -parse_time(row["available_at"]).timestamp()), default=None)
        if not forecast or not nbm or witness is None:
            continue
        result.append(_decision(artifact, event["buckets"], at, forecast, nbm, witness, lane[str(hour)]))
    return result


def _decision(artifact, buckets, at, trace, nbm, witness, residuals):
    hottest = max(trace, key=lambda point: point["temp_f"])
    remainder = {"hrrr_remaining_f": hottest["temp_f"], "expected_remaining_max_f": nbm[1], "all_observations_bound": witness}
    centers = research_hrrr_intraday._centers(remainder)
    probabilities = []
    for bucket in buckets:
        bounds = _limits(bucket)
        variants = {}
        for name, center in centers.items():
            samples = [center + delta for delta in residuals[name]]
            yes = guarded_probability(bounds, samples, .5, witness["lower_f"], artifact["disagreement_reserve"])
            count = sum(bounds[0] <= value < bounds[1] for value in samples)
            variants[name] = {"probability": yes, "calibration_days": len(samples),
                              "uncertain": len(samples) < MIN_BAND_DAYS or min(count, len(samples) - count) < MIN_TAIL_SUPPORT_DAYS}
        probabilities.append({"id": bucket["id"], **variants})
    return {"at": iso(at), "knowledge_at": iso(at), "study_id": artifact["study_id"], "vintage": artifact["vintage"],
            "climate_report_disagreement_reserve": artifact["disagreement_reserve"],
            "hrrr": {**hottest, "complete_frames": len(trace), "all_frames_received_at": max(row["ingested_at"] for row in trace)},
            "nbm": {"remaining_high_f": nbm[1], "issued_at": nbm[0]["issued_at"],
                    "received_at": nbm[0]["available_at"], "source": nbm[0]["hourly_source"]},
            "witness": witness, "raw_centers": centers, "buckets": probabilities}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data", type=Path, default=Path(".data"))
    p.add_argument("source", type=Path)
    args = p.parse_args()
    print(freeze(args.data, args.source))


if __name__ == "__main__":
    main()
