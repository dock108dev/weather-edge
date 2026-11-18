"""Save a receipt-timed point and probability comparison for both live daily lows."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

from . import collector, expansion_live_low, expansion_live_precision, research_model, venue_archive
from .nbm import iso, utc

VERSION = "live-daily-low-bounds-v1"


def study(data, origin):
    day = (origin - timedelta(hours=5)).date().isoformat()
    reference = expansion_live_low.load(data)
    guarded = expansion_live_precision.load(data)
    cases, metrics = [], []
    for event in collector.weather_events(data, start=day, end=day):
        station = event["station"]
        if station not in research_model.STATIONS:
            continue
        candidates = venue_archive.query(data, venue="polymarket-us", family="daily-low", place=station, day=day, limit=40)["rows"]
        market_event = {**event, "family": "daily-low",
                        "buckets": [{"id": row["instrument_id"], "rules": row["rules_primary"]} for row in candidates]}
        raw = expansion_live_low.predict(reference, market_event, origin)
        bounded = expansion_live_precision.predict(guarded, market_event, origin)
        if not raw or not bounded or raw["knowledge_at"] != bounded["knowledge_at"]:
            continue
        reference_p = {row["id"]: row["probability"] for row in raw["buckets"]}
        result = max(bounded["buckets"], key=lambda row: abs(row["probability"] - reference_p[row["id"]]))
        instrument = result["id"]
        market = next(row for row in candidates if row["instrument_id"] == instrument)
        decision = utc(bounded["knowledge_at"])
        books = venue_archive.books(data, "polymarket-us", instrument, start=decision - timedelta(minutes=5),
                                   end=min(origin, decision + timedelta(minutes=5)))
        at_or_before = [book for book in books if utc(book["at"]) <= decision]
        after = [book for book in books if decision < utc(book["at"])]
        raw_probability = reference_p[instrument]
        guarded_probability = result["probability"]
        metrics.append({"method": "paired_live_shadows", "group": station, "reference_low_f": raw["model_low_f"],
                        "bounded_low_f": bounded["model_low_f"], "raw_metar_upper_f": bounded["metar_upper_bound_f"],
                        "reference_yes": raw_probability, "bounded_yes": guarded_probability, "book_receipts": len(books),
                        "calibration_days": result["validation_days"]})
        detail = (f"Raw exact-station report yields a {bounded['metar_upper_bound_f']}°F upper reading. "
                  f"Ridge assigned {raw_probability:.1%} YES to this bucket and the guarded shadow assigned {guarded_probability:.1%}.")
        cases.append({"title": f"{station} · Two live estimates for the same low bucket", "detail": detail, "venue": "polymarket-us",
                      "instrument_id": instrument, "station": station, "date": day, "at": iso(decision), "market": market,
                      "reference": raw, "guarded": bounded, "last_book_before": at_or_before[-1] if at_or_before else None,
                      "first_book_after": after[0] if after else None})
    if not cases:
        raise ValueError("No matching receipt-timed daily-low decisions at the requested origin")
    record = {"version": VERSION, "title": "What the early METAR changed in today's daily lows",
              "hypothesis": "A temperature already reported at the exact station should reduce probability of final daily minima above it.",
              "target": "Original local receipt daily-low forecasts and exact native Polymarket US buckets", "venues": ["polymarket-us"],
              "date_window": {"day": day}, "metrics": metrics, "cases": cases,
              "limits": ["Both forecasts share their latest trained decision clock and original local weather receipt evidence.",
                         "The guarded distribution retains a report exception allowance from its earlier calibration.",
                         "Nearby public book receipts illustrate available depth around the clock. This report contains no simulated execution or completed payout."],
              "provenance": {"origin": iso(origin), "reference_vintage": reference["vintage"], "physical_vintage": guarded["vintage"],
                             "source_sha256": research_model.run_sources(sys.modules[__name__], expansion_live_low, expansion_live_precision)}}
    return research_model.write_record(data, record)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--as-of", type=utc, default=datetime.now(timezone.utc))
    args = parser.parse_args()
    print(study(args.data, args.as_of))


if __name__ == "__main__":
    main()
