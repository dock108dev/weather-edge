"""Test one-minute and one-hour Kalshi candle clocks on the same historical cases."""

import argparse
from collections import defaultdict
from datetime import timedelta
import gzip
from itertools import batched
import json
from pathlib import Path
from statistics import mean
import sys

from . import research_model, venue_archive, venue_benchmark
from .market import iso, parse_time
from .venue_contracts import temperature_window
from .venue_history import CANDLE_KIND, _candle_entries
from .venue_sources import EXCEPTIONS, PublicGET, _rows, _scan_error

VERSION = "matched-candle-clock-sensitivity-v1"
MINUTE_CANDLE_KIND = "candle-1m"
LIMITS = ("2026-11-03", "2026-11-14")


def _scope(data):
    groups = defaultdict(lambda: {venue: [] for venue in venue_archive.VENUES})
    for row in venue_benchmark._source_rows(data, days=LIMITS):
        band = temperature_window(row)
        if band:
            group = (row["family"], row["station"], row["day"], *band)
            groups[group][row["venue"]].append(row)
    windows = defaultdict(set)
    for group in groups.values():
        if any(len(rows) != 1 for rows in group.values()):
            continue
        left, right = group[venue_archive.POLY][0], group[venue_archive.KALSHI][0]
        if not left["climate_starts_at"] or not right["opened_at"] or not right["closed_at"]:
            continue
        at = parse_time(left["climate_starts_at"])
        opens, closes = parse_time(right["opened_at"]), parse_time(right["closed_at"])
        for hour in venue_benchmark.HOURS:
            nominal = at + timedelta(hours=hour)
            if opens <= nominal < closes:
                windows[nominal].add(right["instrument_id"])
    return windows


def _batch(data, get, nominal, tickers, index, *, before=2, after=0, prefix="", interval_minutes=1):
    if interval_minutes not in (1, 60):
        raise ValueError("Unsupported candle window interval")
    kind = MINUTE_CANDLE_KIND if interval_minutes == 1 else CANDLE_KIND
    window_kind = kind + "-window"
    expected = set(tickers)
    scope = f"{prefix}{iso(nominal)}#{index}"
    start = int((nominal - timedelta(minutes=before)).timestamp())
    end = int((nominal + timedelta(minutes=after)).timestamp())
    reply = None
    try:
        reply = get(venue_archive.KALSHI, "/markets/candlesticks", {
            "market_tickers": ",".join(tickers), "start_ts": start, "end_ts": end, "period_interval": interval_minutes,
        })
        returned = {}
        for row in _rows(reply.json(), "markets"):
            ticker = row["market_ticker"]
            if ticker not in expected or ticker in returned:
                raise ValueError("Unexpected batch candle identity")
            returned[ticker] = _candle_entries(_rows(row, "candlesticks"), start, end)
        with venue_archive.connect(data) as db, db:
            receipt = venue_archive.save_receipt(db, venue_archive.KALSHI, kind, reply)
            for ticker, entries in returned.items():
                venue_archive.save_history(db, venue_archive.KALSHI, ticker, kind, entries, receipt)
            count = sum(map(len, returned.values()))
            error = f"{len(expected - returned.keys())} requested markets absent" if expected != returned.keys() else None
            venue_archive.set_progress(db, venue_archive.KALSHI, scope, window_kind, "", 1, count,
                                       "error" if error else "complete", reply.received_at, error)
        return count
    except EXCEPTIONS as error:
        _scan_error(data, venue_archive.KALSHI, scope, window_kind, "", 0, 0, error, reply)
        return 0


def backfill(data):
    get = PublicGET(data)
    scope = _scope(data)
    count = 0
    points = 0
    for nominal, tickers in sorted(scope.items()):
        for number, group in enumerate(batched(sorted(tickers), 40), 1):
            points += _batch(data, get, nominal, group, number)
            count += 1
    return {"requests": count, "stored_points": points, "utc_clocks": len(scope)}


def _identity(row):
    return row["at"], row["polymarket_market"]["instrument_id"], row["kalshi_market"]["instrument_id"]


