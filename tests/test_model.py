from datetime import datetime, timezone

import pytest

from weather_edge.model import (
    _benchmark_buckets,
    _limits,
    _outcome,
    _probability,
    _snapshot,
    compare_prices,
    predict,
)
from weather_edge.nbm import climate_window, forecast_for_day, parse_nbh, parse_nbs


def test_noaa_cards_keep_holes_and_prefer_daily_tmax():
    nbh = (
        " KNYC   NBM V5.0 NBH GUIDANCE    11/05/2026  1200 UTC\n"
        " UTC  13 14 15 16 17\n"
        " TMP  64 67    69 70\n"
        " TSD   2  2     2  3\n"
    ).encode()
    nbs = (
        " KNYC   NBM V5.0 NBS GUIDANCE    11/05/2026  1200 UTC\n"
        " UTC  18 21 00 03 06 09 12\n"
        " FHR  06 09 12 15 18 21 24\n"
        " TXN        72          63\n"
        " XND         2           3\n"
    ).encode()
    h = {**parse_nbh(nbh, "KNYC"), "available_at": "2026-11-05T13:10:00Z", "source": "NOAA NBH"}
    s = {**parse_nbs(nbs, "KNYC"), "available_at": "2026-11-05T13:18:00Z", "source": "NOAA NBS"}
    forecast = forecast_for_day({"nbh": h, "nbs": s}, "2026-11-05")
    assert forecast["available_at"] == "2026-11-05T13:18:00Z"
    assert forecast["valid_at"] == "2026-11-06T00:00:00Z"
    assert forecast["high_f"] == 72
    assert forecast["high_sd_f"] == 2
    assert [row["temp_f"] for row in forecast["hourly"]] == [64, 67, 69, 70]
    assert all(not item["valid_at"].endswith("15:00:00Z") for item in forecast["hourly"])


def test_weekday_window_uses_fixed_local_standard_time():
    start, end = climate_window("2026-08-10")
    assert start.isoformat() == "2026-08-10T05:00:00+00:00"
    assert end.isoformat() == "2026-08-11T05:00:00+00:00"


def _example_forecast():
    return {"issued_at": "2026-11-05T06:00:00Z", "available_at": "2026-11-05T07:00:00Z",
            "valid_at": "2026-11-06T00:00:00Z", "high_f": 72, "high_sd_f": 2,
            "high_source": "noaa-nbm-nbs-txn", "hourly": [{"valid_at": "2026-11-05T07:00:00Z", "temp_f": 66},
                                                          {"valid_at": "2026-11-05T08:00:00Z", "temp_f": 68}]}


def test_snapshot_respects_receipt_and_keeps_previous_noaa_max_when_next_is_hourly_only():
    forecasts = [_example_forecast(),
                 {**_example_forecast(), "issued_at": "2026-11-05T08:00:00Z", "available_at": "2026-11-05T08:05:00Z",
                  "high_f": 98},
                 {**_example_forecast(), "issued_at": "2026-11-05T07:30:00Z", "available_at": "2026-11-05T07:45:00Z",
                  "high_f": 61, "high_source": "noaa-nbm-nbh-hourly-max"}]
    observations = [{"at": "2026-11-05T04:51:00Z", "available_at": "2026-11-05T04:58:00Z", "temp_f": 95},
                    {"at": "2026-11-05T07:30:00Z", "available_at": "2026-11-05T07:40:00Z", "temp_f": 69},
                    {"at": "2026-11-05T07:59:00Z", "available_at": "2026-11-05T08:09:00Z", "temp_f": 97}]
    snapshot = _snapshot(station="KNYC", day="2026-11-05", as_of=datetime(2026, 11, 5, 8, tzinfo=timezone.utc),
                         forecasts=forecasts, observations=observations, standard_utc_offset_hours=-5)
    assert snapshot["noaa_high_f"] == 72
    assert snapshot["observed_high_f"] == 69
    assert snapshot["features"]["latest_observation_minus_nbh_f"] == 2


def test_daily_high_integer_buckets_partition_temperature_without_missing_tail():
    buckets = _benchmark_buckets(72)
    assert sum(_probability(_limits(bucket), [69.6, 72, 74.3], 1.6) for bucket in buckets) == pytest.approx(1)
    pair = {"lower_f": 71, "upper_f": 72}
    assert [_outcome(pair, temperature) for temperature in (70, 71, 72, 73)] == [0, 1, 1, 0]
    assert _probability((100.5, 101.5), [91], 2) > 0


class ConstantRidge:
    def predict(self, X):
        return [-0.5 for _ in X]


def test_predictions_respect_training_cutoff_and_forward_cursor_does_not_reveal_data():
    artifact = {"stations": ["KNYC"], "ridge": ConstantRidge(), "evidence_cutoff": "2026-11-05T08:00:00Z",
                "validation": {"all": {"residuals_f": [-2, -1, 0, 1, 2], "days": 5, "baseline_sd_f": 2}}}
    inputs = {"station": "KNYC", "day": "2026-11-05", "forecasts": [_example_forecast()],
              "buckets": _benchmark_buckets(72), "observations": [
                  {"at": "2026-11-05T10:51:00Z", "available_at": "2026-11-05T10:58:00Z", "temp_f": 71}]}
    assert predict(artifact, as_of="2026-11-05T07:59:00Z", **inputs) is None
    earlier = predict(artifact, as_of="2026-11-05T08:00:00Z", **inputs)
    actual = predict(artifact, as_of="2026-11-05T12:00:00Z", **inputs)
    forward = predict(artifact, as_of="2026-11-05T12:00:00Z", knowledge_at="2026-11-05T08:00:00Z", **inputs)
    assert actual["observed_high_f"] == 71
    assert forward["observed_high_f"] is None
    assert forward["buckets"] == earlier["buckets"]
    assert all(bucket["uncertain"] for bucket in forward["buckets"])


def test_matched_market_scores_use_only_available_prices_within_sixty_seconds():
    artifact = {"stations": ["KNYC"], "ridge": ConstantRidge(), "evidence_cutoff": "2026-11-01T00:00:00Z",
                "validation": {"all": {"residuals_f": [-2, -1, 0, 1, 2], "days": 5, "baseline_sd_f": 2}}}
    event = {"id": "temp-nyc", "station": "KNYC", "date": "2026-11-05", "starts_at": "2026-11-05T05:00:00Z",
             "ends_at": "2026-11-06T05:00:00Z", "settlement": {"high_f": 72}, "forecasts": [_example_forecast()],
             "observations": [], "buckets": [{"id": "71-72f", "label": "71 to 72", "lower_f": 71, "upper_f": 72,
                                               "prices": [
        {"at": "2026-11-05T07:59:00Z", "available_at": "2026-11-05T08:00:00Z", "yes": 0.41},
        {"at": "2026-11-05T08:00:00Z", "available_at": "2026-11-05T08:01:00Z", "yes": 0.98},
        {"at": "2026-11-05T13:58:00Z", "available_at": "2026-11-05T13:59:00Z", "yes": 0.61},
    ]}]}
    report = compare_prices(artifact, {"events": [event]})
    assert report["cases"] == 1
    assert report["by_case"][0]["market_display_yes"] == 0.41
    assert report["by_case"][0]["quote_available_at"] == "2026-11-05T08:00:00Z"
