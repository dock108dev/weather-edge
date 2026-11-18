from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
import json

from test_historical_http import app
from test_venue_archive import DAY, SERIES, native_kalshi, native_poly
from weather_edge import collector, listing_evidence, market, paper_runs, server, strategy_native_capture
from weather_edge import venue_archive as archive, venue_sources
from weather_edge.poly_fee_policy import FEE_CHANGE, frozen_schedule
from weather_edge.venue_contracts import KALSHI_API, POLY_API, kalshi_market
from weather_edge.venue_fees import kalshi_terms


T0 = market.parse_time("2026-11-15T19:00:00Z")
TICK = timedelta(microseconds=1)


def save(db, venue, kind, url, at, body):
    clock = market.iso(at)
    reply = venue_sources.Receipt(url, clock, clock, 200, {}, json.dumps(body).encode())
    return reply, archive.save_receipt(db, venue, kind, reply)


def poly_fixture(tmp_path):
    event, exact = native_poly()
    event.update(endDate="2026-11-16T04:59:00Z")
    exact.update(status="MARKET_STATUS_OPEN", startDate="2026-11-14T15:00:00Z",
                 endDate="2026-11-16T10:00:00Z", minimumTradeQty="0.01", feeCoefficient=.095)
    for i, suffix, rule in ((1, "lt49f", "less than or equal to 48F"),
                            (2, "gte51f", "greater than or equal to 51F")):
        other = deepcopy(exact)
        other.update(id=2 + 10*i, slug=f"tc-{event['slug']}-{suffix}",
                     description=exact["description"].replace("between 49F and 50F", rule))
        for j, side in enumerate(other["marketSides"]):
            side.update(marketId=other["id"], id=(10*i + j + 3))
        event["markets"].append(other)
    url = POLY_API + "/v1/events/slug/" + event["slug"]

    def listing(at, body):
        with archive.connect(tmp_path) as db, db:
            receipt, ident = save(db, archive.POLY, "event-detail", url, at, {"event": body})
            venue_sources._apply_poly(db, receipt, ident, [body])
            return ident

    first = listing(T0, event)
    with archive.connect(tmp_path) as db, db:
        book_at = T0 + timedelta(seconds=3, microseconds=9)
        _, book_id = save(db, archive.POLY, "book", POLY_API + "/v1/markets/" + exact["slug"] + "/book", book_at, {})
        archive.save_book(db, archive.POLY, exact["slug"], book_id, {
            "at": market.iso(book_at), "source_at": market.iso(book_at),
            "depth_kind": "displayed_ladder", "bids": [["0.84", "9"]], "offers": [["0.86", "2"]],
        })
    return event, exact["slug"], listing, first, book_id


def test_board_listing_reuses_bodies_but_reads_new_receipts_at_each_cutoff(tmp_path, monkeypatch):
    event, instrument, listing, first, _ = poly_fixture(tmp_path)
    later_at = T0 + timedelta(seconds=4)
    revised = deepcopy(event)
    revised["endDate"] = "2026-11-16T00:00:00Z"
    tape, issues = strategy_native_capture.polymarket_events(
        tmp_path, later_at, family="daily-high", versioned=False,
    )
    assert not issues and tape

    def lookup(cutoff):
        joined = strategy_native_capture.with_listing_evidence(
            tmp_path, {"events": tape}, cutoff, reuse_bodies=True,
        )
        bucket = next(bucket for bucket in joined["events"][0]["buckets"] if bucket["id"] == instrument)
        return strategy_native_capture.listing_at(bucket, cutoff)

    cache = listing_evidence._board_poly_matches
    cache.cache_clear()
    decompress = listing_evidence.zlib.decompress
    opened = []

    def track(body):
        opened.append(body)
        return decompress(body)

    monkeypatch.setattr(listing_evidence.zlib, "decompress", track)
    assert lookup(T0)["receipt_id"] == first
    assert lookup(T0) == lookup(T0)
    assert len(opened) == 1
    later = listing(later_at, revised)
    assert lookup(later_at - TICK)["receipt_id"] == first
    assert lookup(later_at)["receipt_id"] == later
    assert len(opened) == 2

    # An unreadable body may be skipped for this response. Retry the same
    # bytes on the next read instead of caching a missing listing proof.
    cache.cache_clear()
    def intermittent(body):
        if body == opened[-1]:
            raise listing_evidence.zlib.error("Incomplete read")
        return decompress(body)

    with monkeypatch.context() as patch:
        patch.setattr(listing_evidence.zlib, "decompress", intermittent)
        assert lookup(later_at)["receipt_id"] == first
    assert lookup(later_at)["receipt_id"] == later
    cache.cache_clear()


