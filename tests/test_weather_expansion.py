from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest

from weather_edge import expansion_contract, expansion_precip, expansion_weather, research_model, research_model_low_full_day


def test_madis_only_replay_preserves_native_station_identity_and_receipt():
    from weather_edge.expansion_replay import _madis_observations

    start = datetime(2026, 2, 12, 6, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    source = "https://madis.example/20260212_1200.gz"
    receipts = {
        ("2026-02-12T11:53:00Z", "KAUS 121153Z 34002KT 10SM CLR 07/05 A3001 RMK T00670050"): (
            "2026-02-12T11:58:00Z", source),
        ("2026-02-12T11:53:00Z", "KDFW 121153Z 34002KT 10SM CLR 06/05 A3001 RMK T00600050"): (
            "2026-02-12T11:57:00Z", source),
        ("2026-02-12T05:53:00Z", "KAUS 120553Z 34002KT 10SM CLR 06/05 A3001 RMK T00600050"): (
            "2026-02-12T06:01:00Z", source),
    }
    result = _madis_observations(receipts, "KAUS", start, end)
    assert len(result) == 1
    assert result[0]["temp_f"] == 44.1
    assert result[0]["temperature_precision_c"] == 0.1
    assert result[0]["available_at"] == "2026-02-12T11:58:00Z"
    assert result[0]["receipt_source"] == source


def test_matching_minute_tape_does_not_attribute_pre_receipt_marks_to_post_window():
    from weather_edge.nbm import iso
    from weather_edge.research_model_low_cross_venue_receipts import _window

    at = datetime(2026, 10, 30, 10, 58, tzinfo=timezone.utc)
    opened, closed = at - timedelta(hours=1), at + timedelta(hours=1)
    history = {at + timedelta(minutes=minute + 1): {"at": iso(at + timedelta(minutes=minute)), "probability": value}
               for minute, value in ((-6, 0.20), (-2, 0.25), (0, 0.50), (4, 0.72))}
    result = _window((list(history), history), opened, closed, at, 5)
    assert result["before"]["probability"] == 0.25
    assert result["after_move"] == pytest.approx(0.47)
    history.pop(at + timedelta(minutes=5))
    assert _window((list(history), history), opened, closed, at, 5) is None


def test_daily_low_runtime_marks_only_entirely_implied_bucket_sides():
    from weather_edge.expansion_live_precision import _bucket_estimates

    buckets = [{"id": "lower-tail", "lower_f": None, "upper_f": 39},
               {"id": "middle", "lower_f": 40, "upper_f": 41},
               {"id": "upper-tail", "lower_f": 42, "upper_f": None}]
    config = {"width_f": 2.5, "residuals": [("2026-10-01", -2)], "exceptions": {"uncapped_weight": 0.1}}
    below = _bucket_estimates(buckets, 39, 39, config)
    within = _bucket_estimates(buckets, 40, 40, config)
    assert [row["metar_implied_side"] for row in below] == ["YES", "NO", "NO"]
    assert [row["metar_implied_side"] for row in within] == [None, None, "NO"]
    assert sum(row["probability"] for row in below) == pytest.approx(1)


def test_cooling_anomaly_uses_one_forecast_published_before_both_measurements():
    from weather_edge.research_model_low_cooling import _cooling

    start = datetime(2026, 10, 9, 5, tzinfo=timezone.utc)
    decision = start + timedelta(hours=17)
    prior = {"issued_at": "2026-10-09T12:00:00Z", "available_at": "2026-10-09T12:51:00Z", "source": "prior",
             "card_sha256": "older", "points": [{"valid_at": name, "TMP": value} for name, value in (
                 ("2026-10-09T18:00:00Z", 65), ("2026-10-09T21:00:00Z", 65), ("2026-10-10T00:00:00Z", 60))]}
    revised = {**prior, "issued_at": "2026-10-09T18:00:00Z", "available_at": "2026-10-09T19:00:00Z", "source": "later"}
    measurements = [{"at": at, "available_at": seen, "temp_f": degrees, "raw": raw, "receipt_source": "MADIS"}
                    for at, seen, degrees, raw in (
                        ("2026-10-09T18:55:00Z", "2026-10-09T18:58:00Z", 65, "KNYC 091855Z 19002KT 10SM CLR 18/05 A3001 RMK T01830050"),
                        ("2026-10-09T21:55:00Z", "2026-10-09T21:58:00Z", 59, "KNYC 092155Z 19002KT 10SM CLR 15/05 A3001 RMK T01500050"),
                    )]
    change = _cooling(measurements, "KNYC", start, decision, [prior, revised])
    assert change["forecast"]["source"] == "prior"
    assert change["observed_change_f_per_three_hours"] == pytest.approx(-5.94)
    assert change["residual_change_f_per_three_hours"] < -4


def test_radar_screen_requires_full_day_containment_and_a_measurable_hourly_group():
    from weather_edge.research_model_rain_neighborhood import _hourly_gauge

    start = datetime(2026, 11, 15, 5, tzinfo=timezone.utc)
    decision = start + timedelta(hours=2)
    rows = [{"at": "2026-11-15T05:52:00Z", "available_at": "2026-11-15T05:55:00Z",
             "raw": "KNYC 150552Z 00000KT 8SM OVC030 07/06 A2996 RMK P0000"},
            {"at": "2026-11-15T06:52:00Z", "available_at": "2026-11-15T06:56:00Z",
             "raw": "KNYC 150652Z 00000KT 8SM -RA OVC030 07/06 A2996 RMK P0003"}]
    assert not _hourly_gauge(rows[:1], "KNYC", start, decision)
    assert not _hourly_gauge(rows[1:], "KNYC", start, decision)
    assert not _hourly_gauge(rows, "KNYC", start, decision - timedelta(minutes=8))
    assert _hourly_gauge(rows, "KNYC", start, decision)


def test_extended_nbm_keeps_native_day_separator_and_optional_version():
    from weather_edge.expansion_nbe import daily_extrema, parse_card

    raw = (b" KCVG    NBM  NBE GUIDANCE    11/15/2026  1200 UTC\n"
           b"   MON 16 |TUE 17  CLIMO \n"
           b" UTC    12| 00  12       \n"
           b" FHR    24| 36  48       \n"
           b" TXN    34| 56  33  34 52\n"
           b" XND     2|  5   4       \n"
           b" KCMH    NBM  NBE GUIDANCE    11/15/2026  1200 UTC\n")
    result = parse_card(raw, "KCVG")
    assert result["printed_model_version"] is None
    forecasts = result["forecasts"]
    assert [(row["temp_f"], row["sd_f"]) for row in forecasts] == [(34, 2), (56, 5), (33, 4)]
    assert forecasts[1]["valid_at"] == "2026-11-17T00:00:00Z"
    assert forecasts[1]["field"] == "TXN maximum"
    early = daily_extrema(result)[0]
    updated = daily_extrema(parse_card(raw.replace(b"1200 UTC", b"1800 UTC")
                                      .replace(b"24| 36  48", b"18| 30  42"), "KCVG"))[0]
    assert updated["target_date"] == early["target_date"] == "2026-11-16"
    for field, later_lead in (("minimum", 18), ("maximum", 30)):
        assert updated[field]["lead_hours"] == later_lead
        assert {key: value for key, value in updated[field].items() if key != "lead_hours"} == {
            key: value for key, value in early[field].items() if key != "lead_hours"}


def test_extended_station_cursor_carries_prior_issue_and_ignores_late_older_arrival(tmp_path):
    import hashlib
    import json

    from weather_edge import expansion_nbe
    from weather_edge.nbm import iso

    def save(day, received):
        stamp = datetime(2026, 11, day, 12, tzinfo=timezone.utc)
        raw = f"KPDX NBE issue on {day}"
        path = expansion_nbe.card_path(tmp_path, stamp, station="KPDX")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"station": "KPDX", "issued_at": iso(stamp), "available_at": iso(stamp + timedelta(hours=1)),
                                    "received_at": received, "printed_model_version": None, "source": "NOAA fixture",
                                    "card_sha256": hashlib.sha256(raw.encode()).hexdigest(), "card_text": raw,
                                    "forecasts": [{"valid_at": iso(stamp + timedelta(hours=hour)), "temp_f": value}
                                                  for hour, value in ((24, 38), (36, 48), (48, 37), (60, 49))]}))

    save(12, "2026-11-14T17:00:00Z")
    save(13, "2026-11-13T15:00:00Z")
    save(14, "2026-11-14T18:00:00Z")
    start, through = "2026-11-14T16:00:00Z", "2026-11-14T19:00:00Z"
    windows = expansion_nbe.station_outlooks(tmp_path, "KPDX", start, through)
    assert [row["known_at"] for row in windows] == ["2026-11-13T15:00:00Z", "2026-11-14T18:00:00Z"]
    assert [row["daily_extrema"][0]["target_date"] for row in windows] == ["2026-11-14", "2026-11-15"]
    assert expansion_nbe.station_outlook(tmp_path, "KPDX", through) == windows[-1]
    replay = expansion_nbe.station_outlooks(tmp_path, "KPDX", start, through, original_release=True)
    assert [row["known_at"] for row in replay] == ["2026-11-14T13:00:00Z"]


