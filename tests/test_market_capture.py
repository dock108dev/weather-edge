import copy
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from weather_edge.capture import load_captures, write_capture
from weather_edge.capture_fixture import START, _event, fixture_capture
from weather_edge.market import fee_coefficient, mark_book_progress, paper_trial, parse_book, parse_event, scan, taker_fee


OPTIONS = dict(duration_seconds=30, poll_seconds=8, max_rounds=8, max_events=12)


def probabilities(record):
    buckets = record["events"][0]["buckets"]
    return {b["id"]: p for b, p in zip(buckets, (0.01, 0.03, 0.32, 0.44, 0.15, 0.05))}


class MarketContractTests(unittest.TestCase):
    def assert_side_rows(self, record, rows):
        indexed = {(row["bucket_id"], row["side"]): row for row in rows}
        expected = {(bucket["id"], side) for bucket in record["events"][0]["buckets"] for side in ("YES", "NO")}
        self.assertEqual(set(indexed), expected)
        self.assertEqual(len(rows), len(expected))
        return indexed

    def test_buying_no_uses_complement_of_native_yes_bid(self):
        record = fixture_capture(**OPTIONS)
        prices = scan(record, record["captured_at"], probabilities(record))
        self.assertEqual(len(prices), 12)
        row = next(r for r in prices if r["label"] == "46 or below" and r["side"] == "NO")
        self.assertEqual(row["fills"][0][0], 0.99)
        self.assertGreater(row["break_even_probability"], 0.99)
        self.assertLess(row["ev_dollars"], 0)
        self.assertLessEqual(row["max_loss"], 25)
        self.assertEqual(row["quote_at"], "2026-11-15T17:10:24Z")

    def test_bucket_boundaries_and_native_side_order(self):
        raw = _event()
        raw["markets"][2]["marketSides"].reverse()
        event = parse_event(raw)
        bucket = event["buckets"][2]
        self.assertEqual((bucket["lower_f"], bucket["upper_f"]), (49, 50))
        self.assertEqual(bucket["yes_side_id"], "103")
        self.assertEqual(bucket["no_side_id"], "203")
        raw["markets"][2]["marketSides"][0]["marketId"] = 123456
        with self.assertRaisesRegex(ValueError, "different market"):
            parse_event(raw)

    def test_november_fee_revision_and_bankers_rounding(self):
        old, new = map(fee_coefficient, ("2026-11-05T17:00:00Z", "2026-11-15T17:00:00Z"))
        self.assertEqual(old, Decimal("0.0695"))
        self.assertEqual(new, Decimal("0.095"))
        self.assertEqual(taker_fee([("0.50", "100")], old), Decimal("1.74"))
        self.assertEqual(taker_fee([("0.50", "100")], new), Decimal("2.38"))
        self.assertEqual(taker_fee([("0.99", "100")], new), Decimal("0.09"))

    def test_missing_and_explicit_empty_book_sides(self):
        payload = {"marketData": {"marketSlug": "example", "state": "MARKET_STATE_OPEN", "offers": []}}
        book = parse_book(payload, "example", START)
        self.assertIsNone(book["bids"])
        self.assertEqual(book["offers"], [])
        with self.assertRaisesRegex(ValueError, "identity"):
            parse_book(payload, "other", START)

    def test_display_prices_do_not_create_executable_depth(self):
        record = fixture_capture(**OPTIONS)
        model = probabilities(record)
        for bucket in record["events"][0]["buckets"]:
            bucket["prices"] = [{"at": record["captured_at"], "yes": 0.01, "no": 0.99}]
            bucket["books"] = []
        prices = scan(record, record["captured_at"], model)
        rows = self.assert_side_rows(record, prices)
        for (_, side), row in rows.items():
            self.assertEqual(row["display_price"], {"YES": 0.01, "NO": 0.99}[side])
            self.assertEqual(row["display_quote_at"], "2026-11-15T17:10:30Z")
            self.assertIsNone(row["quote_at"])
            self.assertIsNone(row["break_even_probability"])
            self.assertIsNone(row["ev_dollars"])
        trial = paper_trial(record, lambda *_: model)
        self.assertEqual((trial["signals"], trial["fills"], trial["cash"]), (0, 0, 500.0))

    def test_source_clock_regression_and_top_price_without_size(self):
        record = fixture_capture(**OPTIONS)
        bucket = record["events"][0]["buckets"][2]
        latest = bucket["books"][-1]
        affected = {**record, "events": [{**record["events"][0], "buckets": [{**bucket, "books": [latest]}]}]}
        model = probabilities(record)
        latest["source_at"] = "2026-11-15T17:10:00.000000001Z"
        mark_book_progress(bucket["books"])
        prices = scan(record, record["captured_at"], model)
        rows = self.assert_side_rows(record, prices)
        instrument = "tc-temp-nychigh-2026-11-15-gte49lt50f"
        for side in ("YES", "NO"):
            row = rows[instrument, side]
            self.assertEqual(row["quote_at"], "2026-11-15T17:10:24Z")
            self.assertEqual(row["quote_source_at"], "2026-11-15T17:10:00.000000001Z")
            self.assertEqual(row["status"], "Unverified or unavailable depth")
            self.assertIsNone(row["entry_price"])
            self.assertIsNone(row["break_even_probability"])
            self.assertIsNone(row["ev_dollars"])
        retained = rows["tc-temp-nychigh-2026-11-15-lt47f", "YES"]
        self.assertEqual((retained["quote_at"], retained["entry_price"]), ("2026-11-15T17:10:24Z", 0.08))
        self.assertEqual(latest["source_time_progress"], "regressed")
        self.assertEqual(paper_trial(affected, lambda *_: model)["fills"], 0)
        latest["source_at"] = "2026-11-15T17:10:24Z"
        latest["bids"] = latest["offers"] = None
        latest["bestAsk"] = 0.12
        mark_book_progress(bucket["books"])
        prices = scan(record, record["captured_at"], model)
        rows = self.assert_side_rows(record, prices)
        for side in ("YES", "NO"):
            row = rows[instrument, side]
            self.assertEqual(row["quote_at"], "2026-11-15T17:10:24Z")
            self.assertEqual(row["status"], "Missing side depth")
            self.assertIsNone(row["entry_price"])
            self.assertIsNone(row["ev_dollars"])
        self.assertEqual(paper_trial(affected, lambda *_: model)["fills"], 0)

    def test_source_clock_aliases_still_compare_exact_price_and_size(self):
        original = {"source_at": "2026-11-15T17:10:00.1Z", "bids": [["0.1", "2"]], "offers": []}
        alias = {**original, "source_at": "2026-11-15T12:10:00.100000000-05:00", "bids": [["0.100", "2.0"]]}
        higher = {**alias, "source_at": "2026-11-15T17:10:00.100000001Z"}
        missing_side = {**alias, "offers": None}
        clock_images = [original, alias, missing_side, higher, dict(alias)]
        mark_book_progress(clock_images)
        self.assertEqual([book["source_time_progress"] for book in clock_images],
                         ["first", "unchanged", "conflicting", "advanced", "regressed"])

    def test_missing_model_allows_break_even_math_and_uncertain_does_not_fill(self):
        record = fixture_capture(**OPTIONS)
        prices = scan(record, record["captured_at"], {})
        rows = self.assert_side_rows(record, prices)
        for row in rows.values():
            self.assertGreater(row["break_even_probability"], row["entry_price"])
            self.assertIsNone(row["ev_dollars"])
        instrument = "tc-temp-nychigh-2026-11-15-lt47f"
        for side, expected_price in (("YES", 0.08), ("NO", 0.99)):
            row = rows[instrument, side]
            self.assertEqual((row["quote_at"], row["quote_source_at"], row["entry_price"]),
                             ("2026-11-15T17:10:24Z", "2026-11-15T17:10:24Z", expected_price))
        self.assertAlmostEqual(rows[instrument, "YES"]["break_even_probability"], 0.08683333333333333)
        self.assertAlmostEqual(rows[instrument, "NO"]["break_even_probability"], 0.9907927070947286)
        self.assertEqual(paper_trial(record, lambda *_: {})["fills"], 0)
        uncertain = {bid: {"probability": v, "uncertain": True} for bid, v in probabilities(record).items()}
        self.assertEqual(paper_trial(record, lambda *_: uncertain)["fills"], 0)