def test_exact_ticket_and_multi_origin_capture_choose_the_same_listing(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DATA", tmp_path)
    event, instrument, listing, first, book_id = poly_fixture(tmp_path)
    revision_at = T0 + timedelta(seconds=9)
    revised = deepcopy(event)
    revised["endDate"] = "2026-11-16T00:00:00Z"
    revised["markets"][0]["feeCoefficient"] = .125

    def lookup(get, at, side="YES"):
        return get("/api/trade", venue=archive.POLY, instrument=instrument, side=side,
                   at=market.iso(at), day=DAY, archived=1, budget=25)

    with app() as get:
        assert lookup(get, T0 - TICK)["listing_evidence"]["status"] == "missing"
        assert lookup(get, T0)["listing_evidence"]["receipt_id"] == first
        assert lookup(get, T0 + TICK)["fee_evidence"]["status"] == "verified"
        earlier = lookup(get, revision_at - TICK)
        later = listing(revision_at, revised)
        assert lookup(get, revision_at - TICK) == earlier
        for at in (revision_at, revision_at + TICK):
            row = lookup(get, at)
            assert row["closes_at"] == revised["endDate"]
            assert row["listing_evidence"]["receipt_id"] == later
            assert row["listing_evidence"]["closed_at"] == "2026-11-16T10:00:00Z"
            assert row["listing_evidence"]["opened_at"] == "2026-11-14T15:00:00Z"
            assert row["fee_evidence"]["status"] == "contradictory"
            assert row["posted"]["price"] == .86 and row["book_receipt_id"] == book_id
        no = lookup(get, revision_at, "NO")
        assert earlier["side_id"] == "3" and no["side_id"] == "4" and no["posted"]["price"] == .16

        events, issues = strategy_native_capture.polymarket_events(tmp_path, revision_at + TICK, family="daily-high")
        assert not issues and len(events) == 1
        capture = events[0]
        bucket = next(b for b in capture["buckets"] if b["id"] == instrument)
        assert capture["native_event_ends_at"] == event["endDate"]
        before = strategy_native_capture.terms_at(bucket, revision_at - TICK)
        after = strategy_native_capture.terms_at(bucket, revision_at)
        assert before["listing"]["receipt_id"] == first and before["fees"]["available"]
        assert after["listing"]["receipt_id"] == later and not after["fees"]["available"]
        frozen = paper_runs._freeze({"events": [capture]}, revision_at - TICK)
        own = next(b for b in frozen["events"][0]["buckets"] if b["id"] == instrument)
        assert [r["receipt_id"] for r in own["listing_revisions"]] == [first]

        missing = deepcopy(revised)
        del missing["markets"][0]["feeCoefficient"]
        listing(revision_at + 2*TICK, missing)
        row = lookup(get, revision_at + 2*TICK)
        assert row["fee_evidence"]["status"] == "missing"
        assert "does not advertise" in row["fee_evidence"]["reason"]
        assert row["posted"]["price"] == .86

        # A repeat of the original revision at the same instant disagrees; a
        # fresh receipt later resolves that instant's conflict.
        conflict_id = listing(revision_at + 2*TICK, revised)
        row = lookup(get, revision_at + 2*TICK)
        assert row["listing_evidence"]["status"] == "contradictory"
        assert conflict_id in row["listing_evidence"]["conflicting_receipts"]
        again = listing(revision_at + 3*TICK, revised)
        assert lookup(get, revision_at + 3*TICK)["listing_evidence"]["receipt_id"] == again

        payout_at = revision_at + timedelta(seconds=12)
        with archive.connect(tmp_path) as db, db:
            _, payout_id = save(db, archive.POLY, "settlement", POLY_API + "/v1/markets/" + instrument + "/settlement",
                                payout_at, {"slug": instrument, "settlement": "1"})
        assert lookup(get, payout_at - TICK)["verified_payout"] is None
        assert lookup(get, payout_at)["verified_payout"]["receipt_id"] == payout_id
        assert lookup(get, payout_at, "NO")["verified_payout"]["side_payout"] == "0"


def test_kalshi_event_fee_requires_complete_local_chain_and_effective_clock(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DATA", tmp_path)
    raw = native_kalshi(fractional_trading_enabled=False)
    ticker, event_id = raw["ticker"], raw["event_ticker"]
    fee_at = T0 + timedelta(seconds=3)
    effective = T0 + timedelta(seconds=5)
    late = T0 + timedelta(seconds=7)
    listing_source = KALSHI_API + "/markets?series_ticker=KXHIGHNY"
    fee_source = KALSHI_API + "/events/fee_changes?limit=1000"
    with archive.connect(tmp_path) as db, db:
        _, series_id = save(db, archive.KALSHI, "series", KALSHI_API + "/series?category=Climate+and+Weather",
                            T0, {"series": [SERIES]})
        reply, listing_id = save(db, archive.KALSHI, "catalog-recent", listing_source, T0,
                                 {"markets": [raw], "cursor": ""})
        normalized = kalshi_market(SERIES, raw, reply.source, reply.received_at)
        archive.save_markets(db, [normalized], [raw], reply, listing_id)
        _, _ = save(db, archive.KALSHI, "fee-series", KALSHI_API + "/series/fee_changes?show_historical=true",
                    T0, {"series_fee_change_arr": []})
        _, head = save(db, archive.KALSHI, "fee-event", fee_source, fee_at - TICK,
                       {"cursor": "next", "event_fee_changes": []})
        change = {"event_ticker": event_id, "scheduled_ts": market.iso(effective), "fee_multiplier_override": 2}
        _, tail = save(db, archive.KALSHI, "fee-event", fee_source + "&cursor=next", fee_at,
                       {"cursor": "", "event_fee_changes": [change]})
        _, late_doc = save(db, archive.KALSHI, "fee-event", fee_source, late,
                           {"cursor": "", "event_fee_changes": [{**change, "fee_multiplier_override": 3}]})
        image_at = T0 + timedelta(microseconds=5)
        book_reply, book_id = save(db, archive.KALSHI, "book", KALSHI_API + "/markets/orderbooks", image_at, {})
        book = venue_sources.parse_kalshi_book({"ticker": ticker, "orderbook_fp": {
            "yes_dollars": [["0.10", "2.8"]], "no_dollars": [["0.80", "5.9"], ["0.70", "12"]],
        }}, book_reply)
        archive.save_book(db, archive.KALSHI, ticker, book_id, book)

    with app() as get:
        def ticket(at, side="YES"):
            return get("/api/trade", venue=archive.KALSHI, day=DAY, archived=1,
                       instrument=ticker, side=side, budget=2, at=market.iso(at))

        insufficient = ticket(fee_at - TICK)
        assert insufficient["order"] is None and insufficient["posted"]["price"] == .2
        assert "Complete Kalshi event fee receipt set" in insufficient["blocker"]
        at_receipt = ticket(fee_at)
        assert at_receipt["fee_evidence"]["series_receipt"]["receipt_id"] == series_id
        assert [page["receipt_id"] for page in at_receipt["fee_evidence"]["event_change_receipts"]] == [head, tail]
        assert at_receipt["fee_evidence"]["multiplier"] == ticket(effective - TICK)["fee_evidence"]["multiplier"] == "1"
        assert at_receipt["order"]["quantity"] == 7 and at_receipt["order"]["fee"] == .09
        assert at_receipt["order"]["max_loss"] == 1.69
        assert at_receipt["book_receipt_id"] == book_id and at_receipt["quote_source_at"] is None
        assert at_receipt["blocker"] == "Kalshi paper rechecks are unavailable" and "review_key" not in at_receipt
        assert ticket(effective)["fee_evidence"]["multiplier"] == ticket(effective + TICK)["fee_evidence"]["multiplier"] == "2"
        assert ticket(late - TICK)["fee_evidence"]["multiplier"] == "2"
        assert ticket(late)["fee_evidence"]["event_change_receipts"][0]["receipt_id"] == late_doc
        assert ticket(late)["fee_evidence"]["multiplier"] == ticket(late + TICK)["fee_evidence"]["multiplier"] == "3"
        no = ticket(fee_at, "NO")
        assert no["side_id"] == "no" and no["order"]["fills"] == [[.90, 2]]
        assert no["order"]["max_loss"] == 1.82

        class FixedClock(server.datetime):
            @classmethod
            def now(cls, tz=None):
                return fee_at

        def unexpected_context(*args, **kwargs):
            raise AssertionError("A live Kalshi ticket does not need Polymarket captures or models")

        with monkeypatch.context() as patched:
            patched.setattr(server, "datetime", FixedClock)
            patched.setattr(server, "_trade_context", unexpected_context)
            patched.setattr(server, "active_model", unexpected_context)
            live = get("/api/trade", venue=archive.KALSHI, instrument=ticker, side="YES", budget=2)
        assert live["at"] == market.iso(fee_at) and live["market_open"]
        for field in ("book_receipt_id", "quote_source_at", "listing_evidence", "fee_evidence", "order", "blocker"):
            assert live[field] == at_receipt[field]
        assert "review_key" not in live

        # The live-history preview can open from the exact venue book and
        # station day without waiting for the full live weather/session tape.
        collector.save_weather(tmp_path, [("KNYC", DAY, "observation", "NWS", T0, {
            "at": market.iso(T0), "available_at": market.iso(T0), "source": "NWS", "temp_f": 49,
        })])

        def live_ticket(at):
            return get("/api/trade", venue=archive.KALSHI, day=DAY, archived=1, history_mode="live",
                       instrument=ticker, side="YES", budget=2, at=market.iso(at))

        with monkeypatch.context() as patched:
            def unexpected_session(*args, **kwargs):
                raise AssertionError("Exact archived Kalshi ladder should not build a broad session")
            patched.setattr(server, "_session_context", unexpected_session)
            fast = live_ticket(fee_at)
        with monkeypatch.context() as patched:
            patched.setattr(server, "_kalshi_live_ticket_capture", lambda *args: None)
            assert live_ticket(fee_at) == fast
        assert fast["book_receipt_id"] == book_id and fast["order"] == at_receipt["order"]

        # The newer image was received, but its batch was still unpublished.
        # The scoped read must find the older eligible receipt in that window.
        newer_at = fee_at + TICK
        published_at = newer_at + TICK
        with archive.connect(tmp_path) as db, db:
            newer_reply, newer_id = save(db, archive.KALSHI, "book", KALSHI_API + "/markets/orderbooks", newer_at, {})
            newer = venue_sources.parse_kalshi_book({"ticker": ticker, "orderbook_fp": {
                "yes_dollars": [["0.10", "2.8"]], "no_dollars": [["0.75", "25"]],
            }}, newer_reply)
            newer["available_at"] = market.iso(published_at)
            archive.save_book(db, archive.KALSHI, ticker, newer_id, newer)
        assert live_ticket(newer_at)["book_receipt_id"] == book_id
        assert live_ticket(published_at)["book_receipt_id"] == newer_id
        whole = fee_at + timedelta(seconds=1)
        with archive.connect(tmp_path) as db, db:
            for clock in (whole, whole + TICK):
                reply, ident = save(db, archive.KALSHI, "book", KALSHI_API + "/markets/orderbooks", clock, {})
                archive.save_book(db, archive.KALSHI, ticker, ident, {
                    **newer, "at": reply.received_at, "available_at": reply.received_at,
                })
                assert ident > newer_id
                if clock == whole:
                    whole_id = ident
        assert live_ticket(whole)["book_receipt_id"] == whole_id
        assert live_ticket(whole + TICK)["book_receipt_id"] == ident

    waiver_at = late + timedelta(seconds=1)
    expiration = waiver_at + timedelta(seconds=1)
    with archive.connect(tmp_path) as db, db:
        waived = {**raw, "fee_waiver_expiration_time": market.iso(expiration)}
        save(db, archive.KALSHI, "catalog-recent", listing_source, waiver_at, {"markets": [waived]})
    assert "waiver requires separate start" in kalshi_terms(tmp_path, ticker, expiration - TICK)["reason"]
    assert kalshi_terms(tmp_path, ticker, expiration)["multiplier"] == "3"
    with archive.connect(tmp_path) as db, db:
        save(db, archive.KALSHI, "events", KALSHI_API + "/events?series_ticker=KXHIGHNY", expiration,
             {"events": [{"series_ticker": "KXHIGHNY", "event_ticker": event_id, "fee_multiplier_override": 2}]})
    contradicted = kalshi_terms(tmp_path, ticker, expiration)
    assert contradicted["status"] == "contradictory"
    assert "disagrees with the exact event receipt" in contradicted["reason"]


def test_paper_fee_policy_and_frozen_recheck_lock():
    frozen = paper_runs._fee_at(frozen_schedule())
    for at, rate in ((FEE_CHANGE - TICK, "0.0695"), (FEE_CHANGE, "0.095"), (FEE_CHANGE + TICK, "0.095")):
        assert market.fee_coefficient(at) == frozen(at) == Decimal(rate)
    # Identical depth after the boundary costs less when a paper order
    # was inserted before the boundary than when inserted after it.
    event = {"id": "test", "station": "KNYC", "date": "2026-11-14",
             "ends_at": "2026-11-15T00:00:00Z", "settles_at": "2026-11-15T00:00:00Z"}
    bucket = {"id": "test-market", "label": "test", "quantity_step": "1"}
    book = {"at": market.iso(FEE_CHANGE), "state": "OPEN", "source_at": market.iso(FEE_CHANGE), "depth_kind": "displayed_ladder",
            "bids": [["0.4", "100"]], "offers": [["0.5", "100"]]}
    locked = market._price(event, bucket, "YES", book, 0.9, FEE_CHANGE, Decimal(60), 30, fee_clock=FEE_CHANGE - TICK)
    later = market._price(event, bucket, "YES", book, 0.9, FEE_CHANGE, Decimal(60), 30)
    assert locked["quantity"] == later["quantity"] == 100
    assert locked["fee"] == 1.74 and later["fee"] == 2.38
