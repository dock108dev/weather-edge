"""Compare archived price movement around NOAA's published high forecast revisions."""

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import gzip
from hashlib import sha256
import json
from pathlib import Path
from random import Random
from statistics import mean, median

from sklearn import __version__ as sklearn_version
from sklearn.linear_model import Ridge

from . import research_model, study, venue_archive
from .market import decimal, iso, parse_time
from .model import _limits, _probability
from .strategy_rules import temperature_window


VERSION = "forecast-innovation-price-response-v1"
STATIONS = ("KNYC", "KMDW", "KPHL", "KSFO", "KLAX", "KMIA")
FIT = ("2026-10-01", "2026-10-23")
TEST = ("2026-10-28", "2026-11-13")
MIN_CHANGE = 0.05
NBS = "noaa-nbm-nbs-txn"
POINT_LAG = timedelta(minutes=1)
VENUE_HORIZONS = {
    venue_archive.POLY: ("display-price-1m", timedelta(minutes=1), (5, 15, 60)),
    venue_archive.KALSHI: ("candle-60m", timedelta(hours=1), (60, 180)),
}


def archive_events(data):
    for path in sorted((data / "archive").glob("????-??-??/K???.json")):
        day, station = path.parent.name, path.stem
        if station not in STATIONS or not (FIT[0] <= day <= FIT[1] or TEST[0] <= day <= TEST[1]):
            continue
        blob = path.read_bytes()
        event = json.loads(blob)
        if event.get("station") == station and event.get("date") == day:
            yield event, {"path": str(path.relative_to(data)), "sha256": sha256(blob).hexdigest()}


def catalog(data):
    """Require a unique native event with a settled, exhaustive bucket partition."""
    with venue_archive.connect(data) as db:
        keys = "venue, station, day, event_id, instrument_id, label, units, operator, lower, upper, rules_primary, resolution_source, opened_at, closed_at, settlement_at, yes_payout, settlement_receipt_id"
        query = f"SELECT {keys} FROM markets WHERE family='daily-high' AND status='settled' AND day BETWEEN ? AND ? AND station IN ({','.join('?' for _ in STATIONS)})"
        rows = db.execute(query, (FIT[0], TEST[1], *STATIONS)).fetchall()
    groups = defaultdict(lambda: defaultdict(list))
    for row in map(dict, rows):
        group = row["venue"], row["station"], row["day"]
        groups[group][row["event_id"]].append(row)
    supported = {}
    for key, alternatives in groups.items():
        if len(alternatives) != 1:
            continue
        group = next(iter(alternatives.values()))
        buckets = []
        for row in group:
            limits = temperature_window(row)
            if not (limits and row["settlement_receipt_id"] and row["opened_at"] and row["closed_at"]):
                break
            payout = decimal(row["yes_payout"])
            if payout not in (0, 1):
                break
            buckets.append({**row, "lower_f": limits[0], "upper_f": limits[1], "outcome": int(payout)})
        if (len(buckets) == len(group) and study._partition(buckets) == "exhaustive"
                and sum(row["outcome"] for row in buckets) == 1):
            supported[key] = sorted(buckets, key=lambda row: _limits(row))
    return supported


def revisions(event, gaps):
    day_start = parse_time(event["climate_starts_at"])
    day_end = parse_time(event["climate_ends_at"])
    cards = [fc for fc in event["forecasts"] if fc.get("high_source") == NBS and fc.get("high_f") is not None
             and fc.get("high_sd_f", 0) and fc.get("source", "").startswith("https://noaa-nbm-grib2-pds.s3.amazonaws.com/")]
    cards.sort(key=lambda row: parse_time(row["issued_at"]))
    for previous, current in zip(cards, cards[1:]):
        older, newer = parse_time(previous["issued_at"]), parse_time(current["issued_at"])
        when = parse_time(current["available_at"])
        if not day_start <= when < day_end:
            continue
        if current["valid_at"] != previous["valid_at"] or newer - older != timedelta(hours=6):
            gaps["not a consecutive six-hour update of the same TXN interval"] += 1
            continue
        if not older <= parse_time(previous["available_at"]) < when or newer > when:
            gaps["publication clock incompatible with issue sequence"] += 1
            continue
        yield previous, current