def test_archive_supplement_matches_original_receipt_without_replacing_prior_file(tmp_path):
    import json

    from weather_edge import archive, expansion_replay_enrichment as enrichment

    day = date(2026, 11, 1)
    event = archive._native_station_day(archive._spec("nyc", day))
    reading = {"at": "2026-11-01T05:51:00Z", "available_at": "2026-11-01T06:01:00Z", "temp_f": 46.0,
               "raw": "KNYC 010551Z 00000KT 10SM CLR 08/06 A3001", "timing_proxy": "fixture delay"}
    event["research_observations"] = [reading]
    event["observations_missing_receipt"] = 1
    base = tmp_path / "archive" / str(day) / "KNYC.json"
    archive._write_json(base, event)
    before = base.read_bytes()
    assert archive.catalog(tmp_path)["days"][0]["stations"][0]["coverage"]["observations"] == 0

    proved = {key: value for key, value in reading.items() if key != "timing_proxy"}
    proved.update(receipt_source="NOAA retained file", receipt_basis=enrichment.RECEIPT_BASIS)
    path = enrichment.path_for(tmp_path, day, "KNYC")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"version": enrichment.VERSION, "date": str(day), "station": "KNYC",
                                "climate_starts_at": event["climate_starts_at"], "climate_ends_at": event["climate_ends_at"],
                                "sources": {"NOAA retained file": {"path": "fixture-file", "sha256": "fixture-digest"}},
                                "observations": [{**proved, "available_at": "2026-11-01T05:59:00Z"},
                                                 {**proved, "available_at": "2026-11-01T05:54:00Z"}]}))
    loaded = archive.load_day(tmp_path, day)["events"][0]
    assert loaded["observations"] == [{**proved, "available_at": "2026-11-01T05:54:00Z"}]
    assert loaded["research_observations"] == [] and loaded["observations_missing_receipt"] == 0
    assert loaded["observation_enrichment"]["original_receipts"] == 1
    assert archive.catalog(tmp_path)["days"][0]["stations"][0]["coverage"]["observations"] == 1
    assert base.read_bytes() == before


