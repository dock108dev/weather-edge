import csv
from datetime import datetime, timezone
import json

import pytest

from weather_edge import venue_archive, venue_history, venue_replay, venue_sources
from weather_edge.venue_contracts import kalshi_market, poly_market


DAY = "2026-11-15"
FIRST = "2026-11-15T17:00:00.100000Z"
LATER = "2026-11-15T17:01:00.100000Z"
SERIES = {"ticker": "KXHIGHNY", "title": "Highest temperature in NYC", "frequency": "daily",
          "contract_terms_url": "https://assets.kalshi.com/contract_terms/GLOBALTEMPERATURE.pdf",
          "fee_type": "quadratic", "fee_multiplier": 1}


def native_kalshi(ticker="KXHIGHNY-26NOV15-B49.5", *, status="active", **extra):
    return {
        "ticker": ticker, "event_ticker": ticker.rsplit("-", 1)[0], "market_type": "binary",
        "notional_value_dollars": "1.0000", "status": status, "title": "Highest temperature in NYC",
        "rules_primary": "If the maximum temperature recorded at New York City (CLINYC) for Nov 15, 2026, "
                         "is between 49-50° fahrenheit according to The Weather Company, then the market resolves to Yes.",
        "rules_secondary": "In the event of material error, markets resolve to the last fair price.",
        "floor_strike": 49, "cap_strike": 50, "strike_type": "between",
        "open_time": "2026-11-14T15:00:00Z", "close_time": "2026-11-16T06:00:00Z",
        **extra,
    }


def native_poly():
    event = {"id": 1, "slug": "temp-nychigh-2026-11-15", "title": "Highest temperature in NYC",
             "seriesSlug": "weather-daily-high-nyc"}
    market = {"id": 2, "slug": "tc-temp-nychigh-2026-11-15-gte49lt50f", "status": "MARKET_STATUS_RESOLVED",
              "description": "Will the highest temperature recorded at Central Park (KNYC) in New York City "
                             "for 2026-11-15 as reported by the National Weather Service's Climatological Report "
                             "(Daily) be between 49F and 50F? Outcome verified from NWS Climatological Report.",
              "title": "49 to 50", "marketSides": [{"id": 3, "long": True, "description": "Yes", "marketId": 2},
                                                {"id": 4, "long": False, "description": "No", "marketId": 2}]}
    event["markets"] = [market]
    return event, market


def reply(body, at=FIRST, status=200, headers=None):
    return venue_sources.Receipt("https://example.test/public", FIRST, at, status, headers or {}, json.dumps(body).encode())


def store(data, venue, normalized, raw, at=FIRST):
    received = reply(raw, at)
    with venue_archive.connect(data) as db, db:
        receipt_id = venue_archive.save_receipt(db, venue, "catalog", received)
        venue_archive.save_markets(db, [normalized], [raw], received, receipt_id)
    return receipt_id


def test_rain_close_lane_keeps_current_depth_and_prior_climate_days(tmp_path, monkeypatch):
    monkeypatch.setattr(venue_sources, "clock", lambda: datetime(2026, 11, 15, 19, 5, tzinfo=timezone.utc))

    def instrument(symbol, event, station, family, day, closed, operator=None):
        raw = {"id": symbol}
        store(tmp_path, venue_archive.POLY,
              {"venue": venue_archive.POLY, "instrument_id": symbol, "event_id": event, "station": station,
               "family": family, "day": day, "opened_at": "2026-11-14T08:00:00Z", "closed_at": closed,
               "status": "open", "operator": operator}, raw)

    for station in ("KNYC", "KMDW"):
        for i in range(6):
            instrument(f"{station}-low-{i}", f"{station}-low", station, "daily-low", DAY,
                       "2026-11-17T19:15:00Z")
        instrument(f"{station}-today-rain", f"{station}-today", station, "daily-rain", DAY,
                   "2026-11-16T19:15:00Z", "at-least")
        instrument(f"{station}-prior-rain", f"{station}-prior", station, "daily-rain", "2026-11-14",
                   "2026-11-15T19:15:21Z", "at-least")
        instrument(f"{station}-prior-closed", f"{station}-old", station, "daily-rain", "2026-11-14",
                   "2026-11-15T19:04:59Z", "at-least")
    kept = venue_sources._poly_book_targets(tmp_path, 16)
    assert len(kept) == 16
    assert sum("-low-" in symbol for symbol in kept) == 12
    assert set(kept[-4:]) == {"KNYC-today-rain", "KMDW-today-rain", "KNYC-prior-rain", "KMDW-prior-rain"}
    assert len(venue_sources._poly_book_targets(tmp_path, 14)) == 14


