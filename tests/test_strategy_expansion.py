from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

from weather_edge import venue_archive
from weather_edge.market import iso, parse_time
from weather_edge.strategy_advection import direction_difference
from weather_edge.strategy_alta import december
from weather_edge.strategy_alta_guard import guard_decembers
from weather_edge.strategy_clock_sensitivity import ClockRule
from weather_edge.strategy_crossvenue import comparable_quote_cash, complementary_quote_cash
from weather_edge.strategy_dewpoint import precise_pair, predict_runtime as predict_dewpoint
from weather_edge.strategy_exposure import event_exposure, exit_context
from weather_edge.strategy_latency import recheck_cause
from weather_edge.strategy_live_high_evidence import compare as compare_live_high
from weather_edge.strategy_midnight_metar import midnight_summary
from weather_edge.strategy_native_capture import book_clocks, merge_supplemental, polymarket_events
from weather_edge.strategy_pacific_storm import track_evidence
from weather_edge.strategy_partial_prices import feasible_intervals
from weather_edge.strategy_poly_close_clocks import closed_clock
from weather_edge.strategy_pressure import pressure_point, qualified_change
from weather_edge.strategy_snow_days import reported_amount, scalar_payout
from weather_edge.strategy_snow_events import coverage
from weather_edge.strategy_snow_holidays import label
from weather_edge.strategy_thanksgiving import daily_precipitation, thanksgiving
from weather_edge.strategy_thanksgiving_gefs import member_bounds


def test_receipt_first_query_keeps_all_bucket_siblings_and_excludes_other_venue(tmp_path, monkeypatch):
    start = "2026-11-15T05:00:00Z"
    close = "2026-11-16T05:00:00Z"
    sample = "2026-11-15T19:00:00.000000Z"
    market = {
        "venue": venue_archive.POLY, "event_id": "native-low", "native_event_id": "event-id", "event_title": "Low",
        "family": "daily-low", "station": "KNYC", "day": "2026-11-15", "opened_at": start, "closed_at": close,
        "climate_starts_at": start, "climate_ends_at": close, "resolution_source": "NWS Daily Climate Report",
        "units": "°F", "rules_primary": "Exact observed minimum", "native_status": "OPEN",
        "quantity_step": "1", "yes_side_id": "yes", "no_side_id": "no",
        "first_received_at": venue_archive.receipt_time(start), "last_received_at": sample,
        "receipt_id": 1, "raw_market": "{}",
    }
    first = {**market, "instrument_id": "low-1", "native_id": "one", "label": "40 or below",
             "operator": "at-most", "upper": "40"}
    second = {**market, "instrument_id": "low-2", "native_id": "two", "label": "41 or above",
              "operator": "at-least", "lower": "41"}
    with venue_archive.connect(tmp_path) as db, db:
        reply = SimpleNamespace(body=b"{}", source="https://example.invalid", requested_at=sample,
                                received_at=sample, status=200, headers={})
        receipt = venue_archive.save_receipt(db, venue_archive.POLY, "book", reply)
        for row in (first, second, {**first, "venue": venue_archive.KALSHI}):
            db.execute(f"INSERT INTO markets({','.join(row)}) VALUES ({','.join('?' for _ in row)})", tuple(row.values()))
        for venue, instrument in ((venue_archive.POLY, "low-1"), (venue_archive.KALSHI, "low-1")):
            venue_archive.save_book(db, venue, instrument, receipt, {"at": sample, "depth_kind": "gap"})
    events, issues = polymarket_events(tmp_path, sample, since=sample)
    assert not issues and [event["id"] for event in events] == ["native-low"]
    assert [(row["id"], len(row["books"])) for row in events[0]["buckets"]] == [("low-1", 1), ("low-2", 0)]
    assert polymarket_events(tmp_path, close, since="2026-11-15T19:00:01Z") == ([], [])

    def unexpected_books(*args, **kwargs):
        raise AssertionError("A compact summary opened a full ladder body")

    monkeypatch.setattr(venue_archive, "_book_query", unexpected_books)
    summaries, issues = polymarket_events(tmp_path, sample, since=sample, summarize_books=True)
    assert not issues
    summary, = summaries
    full, = events
    assert {k: v for k, v in summary.items() if k != "buckets"} == {
        k: v for k, v in full.items() if k != "buckets"
    }
    assert [(row["id"], row["books"], set(book_clocks(row))) for row in summary["buckets"]] == [
        ("low-1", [], {parse_time(sample)}), ("low-2", [], set()),
    ]


