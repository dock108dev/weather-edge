"""Exercise the continuous archive's receipt and restart invariants."""
from datetime import datetime, timedelta, timezone
import json
import shutil
import sqlite3

import pytest

from weather_edge import capture, collector, venue_archive
from weather_edge.capture_fixture import START, _event, fixture_capture
from test_historical_http import app
from test_venue_archive import FIRST, SERIES, native_kalshi, reply, store


def test_weather_archive_retains_changes_at_their_first_receipt(tmp_path):
    observed = datetime(2026, 11, 15, 5, 51, tzinfo=timezone.utc)
    later = observed + timedelta(minutes=1)

    def record(value, receipt):
        return "KNYC", "2026-11-15", "observation", "NWS", observed, {
            "at": collector.iso(observed), "available_at": collector.iso(receipt), "source": "NWS", "temp_f": value,
        }

    assert collector.save_weather(tmp_path, [record(50, later)]) == 1
    assert collector.save_weather(tmp_path, [record(50, later + timedelta(minutes=1))]) == 0
    assert collector.save_weather(tmp_path, [record(51, later + timedelta(minutes=2))]) == 1
    assert collector.save_weather(tmp_path, [record(50, later + timedelta(minutes=3))]) == 1
    event, = collector.weather_events(tmp_path)
    assert event["weather_only"] and not event["buckets"]
    assert event["climate_starts_at"] == "2026-11-15T05:00:00Z"
    assert [item["temp_f"] for item in event["observations"]] == [50, 51, 50]
    assert event["observations"][0]["available_at"] == collector.iso(later)


def test_five_minute_reports_use_signed_tenths_and_measured_time():
    body = b"station,valid,metar\nMIA,2026-11-15 06:05,KMIA 150605Z AUTO 00000KT 10SM CLR M01/M02 A3000 RMK T10051022 MADISHF\n"
    now = datetime(2026, 11, 15, 6, 25, tzinfo=timezone.utc)
    rows = list(collector.high_frequency_readings(capture.Reply(body, now, {}), "https://example.invalid"))
    station, day, family, source, issued, reading = rows[0]
    assert (station, day, family, issued.minute) == ("KMIA", "2026-11-15", "research_observation", 5)
    assert reading["source"] == source == collector.IEM_HF
    assert reading["temp_f"] == pytest.approx(31.1)
    assert reading["dewpoint_f"] == pytest.approx(28.04)
    assert reading["available_at"] == "2026-11-15T06:25:00Z"


def test_daily_report_keeps_publication_and_receipt_and_rejects_provisional():
    clock = datetime(2026, 11, 15, 7, tzinfo=timezone.utc)
    published = "2026-11-15T06:17:00Z"
    body = (
        "000\nCDUS41 KOKX 150617\nCLINYC\n"
        "...THE CENTRAL PARK NY CLIMATE SUMMARY FOR NOVEMBER 14 2026...\n\n"
        "TEMPERATURE (F)\n YESTERDAY\n  MAXIMUM 46  340 PM\n  MINIMUM 38  706 AM\n"
        "PRECIPITATION (IN)\n YESTERDAY  T      2.23\n"
    )
    entry = {"id": "test", "issuanceTime": published}

    def report(text):
        packet = {**entry, "productCode": "CLI", "productText": text}
        return capture.Reply(json.dumps(packet).encode(), clock, {})

    *_, value = collector.daily_report("KNYC", entry, report(body), "https://example.invalid")
    assert value["published_at"] == published
    assert value["available_at"] == "2026-11-15T07:00:00Z"
    assert (value["high_f"], value["low_f"], value["precipitation_inches_as_reported"]) == (46, 38, "T")
    assert collector.daily_report("KNYC", entry, report(body.replace(" YESTERDAY", " TODAY")), "https://example.invalid") is None