def test_cincinnati_market_projection_requires_retained_knowledge_and_native_trading_window(tmp_path):
    import json

    from weather_edge import expansion_live_hdd

    version = expansion_live_hdd.STUDY_VERSION
    identifier = version + "-locally-retained"
    event = expansion_live_hdd.EVENT_ID
    instrument = event + "-T1500"
    period = expansion_live_hdd.PERIOD
    market = {"venue": "kalshi", "family": "heating-degree-days", "series_id": "KXHDDCVG", "event_id": event,
              "station": "KCVG", "operator": "below", "instrument_id": instrument, "upper": "1500", "units": "HDD (65°F)",
              "resolution_source": "The Weather Company daily HDD (65)", "climate_starts_at": period["starts_at"],
              "climate_ends_at": period["ends_at"], "opened_at": "2026-09-18T05:00:00Z", "closed_at": "2027-01-01T04:59:00Z",
              "rules_primary": "HDD at KCVG, WMO identifier 72421 between Oct 1, 2026 and Dec 31, 2026",
              "rules_secondary": "The Weather Company HDD (65) and Hourly Mean(F) fallback"}
    path = tmp_path / "research" / "models" / f"{identifier}.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"id": identifier, "version": version,
                               "market_projection": {"schema": 1, "event_id": event, "station": "KCVG", "contract_period": period,
                                   "known_at": "2026-11-15T22:02:00Z", "accrual": {"last_source_available_at": "2026-11-15T19:00:00Z"},
                                   "forecast": {"issued_at": "2026-11-15T12:00:00Z", "source_published_at": "2026-11-15T13:28:00Z",
                                                "received_at": "2026-11-15T21:59:00Z", "hdd_65": 94},
                                   "thresholds": {instrument: {"operator": "below", "threshold_hdd": 1500}}}}))
    get = expansion_live_hdd.market_projection
    assert get(tmp_path, market, "2026-11-15T22:01:59Z") is None
    assert get(tmp_path, market, "2026-11-15T22:02:00Z")["forecast"]["hdd_65"] == 94
    assert get(tmp_path, {**market, "station": "KNYC"}, "2026-11-15T22:02:00Z") is None
    assert get(tmp_path, market, market["closed_at"]) is None


def test_climate_product_preserves_trace_and_required_completion(tmp_path):
    station, day = "KNYC", date(2026, 11, 13)
    folder = tmp_path / "expansion" / "cli" / station
    folder.mkdir(parents=True)
    body = ("CDUS41 KOKX 140625\nCLINYC\n"
            "...THE NEW YORK CITY CLIMATE SUMMARY FOR NOVEMBER 13 2026...\n"
            "TEMPERATURE (F)\n YESTERDAY\n MAXIMUM 47R 1:11 AM 76\n MINIMUM 38 923 AM 23\n"
            "PRECIPITATION (IN)\n YESTERDAY T 2.84\nSNOWFALL (IN)\n YESTERDAY 0.2R 1.5\n")
    product = "202611140625-KOKX-CDUS41-CLINYC"
    (folder / f"{product}.txt").write_text(body)
    result = expansion_weather.raw_report(tmp_path, station, day, product)
    assert result["low_f"] == 38
    assert result["high_f"] == 47
    assert result["high_at_local_standard"] == "1:11 AM"
    assert result["low_at_local_standard"] == "923 AM"
    assert result["precip_as_reported"] == "T"
    assert result["snow_in"] == 0.2
    assert expansion_contract.rain_outcome(result["precip_as_reported"]) == 0
    assert expansion_contract.rain_outcome("MM") == 0
    assert expansion_contract.rain_outcome("0.01") == 1
    assert expansion_contract.rain_outcome(None) is None
    partial = "202611140200-KOKX-CDUS41-CLINYC"
    (folder / f"{partial}.txt").write_text(body)
    assert expansion_weather.raw_report(tmp_path, station, day, partial) is None