def test_query_export_raw_and_safe_related_contracts(tmp_path):
    kalshi = native_kalshi()
    kalshi_row = kalshi_market(SERIES, kalshi, reply(kalshi).source, FIRST)
    receipt_id = store(tmp_path, "kalshi", kalshi_row, kalshi)
    event, poly = native_poly()
    poly_row = poly_market(event, poly, reply(poly).source)
    store(tmp_path, "polymarket-us", poly_row, poly)

    rows = venue_archive.query(tmp_path, place="knyc", day=DAY, family="daily-high")
    assert rows["total"] == 2
    assert [row["venue"] for row in rows["rows"]] == ["kalshi", "polymarket-us"]
    assert venue_archive.query(tmp_path, venue="kalshi", status="settled")["total"] == 0
    assert venue_archive.query(tmp_path, text="B49.5")["total"] == 1

    detail = venue_archive.detail(tmp_path, "kalshi", kalshi_row["instrument_id"])
    assert detail["raw_market"] == kalshi
    related = detail["related"][0]
    assert related["comparison"]["fields"]["station"] == "same"
    assert related["comparison"]["fields"]["resolution_source"] == "different"
    assert related["comparison"]["same_weather_threshold"] is True
    assert related["comparison"]["payout_equivalent"] is False
    assert json.loads(venue_archive.raw_receipt(tmp_path, receipt_id)) == kalshi

    path = venue_archive.export_file(tmp_path, template := tmp_path / "archive.csv")
    assert path == template
    with path.open() as file:
        rows = list(csv.DictReader(file))
    assert len(rows) == 2
    assert rows[1]["yes_side_id"] == "3"


def test_csv_export_preserves_signed_numbers_and_marks_formula_like_text(tmp_path):
    row = {"venue": "kalshi", "instrument_id": "KX-TEST", "event_id": "KX-TEST",
           "title": " +Text", "series_id": "@Text", "label": "-4.5", "status": "open"}
    store(tmp_path, "kalshi", row, row)
    path = venue_archive.export_file(tmp_path, tmp_path / "archive.csv")
    with path.open(newline="") as handle:
        [saved] = list(csv.DictReader(handle))
    assert (saved["instrument_id"], saved["title"], saved["series_id"], saved["label"]) == (
        "KX-TEST", "' +Text", "'@Text", "-4.5",
    )
    [original] = list(venue_archive.export(tmp_path))
    assert (original["title"], original["series_id"], original["label"]) == (" +Text", "@Text", "-4.5")


def test_substring_index_matches_legacy_and_tracks_catalog_updates(tmp_path, monkeypatch):
    from weather_edge import venue_search

    raw = native_kalshi()
    row = kalshi_market(SERIES, raw, reply(raw).source, FIRST)
    store(tmp_path, "kalshi", row, raw)
    legacy = venue_archive.query(tmp_path, text="B49.5")
    with venue_archive.connect(tmp_path) as db:
        if not venue_search.ensure(db):
            pytest.skip("This SQLite build lacks the optional FTS5 trigram index")
    assert venue_archive.query(tmp_path, text="B49.5") == legacy
    event, poly = native_poly()
    p = {**poly_market(event, poly, reply(poly).source), "title": 'Track "revision" forecast'}
    store(tmp_path, "polymarket-us", p, poly)
    assert [r["instrument_id"] for r in venue_archive.export(tmp_path, text='"revision"')] == [p["instrument_id"]]
    with venue_archive.connect(tmp_path) as db, db:
        db.execute("UPDATE markets SET title='Retained forecast' WHERE instrument_id=?", (p["instrument_id"],))
    assert venue_archive.query(tmp_path, text='"revision"')["total"] == 0
    assert venue_archive.query(tmp_path, text="retained")['total'] == 1
    monkeypatch.setattr(venue_search, "SELECTIVE_LIMIT", 1)
    assert venue_archive.query(tmp_path, text="nyc")["total"] == 2
    with venue_archive.connect(tmp_path) as db, db:
        db.execute("DELETE FROM markets WHERE instrument_id=?", (p["instrument_id"],))
    assert venue_archive.query(tmp_path, text="retained")["total"] == 0


