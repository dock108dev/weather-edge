"""Capture one bounded supplemental native Midway high tape for research."""

import argparse
from datetime import timedelta
import json
from pathlib import Path
import time

from . import collector, venue_archive, venue_sources
from .strategy_native_capture import native_event
from .strategy_runs import DEFAULT_DATA


STATION = "KMDW"
FAMILY = "daily-high"


def target(data, at):
    day = (at - timedelta(hours=6)).date().isoformat()
    now = venue_archive.receipt_time(at)
    with venue_archive.connect(data) as db:
        accepted = db.execute("""SELECT * FROM markets WHERE venue=? AND family=? AND station=? AND day=?
                                 AND status='open' AND opened_at<=? AND closed_at>?
                                 ORDER BY instrument_id""",
                              (venue_archive.POLY, FAMILY, STATION, day, now, now)).fetchall()
        if not accepted:
            raise ValueError(f"No open native Chicago Midway high event for {day}")
        exact = native_event(db, [dict(row) for row in accepted], at, at)
    return exact["id"], [bucket["id"] for bucket in exact["buckets"]]


def collect(data, rounds, interval, *, phase=None):
    ident, instruments = target(data, collector.now())
    scope = f"supplemental {STATION} high {ident}"
    if phase:
        scope = f"{scope} / {phase}"
    schedule = time.monotonic()
    recorded = []
    for offset in range(rounds):
        while not collector.paused(data) and (wait := schedule - time.monotonic()) > 0:
            time.sleep(min(wait, 0.25))
        if collector.paused(data):
            break
        clock = collector.now()
        sample = venue_sources.watch_poly_book_ids(data, instruments, scope=scope)
        recorded.append({"round": offset + 1, "at": venue_archive.receipt_time(clock), "returned_depth": sample})
        schedule = max(schedule + interval, time.monotonic())
    return {"event_id": ident, "instruments": instruments, "rounds": recorded, **({"phase": phase} if phase else {}),
            "returned_depth": sum(row["returned_depth"] for row in recorded)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--interval", type=float, default=collector.BOOK_INTERVAL)
    parser.add_argument("--phase", help="Distinct local progress label for this bounded follow-up.")
    args = parser.parse_args()
    if args.rounds < 1 or args.interval <= 0:
        parser.error("Positive poll interval and round count are required")
    try:
        result = collect(args.data, args.rounds, args.interval, phase=args.phase)
    except ValueError as error:
        parser.error(str(error))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
