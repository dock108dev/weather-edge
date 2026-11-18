"""Test a market blend with the physically guarded late daily high."""

import argparse
from collections import Counter, defaultdict
from datetime import date, timedelta
from hashlib import sha256
import json
from pathlib import Path
from statistics import mean

from . import research_model, strategy_high_transfer as weather, venue_archive
from .market import iso, parse_time
from .strategy_kalshi_history import candle_points, snapshot_at
from .strategy_market import normalized_prices
from .strategy_poly_anchor import asof_price


VERSION = "guarded-high-market-anchor-v1"
FIT = (date(2026, 9, 28), date(2026, 10, 9))
SELECT = (date(2026, 10, 11), date(2026, 10, 21))
TEST = (date(2026, 10, 23), date(2026, 10, 27))
WEIGHTS = (0.0, 0.10, 0.25, 0.5, 0.75, 1.0)
BOOK_AGE = timedelta(minutes=5)


def _deadline(value):
    return parse_time(str(value) + "T10:00:00Z")


def _weather_label_before(row, lock):
    report = row["official_report"]
    recorded = report.get("at") or report.get("published_at")
    return recorded and parse_time(recorded) < lock


def _price_frame(db, data, venue, row):
    at = parse_time(row["at"]) + BOOK_AGE
    points = []
    for bucket in row["buckets"]:
        ident = bucket["instrument_id"]
        opened, closed, settles = (bucket.get(key) for key in ("opened_at", "closed_at", "settlement_at"))
        if not (opened and closed and parse_time(opened) <= at < parse_time(closed)):
            return None
        if settles and at >= parse_time(settles):
            return None
        if venue == venue_archive.POLY:
            point = asof_price(db, ident, at)
        else:
            candles = candle_points(data, ident)
            point = snapshot_at(sorted(candles), candles, at)
            if point and at - parse_time(point["at"]) > BOOK_AGE:
                point = None
        if point is None or parse_time(point["at"]) < parse_time(opened):
            return None
        points.append(point)
    probability = normalized_prices([row["midpoint"] if "midpoint" in row else row["mid"] for row in points])
    if probability is None:
        return None
    out = [{**bucket, "market": value, "guarded": guarded, "quote": quote}
           for bucket, value, guarded, quote in zip(row["buckets"], probability, row["predictions"][weather.MODELS[3]], points, strict=True)]
    return {**row, "venue": venue, "price_evidence_at": iso(at), "buckets": out}


def evaluate(panel, weight):
    days = defaultdict(list)
    for row in panel:
        error = mean(((1 - weight) * bucket["market"] + weight * bucket["guarded"] - bucket["payout"]) ** 2
                     for bucket in row["buckets"])
        days[row["date"]].append(error)
    return mean(mean(items) for items in days.values()) if days else None