def test_payout_difference_distinguishes_matched_thresholds_from_other_buckets(tmp_path):
    native = native_kalshi(status="finalized", settlement_value_dollars="1.0000", result="yes")
    current = kalshi_market(SERIES, native, reply(native).source, FIRST)
    store(tmp_path, "kalshi", current, native)
    event, raw = native_poly()
    same = {**poly_market(event, raw, reply(raw).source), "yes_payout": "0"}
    store(tmp_path, "polymarket-us", same, raw)
    other = {**same, "instrument_id": "another-bucket", "lower": "51", "upper": "52"}
    store(tmp_path, "polymarket-us", other, raw)

    paired, adjacent = venue_archive.detail(tmp_path, "kalshi", current["instrument_id"])["related"]
    assert paired["comparison"]["observed_yes_payout_difference"] is True
    assert paired["comparison"]["payout_equivalent"] is False
    assert adjacent["comparison"]["observed_yes_payout_difference"] is False
    assert adjacent["comparison"]["different_verified_raw_yes_amounts"] is True


def test_compound_components_require_rule_date_and_exact_native_event(tmp_path):
    first = native_kalshi(status="finalized", settlement_value_dollars="1.0000", result="yes")
    other = native_kalshi("KXHIGHNY-26NOV14-T70", status="finalized", settlement_value_dollars="0.0000", result="no",
                          rules_primary=first["rules_primary"].replace("Nov 15", "Nov 14"))
    for raw in (first, other):
        store(tmp_path, "kalshi", kalshi_market(SERIES, raw, reply(raw).source, FIRST), raw)
    series = {**SERIES, "ticker": "KXCITIESWEATHER", "title": "High in several cities"}
    raw = native_kalshi("KXCITIESWEATHER-26NOV15-T1", rules_primary="If all stated conditions for Nov 15, 2026 occur, Yes pays.",
                        custom_strike={"Associated Markets": f"{first['ticker']}, {other['ticker']}",
                                       "Associated Events": f"{first['event_ticker']}, {other['event_ticker']}",
                                       "Associated Market Sides": "yes, no"})
    market = kalshi_market(series, raw, reply(raw).source, FIRST)
    store(tmp_path, "kalshi", market, raw)

    current, stale = venue_archive.detail(tmp_path, "kalshi", market["instrument_id"])["components"]
    assert current["identity_confirmed"] and current["market"]["yes_payout"] == "1"
    assert (stale["archived_component_day"], stale["side"]) == ("2026-11-14", "no")
    assert not stale["identity_confirmed"] and stale["market"] is None
    assert "different day" in stale["mismatch_reason"]