def test_collector_stop_persists_and_blocks_transport(tmp_path, monkeypatch):
    class UnavailableTransport:
        def __init__(self, deadline):
            pass

        def __call__(self, *args, **kwargs):
            pytest.fail("A stopped collector must not send a new request")

    monkeypatch.setattr(collector, "ReadOnlyHTTP", UnavailableTransport)
    assert collector.stop(tmp_path)["state"] == "stopped"
    assert collector.start(tmp_path)["state"] == "stopped"
    with pytest.raises(OSError, match="Collection stopped locally"):
        collector.tracked_http(tmp_path, 3)("https://example.invalid")


def test_scheduler_canonical_failure_recovery_and_independent_diagnostic(tmp_path, monkeypatch):
    from weather_edge import venue_sources

    source = collector.KALSHI_BOOKS
    task = collector.refresh_kalshi_books
    instant = [START]
    monkeypatch.setattr(collector, "now", lambda: instant[0])

    def success(*args, **kwargs):
        return {"usable": 1, "selected": 1, "targets": {"KXTEST": {
            "received_at": collector.iso(instant[0]), "available_at": collector.iso(instant[0]), "reason": None,
        }}}

    monkeypatch.setattr(venue_sources, "watch_once", success)
    collector._run_task(task, tmp_path, source)
    first = collector.source_status(tmp_path)[source]
    instant[0] += timedelta(seconds=15)

    def fail(*args, **kwargs):
        instant[0] += timedelta(seconds=2)
        raise RuntimeError("Injected book transport crash")

    monkeypatch.setattr(venue_sources, "watch_once", fail)
    collector._run_task(task, tmp_path, source)
    failed = collector.source_status(tmp_path)
    assert list(failed) == [source]
    assert failed[source]["duration_seconds"] == 2 and failed[source]["failures"] == 1
    assert failed[source]["last_success"] == first["last_success"]
    assert failed[source]["revision"] == first["revision"]
    assert failed[source]["issue"].endswith("Injected book transport crash")
    instant[0] += timedelta(seconds=15)
    monkeypatch.setattr(venue_sources, "watch_once", success)
    collector._run_task(task, tmp_path, source)
    recovered = collector.source_status(tmp_path)[source]
    assert recovered["issue"] is None and recovered["failures"] == 0
    assert recovered["last_error"].endswith("Injected book transport crash")
    assert recovered["last_successful_receipt_at"] == recovered["published_at"] == collector.iso(instant[0])
    collector.mark_source(tmp_path, source, outcome="unchanged")
    assert collector.source_status(tmp_path)[source]["revision"] == recovered["revision"]

    real_write = collector._write_source_status

    def blocked_write(data, name, **result):
        if name == source:
            raise sqlite3.OperationalError("Injected write failure")
        return real_write(data, name, **result)

    monkeypatch.setattr(collector, "_write_source_status", blocked_write)
    monkeypatch.setattr(venue_sources, "watch_once", fail)
    collector._run_task(task, tmp_path, source)
    collector._run_task(lambda data: collector.mark_source(data, collector.NDBC, count=1), tmp_path, collector.NDBC)
    assert collector.source_status(tmp_path)[collector.NDBC]["latest_rows"] == 1
    diagnostic = json.loads((tmp_path / "collector/status-errors.jsonl").read_text())
    assert diagnostic["source"] == source
    assert "Injected book transport crash" in diagnostic["issue"]
    assert "Injected write failure" in diagnostic["traceback"]


def test_reconcile_old_scheduler_row_preserves_recovery_and_failure_history(tmp_path, monkeypatch):
    clock = [START]
    monkeypatch.setattr(collector, "now", lambda: clock[0])
    collector.mark_source(tmp_path, "refresh_kalshi_books", error="Retained legacy failure")
    clock[0] += timedelta(seconds=15)
    collector.mark_source(tmp_path, collector.KALSHI_BOOKS, count=2, new=2)
    collector._reconcile_status(tmp_path, collector._schedule())
    rows = collector.source_status(tmp_path)
    assert list(rows) == [collector.KALSHI_BOOKS]
    row = rows[collector.KALSHI_BOOKS]
    assert row["issue"] is None and row["failures"] == 0 and row["total_new"] == 2
    assert row["last_error"] == "Retained legacy failure"
    assert row["legacy_issue"]["name"] == "refresh_kalshi_books"