def test_rain_governing_revision_uses_local_day_cutoff(tmp_path, monkeypatch):
    # Nov. 1 is the autumn clock change and its 14:00 ET is 19:00Z.
    reports = [{"published_at": f"2026-11-01T{clock}Z", "precip_as_reported": amount}
               for clock, amount in (("06:00:00", "0.00"), ("18:15:00", "0.01"), ("19:05:00", "0.10"))]
    monkeypatch.setattr(expansion_weather, "reports_for_day", lambda *args, **kwargs: reports)
    selected, seen, cutoff = expansion_contract.rain_report(tmp_path, date(2026, 10, 31), None)
    assert cutoff == datetime(2026, 11, 1, 19, tzinfo=timezone.utc)
    assert selected["precip_as_reported"] == "0.01"
    assert len(seen) == 3


def test_rain_shadow_requires_exact_rule_and_contained_known_gauge(tmp_path, monkeypatch):
    from weather_edge import expansion_live_rain

    day = date(2026, 11, 15)
    rules = ('KNYC (KNYC), November 15, 2026. National Weather Service Daily Climate Report. '
             '0.01 inches or more. A trace ("T") does not count. missing ("M" or "MM"). '
             'total liquid-equivalent precipitation. 05:00 UTC to 04:59 UTC. 2:00 PM ET. first one published afterwards.')
    event = {"family": "daily-rain", "venue": "polymarket-us", "station": "KNYC", "date": str(day),
             "buckets": [{"id": f"raincli-rain-daily-{day}-knyc", "rules": rules}], "observations": [
                 {"source": "NWS", "at": "2026-11-15T17:51:00Z", "available_at": "2026-11-15T18:07:00Z",
                  "report": "KNYC 151751Z 00000KT 10SM FEW040 10/04 A2992 RMK AO2 60003"},
             ]}
    artifact = {"data": tmp_path, "vintage": "fixture", "evidence_cutoff": "2026-01-01T00:00:00Z",
                "gauge_cases": 3, "gauge_conflicts": 0, "rain_error_allowance": 0.5, "forecast_bands": []}
    monkeypatch.setattr(expansion_live_rain, "weather", lambda *args: {"climate_starts_at": "2026-11-15T05:00:00Z",
                        "forecast": {"p06_overlap_probability": 0.0, "issued_at": "2026-11-15T00:00:00Z",
                                     "available_at": "2026-11-15T17:00:00Z"}})
    early = expansion_live_rain.predict(artifact, event, "2026-11-15T18:05:00Z")
    assert early["gauge_witness"] is None
    assert early["buckets"][0]["yes_upper"] == 1.0
    late = expansion_live_rain.predict(artifact, event, "2026-11-15T18:08:00Z")
    assert late["gauge_witness"]["amount_in"] == 0.03
    assert late["buckets"][0]["probability"] == 0.5
    assert late["available_at"] == "2026-11-15T18:07:00Z"
    event["buckets"][0]["rules"] = rules.replace("0.01 inches", "0.10 inches")
    assert expansion_live_rain.predict(artifact, event, "2026-11-15T18:08:00Z") is None


def test_daily_low_observation_guard_uses_as_of_clock():
    day = date(2026, 11, 14)
    decision = datetime(2026, 11, 14, 8, tzinfo=timezone.utc)
    forecasts = [{"issued_at": "2026-11-14T00:00:00Z", "available_at": "2026-11-14T01:15:00Z", "mean_f": 34},
                 {"issued_at": "2026-11-14T06:00:00Z", "available_at": "2026-11-14T08:05:00Z", "mean_f": 30}]
    observations = [{"at": "2026-11-14T07:45:00Z", "temp_f": 40, "dewpoint_f": 24},
                    {"at": "2026-11-14T07:56:00Z", "temp_f": 35, "dewpoint_f": 23}]
    row = research_model.make_snapshot("KNYC", day, decision, forecasts, observations)
    assert row["forecast"]["mean_f"] == 34
    assert row["observed_min_f"] == 40


def test_remaining_forecast_requires_coverage_through_climate_close():
    start = datetime(2025, 12, 15, 2, tzinfo=timezone.utc)
    card = {"points": [{"valid_at": "2025-12-15T00:00:00Z", "TMP": 25},
                       {"valid_at": "2025-12-15T03:00:00Z", "TMP": 22},
                       {"valid_at": "2025-12-15T06:00:00Z", "TMP": 19}]}
    assert research_model_low_full_day.remaining_minimum(card, start, start + timedelta(hours=3)) == 20
    assert research_model_low_full_day.remaining_minimum(card, start, start + timedelta(hours=5)) is None


def test_actual_station_clock_does_not_use_the_archive_lag():
    start = datetime(2026, 11, 8, 5, tzinfo=timezone.utc)
    at = datetime(2026, 11, 8, 14, tzinfo=timezone.utc)
    readings = [
        {"at": "2026-11-08T13:59:00Z", "available_at": "2026-11-08T13:59:30Z", "temp_f": 46},
        {"at": "2026-11-08T13:45:00Z", "available_at": "2026-11-08T14:01:00Z", "temp_f": 45},
        {"at": "2026-11-08T13:35:00Z", "available_at": "2026-11-08T13:45:00Z", "temp_f": 44, "timing_proxy": "10 minutes"},
    ]
    assert expansion_weather.observations_as_of(readings, start, at, delay_minutes=None) == readings[:1]


