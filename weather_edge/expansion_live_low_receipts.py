"""Narrow the scheduled daily-low shadow when an exact METAR arrives."""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path

from . import expansion_live_low, expansion_live_precision, expansion_metar, expansion_weather, research_model
from .history import _write
from .nbm import climate_window, utc

VERSION = "daily-low-first-receipt-shadow-v1"
QUALIFICATION = "daily-low-original-receipt-updates-v1"
RUNTIME = Path("research/runtime/daily-low-first-receipt")


def _receipt_rows(observations, start, as_of):
    for row in observations:
        try:
            receipt = utc(row["available_at"])
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
        if start <= receipt <= as_of and not row.get("timing_proxy"):
            yield receipt, row


def tightened_bound(observations, station, start, scheduled_at, as_of, prior_upper):
    """Accept a compatible newly tighter witness only if usable on its receipt clock."""
    reports = defaultdict(list)
    seen = set()
    for receipt, row in sorted(_receipt_rows(observations, start, as_of), key=lambda item: item[0]):
        try:
            candidates = expansion_metar.metar_low_evidence(row, station, start, as_of)
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
        for witness in candidates:
            key = witness["report_at"], witness["kind"]
            value = key, witness["lower_f"], witness["upper_f"], witness["temperature_precision_c"]
            if value in seen:
                continue
            seen.add(value)
            if utc(witness["period_end"]) > receipt:
                continue
            reports[key].append({**witness, "source": row.get("source"),
                                 "receipt_source": row.get("receipt_source") or row.get("source_url")})
    # A later correction can invalidate a same-time witness. Do not combine
    # conflicting raw readings or mistake a duplicate for a fresh tightening.
    eligible = [row for family in reports.values()
                if max(row["lower_f"] for row in family) <= min(row["upper_f"] for row in family)
                for row in family if row["upper_f"] < prior_upper and utc(row["available_at"]) > scheduled_at]
    return min(eligible, key=lambda row: (row["upper_f"], row["available_at"], row["temperature_precision_c"]), default=None)


def _live_observations(event):
    return [row for row in event.get("observations", []) if row.get("source") in {"NWS", "Aviation Weather Center"}]


def decision_clocks(artifact, event, as_of):
    """List eligible tighter-report times using the event's actual saved inputs."""
    if artifact is None or event.get("family") != "daily-low" or event.get("station") not in artifact["stations"]:
        return []
    station = event["station"]
    start, end = climate_window(date.fromisoformat(event["date"]), expansion_weather.STATIONS[station][3])
    now = min(utc(as_of), end - timedelta(microseconds=1))
    qualified = utc(artifact["qualification_evidence_cutoff"])
    observations = _live_observations(event)
    receipts = sorted({receipt for receipt, _ in _receipt_rows(observations, start, now)})
    anchors = sorted(start + timedelta(hours=row["hour"]) for row in artifact["scheduled"]["groups"]
                     if row["station"] == station and row["exceptions"] is not None)
    output = []
    for anchor, next_anchor in zip(anchors, [*anchors[1:], end], strict=False):
        clocks = [receipt for receipt in receipts if anchor < receipt < next_anchor and qualified <= receipt]
        if not clocks:
            continue
        scheduled = expansion_live_precision.predict(artifact["scheduled"], event, anchor)
        if scheduled is None:
            continue
        upper = scheduled["metar_upper_bound_f"]
        for receipt in clocks:
            updated = tightened_bound(observations, station, start, anchor, receipt, upper)
            if updated is None:
                continue
            if utc(updated["available_at"]) == receipt:
                output.append(updated["available_at"])
    return output


