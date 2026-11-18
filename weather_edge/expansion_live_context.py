"""Compare receipt-timed remaining temperature guidance with the low shadow model."""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from . import collector, expansion_live_low, expansion_nbm, expansion_weather
from . import research_model, research_model_low_full_day, venue_archive
from .nbm import climate_window, iso, utc

VERSION = "live-low-context-v1"


def _profiles(data, event, station, day, at):
    profiles = []
    for row in event.get("forecasts", []):
        if row.get("short_range_availability_basis") != "local_receipt" or not row.get("short_range_temperature"):
            continue
        issue = utc(row["short_range_issued_at"])
        receipt = utc(row["short_range_available_at"])
        if issue <= receipt <= at and at - issue <= timedelta(hours=36):
            profiles.append({"issued_at": iso(issue), "available_at": iso(receipt), "source": row["short_range_source"],
                             "points": [{"valid_at": item["valid_at"], "TMP": item["temp_f"]}
                                        for item in row["short_range_temperature"]]})
    for delta in (-1, 0, 1):
        for hour in research_model.NBS_CYCLES:
            issue = datetime.combine(day + timedelta(days=delta), time(hour), timezone.utc)
            card = expansion_nbm.load_card(data, station, issue)
            if card and issue <= utc(card["available_at"]) <= utc(card["received_at"]) <= at and at - issue <= timedelta(hours=36):
                profiles.append({**card, "available_at": card["received_at"]})
    return profiles


def physical_context(data, event, at):
    """Return temperatures and original clocks for a research comparison."""
    station = event.get("station")
    if station not in research_model.STATIONS or event.get("family") != "daily-low":
        return None
    day = date.fromisoformat(event["date"])
    at = utc(at)
    start, end = climate_window(day, expansion_weather.STATIONS[station][3])
    if not start <= at < end:
        return None
    readings = expansion_live_low._runtime_observations(event, start, at)
    candidates = _profiles(data, event, station, day, at)
    if not readings or not candidates:
        return None
    ordered = sorted(candidates, key=lambda row: row["issued_at"], reverse=True)
    pair = next(((card, remainder) for card in ordered if (remainder := research_model_low_full_day.remaining_minimum(
        card, at, end)) is not None), None)
    if pair is None:
        return None
    card, remaining = pair
    observed = min(readings, key=lambda row: row["temp_f"])
    return {"at": iso(at), "observed_low_f": observed["temp_f"], "observation": observed,
            "remaining_low_f": remaining, "combined_low_f": min(observed["temp_f"], remaining),
            "forecast": {"issued_at": card["issued_at"], "available_at": card["available_at"],
                         "source": card["source"], "covered_from": iso(at), "covered_to": iso(end),
                         "card_sha256": card.get("card_sha256")}}


def live_study(data, at, day):
    data = Path(data)
    origin = utc(at)
    frozen_model = expansion_live_low.load(data)
    reports = []
    cases = []
    for event in collector.weather_events(data, start=str(day), end=str(day)):
        station = event["station"]
        if station not in research_model.STATIONS:
            continue
        rows = venue_archive.query(data, venue="polymarket-us", family="daily-low",
                                   place=station, day=str(day), limit=40)["rows"]
        event = {**event, "family": "daily-low",
                 "buckets": [{"id": row["instrument_id"], "rules": row["rules_primary"]} for row in rows]}
        ridge = expansion_live_low.predict(frozen_model, event, origin)
        if ridge is None:
            continue
        at_decision = utc(ridge["knowledge_at"])
        physics = physical_context(data, event, at_decision)
        if physics is None:
            continue
        market = max(ridge["buckets"], key=lambda row: row["probability"])
        source = next(row for row in rows if row["instrument_id"] == market["id"])
        metrics = {"method": "live_compare", "group": station, "ridge_low_f": ridge["model_low_f"],
                   "observed_low_f": physics["observed_low_f"], "remaining_nbs_low_f": physics["remaining_low_f"],
                   "observed_or_remaining_low_f": physics["combined_low_f"],
                   "weather_decision_at": ridge["knowledge_at"], "validation_days": market["validation_days"]}
        reports.append(metrics)
        text = (f"Earlier Ridge shadow {ridge['model_low_f']:.1f}°F. "
                f"Lowest captured station reading {physics['observed_low_f']:.1f}°F. "
                f"As-issued guidance through climate close {physics['remaining_low_f']:.1f}°F. "
                f"The combined estimate was {physics['combined_low_f']:.1f}°F.")
        cases.append({"station": station, "date": str(day), "at": ridge["knowledge_at"], "venue": "polymarket-us",
                      "instrument_id": market["id"], "title": f"{station} live daily-low weather comparison",
                      "detail": text, "market": source, "forecast": ridge["forecast"],
                      "daily_low_shadow": ridge, "observations_and_remaining": physics})
    if not cases:
        raise ValueError("No real daily-low decision has both prior shadow and causal remaining guidance")
    record = {
        "version": VERSION, "title": "Live minimum versus the remaining forecast",
        "hypothesis": "A live station reading and the available full remainder profile identify when the fitted morning-TXN shadow runs warm.",
        "target": "Live daily minimum temperature context",
        "venues": ["polymarket-us"], "date_window": {"live": [str(day)]}, "metrics": reports, "cases": cases,
        "limits": [
            "All visible weather clocks use previously recorded public receipts. The Ridge display freezes to its trained decision clock.",
            "The remaining-guidance comparison combines the lowest reported observation and the most recent issue covering the rest of the climate day.",
            "The final CLI temperature and exchange payout are still pending for this live example. Station readings can be rounded or revised.",
        ],
        "provenance": {"shadow_vintage": frozen_model["vintage"], "source_sha256": research_model.run_sources(
            sys.modules[__name__], research_model_low_full_day, expansion_live_low)},
    }
    return research_model.write_record(data, record)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path(".data"))
    parser.add_argument("--as-of", type=utc, default=datetime.now(timezone.utc))
    parser.add_argument("--day", type=date.fromisoformat)
    args = parser.parse_args()
    day = args.day or (args.as_of - timedelta(hours=5)).date()
    print(live_study(args.data_dir, args.as_of, day))


if __name__ == "__main__":
    main()
