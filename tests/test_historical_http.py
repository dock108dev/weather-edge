"""Exercise model evidence through the same HTTP handler used by the browser."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta
from hashlib import sha256
from http.client import HTTPConnection
from threading import Thread
from urllib.parse import urlencode
import json

import joblib
import pytest

from test_model_evidence import make_study
from weather_edge import backtest, model_evidence, paper_runs, server, venue_archive, venue_sources
from weather_edge.capture import write_capture
from weather_edge.capture_fixture import START, _event, fixture_capture
from weather_edge.market import iso, parse_time
from weather_edge.venue_contracts import POLY_API


class FixedRidge:
    def predict(self, X):
        return [-0.5] * len(X)


def write_legacy(path, cutoff):
    path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump({"schema_version": 1, "stations": ["KNYC"], "evidence_cutoff": server.iso(cutoff),
                 "ridge": FixedRidge(), "validation": {"all": {"residuals_f": [-2, -1, 0, 1, 2],
                                                              "days": 10, "baseline_sd_f": 2}}}, path)
    return sha256(path.read_bytes()).hexdigest()


def witness_fixture_listing(data, at):
    native = _event()
    for market in native["markets"]:
        market["startDate"] = native["startDate"]
    url = POLY_API + "/v1/events/slug/" + native["slug"]
    reply = venue_sources.Receipt(url, iso(at), iso(at), 200, {}, json.dumps({"event": native}).encode())
    with venue_archive.connect(data) as db, db:
        receipt = venue_archive.save_receipt(db, venue_archive.POLY, "event-detail", reply)
        venue_sources._apply_poly(db, reply, receipt, [native])


@contextmanager
def app():
    try:
        listener = server.ThreadingHTTPServer(("127.0.0.1", 0), server.UIHandler)
    except PermissionError:
        # Some managed shells forbid sockets. Dispatch the same raw requests
        # through BaseHTTPRequestHandler so API assertions still run there.
        from io import BytesIO
        from types import SimpleNamespace

        def request(path, *, payload=None, expect=200, **query):
            body = json.dumps(payload).encode() if payload is not None else b""
            action = "POST" if payload is not None else "GET"
            target = path + ("?" + urlencode(query) if query else "")
            raw = (f"{action} {target} HTTP/1.0\r\nHost: 127.0.0.1:8765\r\n"
                   f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n").encode() + body
            output = BytesIO()
            handler = object.__new__(server.UIHandler)
            handler.directory = str(server.BUILD)
            handler.server = SimpleNamespace(server_port=8765)
            handler.client_address = ("127.0.0.1", 0)
            handler.rfile = BytesIO(raw)
            handler.wfile = output
            handler.close_connection = True
            handler.handle_one_request()
            head, body = output.getvalue().split(b"\r\n\r\n", 1)
            status = int(head.split(b" ", 2)[1])
            assert expect is None or status == expect, (target, status, body)
            value = json.loads(body) if target.startswith("/api/") else body
            return (status, value) if expect is None else value

        yield request
        return

    with listener:
        thread = Thread(target=listener.serve_forever, daemon=True)
        thread.start()

        def request(path, *, payload=None, expect=200, **query):
            connection = HTTPConnection("127.0.0.1", listener.server_port, timeout=5)
            target = path + ("?" + urlencode(query) if query else "")
            try:
                connection.request("GET" if payload is None else "POST", target,
                                   body=None if payload is None else json.dumps(payload),
                                   headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                body = response.read()
                assert expect is None or response.status == expect, (target, response.status, body)
                value = json.loads(body) if target.startswith("/api/") else body
                return (response.status, value) if expect is None else value
            finally:
                connection.close()

        try:
            yield request
        finally:
            listener.shutdown()
            thread.join()


@pytest.fixture
def retained_case(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DATA", tmp_path)
    record = fixture_capture(duration_seconds=30, poll_seconds=8, max_rounds=8, max_events=12)
    record["kind"] = "live"
    event, = record["events"]
    bucket = event["buckets"][2]
    for contract in event["buckets"]:
        for book in contract["books"]:
            book["http_cache"] = {"cf-cache-status": "MISS"}
    old = START + timedelta(seconds=7)
    witness = START + timedelta(seconds=8, microseconds=1)
    cutoff = START + timedelta(seconds=9)
    later = START + timedelta(seconds=16)
    report = START + timedelta(seconds=10)
    payout = START + timedelta(seconds=14)
    bucket["prices"] = [{"yes": .95, "no": .10, "at": server.iso(old), "available_at": server.iso(old)},
                        {"yes": .99, "no": .05, "at": server.iso(later), "available_at": server.iso(later)}]
    event["station_name"] = "Central Park"
    event["settlement"] = {"available_at": server.iso(report), "high_f": 50, "winning_bucket": bucket["id"],
                           "payout_available_at": server.iso(payout), "yes_payout": 1}
    path = tmp_path / "archive" / event["date"] / "KNYC.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(event))
    witness_fixture_listing(tmp_path, START - timedelta(seconds=1))
    # Witness the individual publications independently of the later exact
    # capture, whose book set still contains all the original receipts.
    for at in sorted({book["at"] for book in bucket["books"]}, key=parse_time):
        snapshot = deepcopy(record)
        snapshot["captured_at"] = at
        for contract in snapshot["events"][0]["buckets"]:
            contract["books"] = [book for book in contract["books"] if book["at"] == at]
        write_capture(snapshot, tmp_path / "captures")
    write_capture(record, tmp_path / "captures")
    return event, bucket, record, old, witness, cutoff, later, report, payout


def test_ticket_weather_pinned_capture_and_frozen_saves_share_one_model_clock(tmp_path, retained_case):
    event, bucket, record, old, witness, cutoff, later, report, payout = retained_case
    legacy = tmp_path / "model" / "model.joblib"
    vintage = write_legacy(legacy, cutoff)
    _, snapshot = model_evidence.legacy_snapshot(tmp_path, legacy.read_bytes())
    receipt = model_evidence.record(tmp_path, "legacy", vintage, witness, "local-observation",
                                    {"artifact": str(snapshot.relative_to(tmp_path)), "artifact_sha256": vintage})
    ident = {"venue": "polymarket-us", "instrument": bucket["id"], "day": event["date"]}
    with app() as get:
        prior = get("/api/trade", archived=1, side="YES", at=server.iso(old), **ident)
        assert prior["posted"]["price"] == .95
        assert prior["probability"] is None and prior["order"] is None
        assert prior["model_gap"] == "Model availability unverified at this clock"
        mode = {"mode": "replay", "event": event["id"], **ident}
        assert get("/api/decision", at=server.iso(old), **mode)["decision"] is None
        assert get("/api/decision", at=server.iso(witness), **mode)["decision"] is None
        assert get("/api/decision", mode="live", event=event["id"], at=server.iso(old), **ident)["decision"] is None
        pinned = get("/api/trade", side="YES", archived=1, history_mode="live", at=server.iso(old), **ident)
        assert pinned["probability"] is None and pinned["order"] is None

        # Advancing valid time beyond both the model and settlement clocks
        # must leave the exact ticket priced at its origin.
        forecast = get("/api/trade", forecast=1, forecast_valid=server.iso(later), side="YES",
                       at=server.iso(old), **ident)
        assert forecast == prior
        station, = get("/api/session", mode="replay", **ident)["events"]
        assert station["observations"] and station["forecasts"]
        assert station["settlement"]["available_at"] == server.iso(report)
        assert station["settlement"]["payout_available_at"] == server.iso(payout)
        assert all(server.stamp(item["at"]) >= cutoff for item in station["decisions"])

        after = get("/api/trade", archived=1, side="YES", at=server.iso(later), **ident)
        decision = get("/api/decision", at=server.iso(later), **mode)["decision"]
        eligible = server.backtest_probability_lookup()(event, later)[bucket["id"]]
        assert after["probability"] == eligible["probability"] == decision["buckets"][2]["probability"]
        assert after["model_vintage"] == decision["vintage"] == vintage
        assert after["model_availability"] == decision["model_availability"] == receipt
        assert after["posted"]["price"] == .99

        def save_through(clock):
            result = get("/api/runs", payload={"at": server.iso(clock), "size": 25, "reaction": 5})
            return paper_runs.load_run(tmp_path, result["id"])

        before_run, after_run = save_through(old), save_through(later)
        assert before_run["inputs"]["probabilities"][event["id"]] == {record["started_at"]: {}}
        assert before_run["result"]["fills"] == 0
        assert after_run["inputs"]["probabilities"][event["id"]][iso(later)][bucket["id"]]["model_vintage"] == vintage
        assert paper_runs.verify_run(after_run) == after_run["result"]
        backtest_result = get("/api/backtests", payload={"capture_at": record["captured_at"], "strategy": "two-sided",
                                                          "size": 25, "reaction": 5})
        replayed = paper_runs.load_run(backtest.folder(tmp_path, False), backtest_result["id"])
        assert replayed["inputs"]["probabilities"][event["id"]][record["started_at"]] == {}
        assert replayed["inputs"]["probabilities"][event["id"]][iso(later)][bucket["id"]] == eligible
        assert get("/api/backtests/" + replayed["id"]) == backtest_result


@pytest.mark.parametrize("failure", ["EOFError", "ValueError"], ids=["empty-file", "invalid-estimator"])
def test_corrupt_optional_artifact_does_not_block_startup_or_recovery(tmp_path, retained_case, monkeypatch, caplog, failure):
    import sys

    event, bucket, _, old, witness, cutoff, later, _, _ = retained_case
    legacy = tmp_path / "model" / "model.joblib"
    legacy.parent.mkdir()
    if failure == "EOFError":
        legacy.touch()
    else:
        write_legacy(legacy, cutoff)
        malformed = joblib.load(legacy)
        malformed["ridge"] = None
        joblib.dump(malformed, legacy)
    selected = server.study.study_dir(tmp_path) / "selected.joblib"
    blob = make_study(selected, cutoff)
    model_vintage = sha256(blob).hexdigest()
    archived = selected.parent / "vintages" / model_vintage / selected.name
    model_evidence.snapshot(archived, blob, model_vintage)
    receipt = model_evidence.record(tmp_path, "study", model_vintage, witness, "local-observation", {})
    build = tmp_path / "dist"
    build.mkdir()
    (build / "index.html").write_text("<title>Weather Edge</title>")
    monkeypatch.setattr(server, "BUILD", build)
    monkeypatch.setattr(sys, "argv", ["server", "--port", "18765", "--no-collect"])
    monkeypatch.setattr(server.collector, "start", lambda _data: pytest.fail("Disposable instance started collection"))
    real_listener = server.ThreadingHTTPServer
    started = False

    class StartupProbe:
        def __init__(self, address, handler):
            nonlocal started
            assert address == ("127.0.0.1", 18765) and handler is server.UIHandler
            started = True
            self.server_port = address[1]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def serve_forever(self):
            # Start the HTTP fixture only after the production startup scan.
            monkeypatch.setattr(server, "ThreadingHTTPServer", real_listener)
            with app() as get:
                assert b"Weather Edge" in get("/")
                ident = {"venue": "polymarket-us", "instrument": bucket["id"], "day": event["date"]}
                station, = get("/api/session", mode="replay", **ident)["events"]
                assert station["forecasts"] and station["observations"]
                assert get("/api/trade", archived=1, side="YES", at=server.iso(old), **ident)["posted"]["price"] == .95
                healthy = get("/api/trade", archived=1, side="YES", at=server.iso(later), **ident)
                assert healthy["probability"] is not None and healthy["model_vintage"] == model_vintage
                assert healthy["model_availability"] == receipt

    monkeypatch.setattr(server, "ThreadingHTTPServer", StartupProbe)
    server.main()
    assert started
    assert f"legacy model unavailable ({failure}): model.joblib" in caplog.text
    assert "legacy" not in server.active_model()
    vintage = write_legacy(legacy, cutoff)
    recovered = server.active_model()
    assert recovered["legacy_vintage"] == vintage
    assert (tmp_path / "model" / "vintages" / f"{vintage}.joblib").read_bytes() == legacy.read_bytes()
    observed = recovered["availability"][vintage]
    assert observed["basis"] == "local-observation" and server.stamp(observed["available_at"]) > later
    assert server.active_model()["availability"][vintage] == observed
    # Disable the independent study for a direct assertion on the recovered family's estimate.
    decision = server.evidenced_inference({**recovered, "study": None}, event, later)
    assert decision["vintage"] == vintage
