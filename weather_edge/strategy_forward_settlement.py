"""Audit verified forward payouts against archived native pre-close books."""

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
from statistics import mean

from . import research_model, venue_archive
from .market import decimal, iso, parse_time
from .strategy_rules import book_status
from .strategy_runs import DEFAULT_DATA, RUN_SPLIT


VERSION = "verified-forward-payout-books-v1"
FAMILIES = ("daily-high", "daily-low", "daily-rain", "hourly-weather")
LOOKBACK_MINUTES = (2, 10, 20)
MAX_LOCAL_MARK_AGE = timedelta(seconds=120)


def verified_rows(db, since, through):
    fields = """m.venue,m.family,m.event_id,m.instrument_id,m.station,m.region,m.day,m.label,m.rules_primary,
            m.opened_at,m.closed_at,m.settlement_at,m.settlement_received_at,m.settlement_source,
            m.settlement_receipt_id,m.yes_payout,m.no_payout,m.expiration_value,r.status AS http_status"""
    return db.execute(f"""SELECT {fields} FROM markets m JOIN receipts r ON m.settlement_receipt_id=r.id
                           WHERE m.status='settled' AND m.day BETWEEN '2026-11-14' AND '2026-11-15'
                             AND m.family IN (?,?,?,?) AND m.settlement_received_at BETWEEN ? AND ?
                             AND m.yes_payout IN ('0','1') AND m.no_payout IN ('0','1')
                           ORDER BY m.settlement_received_at,m.venue,m.event_id,m.instrument_id""",
                      (*FAMILIES, iso(since), iso(through))).fetchall()


def inspect(data, since, through):
    records, status = [], defaultdict(Counter)
    with venue_archive.connect(data) as db:
        db.execute("BEGIN")
        for row in map(dict, verified_rows(db, since, through)):
            if row["http_status"] != 200 or row["yes_payout"] == row["no_payout"] or not row["settlement_source"]:
                continue
            found = []
            if row["opened_at"] and row["closed_at"]:
                opened, closed = (parse_time(row[x]) for x in ("opened_at", "closed_at"))
                upper = min(closed, through, parse_time(row["settlement_received_at"]))
                interval = venue_archive._book_query(db, row["venue"], row["instrument_id"], start=max(since, opened), end=upper)
                instants = [book for book in interval if parse_time(book["at"]) < closed]
                # Numeric prices remain preserved for the positive payout case.
                found = sorted(instants, key=lambda b: parse_time(b["at"]))
            complete = [book for book in found if any(book.get(key) for key in ("yes_bids", "no_bids", "bids", "offers"))]
            with_clock = [book for book in complete if book.get("source_at")]
            qualified = [book for book in with_clock
                         if book_status({**book, "http_cache": book.get("http_headers", {})}, None, parse_time(book["at"]), "origin-confirmed") is None]
            key = row["venue"], row["family"], row["day"]
            c = status[key]
            c["verified_payout_instruments"] += 1
            c["verified_instruments_with_pre_close_books"] += bool(found)
            c["verified_instruments_with_pre_close_depth"] += bool(complete)
            c["pre_close_book_receipts"] += len(found)
            c["pre_close_book_receipts_with_exchange_clock"] += len(with_clock)
            c["pre_close_origin_confirmed_receipts"] += len(qualified)
            if row["settlement_at"] and since <= parse_time(row["settlement_at"]) <= through:
                c["exchange_published_during_forward_window"] += 1
            records.append({**row, "pre_close_books": found, "depth_receipts": len(complete),
                            "depth_receipts_with_exchange_clock": len(with_clock), "origin_qualified_receipts": len(qualified)})
    return records, status


def _case(row):
    latest = row["pre_close_books"][-1]
    native = row["settlement_at"] or "unknown"
    return {"station": row["station"], "region": row["region"], "date": row["day"], "venue": row["venue"],
            "event_id": row["event_id"], "instrument_id": row["instrument_id"], "at": latest["at"],
            "title": f"{row['label']} · forward native payout",
            "detail": f"Pre-close ladder received at {latest['at']}; native settlement {native}, locally verified at {row['settlement_received_at']}. YES paid ${row['yes_payout']}.",
            "data": row}


