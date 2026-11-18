from copy import deepcopy
from datetime import timedelta
from decimal import Decimal

import pytest

from weather_edge.capture_fixture import fixture_capture
from weather_edge.market import iso, paper_trial, parse_time, scan
from weather_edge.paper_runs import create_run, verify_run
from weather_edge.strategy_clock_sensitivity import ClockRule
from weather_edge.strategy_native_capture import native_event
from weather_edge.strategy_rules import StrategyRule, book_status, distinct_observations, recent_observation, temperature_window
from weather_edge.strategy_runs import MarketAnchor


def book(at, *, cache="EXPIRED", age=None, bid="0.4", offer="0.6"):
    return {
        "at": iso(at), "source_at": iso(at), "depth_kind": "displayed_ladder", "state": "MARKET_STATE_OPEN",
        "source_time_progress": "advanced", "cache_age_seconds": age, "http_cache": {"cf-cache-status": cache},
        "bids": [[bid, "100"]], "offers": [[offer, "100"]],
    }


def test_legacy_accepts_headerless_cache_but_origin_confirmed_requires_positive_evidence():
    now = parse_time("2026-11-15T17:00:00Z")
    cached = book(now, cache="HIT")
    assert book_status(cached, "YES", now, "legacy") is None
    assert book_status(cached, "YES", now, "origin-confirmed") == "Cached response without positive Age"
    assert book_status(book(now, cache="HIT", age=0), None, now, "origin-confirmed")
    assert book_status(book(now, cache="EXPIRED"), "YES", now, "origin-confirmed") is None
    assert book_status(book(now, cache="REVALIDATED"), "NO", now, "origin-confirmed") is None
    assert book_status(book(now, cache="EXPIRED", age=12), None, now, "origin-confirmed")
    headerless = book(now, cache=None)
    assert book_status(headerless, None, now, "origin-confirmed") == "No affirmative origin evidence"
    regressed = {**book(now), "source_time_progress": "regressed"}
    assert book_status(regressed, None, now, "origin-confirmed") == "Regressing or conflicting exchange timestamp"


def test_live_anchor_never_borrows_missing_or_future_other_bucket_prices():
    now = parse_time("2026-11-15T17:00:00Z")
    future = now + timedelta(seconds=20)
    buckets = [
        {"id": "under", "lower_f": None, "upper_f": 49, "books": [book(now, bid="0.4", offer="0.5")]},
        {"id": "over", "lower_f": 50, "upper_f": None, "books": [book(future, bid="0.5", offer="0.6")]},
    ]
    market = {"id": "series", "buckets": buckets}
    noaa = {"under": {"probability": 0.2}, "over": {"probability": 0.8}}
    anchor = MarketAnchor({"weather_weight": 0.25, "max_book_age_seconds": 30, "raw_mass": [0.8, 1.2]}, [market])
    assert anchor.apply(market, now, noaa) == noaa
    resolved = anchor.apply(market, future, noaa)
    assert resolved["under"]["probability"] == 0.3875
    assert resolved["over"]["model_method"] == "market-midpoint-noaa-blend"
    assert anchor.apply(market, now + timedelta(seconds=31), noaa) == noaa


def test_exchange_clock_sensitivity_preserves_native_nanosecond_advances_and_bounds():
    now = parse_time("2026-11-15T17:00:30Z")
    rule = ClockRule("two-sided", "origin-confirmed", 0.25, max_exchange_age=30, require_advance=True)
    signal = {**book(now), "source_at": "2026-11-15T17:00:00.000000001Z"}
    later = {**book(now), "source_at": "2026-11-15T17:00:00.000000002Z"}
    assert rule.recheck_status(signal, later) is None
    assert rule.recheck_status(signal, {**signal, "source_at": "2026-11-15T17:00:00.0000000010Z"})
    assert rule.depth_status(signal, "YES", now) is None
    assert rule.depth_status({**signal, "source_at": "2026-11-15T17:00:30.000000001Z"}, "YES", now)


def test_one_cash_ledger_and_station_day_cap_across_strategy_venues():
    capture = fixture_capture(duration_seconds=30, poll_seconds=8, max_rounds=8, max_events=12)
    source = capture["events"][0]
    copy = deepcopy(source)
    copy["id"] += "-other"
    copy["venue"] = "second-simulated-venue"
    capture["events"].append(copy)
    for event in capture["events"]:
        for bucket in event["buckets"]:
            for quote in bucket["books"]:
                quote["http_cache"] = {"cf-cache-status": "EXPIRED"}
    probabilities = (0.01, 0.03, 0.32, 0.44, 0.15, 0.05)

    def predict(event, _):
        return {b["id"]: {"probability": p} for b, p in zip(event["buckets"], probabilities, strict=True)}

    result = paper_trial(capture, predict, rules=StrategyRule("two-sided", "origin-confirmed", 0.25))
    assert result["fills"] == 1
    assert Decimal(str(result["cash"])) + Decimal(str(result["cash_committed"])) == 500
    assert result["cash_committed"] <= 25