def test_stop_survives_scheduler_restart_and_corrupt_source_read(tmp_path, monkeypatch):
    from weather_edge import server

    monkeypatch.setattr(server, "DATA", tmp_path)
    checked = []

    class OneCycle:
        ended = False

        def is_set(self):
            return self.ended

        def wait(self, _):
            self.ended = True

    monkeypatch.setattr(collector, "Event", OneCycle)
    monkeypatch.setattr(collector.signal, "signal", lambda *_: None)
    monkeypatch.setattr(collector, "_schedule", lambda: ((lambda data: checked.append(collector.paused(data)), 1, "Fake"),))
    collector.mark_source(tmp_path, "Fake", count=1)
    collector.mark_source(tmp_path, "Sibling", count=2)
    with collector.connection(tmp_path) as db:
        db.execute("UPDATE source_health SET body='{' WHERE name='Fake'")
    with app() as get:
        response = get("/api/collection", payload={"action": "stop"})
        assert response["state"] == "stopped" and response["enabled"] is False
        assert response["telemetry_issues"][0]["source"] == "Fake"
        assert response["sources"]["Sibling"]["latest_rows"] == 2
        collector.run(tmp_path)
        collector.run(tmp_path)
        assert checked == []
        assert get("/api/collection")["state"] == "stopped"
        monkeypatch.setattr(collector.subprocess, "Popen", lambda *_a, **_k: collector.run(tmp_path))
        restarted = get("/api/collection", payload={"action": "start"})
        assert restarted["enabled"] is True and not collector.paused(tmp_path)
        assert checked == [False]


def test_books_switch_to_verified_next_day_without_new_discovery(tmp_path, monkeypatch):
    old = _event()
    next_day = json.loads(json.dumps(old).replace("2026-11-16", "2026-11-17").replace("2026-11-15", "2026-11-16"))
    discovered = datetime(2026, 11, 16, 4, 58, tzinfo=timezone.utc)
    body = json.dumps({"events": [old, next_day]}).encode()
    monkeypatch.setattr(collector, "tracked_http", lambda *_: lambda _: capture.Reply(body, discovered, {}))
    collector.refresh_market_catalog(tmp_path)
    catalog = collector.read_json(collector.collect_path(tmp_path) / "listings.json")
    assert [row["id"] for row in catalog["selected"]] == [old["slug"]]
    assert len(catalog["candidates"]) == 2

    after = discovered + timedelta(minutes=2, seconds=1)
    requested, saved = [], []
    monkeypatch.setattr(collector, "now", lambda: after)
    monkeypatch.setattr(collector, "tracked_http", lambda *_: lambda _: capture.Reply(b"{}", after, {}, after))

    def book(_get, bucket, _clock):
        _get("https://example.invalid")
        requested.append(bucket["id"])
        return {"depth_kind": "displayed_ladder", "offers": [[".10", "5"]], "at": collector.iso(after)}

    monkeypatch.setattr(capture, "_fetch_book", book)
    monkeypatch.setattr(capture, "write_capture", lambda record, _: saved.append(record))
    collector.refresh_books(tmp_path)
    assert requested and all(next_day["slug"] in instrument for instrument in requested)
    assert [event["id"] for event in saved[0]["events"]] == [next_day["slug"]]
    status = collector.status(tmp_path)
    assert status["selected_markets"][0][-1] == next_day["slug"]
    batch = status["book_batches"][venue_archive.POLY][venue_archive.POLY_HIGH_PRIORITY]
    assert set(batch["targets"]) == set(requested) and batch["requested"] == batch["usable"] == len(requested)