def test_crossvenue_same_side_and_complement_price_use_whole_size_and_fee_multiplier():
    quote = {"polymarket_at": "2026-11-15T21:45:00Z", "same_sides": ["YES"],
             "polymarket_quotes": {"YES": {"price": 0.4, "size": 10}},
             "kalshi_quotes": {"YES": {"price": 0.5, "size": 3.9}, "NO": {"price": 0.5, "size": 2.2}}}
    terms = {"available": True, "taker_rate": "0.07", "multiplier": "2"}
    same, = comparable_quote_cash(quote, terms)
    assert same["common_whole_contracts"] == 3 and same["kalshi"]["debit"] == "1.61"
    comp, = complementary_quote_cash(quote, terms)
    assert comp["common_whole_contracts"] == 2
    assert comp["polymarket"]["side"] == "YES" and comp["kalshi"]["side"] == "NO"
    assert comp["kalshi"]["debit"] == "1.07"
    assert complementary_quote_cash(quote, {"available": False}) == []


def test_supplemental_books_join_existing_partition_and_preserve_both_end_clocks():
    booked = {"at": "2026-11-15T21:45:00Z", "source_at": "2026-11-15T21:44:59Z", "bids": [], "offers": []}
    optional = {"at": "2026-11-15T21:45:30Z", "source_at": "2026-11-15T21:45:29Z", "bids": [], "offers": []}
    bucket = {"id": "option", "market_id": "one", "yes_side_id": "yes", "no_side_id": "no",
              "lower_f": None, "upper_f": 54, "quantity_step": "0.01"}
    event = {"id": "midway", "native_id": "market", "date": "2026-11-15", "station": "KMDW",
             "climate_starts_at": "2026-11-15T06:00:00Z", "climate_ends_at": "2026-11-16T06:00:00Z",
             "ends_at": "2026-11-16T04:59:00Z", "buckets": [{**bucket, "books": [booked]}]}
    source = {**event, "venue": venue_archive.POLY, "family": "daily-high", "ends_at": "2026-11-16T11:00:00Z",
              "native_market_ends_at": "2026-11-16T11:00:00Z", "buckets": [{**bucket, "books": [booked, optional]}]}
    tape = {"events": [event]}
    assert merge_supplemental(tape, [source]) == (["midway"], [])
    joined, = tape["events"]
    assert joined["ends_at"] == source["ends_at"]
    assert joined["legacy_event_ends_at"] == event["ends_at"]
    assert joined["native_market_ends_at"] == source["ends_at"]
    assert joined["supplemental_book_receipts"] == 1
    assert [row["source_time_progress"] for row in joined["buckets"][0]["books"]] == ["first", "advanced"]
    assert event["buckets"][0]["books"] == [booked]
    changed = {**source, "buckets": [{**bucket, "upper_f": 55, "books": [optional]}]}
    accepted, issues = merge_supplemental(tape, [changed])
    assert not accepted and "different contract terms" in issues[0]["reason"]
    assert tape["events"] == [joined]


def test_guard_archive_extracts_third_month_of_snow_and_preserves_zero():
    rows = [[], [], [], [], [], ["WINTER YEAR", None, "October", "November", "December"],
            [None, None, "Snow", "H20", "Snow", "H20", "Snow", "H20"],
            ["1999 to 00", None, 10, 1.2, 31, 4, 0, 0], ["2000 to 01", None, None, None, 20, 2, None, None]]
    assert guard_decembers({"address": "A1:H9", "values": rows}) == {
        1999: {"guard_december_in": 0.0, "guard_workbook_row": 8, "guard_winter_label": "1999 to 00"},
    }