def priced_snapshots(records):
    """Use literal two-sided marks, leaving single-sided and missing frames distinct."""
    grouped = defaultdict(list)
    for row in records:
        if not row["closed_at"]:
            continue
        close = parse_time(row["closed_at"])
        payout_at = parse_time(row["settlement_at"]) if row["settlement_at"] else None
        for horizon in LOOKBACK_MINUTES:
            at = close - timedelta(minutes=horizon)
            if payout_at and at >= payout_at:
                continue
            native = next((book for book in reversed(row["pre_close_books"])
                           if timedelta(0) <= at - parse_time(book["at"]) <= MAX_LOCAL_MARK_AGE), None)
            labels = {key: row[key] for key in ("instrument_id", "event_id", "day", "family", "yes_payout", "venue")}
            record = {**labels, "at": iso(at), "native_book": native, "yes_midpoint": None}
            if native is not None:
                bids, offers = native.get("yes_bids") or [], native.get("yes_offers") or []
                if bids and offers and 0 <= decimal(bids[0][0]) <= decimal(offers[0][0]) <= 1:
                    record["yes_midpoint"] = float((decimal(bids[0][0]) + decimal(offers[0][0])) / 2)
                    record["grade"] = "two-sided display"
                else:
                    record["grade"] = "one-sided or crossed display" if bids or offers else "empty display"
            else:
                record["grade"] = "no displayed receipt in the local mark window"
            grouped[row["venue"], row["family"], horizon].append(record)

    metrics, details = [], {}
    for (venue, family, horizon), rows in sorted(grouped.items()):
        key = f"{venue} / {family} / {horizon} minutes"
        grades = Counter(row["grade"] for row in rows)
        by_event = defaultdict(list)
        for row in rows:
            if row["yes_midpoint"] is not None:
                by_event[row["event_id"]].append((row["yes_midpoint"] - float(row["yes_payout"])) ** 2)
        metrics.append({"venue": venue, "family": family, "minutes_before_native_close": horizon,
                        "matched_instruments": len(rows), "independent_native_events_with_two_sided_price": len(by_event),
                        "calendar_dates_with_two_sided_price": len({r["day"] for r in rows if r["yes_midpoint"] is not None}),
                        "tape_midpoint_brier": mean(mean(scores) for scores in by_event.values()) if by_event else None,
                        "two_sided_price_proxies": grades["two-sided display"], "missing_local_timestamp": grades["no displayed receipt in the local mark window"],
                        "one_sided_or_crossed": grades["one-sided or crossed display"], "empty_depth": grades["empty display"]})
        details[key] = rows
    return metrics, details


def report(data, since, through):
    records, status = inspect(data, since, through)
    matches = [row for row in records if row["pre_close_books"]]
    price_metrics, price_details = priced_snapshots(matches)
    examples = []
    represented = set()
    for row in sorted(matches, key=lambda r: (r["yes_payout"], r["settlement_received_at"], r["venue"]), reverse=True):
        key = row["venue"], row["family"], row["event_id"]
        if key not in represented:
            represented.add(key)
            examples.append(_case(row))
    record = {"version": VERSION, "title": "Which forward payouts had earlier archived native books?",
              "hypothesis": "A verified local payout can close a paper position only when the exchange's exact earlier book itself had eligible fill evidence.",
              "target": "Native binary weather payouts first verified within the forward run, with the exact same instrument's dated pre-close books.",
              "venues": list(venue_archive.VENUES), "date_window": {"from": iso(since), "through": iso(through)},
              "metrics": [{"venue": venue, "family": family, "date": day, **dict(counts), "simulated_fills": 0}
                          for (venue, family, day), counts in sorted(status.items())], "cases": examples[:18],
              "mark_price_metrics": price_metrics,
              "limits": ["Forward verification is the first local verified payout receipt retained in the archive. It can cover an earlier exchange event.",
                         "Native pre-close depth preserves local receipt and payload evidence. Kalshi REST often lacked an exchange image timestamp.",
                         "Price-only Brier requires a two-sided displayed mark received no more than two minutes before each fixed lookback. Thresholds within an event are dependent.",
                         "A real later payout never upgrades missing book clocks or supplies an earlier quote. This audit does not replay orders."],
              "provenance": {"engine": VERSION, "archive": "venues.sqlite3", "scope": "2026-11-14 and 2026-11-15 native climate dates",
                             "implementation_sha256": sha256(Path(__file__).read_bytes()).hexdigest()}}
    return research_model.write_record(data, record, {"matching_positions": matches, "mark_price_details": price_details,
                                                       "native_payouts": [{k: v for k, v in row.items()
                                                                                                          if k != "pre_close_books"} for row in records]})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--since", default=RUN_SPLIT)
    parser.add_argument("--through", default=iso(datetime.now(timezone.utc)))
    args = parser.parse_args()
    since, through = map(parse_time, (args.since, args.through))
    if through < since:
        parser.error("Forward cutoff precedes the split")
    path = report(args.data, since, through)
    record = json.loads(path.read_bytes())
    print(json.dumps({"path": str(path), "metrics": record["metrics"], "mark_price_metrics": record["mark_price_metrics"],
                      "cases": [{k: v for k, v in case.items() if k != "data"}
                                                                                for case in record["cases"]]}, indent=2))


if __name__ == "__main__":
    main()