def test_weekend_rain_reporting_dates_stay_distinct_from_native_trade_close():
    series = {**SERIES, "ticker": "KXRAINWKND", "title": "Where will it rain this weekend?",
              "contract_terms_url": "https://assets.kalshi.com/contract_terms/RAINGLOBALPERIOD.pdf"}
    raw = native_kalshi("KXRAINWKND-26NOV14-SFO", strike_type="greater", floor_strike=0, cap_strike=None,
                        close_time="2026-11-16T08:00:00Z",
                        rules_primary="If the total precipitation at CLISFO in San Francisco on any day within November 14, 2026 "
                                      "through November 15, 2026 is strictly greater than 0 inches, then the market resolves to Yes.",
                        rules_secondary="“Trace” amounts (T) and missing daily precipitation values are counted as 0 inches.")
    row = kalshi_market(series, raw, reply(raw).source, FIRST)

    assert (row["family"], row["station"], row["day"]) == ("weekend-rain", "KSFO", "2026-11-14")
    assert row["day_basis"] == "Source reporting dates 2026-11-14 through 2026-11-15 inclusive"
    assert row["operator"] == "above" and row["resolution_source"] == "The Weather Company"
    assert row["closed_at"] == "2026-11-16T08:00:00Z"
    assert "climate_ends_at" not in row


def test_older_fixed_point_candles_support_replay_and_family_overlap(tmp_path):
    raw = native_kalshi(status="finalized", settlement_value_dollars="1.0000", result="yes")
    market = kalshi_market(SERIES, raw, reply(raw).source, FIRST)
    ticker = market["instrument_id"]
    store(tmp_path, "kalshi", market, raw)
    copied = reply({})
    stamp = datetime(2026, 11, 15, 17, 0, tzinfo=timezone.utc)
    with venue_archive.connect(tmp_path) as db, db:
        receipt = venue_archive.save_receipt(db, "kalshi", "candle-60m", copied)
        venue_archive.save_history(db, "kalshi", ticker, "candle-60m", [(0, "2026-11-15T17:00:00Z",
            {"yes_bid": {"close": "0.7800"}, "yes_ask": {"close": "0.7900"}})], receipt)
        venue_archive.save_book(db, "kalshi", ticker, receipt, {"at": FIRST, "depth_kind": "gap"})
        assert list(venue_replay._prices(db, market, stamp, stamp))[0]["yes"] == pytest.approx(0.785)
    assert venue_archive.kalshi_dollar_close({"close": 78}) is None
    summaries = venue_archive.coverage(tmp_path)["families"]
    assert len(summaries) == 1
    assert summaries[0]["cataloged"] == summaries[0]["payout_and_price_history"] == 1
    assert summaries[0]["depth_receipts"] == 0


def test_venue_weather_join_requires_the_contract_station_date(tmp_path):
    raw = native_kalshi()
    market = kalshi_market(SERIES, raw, reply(raw).source, FIRST)
    store(tmp_path, "kalshi", market, raw)
    previous = {"station": "KNYC", "date": "2026-11-14", "climate_starts_at": "2026-11-14T05:00:00Z",
                "climate_ends_at": "2026-11-15T05:00:00Z", "starts_at": "2026-11-14T05:00:00Z",
                "ends_at": "2026-11-15T05:00:00Z", "observations": [{"temp_f": 49}]}
    current = {**previous, "date": DAY, "climate_starts_at": "2026-11-15T05:00:00Z",
               "climate_ends_at": "2026-11-16T05:00:00Z", "observations": [{"temp_f": 52}]}
    ticker = market["instrument_id"]

    assert venue_replay.load(tmp_path, "kalshi", ticker, DAY, [previous], archive_prices=False) is None
    loaded = venue_replay.load(tmp_path, "kalshi", ticker, DAY, [previous, current], archive_prices=False)
    assert loaded["date"] == DAY
    assert loaded["observations"] == current["observations"]
    assert loaded["climate_starts_at"] == current["climate_starts_at"]