def _price_entry(venue, at, receipt, body):
    if venue == venue_archive.POLY:
        stamp = body.get("timestamp")
        low, high = body.get("shortPrice"), body.get("longPrice")
        if low is not None:
            low = 1 - decimal(low)
    else:
        stamp = body.get("end_period_ts")
        low = venue_archive.kalshi_dollar_close(body.get("yes_bid"))
        high = venue_archive.kalshi_dollar_close(body.get("yes_ask"))
    if not isinstance(stamp, int) or low is None or high is None or datetime.fromtimestamp(stamp, timezone.utc) != at:
        return None
    bid, ask = decimal(low), decimal(high)
    if not 0 <= bid <= ask <= 1 or (bid == 0 and ask == 1):
        return None
    return {"at": iso(at), "available_at_under_lag": iso(at + POINT_LAG), "receipt_id": receipt,
            "yes_bid": float(bid), "yes_ask": float(ask), "midpoint": float((bid + ask) / 2)}


def prices(db, venue, bucket, start, end):
    kind, _, _ = VENUE_HORIZONS[venue]
    rows = db.execute("""SELECT at, receipt_id, body FROM history
                        WHERE venue=? AND instrument_id=? AND kind=? AND at BETWEEN ? AND ? ORDER BY at""",
                      (venue, bucket["instrument_id"], kind, iso(start), iso(end)))
    points = {}
    for raw in rows:
        at = parse_time(raw["at"])
        if point := _price_entry(venue, at, raw["receipt_id"], json.loads(raw["body"])):
            points[at + POINT_LAG] = point
    return list(points), points


def latest(times, trace, at, maximum_age):
    index = bisect_right(times, at) - 1
    if index >= 0 and at - times[index] <= maximum_age:
        return trace[times[index]]
    return None


def assemble(data):
    markets = catalog(data)
    gaps = Counter()
    sources = []
    cases = []
    opportunity_count = Counter()
    with venue_archive.connect(data) as db:
        for event, source in archive_events(data):
            sources.append(source)
            clock = "fit" if event["date"] <= FIT[1] else "later test"
            weather_revisions = list(revisions(event, gaps))
            for venue, (_, resolution, horizons) in VENUE_HORIZONS.items():
                buckets = markets.get((venue, event["station"], event["date"]))
                if not buckets:
                    gaps[f"{venue}: no exact verified native partition"] += 1
                    continue
                opens = max(parse_time(row["opened_at"]) for row in buckets)
                closes = min(parse_time(row["closed_at"]) for row in buckets)
                for before, after in weather_revisions:
                    at = parse_time(after["available_at"])
                    if not opens < at < closes:
                        continue
                    comparison = [
                        (b, *[_probability(_limits(b), [f["high_f"]], f["high_sd_f"]) for f in (before, after)])
                        for b in buckets
                    ]
                    bucket, old, new = max(comparison, key=lambda item: abs(item[1] - item[2]))
                    signal = new - old
                    if abs(signal) < MIN_CHANGE:
                        gaps[f"{venue}: no five-point probability revision"] += 1
                        continue
                    opportunity_count[venue, clock] += 1
                    span = timedelta(minutes=max(horizons))
                    times, trace = prices(db, venue, bucket, at - span - resolution - POINT_LAG, at + span)
                    opening = latest(times, trace, at, resolution)
                    if opening is None or parse_time(opening["at"]) < opens:
                        gaps[f"{venue}: no timely pre-publication quote"] += 1
                        continue
                    movement = []
                    for horizon in horizons:
                        interval = timedelta(minutes=horizon)
                        end = at + interval
                        if end >= closes:
                            gaps[f"{venue}/{horizon}m: market closes before measurement"] += 1
                            continue
                        later = latest(times, trace, end, resolution)
                        if later is None or parse_time(later["at"]) < at:
                            gaps[f"{venue}/{horizon}m: no later displayed price"] += 1
                            continue
                        placebo = latest(times, trace, at - interval, resolution)
                        movement.append({"minutes": horizon, "before": opening, "after": later, "placebo": placebo,
                                         "change": later["midpoint"] - opening["midpoint"],
                                         "placebo_change": opening["midpoint"] - placebo["midpoint"] if placebo else None})
                    cases.append({"window": clock, "venue": venue, "station": event["station"], "date": event["date"],
                                  "event_id": bucket["event_id"], "instrument_id": bucket["instrument_id"], "at": iso(at),
                                  "title": f"{event['station']} {bucket['label']} forecast revision",
                                  "detail": f"NOAA TXN {before['high_f']} to {after['high_f']}°F. Bucket probability {old:.1%} to {new:.1%}. Native YES paid ${bucket['outcome']}.",
                                  "bounds": [bucket["lower_f"], bucket["upper_f"]], "old_p": old, "new_p": new,
                                  "signal": signal, "outcome": bucket["outcome"], "market": bucket,
                                  "forecasts": [before, after], "weather_archive": source, "movement": movement})
    return cases, dict(gaps), sources, opportunity_count