def test_goes_projection_matches_noaa_fixed_grid_reference():
    from weather_edge import expansion_satellite

    projection = {"longitude_of_projection_origin": -75, "semi_major_axis": 6378137,
                  "semi_minor_axis": 6356752.31414, "perspective_point_height": 35786023}
    point = expansion_satellite.project(33.846162, -84.690932, projection)
    assert point == pytest.approx((-0.024052, 0.095340), abs=1e-7)
    assert expansion_satellite.project(0, 105, projection) is None


def test_goes_skin_uses_only_high_quality_land_values(monkeypatch):
    from weather_edge import expansion_satellite

    class PackedTemperature:
        attrs = {"valid_range": [9200, 61200], "scale_factor": 0.0025, "add_offset": 190}

        def __getitem__(self, index):
            return self.values[index]

    field = PackedTemperature()
    field.values = np.full((5, 5), 40_000, dtype=np.uint16)
    field.values[0, 0] = 65535
    quality = np.zeros(field.values.shape, dtype=np.uint16)
    quality[0, 1] = 1
    flags = np.zeros(field.values.shape, dtype=np.uint16)
    flags[2, 2] = 128
    monkeypatch.setattr(expansion_satellite, "_locate", lambda *args: (2, 2, slice(0, 5), slice(0, 5)))
    pixels = np.arange(5, dtype=float)
    result = expansion_satellite._skin_point(field, quality, pixels, pixels, {}, "KNYC", 40.0, -74.0, native_flags=flags)
    assert result["center_kelvin"] is None
    assert result["window_good_pixels"] == 22
    assert result["window_mean_kelvin"] == pytest.approx(290.0)
    assert result["window_mean_f"] == pytest.approx(62.33)


def test_hrrr_longwave_requires_paired_instantaneous_surface_fields():
    from weather_edge import expansion_hrrr_longwave

    source = ("101:0:d=2026110807:DLWRF:surface:1-2 hour ave fcst:\n"
              "102:120:d=2026110807:DLWRF:surface:2 hour fcst:\n"
              "103:410:d=2026110807:ULWRF:top of atmosphere:2 hour fcst:\n"
              "104:830:d=2026110807:ULWRF:surface:2 hour fcst:\n"
              "105:1190:d=2026110807:TMP:surface:2 hour fcst:\n")
    assert expansion_hrrr_longwave._ranges(source, 2) == {"DLWRF": (120, 409), "ULWRF": (830, 1189)}
    with pytest.raises(ValueError, match="Both native"):
        expansion_hrrr_longwave._ranges(source, 3)


def test_nbm_backfill_passes_the_injected_public_reader(tmp_path, monkeypatch):
    from weather_edge import expansion_nbm

    day = date(2026, 2, 12)

    def public_reader(*args, **kwargs):
        raise AssertionError("This fixture must never issue a request")

    def card(station, issue, *, get):
        assert get is public_reader
        return {"station": station, "issued_at": issue.isoformat()}

    monkeypatch.setattr(expansion_nbm, "_fetch_card", card)
    result = expansion_nbm.backfill(tmp_path, ["KATL"], day, day, [0], workers=1, get=public_reader)
    assert result["counts"]["KATL"] == {"reused": 0, "new": 1, "missing": 0}


def test_co2_nasa_display_quantizes_before_the_exact_threshold():
    from decimal import Decimal

    from weather_edge.research_model_co2 import _quantized

    assert _quantized(439.5, 0) == Decimal("440")
    assert _quantized(439.5, 2) == Decimal("439.50")
    assert _quantized("439.995", 2) == Decimal("440.00")


def test_precip_frame_uses_later_publication_when_deriving_interval(tmp_path, monkeypatch):
    issue = datetime(2026, 11, 13, 0, tzinfo=timezone.utc)
    calls = []

    def get(*args, **kwargs):
        raise AssertionError("no network")

    def download(stamp, lead, fetch, start_step=None):
        assert stamp == issue and fetch is get
        calls.append((lead, start_step))
        if lead == 6:
            return np.array([3.4, 0.4]), 0.1, 0, issue + timedelta(hours=3), {"source": "later-lead"}
        return np.array([1.1, 0.5]), 0.1, 0, issue + timedelta(hours=4), {"source": "earlier-lead"}

    monkeypatch.setattr(expansion_precip, "_download", download)
    monkeypatch.setattr(expansion_precip, "WIDTH", 2)
    monkeypatch.setattr(expansion_precip, "HEIGHT", 1)
    result = expansion_precip.cache_matching(["2026111300-f006"], tmp_path, get=get)
    assert result == {"frames": ["2026111300-f006"], "errors": {}}
    assert calls == [(6, None), (3, 0)]
    (frame,) = expansion_precip.manifest(tmp_path)
    assert frame["accumulation_starts_at"] == "2026-11-13T03:00:00+00:00"
    assert frame["valid_at"] == "2026-11-13T06:00:00+00:00"
    assert frame["available_at"] == "2026-11-13T04:00:00+00:00"