def test_audited_native_close_caps_only_its_own_replay_display(tmp_path):
    event, raw = native_poly()
    selected = poly_market(event, raw, reply(raw).source)
    other = {**selected, "instrument_id": "another-bucket", "lower": "51", "upper": "52"}
    for market in (selected, other):
        store(tmp_path, venue_archive.POLY, market, raw)

    cut = "2026-11-15T16:30:00.000000157Z"
    observed = {"book_state": "MARKET_STATE_CLOSED", "closeSetTime": cut, "lastTradeSetTime": "2026-11-15T16:27:10Z"}
    marks = [(0, "2026-11-15T16:29:00Z", {"longPrice": .5, "shortPrice": .5}),
             (1, "2026-11-15T16:30:00Z", {"longPrice": .51, "shortPrice": .49})]
    start = datetime(2026, 11, 15, 16, tzinfo=timezone.utc)
    end = datetime(2026, 11, 15, 18, tzinfo=timezone.utc)
    with venue_archive.connect(tmp_path) as db, db:
        receipt = venue_archive.save_receipt(db, venue_archive.POLY, "closed-book-clock", reply(observed, LATER))
        venue_archive.save_history(db, venue_archive.POLY, selected["instrument_id"], "closed-book-clock",
                                   [(cut, cut, observed)], receipt)
        receipt = venue_archive.save_receipt(db, venue_archive.POLY, "display-price-1m", reply(marks, LATER))
        for row in (selected, other):
            venue_archive.save_history(db, venue_archive.POLY, row["instrument_id"], "display-price-1m", marks, receipt)
        replayed = {}
        for row in db.execute("SELECT * FROM markets"):
            replayed[row["instrument_id"]] = venue_replay._bucket(db, row, start, end)
    audited, sibling = replayed[selected["instrument_id"]], replayed[other["instrument_id"]]
    assert audited["native_trading_closed_at"] == cut
    assert [row["at"] for row in audited["prices"]] == ["2026-11-15T16:29:00Z"]
    assert "native_trading_closed_at" not in sibling
    assert [row["at"] for row in sibling["prices"]] == [mark[1] for mark in marks]


def test_hourly_history_divides_over_limit_groups_and_long_instruments(tmp_path, monkeypatch):
    monkeypatch.setattr(venue_history, "MAX_KALSHI_CANDLES", 6)
    start = "2026-11-15T06:00:00Z"
    calls = []

    def get(venue, path, params):
        assert venue == venue_archive.KALSHI and path == "/markets/candlesticks"
        tickers = params["market_tickers"].split(",")
        left, right = params["start_ts"], params["end_ts"]
        assert len(tickers) * venue_history._candle_limit(left, right) <= 6
        calls.append((tickers, left, right))
        return reply({"markets": [{"market_ticker": ticker, "candlesticks": [{"end_period_ts": right}]}
                                  for ticker in tickers]})

    same = [{"instrument_id": value, "opened_at": start, "closed_at": "2026-11-15T10:00:00Z"} for value in ("ONE", "TWO")]
    assert venue_history._live_candles(tmp_path, get, same) == 2
    long = {"instrument_id": "LONG", "opened_at": start, "closed_at": "2026-11-15T14:00:00Z"}
    assert venue_history._live_candles(tmp_path, get, [long]) == 2
    assert len(calls) == 4 and calls[-2][2] + 1 == calls[-1][1]
    with venue_archive.connect(tmp_path) as db:
        assert db.execute("SELECT state,pages,rows FROM progress WHERE scope='LONG' AND kind='candle-60m'").fetchone()[:] == (
            "complete", 2, 2,
        )


def test_kalshi_settlement_keeps_first_observed_receipt(tmp_path):
    native = native_kalshi(status="finalized", settlement_value_dollars="0.4200", result="scalar",
                           settlement_ts="2026-11-15T16:00:00Z")
    ticker = native["ticker"]
    row = kalshi_market(SERIES, native, reply(native).source, FIRST)
    first_id = store(tmp_path, "kalshi", row, native)
    store(tmp_path, "kalshi", kalshi_market(SERIES, native, reply(native).source, LATER), native, LATER)
    store(tmp_path, "kalshi", kalshi_market(SERIES, native_kalshi(), reply(native).source, FIRST), native_kalshi())
    detail = venue_archive.detail(tmp_path, "kalshi", ticker)
    assert detail["settlement_receipt"]["id"] == first_id
    assert detail["settlement_received_at"] == FIRST
    assert detail["settlement_at"] == "2026-11-15T16:00:00Z"
    assert (detail["yes_payout"], detail["no_payout"]) == ("0.42", "0.58")