def _day_average(rows, getter):
    by_day = defaultdict(list)
    for row in rows:
        by_day[row[0]["date"]].append(getter(*row))
    return dict((key, mean(values)) for key, values in by_day.items())


def _interval(values):
    if len(values) < 3:
        return None
    rng = Random(11015)
    sample = sorted(mean(rng.choices(values, k=len(values))) for _ in range(1000))
    return [sample[49], sample[949]]


def _sign(value):
    return 1 if value > 0 else -1


def _features(case, outcome):
    opening = outcome["before"]
    return [case["signal"], case["signal"] * (1 - abs(2 * opening["midpoint"] - 1)),
            case["signal"] * (opening["yes_ask"] - opening["yes_bid"])]


def analyze(cases, potential):
    metrics, models = [], []
    for venue, (_, _, horizons) in VENUE_HORIZONS.items():
        for horizon in horizons:
            matched = {phase: [(case, move) for case in cases if case["window"] == phase and case["venue"] == venue
                               for move in case["movement"] if move["minutes"] == horizon]
                       for phase in ("fit", "later test")}
            fit = matched["fit"]
            learned = Ridge(alpha=0.1, fit_intercept=False).fit([_features(*row) for row in fit],
                                                                [move["change"] for _, move in fit]) if fit else None
            if learned is not None:
                models.append({"venue": venue, "minutes": horizon, "weights": learned.coef_.tolist()})
            for phase, tested in matched.items():
                signed = [move["change"] * _sign(case["signal"]) for case, move in tested]
                day_moves = _day_average(tested, lambda case, move: move["change"] * _sign(case["signal"]))
                placebo_pairs = [(case, move) for case, move in tested if move["placebo_change"] is not None]
                net_by_day = _day_average(placebo_pairs, lambda case, move: (
                    move["change"] - move["placebo_change"]) * _sign(case["signal"]))
                naive = _day_average(tested, lambda _, move: abs(move["change"]))
                updated = _day_average(tested, lambda case, move: abs(move["change"] - float(learned.predict([
                    _features(case, move)])[0]))) if learned is not None else {}
                interval = _interval(list(net_by_day.values()))
                metrics.append({"window": phase, "venue": venue, "horizon_minutes": horizon,
                                "opportunities": potential[venue, phase], "matched_revisions": len(tested),
                                "matching_calendar_dates": len(day_moves),
                                "matching_station_days": len({(case["station"], case["date"]) for case, _ in tested}),
                                "same_direction": sum(value > 0 for value in signed), "flat": signed.count(0),
                                "median_signed_move_points": 100 * median(signed) if signed else None,
                                "mean_same_day_placebo_adjusted_points": 100 * mean(net_by_day.values()) if net_by_day else None,
                                "placebo_matched": len(placebo_pairs),
                                "placebo_ci90_low_points": 100 * interval[0] if interval else None,
                                "placebo_ci90_high_points": 100 * interval[1] if interval else None,
                                "old_noaa_brier": mean((case["old_p"] - case["outcome"]) ** 2 for case, _ in tested) if tested else None,
                                "new_noaa_brier": mean((case["new_p"] - case["outcome"]) ** 2 for case, _ in tested) if tested else None,
                                "no_move_mae_points": 100 * mean(naive.values()) if naive else None,
                                "ridge_mae_points": 100 * mean(updated.values()) if updated else None})
    return metrics, models


