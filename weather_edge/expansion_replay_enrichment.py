"""Attach original receipt clocks to saved station-day observations."""

from __future__ import annotations

import argparse
from datetime import date
from hashlib import sha256
import json
from pathlib import Path

from . import history
from .nbm import iso, utc

VERSION = "original-madis-receipts-v1"
RECEIPT_BASIS = "NOAA MADIS original timeReceived"
DAY = date(2026, 11, 1)
STATIONS = ("KATL", "KDCA", "KEWR", "KLAS", "KMSY", "KTTN", "KAUS", "KDFW", "KSEA")


def path_for(data, day, station):
    return data / "archive-observation-enrichment" / str(day) / f"{station}.json"


def _key(row):
    return utc(row["at"]), history._report_key(row.get("raw") or "")


def attach(event, path):
    supplement = json.loads(path.read_bytes())
    if (supplement["version"], supplement["station"], supplement["date"]) != (VERSION, event["station"], event["date"]):
        raise ValueError("Original observation supplement has a different station or date")
    window = ("climate_starts_at", "climate_ends_at")
    start, close = (utc(event[field]) for field in window)
    if (start, close) != tuple(utc(supplement[field]) for field in window):
        raise ValueError("Original observation supplement has a different climate window")

    observations = {_key(row): row for row in event["observations"]}
    retrospective = {_key(row): row for row in event.get("research_observations", []) if row.get("timing_proxy")}
    added = set()
    for row in sorted(supplement["observations"], key=lambda item: (_key(item), utc(item["available_at"]))):
        key = _key(row)
        baseline = observations.get(key) or retrospective.get(key)
        if (not baseline or not baseline.get("timing_proxy") or row.get("timing_proxy")
                or row.get("receipt_basis") != RECEIPT_BASIS or row.get("receipt_source") not in supplement["sources"]
                or row["temp_f"] != baseline["temp_f"] or not start <= key[0] < close
                or utc(row["available_at"]) < key[0]):
            continue
        observations[key] = row
        added.add(key)
    if not added:
        return event

    previously_missing = event.get("observations_missing_receipt", 0)
    remaining = max(0, previously_missing - len(added))
    issue = f"{previously_missing} reports lack matched original receipt times"
    issues = [row for row in event.get("issues", []) if row.get("reason") != issue]
    if remaining:
        issues.append({"source": RECEIPT_BASIS, "reason": f"{remaining} reports lack matched original receipt times"})
    return {**event, "observations": sorted(observations.values(), key=lambda row: (_key(row)[0], utc(row["available_at"]), _key(row)[1])),
            "research_observations": [row for row in event.get("research_observations", []) if _key(row) not in added],
            "observations_missing_receipt": remaining, "issues": issues,
            "observation_enrichment": {"original_receipts": len(added), "basis": RECEIPT_BASIS, "sources": supplement["sources"]}}


def build(data):
    from . import expansion_replay

    targets = {(station, DAY) for station in STATIONS}
    receipts, errors = expansion_replay._retained_clocks(data, targets)
    summaries = []
    for station in STATIONS:
        base = data / "archive" / str(DAY) / f"{station}.json"
        raw = base.read_bytes()
        event = json.loads(raw)
        clocks = receipts.get((station, DAY), {})
        sources, observations = {}, []
        candidates = event["observations"] + event.get("research_observations", [])
        for row in candidates:
            at, report = _key(row)
            if not row.get("timing_proxy") or (match := clocks.get((iso(at), report))) is None:
                continue
            received, source = match
            local = data / "madis-metar" / source.rsplit("/", 1)[-1]
            if source not in sources:
                sources[source] = {"path": str(local.relative_to(data)), "sha256": sha256(local.read_bytes()).hexdigest()}
            observations.append({**{key: value for key, value in row.items() if key != "timing_proxy"},
                                 "source": "IEM ASOS / NOAA MADIS", "receipt_source": source,
                                 "receipt_basis": RECEIPT_BASIS, "available_at": received})
        if observations:
            path = path_for(data, DAY, station)
            payload = {"version": VERSION, "station": station, "date": str(DAY),
                       **{field: event[field] for field in ("climate_starts_at", "climate_ends_at")},
                       "baseline_archive_sha256": sha256(raw).hexdigest(), "sources": sources, "observations": observations}
            expansion_replay._write_new(path, payload)
            merged = attach(event, path)
            summaries.append({"station": station, "exact_receipts": merged["observation_enrichment"]["original_receipts"],
                              "unmatched": merged["observations_missing_receipt"], "path": str(path)})
    return {"date": str(DAY), "stations": summaries, "retained_file_issues": errors}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=Path(".data"), type=Path)
    args = parser.parse_args()
    print(json.dumps(build(args.data_dir), indent=2))


if __name__ == "__main__":
    main()
