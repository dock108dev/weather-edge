from datetime import timedelta
from hashlib import sha256
import os

import joblib
import pytest

from weather_edge import model_evidence, server
from weather_edge.capture_fixture import START, fixture_capture


def make_study(path, cutoff):
    path.parent.mkdir(parents=True, exist_ok=True)
    calibration = {"days": 10, "residual_sd_f": 3, "residuals_f": [-2] * 5 + [2] * 5}
    joblib.dump({"schema_version": server.study.ARTIFACT_SCHEMA, "version": server.study.MODEL_VERSION,
                 "stations": ["KNYC"], "selected": "noaa", "operational_method": "noaa",
                 "operational_from": cutoff.isoformat(), "evidence_cutoff": cutoff.isoformat(),
                 "estimators": {}, "calibration": {"noaa": {"KNYC": {"all": calibration}}}}, path)
    return path.read_bytes()


@pytest.mark.parametrize("cutoff_delay,witness_delay", [(12, 8), (8, 12)])
def test_historical_model_requires_both_clocks(tmp_path, monkeypatch, cutoff_delay, witness_delay):
    before = START
    witnessed = START + timedelta(seconds=witness_delay, microseconds=1)
    cutoff = START + timedelta(seconds=cutoff_delay)
    usable = max(witnessed, cutoff)
    monkeypatch.setattr(server, "DATA", tmp_path)
    event, = fixture_capture(duration_seconds=30, poll_seconds=8, max_rounds=8, max_events=12)["events"]
    instrument = event["buckets"][2]["id"]

    selected = server.study.study_dir(tmp_path) / "selected.joblib"
    blob = make_study(selected, cutoff)
    vintage = sha256(blob).hexdigest()
    path = selected.parent / "vintages" / vintage / selected.name
    model_evidence.snapshot(path, blob, vintage)
    os.utime(path, (before.timestamp(), before.timestamp()))
    assert server.backtest_probability_lookup()(event, usable) == {}

    receipt = model_evidence.record(tmp_path, "study", vintage, witnessed, "frozen-paper-trial", {"run_id": "test"})
    lookup = server.backtest_probability_lookup()
    assert lookup(event, before) == {}
    assert lookup(event, min(witnessed, cutoff)) == {}
    assert lookup(event, usable - timedelta(microseconds=1)) == {}
    assert usable in lookup.clocks(event, START + timedelta(minutes=1))
    estimate = lookup(event, usable)[instrument]
    assert estimate["model_vintage"] == vintage
    assert estimate["model_availability"] == receipt
    assert estimate["probability"] == pytest.approx(lookup.decision(event, usable)["buckets"][2]["probability"])

    os.utime(path, (usable.timestamp(), usable.timestamp()))
    assert server.backtest_probability_lookup()(event, usable)[instrument]["model_availability"] == receipt

    # An apparently earlier receipt for different bytes cannot attest this model.
    path.write_bytes(blob + b"invalid extra byte")
    assert server.backtest_probability_lookup()(event, usable) == {}
