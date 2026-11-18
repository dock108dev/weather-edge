import json
import math

from weather_edge import collector, strategy_twc
from weather_edge.market import parse_time
from weather_edge.strategy_heat_forecast import station_guidance
from weather_edge.strategy_metar_high import guarded_probability, metar_high_evidence
from weather_edge.strategy_periods import classify_heat_day
from weather_edge.strategy_twc_check import climate_indices, exact_report, measured_payout


def source_receipt(hour, *, state="official", value="T"):
    day = "2026-11-07"
    rows = [
        {"station": {"icao": "KNYC", "city": "New York City", "timezone": "America/New_York", "cliId": "NYC"},
         "status": state,
         "data": {"reportDate": day, "maxTemp": 61, "minTemp": 49, "precipitation": value, "issueTime": ""}
                 if state == "official" else None,
         "avgHours": 24 if state == "official" else 22, "avgTemp": 55},
    ]
    body = json.dumps({"date": day, "results": rows}).encode()
    at = f"2026-11-08T{hour}:00:00.010000Z"
    return {
        "day": day, "requested_at": at, "received_at": at, "source": strategy_twc.SOURCE + day,
        "status": 200, "headers": {}, "body": body,
    }


def test_provider_report_preserves_original_trace_and_real_receipt_clock(tmp_path):
    preliminary = source_receipt("07", state="no_report")
    official = source_receipt("09")
    assert strategy_twc.store(tmp_path, preliminary) == 1
    assert strategy_twc.store(tmp_path, official) == 1

    missing_before_download = strategy_twc.reports(tmp_path, station="KNYC", as_of="2026-11-08T07:00:00Z")
    assert missing_before_download == []
    old, = strategy_twc.reports(tmp_path, station="KNYC", as_of="2026-11-08T08:00:00Z")
    latest, = strategy_twc.reports(tmp_path, station="KNYC", as_of="2026-11-08T09:01:00Z")
    assert old["provider_status"] == "no_report" and old["temperature_high_f"] is None
    assert latest["precipitation_in"] == "T" and latest["provider_issued_at"] is None
    assert latest["available_at"] == official["received_at"]
    assert strategy_twc.raw_receipt(tmp_path, latest["receipt_id"]) == official["body"]


def test_provider_backfill_honors_stop_and_keeps_local_reads(tmp_path, monkeypatch):
    strategy_twc.store(tmp_path, source_receipt("09"))
    (collector.collect_path(tmp_path) / "paused").touch()

    def unexpected_fetch(*_):
        raise AssertionError("Stopped collection must never request a new provider receipt")

    monkeypatch.setattr(strategy_twc, "fetch", unexpected_fetch)
    result = strategy_twc.backfill(tmp_path, "2026-11-07", "2026-11-08", refresh=True)
    assert result["days_fetched"] == 0
    assert result["issues"] == {"2026-11-07": "Collection stopped locally"}
    assert len(strategy_twc.reports(tmp_path, day="2026-11-07")) == 1


def test_trace_mapping_requires_matching_station_and_explicit_rule(tmp_path):
    strategy_twc.store(tmp_path, source_receipt("09"))
    by_station, by_cli = climate_indices(strategy_twc.reports(tmp_path))
    market = {"day": "2026-11-07", "rules_primary": "CLINYC total precipitation", "station": None,
              "family": "daily-rain", "units": "in", "operator": "above", "lower": "0",
              "rules_secondary": "“Trace” amounts (T) and missing daily precipitation values are counted as 0 inches."}
    report, reason = exact_report(market, by_station, by_cli)
    assert reason is None and report["station"] == "KNYC"
    measured, reason = measured_payout(market, report)
    assert reason is None and measured[0:2] == ("0", 0)
    assert measured_payout({**market, "rules_secondary": None}, report)[0] is None
    assert exact_report({**market, "station": "KEWR"}, by_station, by_cli)[0] is None


def test_nbe_delimited_temperature_and_lead_fields_keep_alignment():
    card = (
        b" KDFW    NBM V5.0 NBE GUIDANCE    10/04/2026  1800 UTC\n"
        b" FHR    18| 30  42| 54  66| 78\n"
        b" TMP    67| 75  62| 75 -99| 77\n"
    )
    parsed = station_guidance(card, "KDFW", "nbe")
    assert [r["temp_f"] for r in parsed["points"]] == [67, 75, 62, 75, 77]
    assert parsed["points"][-1]["at"] == "2026-10-08T00:00:00Z"


def test_heat_streak_keeps_displayed_rounding_ties_ambiguous():
    assert classify_heat_day({"hours_reported": 24, "hourly_average_f": 90.5}, 90)[0] is None
    assert classify_heat_day({"hours_reported": 17, "hourly_average_f": 92.0}, 90)[0] is False
    assert classify_heat_day({"hours_reported": 18, "hourly_average_f": 90.6}, 90)[0] is True


def test_six_hour_high_requires_both_possible_windows_in_day_and_before_decision():
    start = parse_time("2026-11-15T05:00:00Z")
    clock = parse_time("2026-11-15T18:02:00Z")
    observation = {"at": "2026-11-15T17:51:00Z", "available_at": "2026-11-15T18:01:00Z",
                   "report": "METAR KNYC 151751Z AUTO 09/06 A2947 RMK AO2 T00890061 10111"}
    exact = metar_high_evidence(observation, "KNYC", start, clock)
    assert [row["kind"] for row in exact] == ["point", "six-hour high"]
    assert exact[-1]["lower_f"] == 52
    incomplete = {**observation, "at": "2026-11-15T05:51:00Z", "available_at": "2026-11-15T06:01:00Z",
                  "report": observation["report"].replace("151751Z", "150551Z")}
    assert [row["kind"] for row in metar_high_evidence(incomplete, "KNYC", start, clock)] == ["point"]


def test_high_guard_keeps_prior_conflict_reserve_and_total_probability():
    lower = guarded_probability((-math.inf, 48.5), [48, 49, 50], 0.5, 49, 0.1)
    upper = guarded_probability((48.5, math.inf), [48, 49, 50], 0.5, 49, 0.1)
    assert 0 < lower < 0.1
    assert math.isclose(lower + upper, 1)
