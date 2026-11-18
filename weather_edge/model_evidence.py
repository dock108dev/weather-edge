"""Timestamped local evidence of model artifacts available to Weather Edge."""

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
from tempfile import NamedTemporaryFile

from .market import iso, parse_time


FAMILIES = frozenset({"study", "legacy", "daily_low", "daily_low_precision"})
DIGEST = re.compile(r"[0-9a-f]{64}")


def _write(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}-", delete=False) as file:
        temporary = Path(file.name)
        try:
            file.write(body)
            file.flush()
            # A completed immutable receipt is visible in one filesystem step.
            try:
                path.hardlink_to(temporary)
            except FileExistsError:
                pass
        finally:
            temporary.unlink(missing_ok=True)


def record(data, family, vintage, at, basis, source):
    if family not in FAMILIES or not DIGEST.fullmatch(vintage):
        raise ValueError("Unknown model family or artifact digest")
    receipt = {"family": family, "vintage": vintage, "available_at": iso(at), "basis": basis, "source": source}
    body = json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
    path = Path(data) / "model-availability" / family / vintage / f"{sha256(body).hexdigest()}.json"
    if not path.is_file():
        _write(path, body)
    return {**receipt, "receipt_sha256": path.stem}


def published(data, family, vintage, artifact):
    path = Path(artifact)
    digest = sha256(path.read_bytes()).hexdigest()
    if digest != vintage:
        raise ValueError("Published artifact does not match its digest")
    return record(data, family, vintage, datetime.now(timezone.utc), "local-publish",
                  {"artifact": str(path.relative_to(data)), "artifact_sha256": digest})


def legacy_snapshot(data, blob):
    vintage = sha256(blob).hexdigest()
    path = Path(data) / "model" / "vintages" / f"{vintage}.joblib"
    snapshot(path, blob, vintage)
    return vintage, path


def snapshot(path, blob, vintage):
    """Keep the decoded bytes under their digest, repairing an invalid copy."""
    if not path.is_file():
        _write(path, blob)
    elif sha256(path.read_bytes()).hexdigest() != vintage:
        # Replace only a copy that fails its immutable identity check.
        with NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}-", delete=False) as file:
            temporary = Path(file.name)
            try:
                file.write(blob)
                file.flush()
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)


def available(data):
    """Return the earliest witnessed availability for each exact artifact."""
    earliest = {}
    root = Path(data) / "model-availability"
    for path in root.glob("*/*/*.json"):
        try:
            body = path.read_bytes()
            if sha256(body).hexdigest() != path.stem:
                continue
            item = json.loads(body)
            family, vintage = item["family"], item["vintage"]
            if (family not in FAMILIES or not DIGEST.fullmatch(vintage)
                    or path.parent.name != vintage or path.parent.parent.name != family):
                continue
            clock = parse_time(item["available_at"])
            key = family, vintage
            if key not in earliest or clock < parse_time(earliest[key]["available_at"]):
                earliest[key] = {**item, "receipt_sha256": path.stem}
        except (OSError, TypeError, ValueError, KeyError):
            continue
    return earliest
