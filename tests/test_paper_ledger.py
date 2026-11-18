"""Paper order recovery across real process exits and concurrent HTTP requests."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from hashlib import sha256
import json
import os
from pathlib import Path
from shutil import copytree
import sqlite3
import subprocess
import sys
from threading import Barrier, Event
from uuid import uuid4

import pytest

from test_historical_http import app, write_legacy
from test_paper_eligibility import DEADLINE, POLY, RECHECK, SIGNAL, clocked_app, prepare
from weather_edge import model_evidence, paper_runs, server, trade, trade_orders, venue_sources
from weather_edge.market import iso, parse_time


LEDGER = "/api/trade/orders"
EXIT = 77


def submit(request, instrument, *, budget=25, request_id=None):
    selection = dict(venue=POLY, instrument=instrument, side="YES", budget=budget)
    ticket = request("/api/trade", **selection)
    payload = {**selection, "request_id": request_id or str(uuid4()), "review_key": ticket["review_key"]}
    return ticket, payload


def payout(data, instrument, at, yes):
    def read(venue, path):
        assert venue == POLY
        return venue_sources.Receipt("https://api.polymarket.us" + path, iso(at), iso(at), 200, {},
                                     json.dumps({"slug": instrument, "settlement": yes}).encode())
    assert venue_sources._poly_payout(data, read, instrument) == 200


def step(data, instrument, at):
    probability = lambda *_: {instrument: {"probability": .8, "uncertain": False, "model_version": "controlled"}}
    trade_orders.reconcile(data, at, lambda: None, lambda _: probability)


def verified(data, run_id):
    path = paper_runs._path(data / "trade-paper", run_id)
    run = paper_runs.load_run(data / "trade-paper", run_id)
    assert paper_runs.verify_run(run) == run["result"]
    return run, sha256(path.read_bytes()).hexdigest()


def database(data):
    return data / "trade-paper" / "orders.sqlite3"


def crash(data, instrument, at, boundary, payload=None):
    process = subprocess.run(
        [sys.executable, __file__, str(data), instrument, iso(at), boundary, json.dumps(payload)],
        capture_output=True, text=True, timeout=20,
        env={**os.environ, "PYTHONPATH": os.pathsep.join((str(Path(__file__).resolve().parents[1]), str(Path(__file__).parent)))},
    )
    assert process.returncode == EXIT, (process.returncode, process.stdout, process.stderr)
    return process.stdout.strip()


def interrupt_after(original, name):
    def interrupted(*args, **kwargs):
        result = original(*args, **kwargs)
        if result is not None or name == "_publish":
            print(f"PID {os.getpid()} exited after {name}", flush=True)
            os._exit(EXIT)
        return result
    return interrupted


@pytest.mark.parametrize("boundary", ["record", "_record_proposal", "_publish", "_finalize"])
def test_entry_restart_and_exact_retry(tmp_path, monkeypatch, boundary):
    instrument, _, _ = prepare(tmp_path)
    clock = clocked_app(monkeypatch, tmp_path, instrument, SIGNAL)
    with app() as request:
        _, payload = submit(request, instrument)
        if boundary == "record":
            stopped = crash(tmp_path, instrument, SIGNAL, boundary, payload)
        else:
            request(LEDGER, payload=payload)
            stopped = crash(tmp_path, instrument, RECHECK, boundary)
        before = request(LEDGER)
        assert before["cash"] == 478.29 and before["committed"] == 21.71
        assert before["exposure"] == [{"station": before["orders"][0]["ticket"]["station"],
                                       "day": before["orders"][0]["ticket"]["day"], "cost": 21.71}]
        assert before["orders"][0]["status"] == ("Simulated" if boundary == "_finalize" else "Pending recheck")
        with sqlite3.connect(database(tmp_path)) as db:
            if boundary in ("_record_proposal", "_publish"):
                proposed, = db.execute("SELECT proposal FROM reconciliation").fetchone()
                planned = json.loads(proposed)[2]
            else:
                planned = None
        first_id = planned["id"] if planned else (before["orders"][0]["result"] or {}).get("run_id")
        before_files = list((tmp_path / "trade-paper" / "paper-runs").glob("*.json.gz"))
        assert len(before_files) == int(boundary in ("_publish", "_finalize"))
        first_bytes = before_files[0].read_bytes() if before_files else None

        # Once proposed, recovery cannot depend on evidence being readable.
        if planned or boundary == "_finalize":
            monkeypatch.setattr(trade_orders, "_evaluate", lambda *_: pytest.fail("re-evaluated committed proposal"))
        clock[0] = DEADLINE + timedelta(minutes=2)
        step(tmp_path, instrument, clock[0])
        done = request(LEDGER)
        exactly = request(LEDGER, payload=payload)
        request(LEDGER, payload={**payload, "review_key": "another-quote"}, expect=409)
    position = exactly["result"]["position"]
    assert exactly == done["orders"][0]
    assert exactly["id"] == payload["request_id"] and exactly["status"] == "Simulated"
    assert position["quantity"] == 200 and position["max_loss"] == 21.71
    assert done["cash"] == before["cash"] and done["committed"] == before["committed"]
    run, digest = verified(tmp_path, exactly["result"]["run_id"])
    assert run["inputs"]["as_of"] == iso(RECHECK)
    if first_id:
        assert run["id"] == first_id
    if first_bytes:
        assert paper_runs._path(tmp_path / "trade-paper", run["id"]).read_bytes() == first_bytes
    step(tmp_path, instrument, clock[0] + timedelta(seconds=1))
    assert trade_orders.orders(tmp_path, False) == done
    print(stopped, "order", exactly["id"], "entry", run["id"], digest, "cash", done["cash"], "committed", done["committed"])


@pytest.mark.parametrize(("boundary", "yes"), [("_record_proposal", 1), ("_publish", 1), ("_finalize", 0)])
def test_offline_through_verified_payout_restart(tmp_path, monkeypatch, boundary, yes):
    instrument, _, _ = prepare(tmp_path)
    clock = clocked_app(monkeypatch, tmp_path, instrument, SIGNAL)
    with app() as request:
        ticket, payload = submit(request, instrument)
        request(LEDGER, payload=payload)
        received = parse_time(ticket["closes_at"]) + timedelta(minutes=5)
        payout(tmp_path, instrument, received, yes)
        clock[0] = received + timedelta(minutes=5)
        # Count only payout boundaries, so the first process comes online
        # after payout and first recovers the original entry.
        stopped = crash(tmp_path, instrument, clock[0], "payout:" + boundary)
        before = request(LEDGER)
        row, = before["orders"]
        settled = boundary == "_finalize"
        assert row["result"]["position"]["settled"] is settled
        root = tmp_path / "trade-paper"
        with sqlite3.connect(database(tmp_path)) as db:
            plans = db.execute("SELECT proposal FROM reconciliation").fetchall()
        planned = json.loads(plans[0][0])[2] if plans else paper_runs.load_run(root, row["result"]["run_id"])
        entry_id = planned["inputs"]["parent_run"]
        entry, entry_hash = verified(tmp_path, entry_id)
        assert entry["inputs"]["as_of"] == iso(RECHECK)
        child_file = paper_runs._path(root, planned["id"])
        child_bytes = child_file.read_bytes() if child_file.exists() else None
        monkeypatch.setattr(trade_orders, "_payout", lambda *_: pytest.fail("re-evaluated committed proposal"))
        step(tmp_path, instrument, clock[0] + timedelta(minutes=2))
        done = request(LEDGER)
        exact = request(LEDGER, payload=payload)
        assert exact == done["orders"][0]
    result = exact["result"]
    child, child_hash = verified(tmp_path, result["run_id"])
    position = result["position"]
    assert child["id"] == planned["id"] and child["inputs"]["parent_run"] == entry_id
    assert child["inputs"]["as_of"] == iso(received) and position["settled"] and position["payout"] == 200 * yes
    assert (position["quantity"], position["max_loss"]) == (200, 21.71)
    witness = next(b["settlement"] for e in child["inputs"]["evidence"]["events"] for b in e["buckets"] if b.get("settlement"))
    assert witness["receipt_id"] and len(witness["receipt_sha256"]) == 64
    assert done["cash"] == (678.29 if yes else 478.29) and done["committed"] == 0 and not done["exposure"]
    if child_bytes:
        assert child_file.read_bytes() == child_bytes
    assert verified(tmp_path, entry_id)[1] == entry_hash
    step(tmp_path, instrument, clock[0] + timedelta(minutes=3))
    assert trade_orders.orders(tmp_path, False) == done and verified(tmp_path, child["id"])[1] == child_hash
    assert len(list(child_file.parent.glob("*.json.gz"))) == 2
    print(stopped, "order", exact["id"], "entry", entry_id, entry_hash, "payout", child["id"], child_hash,
          "cash", done["cash"], "committed", done["committed"], "witness", witness["receipt_id"])


def test_read_only_snapshot_and_no_lock_during_model(tmp_path, monkeypatch):
    missing = tmp_path / "missing"
    monkeypatch.setattr(server, "DATA", missing)
    with app() as request:
        assert request(LEDGER) == {"orders": [], "cash": 500, "committed": 0, "exposure": [],
                                   "station_limit": 25, "active": False}
        assert not missing.exists()
    instrument, _, _ = prepare(tmp_path)
    clocked_app(monkeypatch, tmp_path, instrument, SIGNAL)
    with app() as request, ThreadPoolExecutor() as pool:
        _, first = submit(request, instrument)
        prior = request(LEDGER, payload=first)
        entered, release = Event(), Event()

        def predict(*_):
            entered.set()
            assert release.wait(5)
            return {instrument: {"probability": .8, "uncertain": False, "model_version": "controlled"}}

        work = pool.submit(trade_orders.reconcile, tmp_path, RECHECK, lambda: None, lambda _: predict)
        try:
            assert entered.wait(5)
            # No lookup, model or publication takes place in the GET handler,
            # and the second reservation can obtain the ledger writer lock.
            with monkeypatch.context() as guard:
                guard.setattr(server, "current_capture", lambda *_: pytest.fail("GET read a capture"))
                guard.setattr(server, "trial_probability_lookup", lambda *_: pytest.fail("GET loaded the model"))
                assert request(LEDGER)["orders"] == [prior]
                with trade_orders._ledger(tmp_path) as (db, _):
                    assert trade_orders._find(db, False, prior["id"])["status"] == "Pending recheck"
        finally:
            release.set()
        work.result(timeout=5)
        assert request(LEDGER)["orders"][0]["status"] == "Simulated"

        # Force a separate commit between allocation and the recent-orders
        # query. The GET still sees one coherent snapshot; WAL only used here
        # so the writer can complete while the reader is paused.
        with sqlite3.connect(database(tmp_path)) as db:
            db.execute("PRAGMA journal_mode=WAL")
        allocation = trade_orders._allocation
        seen, finish = Event(), Event()
        def between(db, fixture):
            accounts = allocation(db, fixture)
            seen.set()
            assert finish.wait(5)
            return accounts
        monkeypatch.setattr(trade_orders, "_allocation", between)
        reading = pool.submit(request, LEDGER)
        try:
            assert seen.wait(5)
            with trade_orders._ledger(tmp_path) as (db, _):
                db.execute("UPDATE orders SET status='Skipped',result=NULL")
        finally:
            finish.set()
        old = reading.result(timeout=5)
        assert old["cash"] == 478.29 and old["orders"][0]["status"] == "Simulated"
        now = request(LEDGER)
        assert now["cash"] == 500 and now["orders"][0]["status"] == "Skipped"


@pytest.mark.parametrize("race", ["identical", "different quote", "station room", "cash"])
def test_parallel_reservations_and_request_identity(tmp_path, monkeypatch, race):
    instrument, _, _ = prepare(tmp_path)
    clocked_app(monkeypatch, tmp_path, instrument, SIGNAL)
    with app() as request:
        ticket, one = submit(request, instrument)
        _, two = submit(request, instrument, budget=24 if race in ("different quote", "cash") else 25,
                        request_id=one["request_id"] if race in ("identical", "different quote") else None)
        if race == "cash":
            # Existing reviewed reservations across independent station-days
            # leave $30. The contenders each reserve $21.71 with distinct days.
            exemplar, witness = server._trade_ticket(False, {}, (POLY, instrument, "YES"), 25)
            for amount in [25] * 18 + [20]:
                old = deepcopy(exemplar)
                old.update(station=str(uuid4()), order={**old["order"], "max_loss": amount})
                trade_orders.record(tmp_path, False, str(uuid4()), old, witness)

        barrier = Barrier(2)
        if race == "cash":
            orig = server._trade_ticket
            def distinct(fixture, params, selection, size):
                refreshed, witness = orig(fixture, params, selection, size)
                return {**refreshed, "station": f"disposable-station-{size}"}, witness
            monkeypatch.setattr(server, "_trade_ticket", distinct)

        def post(payload):
            barrier.wait()
            return request(LEDGER, payload=payload, expect=None)
        with ThreadPoolExecutor(2) as pool:
            replies = list(pool.map(post, (one, two)))
        assert sorted(status for status, _ in replies) == {
            "identical": [200, 200], "different quote": [200, 409], "station room": [200, 400], "cash": [200, 400],
        }[race]
        if race == "identical":
            assert replies[0] == replies[1]
        else:
            failure = next(item["error"] for status, item in replies if status != 200)
            assert {"different quote": "ID belongs", "station room": "station-day cap", "cash": "paper cash"}[race] in failure
        state = trade_orders.orders(tmp_path, False)
        assert state["committed"] == pytest.approx((470 if race == "cash" else 0) + ticket["order"]["max_loss"])
        assert len(state["orders"]) == (20 if race == "cash" else 1)


def test_unreadable_capture_and_failed_publication_are_retryable(tmp_path, monkeypatch):
    instrument, _, _ = prepare(tmp_path)
    clocked_app(monkeypatch, tmp_path, instrument, SIGNAL)
    with app() as request:
        ticket, payload = submit(request, instrument)
        request(LEDGER, payload=payload)
        # A second, isolated fixture order progresses despite the broken live capture.
        from test_trade import NO_BUCKET, probabilities, tape
        fixture, at = tape()
        rows, witness = trade.market_rows(tmp_path, fixture, probabilities, at, fixture=True,
                                          selected=(POLY, NO_BUCKET, "NO"), budget=12)
        trade_orders.record(tmp_path, True, payload["request_id"], rows[0], witness)
        latest = max((tmp_path / "captures").glob("*.json"))
        raw = latest.read_bytes()
        latest.write_text("{broken")
        issues = {}
        observe = lambda key, issue: issues.update({key: issue})
        trade_orders.reconcile(tmp_path, DEADLINE, lambda: fixture, lambda _: probabilities, report=observe)
        assert "Unreadable or malformed capture file" in issues[(0, payload["request_id"])]
        assert request(LEDGER)["committed"] == ticket["order"]["max_loss"]
        assert request(LEDGER, fixture=1)["orders"][0]["status"] == "Simulated"
        latest.write_bytes(raw)
        with monkeypatch.context() as fault:
            def fail(*args, **kwargs):
                raise OSError("disk publication stopped")
            fault.setattr(paper_runs, "store_run", fail)
            trade_orders.reconcile(tmp_path, DEADLINE, lambda: fixture,
                                   lambda _: lambda *_: {instrument: {"probability": .8, "model_version": "controlled"}},
                                   report=observe)
            assert issues[(0, payload["request_id"])] == "OSError: disk publication stopped"
            assert request(LEDGER)["orders"][0]["status"] == "Pending recheck"
        with sqlite3.connect(database(tmp_path)) as db:
            planned, = db.execute("SELECT proposal FROM reconciliation").fetchone()
        step(tmp_path, instrument, DEADLINE + timedelta(minutes=1))
        assert request(LEDGER)["orders"][0]["result"]["run_id"] == json.loads(planned)[2]["id"]


def test_reviewed_ridge_is_reloaded_after_rotation(tmp_path, monkeypatch):
    original = tmp_path / "model" / "model.joblib"
    digest = write_legacy(original, SIGNAL)
    _, saved = model_evidence.legacy_snapshot(tmp_path, original.read_bytes())
    proof = model_evidence.record(tmp_path, "legacy", digest, SIGNAL, "controlled", {})
    assert write_legacy(original, RECHECK) != digest
    monkeypatch.setattr(server, "DATA", tmp_path)
    monkeypatch.setattr(server, "trial_probability_lookup", lambda model: model)
    # Exercise both the modern full digest and a v2 row with the prefix alone.
    reviewed = {"model_method": "ridge", "model_version": f"knyc-ridge-{digest[:10]}"}
    for info in (reviewed, {**reviewed, "model_vintage": digest, "model_availability": proof}):
        model = server.paper_order_probability({"signal_info": info})
        assert model["legacy"]["vintage"] == digest and model["availability"][digest] == proof
    saved.unlink()
    with pytest.raises(OSError, match="Reviewed legacy model is unreadable"):
        server.paper_order_probability({"signal_info": reviewed})


def test_catch_up_freezes_same_entry_at_the_same_evidence_clock(tmp_path, monkeypatch):
    online, offline = tmp_path / "online", tmp_path / "offline"
    instrument, _, _ = prepare(online)
    clocked_app(monkeypatch, online, instrument, SIGNAL)
    with app() as request:
        _, payload = submit(request, instrument)
        request(LEDGER, payload=payload)
    copytree(online, offline)
    step(online, instrument, RECHECK)
    step(offline, instrument, DEADLINE + timedelta(hours=2))
    assert trade_orders.orders(online, False) == trade_orders.orders(offline, False)
    checked, = trade_orders.orders(offline, False)["orders"]
    assert checked["status"] == "Simulated"
    assert verified(offline, checked["result"]["run_id"])[0]["inputs"]["as_of"] == iso(RECHECK)


def test_late_archive_publication_keeps_v2_grace_but_releases_v3(tmp_path, monkeypatch):
    v3, v2 = tmp_path / "v3", tmp_path / "v2"
    instrument, _, _ = prepare(v3, "late")
    clocked_app(monkeypatch, v3, instrument, SIGNAL)
    with app() as request:
        _, payload = submit(request, instrument)
        request(LEDGER, payload=payload)
    copytree(v3, v2)
    with trade_orders._ledger(v2) as (db, _):
        row = trade_orders._find(db, False, payload["request_id"])
        intent = json.loads(row["intent"])
        for key in ("policy", "clock_basis", "fee_schedule", "fee_clock", "selection"):
            intent.pop(key)
        intent["ticket"].pop("review_key")
        db.execute("UPDATE orders SET intent=?", (json.dumps(intent),))
    released = DEADLINE + timedelta(seconds=1)
    step(v3, instrument, released)
    modern = trade_orders.orders(v3, False)
    assert (modern["orders"][0]["status"], modern["cash"], modern["committed"], modern["exposure"]) == (
        "Skipped", 500, 0, [],
    )
    assert modern["orders"][0]["result"]["reason"] == "Missing eligible recheck"
    assert verified(v3, modern["orders"][0]["result"]["run_id"])[0]["inputs"]["as_of"] == iso(DEADLINE)

    delayed = max((v2 / "captures").glob("*.json"))
    away = delayed.rename(v2 / delayed.name)
    step(v2, instrument, DEADLINE)
    assert trade_orders.orders(v2, False)["orders"][0]["status"] == "Pending recheck"
    away.rename(delayed)
    step(v2, instrument, released)
    monkeypatch.setattr(server, "DATA", v2)
    with app() as request:
        order = request(LEDGER, payload={**payload, "review_key": "legacy-key-cannot-be-compared"})
    assert order["status"] == "Simulated" and order["result"]["position"]["opened_at"] == iso(RECHECK)
    assert verified(v2, order["result"]["run_id"])[0]["inputs"]["as_of"] == iso(DEADLINE)
    print("late order", payload["request_id"], "v3", modern["orders"][0]["result"]["run_id"],
          "cash", modern["cash"], "committed", modern["committed"], "v2", order["result"]["run_id"])


if __name__ == "__main__":
    data, instrument, clock, boundary, payload = sys.argv[1:]
    data, at = Path(data), parse_time(clock)
    if boundary == "record":
        clocked_app(pytest.MonkeyPatch(), data, instrument, at)
        trade_orders.record = interrupt_after(trade_orders.record, "record")
        with app() as request:
            request(LEDGER, payload=json.loads(payload))
    else:
        payout_only, _, name = boundary.rpartition(":")
        original = getattr(trade_orders, name)
        if payout_only:
            pending = interrupt_after(original, name)
            calls = [0]
            def chosen(*args, **kwargs):
                calls[0] += 1
                return pending(*args, **kwargs) if calls[0] == 2 else original(*args, **kwargs)
            setattr(trade_orders, name, chosen)
        else:
            setattr(trade_orders, name, interrupt_after(original, name))
        step(data, instrument, at)
    raise AssertionError(f"Process did not exit at {boundary}")