class CaptureFixtureTests(unittest.TestCase):
    def test_forecast_observation_and_depth_receipts(self):
        record = fixture_capture(**OPTIONS)
        self.assertEqual(record["kind"], "fixture")
        self.assertEqual(len(record["events"]), 1)
        self.assertEqual(record["rounds"], 4)
        event = record["events"][0]
        self.assertEqual(event["forecasts"][0]["high_f"], 51)
        self.assertEqual(event["forecasts"][0]["available_at"], record["started_at"])
        self.assertEqual({v["source"] for v in event["observations"]}, {"NWS", "Aviation Weather Center"})
        self.assertEqual(len(event["buckets"]), 6)
        self.assertTrue(all(len(v["books"]) == 4 for v in event["buckets"]))

    def test_settlement_releases_cash_only_when_exchange_payout_observed(self):
        original = fixture_capture(**OPTIONS)
        model = probabilities(original)
        first = paper_trial(original, lambda *_: model, reaction_seconds=5)
        self.assertEqual((first["signals"], first["rechecked"], first["persisted"], first["fills"]), (5, 4, 3, 1))
        self.assertEqual((first["cash"], first["cash_committed"], first["realized_pnl"]), (475, 25, 0))
        self.assertEqual(first["station_days"], [{"station": "KNYC", "date": "2026-11-15", "fills": 1,
                                                  "settled": 0, "cash_committed": 25, "realized_pnl": 0}])
        self.assertEqual((first["settled_station_days"], first["profitable_station_days"]), (0, 0))
        repriced = next(r for r in first["attempts"] if r["label"] == "51 to 52")
        self.assertEqual(repriced["result"], "Edge did not persist")
        self.assertLess(repriced["reaction_ev_dollars"], 0)

        with TemporaryDirectory() as folder:
            folder = Path(folder)
            write_capture(original, folder)
            self.assertIsNone(load_captures(folder))
            previous = load_captures(folder, fixture=True)
            follow_up = fixture_capture(start=START + timedelta(days=1), previous=previous, **OPTIONS)
            self.assertEqual(follow_up["rounds"], 0)
            self.assertEqual(len(follow_up["settlements"][previous["events"][0]["id"]]), 6)
            write_capture(follow_up, folder)
            merged = load_captures(folder, fixture=True)
            self.assertEqual(merged["session_count"], 2)
            self.assertEqual(len(merged["sessions"]), 2)
        before = paper_trial(merged, lambda *_: model, as_of=original["captured_at"])
        self.assertEqual(before["cash_committed"], 25)
        after = paper_trial(merged, lambda *_: model)
        self.assertEqual((after["settled"], after["cash"], after["cash_committed"], after["realized_pnl"]), (1, 475, 0, -25))
        self.assertEqual(after["station_days"], [{"station": "KNYC", "date": "2026-11-15", "fills": 1,
                                                  "settled": 1, "cash_committed": 0, "realized_pnl": -25}])
        self.assertEqual((after["settled_station_days"], after["profitable_station_days"]), (1, 0))

    def test_reaction_wait_uses_first_eligible_book_in_window(self):
        record = fixture_capture(**OPTIONS)
        model = probabilities(record)
        lagged = paper_trial(record, lambda *_: model, reaction_seconds=10, max_recheck_lag=0)
        self.assertEqual(lagged["fills"], 0)
        gap = copy.deepcopy(record)
        bucket = gap["events"][0]["buckets"][2]
        bucket["books"][1]["depth_kind"] = "gap"
        after_gap = paper_trial(gap, lambda *_: model)
        first = next(a for a in after_gap["attempts"] if a["label"] == "49 to 50")
        self.assertEqual(first["reaction_at"], "2026-11-15T17:10:16Z")
        self.assertGreater(first["reaction_ev_dollars"], 0)
        bucket["books"][2]["depth_kind"] = "gap"
        without_book = paper_trial(gap, lambda *_: model)
        first = next(a for a in without_book["attempts"] if a["label"] == "49 to 50")
        self.assertIsNone(first["reaction_at"])
        self.assertEqual(first["result"], "Missing eligible recheck")


if __name__ == "__main__":
    unittest.main()