def test_recheck_cause_distinguishes_book_and_forecast_updates():
    signal = {"probability": 0.8, "size": 10, "all_in": 7.25, "estimated_after_fee": 0.75, "uncertain": False}
    market = {**signal, "all_in": 7.9, "estimated_after_fee": 0.1}
    weather = {**signal, "probability": 0.73, "estimated_after_fee": 0.05}
    joint = {**signal, "probability": 0.77, "all_in": 7.6, "estimated_after_fee": 0.1}
    assert recheck_cause(signal, market, 0.25, compare_swaps=lambda: (market, signal))["category"] == "updated book alone removes edge"
    shifted_size = {**weather, "size": 2, "all_in": 1.5}
    weather_only = recheck_cause(signal, shifted_size, 0.25, compare_swaps=lambda: (signal, weather))
    assert weather_only["category"] == "updated forecast alone removes edge"
    assert weather_only["signal_probability_at_delayed_book_quantity"] == 10
    assert recheck_cause(signal, joint, 0.25, compare_swaps=lambda: (signal, signal))["category"] == "combined changes remove edge"
    assert recheck_cause(signal, {**signal, "size": None}, 0.25,
                         compare_swaps=lambda: (signal, signal))["category"] == "displayed liquidity gap"


def test_late_high_preview_uses_exact_origin_ladder_and_locked_weather_clock():
    early = "2026-11-15T21:59:45Z"
    at = "2026-11-15T22:00:15Z"
    end = "2026-11-15T22:15:00Z"
    eligible = {"at": at, "source_at": at, "depth_kind": "displayed_ladder", "state": "MARKET_STATE_OPEN",
                "http_cache": {"cf-cache-status": "EXPIRED"}, "bids": [["0.2", "20"]], "offers": [["0.3", "20"]]}
    bucket = {"id": "native-bucket", "label": "51–52", "quantity_step": "1",
              "books": [{**eligible, "at": early, "source_at": early}, eligible, {**eligible, "at": end, "source_at": end}]}
    event = {"id": "native-event", "station": "KPHL", "date": "2026-11-15", "climate_starts_at": "2026-11-15T05:00:00Z",
             "ends_at": "2026-11-16T04:59:00Z", "settles_at": "2026-11-16T11:00:00Z", "buckets": [bucket]}
    weather = {"probability": 0.7, "uncertain": False, "strategy_context": {"knowledge_at": "2026-11-15T22:00:00Z"}}
    snapshot = {"capture": {"events": [event]}, "split": "2026-11-15T17:07:28Z",
                "fee_schedule": [{"from": "2026-11-14T05:00:00Z", "coefficient": "0.095"}],
                "late_transfer_high_probabilities": {event["id"]: {iso(clock): {bucket["id"]: weather}
                                                                   for clock in (early, at, end)}}}
    rows = compare_live_high(snapshot, lambda *_: {bucket["id"]: {"probability": 0.2}})
    case, = rows["transfer", event["id"]]
    assert case["at"] == iso(at) and case["instrument_id"] == bucket["id"]
    assert case["shadow_paper_preview"][0]["paper_signal_at_fixed_floor"]
    assert not any(row["paper_signal_at_fixed_floor"] for row in case["baseline_paper_preview"])


def test_close_statistics_require_a_closed_exact_native_contract_and_ordered_trade():
    stats = {"closeSetTime": "2026-11-10T08:00:55Z", "lastTradeSetTime": "2026-11-10T08:00:03Z",
             "settlementSetTime": "2026-11-10T08:00:55Z"}
    book = {"marketSlug": "exact", "state": "MARKET_STATE_CLOSED", "stats": stats}
    assert closed_clock({"marketData": book}, "exact")["lastTradeSetTime"] == stats["lastTradeSetTime"]
    assert closed_clock({"marketData": book}, "other") is None
    assert closed_clock({"marketData": {**book, "state": "MARKET_STATE_OPEN"}}, "exact") is None
    reversed_stats = {**stats, "lastTradeSetTime": "2026-11-10T08:01:00Z"}
    assert closed_clock({"marketData": {**book, "stats": reversed_stats}}, "exact") is None


