from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
from threading import Barrier
from uuid import uuid4

from test_historical_http import witness_fixture_listing
from weather_edge import paper_runs, trade, trade_orders
from weather_edge.capture import write_capture
from weather_edge.capture_fixture import fixture_capture
from weather_edge.market import iso, parse_time
from weather_edge.strategy_rules import book_status


YES_BUCKET = "tc-temp-nychigh-2026-11-15-gte51lt52f"
NO_BUCKET = "tc-temp-nychigh-2026-11-15-gte49lt50f"


def tape():
    capture = fixture_capture(duration_seconds=30, poll_seconds=8, max_rounds=8, max_events=12)
    for event in capture["events"]:
        for bucket in event["buckets"]:
            for book in bucket["books"]:
                book["available_at"] = book["at"]
    return capture, parse_time(capture["started_at"])


def probabilities(event, when):
    return {
        YES_BUCKET: {"probability": 0.25, "uncertain": False, "model_version": "synthetic"},
        NO_BUCKET: {"probability": 0.01, "uncertain": False, "model_version": "synthetic"},
    }


def test_ready_uses_freshness_and_confidence_before_ev(tmp_path):
    capture, now = tape()
    witness_fixture_listing(tmp_path, now)
    event = capture["events"][0]
    bucket = next(b for b in event["buckets"] if b["id"] == YES_BUCKET)
    book = bucket["books"][0]
    selected = (trade.POLY, YES_BUCKET, "YES")

    def preview(at, lookup=probabilities):
        rows, _ = trade.market_rows(tmp_path, capture, lookup, at, selected=selected)
        return rows[0]

    assert preview(now)["category"] == "freshness"
    book["http_cache"] = {"cf-cache-status": "MISS"}
    ready = preview(now)
    assert ready["blocker"] is None
    assert ready["order"]["partial"] and ready["order"]["quantity"] == 32
    assert ready["order"]["max_loss"] == 6.21
    assert ready["order"]["highest_price"] == 0.18
    assert ready["exchange_url"] == "https://polymarket.us/event/temp-nychigh-2026-11-15"

    def uncertain(*_):
        return {YES_BUCKET: {"probability": 0.9, "uncertain": True}}

    watch = preview(now, uncertain)
    assert watch["order"]["ev_dollars"] > 0
    assert watch["blocker"] == "Uncertain model probability"
    assert "review_key" not in watch
    bucket["books"] = [book]
    stale = preview(now + timedelta(seconds=31))
    assert stale["blocker"] == "Book is stale" and stale["order"] is None
    book["source_at"] = iso(now - timedelta(seconds=trade.EXCHANGE_AGE_SECONDS + 1))
    stale_image = preview(now)
    assert stale_image["blocker"] == "Exchange image is stale" and stale_image["order"] is None
    assert stale_image["book_age"] == 0
    board = trade.opportunity_board([stale_image], now, False)
    assert board["ready"] == []
    assert board["prices"][0]["posted"] == {
        "price": 0.18, "quantity": 32.0, "kind": "Book offer", "at": book["at"],
    }
    assert board["coverage"][trade.POLY]["book_sides"] == 1


def test_posted_offer_clocks_are_independent_of_the_paper_book(tmp_path, monkeypatch):
    capture, now = tape()
    witness_fixture_listing(tmp_path, now)
    event = capture["events"][0]
    bucket = next(b for b in event["buckets"] if b["id"] == YES_BUCKET)
    book = bucket["books"][0]
    book["http_cache"] = {"cf-cache-status": "MISS"}
    received = now + timedelta(seconds=3)
    newer = {**book, "at": iso(received), "source_at": iso(now - timedelta(hours=20)),
             "cache_age_seconds": 27, "offers": [["0.99", "50"]]}
    market = {"event_id": event["id"], "raw_market": "{}", "last_book": json.dumps(newer)}
    monkeypatch.setattr(trade, "_catalog", lambda *_: {(trade.POLY, YES_BUCKET): market})
    rows, _ = trade.market_rows(tmp_path, capture, probabilities, received, selected=(trade.POLY, YES_BUCKET, "YES"))
    row = rows[0]
    assert row["posted"]["price"] == 0.99 and row["posted_receipt_age"] == 0
    assert row["posted_exchange_age"] == 20 * 3600 + 3
    assert row["receipt_age"] == 3 and row["exchange_age"] == 3
    assert row["blocker"] == "Newer public book awaits collector confirmation"
    assert not row["order"] and "review_key" not in row


