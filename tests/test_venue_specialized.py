from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from weather_edge import venue_arctic, venue_enso, venue_index, venue_index_audit, venue_index_model, venue_monthly_rain, venue_rain_replay, venue_storm
from weather_edge.market import iso, parse_time
from weather_edge.venue_contracts import kalshi_market


def native_market(series, event, rules, secondary=""):
    symbol = event + "-T2"
    return kalshi_market(series, {"ticker": symbol, "event_ticker": event, "rules_primary": rules,
                                 "rules_secondary": secondary, "strike_type": "greater", "floor_strike": 2},
                         "https://external-api.kalshi.com/trade-api/v2/markets", "2026-11-15T19:15:00Z")


def test_daily_high_uses_named_station_with_exact_day_without_guessing_contract_clock():
    series = {"ticker": "KXHIGHTDC", "title": "Washington DC Daily Max Temp", "frequency": "daily",
              "contract_terms_url": "https://assets.kalshi.com/contract_terms/GLOBALTEMPERATURE.pdf"}
    row = native_market(series, "KXHIGHTDC-26NOV16", "If the maximum temperature recorded at Washington DC (CLIDCA) "
                        "for Nov 16, 2026, is greater than 2° fahrenheit according to The Weather Company.")
    assert (row["family"], row["station"], row["day"]) == ("daily-high", "KDCA", "2026-11-16")
    assert row.get("climate_starts_at") is None


def test_monthly_rain_rules_preserve_source_and_o_hare_station_change():
    series = {"ticker": "KXRAINCHIM", "title": "Rain Chicago", "frequency": "monthly",
              "contract_terms_url": "https://assets.kalshi.com/contract_terms/RAINM.pdf"}
    earlier = native_market(series, "KXRAINCHIM-26FEB", "If the total precipitation at CLIMDW in Chicago in Feb 2026 "
                            "is strictly greater than 2 inches.", "The primary resolution link will be a sum of NOAA’s Climate Data "
                            "Online Daily Summaries of precipitation. Otherwise NWS Daily Climate Report.")
    later = native_market(series, "KXRAINCHIM-26NOV", "If the total precipitation at CLIORD in Chicago in Nov 2026 "
                          "is strictly greater than 2 inches.", "The corresponding TWC Report (Daily). This Contract has been amended.")
    assert (earlier["station"], later["station"]) == ("KMDW", "KORD")
    assert earlier["resolution_source"] == "NOAA NCEI Daily Summaries"
    assert later["resolution_source"] == "The Weather Company"
    assert later["report_policy"] == "TWC Daily report; amended material-error terms"
    assert later["climate_starts_at"] == "2026-11-01T06:00:00Z"
    assert later["climate_ends_at"] == "2026-12-01T06:00:00Z"


def test_climate_rain_parser_respects_exact_label_day_and_precipitation_section():
    product = "202611130625-KOKX-CDUS41-CLINYC"
    body = """CLINYC
CLIMATE REPORT
NATIONAL WEATHER SERVICE NEW YORK NY
...THE CENTRAL PARK CLIMATE SUMMARY FOR NOVEMBER 12 2026...
PRECIPITATION (IN)
  YESTERDAY      T       1.20
  MONTH TO DATE  2.57

SNOWFALL (IN)
  YESTERDAY      0.0
  MONTH TO DATE  99.0
"""
    row = venue_monthly_rain._parse_cli(body, "KNYC", date(2026, 11, 12), product)
    assert row["daily_raw"] == "T"
    assert row["monthly_raw"] == "2.57"
    assert venue_monthly_rain._parse_cli(body, "KNYC", date(2026, 11, 13), product) is None


