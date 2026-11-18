from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

import pytest

from test_historical_http import app
from test_listing_evidence import T0, poly_fixture
from weather_edge import backtest, capture, paper_eligibility, paper_runs, server, strategy_native_capture, trade_orders
from weather_edge.market import iso
from weather_edge.poly_fee_policy import FEE_CHANGE
from weather_edge.venue_archive import POLY


SIGNAL = T0 + timedelta(seconds=10)
RECHECK = SIGNAL + timedelta(seconds=31)
DEADLINE = SIGNAL + timedelta(seconds=45)
THROUGH = DEADLINE + timedelta(seconds=30)
SETTINGS = {"strategy": "two-sided", "size": 25, "reaction": 30}


def prepare(data, case="supported", *, signal=SIGNAL, recheck=RECHECK):
    native, instrument, listing, first, _ = poly_fixture(data)
    reviewed = deepcopy(native)
    if signal < FEE_CHANGE:
        reviewed["markets"][0].update(startDate=iso(signal - timedelta(hours=2)), feeCoefficient=.0695)
        reviewed["startDate"] = reviewed["markets"][0]["startDate"]
        for other in reviewed["markets"][1:]:
            other["startDate"] = reviewed["startDate"]
    if case == "before-open":
        reviewed["markets"][0]["startDate"] = iso(signal + timedelta(seconds=1))
    if case == "cutoff":
        reviewed["endDate"] = iso(recheck)
    if case == "fee":
        reviewed["markets"][0]["feeCoefficient"] = .125
    if case == "extension":
        shortened = deepcopy(native)
        shortened["endDate"] = iso(signal - timedelta(seconds=1))
        listing(signal - timedelta(seconds=8), shortened)
    signal_listing = listing(signal - timedelta(seconds=5), reviewed)
    if reviewed == native and case != "extension":
        signal_listing = first
    events, issues = strategy_native_capture.polymarket_events(
        data, max(signal, T0 + timedelta(seconds=4)), family="daily-high",
    )
    assert len(events) == 1 and not issues
    event = events[0]
    if case == "extension":
        event["ends_at"] = iso(signal - timedelta(seconds=1))
    if case in {"fee-revision", "identity-revision", "schedule-update"}:
        revised = deepcopy(reviewed)
        if case == "identity-revision":
            revised["markets"][0]["marketSides"][0]["id"] = 9876
        else:
            revised["markets"][0]["feeCoefficient"] = .095 if case == "schedule-update" else .125
        listing(recheck - timedelta(seconds=1), revised)
    # Use real capture files and their completed clocks. A deliberately later
    # publication changes availability while preserving the exact HTTP receipt.
    available = recheck + timedelta(seconds=15) if case == "late" else recheck
    snapshots = []
    tape = [(signal, signal, signal), (recheck, available, recheck)]
    if case == "regression":
        intermediate = signal + timedelta(seconds=25)
        tape = [(signal, signal, signal), (intermediate, intermediate, intermediate),
                (recheck, available, signal + timedelta(seconds=5))]
    for receipt, completed, image in tape:
        snap = deepcopy(event)
        for bucket in snap["buckets"]:
            bucket.pop("listing_revisions", None)
            bucket["books"] = [] if bucket["id"] != instrument else [{
                "at": iso(receipt), "source_at": iso(image), "state": "MARKET_STATE_OPEN",
                "depth_kind": "displayed_ladder", "http_cache": {"cf-cache-status": "MISS"},
                "bids": [["0.04", "200"]], "offers": [["0.10" if signal >= FEE_CHANGE else "0.50", "200"]],
            }]
        record = {"kind": "live", "version": capture.SCHEMA_VERSION, "started_at": iso(signal),
                  "captured_at": iso(completed), "rounds": [], "grids": [], "clouds": [], "winds": [],
                  "issues": [], "events": [snap]}
        capture.write_capture(record, data / "captures")
        snapshots.append(record)
    return instrument, signal_listing, snapshots


def clocked_app(monkeypatch, data, instrument, at, *, info=None):
    reading = [at]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=timezone.utc):
            return reading[0].astimezone(tz)

    estimate = info or {"probability": .8, "uncertain": False, "model_version": "controlled"}
    def predict(*_):
        return {instrument: estimate}

    monkeypatch.setattr(server, "DATA", data)
    monkeypatch.setattr(server, "datetime", Clock)
    monkeypatch.setattr(server, "trial_probability_lookup", lambda *_: predict)
    monkeypatch.setattr(server, "backtest_probability_lookup", lambda: predict)
    monkeypatch.setattr(server, "HistoricalModels", lambda: predict)
    return reading


def record_order(request, ticket, selection):
    return request("/api/trade/orders", payload={
        **selection, "request_id": str(uuid4()), "review_key": ticket["review_key"],
    })