def test_pressure_change_requires_exact_station_and_both_original_madis_receipts(monkeypatch, tmp_path):
    from weather_edge import strategy_pressure

    original = {"at": "2026-11-15T18:54:00Z", "raw_report": "METAR KPHL 151854Z 30010KT 10SM CLR 08/02 A2974 RMK SLP997"}
    later = {"at": "2026-11-15T21:54:00Z", "raw_report": "METAR KPHL 152154Z 29005KT 10SM CLR 07/00 A2980 RMK SLP015"}
    assert pressure_point(original, "KPHL")["pressure_hpa"] == 999.7
    assert pressure_point(later, "KPHL")["pressure_hpa"] == 1001.5
    assert pressure_point(later, "KNYC") is None
    start, at = map(parse_time, ["2026-11-15T05:00:00Z", "2026-11-15T22:00:00Z"])
    rows = [(iso(row["at"]), row["raw_report"].removeprefix("METAR ")) for row in (original, later)]
    seen = {key: (iso(parse_time(key[0]) + timedelta(minutes=2)), "native-source") for key in rows}
    monkeypatch.setattr(strategy_pressure, "_hour_receipts", lambda *_: seen)
    event = qualified_change([original, later], "KPHL", start, at, tmp_path)
    assert round(event["trend_hpa_per_3h"], 2) == 1.8
    seen[rows[1]] = ("2026-11-15T22:00:01Z", "native-source")
    assert qualified_change([original, later], "KPHL", start, at, tmp_path) is None


def test_partial_quote_partition_keeps_missing_sides_and_checks_total_mass():
    bounds = [(Decimal("0.35"), Decimal("0.7")), (Decimal("0.45"), Decimal("0.6")), (Decimal("0"), Decimal("1"))]
    assert feasible_intervals(bounds) == [
        (Decimal("0.35"), Decimal("0.55")), (Decimal("0.45"), Decimal("0.60")), (Decimal("0"), Decimal("0.20")),
    ]
    assert feasible_intervals([(Decimal("0.7"), Decimal("0.8")), (Decimal("0.4"), Decimal("0.5"))]) is None


def test_full_exit_needs_opposing_origin_depth_for_the_held_side():
    at = parse_time("2026-11-15T22:00:00Z")
    book = {"at": iso(at), "source_at": iso(at), "depth_kind": "displayed_ladder", "state": "MARKET_STATE_OPEN",
            "http_cache": {"cf-cache-status": "EXPIRED"}, "bids": [["0.6", "2"]], "offers": [["0.7", "3"]]}
    event = {"ends_at": "2026-11-16T10:00:00Z", "buckets": [{"id": "option", "books": [book]}]}
    yes = {"bucket_id": "option", "side": "YES", "quantity": 2}
    no = {**yes, "side": "NO"}
    assert exit_context(event, yes, at)["full_net_proceeds"] == Decimal("1.15")
    assert exit_context(event, no, at)["full_net_proceeds"] == Decimal("0.56")
    less = exit_context(event, {**yes, "quantity": 4}, at)
    assert less["visible_exit_contracts"] == Decimal(2) and less["full_net_proceeds"] is None


def test_thanksgiving_raw_trace_and_wet_labels_remain_distinct():
    identity = "USW00013874"
    row = identity + "202511PRCP" + "    0T X" + "    3  X" + "   20A X" + ("-9999   " * 28)
    output = daily_precipitation(row.encode(), identity)
    assert output[thanksgiving(2025).replace(day=1)]["reported_measurable"] is False
    assert output[thanksgiving(2025).replace(day=2)]["reported_measurable"] is True
    assert thanksgiving(2025).replace(day=3) not in output
    assert thanksgiving(2025).isoformat() == "2025-11-27"


