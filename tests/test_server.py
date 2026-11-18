"""Integration checks for the data sent to normal and fixture browser sessions."""

from weather_edge import server
from weather_edge.capture import write_capture
from weather_edge.capture_fixture import fixture_capture


def test_fixture_cache_and_price_evidence_are_isolated(tmp_path, monkeypatch):
    record = fixture_capture(duration_seconds=30, poll_seconds=8, max_rounds=8, max_events=12)
    write_capture(record, tmp_path / "fixtures")
    monkeypatch.setattr(server, "DATA", tmp_path)
    monkeypatch.setattr(server, "GRID", tmp_path / "grids")
    monkeypatch.setattr(server, "CLOUDS", tmp_path / "clouds")
    monkeypatch.setattr(server, "active_model", lambda: None)

    empty = server.session_data("live")
    assert not empty["cached"]
    assert empty["events"] == []
    fixture = server.session_data("live", fixture=True)
    assert fixture["cached"] and fixture["fixture"]
    assert fixture["captured_at"] == record["captured_at"]
    assert fixture["grids"] == fixture["clouds"] == []
    event, = fixture["events"]
    assert event["station_name"] == "Central Park"
    bucket = event["buckets"][0]
    assert "books" not in bucket
    assert bucket["prices"][0]["yes"] is not None
    assert bucket["prices"][0]["at"] == record["started_at"]
    no_model = server.paper_data(True, server.stamp(fixture["captured_at"]), 25, 5)
    assert len(no_model["rows"]) == 12
    assert all(row["ev_dollars"] is None for row in no_model["rows"])
    assert no_model["trial"]["cash"] == 500


def test_illegal_quote_images_become_chart_gaps():
    book = {
        "depth_kind": "displayed_ladder",
        "bids": [["0.3", "10"]],
        "offers": [["0.4", "10"]],
        "state": "MARKET_STATE_OPEN",
        "source_at": "2026-11-15T17:10:00Z",
        "source_time_progress": "advanced",
    }
    assert server._screen_book(book) == 0.35
    assert server._screen_book({**book, "source_time_progress": "regressed"}) is None
    assert server._screen_book({**book, "bids": [["0.4", "10"]]}) is None
    assert server._screen_book({**book, "source_at": None}) is None
    assert server._screen_book({**book, "cache_age_seconds": 1}) is None


def test_compact_live_session_keeps_other_station_choices(monkeypatch):
    record = fixture_capture(duration_seconds=8, poll_seconds=8, max_rounds=1, max_events=1)
    event = record["events"][0]
    miami = {**event, "station": "KMIA", "id": "miami"}
    calls = []

    def full_detail(selected, *args, **kwargs):
        calls.append(selected["station"])
        return selected

    monkeypatch.setattr(server, "current_capture", lambda *args, **kwargs: {**record, "events": [event, miami]})
    monkeypatch.setattr(server.collector, "weather_events", lambda *_: [])
    monkeypatch.setattr(server, "_forecast_layers", lambda *_: {})
    monkeypatch.setattr(server, "active_model", lambda: None)
    monkeypatch.setattr(server, "_frontend_event", full_detail)
    session = server.session_data("live", compact=True, event_id="miami")
    nyc_stub, miami_full = session["events"]
    assert calls == ["KMIA"] and session["detail_event"] == "miami"
    assert nyc_stub["summary_only"] and nyc_stub["place"] == "New York City · Central Park"
    assert nyc_stub["buckets"][0] == {key: event["buckets"][0][key] for key in ("id", "label")}
    assert miami_full["observations"] == event["observations"]


def test_index_replay_keeps_first_receipt_and_later_revision_separate(monkeypatch):
    previous = {"at": "2026-11-15T18:40:00Z", "temperature_f": "49.10", "contributors": 8, "status": "normal",
                "received_at": "2026-11-15T18:44:02Z", "receipt_id": 1, "source": "index"}
    duplicate = {**previous, "received_at": "2026-11-15T18:48:10Z", "receipt_id": 2}
    revision = {**previous, "temperature_f": "48.91", "received_at": "2026-11-15T18:48:30Z", "receipt_id": 3}
    target = {**previous, "at": "2026-11-15T19:00:00Z", "received_at": "2026-11-15T19:04:30Z", "receipt_id": 4}
    monkeypatch.setattr(server.venue_index, "points", lambda *args, **kwargs: [previous, duplicate, revision, target])
    result = server._index_replay({"series_id": "KXTEMPNYCHS", "closed_at": target["at"]}, "2026-11-15T18:45:00Z")
    assert [row["receipt_id"] for row in result["points"]] == [1, 3, 4]
    assert result["captured_by_decision"] == previous
    assert result["target_point"] == target