def report(data, previous, *, fetch):
    frozen, details = weather._archived(data, previous)
    earlier = [row for row in details["calibration"] if FIT[0] <= date.fromisoformat(row["date"]) <= FIT[1]
               and _weather_label_before(row, _deadline(SELECT[0]))]
    select = [row for row in details["calibration"] if SELECT[0] <= date.fromisoformat(row["date"]) <= SELECT[1]
              and _weather_label_before(row, _deadline(TEST[0]))]
    (_, held), dropped, raw_sources = weather.meteorology(data, fetch, calibration=FIT, test=TEST)
    stated = weather.native_buckets(data, [*select, *held], window=(SELECT[0], TEST[1]))
    reports, gaps, cases, all_rows = [], Counter(), [], {}
    with venue_archive.connect(data) as db:
        for venue in venue_archive.VENUES:
            supported = {(station, day): buckets for (v, station, day), buckets in stated.items() if v == venue}
            if not supported:
                continue
            selection_buckets = {key: values for key, values in supported.items() if venue == venue_archive.POLY or
                                 all(b["settlement_at"] and parse_time(b["settlement_at"]) < _deadline(TEST[0]) for b in values)}
            panels = {}
            for phase, cohort, scopes in (("selection", select, selection_buckets), ("later test", held, supported)):
                if not scopes:
                    continue
                winners, _ = weather.evaluate(earlier, [row for row in cohort if row["local_hour"] == 17], scopes)
                panels[phase] = []
                for row in winners:
                    if priced := _price_frame(db, data, venue, row):
                        panels[phase].append(priced)
                    else:
                        gaps[venue, phase] += 1
            earlier_cases, test = panels.get("selection", []), panels.get("later test", [])
            if not earlier_cases or not test:
                all_rows[venue] = panels
                continue
            locked = min(WEIGHTS, key=lambda value: evaluate(earlier_cases, value))
            for phase, available in panels.items():
                for variant, weight in (("Native midpoint only", 0.0), ("Locked weather blend", locked), ("Guarded weather only", 1.0)):
                    reports.append({"window": phase, "venue": venue, "model": variant, "locked_weather_weight": weight,
                                    "brier": evaluate(available, weight), "station_days": len(available),
                                    "calendar_dates": len({row["date"] for row in available}),
                                    "buckets": sum(len(row["buckets"]) for row in available)})
            daily = {}
            for day in sorted({row["date"] for row in test}):
                group = [row for row in test if row["date"] == day]
                daily[day] = {"market": evaluate(group, 0), "blend": evaluate(group, locked), "weather": evaluate(group, 1)}
            all_rows[venue] = {**panels, "locked_weight": locked, "daily_scores": daily,
                               "selection_grid": {str(w): evaluate(earlier_cases, w) for w in WEIGHTS}}
            largest = max(test, key=lambda row: max(abs(b["market"] - b["guarded"]) for b in row["buckets"]))
            decision = max(largest["buckets"], key=lambda b: abs(b["market"] - b["guarded"]))
            cases.append({"station": largest["station"], "date": largest["date"], "at": largest["price_evidence_at"],
                          "venue": venue, "instrument_id": decision["instrument_id"], "title": f"{largest['station']} late high probability",
                          "detail": f"Venue proxy {decision['market']:.1%}, guarded high {decision['guarded']:.1%}, actual YES ${decision['payout']}.",
                          "data": {"weather": largest, "instrument": decision}})
    record = {"version": VERSION, "title": "Does the stronger late high forecast improve the market?",
              "hypothesis": "A fixed, earlier-selected weight on the physically guarded 17:00 local forecast beats a synchronized price proxy on a new date window.",
              "target": "Exact native daily-high payout thresholds aligned with genuine physical forecasts and same-date NOAA evidence.",
              "venues": list(venue_archive.VENUES), "date_window": {"from": str(FIT[0]), "through": str(TEST[1]),
                                                                 "fit": list(map(str, FIT)), "select": list(map(str, SELECT)),
                                                                 "test": list(map(str, TEST))},
              "metrics": reports, "cases": cases,
              "limits": ["October 23–27 was a gap before the broader transfer evaluation and is held here.",
                         "Midpoints arrive no earlier than their assumed one-minute display delay. The 17:05 standard price check uses weather known at 17:00.",
                         "Every display frame falls inside the archived native trading window and precedes the native payout clock where present.",
                         "Only exact weather and native payout agreement is evaluated. Venue-specific reporting terms remain distinct.",
                         "Selection uses complete prior official report outcomes. Kalshi additionally requires a prior native payout clock.",
                         "The sample covers at most five test dates and supports no executions or promotion by itself."],
              "provenance": {"engine": VERSION, "earlier_source": frozen["id"], "temperature_residual_training_frames": len(earlier),
                             "forecast_gaps": dropped, "price_gaps": {"/".join(key): value for key, value in gaps.items()},
                             "study_implementation_sha256": sha256(Path(__file__).read_bytes()).hexdigest()}}
    return research_model.write_record(data, record, {"panels": all_rows, "raw_cards": raw_sources, "prior_residual_observations": earlier})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=venue_archive.DEFAULT_DATA)
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("prior_transfer_record", type=Path)
    args = parser.parse_args()
    result = report(args.data, args.prior_transfer_record, fetch=args.fetch)
    rec = json.loads(result.read_bytes())
    print(json.dumps({"path": str(result), "metrics": rec["metrics"], "provenance": rec["provenance"]}, indent=2))


if __name__ == "__main__":
    main()
