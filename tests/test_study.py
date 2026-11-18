from datetime import timedelta
from hashlib import sha256
from io import BytesIO
from statistics import NormalDist
import json

import joblib
import pytest

from weather_edge import study
from weather_edge.model_study_data import WINDOWS
from weather_edge.nbm import utc


class FixedRidge:
    def predict(self, X):
        return [-0.5] * len(X)


def artifact():
    calibration = {
        method: {"KNYC": {"all": {"days": 10, "residual_sd_f": 3.0, "residuals_f": [-2.0] * 5 + [2.0] * 5}}}
        for method in study.METHODS
    }
    return {"version": study.MODEL_VERSION, "stations": ["KNYC"], "selected": "ridge_sky",
            "operational_method": "noaa", "operational_from": "2026-11-05T13:00:00Z",
            "evidence_cutoff": "2026-10-28T08:40:00Z", "calibration": calibration,
            "estimators": {"ridge": FixedRidge(), "ridge_sky": FixedRidge()}}


def event(*, sky=True, noaaspread=2):
    hourly = {"valid_at": "2026-11-05T18:00:00Z", "temp_f": 72}
    if sky:
        hourly["sky_pct"] = 80
    forecast = {"issued_at": "2026-11-05T06:00:00Z", "available_at": "2026-11-05T07:00:00Z",
                "valid_at": "2026-11-06T00:00:00Z", "high_f": 72, "high_sd_f": noaaspread,
                "high_source": "noaa-nbm-nbs-txn", "hourly": [hourly],
                "hourly_source": "https://noaa.example/nbh"}
    return {"date": "2026-11-05", "station": "KNYC", "forecasts": [forecast], "observations": [],
            "buckets": [{"id": "below", "lower_f": None, "upper_f": 70},
                        {"id": "center", "lower_f": 71, "upper_f": 72},
                        {"id": "above", "lower_f": 73, "upper_f": None}]}


def test_study_windows_have_disjoint_date_groups_and_label_embargoes():
    for (_, end), (start, _) in zip(WINDOWS.values(), list(WINDOWS.values())[1:]):
        assert start - end == timedelta(days=2)
    assert study._split_label_cutoff("selection") == utc("2026-10-29T02:00:00Z")


def test_operational_noaa_follows_cutover_but_earlier_clock_keeps_pretest_selection():
    earlier = study.predict(artifact(), event(), "2026-11-05T12:59:00Z")
    current = study.predict(artifact(), event(), "2026-11-05T13:00:00Z")
    projected = study.predict(artifact(), event(), "2026-11-05T19:00:00Z", "2026-11-05T12:59:00Z")
    assert earlier["method"] == projected["method"] == "ridge_sky"
    assert earlier["version"] == projected["version"] == study.SELECTION_VERSION
    assert earlier["buckets"] == projected["buckets"]
    assert current["method"] == "noaa"
    assert current["version"] == study.OPERATIONAL_VERSION
    assert current["model_high_f"] == 72
    baseline = NormalDist(72, 2)
    assert current["buckets"][1]["probability"] == pytest.approx(baseline.cdf(72.5) - baseline.cdf(70.5))
    assert current["probability_mass"] == pytest.approx(1)
    assert current["partition"] == "exhaustive"
    for bucket in current["buckets"]:
        assert bucket["probability"] + bucket["no_probability"] == pytest.approx(1)


def test_operational_fallback_uses_earlier_calibration_when_noaa_spread_missing():
    current = study.predict(artifact(), event(noaaspread=None), "2026-11-05T13:00:00Z")
    baseline = NormalDist(72, 3)
    assert current["buckets"][1]["probability"] == pytest.approx(baseline.cdf(72.5) - baseline.cdf(70.5))
    assert current["q10_f"] == pytest.approx(baseline.inv_cdf(0.1))


def test_sky_join_requires_same_source_issue_and_a_publication_before_forecast(tmp_path):
    saved = artifact()
    saved["card_cache"] = tmp_path
    weather = event(sky=False)
    original = weather["forecasts"][0]
    card = {"source": original["hourly_source"], "issued_at": original["issued_at"],
            "available_at": "2026-11-05T06:55:00Z", "hourly": [{**original["hourly"][0], "sky_pct": 80}]}
    path = tmp_path / "KNYC-20261105-06-nbh.json"
    for fields in ({"available_at": "2026-11-05T08:00:00Z"}, {"source": "other"}, {}):
        path.write_text(json.dumps({**card, **fields}))
        study._read_card.cache_clear()
        model = study.predict(saved, weather, "2026-11-05T12:00:00Z")
        assert model["selected_method"] == "ridge_sky"
        assert model["method"] == ("ridge" if fields else "ridge_sky")
        assert bool(model["fallback_reason"]) == bool(fields)
    assert "sky_pct" not in original["hourly"][0]


def test_partial_partitions_keep_missing_mass():
    weather = event()
    weather["buckets"] = weather["buckets"][:1]
    model = study.predict(artifact(), weather, "2026-11-05T13:00:00Z")
    assert model["partition"] == "partial"
    assert model["probability_mass"] == model["buckets"][0]["probability"] < 1


def test_vintage_archives_old_and_new_evaluations_without_changing_role_version(tmp_path):
    root = study.study_dir(tmp_path)
    root.mkdir(parents=True)
    snapshot = {**artifact(), "schema_version": study.ARTIFACT_SCHEMA}
    serialized = BytesIO()
    joblib.dump(snapshot, serialized)
    model_bytes = serialized.getvalue()
    vintage = sha256(model_bytes).hexdigest()
    weather = b"weather evidence"
    weather_digest = sha256(weather).hexdigest()
    old_report = json.dumps({"inputs_sha256": weather_digest}).encode()
    (root / "selected.joblib").write_bytes(model_bytes)
    (root / "report.json").write_bytes(old_report)
    study._preserve_current(root, weather, weather_digest)

    newer_report = json.dumps({"vintage": vintage, "evaluation": "active market evidence"}).encode()
    for _ in range(2):
        assert study._snapshot_vintage(root, model_bytes, newer_report, weather, b"market evidence") == vintage
    vintage_dir = root / "vintages" / vintage
    archive = vintage_dir / "evaluations"
    assert len(list(archive.iterdir())) == 2
    assert (archive / sha256(old_report).hexdigest() / "report.json").read_bytes() == old_report
    assert (archive / sha256(newer_report).hexdigest() / "market-inputs.json.gz").read_bytes() == b"market evidence"
    assert (vintage_dir / "inputs.json.gz").read_bytes() == weather
    for loaded in (study.load_selected(tmp_path), study.load_selected(tmp_path, vintage=vintage)):
        prediction = study.predict(loaded, event(), "2026-11-05T13:00:00Z")
        assert prediction["vintage"] == vintage
        assert prediction["version"] == study.OPERATIONAL_VERSION