def test_gefs_holiday_amount_bounds_do_not_apportion_partial_intervals():
    start, end = map(parse_time, ["2026-11-26T06:00:00Z", "2026-11-26T14:00:00Z"])
    fields = {"member": 1, "available_at": "2026-11-15T14:00:00Z", "received_at": "2026-11-15T22:00:00Z"}
    rows = [{**fields, "starts_at": iso(start), "ends_at": iso(start + timedelta(hours=6)), "stations": {"KDFW": {"liquid_mm": 2}}},
            {**fields, "starts_at": iso(start + timedelta(hours=6)), "ends_at": iso(start + timedelta(hours=12)), "stations": {"KDFW": {"liquid_mm": 3}}}]
    valid = member_bounds(rows, "KDFW", start, end)
    assert (valid["lower_mm"], valid["upper_mm"], valid["full_sixes"], valid["overlapping_sixes"]) == (2, 5, 1, 2)
    assert member_bounds(rows[:1], "KDFW", start, end) is None


def test_snow_trace_keeps_separate_holiday_and_scalar_semantics():
    day = "2025-12-25"
    raw = {day: {"snow": ["0.0", "T"], "depth": ["0", " "]}}
    assert reported_amount(raw[day]["snow"]) == (Decimal(0), "trace")
    assert reported_amount(["1.4", "A"]) == (None, None)
    assert label(raw, day)["yes_including_trace"] is True
    assert label(raw, day)["white_christmas_depth_proxy"] is False
    terms = {"lower_payout_days": 1, "upper_payout_days": 4}
    assert scalar_payout(0, terms) == 0
    assert scalar_payout(2, terms) == Decimal("0.33")
    assert scalar_payout(7, terms) == 1


def test_snow_path_bounds_crossing_windows_and_rejects_coverage_gaps():
    begin = parse_time("2026-02-21T05:00:00Z")
    end = parse_time("2026-02-22T05:00:00Z")
    rows = [{"starts_at": iso(begin + timedelta(hours=-5 + hour)), "ends_at": iso(begin + timedelta(hours=1 + hour)),
             "amount_in": str(i), "product": "nbs"} for i, hour in enumerate((0, 6, 12, 18, 24), 1)]
    result = coverage(rows, begin, end)
    assert result["lower_in"] == "9" and result["upper_in"] == "15"
    assert coverage(rows[:2] + rows[3:], begin, end) is None


def test_otto_uses_only_pacific_part_of_duplicated_track():
    raw = b"\n".join((b"EP222016, OTTO, 3,", b"20161124, 1200, , HU, 11.0N, 83.0W, 100,",
                      b"20161125, 0330, T, TS, 10.7N, 85.6W, 60,", b"20161125, 0600, , TS, 10.6N, 86.2W, 60,"))
    row, = track_evidence(raw, (2016,))[2016]
    assert row["peak_outside_kt"] == 100 and row["peak_inside_kt"] == 60
    assert list(row["first_by_kind"]) == ["named storms"]


def test_paired_exchange_recheck_age_and_advance_are_independent():
    start = parse_time("2026-11-15T18:00:00Z")
    rule = ClockRule("two-sided", "origin-confirmed", 0.25, 30, True)
    signal = {"at": iso(start), "source_at": iso(start), "depth_kind": "displayed_ladder", "state": "MARKET_STATE_OPEN",
              "http_cache": {"cf-cache-status": "EXPIRED"}}
    unchanged = {**signal, "at": iso(start + timedelta(seconds=20))}
    assert rule.depth_status(unchanged, None, start + timedelta(seconds=20)) is None
    assert rule.recheck_status(signal, unchanged) == "Recheck exchange clock did not advance"
    advanced = {**signal, "at": iso(start + timedelta(seconds=35)), "source_at": iso(start + timedelta(seconds=20))}
    assert rule.recheck_status(signal, advanced) is None
    assert rule.depth_status({**advanced, "source_at": iso(start)}, None, start + timedelta(seconds=35))
    assert direction_difference(350, 10) == 20