def test_rain_metar_parser_excludes_vicinity_remarks_trend_and_incomplete_negative():
    raw = "METAR KMIA 192353Z 12008KT 10SM -SHRA RASN VCSH FEW020 RMK RA TEST"
    assert venue_rain_replay._primary_weather(raw) == ["-SHRA", "RASN"]
    assert venue_rain_replay._primary_weather("METAR KMIA 192153Z 10SM VCSH FEW020 TEMPO +RA RMK RA") == []
    start = parse_time("2026-10-19T05:00:00Z")
    end = start + timedelta(hours=24)
    no_rain = [{"icaoId": "KMIA", "metarType": "METAR", "obsTime": int((start + timedelta(hours=i)).timestamp()),
                "receiptTime": iso(start + timedelta(hours=i, minutes=2)), "rawOb": "METAR KMIA 10SM FEW020"}
               for i in range(25)]
    assert venue_rain_replay._metar_resolution(no_rain, "KMIA", start, end)["expected_yes"] is False
    assert venue_rain_replay._metar_resolution(no_rain[:-1], "KMIA", start, end)["expected_yes"] is None
    assert venue_rain_replay._metar_resolution(no_rain[1:], "KMIA", start, end)["expected_yes"] is None


def test_index_receipts_use_eligible_prior_minute_and_ignore_future_restatement():
    minute = parse_time("2026-11-15T18:30:00Z")
    decision = minute + timedelta(minutes=6)
    latest = minute + timedelta(minutes=1)
    point = {"at": iso(minute), "temperature_f": "49.10", "received_at": iso(minute + timedelta(minutes=5)), "body": {}}
    revised_future = dict(point, temperature_f="48.8", received_at=iso(decision + timedelta(seconds=1)))
    later = dict(point, at=iso(latest), temperature_f="49.12", received_at=iso(decision + timedelta(seconds=2)))
    series = [minute, latest], {minute: [point, revised_future], latest: [later]}
    assert venue_index_model._issued(series, decision) is later
    assert venue_index_model._issued(series, decision, local=True) is point


def test_index_allocates_accepted_weights_and_applies_celsius_offsets():
    point = {"contributors": 1, "body": {"stations": [{"code": "ok", "station_id": "A", "temp_f": 42.5}]}}
    config = {"city_reference_c": "0.4", "stations": [{"station_id": "A", "offset_c": "1", "weight": "0.7"},
                                                         {"station_id": "B", "offset_c": "-1", "weight": "0.3"}]}
    reconstructed = venue_index_audit.recreate(point, config)
    assert reconstructed["reconstructed"] == Decimal("41.42")
    assert reconstructed["unadjusted"] == Decimal("42.5")
    assert reconstructed["all_members_present"] is False
    stamp = int(datetime(2026, 11, 15, tzinfo=timezone.utc).timestamp() * 1000)
    unavailable = {"city": "nyc", "units": "fahrenheit", "timeseries": [{"t": stamp, "status": "unavailable", "v": None}]}
    assert list(venue_index._rows_by_time(unavailable, "nyc", stamp, stamp))[0][3] is None
    unavailable["timeseries"][0]["status"] = "normal"
    with pytest.raises(ValueError):
        list(venue_index._rows_by_time(unavailable, "nyc", stamp, stamp))


def test_atlantic_storm_counts_respect_basin_specific_maximum():
    identity = "AL022022"
    basin = {identity: {"max_wind_kt": 50, "first_date": "2022-07-01", "last_date": "2022-07-02"}}
    tracks = [{"id": identity, "name": "BONNIE", "observations": [
        {"at": "2022-07-01T12:00:00Z", "wind_kt": 50, "category": "TS"},
        {"at": "2022-07-03T12:00:00Z", "wind_kt": 100, "category": "HU"},
    ]}]
    assert [item["id"] for item in venue_storm._wind_events(tracks, basin, 2022, "storms")] == [identity]
    assert venue_storm._wind_events(tracks, basin, 2022, "major hurricanes") == []


def test_enso_contract_season_compares_only_with_single_period_official_forecast():
    series = {"ticker": "KXRONI", "title": "El Niño strength", "contract_terms_url": venue_enso.TERMS}
    daily = native_market(series, "KXRONI-27APR", "If the peak RONI value during the NDJ 2026-27 season is at least 2.0°C.")
    daily["operator"] = "at-least"
    broad = native_market(series, "KXRONI-27AUG", "If the peak RONI value during the 2026-27 El Niño season is at least 2.0°C.")
    body = b"<h2>Issued November 2026</h2><table><tr><th>NDJ Nov Dec Jan</th>" + b"<td>0</td>" * 7 + b"<td>4</td><td>96</td></tr></table>"
    issue = {"strengths": venue_enso.parse_outlook(body, 11, "strengths")}
    assert daily["climate_starts_at"] == "2026-11-01T00:00:00Z"
    assert daily["climate_ends_at"] == "2027-02-01T00:00:00Z"
    assert venue_enso._forecast_probability(daily, issue)[0] == .96
    assert venue_enso._forecast_probability(broad, issue)[0] is None


