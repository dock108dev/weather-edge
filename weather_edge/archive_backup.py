"""Back up a quiet local Weather Edge data root; verify or restore it offline."""

import argparse
from contextlib import ExitStack, closing, contextmanager
from datetime import datetime, timezone
from fcntl import LOCK_EX, LOCK_NB, flock
from hashlib import file_digest, sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
from shutil import disk_usage
import sqlite3
import stat
from tempfile import mkdtemp
import zlib


FORMAT = 1
INDEXES = {"capture-index.sqlite3", "fixture-index.sqlite3"}
LOCKS = ("collector/worker.lock", "trade-paper/reconcile.lock")
OMIT = {*INDEXES, *LOCKS, "collector/worker.json", "strategy-snow/alta/reader/node_modules"}
SQLITE_SUFFIXES = (".sqlite3", ".sqlite", ".db")
SIDECARS = ("-wal", "-shm", "-journal")
CHUNK = 2**20


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    with path.open("rb") as file:
        return file_digest(file, "sha256").hexdigest()


def packed(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def is_store(name):
    return name.endswith(SQLITE_SUFFIXES)


def sidecar(name):
    return next((suffix for suffix in SIDECARS if name.endswith(suffix) and is_store(name[:-len(suffix)])), None)


def build_identity(root):
    index = root / "index.html"
    # The content-addressed script and stylesheet referenced by the served HTML.
    emitted = set(re.findall(r'''(?:src|href)=["']/?(app-[A-Z0-9]+\.(?:js|css))["']''', index.read_text()))
    if {Path(name).suffix for name in emitted} != {".js", ".css"}:
        raise ValueError(f"{index}: expected the built app script and stylesheet")
    return {p: digest(root / p) for p in sorted({"index.html", *emitted})}


def files(root, *, source=False):
    def fail(error):
        raise error

    if not root.is_dir() or root.is_symlink():
        raise ValueError(f"Expected a regular data directory: {root}")
    result = {}
    for base, dirs, names in os.walk(root, onerror=fail):
        base = Path(base)
        for name in dirs + names:
            path = base / name
            key = path.relative_to(root).as_posix()
            side = sidecar(key)
            if source and (key in OMIT or side and key[:-len(side)] in INDEXES or side == "-shm"):
                if name in dirs:
                    dirs.remove(name)
                continue
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                continue
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(f"{path}: expected a regular file (links and special files are unsupported)")
            if source and side and not info.st_size:
                continue
            result[key] = info
    return result


def fingerprint(entries):
    return {name: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns) for name, s in entries.items()}


