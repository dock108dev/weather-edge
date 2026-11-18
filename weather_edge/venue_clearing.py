"""Archive public Polymarket US clearing report rows as daily price and volume marks."""

import argparse
from datetime import date, datetime, timedelta, timezone
import csv
import gzip
from hashlib import sha256
import json
from pathlib import Path
import re
import time
from urllib.request import Request, build_opener
from uuid import uuid4

from .capture import ImmutableOrigin
from .collector import paused
from .history import MARKET_REPORTS, _write
from .market import decimal, iso
from .venue_archive import POLY, connect, save_history, save_metadata, save_receipt, set_progress
from .venue_sources import CollectionPaused, Receipt

KIND = "clearing-daily-mark"
REPORT = re.compile(r"\d{8}-daily-market-report\.csv")
MAX_REPORT_BYTES = 200 * 2**20


def _clock():
    return iso(datetime.now(timezone.utc))


def _request(data, filename, *, expected_tag=None):
    if paused(data):
        raise CollectionPaused
    if filename != "manifest.json" and not REPORT.fullmatch(filename):
        raise ValueError("Unexpected Polymarket US market report route")
    source = f"{MARKET_REPORTS}/{filename}"
    headers = {"User-Agent": "WeatherEdge/0.1 local market research", "Accept-Encoding": "identity"}
    if expected_tag is not None and re.fullmatch(r"[0-9a-fA-F-]+", expected_tag):
        headers["If-Match"] = f'"{expected_tag}"'
    request = Request(source, headers=headers, method="GET")
    start = _clock()
    result = build_opener(ImmutableOrigin()).open(request, timeout=30)
    headers = {key.lower(): value for key, value in result.headers.items()
               if key.lower() in {"etag", "date", "content-length", "content-type", "last-modified", "cache-control"}}
    return result, source, start, headers


def _manifest(data):
    response, source, started, headers = _request(data, "manifest.json")
    with response:
        raw = response.read(2**20 + 1)
    if len(raw) > 2**20:
        raise ValueError("Manifest exceeded expected size")
    reply = Receipt(source, started, _clock(), 200, headers, raw)
    payload = reply.json()
    with connect(data) as db, db:
        receipt_id = save_receipt(db, POLY, "clearing-manifest", reply)
        save_metadata(db, POLY, "clearing-manifest", payload, receipt_id)
    return {item["filename"]: item for item in payload["files"]}, receipt_id


def _capture_report(data, spec, manifest_receipt_id):
    filename, size, tag = spec["filename"], int(spec["size"]), spec["eTag"]
    if not 0 < size <= MAX_REPORT_BYTES:
        raise ValueError(f"Report size {size} is outside the configured streaming bound")
    if not re.fullmatch(r"[0-9a-fA-F-]+", tag):
        raise ValueError("Unexpected manifest ETag")
    root = Path(data) / "venues" / "clearing"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{filename}.{tag}.gz"
    metadata = path.with_suffix(".receipt.json")
    if path.is_file() and metadata.is_file():
        return path, json.loads(metadata.read_bytes())
    response, source, started, headers = _request(data, filename, expected_tag=tag)
    temporary = path.with_suffix(f".{uuid4().hex}.part")
    digest = sha256()
    received = 0
    deadline = time.monotonic() + 120
    try:
        with response, gzip.open(temporary, "wb", compresslevel=4) as stream:
            if headers.get("etag", "").strip('"') != tag:
                raise ValueError("Report ETag disagrees with the provider's manifest")
            while chunk := response.read(1 << 18):
                if time.monotonic() >= deadline:
                    raise TimeoutError("Clearing report exceeded transfer window")
                received += len(chunk)
                if received > size:
                    raise ValueError("Report exceeds advertised size")
                digest.update(chunk)
                stream.write(chunk)
        if received != size:
            raise ValueError("Incomplete report body")
        record = {
            "original_bytes": received, "sha256": digest.hexdigest(), "etag": tag,
            "provider_last_modified_at": spec["lastModified"], "manifest_receipt_id": manifest_receipt_id,
            "source": source, "http_headers": headers, "requested_at": started, "received_at": _clock(),
            "path": str(path.relative_to(data)),
        }
        temporary.replace(path)
        _write(metadata, json.dumps(record, separators=(",", ":")).encode())
        return path, record
    finally:
        temporary.unlink(missing_ok=True)


def _index(data, path, report, filename):
    with connect(data) as db:
        weather = dict(db.execute("SELECT instrument_id,yes_payout FROM markets WHERE venue=?", (POLY,)))
    pos = date(int(filename[:4]), int(filename[4:6]), int(filename[6:8])).isoformat()
    grouped = []
    with gzip.open(path, "rt", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        required = {"Report ID", "Business Date", "Symbol", "Trade Volume", "Settlement Price"}
        if not reader.fieldnames or not required <= set(reader.fieldnames):
            raise ValueError("Unexpected Polymarket clearing report columns")
        for row in reader:
            instrument = row["Symbol"]
            if instrument not in weather or row["Business Date"] != pos:
                continue
            observed = decimal(row["Settlement Price"]) if row.get("Settlement Price") else None
            payout = weather[instrument]
            grouped.append((instrument, f"{pos}/{row['Report ID']}/{report['etag']}", {
                "raw_market_report_row": row, "report_source": report["source"],
                "report_publication_bound": report["provider_last_modified_at"],
                "retrospective_payout_match": None if observed is None or payout is None else observed == decimal(payout),
                "evidence_kind": "Daily clearing display mark and reported aggregate activity",
            }))
    at = iso(report["provider_last_modified_at"])
    manifest = Receipt(report["source"], report["requested_at"], report["received_at"], 200, report["http_headers"],
                       json.dumps(report, separators=(",", ":")).encode())
    with connect(data) as db, db:
        receipt_id = save_receipt(db, POLY, "clearing-file-pointer", manifest)
        for instrument, key, row in grouped:
            save_history(db, POLY, instrument, KIND, [(key, at, row)], receipt_id)
        set_progress(db, POLY, filename, KIND, "", 1, len(grouped), "complete", report["received_at"])
    return len(grouped)


def backfill(data, start, end):
    data = Path(data)
    rows, manifest = _manifest(data)
    result = {}
    for n in range((end - start).days + 1):
        day = start + timedelta(days=n)
        filename = f"{day:%Y%m%d}-daily-market-report.csv"
        if filename not in rows:
            result[day.isoformat()] = {"error": "No report in provider manifest"}
            continue
        try:
            path, report = _capture_report(data, rows[filename], manifest)
            result[day.isoformat()] = {"indexed_market_rows": _index(data, path, report, filename), **report}
        except (OSError, ValueError, KeyError, TypeError) as error:
            result[day.isoformat()] = {"error": str(error)}
            with connect(data) as db, db:
                set_progress(db, POLY, filename, KIND, "", 0, 0, "error", _clock(), str(error))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--from", required=True, dest="start", type=date.fromisoformat)
    parser.add_argument("--through", required=True, dest="end", type=date.fromisoformat)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(backfill(args.data, args.start, args.end), indent=2))
    except CollectionPaused:
        print("Collection is stopped. Saved reports remain available.")


if __name__ == "__main__":
    main()