def test_global_rule_drives_fixed_threshold_and_keeps_reference_year():
    series = {"ticker": "KXGTEMP", "title": "How hot will 2026 be?",
              "contract_terms_url": "https://assets.kalshi.com/contract_terms/GLBTEMP.pdf"}
    row = native_market(series, "KXGTEMP-27", "If the global temperature anomaly in 2026 is greater than 1.28 degrees Celsius "
                                              "and above the 2025 value, this contract resolves Yes.")
    assert (row["day"], row["operator"], row["lower"]) == ("2026-01-01", "above-threshold-and-reference", "1.28")
    assert "2025 value" in row["report_policy"]


def test_arctic_conflicting_direction_and_strict_published_precision():
    series = {"ticker": "KXARCTICICEMIN", "title": "Arctic minimum", "contract_terms_url": venue_arctic.TERMS}
    primary = "If the National Snow & Ice Data Center records a day between December 19, 2025 and October 01, 2026 "
    market = {"event_ticker": "KXARCTICICEMIN-26OCT01", "ticker": "KXARCTICICEMIN-26OCT01-T4.6",
              "rules_primary": primary + "where the extent of Arctic sea ice is below 4.6 million square kilometers.",
              "strike_type": "less", "cap_strike": 4600000}
    normalized = kalshi_market(series, market, "public", "2026-11-15T19:15:00Z")
    assert (normalized["day"], normalized["units"], normalized["operator"], normalized["upper"]) == (
        "2025-12-19", "million km²", "below", "4.6")
    assert venue_arctic._beyond("4.599", "below", "4.6") == 1
    assert venue_arctic._beyond("4.60", "below", "4.6") == 0
    market["rules_primary"] = market["rules_primary"].replace("below", "above")
    conflict = kalshi_market(series, market, "public", "2026-11-15T19:15:00Z")
    assert conflict["operator"] == "conflicting"
    assert conflict["upper"] is None


def test_period_low_uses_exact_eastern_calendar_bounds_across_clock_change():
    series = {"ticker": "KXLOWTPERIOD", "title": "Period temperature",
              "contract_terms_url": "https://assets.kalshi.com/contract_terms/GLOBALTEMPERATURE.pdf"}
    market = native_market(series, "KXLOWTPERIOD-26NOV02NYC",
                           "If the minimum temperature recorded at New York City (KNYC) for Nov 1 to 2, 2026 "
                           "(inclusive, America/New_York local calendar dates), is less than 2° Fahrenheit "
                           "according to The Weather Company, then the market resolves to Yes.",
                           "The period minimum is the lowest of the official daily minimum temperatures.")
    assert market["family"] == "period-low"
    assert market["climate_starts_at"] == "2026-11-01T04:00:00Z"
    assert market["climate_ends_at"] == "2026-11-03T05:00:00Z"
    assert market["units"] == "°F"


def test_legacy_midway_and_trace_rule_keep_the_exact_named_station_and_day():
    series = {"ticker": "KXHIGHCHI", "title": "Daily high temperature in Chicago", "frequency": "daily"}
    late = native_market(series, "HIGHCHI-21AUG19", "If the highest temperature recorded at Midway International Airport, IL, "
                        "for August 19th, 2021 is strictly greater than 81°F")
    assert (late["station"], late["day"], late["day_basis"]) == ("KMDW", "2021-08-19", "contract rule")
    assert late.get("climate_starts_at") is None

    rain = {"ticker": "KXRAINNYC", "title": "Daily rain in NYC", "frequency": "daily"}
    primary = ("If the number of inches of precipitation recorded in Central Park, New York on April 03, 2026 "
               "as reported by the National Weather Service's Daily Climate Report is strictly greater than 0")
    traced = native_market(rain, "KXRAINNYC-26APR03", primary,
                           'The value "T" ("trace") corresponds to a value greater than 0 but less than 0.01.')
    assert (traced["station"], traced["operator"], traced["lower"]) == ("KNYC", "above-or-trace", "0")
    assert traced["climate_starts_at"] == "2026-04-03T05:00:00Z"