@contextmanager
def quiet_locks(root):
    # Don't add process state to the source. These are additional checks; quiet
    # operation also requires stopping listeners and any standalone writers.
    with ExitStack() as stack:
        for name in LOCKS:
            path = root / name
            if not path.exists():
                continue
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            stack.callback(os.close, fd)
            try:
                flock(fd, LOCK_EX | LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError(f"{path}: active owner; stop all writers before taking the backup") from exc
        yield


def disjoint(a, b):
    if a.is_relative_to(b) or b.is_relative_to(a):
        raise ValueError(f"Use separate, non-nested roots: {a} and {b}")


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def publication(destination):
    if destination.exists() or destination.is_symlink():
        raise ValueError(f"Destination already exists: {destination}")
    work = Path(mkdtemp(prefix=f".{destination.name}.incomplete-", dir=destination.parent))
    try:
        yield work
        for base, _, _ in os.walk(work, topdown=False):
            sync_dir(base)
        if destination.exists() or destination.is_symlink():
            raise ValueError(f"Destination appeared during the operation: {destination}")
        work.rename(destination)
        sync_dir(destination.parent)
    except Exception as exc:
        raise ValueError(f"{exc}; unpublished work path: {work}") from exc


def copy_file(source, target, *, mtime_ns=None):
    h = sha256()
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as src, target.open("xb") as dst:
        while block := src.read(CHUNK):
            dst.write(block)
            h.update(block)
        dst.flush()
        if mtime_ns is not None:
            os.utime(dst.fileno(), ns=(mtime_ns, mtime_ns))
        os.fsync(dst.fileno())
    info = target.stat()
    return {"bytes": info.st_size, "sha256": h.hexdigest(),
            **({"mtime_ns": info.st_mtime_ns} if mtime_ns is not None else {})}


def read_db(path, *, sealed=False):
    # Sealed outputs are self-contained DELETE-mode copies. Read them without
    # creating shared-memory files or running the application's migrations.
    return closing(sqlite3.connect(path.as_uri() + "?mode=ro" + ("&immutable=1" if sealed else ""),
                                   uri=True, timeout=10))


def schema(db):
    rows = [tuple(row) for row in db.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name")]
    return {"user_version": db.execute("PRAGMA user_version").fetchone()[0],
            "application_id": db.execute("PRAGMA application_id").fetchone()[0],
            "schema_sha256": sha256(packed(rows)).hexdigest()}, {row[1] for row in rows if row[0] == "table"}


def logical(db, name, receipt_ids):
    meta, tables = schema(db)
    marks = {}
    if name == "venues.sqlite3" and {"receipts", "bodies"} <= tables:
        newest = db.execute("SELECT max(id) FROM receipts").fetchone()[0]
        marks["last_receipt_id"] = newest
        selected = set(receipt_ids)
        # The newest receipt may share a body with an older response.
        if newest is not None:
            selected.add(newest)
        records = {}
        for ident in sorted(selected):
            row = db.execute(
                "SELECT r.venue,r.kind,r.requested_at,r.received_at,r.status,r.digest,b.compressed "
                "FROM receipts r LEFT JOIN bodies b USING(digest) WHERE r.id=?", (ident,)).fetchone()
            if row is None or row[-1] is None:
                raise ValueError(f"{name}: receipt {ident} is missing its raw body")
            actual = sha256(zlib.decompress(row[-1])).hexdigest()
            if actual != row[-2]:
                raise ValueError(f"{name}: receipt {ident} has a raw body digest mismatch")
            records[str(ident)] = dict(zip(("venue", "kind", "requested_at", "received_at", "status", "sha256"),
                                          row[:-1]))
        marks["receipts"] = records
        if "book_batches" in tables:
            marks["last_book_batch"] = db.execute(
                "SELECT id,completed_at FROM book_batches ORDER BY id DESC LIMIT 1").fetchone()
    elif name == "weather.sqlite3" and "weather" in tables:
        marks["weather_count"], marks["last_weather_seen"] = db.execute(
            "SELECT count(*), max(seen) FROM weather").fetchone()
    elif name in {"marine.sqlite3", "twc-climate.sqlite3"}:
        for table in sorted(tables & {"receipt", "receipts", "report", "observations"}):
            marks[table] = db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    elif name == "trade-paper/orders.sqlite3":
        for table in sorted(tables & {"orders", "reconciliation"}):
            rows = db.execute(f"SELECT * FROM {table} ORDER BY fixture,request_id").fetchall()
            marks[table] = {"count": len(rows), "sha256": sha256(packed(rows)).hexdigest()}
    # Stored JSON uses arrays, including the last book-batch record.
    return json.loads(packed({**meta, "records": marks}))


def snapshot(source, target, name, ids):
    target.parent.mkdir(parents=True, exist_ok=True)
    with read_db(source) as src, closing(sqlite3.connect(target)) as dst:
        src.execute("BEGIN")
        expected = logical(src, name, ids)  # Establishes the committed read snapshot.
        cutoff = now()
        src.backup(dst)
        if dst.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
            raise ValueError(f"{name}: could not produce a self-contained SQLite copy")
        if logical(dst, name, ids) != expected:
            raise ValueError(f"{name}: logical witness changed in the SQLite copy")
    with target.open("rb") as file:
        os.fsync(file.fileno())
    return {"bytes": target.stat().st_size, "sha256": digest(target), "snapshot_at": cutoff, "sqlite": expected}


def ledger(root):
    """Read all ledger references and both account namespaces without opening a writer."""
    from . import paper_runs, trade_orders

    path = root / "trade-paper/orders.sqlite3"
    if not path.is_file():
        return None
    run_root = path.parent
    checked = {}

    def run_ref(ident, order, trail=()):
        if ident in trail:
            raise ValueError(f"order {order}: circular paper run parent {ident}")
        if ident not in checked:
            try:
                run = paper_runs.load_run(run_root, ident)
                paper_runs.verify_run(run)
                if run.get("inputs", {}).get("parent_run"):
                    run_ref(run["inputs"]["parent_run"], order, (*trail, ident))
            except (OSError, ValueError, EOFError, TypeError, zlib.error) as exc:
                raise ValueError(f"order {order}: required paper run {ident}: {exc}") from exc
            checked[ident] = run
        return checked[ident]

    pending, proposals = [], []
    with read_db(path, sealed=True) as db:
        db.row_factory = sqlite3.Row
        _, tables = schema(db)
        if "orders" in tables:
            for row in db.execute("SELECT * FROM orders ORDER BY fixture,request_id"):
                ident = row["request_id"]
                if row["status"] == "Pending recheck":
                    pending.append({"fixture": bool(row["fixture"]), "order_id": ident})
                if row["result"]:
                    result = json.loads(row["result"])
                    if result.get("run_id"):
                        run_ref(result["run_id"], ident)
            if "reconciliation" in tables:
                for saved in db.execute("SELECT * FROM reconciliation ORDER BY fixture,request_id"):
                    ident = saved["request_id"]
                    order = trade_orders._find(db, saved["fixture"], ident)
                    if order is None or saved["expected"] != trade_orders._expected(order):
                        raise ValueError(f"order {ident}: saved reconciliation doesn't match its ledger row")
                    status, result, run = json.loads(saved["proposal"])
                    if run is not None:
                        paper_runs.verify_run(run)
                        if result.get("run_id") != run["id"]:
                            raise ValueError(f"order {ident}: saved reconciliation refers to the wrong run")
                        if parent := run["inputs"].get("parent_run"):
                            run_ref(parent, ident)
                        if paper_runs._path(run_root, run["id"]).exists():
                            existing = run_ref(run["id"], ident)
                            if packed([run["inputs"], run["result"]]) != packed([existing["inputs"], existing["result"]]):
                                raise ValueError(f"order {ident}: saved and published reconciliation disagree")
                    proposals.append({"order_id": ident, "fixture": bool(saved["fixture"]), "status": status,
                                      "run_id": run["id"] if run is not None else None,
                                      "sha256": sha256(saved["proposal"].encode()).hexdigest()})
        accounts = {}
        for fixture in (False, True):
            cash, committed, exposures = (trade_orders._allocation(db, fixture) if "orders" in tables
                                          else (trade_orders.trade.BANKROLL, 0, {}))
            accounts["fixture" if fixture else "live"] = {
                "cash": str(cash), "committed": str(committed),
                "exposure": [[*key, str(cost)] for key, cost in sorted(exposures.items())],
            }
    return {"accounts": accounts, "pending": pending, "proposals": proposals, "committed_runs": sorted(checked)}


def collection(entries):
    return {"paused": "collector/paused" in entries,
            "files": {k: entries[k]["sha256"] for k in sorted(entries)
                      if k == "collector/paused" or k.startswith("collector/") and "backoff" in Path(k).name
                      or Path(k).parent == Path("venues") and k.endswith(".pace")}}


def safe_name(name):
    p = PurePosixPath(name)
    return not p.is_absolute() and p.as_posix() == name and p.parts and ".." not in p.parts


def run_ids(entries):
    return {n: Path(n).name.removesuffix(".json.gz") for n in sorted(entries)
            if "/paper-runs/" in "/" + n and n.endswith(".json.gz")}


def validate_data(root, manifest, *, hashes=True):
    entries = manifest["files"]
    actual = files(root)
    if difference := sorted(set(entries) ^ set(actual)):
        raise ValueError(f"{root}: missing or unexpected archive paths: {', '.join(difference[:8])}")
    for name, entry in entries.items():
        path = root / name
        if actual[name].st_size != entry["bytes"] or hashes and digest(path) != entry["sha256"]:
            raise ValueError(f"{path}: backup size or SHA-256 does not match the manifest")
        if "mtime_ns" in entry and actual[name].st_mtime_ns != entry["mtime_ns"]:
            raise ValueError(f"{path}: original modification time does not match the manifest")
        if "sqlite" in entry:
            try:
                with read_db(path, sealed=True) as db:
                    check = db.execute("PRAGMA quick_check").fetchall()
                    if check != [("ok",)]:
                        raise ValueError(f"SQLite quick_check: {check[:3]}")
                    if logical(db, name, manifest["venue_receipt_ids"]) != entry["sqlite"]:
                        raise ValueError("SQLite schema or logical witness does not match")
            except (ValueError, sqlite3.Error, zlib.error) as exc:
                raise ValueError(f"{path}: {exc}") from exc
    if collection(entries) != manifest["collection"] or run_ids(entries) != manifest["runs"]:
        raise ValueError("Archived collection controls or run IDs do not match the manifest")
    if ledger(root) != manifest["ledger"]:
        raise ValueError(f"{root}: restored ledger accounting, run references or proposals do not match")


def backup(source, destination, *, source_id, build, scope, receipts=()):
    source = source.resolve(strict=True)
    destination = output_path(destination)
    disjoint(source, destination)
    identity = build_identity(build)
    ids = sorted(set(receipts))
    with quiet_locks(source), publication(destination) as work:
        root = work / "data"
        root.mkdir()
        started = now()
        before = files(source, source=True)
        if ids and "venues.sqlite3" not in before:
            raise ValueError("Requested venue receipts but venues.sqlite3 is missing")
        entries = {}
        for name in sorted(before):
            if sidecar(name):
                if name[:-len(sidecar(name))] not in before:
                    raise ValueError(f"{source / name}: SQLite sidecar without its main database")
                continue
            path = source / name
            if is_store(name):
                entries[name] = snapshot(path, root / name, name, ids)
            else:
                mtime_ns = before[name].st_mtime_ns
                entry = copy_file(path, root / name, mtime_ns=mtime_ns)
                if entry["mtime_ns"] != mtime_ns:
                    raise ValueError(f"{root / name}: destination cannot preserve the original modification time")
                entries[name] = entry
        if fingerprint(before) != fingerprint(files(source, source=True)):
            raise ValueError("Source changed while copying; stop all writers and start a new backup")
        cutoff = now()
        manifest = {"format": FORMAT, "scope": scope, "source": str(source), "source_id": source_id,
                    "build": identity, "started_at": started, "quiet_through": cutoff,
                    "boundary": "operator-confirmed quiet data root; checked owner locks and file metadata",
                    "venue_receipt_ids": ids, "files": entries, "collection": collection(entries),
                    "ledger": ledger(root), "runs": run_ids(entries)}
        validate_data(root, manifest, hashes=False)
        body = packed(manifest) + b"\n"
        with (work / "manifest.json").open("xb") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
    return manifest, sha256(body).hexdigest()


def read_manifest(bundle):
    path = bundle / "manifest.json"
    if (bundle.name.startswith(".") and ".incomplete-" in bundle.name
            or path.is_symlink() or not path.is_file() or {p.name for p in bundle.iterdir()} != {"data", "manifest.json"}):
        raise ValueError(f"{bundle}: incomplete or invalid backup; expected a published data/ and manifest.json")
    body = path.read_bytes()
    manifest = json.loads(body)
    if manifest.get("format") != FORMAT:
        raise ValueError("Unsupported archive backup manifest format")
    if any(not safe_name(name) or name in OMIT or sidecar(name) or ("sqlite" in entry) != is_store(name)
           or ("mtime_ns" in entry) == is_store(name)
           for name, entry in manifest["files"].items()):
        raise ValueError("Manifest contains an invalid archive path or SQLite entry")
    return manifest, sha256(body).hexdigest()


def verify(bundle, restored=None):
    bundle = bundle.resolve(strict=True)
    manifest, sha = read_manifest(bundle)
    destination = None if restored is None else restored.resolve(strict=True)
    if destination:
        disjoint(bundle, destination)
        disjoint(Path(manifest["source"]), destination)
    validate_data(destination or bundle / "data", manifest)
    return manifest, sha


def output_path(path):
    # Resolve the parent only, so a destination symlink (even dangling) is rejected.
    return path.parent.resolve(strict=True) / path.name


def restore(bundle, destination):
    bundle = bundle.resolve(strict=True)
    destination = output_path(destination)
    disjoint(bundle, destination)
    manifest, sha = read_manifest(bundle)
    disjoint(Path(manifest["source"]), destination)
    present = files(bundle / "data")
    if set(present) != set(manifest["files"]):
        raise ValueError("Backup data paths do not match its manifest; run verify for details")
    with publication(destination) as work:
        for name, entry in manifest["files"].items():
            mtime_ns = entry.get("mtime_ns")
            if mtime_ns is not None and present[name].st_mtime_ns != mtime_ns:
                raise ValueError(f"{name}: original modification time does not match the manifest")
            copied = copy_file(bundle / "data" / name, work / name, mtime_ns=mtime_ns)
            if copied["bytes"] != entry["bytes"] or copied["sha256"] != entry["sha256"]:
                raise ValueError(f"{name}: backup size or SHA-256 does not match the manifest")
        validate_data(work, manifest, hashes=False)
    return manifest, sha


def measure(root, volumes):
    """Sample regular-file bytes. APFS clone extents are counted per file."""
    root = root.resolve(strict=True)
    started = now()
    paths, sqlite = {}, {}
    for base, _, names in os.walk(root):
        for name in names:
            path = Path(base) / name
            try:
                s = path.lstat()
            except FileNotFoundError:
                continue  # A live WAL or temporary file can disappear during inventory.
            if not stat.S_ISREG(s.st_mode):
                continue
            rel = path.relative_to(root)
            item = paths.setdefault(rel.parts[0], {"logical_bytes": 0, "allocated_bytes": 0})
            item["logical_bytes"] += s.st_size
            item["allocated_bytes"] += s.st_blocks * 512
            if is_store(rel.as_posix()) or sidecar(rel.as_posix()):
                sqlite[rel.as_posix()] = {"logical_bytes": s.st_size, "allocated_bytes": s.st_blocks * 512}
    totals = {key: sum(row[key] for row in paths.values()) for key in ("logical_bytes", "allocated_bytes")}
    available = {}
    for folder in [root, *volumes]:
        p = folder.resolve(strict=True)
        available[str(p)] = {"device": p.stat().st_dev, "free_bytes": disk_usage(p).free}
    return {"started_at": started, "completed_at": now(), "root": str(root), "totals": totals,
            "paths": paths, "sqlite": sqlite, "volumes": available}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    save = sub.add_parser("backup")
    save.add_argument("source", type=Path)
    save.add_argument("bundle", type=Path)
    save.add_argument("--quiescent", required=True, action="store_true", help="Confirm all source writers are stopped")
    save.add_argument("--source-id", required=True, help="Git commit or staged tree identifying the running source")
    save.add_argument("--build", type=Path, default=Path("dist"), help="Served build directory")
    save.add_argument("--scope", default="full-data-root", help="Label the scope of this source (full by default)")
    save.add_argument("--venue-receipt", type=int, action="append", default=[])
    check = sub.add_parser("verify")
    check.add_argument("bundle", type=Path)
    check.add_argument("--restored", type=Path, help="Check a fresh restore before building indexes or launching")
    unpack = sub.add_parser("restore")
    unpack.add_argument("bundle", type=Path)
    unpack.add_argument("destination", type=Path)
    sample = sub.add_parser("measure")
    sample.add_argument("root", type=Path)
    sample.add_argument("--volume", type=Path, action="append", default=[], help="Existing backup or restore parent")
    args = parser.parse_args()
    try:
        if args.action == "measure":
            print(json.dumps(measure(args.root, args.volume), indent=2, sort_keys=True))
            return
        if args.action == "backup":
            manifest, sha = backup(args.source, args.bundle, source_id=args.source_id,
                                   build=args.build, scope=args.scope, receipts=args.venue_receipt)
        elif args.action == "verify":
            manifest, sha = verify(args.bundle, args.restored)
        else:
            manifest, sha = restore(args.bundle, args.destination)
        print(json.dumps({"status": args.action + " ok", "manifest_sha256": sha, "scope": manifest["scope"],
                          "files": len(manifest["files"]),
                          "bytes": sum(row["bytes"] for row in manifest["files"].values()),
                          "collection": manifest["collection"], "ledger": manifest["ledger"]}, indent=2))
    except (OSError, ValueError, sqlite3.Error, KeyError, TypeError, EOFError, zlib.error) as exc:
        parser.exit(1, f"{args.action}: {exc}\n")


if __name__ == "__main__":
    main()