def test_native_partial_batches_trace_exact_prices_and_requests(tmp_path, monkeypatch):
    from weather_edge import server, venue_archive, venue_sources
    from weather_edge.venue_contracts import kalshi_market

    at = collector.parse_time("2026-11-15T18:00:02Z")

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return at.astimezone(tz) if tz else at.replace(tzinfo=None)

    monkeypatch.setattr(collector, "now", lambda: at)
    monkeypatch.setattr(venue_sources, "clock", lambda: at)
    monkeypatch.setattr(server, "DATA", tmp_path)
    monkeypatch.setattr(server, "datetime", Clock)
    monkeypatch.setattr(server, "_trade_context", lambda *_a, **_k: (None, at))

    good, missing, outside = ("KXHIGHNY-26NOV15-" + suffix for suffix in ("B49.5", "B51.5", "B53.5"))
    low, rain = "test-native-low", "test-native-rain"
    for ticker in (good, missing, outside):
        native = native_kalshi(ticker)
        store(tmp_path, venue_archive.KALSHI, kalshi_market(SERIES, native, reply({}).source, FIRST), native)
    for instrument, family in ((low, "daily-low"), (rain, "daily-rain")):
        store(tmp_path, venue_archive.POLY, {
            "venue": venue_archive.POLY, "instrument_id": instrument, "event_id": instrument,
            "family": family, "station": "KNYC", "day": "2026-11-15", "status": "open",
            "opened_at": "2026-11-15T16:00:00Z", "closed_at": "2026-11-16T08:00:00Z",
        }, {"id": instrument})
    reply_at = at - timedelta(seconds=1)
    monkeypatch.setattr(venue_sources, "_kalshi_book_targets", lambda *_: [good, missing])
    monkeypatch.setattr(venue_sources.PublicGET, "__call__", lambda *_: reply(
        {"orderbooks": [{"ticker": good, "orderbook_fp": {"no_dollars": [["0.83", "25"]]}}]},
        collector.iso(reply_at)))
    monkeypatch.setattr(venue_sources.PublicGET, "_pace", lambda *_: None)
    rain_available = False

    def poly_get(url):
        instrument = url.split("/")[-2]
        payload = {"marketData": {"marketSlug": instrument, "transactTime": collector.iso(reply_at - timedelta(hours=2)),
                   "offers": [{"px": {"currency": "USD", "value": ".21"}, "qty": 40}]}} if (
                       instrument == low or rain_available) else {}
        return capture.Reply(json.dumps(payload).encode(), reply_at, {"cf-cache-status": "HIT"},
                             reply_at - timedelta(milliseconds=100))

    monkeypatch.setattr(venue_sources, "ReadOnlyHTTP", lambda _: poly_get)
    collector.refresh_kalshi_books(tmp_path)
    collector.refresh_poly_weather_books(tmp_path)
    with app() as get:
        response = get("/api/collection")
        for venue, scope, source, ids in (
            (venue_archive.KALSHI, venue_archive.KALSHI_PRIORITY, collector.KALSHI_BOOKS, (good, missing)),
            (venue_archive.POLY, venue_archive.POLY_LOW_RAIN_PRIORITY, collector.POLY_WEATHER_BOOKS, (low, rain)),
        ):
            batch = response["book_batches"][venue][scope]
            assert response["sources"][source]["outcome"] == "partial"
            assert (batch["selected"], batch["requested"], batch["usable"], batch["missing_or_invalid"]) == (2, 2, 1, 1)
            assert batch["targets"][ids[1]]["state"] == "missing_or_invalid"
            valid = batch["targets"][ids[0]]
            assert valid["received_at"] == collector.iso(reply_at) and valid["available_at"] == collector.iso(at)
            assert valid["source_at"] != valid["available_at"]
            ticket = get("/api/trade", venue=venue, instrument=ids[0], side="YES")
            assert ticket["book_lane"] == source and ticket["posted"]["kind"] == "Book offer"
            assert ticket["source_trace"]["automatic_selection"]["receipt_id"] == valid["receipt_id"]
            assert ticket["source_trace"]["recheck"]["state"] == "blocked"
            omitted = get("/api/trade", venue=venue, instrument=ids[1], side="YES")
            assert omitted["source_trace"]["automatic_selection"]["state"] == "missing_or_invalid"
        kalshi = response["book_batches"][venue_archive.KALSHI][venue_archive.KALSHI_PRIORITY]
        assert len({target["receipt_id"] for target in kalshi["targets"].values()}) == 1
        outside_ticket = get("/api/trade", venue=venue_archive.KALSHI, instrument=outside, side="YES")
        assert outside_ticket["source_trace"]["automatic_selection"]["state"] == "outside_sample"
        funnel = get("/api/trade", summary=1, funnel=1)["funnel"]
        coverage = funnel["instrument_coverage"][collector.KALSHI_BOOKS]
        assert (coverage["open_instruments"], coverage["requested"], coverage["outside_sample"]) == (3, 2, 1)
        assert funnel["venues"][venue_archive.KALSHI]["open_sides"] == 6
        assert get("/api/trade/book", payload={"venue": venue_archive.KALSHI, "instrument": good, "side": "YES"}) == {"received": 1}
        later = get("/api/collection")
        assert later["book_batches"][venue_archive.KALSHI][venue_archive.KALSHI_PRIORITY]["id"] == kalshi["id"]
        assert later["book_batches"][venue_archive.KALSHI][venue_archive.MANUAL_BOOK]["selected"] == 1
        at += timedelta(seconds=collector.BOOK_INTERVAL)
        reply_at = at - timedelta(seconds=1)
        rain_available = True
        collector.refresh_poly_weather_books(tmp_path)
        published = get("/api/collection")
        batch = published["book_batches"][venue_archive.POLY][venue_archive.POLY_LOW_RAIN_PRIORITY]
        assert batch["usable"] == 2
        assert published["sources"][collector.POLY_WEATHER_BOOKS]["outcome"] == "published"
        for instrument, family in ((low, "daily-low"), (rain, "daily-rain")):
            target = batch["targets"][instrument]
            assert target["family"] == family
            stored = next(book for book in venue_archive.books(tmp_path, venue_archive.POLY, instrument)
                          if book["receipt_id"] == target["receipt_id"])
            assert collector.parse_time(stored["at"]) <= collector.parse_time(stored["available_at"]) <= at
            assert collector.parse_time(stored["source_at"]) < collector.parse_time(stored["at"])