def test_storm_total_and_election_rain_keep_exact_observation_dates_without_inventing_utc_hours():
    terms = "https://assets.kalshi.com/contract_terms/RAINGLOBALPERIOD.pdf"
    series = {"ticker": "KXRAINSBOS", "title": "Boston rain total", "contract_terms_url": terms}
    row = native_market(series, "KXRAINSBOS-26SEP26-27SEP26",
                        "If the total precipitation at Boston Logan Airport (KBOS; BOS on the TWC daily dashboard) in Boston "
                        "in September 26–27, 2026 is strictly greater than 2.5 inches, then the market resolves to Yes.",
                        "The total is the sum of The Weather Company's daily values from September 26, 2026 through September 27, 2026, "
                        "inclusive. If The Weather Company publishes a total for that exact period, that total takes precedence.")
    assert (row["family"], row["station"], row["day"], row["operator"], row["lower"]) == (
        "period-rain", "KBOS", "2026-09-26", "above", "2.5")
    assert "2026-09-27 inclusive" in row["day_basis"]
    assert row.get("climate_starts_at") is None and row.get("climate_ends_at") is None

    election = {"ticker": "KXRAINELECTION", "title": "Election Day rain", "contract_terms_url": terms}
    phoenix = native_market(election, "KXRAINELECTION-03NOV26", "If the total precipitation at Phoenix (KPHX) in Phoenix "
                            "in November 3, 2026 is strictly greater than 0 inches, then the market resolves to Yes.",
                            "Trace amounts and missing daily precipitation values are counted as 0 inches.")
    assert (phoenix["family"], phoenix["station"], phoenix["day"], phoenix["resolution_source"]) == (
        "election-rain", "KPHX", "2026-11-03", "The Weather Company")


def test_lake_temperature_and_emissions_use_file_specific_units_and_strikes():
    michigan = native_market({"ticker": "KXMICHTEMP", "title": "Lake Michigan water temperature"}, "MICHTEMP-22AUG29",
                            "If the average surface water temperature on Lake Michigan is above 21.25° Celsius on August 29, 2022 "
                            "according to NOAA, then the market resolves to Yes.")
    assert (michigan["family"], michigan["day"], michigan["operator"], michigan["lower"]) == (
        "lake-surface-temperature", "2022-08-29", "above", "21.25")
    assert michigan["resolution_source"].endswith("(GLSEA)")
    emissions = native_market({"ticker": "KXUSCLIMATE", "title": "US climate goals"}, "USCLIMATE-2025",
                             "If the US has 4909.9 million metric tonnes of CO2 emissions or fewer in a year by 2025, "
                             "then the market resolves to Yes.")
    assert (emissions["family"], emissions["operator"], emissions["upper"], emissions["units"]) == (
        "annual-emissions", "at-most", "4909.9", "million metric tonnes CO₂e")
    assert "2024 through 2025" in emissions["day_basis"]


def test_legacy_instrument_alias_requires_independent_exact_event_membership():
    series = {"ticker": "KXHIGHAUS", "title": "Daily high temperature in Austin", "frequency": "daily"}
    market = {"ticker": "HIGHAUS--23MAY11-T91", "event_ticker": "HIGHAUS-23MAY11"}
    event = {"event_ticker": "HIGHAUS-23MAY11", "series_ticker": "KXHIGHAUS", "title": "Austin on May 11"}
    at = "2026-11-15T19:15:00Z"
    with pytest.raises(ValueError):
        kalshi_market(series, market, "public", at)
    with pytest.raises(ValueError):
        kalshi_market(series, market, "public", at, event={**event, "series_ticker": "UNRELATED"})
    result = kalshi_market(series, market, "public", at, event=event)
    assert result["instrument_id"] == market["ticker"] and result["event_id"] == event["event_ticker"]
    ordinary = kalshi_market(series, {**market, "ticker": market["event_ticker"]}, "public", at)
    assert ordinary["instrument_id"] == ordinary["event_id"]
