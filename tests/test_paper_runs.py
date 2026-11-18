from copy import deepcopy
from decimal import Decimal

import pytest

from weather_edge.capture import load_captures, write_capture
from weather_edge.capture_fixture import fixture_capture
from weather_edge.paper_runs import create_run, follow_payouts, list_runs, store_run, verify_run

OPTIONS = dict(duration_seconds=30, poll_seconds=8, max_rounds=8, max_events=12)


def fixture_probabilities(event, _):
    probabilities = (0.01, 0.03, 0.32, 0.44, 0.15, 0.05)
    return {
        b["id"]: {"probability": probability, "model_version": "fixture-frozen-v1",
                  "forecast_issued_at": event["forecasts"][0]["issued_at"]}
        for b, probability in zip(event["buckets"], probabilities, strict=True)
    }


def test_saved_trial_reproduces_from_frozen_probabilities_and_fee_schedule(tmp_path):
    capture = fixture_capture(**OPTIONS)
    first = store_run(tmp_path, create_run(capture, fixture_probabilities))
    assert first["result"]["fills"] == 1
    assert first["result"]["cash"] == 475
    assert first["result"]["cash_committed"] == 25
    assert any(a["reaction_ev_dollars"] < 0 for a in first["result"]["attempts"]
               if a["reaction_ev_dollars"] is not None)
    new = create_run(capture, lambda *_: {})
    assert new["id"] != first["id"] and new["result"]["fills"] == 0
    assert verify_run(first)["positions"][0]["model_version"] == "fixture-frozen-v1"
    assert store_run(tmp_path, create_run(capture, fixture_probabilities))["saved_at"] == first["saved_at"]
    assert len(list_runs(tmp_path, "fixture")) == 1


@pytest.mark.parametrize("yes_payout", [0, 0.5, 1])
def test_later_payout_keeps_entry_assumptions_reconciles_and_leaves_parent_unchanged(yes_payout):
    capture = fixture_capture(**OPTIONS)
    original = create_run(capture, fixture_probabilities)
    future = deepcopy(capture)
    future["captured_at"] = "2026-11-17T01:00:00Z"
    position, = original["result"]["positions"]
    for bucket in future["events"][0]["buckets"]:
        if bucket["id"] == position["bucket_id"]:
            bucket["settlement"] = {"yes_payout": yes_payout, "available_at": "2026-11-17T00:50:00Z",
                                    "source": "fixture-exchange"}
    child = follow_payouts(original, future)
    settled = verify_run(child)
    final, = settled["positions"]
    assert child["inputs"]["parent_run"] == original["id"]
    assert final["settled"] and final["model_version"] == position["model_version"]
    assert final["opened_at"] == position["opened_at"] and final["fee"] == position["fee"]
    assert final["payout"] == position["quantity"] * yes_payout
    assert settled["cash_committed"] == 0
    assert Decimal(str(settled["cash"])) == 500 + Decimal(str(settled["realized_pnl"]))
    assert verify_run(original)["cash_committed"] == 25


def test_reimporting_same_book_receipts_does_not_change_trial(tmp_path):
    capture = fixture_capture(**OPTIONS)
    write_capture(capture, tmp_path)
    start = create_run(load_captures(tmp_path, fixture=True), fixture_probabilities)
    write_capture(capture, tmp_path)
    repeated = create_run(load_captures(tmp_path, fixture=True), fixture_probabilities)
    assert start["id"] == repeated["id"]
    assert start["result"] == repeated["result"]