def _score(values, field):
    cases = defaultdict(list)
    for row in values:
        cases[row["date"]].append((row[field] - row["outcome"]) ** 2)
    return mean(mean(score) for score in cases.values()) if cases else None


def compare(data, coarse, fine):
    coarse_rows = {_identity(row): row for row in coarse["timeline"] if LIMITS[0] <= row["date"] <= LIMITS[1]}
    fine_rows = {_identity(row): row for row in fine["timeline"]}
    same = []
    revised = []
    for key in coarse_rows.keys() & fine_rows.keys():
        before, after = coarse_rows[key], fine_rows[key]
        if before["outcome"] != after["outcome"] or before["polymarket"] != after["polymarket"]:
            revised.append(key)
            continue
        same.append({**after, "kalshi_hourly": before["kalshi"], "hourly_price": before["kalshi_price"]})
    same.sort(key=_identity)
    cases = []
    unique = set()
    for row in sorted(same, key=lambda item: abs(item["kalshi_hourly"] - item["kalshi"]), reverse=True):
        ident = row["station"], row["date"]
        if ident in unique:
            continue
        native = row["kalshi_market"]
        cases.append({
            "venue": venue_archive.KALSHI, "instrument_id": native["instrument_id"],
            "date": row["date"], "station": row["station"], "at": row["at"],
            "title": f"{row['station']} · {native['label']} · {row['date']}",
            "detail": f"Kalshi hourly midpoint {row['kalshi_hourly']:.1%}, minute midpoint {row['kalshi']:.1%}. "
                      f"Polymarket US display midpoint {row['polymarket']:.1%}.",
            "data": row,
        })
        unique.add(ident)
        if len(cases) == 6:
            break
    replay = {
        "version": VERSION, "timeline": same, "hourly_source_input": coarse,
        "minute_source_input": fine, "revised_keys": sorted(revised),
    }
    frozen, digest = research_model.freeze_inputs(data, replay)
    selection = venue_benchmark._stats(same)
    result = {
        "version": VERSION, "title": "Hourly versus minute market price evidence",
        "hypothesis": "Minute candles improve alignment with Polymarket US and may recover cases hidden by hourly rollups.",
        "target": "Matched station-day threshold and clock", "venues": ["Kalshi", "Polymarket US"],
        "date_window": {"common comparison": list(LIMITS)},
        "coverage": {"same cases": selection, "hourly only": len(coarse_rows.keys() - fine_rows.keys()),
                     "minute only": len(fine_rows.keys() - coarse_rows.keys()), "changed label or display": len(revised)},
        "metrics": [{"group": "Identical cases", "label": label, "method": field, "bucket_brier": _score(same, field),
                     **selection} for field, label in (("kalshi_hourly", "Kalshi hourly"), ("kalshi", "Kalshi minute"),
                                                     ("polymarket", "Polymarket US display"))],
        "limits": ["Historical BBO closing values and display prices are probability proxies with no executable size.",
                   "Minute and hourly endpoints share the same nominal clock. Neither retains original local receipt time.",
                   "This comparison revisits the same dates with a different evidence clock."],
        "cases": cases, "missing": fine["missing"],
        "provenance": {"frozen_input": str(frozen.relative_to(data)), "frozen_input_sha256": digest,
                       "unchanged_clock_method": venue_benchmark.VERSION,
                       "source_sha256": research_model.run_sources(sys.modules[__name__], venue_benchmark)},
    }
    return research_model.write_record(data, result, {"common": same})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--hourly-input", type=Path, required=True)
    parser.add_argument("--download", action="store_true")
    args = parser.parse_args(argv)
    if args.download:
        print(json.dumps(backfill(args.data)), flush=True)
    coarse = json.loads(gzip.decompress(args.hourly_input.read_bytes()))
    fine = venue_benchmark.gather(args.data, kalshi_kind=MINUTE_CANDLE_KIND, days=LIMITS)
    print(compare(args.data, coarse, fine))


if __name__ == "__main__":
    main()