def matched_venues(cases):
    """Measure the one-hour change where the same TXN move targets the same integer bounds."""
    groups = defaultdict(dict)
    for case in cases:
        move = next((row for row in case["movement"] if row["minutes"] == 60), None)
        if move is not None:
            key = case["window"], case["station"], case["date"], case["at"], tuple(case["bounds"])
            groups[key][case["venue"]] = case, move
    outcomes, metrics = [], []
    for (phase, station, day, at, bounds), matched in groups.items():
        if set(matched) != set(VENUE_HORIZONS):
            continue
        quotes = {venue: {"instrument_id": case["instrument_id"], "price_change": move["change"],
                          "payout": case["outcome"], "source": case["market"]["resolution_source"]}
                  for venue, (case, move) in matched.items()}
        signal = next(iter(matched.values()))[0]["signal"]
        outcomes.append({"window": phase, "station": station, "date": day, "at": at, "bounds": list(bounds),
                         "signal": signal, "prices": quotes})
    for phase in ("fit", "later test"):
        joined = [row for row in outcomes if row["window"] == phase]
        for venue in VENUE_HORIZONS:
            signed = [row["prices"][venue]["price_change"] * _sign(row["signal"]) for row in joined]
            metrics.append({"window": f"{phase} exact two-venue weather intervals", "venue": venue, "horizon_minutes": 60,
                            "matching_calendar_dates": len({row["date"] for row in joined}), "matched_revisions": len(joined),
                            "matching_station_days": len({(row["station"], row["date"]) for row in joined}),
                            "same_direction": sum(value > 0 for value in signed), "flat": signed.count(0),
                            "median_signed_move_points": 100 * median(signed) if signed else None,
                            "historical_payout_disagreements": sum(len({p["payout"] for p in row["prices"].values()}) > 1
                                                                   for row in joined)})
    return metrics, outcomes


def representative(cases):
    allowed = [case for case in cases if case["window"] == "later test" and case["movement"]]
    examples = []
    for venue in VENUE_HORIZONS:
        rows = [case for case in allowed if case["venue"] == venue]
        if not rows:
            continue
        examples.append(max(rows, key=lambda case: abs(case["signal"])))
        examples.append(min(rows, key=lambda case: case["movement"][0]["change"] * _sign(case["signal"])))
    return examples


def verify(data, path):
    report = json.loads(path.read_bytes())
    provenance = report["provenance"]
    payload = (data / provenance["replay_details_path"]).read_bytes()
    if sha256(payload).hexdigest() != provenance["replay_details_sha256"]:
        raise ValueError("Forecast innovation evidence checksum mismatch")
    details = json.loads(gzip.decompress(payload))
    potential = {(venue, phase): count for venue, phase, count in details["pre_quote_opportunities"]}
    metrics, models = analyze(details["cases"], potential)
    matched_metrics, matched = matched_venues(details["cases"])
    if [*metrics, *matched_metrics] != report["metrics"] or models != details["fit_models"] or matched != details["matched_venues"]:
        raise ValueError("Forecast innovation metrics differ from the frozen record")
    return {"id": report["id"], "cases": len(details["cases"]), "verified": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=venue_archive.DEFAULT_DATA)
    parser.add_argument("--verify", type=Path)
    args = parser.parse_args()
    if args.verify:
        print(json.dumps(verify(args.data, args.verify)))
        return
    cases, gaps, sources, potential = assemble(args.data)
    metrics, models = analyze(cases, potential)
    matched_metrics, matched = matched_venues(cases)
    metrics.extend(matched_metrics)
    result = {"version": VERSION, "title": "Did archived market prices react to NOAA high revisions?",
              "hypothesis": "A five-point NOAA daily-high revision moves its most affected bucket beyond prepublication drift.",
              "target": "Exact venue payout buckets, coherent bid–ask prices, and consecutive six-hour NOAA TXN updates.",
              "venues": list(VENUE_HORIZONS), "date_window": {"from": FIT[0], "through": TEST[1], "fit": FIT, "test": TEST},
              "metrics": metrics, "cases": representative(cases),
              "limits": ["Historical NOAA object publication clocks do not establish receipt or processing by other traders.",
                         "A one-minute availability offset applies to display marks; Kalshi hourly candles blur the timing of the move.",
                         "Placebo price movement on the same station-day helps measure drift but cannot identify causal impact.",
                         "Native payout terms stay attached to each venue even where the underlying station and interval align.",
                         "Only later binary exchange outcomes enter the hindsight weather score. Historical price bars and display marks have no executable depth."],
              "provenance": {"engine": VERSION, "station_panel": STATIONS, "minimum_probability_change": MIN_CHANGE,
                             "source_count": len(sources), "matched_revisions": len(cases), "gaps": gaps,
                             "scikit_learn": sklearn_version, "ridge_alpha": 0.1, "market_publication_delay_seconds": 60,
                             "implementation_sha256": sha256(Path(__file__).read_bytes()).hexdigest()}}
    path = research_model.write_record(args.data, result, {
        "cases": cases, "archive_inputs": sources, "fit_models": models, "matched_venues": matched,
        "pre_quote_opportunities": [[venue, phase, count] for (venue, phase), count in potential.items()],
    })
    print(json.dumps({"path": str(path), "metrics": metrics, "gaps": gaps}, indent=2))


if __name__ == "__main__":
    main()