def test_identical_cache_receipt_keeps_only_the_unexpired_origin_witness(tmp_path):
    capture, start = tape()
    witness_fixture_listing(tmp_path, start)
    event = capture["events"][0]
    bucket = next(row for row in event["buckets"] if row["id"] == YES_BUCKET)
    origin = {**bucket["books"][0], "http_cache": {"cf-cache-status": "EXPIRED"}}
    cached = {**origin, "at": iso(start + timedelta(seconds=16)), "available_at": iso(start + timedelta(seconds=16)),
              "cache_age_seconds": 16,
              "http_cache": {"cf-cache-status": "HIT"}, "source_time_progress": "unchanged"}
    bucket["books"] = [origin, cached]
    clk = start + timedelta(seconds=20)
    target = (trade.POLY, YES_BUCKET, "YES")
    before, _ = trade.market_rows(tmp_path, capture, probabilities, clk, selected=target, retain_origin=False)
    after, witness = trade.market_rows(tmp_path, capture, probabilities, clk, selected=target)
    assert before[0]["blocker"] == "Cached response with recorded Age"
    assert after[0]["retained_origin"] and witness[2] is origin
    assert after[0]["quote_at"] == origin["at"] and after[0]["observed_receipt_at"] == cached["at"]
    assert after[0]["valid_for"] == 10

    expired, _ = trade.market_rows(tmp_path, capture, probabilities, start + timedelta(seconds=31), selected=target)
    assert expired[0]["blocker"] and not expired[0]["retained_origin"]
    changed = {**cached, "at": iso(start + timedelta(seconds=10)), "offers": [["0.19", "32"]]}
    assert trade.origin_witness([origin, changed, cached], clk, cached) is cached
    assert trade.origin_witness([origin, cached], clk, cached, changed) is cached
    assert book_status(cached, "YES", parse_time(cached["at"]), trade.FRESHNESS)


def test_trade_keeps_listed_event_close_after_recent_capture_rolls_out(tmp_path, monkeypatch):
    capture, now = tape()
    market = {"venue": trade.POLY, "instrument_id": YES_BUCKET, "event_id": capture["events"][0]["id"],
              "raw_market": "{}", "label": "51 to 52", "status": "open",
              "closed_at": iso(now + timedelta(hours=3)), "listed_event_end_at": iso(now)}
    monkeypatch.setattr(trade, "_catalog", lambda *_: {(trade.POLY, YES_BUCKET): market})
    rows, _ = trade.market_rows(tmp_path, None, probabilities, now)
    assert all(not row["market_open"] and row["blocker"] == "Listed event window ended" for row in rows)
    assert all(row["closes_at"] == iso(now) for row in rows)
    assert trade.funnel_snapshot(rows)["venues"][trade.POLY]["listed_window_ended_sides"] == 2


def test_practice_starts_with_the_same_fresh_balance_every_time():
    first, second = trade_orders.practice(), trade_orders.practice()
    assert first == second
    assert first["position"]["bucket_id"] == NO_BUCKET  # The synthetic practice selects YES on this bucket.
    assert first["position"]["side"] == "YES"
    assert first["position"]["max_loss"] == 25
    assert first["starting_balance"] == 500 and first["available_balance"] == 475


def test_selected_fixture_uses_later_receipt_price_limit_and_exact_side(tmp_path):
    capture, now = tape()
    request_id = str(uuid4())
    rows, witness = trade.market_rows(tmp_path, capture, probabilities, now, fixture=True,
                                      selected=(trade.POLY, YES_BUCKET, "YES"))
    first = trade_orders.record(tmp_path, True, request_id, rows[0], witness)
    assert first["status"] == "Pending recheck"
    through = parse_time(capture["captured_at"])
    trade_orders.reconcile(tmp_path, through, lambda: capture, lambda _: probabilities)
    result = trade_orders.orders(tmp_path, True)
    skipped = result["orders"][0]
    assert skipped["status"] == "Skipped"
    assert skipped["result"]["reason"] == "Price moved above the reviewed limit"
    assert result["cash"] == 500 and result["committed"] == 0

    rows, witness = trade.market_rows(tmp_path, capture, probabilities, now, fixture=True, budget=12,
                                      selected=(trade.POLY, NO_BUCKET, "NO"))
    request_id = str(uuid4())
    callers = Barrier(2)

    def submit():
        callers.wait()
        return trade_orders.record(tmp_path, True, request_id, rows[0], witness)

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert set(pool.map(lambda _: submit()["id"], range(2))) == {request_id}
    trade_orders.reconcile(tmp_path, through, lambda: capture, lambda _: probabilities)
    result = trade_orders.orders(tmp_path, True)
    filled = next(item for item in result["orders"] if item["id"] == request_id)
    position = filled["result"]["position"]
    audit = paper_runs.load_run(tmp_path / "trade-paper", filled["result"]["run_id"])
    assert filled["status"] == "Simulated" and len(result["orders"]) == 2
    assert position["side"] == "NO" and position["bucket_id"] == NO_BUCKET and position["entry_price"] == 0.95
    assert parse_time(position["opened_at"]) > parse_time(filled["created_at"])
    assert result["cash"] == 500 - position["max_loss"]
    assert result["committed"] == position["max_loss"]
    reproduced = paper_runs.verify_run(audit)
    assert len(reproduced["attempts"]) == reproduced["fills"] == 1
    assert trade_orders.orders(tmp_path, False)["orders"] == []


