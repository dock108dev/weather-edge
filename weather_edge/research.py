"""Read the immutable local model and paper research records for the browser."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
import re

KINDS = {"models", "strategies"}
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}")


def load(data, kind, run_id):
    if kind not in KINDS or not RUN_ID.fullmatch(run_id):
        return None
    root = Path(data).resolve() / "research"
    path = (root / kind / f"{run_id}.json").resolve()
    if not path.is_relative_to(root):
        return None
    try:
        record = json.loads(path.read_bytes())
    except FileNotFoundError:
        return None
    if not isinstance(record, dict) or record.get("id") != run_id:
        raise ValueError(f"Invalid research record identity: {kind}/{run_id}")
    return {**record, "kind": kind}


def evidence_file(data, record):
    """Resolve the optional frozen evidence associated with a local research record."""
    sources = record.get("provenance") or {}
    for field in ("replay_details_path", "frozen_inputs", "frozen_input"):
        relative = sources.get(field)
        if not isinstance(relative, str):
            continue
        path = (Path(data) / relative).resolve()
        if (path.is_relative_to((Path(data) / "research").resolve())
                and path.name.endswith(".json.gz") and path.is_file()):
            return path
    return None


@lru_cache(maxsize=3)
def _case_index(data, revisions):
    index = {}
    for item in catalog(data)["records"]:
        record = load(data, item["kind"], item["id"])
        if record is None:
            continue
        for case_index, row in enumerate(record.get("cases") or []):
            key = row.get("venue"), row.get("instrument_id") or row.get("bucket_id")
            if None in key:
                continue
            index.setdefault(key, []).append({"kind": record["kind"], "run": record["id"], "case": case_index,
                                              "title": row.get("title") or record.get("title") or record["id"]})
    return index


def cases_for_market(data, venue, instrument):
    root = Path(data).resolve()
    revisions = tuple((root / "research" / kind).stat().st_mtime_ns if (root / "research" / kind).is_dir() else 0
                      for kind in sorted(KINDS))
    return _case_index(root, revisions).get((venue, instrument), [])[:6]


def catalog(data):
    root = Path(data) / "research"
    records, issues = [], []
    for kind in sorted(KINDS):
        for path in (root / kind).glob("*.json"):
            try:
                record = load(data, kind, path.stem)
            except (ValueError, OSError) as error:
                issues.append({"kind": kind, "id": path.stem, "reason": str(error)})
                continue
            if record is None:
                continue
            fields = ("id", "kind", "title", "target", "hypothesis", "created_at", "date_window", "venues", "summary", "supersedes")
            records.append({key: record[key] for key in fields if key in record})
    records.sort(key=lambda row: (str(row.get("created_at") or ""), row["id"]), reverse=True)
    return {"records": records, "issues": issues}