def reconcile(data, at):
    trade_orders.reconcile(data, at, lambda: None, lambda _: server.trial_probability_lookup(None))


@pytest.mark.parametrize(("case", "reason", "phase"), [
    ("before-open", "Market upcoming", "signal"),
    ("cutoff", "Listed event window ended", "recheck"),
    ("fee", "Listed fee differs from the retained fee schedule", "signal"),
    ("late", "Book became available after the recheck deadline", "recheck"),
    ("regression", "Regressing or conflicting exchange timestamp", "recheck"),
    ("supported", None, None),
    ("extension", None, None),
])
def test_strict_trade_and_backtest_share_evidence(tmp_path, monkeypatch, case, reason, phase):
    instrument, receipt, _ = prepare(tmp_path, case)
    clock = clocked_app(monkeypatch, tmp_path, instrument, SIGNAL)
    selection = {"venue": POLY, "instrument": instrument, "side": "YES", "budget": 25}
    day = SIGNAL.date().isoformat()
    with app() as request:
        signal = request("/api/trade", **selection)
        assert signal["posted"]["price"] == .10
        assert signal["listing_evidence"]["receipt_id"] == receipt
        assert signal["paper_evidence"]["phase"] == "signal"
        if phase == "signal":
            assert signal["blocker"] == reason and not signal["order"]
            assert "review_key" not in signal
        else:
            assert signal["blocker"] is None
            order = record_order(request, signal, selection)
            assert order["policy"] == paper_eligibility.POLICY
        clock[0] = RECHECK
        delayed = request("/api/trade", **selection)
        if case == "cutoff":
            assert delayed["blocker"] == reason and delayed["closes_at"] == iso(RECHECK)
        if case == "late":
            assert delayed["quote_at"] == iso(SIGNAL)
        clock[0] = THROUGH
        result = request("/api/backtests", payload={**SETTINGS, "start": day, "end": day})
        assert request("/api/backtests/" + result["id"]) == result
        assert result["engine"] == paper_runs.ENGINE and result["clock_basis"] == paper_eligibility.APP_CLOCK
        assert result["evaluation"]["freshness"] == "origin-confirmed"
        run = paper_runs.load_run(backtest.folder(tmp_path, False), result["id"])
        assert paper_runs.verify_run(run) == run["result"]
        if phase == "signal":
            rejected = next(r for r in run["result"]["signal_rejections"] if r["side"] == "YES" and r["at"] == iso(SIGNAL))
            assert rejected["reason"] == reason and rejected["posted_price"] == .10
        else:
            reconcile(tmp_path, clock[0])
            checked = request("/api/trade/orders")["orders"][0]
            attempt, *_ = [a for a in run["result"]["attempts"] if a["signal_at"] == iso(SIGNAL)]
            # Backtest can explain why an already known late receipt was
            # excluded. The ledger freezes only what was visible by deadline.
            assert checked["result"]["reason"] == ("Missing eligible recheck" if case == "late" else attempt["result"])
            if phase == "recheck":
                gap, = attempt["recheck_rejections"]
                assert gap["reason"] == reason and gap["book_at"] == iso(RECHECK)
                assert gap["phase"] == "recheck" and not attempt["reaction_at"]
                if case == "cutoff":
                    assert gap["closes_at"] == iso(RECHECK)
                elif case == "late":
                    assert gap["book_available_at"] > iso(DEADLINE)
                assert checked["status"] == "Skipped"
            else:
                assert checked["status"] == "Simulated"
                fill, = run["result"]["positions"]
                assert fill["quantity"] == signal["order"]["quantity"] == 200
                assert fill["entry_price"] == signal["order"]["highest_price"] == .10
                assert fill["max_loss"] == 21.71
        assert result["fills"] == int(reason is None)
        if case == "supported":
            inputs = run["inputs"]
            limited = paper_runs.create_run(
                inputs["evidence"], lambda event, at: inputs["probabilities"][event["id"]][iso(at)],
                order_size=25, reaction_seconds=30, bankroll=15, max_station_day=12,
            )
            position, = paper_runs.verify_run(limited)["positions"]
            assert position["max_loss"] <= 12 < 15
            assert position["quantity"] < signal["order"]["quantity"]


