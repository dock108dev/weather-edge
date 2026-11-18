import unittest

from weather_edge.history import bucket_bounds, parse_observations, parse_prices
from weather_edge.nbm import climate_window


class ReplayEvidenceTests(unittest.TestCase):
    def test_reported_upper_bound_is_inclusive(self):
        self.assertEqual(bucket_bounds("71 to 72"), (71, 72))
        self.assertEqual(bucket_bounds("70 or below"), (None, 70))
        self.assertEqual(bucket_bounds("79 or above"), (79, None))
        with self.assertRaises(ValueError):
            bucket_bounds("72 to 71")

    def test_prices_are_independent_and_missing_minutes_stay_missing(self):
        start, end = climate_window("2026-11-05")
        result = parse_prices({"history": [
            {"timestamp": start.timestamp(), "longPrice": 0.28, "shortPrice": 0.80},
            {"timestamp": start.timestamp() + 120, "longPrice": 0.29},
        ]}, start, end)
        self.assertEqual(result, [
            {"at": "2026-11-05T05:00:00Z", "available_at": "2026-11-05T05:01:00Z", "yes": 0.28, "no": 0.8},
            {"at": "2026-11-05T05:02:00Z", "available_at": "2026-11-05T05:03:00Z", "yes": 0.29, "no": None},
        ])

    def test_receipt_time_is_separate_from_measurement(self):
        start, end = climate_window("2026-11-05")
        observation_csv = (
            "station,valid,tmpf,metar\n"
            "NYC,2026-11-05 04:51,63,KNYC 050451Z AUTO\n"
            "NYC,2026-11-05 05:51,62,KNYC 050551Z AUTO\n"
            "NYC,2026-11-05 06:01,62,KNYC 050601Z AUTO\n"
        )
        observations, unmatched = parse_observations(observation_csv, "KNYC", start, end, {
            ("2026-11-05T05:51:00Z", "KNYC 050551Z AUTO"): ("2026-11-05T05:58:00Z", "madis-file"),
        })
        self.assertEqual(len(observations), 1)
        self.assertEqual(unmatched, 1)
        self.assertEqual(observations[0]["at"], "2026-11-05T05:51:00Z")
        self.assertEqual(observations[0]["available_at"], "2026-11-05T05:58:00Z")

    def test_climate_window_uses_standard_time_during_dst(self):
        start, end = climate_window("2026-09-05")
        self.assertEqual(start.isoformat(), "2026-09-05T05:00:00+00:00")
        self.assertEqual(end.isoformat(), "2026-09-06T05:00:00+00:00")