def test_polymarket_payout_needs_explicit_verified_endpoint(tmp_path):
    event, market = native_poly()
    ticker = market["slug"]
    market["outcomePrices"] = '["1","0"]'
    store(tmp_path, "polymarket-us", poly_market(event, market, reply(market).source), market)
    detail = venue_archive.detail(tmp_path, "polymarket-us", ticker)
    assert detail["settlement_status"] == "pending payout"
    assert detail["yes_payout"] is None
    venue_sources._poly_payout(tmp_path, lambda *args: reply({"slug": ticker, "settlement": 1}, LATER), ticker)
    settled = venue_archive.detail(tmp_path, "polymarket-us", ticker)
    assert settled["settlement_status"] == "verified payout"
    assert settled["settlement_at"] is None
    assert settled["settlement_received_at"] == LATER
    assert (settled["yes_payout"], settled["no_payout"]) == ("1", "0")


def test_polymarket_closed_book_clock_leaves_listing_close_and_cash_separate(tmp_path):
    event, raw = native_poly()
    event["endDate"] = "2026-11-15T16:00:00Z"
    raw["endDate"] = "2026-11-15T19:00:00Z"
    ticker = raw["slug"]
    parsed = poly_market(event, raw, reply(raw).source)
    original = reply(event)
    with venue_archive.connect(tmp_path) as db, db:
        receipt = venue_archive.save_receipt(db, venue_archive.POLY, "catalog", original)
        venue_archive.save_event(db, venue_archive.POLY, event["slug"], event["seriesSlug"], event["title"], event, receipt)
        venue_archive.save_markets(db, [parsed], [raw], original, receipt)
    assert venue_archive.detail(tmp_path, venue_archive.POLY, ticker)["clocks"]["native_closed_book_at"] is None
    with venue_archive.connect(tmp_path) as db:
        assert venue_archive.native_closed_book_at(db, ticker) is None

    observed = {"book_state": "MARKET_STATE_CLOSED", "closeSetTime": "2026-11-15T16:30:00.000000157Z",
                "lastTradeSetTime": "2026-11-15T16:25:00.000001000Z"}
    with venue_archive.connect(tmp_path) as db, db:
        receipt = venue_archive.save_receipt(db, venue_archive.POLY, "closed-book-clock", reply(observed, LATER))
        venue_archive.save_history(db, venue_archive.POLY, ticker, "closed-book-clock", [(observed["closeSetTime"],
                                  "2026-11-15T16:30:00Z", observed)], receipt)
        assert venue_archive.native_closed_book_at(db, ticker) == observed["closeSetTime"]
    detail = venue_archive.detail(tmp_path, venue_archive.POLY, ticker)
    clocks = detail["clocks"]
    assert clocks["native_closed_book_at"] == observed["closeSetTime"]
    assert clocks["native_last_trade_at"] == observed["lastTradeSetTime"]
    assert clocks["native_closed_book_receipt"]["id"] == receipt
    assert clocks["nominal_event_end_at"] == event["endDate"]
    assert clocks["listed_market_end_at"] == detail["closed_at"] == raw["endDate"]
    assert detail["settlement_received_at"] is None and detail["yes_payout"] is None

    conflicting = {**observed, "closeSetTime": "2026-11-15T16:31:00Z"}
    with venue_archive.connect(tmp_path) as db, db:
        proof = venue_archive.save_receipt(db, venue_archive.POLY, "closed-book-clock", reply(conflicting, LATER))
        venue_archive.save_history(db, venue_archive.POLY, ticker, "closed-book-clock",
                                   [(conflicting["closeSetTime"], conflicting["closeSetTime"], conflicting)], proof)
        assert venue_archive.native_closed_book_at(db, ticker) is None
    audited = venue_archive.detail(tmp_path, venue_archive.POLY, ticker)["clocks"]
    assert audited["native_close_conflict"] and len(audited["native_closed_book_observations"]) == 2