def test_live_low_uses_exact_rules_and_receipts(tmp_path):
    from weather_edge import expansion_live_low

    common = ("Will the lowest temperature recorded at Central Park (KNYC) in New York City "
              "for 2026-11-15 as reported by the National Weather Service's Climatological Report (Daily) be ")
    event = {
        "family": "daily-low", "station": "KNYC", "date": "2026-11-15",
        "buckets": [{"id": f"tc-temp-nyclow-2026-11-15-{suffix}", "rules": common + condition + "?"}
                    for suffix, condition in (("lt31f", "less than or equal to 30F"),
                                              ("gte31lt32f", "between 31F and 32F"),
                                              ("gte33f", "greater than or equal to 33F"))],
        "forecasts": [{"low_f": 32, "low_sd_f": 2, "low_valid_at": "2026-11-15T12:00:00Z",
                       "low_issued_at": "2026-11-15T00:00:00Z", "low_available_at": "2026-11-15T11:00:00Z",
                       "low_source": "test NOAA", "low_availability_basis": "local_receipt"}],
        "observations": [
            {"source": "NWS", "temp_f": 35, "at": "2026-11-15T16:00:00Z", "available_at": "2026-11-15T16:10:00Z"},
            {"source": "NWS", "temp_f": 20, "at": "2026-11-15T17:50:00Z", "available_at": "2026-11-15T18:05:00Z"},
        ],
    }
    artifact = {
        "data": tmp_path, "stations": ("KNYC",), "method": "noaa", "model": None,
        "widths": {("KNYC", 13): 1.5}, "evidence_cutoff": "2026-10-28T10:00:00Z", "vintage": "test",
        "calibration": {("KNYC", 13): [("2026-10-03", -1), ("2026-10-04", 1)]},
    }
    answer = expansion_live_low.predict(artifact, event, "2026-11-15T18:10:00Z")
    assert answer["knowledge_at"] == "2026-11-15T18:00:00Z"
    assert answer["observed_min_f"] == 35
    assert abs(sum(b["probability"] for b in answer["buckets"]) - 1) < 1e-12
    assert all(b["validation_days"] == 2 for b in answer["buckets"])
    assert expansion_live_low.predict(artifact, {**event, "family": "daily-high"}, "2026-11-15T18:10:00Z") is None
    event["buckets"][0]["rules"] = event["buckets"][0]["rules"].replace("KNYC", "KLGA")
    assert expansion_live_low.predict(artifact, event, "2026-11-15T18:10:00Z") is None


def test_metar_fahrenheit_precision_and_six_hour_local_day_join():
    from weather_edge import expansion_metar

    assert expansion_metar.integer_asos_interval(27, 1) == (80, 81)
    assert expansion_metar.integer_asos_interval(5, 1) == (41, 41)
    assert expansion_metar.integer_asos_interval(5, 0.1) == (41, 41)
    row = {"at": "2026-11-15T11:53:00Z", "available_at": "2026-11-15T12:21:00Z",
           "report": "KMDW 151153Z 29004KT 10SM FEW035 00/M03 A3039 RMK AO2 T00001028 10022 21011 $"}
    known = datetime(2026, 11, 15, 13, tzinfo=timezone.utc)
    local_start = datetime(2026, 11, 15, 6, tzinfo=timezone.utc)
    regular = expansion_metar.metar_low_evidence(row, "KMDW", local_start, known)
    assert [(item["kind"], item["upper_f"]) for item in regular] == [("point", 32)]
    extended = expansion_metar.metar_low_evidence(row, "KMDW", local_start - timedelta(hours=1), known)
    assert [(item["kind"], item["upper_f"]) for item in extended] == [("point", 32), ("six-hour low", 30)]
    assert expansion_metar.metar_low_evidence(row, "KMDW", local_start, known - timedelta(hours=1)) == []


