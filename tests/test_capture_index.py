"""Archived receipts remain queryable as a writer adds files."""
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from shutil import copyfile

from weather_edge import capture, capture_index
from weather_edge.capture_fixture import fixture_capture
from weather_edge.market import iso, parse_time


def test_index_updates_only_new_receipts_and_keeps_native_evidence(tmp_path, monkeypatch):
    folder = tmp_path / "captures"
    record = fixture_capture(duration_seconds=30, poll_seconds=8, max_rounds=8, max_events=12)
    record["kind"] = "live"
    original = capture.write_capture(record, folder)
    copyfile(original, folder / "duplicate.json")
    (folder / "broken.json").write_text("{malformed")
    captures = capture.load_captures(folder)
    assert captures["session_count"] == 1
    assert captures["issues"][0]["source"] == "broken.json"
    assert capture_index.path_for(folder) == tmp_path / "capture-index.sqlite3"

    event, = captures["events"]
    bucket = event["buckets"][0]
    symbol = bucket["id"]
    calls = []
    original_validation = capture._validate_capture

    def validate(body):
        calls.append(body)
        return original_validation(body)

    monkeypatch.setattr(capture, "_validate_capture", validate)
    latest_book = capture_index.latest_books(folder, symbol, limit=1)[0]
    assert latest_book["at"] == bucket["books"][-1]["at"]
    assert Path(latest_book["capture_file"]).name == original.name
    assert len(latest_book["capture_sha256"]) == 64
    assert not calls

    later = {
        **record, "started_at": "2026-11-16T09:59:59Z", "captured_at": "2026-11-16T10:00:00Z",
        "events": [],
        "settlements": {event["id"]: {symbol: {"yes_payout": 1, "available_at": "2026-11-16T10:00:00Z"}}},
    }
    added = capture.write_capture(later, folder)
    records, issues = capture_index.read(folder, validate, kind="live", version=1,
                                        instrument=bucket["market_id"], since="2026-11-16T05:00:00-05:00")
    assert [row[0] for row in records] == [added.name]
    assert len(calls) == 1
    assert issues[0]["source"] == "broken.json"
    assert capture.load_captures(folder)["events"][0]["buckets"][0]["settlement"]["yes_payout"] == 1
    assert len(calls) == 1


def test_recent_window_retains_preceding_exchange_clock(tmp_path):
    folder = tmp_path / "captures"
    earlier = fixture_capture(duration_seconds=30, poll_seconds=8, max_rounds=8, max_events=12)
    earlier["kind"] = "live"
    capture.write_capture(earlier, folder)

    later = deepcopy(earlier)
    first = parse_time(earlier["captured_at"]) + timedelta(seconds=5)
    later["started_at"] = later["captured_at"] = iso(first)
    bucket = later["events"][0]["buckets"][0]
    bucket["books"] = [{**bucket["books"][0], "at": iso(first)}]
    for sibling in later["events"][0]["buckets"][1:]:
        sibling["books"] = []
    capture.write_capture(later, folder)
    bounded = capture.load_captures(folder, since=iso(first))
    assert bounded["session_count"] == 1
    book, = bounded["events"][0]["buckets"][0]["books"]
    assert book["source_time_progress"] == "regressed"


def test_live_snapshot_assembles_only_appended_receipts_and_preserves_published_records(tmp_path, monkeypatch):
    folder = tmp_path / "captures"
    initial = fixture_capture(duration_seconds=30, poll_seconds=8, max_rounds=8, max_events=12)
    initial["kind"] = "live"
    event = initial["events"][0]
    unchanged = {**deepcopy(event), "id": "another-event"}
    initial["events"].append(unchanged)
    original_file = capture.write_capture(initial, folder)
    snapshot = capture.CaptureSnapshot(folder)
    batches = []
    read = capture_index.read_snapshot

    def observe(*args, **kwargs):
        extends, rows, issues = read(*args, **kwargs)
        batches.append((extends, [row[0] for row in rows]))
        return extends, rows, issues

    monkeypatch.setattr(capture_index, "read_snapshot", observe)
    before = snapshot.load(folder.stat().st_mtime_ns)
    original_book = before["events"][0]["buckets"][0]["books"][0]
    assert original_book["capture_file"] == original_file.name
    assert original_book["source_time_progress"] == "first"

    publication = deepcopy(initial)
    later = parse_time(initial["captured_at"]) + timedelta(seconds=5)
    publication["started_at"] = publication["captured_at"] = iso(later)
    publication["events"].pop()
    bucket = publication["events"][0]["buckets"][0]
    bucket["books"] = [
        {**bucket["books"][0], "at": iso(parse_time(original_book["at"]) - timedelta(seconds=1)),
         "source_at": iso(parse_time(original_book["source_at"]) + timedelta(seconds=1))},
    ]
    new = capture.write_capture(publication, folder)
    after = snapshot.load(folder.stat().st_mtime_ns)
    assert after == capture.load_captures(folder, versioned_windows=True)
    assert after["events"][1] is before["events"][1]
    added_book, older_book, *_ = after["events"][0]["buckets"][0]["books"]
    assert added_book["capture_file"] == new.name
    assert added_book["available_at"] == publication["captured_at"]
    assert older_book["capture_sha256"] == original_book["capture_sha256"]
    assert older_book["source_time_progress"] == "regressed"
    assert original_book["source_time_progress"] == "first"
    assert before["session_count"] == 1

    payout_at = iso(later + timedelta(seconds=5))
    payout = capture.write_capture({
        **publication, "started_at": payout_at, "captured_at": payout_at, "events": [],
        "settlements": {event["id"]: {bucket["id"]: {"yes_payout": 1, "available_at": payout_at}}},
    }, folder)
    settled = snapshot.load(folder.stat().st_mtime_ns)
    assert settled == capture.load_captures(folder, versioned_windows=True)
    assert settled["events"][0]["buckets"][0]["settlement"]["available_at"] == payout_at
    assert "settlement" not in after["events"][0]["buckets"][0]
    assert batches == [(False, [original_file.name]), (True, [new.name]), (True, [payout.name])]

    # A byte-identical earlier filename becomes the canonical witness. Even
    # though the original still exists, the prefix check must force a rebuild.
    duplicate = folder / "000-first.json"
    copyfile(original_file, duplicate)
    rebuilt = snapshot.load(folder.stat().st_mtime_ns)
    assert rebuilt == capture.load_captures(folder, versioned_windows=True)
    assert batches[-1] == (False, [duplicate.name, new.name, payout.name])
    assert rebuilt["events"][0]["buckets"][0]["books"][1]["capture_file"] == duplicate.name
    assert older_book["capture_file"] == original_file.name

    new.unlink()
    removed = snapshot.load(folder.stat().st_mtime_ns)
    assert removed == capture.load_captures(folder, versioned_windows=True)
    assert batches[-1] == (False, [duplicate.name, payout.name])
    assert removed["events"][0]["buckets"][0]["books"][0]["source_time_progress"] == "first"
