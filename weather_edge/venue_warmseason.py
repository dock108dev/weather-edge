"""Find price support for exact earlier daily-high pairs on both exchanges."""

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone
from itertools import batched
import json
from pathlib import Path

from . import expansion_nbm, venue_archive, venue_history
from .collector import paused
from .market import iso, parse_time
from .venue_contracts import temperature_window
from .venue_sources import PublicGET

STATIONS = ("KNYC", "KMDW", "KMIA", "KLAX")
WINDOW = ("2026-05-01", "2026-09-30")
CLOCKS = (9, 13, 17)


def pairs(data, *, stations=STATIONS, window=WINDOW):
    if not stations:
        return []
    placeholders = ",".join("?" for _ in stations)
    with venue_archive.connect(data) as db:
        rows = [dict(row) for row in db.execute(
            f"""SELECT {venue_archive.PUBLIC_SELECT} FROM markets WHERE day BETWEEN ? AND ? AND family='daily-high'
            AND station IN ({placeholders}) AND yes_payout IS NOT NULL ORDER BY day,station,instrument_id""", (*window, *stations),
        )]
    candidates = defaultdict(lambda: defaultdict(list))
    for market in rows:
        if band := temperature_window(market):
            candidates[(market["station"], market["day"], band)][market["venue"]].append(market)
    output = []
    for key, group in candidates.items():
        if any(len(group[venue]) != 1 for venue in venue_archive.VENUES):
            continue
        left, right = group[venue_archive.POLY][0], group[venue_archive.KALSHI][0]
        if all(row["opened_at"] and row["closed_at"] for row in (left, right)) and left["climate_starts_at"]:
            output.append({"station": key[0], "day": key[1], "bounds": key[2], "polymarket": left, "kalshi": right})
    return sorted(output, key=lambda row: (row["day"], row["station"], row["polymarket"]["instrument_id"]))


def anchor_pairs(cases):
    groups = defaultdict(list)
    for row in cases:
        groups[(row["day"][:7], row["station"])].append(row)
    chosen = []
    for rows in groups.values():
        days = sorted({row["day"] for row in rows})
        near_middle = days[len(days) // 2]
        market_day = [row for row in rows if row["day"] == near_middle]
        chosen.append(market_day[len(market_day) // 2])
    return chosen


def _attempt(data, get, row, *, clocks):
    left, right = row["polymarket"], row["kalshi"]
    points = {}
    for clock in clocks:
        at = parse_time(left["climate_starts_at"]) + timedelta(hours=clock)
        if not all(parse_time(item["opened_at"]) <= at < parse_time(item["closed_at"]) for item in (left, right)):
            continue
        points[iso(at)] = venue_history._poly_price(data, get, left, window=(at - timedelta(minutes=4), at + timedelta(minutes=2)))
    return {"station": row["station"], "day": row["day"], "instrument_id": left["instrument_id"], "windows": points}


def probe(data, cases):
    get = PublicGET(data)
    return [_attempt(data, get, row, clocks=(13,)) for row in anchor_pairs(cases)]


def _complete(data, venue, kind):
    with venue_archive.connect(data) as db:
        return {row["scope"] for row in db.execute(
            "SELECT scope FROM progress WHERE venue=? AND kind=? AND state='complete'", (venue, kind),
        )}


def acquire(data, cases, venue, clocks):
    get = PublicGET(data)
    if venue == venue_archive.KALSHI:
        kept = count = 0
        live = defaultdict(list)
        completed = _complete(data, venue, venue_history.CANDLE_KIND)
        cutoff = venue_history._cutoffs(data).get("market_settled_ts")
        for row in cases:
            market = row["kalshi"]
            if market["instrument_id"] in completed:
                continue
            if venue_history._archive_partition(market, cutoff, "market"):
                kept += venue_history._archive_candle(data, get, market)
                count += 1
            else:
                live[market["day"]].append(market)
            if count and count % 250 == 0:
                print(f"{count} historical Kalshi requests, {kept} reported points", flush=True)
        for group in live.values():
            for batch in batched(group, 40):
                kept += venue_history._live_candles(data, get, batch)
                count += 1
        return {"requests": count, "reported_points": kept}

    complete = _complete(data, venue, venue_history.DISPLAY_KIND + "-window")
    kept = count = 0
    for row in cases:
        market = row["polymarket"]
        start = parse_time(market["climate_starts_at"])
        for clock in clocks:
            at = start + timedelta(hours=clock)
            request = (at - timedelta(minutes=4), at + timedelta(minutes=2))
            scope = f"{market['instrument_id']}/{iso(request[0])}/{iso(request[1])}"
            if scope in complete or not all(parse_time(item["opened_at"]) <= at < parse_time(item["closed_at"])
                                            for item in (market, row["kalshi"])):
                continue
            kept += venue_history._poly_price(data, get, market, window=request)
            count += 1
            if count % 250 == 0:
                print(f"{count} Polymarket US windows, {kept} reported points", flush=True)
    return {"requests": count, "reported_points": kept}


def acquire_forecasts(data, cases):
    dates = sorted({(row["day"], row["station"]) for row in cases})

    def station_card(pair):
        period, station = pair
        issue = datetime.combine(date.fromisoformat(period), time(hour=12), timezone.utc)
        exists = expansion_nbm.card_path(data, station, issue).is_file()
        if paused(data):
            return {"station": station, "day": period, "state": "paused"}
        try:
            expansion_nbm.load_card(data, station, issue, fetch=True)
        except (OSError, ValueError, KeyError) as error:
            return {"station": station, "day": period, "state": "missing", "error": str(error)}
        return {"station": station, "day": period, "state": "reused" if exists else "new"}

    with ThreadPoolExecutor(max_workers=3) as pool:
        return list(pool.map(station_card, dates))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--venue", choices=venue_archive.VENUES)
    parser.add_argument("--hour", type=int, action="append", choices=CLOCKS)
    parser.add_argument("--nbm", action="store_true")
    args = parser.parse_args(argv)
    candidates = pairs(args.data)
    print(f"{len(candidates)} exact pairs across {len({(r['station'], r['day']) for r in candidates})} station-days", flush=True)
    if args.probe:
        print(json.dumps(probe(args.data, candidates), indent=2), flush=True)
    if args.venue:
        print(json.dumps(acquire(args.data, candidates, args.venue, args.hour or (13,))), flush=True)
    if args.nbm:
        history = acquire_forecasts(args.data, candidates)
        print(json.dumps(history, indent=2), flush=True)


if __name__ == "__main__":
    main()
