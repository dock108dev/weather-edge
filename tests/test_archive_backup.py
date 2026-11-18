"""Exercise archive publication, committed WAL recovery, and isolation on disk."""

from fcntl import LOCK_EX, LOCK_NB, flock
import os
import shutil
import subprocess
import sys

import pytest

from weather_edge import archive_backup as backup, paper_runs, venue_archive, venue_sources


def build(root):
    root.mkdir()
    (root / "index.html").write_text('<link href="/app-XYZ.css"><script src="/app-ABC.js"></script>')
    (root / "app-XYZ.css").write_text("html{}")
    (root / "app-ABC.js").write_text("42;")
    return root


def save(root, bundle, **args):
    assets = root.parent / "dist"
    return backup.backup(root, bundle, source_id="test-build", scope="test",
                         build=assets if assets.exists() else build(assets), **args)


def test_committed_wal_contains_native_body_and_restore_reopens_it(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    with venue_archive.connect(root) as db:
        assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        db.execute("PRAGMA wal_autocheckpoint=0")
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        reply = venue_sources.Receipt("https://example.test/native", "2026-11-15T00:00:00Z",
                                     "2026-11-15T00:00:01Z", 200, {}, b'{"exact":"native"}')
        with db:
            ident = venue_archive.save_receipt(db, "kalshi", "book", reply)
        main = root / "venues.sqlite3"
        assert (root / "venues.sqlite3-wal").stat().st_size > 0
        with backup.read_db(main, sealed=True) as original_main:
            assert original_main.execute("SELECT count(*) FROM receipts").fetchone()[0] == 0

        (root / "capture-index.sqlite3").write_bytes(b"optional derived index")
        (root / "fixture-index.sqlite3-wal").write_bytes(b"optional derived WAL")
        (root / "collector").mkdir()
        (root / "collector/paused").write_bytes(b"stopped")
        (root / "collector/iem-backoff.json").write_bytes(b'{"retry_at":"clock"}')
        (root / "collector/worker.json").write_text('{"pid":123}')
        (root / "collector/worker.lock").touch()
        (root / "captures").mkdir()
        (root / "captures/origin.json").write_bytes(b"retained exact receipt")

        bundle, restored = tmp_path / "bundle", tmp_path / "restored"
        manifest, manifest_hash = save(root, bundle, receipts=(ident,))
        assert backup.verify(bundle) == (manifest, manifest_hash)
        assert backup.restore(bundle, restored) == backup.verify(bundle, restored)
        assert manifest["collection"]["paused"]
        assert set(manifest["collection"]["files"]) == {"collector/paused", "collector/iem-backoff.json"}
        assert manifest["files"]["venues.sqlite3"]["sqlite"]["records"]["receipts"][str(ident)]["sha256"]
        assert (restored / "captures/origin.json").read_bytes() == b"retained exact receipt"
        assert not list(restored.glob("*index*"))
        assert not (restored / "collector/worker.json").exists()
        assert not (restored / "collector/worker.lock").exists()
        assert venue_archive.raw_receipt(restored, ident) == reply.body
        assert backup.digest(main) != backup.digest(restored / "venues.sqlite3")


def test_active_lock_and_changing_source_refuse_publication(tmp_path, monkeypatch):
    root = tmp_path / "source"
    (root / "collector").mkdir(parents=True)
    data = root / "evidence.json"
    data.write_bytes(b"before")
    bundle = tmp_path / "bundle"
    with (root / "collector/worker.lock").open("wb") as owner:
        flock(owner, LOCK_EX | LOCK_NB)
        with pytest.raises(ValueError, match="active owner"):
            save(root, bundle)
    copy_file = backup.copy_file

    def race(src, dst, **kwargs):
        entry = copy_file(src, dst, **kwargs)
        data.write_bytes(b"changed during copy")
        return entry

    monkeypatch.setattr(backup, "copy_file", race)
    with pytest.raises(ValueError, match="Source changed"):
        save(root, bundle)
    assert not bundle.exists()


def test_verified_restore_cannot_overwrite_and_rejects_damaged_backup(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    (root / "evidence.json").write_bytes(b"authentic")
    bundle = tmp_path / "bundle"
    manifest, sha = save(root, bundle)
    with pytest.raises(ValueError, match="separate, non-nested"):
        backup.restore(bundle, root / "nest")
    empty = tmp_path / "occupied"
    empty.mkdir()
    with pytest.raises(ValueError, match="already exists"):
        backup.restore(bundle, empty)

    invalid = tmp_path / "damaged"
    shutil.copytree(bundle, invalid)
    damaged = invalid / "data/evidence.json"
    clock = damaged.stat().st_mtime_ns
    damaged.write_bytes(b"corrupted")
    os.utime(damaged, ns=(clock, clock))
    destination = tmp_path / "restore"
    with pytest.raises(ValueError, match=r"evidence\.json:.*SHA-256"):
        backup.verify(invalid)
    with pytest.raises(ValueError, match=r"evidence\.json:.*SHA-256"):
        backup.restore(invalid, destination)
    assert not destination.exists() and list(empty.iterdir()) == []
    assert backup.verify(bundle) == (manifest, sha)
    assert (root / "evidence.json").read_bytes() == b"authentic"


@pytest.mark.parametrize("action", ["backup", "restore"])
def test_process_exit_leaves_only_an_incomplete_directory(tmp_path, action):
    root = tmp_path / "data"
    root.mkdir()
    (root / "evidence.json").write_bytes(b"retained")
    bundle = tmp_path / "bundle"
    destination = tmp_path / "destination"
    if action == "backup":
        build(tmp_path / "dist")
    else:
        save(root, bundle)
    script = """
import os
from pathlib import Path
import sys
from weather_edge import archive_backup as backup

root, bundle, target = [Path(p) for p in sys.argv[1:4]]
original = backup.copy_file
def interrupt(source, destination, **kwargs):
    original(source, destination, **kwargs)
    os._exit(73)
backup.copy_file = interrupt
if sys.argv[4] == "backup":
    backup.backup(root, target, build=root.parent / "dist", source_id="test", scope="test")
else:
    backup.restore(bundle, target)
"""
    stopped = subprocess.run([sys.executable, "-c", script, str(root), str(bundle), str(destination), action],
                             timeout=8)
    assert stopped.returncode == 73 and not destination.exists()
    incomplete, = tmp_path.glob(".destination.incomplete-*")
    with pytest.raises(ValueError, match="incomplete or invalid backup"):
        backup.verify(incomplete)
    if action == "backup":
        save(root, destination)
        backup.verify(destination)
    else:
        backup.restore(bundle, destination)
        backup.verify(bundle, destination)


def test_missing_and_corrupt_paper_run_name_the_repair_target(tmp_path):
    run_id = "a" * 24
    directory = tmp_path / "paper-runs"
    directory.mkdir()
    path = directory / f"{run_id}.json.gz"
    with pytest.raises(ValueError, match=f"{run_id} is missing; restore paper-runs/{run_id}"):
        paper_runs.load_run(tmp_path, run_id)
    path.write_bytes(b"invalid gzip")
    with pytest.raises(ValueError, match=f"Cannot read paper run {run_id}"):
        paper_runs.load_run(tmp_path, run_id)


def test_original_file_clock_and_provider_pacing_survive_restore(tmp_path):
    root = tmp_path / "archive"
    trace = root / "archive-raw/asos-minute/KNYC/2026-11-06.csv"
    trace.parent.mkdir(parents=True)
    trace.write_bytes(b"original trace")
    clock = 1794612345987654321
    os.utime(trace, ns=(clock, clock))
    pace = root / "venues/kalshi.pace"
    pace.parent.mkdir()
    pace.write_text("1794612345.98")
    bundle, restored = tmp_path / "bundle", tmp_path / "restore"
    manifest, _ = save(root, bundle)
    backup.restore(bundle, restored)
    target = restored / trace.relative_to(root)
    assert target.stat().st_mtime_ns == trace.stat().st_mtime_ns
    assert manifest["collection"]["files"]["venues/kalshi.pace"] == backup.digest(pace)
    os.utime(target, ns=(clock + 10**9, clock + 10**9))
    with pytest.raises(ValueError, match="original modification time"):
        backup.verify(bundle, restored)