@pytest.mark.parametrize(("case", "reason"), [
    ("schedule-update", None),
    ("fee-revision", "Listed fee differs from the retained fee schedule"),
    ("identity-revision", "Listing disagrees with the exact event, market or side identity"),
])
def test_recheck_checks_new_terms_and_allocates_at_reviewed_rate(tmp_path, monkeypatch, case, reason):
    start = FEE_CHANGE - timedelta(seconds=20)
    later = start + timedelta(seconds=31)
    instrument, _, _ = prepare(tmp_path, case, signal=start, recheck=later)
    clock = clocked_app(monkeypatch, tmp_path, instrument, start)
    selection = {"venue": POLY, "instrument": instrument, "side": "YES", "budget": 25}
    with app() as request:
        ticket = request("/api/trade", **selection)
        assert ticket["blocker"] is None and ticket["fee_evidence"]["policy_coefficient"] == "0.0695"
        record_order(request, ticket, selection)
        clock[0] = later + timedelta(seconds=30)
        reconcile(tmp_path, clock[0])
        order = request("/api/trade/orders")["orders"][0]
        attempt = order["result"]["attempt"]
        if reason:
            assert order["status"] == "Skipped" and order["result"]["reason"] == reason
            assert attempt["recheck_rejections"][0]["book_at"] == iso(later)
        else:
            assert order["status"] == "Simulated"
            fill = order["result"]["position"]
            assert attempt["coefficient"] == attempt["reaction_quote"]["coefficient"] == fill["coefficient"] == .0695
            assert fill["quantity"] <= ticket["order"]["quantity"]
            assert fill["max_loss"] <= ticket["order"]["max_loss"] <= 25
            assert fill["fee"] == ticket["order"]["fee"] and fill["opened_at"] == iso(later)


def test_pending_legacy_intent_keeps_original_clocks_and_digest_prefix(tmp_path, monkeypatch):
    instrument, _, _ = prepare(tmp_path)
    digest = "fd2b26762f" + "8" * 54
    version = "knyc-ridge-" + digest[:10]
    estimate = {"model_method": "ridge", "model_version": version, "model_vintage": digest, "probability": .8}
    clock = clocked_app(monkeypatch, tmp_path, instrument, SIGNAL, info=estimate)
    selection = {"venue": POLY, "instrument": instrument, "side": "YES", "budget": 25}
    with app() as request:
        pending = record_order(request, request("/api/trade", **selection), selection)
        with trade_orders._ledger(tmp_path) as (db, _):
            row = trade_orders._find(db, False, pending["id"])
            intent = json.loads(row["intent"])
            for key in ("policy", "clock_basis", "fee_schedule", "fee_clock", "selection"):
                intent.pop(key)
            intent["signal_info"].pop("model_vintage")
            intent["station_room"] = 12
            db.execute("UPDATE orders SET intent=? WHERE request_id=?", (json.dumps(intent), pending["id"]))
        clock[0] = THROUGH
        reconcile(tmp_path, clock[0])
        order = request("/api/trade/orders")["orders"][0]
    assert order["status"] == "Simulated"
    checked = paper_runs.load_run(tmp_path / "trade-paper", order["result"]["run_id"])
    frozen = checked["inputs"]
    position, = paper_runs.verify_run(checked)["positions"]
    assert frozen["engine"] == paper_runs.LEGACY_ENGINE
    assert frozen["settings"]["selection"]["at"] == iso(SIGNAL) and frozen["as_of"] == iso(DEADLINE)
    assert order["reaction_due_at"] == iso(SIGNAL + timedelta(seconds=30))
    assert position["opened_at"] == iso(RECHECK) and position["max_loss"] <= 12
    assert position["model_vintage"] == digest and position["model_version"] == version


def test_evaluate_routes_save_distinct_result_and_explain_missing_evidence(tmp_path, monkeypatch):
    instrument, _, _ = prepare(tmp_path)
    monkeypatch.setattr(server, "DATA", tmp_path)
    tape = capture.load_captures(tmp_path / "captures")
    for event in tape["events"]:
        for bucket in event["buckets"]:
            for book in bucket["books"]:
                book.pop("available_at")
    evaluation = {"strategy": "two-sided", "freshness": "origin-confirmed", "max_exchange_age": 120,
                  "source": "range", "from": iso(SIGNAL), "through": iso(THROUGH),
                  "archive_station_days": [], "receipt_issues": {}}
    legacy = paper_runs.create_run(
        tape, lambda *_: {instrument: {"probability": .8}}, engine=paper_runs.LEGACY_ENGINE,
        reaction_seconds=30, evaluation=evaluation,
    )
    root = backtest.folder(tmp_path, False)
    paper_runs.store_run(root, legacy)
    file = paper_runs._path(root, legacy["id"])
    original_bytes = file.read_bytes()
    with app() as request:
        original = request("/api/backtests/" + legacy["id"])
        result = request("/api/backtests/" + legacy["id"] + "/evaluate", payload={})
        assert request("/api/backtests/" + result["id"]) == result
        assert request("/api/backtests/" + legacy["id"]) == original
    assert file.read_bytes() == original_bytes
    assert result["source_run"] == legacy["id"] != result["id"]
    assert result["comparison"]["fills"] == [1, 0]
    assert result["evidence_gaps"]["books_without_first_app_availability"] == 2
    assert result["evidence_gaps"]["instruments_without_retained_listing_revisions"] == [instrument]
    assert paper_runs.verify_run(paper_runs.load_run(root, result["id"]))["fills"] == 0