def test_capture_loader_deduplicates_identical_file_and_keeps_healthy_sessions(tmp_path):
    record = fixture_capture(duration_seconds=30, poll_seconds=8, max_rounds=8, max_events=12)
    path = capture.write_capture(record, tmp_path)
    shutil.copyfile(path, tmp_path / "copy.json")
    (tmp_path / "bad.json").write_text("{invalid")
    loaded = capture.load_captures(tmp_path, fixture=True)
    assert loaded["session_count"] == 1
    assert len(loaded["events"][0]["buckets"][0]["books"]) == 4
    assert any(issue["source"] == "bad.json" for issue in loaded["issues"])


def test_payout_queue_moves_previously_attempted_contracts_behind_unchecked(tmp_path):
    event, = fixture_capture(duration_seconds=30, poll_seconds=8, max_rounds=8, max_events=12)["events"]
    clock = START + timedelta(days=2)
    previous = {"events": [event]}
    target, *_ = collector._payout_queue(tmp_path, previous, clock)
    db = collector.connection(tmp_path)
    try:
        with db:
            db.execute("INSERT INTO settlement_checks VALUES (?,?)", (target[1], collector.iso(clock)))
    finally:
        db.close()
    assert collector._payout_queue(tmp_path, previous, clock)[-1] == target