def test_temperature_windows_normalize_strict_integer_bounds_and_reject_unknown_units():
    contract = {"units": "°F", "operator": "below", "lower": None, "upper": "49"}
    assert temperature_window(contract) == (None, 48)
    assert temperature_window({**contract, "operator": "at-most", "upper": "48"}) == (None, 48)
    assert temperature_window({**contract, "upper": "49.5"}) is None
    assert temperature_window({**contract, "units": "°C"}) is None


def test_strict_saved_trial_reproduces_and_scan_reports_unknown_age():
    capture = fixture_capture(duration_seconds=30, poll_seconds=8, max_rounds=8, max_events=12)
    probabilities = (0.01, 0.03, 0.32, 0.44, 0.15, 0.05)

    def predict(event, _):
        return {b["id"]: {"probability": p} for b, p in zip(event["buckets"], probabilities, strict=True)}

    legacy = create_run(capture, predict)
    assert legacy["result"]["fills"] == 1
    unsupported = create_run(capture, predict, freshness="origin-confirmed")
    assert verify_run(unsupported)["fills"] == 0
    for e in capture["events"]:
        for bucket in e["buckets"]:
            for quote in bucket["books"]:
                quote.update(http_cache={"cf-cache-status": "EXPIRED"}, cache_age_seconds=None)

    strict = create_run(capture, predict, freshness="origin-confirmed")
    assert verify_run(strict)["fills"] == 1
    assert strict["inputs"]["settings"]["freshness"] == "origin-confirmed"
    assert verify_run(legacy)["fills"] == 1
    event, = capture["events"]
    through = capture["captured_at"]
    rows = scan(capture, through, predict(event, through), freshness="origin-confirmed")
    assert rows[0]["response_cache_status"] == "EXPIRED"
    assert rows[0]["response_cache_age"] is None and rows[0]["effective_quote_age"] is None
    assert rows[0]["paper_evidence_gap"] is None


def test_native_low_overlap_is_withheld_without_mutating_contracts(monkeypatch):
    from weather_edge import venue_archive

    monkeypatch.setattr(venue_archive, "_book_query", lambda *args, **kwargs: [])
    start = "2026-11-15T05:00:00Z"
    end = "2026-11-16T05:00:00Z"
    common = {
        "event_id": "temp-nyclow-2026-11-15", "native_event_id": "1", "family": "daily-low",
        "station": "KNYC", "day": "2026-11-15", "closed_at": end, "climate_starts_at": start,
        "climate_ends_at": end, "resolution_source": "NWS Daily Climate Report", "opened_at": start,
        "units": "°F", "quantity_step": "1", "rules_primary": "Exact observed minimum", "native_status": "OPEN",
        "settlement_received_at": None, "yes_payout": None, "yes_side_id": "affirmative", "no_side_id": "negative",
    }
    left = {**common, "instrument_id": "left", "native_id": "10", "label": "at most 40",
            "operator": "at-most", "lower": None, "upper": "40"}
    right = {**common, "instrument_id": "right", "native_id": "11", "label": "at least 40",
             "operator": "at-least", "lower": "40", "upper": None}
    with pytest.raises(ValueError, match="complete exact bucket partition"):
        native_event(None, [left, right], None, parse_time(end))
    assert left["upper"] == right["lower"] == "40"


def test_delayed_mirror_does_not_reopen_observation_window_and_correction_survives():
    first = {"at": "2026-11-15T16:51:00Z", "available_at": "2026-11-15T16:56:22Z",
             "source": "Aviation Weather Center", "temp_f": 48.92,
             "report": "METAR KNYC 151651Z AUTO VRB05KT 10SM BKN044 09/M02 A3014 ="}
    blank = {**first, "source": "NWS", "available_at": "2026-11-15T17:43:05Z", "report": ""}
    copy = {**blank, "available_at": "2026-11-15T17:48:55Z",
            "report": "KNYC 151651Z AUTO VRB05KT 10SM BKN044 09/M02 A3014"}
    corrected = {**copy, "available_at": "2026-11-15T17:52:00Z", "temp_f": 50.0,
                 "report": "KNYC 151651Z AUTO VRB05KT 10SM BKN044 10/M02 A3014"}
    event = {"climate_starts_at": "2026-11-15T05:00:00Z", "observations": [copy, first, blank, corrected]}
    assert recent_observation(event, parse_time("2026-11-15T16:58:00Z")) == first
    assert recent_observation(event, parse_time("2026-11-15T17:50:00Z")) is None
    final = parse_time("2026-11-15T17:54:00Z")
    assert distinct_observations(event, final) == [first, corrected]
    assert recent_observation(event, final) == corrected