def test_kalshi_sizing_uses_native_side_and_venue_fee_rounding(tmp_path, monkeypatch):
    _, now = tape()
    market = {
        "instrument_id": "KXHIGHNY-26NOV15-B49.5",
        "raw_market": '{"market_type":"binary","notional_value_dollars":"1.0000"}',
    }
    book = {
        "at": now.isoformat(), "http_status": 200, "depth_kind": "displayed_ladder",
        "yes_offers": [["0.2", "5"], ["0.3", "12"]],
        "yes_bids": [["0.1", "2"]],
        "no_offers": [["0.9", "2"]], "no_bids": [["0.8", "5"]],
    }
    monkeypatch.setattr(trade.venue_fees, "kalshi_terms",
                        lambda *args: {"available": True, "taker_rate": "0.07", "multiplier": "1"})
    yes, blocker = trade._kalshi_depth(tmp_path, market, book, "YES", now, 2)
    no, _ = trade._kalshi_depth(tmp_path, market, book, "NO", now, 2)
    assert not blocker and yes["fills"] == [[0.2, 5], [0.3, 2]]
    assert yes["max_loss"] == 1.69 and yes["fee"] == 0.09
    assert no["fills"] == [[0.9, 2]] and no["max_loss"] == 1.82
    seen = trade._posted_price(trade.KALSHI, "NO", book, {"NO": "0.13"}, {}, now, False)
    assert seen["price"] == 0.9 and seen["quantity"] == 2
    archived = trade._posted_price(trade.KALSHI, "NO", None, {"NO": "0.13"},
                                  {"last_received_at": now.isoformat()}, now, True)
    assert archived is None


def test_legacy_recheck_recognizes_prior_digest_prefix_and_honors_full_digest():
    _, at = tape()
    later = at + timedelta(seconds=30)
    digest = "fd2b26762f" + "8" * 54
    signal = {"model_version": "knyc-ridge-fd2b26762f", "model_method": "ridge", "probability": 0.25}
    latest = {**signal, "model_vintage": digest, "probability": 0.22}

    def recheck(original, current):
        lookup = trade_orders.reviewed_probability(YES_BUCKET, at, original, lambda *_: {YES_BUCKET: current})
        assert lookup({}, at) == {YES_BUCKET: original}
        return lookup({}, later)

    assert recheck(signal, latest) == {YES_BUCKET: latest}
    assert recheck(signal, {**latest, "model_version": "knyc-ridge-aaaaaaaaaa"}) == {}
    assert recheck(signal, {**latest, "model_vintage": "a" * 64}) == {}
    assert recheck(signal, {**latest, "model_vintage": digest[:10]}) == {}
    assert recheck(signal, {**latest, "model_method": "noaa"}) == {}
    assert recheck({**signal, "model_vintage": digest}, latest) == {YES_BUCKET: latest}
    assert recheck({**signal, "model_vintage": digest[:10] + "a" * 54}, latest) == {}
    assert recheck({**signal, "model_vintage": digest}, signal) == {}


def test_live_recheck_stays_inside_reserved_cost_when_depth_moves(tmp_path):
    capture, now = tape()
    capture["kind"] = "live"
    witness_fixture_listing(tmp_path, now)
    bucket = next(b for b in capture["events"][0]["buckets"] if b["id"] == NO_BUCKET)
    first = bucket["books"][0]
    first["http_cache"] = {"cf-cache-status": "MISS"}
    first["source_at"] = iso(now - timedelta(seconds=110))

    def lookup(*_):
        return {NO_BUCKET: {"probability": 0.55, "uncertain": False, "model_version": "synthetic"}}

    rows, witness = trade.market_rows(tmp_path, capture, lookup, now, budget=25,
                                      selected=(trade.POLY, NO_BUCKET, "YES"))
    ticket = rows[0]
    order = ticket["order"]
    assert order["highest_price"] == 0.2 and order["cost_at_limit"] > order["max_loss"]
    stored = trade_orders.record(tmp_path, False, str(uuid4()), ticket, witness)
    due = parse_time(stored["reaction_due_at"])
    trade_orders.reconcile(tmp_path, due + timedelta(seconds=1), lambda: None, lambda _: lookup)
    check = trade_orders.orders(tmp_path, False)
    assert check["orders"][0]["status"] == "Pending recheck"
    assert check["committed"] == order["max_loss"]

    recheck_at = iso(due + timedelta(seconds=2))
    bucket["books"] = [
        first, {**first, "at": iso(due + timedelta(seconds=1))},
        {**first, "at": recheck_at, "source_at": recheck_at, "offers": [["0.20", "150"]]},
    ]
    capture["captured_at"] = recheck_at
    write_capture(capture, tmp_path / "captures")
    trade_orders.reconcile(tmp_path, parse_time(recheck_at), lambda: None, lambda _: lookup)
    checked = trade_orders.orders(tmp_path, False)
    assert checked["orders"][0]["status"] == "Simulated"
    position = checked["orders"][0]["result"]["position"]
    assert position["max_loss"] <= order["max_loss"] and position["quantity"] < order["quantity"]
    assert position["entry_price"] == order["highest_price"] and position["opened_at"] == recheck_at
    assert checked["orders"][0]["result"]["attempt"]["recheck_gaps"] == {"Exchange image is stale": 1}