def freeze(data, qualification, *, scheduled_vintage=None):
    data = Path(data)
    raw = Path(qualification).read_bytes()
    evidence = json.loads(raw)
    if evidence["version"] != QUALIFICATION or set(evidence["stations"]) != set(research_model.STATIONS):
        raise ValueError("Expected the original-receipt two-station daily-low study")
    scheduled = expansion_live_precision.load(data, vintage=scheduled_vintage)
    if not scheduled:
        raise ValueError("No scheduled guarded daily-low vintage is available")
    fit = evidence["provenance"]["scheduled_calibration_input_sha256"]
    if scheduled["input_sha256"] != fit:
        raise ValueError("The first-receipt study used a different initial calibration")
    runtime_hash = research_model._digest(Path(__file__))
    if evidence["provenance"]["source_sha256"].get(Path(__file__).name) != runtime_hash:
        raise ValueError("Run the qualification with this receipt eligibility implementation")
    if evidence["coverage"]["raw_cli_bound_disagreements"] or not evidence["coverage"]["At the exact tighter report receipt"]["snapshots"]:
        raise ValueError("First-receipt qualification has unsupported climate bounds")
    body = {"version": VERSION, "scheduled_vintage": scheduled["vintage"], "stations": list(research_model.STATIONS),
            "qualification_study_id": evidence["id"], "qualification_sha256": hashlib.sha256(raw).hexdigest(),
            "qualification_path": str(Path(qualification).resolve().relative_to(data.resolve())),
            "qualification_dates": evidence["date_window"]["later_reports"],
            "qualification_evidence_cutoff": evidence["date_window"]["official_evidence_cutoff_at"],
            "qualification_written_at": evidence["created_at"],
            "qualification_coverage": evidence["coverage"]["All originally timed receipt updates"],
            "runtime_source_sha256": runtime_hash}
    encoded = research_model._json_bytes(body)
    vintage = hashlib.sha256(encoded).hexdigest()
    path = data / RUNTIME / f"{vintage}.json"
    if not path.exists():
        _write(path, encoded)
    _write(data / RUNTIME / "active.json", research_model._json_bytes({"vintage": vintage, "path": str(path.relative_to(data))}))
    return path


def load(data, *, vintage=None):
    data = Path(data)
    root = data / RUNTIME
    if vintage is None:
        pointer = root / "active.json"
        if not pointer.is_file():
            return None
        vintage = json.loads(pointer.read_bytes())["vintage"]
    if not isinstance(vintage, str) or len(vintage) != 64 or any(letter not in "0123456789abcdef" for letter in vintage):
        raise ValueError("Invalid daily-low first-receipt vintage")
    path = root / f"{vintage}.json"
    if not path.is_file():
        return None
    raw = path.read_bytes()
    provenance = json.loads(raw)
    if hashlib.sha256(raw).hexdigest() != vintage or provenance["version"] != VERSION:
        raise ValueError("Daily-low first-receipt artifact mismatch")
    scheduled = expansion_live_precision.load(data, vintage=provenance["scheduled_vintage"])
    return {**provenance, "vintage": vintage, "scheduled": scheduled} if scheduled else None


def predict(artifact, event, as_of):
    if artifact is None or event.get("station") not in artifact["stations"]:
        return None
    scheduled = expansion_live_precision.predict(artifact["scheduled"], event, as_of)
    if scheduled is None:
        return None
    station, day = event["station"], date.fromisoformat(event["date"])
    now, anchor = utc(as_of), utc(scheduled["knowledge_at"])
    qualified = utc(artifact["qualification_evidence_cutoff"])
    if now < qualified:
        return None
    start, _ = climate_window(day, expansion_weather.STATIONS[station][3])
    observations = _live_observations(event)
    updated = tightened_bound(observations, station, start, anchor, now, scheduled["metar_upper_bound_f"])
    origin = {**scheduled, "version": VERSION, "vintage": artifact["vintage"], "method": "guarded_cap_first_receipt",
              "scheduled_knowledge_at": scheduled["knowledge_at"], "scheduled_vintage": scheduled["vintage"],
              "scheduled_model_low_f": scheduled["model_low_f"], "scheduled_upper_bound_f": scheduled["metar_upper_bound_f"],
              "qualification_study_id": artifact["qualification_study_id"], "qualification_coverage": artifact["qualification_coverage"],
              "qualification_evidence_cutoff": artifact["qualification_evidence_cutoff"],
              "qualification_written_at": artifact["qualification_written_at"],
              "bound_receipt_update": None, "receipt_status": "scheduled-only"}
    if updated is None or utc(updated["available_at"]) < qualified:
        return origin
    center = min(updated["upper_f"], scheduled["remaining_context"]["remaining_low_f"])
    config = next(row for row in artifact["scheduled"]["groups"] if row["station"] == station and row["hour"] == scheduled["local_standard_decision_hour"])
    buckets = expansion_live_low._validated_buckets(event, day, station)
    probabilities = expansion_live_precision._bucket_estimates(buckets, center, updated["upper_f"], config)
    return {**origin, "knowledge_at": updated["available_at"], "available_at": updated["available_at"],
            "receipt_status": "tighter-report", "bound_receipt_update": updated, "metar_evidence": updated,
            "metar_upper_bound_f": updated["upper_f"], "model_low_f": center, "buckets": probabilities}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--qualification", type=Path, required=True)
    parser.add_argument("--scheduled-vintage")
    args = parser.parse_args()
    print(freeze(args.data, args.qualification, scheduled_vintage=args.scheduled_vintage))


if __name__ == "__main__":
    main()