def test_historical_pagination_resumes_with_correct_filter_and_legacy_ids(tmp_path):
    ids = ("HIGHNY-26NOV15-T49", "KXHIGHNY-26NOV15-B49.5")
    calls = []

    def get(venue, path, params):
        assert venue == "kalshi" and path == "/historical/markets"
        assert "mve_filter" not in params
        calls.append(params)
        page = len(calls) - 1
        return reply({"cursor": "page-2" if page == 0 else "", "markets": [native_kalshi(ids[page])]})

    venue_sources._kalshi_scan(tmp_path, get, SERIES, kind="catalog-archive", page_cap=1)
    assert venue_archive.coverage(tmp_path)["pending"][0]["next_page"] == "page-2"
    assert venue_sources._kalshi_scan(tmp_path, get, SERIES, kind="catalog-archive") == 2
    assert calls[1]["cursor"] == "page-2"
    assert venue_archive.coverage(tmp_path)["pending"] == []
    assert venue_archive.query(tmp_path, venue="kalshi")["total"] == 2


def test_event_inventory_recovers_prior_catalog_alias_under_exact_native_series(tmp_path, monkeypatch):
    alias = "HIGHNY--26NOV15-B49.5"
    event_id = "HIGHNY-26NOV15"
    market = native_kalshi(alias, event_ticker=event_id)
    event = {"event_ticker": event_id, "series_ticker": SERIES["ticker"], "title": "Historical native weather event"}
    with venue_archive.connect(tmp_path) as db, db:
        source = venue_archive.save_receipt(db, "kalshi", "series", reply(SERIES))
        venue_archive.save_series(db, "kalshi", SERIES["ticker"], "daily-high", SERIES["title"], SERIES, source)

    def get(venue, path, params):
        assert venue == "kalshi" and params["series_ticker"] == SERIES["ticker"]
        if path == "/historical/markets":
            return reply({"markets": [market], "cursor": ""})
        assert path == "/events"
        return reply({"events": [event], "cursor": ""}, LATER)

    assert venue_sources._kalshi_scan(tmp_path, get, SERIES, kind="catalog-archive") == 0
    assert venue_archive.detail(tmp_path, "kalshi", alias) is None
    monkeypatch.setattr(venue_history, "PublicGET", lambda data: get)
    venue_history.event_inventory(tmp_path, [SERIES["ticker"]])
    item = venue_archive.detail(tmp_path, "kalshi", alias)
    assert item["event_id"] == event_id and item["instrument_id"] == alias
    assert item["event"]["metadata"] == event


def test_fixed_point_book_preserves_missing_side_and_absent_source_time(tmp_path):
    copied = reply({}, headers={"date": "Sun, 15 Nov 2026 17:00:00 GMT", "age": "2", "via": "test"})
    row = {"ticker": "KXHIGHNY-26NOV15-B49.5", "orderbook_fp": {"no_dollars": [["0.0732", "30.25"], ["0.50", "0.06"]]}}
    book = venue_sources.parse_kalshi_book(row, copied)
    assert book["yes_bids"] is None
    assert book["no_offers"] is None
    assert book["yes_offers"] == [["0.50", "0.06"], ["0.9268", "30.25"]]
    assert book["source_at"] is None
    with venue_archive.connect(tmp_path) as db, db:
        receipt_id = venue_archive.save_receipt(db, "kalshi", "book", copied)
        venue_archive.save_book(db, "kalshi", row["ticker"], receipt_id, book)
    start = datetime(2026, 11, 15, 17, 0, tzinfo=timezone.utc)
    found = venue_archive.books(tmp_path, "kalshi", row["ticker"], start=start)
    assert len(found) == 1
    assert found[0]["http_headers"]["via"] == "test"
    assert found[0]["cache_age_seconds"] == 2


def test_stop_prevents_public_request(tmp_path):
    (tmp_path / "collector").mkdir()
    (tmp_path / "collector" / "paused").touch()
    transport = venue_sources.PublicGET(tmp_path)
    with pytest.raises(venue_sources.CollectionPaused):
        transport("kalshi", "/markets")