def test_dew_point_requires_original_station_digits_and_eligible_clock():
    at = parse_time("2026-11-15T20:00:00Z")
    starts = parse_time("2026-11-15T05:00:00Z")
    row = {"at": "2026-11-15T19:54:00Z", "available_at": "2026-11-15T19:58:14.826950Z",
           "report": "METAR KPHL 151954Z 32012G24KT 10SM BKN055 11/M02 A3019 RMK AO2 SLP222 T01061017 $"}
    trial = precise_pair(row, "KPHL", starts, at)
    assert trial["temperature_c"] == 10.6 and trial["dewpoint_c"] == -1.7
    assert round(trial["depression_f"], 2) == 22.14
    assert precise_pair(row, "KMDW", starts, at) is None
    assert precise_pair({**row, "available_at": "2026-11-15T20:01:00Z"}, "KPHL", starts, at) is None
    assert precise_pair({**row, "report": row["report"].replace("T01061017", "T0106////")}, "KPHL", starts, at) is None
    vintage = {"stations": ["KPHL"], "study_created_at": "2026-11-15T20:15:00Z"}
    event = {"station": "KPHL", "date": "2026-11-15", "climate_starts_at": iso(starts),
             "climate_ends_at": "2026-11-16T05:00:00Z", "observations": [row]}
    assert predict_dewpoint(vintage, event, at) is None


def test_midnight_summary_requires_exact_climate_window_and_station():
    closes = parse_time("2026-11-14T05:00:00Z")
    row = {"at": "2026-11-14T04:51:00Z", "available_at": "2026-11-14T05:01:00Z",
           "raw_report": "KNYC 140451Z AUTO 34007KT 10SM OVC021 04/M01 A2975 RMK AO2 SLP066 T00391006 401110033 $"}
    evidence = midnight_summary(row, "KNYC", closes)
    assert (evidence["high_f"], evidence["low_f"], evidence["minutes_before_climate_end"]) == (52, 38, 9)
    assert midnight_summary(row, "KPHL", closes) is None
    assert midnight_summary(row, "KNYC", closes + timedelta(hours=1)) is None
    assert midnight_summary(row, "KNYC", closes - timedelta(hours=5)) is None


def test_simulated_opposite_sides_reduce_joint_worst_case_on_the_same_instrument():
    positions = [{"bucket_id": "same", "side": "YES", "quantity": 5.41, "max_loss": 1.1038, "ev_dollars": 0.50},
                 {"bucket_id": "same", "side": "NO", "quantity": 9.94, "max_loss": 6.671, "ev_dollars": 0.33}]
    risk = event_exposure(positions, None)
    assert risk["minimum_binary_payout"] == Decimal("5.41")
    assert risk["maximum_binary_payout"] == Decimal("9.94")
    assert risk["worst_net_at_ordinary_payout"] == Decimal("-2.3648")


def test_alta_sparse_days_need_published_total_or_conserved_accumulation():
    rows = [{"reported_at": "2024-11-30", "ytd_snow": 20.5, "new_snow": 2},
            {"reported_at": "2024-12-03", "ytd_snow": 24, "new_snow": 3.5},
            {"reported_at": "2024-12-29", "ytd_snow": 27, "new_snow": 3},
            {"reported_at": "2025-01-02", "ytd_snow": 29, "new_snow": 2}]
    result, issue = december(2024, rows, None)
    assert issue is None and result["december_in"] == 6.5 and result["cumulative_series_continuity"]
    revised = [*rows[:3], {**rows[3], "ytd_snow": 28.5}]
    assert december(2024, revised, None)[0] is None
    published, issue = december(2024, revised, "6.5")
    assert issue is None and published["direct_monthly_table"] == "6.5"