def test_live_low_uses_only_compatible_tighter_original_receipts(tmp_path, monkeypatch):
    from weather_edge import expansion_live_low_receipts, expansion_live_precision

    common = ("Will the lowest temperature recorded at Central Park (KNYC) in New York City "
              "for 2026-11-15 as reported by the National Weather Service's Climatological Report (Daily) be ")
    initial = {"source": "NWS", "at": "2026-11-15T17:51:00Z", "available_at": "2026-11-15T17:54:00Z", "temp_f": 52,
               "report": "KNYC 151751Z 27004KT 10SM CLR 11/01 A3021 RMK AO2 T01110006"}
    tighter = {"source": "Aviation Weather Center", "at": "2026-11-15T18:51:00Z", "available_at": "2026-11-15T19:02:00Z", "temp_f": 50,
               "report": "KNYC 151851Z 27004KT 10SM CLR 10/01 A3021 RMK AO2 T01000006"}
    duplicated = {**tighter, "source": "NWS", "available_at": "2026-11-15T19:09:00Z"}
    other_site = {**tighter, "at": "2026-11-15T19:51:00Z", "available_at": "2026-11-15T20:05:00Z", "temp_f": 41,
                  "report": "KLGA 151951Z 27004KT 10SM CLR 05/01 A3021 RMK AO2 T00500006"}
    event = {"family": "daily-low", "station": "KNYC", "date": "2026-11-15", "observations": [initial, tighter, duplicated, other_site],
             "buckets": [{"id": f"tc-temp-nyclow-2026-11-15-{slug}", "rules": common + condition + "?"}
                         for slug, condition in (("lt51f", "less than or equal to 50F"), ("gte51f", "greater than or equal to 51F"))]}
    scheduled_fit = {"data": tmp_path, "vintage": "fixture-scheduled", "evidence_cutoff": "2026-10-01T00:00:00Z",
                     "groups": [{"station": "KNYC", "hour": 13, "width_f": 1.4, "residuals": [("2026-10-02", 0)],
                                 "exceptions": {"uncapped_weight": 0.10}}]}
    artifact = {"stations": ["KNYC"], "vintage": "fixture-receipt", "scheduled": scheduled_fit,
                "qualification_study_id": "fixture-study", "qualification_coverage": {"calendar_dates": 16},
                "qualification_evidence_cutoff": "2026-11-15T12:00:00Z", "qualification_written_at": "2026-11-15T13:00:00Z"}
    guidance = {"remaining_low_f": 56, "observed_low_f": 52, "forecast": {"source": "fixture NOAA alert",
                "issued_at": "2026-11-15T12:00:00Z", "available_at": "2026-11-15T13:00:00Z", "covered_to": "2026-11-16T05:00:00Z"}}
    monkeypatch.setattr(expansion_live_precision.expansion_live_context, "physical_context", lambda *args: guidance)
    early = expansion_live_low_receipts.predict(artifact, event, "2026-11-15T19:01:59Z")
    then = expansion_live_low_receipts.predict(artifact, event, "2026-11-15T19:02:00Z")
    late = expansion_live_low_receipts.predict(artifact, event, "2026-11-15T20:10:00Z")
    assert early["receipt_status"] == "scheduled-only"
    assert early["knowledge_at"] == "2026-11-15T18:00:00Z"
    assert then["receipt_status"] == "tighter-report"
    assert then["metar_upper_bound_f"] == 50
    assert then["model_low_f"] == 50
    assert then["scheduled_model_low_f"] == 52
    assert then["knowledge_at"] == late["available_at"] == "2026-11-15T19:02:00Z"
    assert then["forecast"] == early["forecast"] == late["forecast"]
    assert then["buckets"][0]["probability"] > early["buckets"][0]["probability"]
    assert late["buckets"] == then["buckets"]
    assert expansion_live_low_receipts.decision_clocks(artifact, event, "2026-11-15T20:10:00Z") == ["2026-11-15T19:02:00Z"]
    correction = {**duplicated, "available_at": "2026-11-15T20:12:00Z", "temp_f": 51,
                  "report": "KNYC 151851Z COR 27004KT 10SM CLR 11/01 A3021 RMK AO2 T01060006"}
    event["observations"].append(correction)
    corrected = expansion_live_low_receipts.predict(artifact, event, "2026-11-15T20:14:00Z")
    assert corrected["receipt_status"] == "scheduled-only"
    assert corrected["buckets"] == early["buckets"]


def test_early_six_hour_metar_cannot_create_a_later_first_receipt():
    from weather_edge.expansion_live_low_receipts import tightened_bound
    from weather_edge.nbm import utc

    start = utc("2026-11-15T05:00:00Z")
    scheduled = utc("2026-11-15T17:00:00Z")
    report = {"at": "2026-11-15T17:53:00Z", "available_at": "2026-11-15T17:57:00Z",
              "report": "KNYC 151753Z 29004KT 10SM FEW035 10/03 A3039 RMK AO2 T01000028 20000"}
    now = utc("2026-11-15T18:03:00Z")
    assert tightened_bound([report], "KNYC", start, scheduled, now, 43) is None
    later = {**report, "available_at": "2026-11-15T18:01:00Z"}
    assert tightened_bound([later], "KNYC", start, scheduled, now, 43)["upper_f"] == 32
    assert tightened_bound([report, later], "KNYC", start, scheduled, now, 43) is None


def test_intraday_hrrr_waits_for_entire_newer_cycle_and_local_receipt(tmp_path, monkeypatch):
    from weather_edge import hrrr

    def trace(*args, cycle, provenance):
        return [{"valid_at": f"2026-11-15T{hour}:00:00Z", "issued_at": f"2026-11-15T{cycle}:00:00Z",
                 "available_at": f"2026-11-15T{publish}", "ingested_at": f"2026-11-15T{receive}"}
                for hour, publish, receive in ([(22, "17:00:00Z", "17:02:00Z"), (23, "17:10:00Z", "17:12:00Z")]
                    if cycle == 12 else [(22, "19:10:00Z", "19:12:00Z"), (23, "21:59:00Z", "22:00:01Z")])]

    monkeypatch.setattr(hrrr, "station_trace", trace)
    end = "2026-11-16T00:00:00Z"
    args = (tmp_path, "KNYC", "2026-11-15", "2026-11-15T22:00:00Z", end)
    assert {row["issued_at"][11:13] for row in hrrr.complete_remaining_trace(*args)} == {"18"}
    assert {row["issued_at"][11:13] for row in hrrr.complete_remaining_trace(*args, local_receipt=True)} == {"12"}


def test_native_hrrr_probe_honors_pinned_issue_and_local_clock(tmp_path, monkeypatch):
    from hashlib import sha256
    import json

    from weather_edge import hrrr

    valid = "2026-11-15T22:00:00Z"
    late = "2026-11-15T18:00:00Z"
    for hour, receipt in ((12, "2026-11-15T21:01:00Z"), (18, "2026-11-15T22:00:01Z")):
        issue = datetime(2026, 11, 15, hour, tzinfo=timezone.utc)
        path = hrrr.frame_path(tmp_path, issue, 22 - hour)
        path.parent.mkdir(parents=True)
        raw = str(hour).encode()
        path.with_suffix(".grib2").write_bytes(raw)
        path.write_text(json.dumps({"source": "original field", "valid_at": valid,
                                    "issued_at": issue.isoformat(), "sha256": sha256(raw).hexdigest(),
                                    "available_at": "2026-11-15T21:00:00Z", "ingested_at": receipt,
                                    "native_units": "K", "resolution_m": 3000}))
    monkeypatch.setattr(hrrr, "decode_station_cells", lambda raw, *args, **kwargs: {
        "point": {"temp_f": int(raw), "distance_km": 1.0}})
    args = (tmp_path, 40.0, -75.0, valid, valid)
    assert hrrr.point(*args)["temp_f"] == 18
    assert hrrr.point(*args, local_receipt=True)["temp_f"] == 12
    assert hrrr.point(*args, local_receipt=True, pinned_issue=late) is None
    assert hrrr.point(tmp_path, 40.0, -75.0, "2026-11-15T22:00:01Z", valid,
                      local_receipt=True, pinned_issue=late)["temp_f"] == 18


def test_madis_dense_timeline_uses_local_editions_and_keeps_corrections(tmp_path):
    import json

    from weather_edge import omo

    hour = datetime(2026, 11, 15, 19, tzinfo=timezone.utc)
    directory = omo.path_for(tmp_path, hour, live=True).parent
    directory.mkdir(parents=True)
    point = {"at": "2026-11-15T19:15:00Z", "madis_modified_at": "2026-11-15T19:17:01Z",
             "temp_c_whole": 6, "temp_f": 42.8}
    for index, (local, temp) in enumerate((("19:22:00Z", 6), ("19:23:00Z", 6), ("19:28:00Z", 7), ("19:30:00Z", 6))):
        record = {"ingested_at": f"2026-11-15T{local}", "available_at": "2026-11-15T19:20:00Z", "source": "NOAA public file",
                  "sha256": str(index), "stations": {"KMDW": [{**point, "temp_c_whole": temp}]}}
        (directory / f"{index}-live.json").write_text(json.dumps(record))
    rows = omo.station_trace(tmp_path, "KMDW", hour, hour + timedelta(hours=1), original_receipts=True)
    assert [row["temp_c_whole"] for row in rows] == [6, 7, 6]
    assert rows[0]["available_at"] == "2026-11-15T19:22:00Z"
    assert rows[0]["source_at"] == "2026-11-15T19:20:00Z"
    assert all(row["display_only"] for row in rows)


def test_hrrr_shadow_uses_pinned_qualification_and_same_observed_floor(tmp_path, monkeypatch):
    from weather_edge import expansion_live_hrrr_high as shadow

    event = {"station": "KPHL", "date": "2026-11-15", "venue": "polymarket-us",
             "climate_starts_at": "2026-11-15T05:00:00Z", "climate_ends_at": "2026-11-16T05:00:00Z",
             "buckets": [{"id": "lt64", "lower_f": None, "upper_f": 63}, {"id": "gte64", "lower_f": 64, "upper_f": None}],
             "observations": [{"source": "Aviation Weather Center", "at": "2026-11-15T20:54:00Z",
                               "available_at": "2026-11-15T20:58:00Z",
                               "report": "KPHL 152054Z 11008KT 10SM CLR 18/00 A3001 RMK AO2 T01830000"}]}
    artifact = {"data": tmp_path, "study_id": "earlier-study", "qualified_at": "2026-11-15T21:30:00Z", "vintage": "immutable",
                "hours": [13, 17], "disagreement_reserve": .02,
                "residuals": {"KPHL": {"17": {name: [-1, 0, 1] for name in ("nbm", "hrrr", "blend")}}}}
    forecast = {"issued_at": "2026-11-15T18:00:00Z", "hourly_source": "NBM", "available_at": "2026-11-15T19:10:00Z"}
    monkeypatch.setattr(shadow.strategy_high_transfer, "_live_remaining", lambda *args: (forecast, 68))

    def hrrr(*args, local_receipt):
        assert local_receipt
        return [{"temp_f": 60, "issued_at": "2026-11-15T18:00:00Z", "ingested_at": "2026-11-15T19:35:00Z"}]

    monkeypatch.setattr(shadow.hrrr, "complete_remaining_trace", hrrr)
    assert shadow.timeline(artifact, event, "2026-11-15T21:59:59Z") == []
    live, = shadow.timeline(artifact, event, "2026-11-15T22:00:01Z")
    assert live["knowledge_at"] == "2026-11-15T22:00:00Z"
    assert live["raw_centers"] == {"nbm": 68, "hrrr": 65, "blend": 65}
    assert live["buckets"][0]["hrrr"]["probability"] < .02
    assert shadow.timeline(artifact, {**event, "venue": "kalshi"}, "2026-11-15T22:00:01Z") == []
