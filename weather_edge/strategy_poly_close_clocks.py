"""Compare Polymarket US event metadata and observed closing-book clocks."""

import argparse
from collections import defaultdict
from hashlib import sha256
import json
from pathlib import Path
from statistics import median
from urllib.parse import quote

from . import research_model, venue_archive, venue_sources
from .market import iso, parse_time


VERSION = "polymarket-temperature-close-clocks-v1"
KIND = "closed-book-clock"
START, END = "2026-10-31", "2026-11-14"
BOOK_DOCS = "https://docs.polymarket.us/api-reference/markets/get-market-book"
RULES = "https://www.cftc.gov/IndustryOversight/IndustryFilings/TradingOrganizationProducts/60333"
FAQ = "https://docs.polymarket.us/faqs/weather-faqs"


def market_rows(db):
    return db.execute("""SELECT m.venue,m.instrument_id,m.event_id,m.station,m.day,m.family,m.label,m.opened_at,m.closed_at,
                                m.climate_starts_at,m.climate_ends_at,m.yes_payout,m.settlement_at,m.settlement_received_at,
                                m.settlement_source,json_extract(e.metadata,'$.endDate') AS event_end_at
                         FROM markets m JOIN events e ON e.venue=m.venue AND e.event_id=m.event_id
                         WHERE m.venue=? AND m.family IN ('daily-high','daily-low') AND m.status='settled'
                           AND m.day BETWEEN ? AND ? AND m.climate_ends_at IS NOT NULL
                           AND m.yes_payout IN ('0','1') ORDER BY m.day DESC,m.family,m.station,m.event_id,m.instrument_id""",
                      (venue_archive.POLY, START, END))


def closed_clock(payload, instrument):
    market = payload["marketData"]
    if market.get("marketSlug") != instrument or market.get("state") != "MARKET_STATE_CLOSED":
        return None
    stats = market.get("stats", {})
    reported = {key: stats.get(key) for key in ("closeSetTime", "lastTradeSetTime", "settlementSetTime",
                                                "settlementPreliminaryFlag", "settlementPx", "closePx", "lastTradePx")}
    if not reported["closeSetTime"] or not reported["lastTradeSetTime"]:
        return None
    if parse_time(reported["lastTradeSetTime"]) > parse_time(reported["closeSetTime"]):
        return None
    return {**reported, "book_state": market["state"], "book_transact_time": market.get("transactTime")}


def acquire(data, budget):
    get = venue_sources.PublicGET(data)
    selected = {}
    with venue_archive.connect(data) as db:
        queried = {row["instrument_id"] for row in db.execute("SELECT instrument_id FROM history WHERE venue=? AND kind=?",
                                                            (venue_archive.POLY, KIND))}
        for row in market_rows(db):
            key = row["event_id"], row["yes_payout"]
            selected.setdefault(key, row["instrument_id"])
    result = {"attempted": 0, "verified": 0, "source_gaps": []}
    for instrument in [value for value in selected.values() if value not in queried][:budget]:
        try:
            receipt = get(venue_archive.POLY, f"/v1/markets/{quote(instrument, safe='')}/book")
            result["attempted"] += 1
        except venue_sources.CollectionPaused:
            break
        except venue_sources.EXCEPTIONS as error:
            result["source_gaps"].append({"instrument_id": instrument, "reason": str(error)})
            continue
        issue = "Closed-state clock pair not present"
        try:
            evidence = closed_clock(receipt.json(), instrument)
        except venue_sources.EXCEPTIONS as error:
            evidence, issue = None, str(error)
        with venue_archive.connect(data) as db, db:
            number = venue_archive.save_receipt(db, venue_archive.POLY, KIND, receipt)
            if evidence:
                venue_archive.save_history(db, venue_archive.POLY, instrument, KIND,
                                           [(evidence["closeSetTime"], iso(evidence["closeSetTime"]), evidence)], number)
                result["verified"] += 1
            else:
                result["source_gaps"].append({"instrument_id": instrument, "reason": issue,
                                              "receipt_id": number})
    return result


def legacy_clocks(data):
    known = {}
    for path in sorted((data / "replay-raw").glob("*/*-closed-book.json")):
        payload = path.read_bytes()
        raw = json.loads(payload)
        instrument = raw.get("marketData", {}).get("marketSlug")
        if instrument and (entry := closed_clock(raw, instrument)):
            known[instrument] = {**entry, "legacy_source": str(path.relative_to(data)), "sha256": sha256(payload).hexdigest()}
    return known


def collect(data):
    legacy = legacy_clocks(data)
    samples = []
    with venue_archive.connect(data) as db:
        db.execute("BEGIN")
        source = {row["instrument_id"]: {**json.loads(row["body"]), "book_receipt_id": row["receipt_id"],
                   "book_receipt_sha256": row["digest"], "body_source": row["source"], "local_book_received_at": row["received_at"],
                   "local_book_requested_at": row["requested_at"]} for row in db.execute(
            """SELECT h.instrument_id,h.body,h.receipt_id,r.digest,r.source,r.received_at,r.requested_at
               FROM history h JOIN receipts r ON r.id=h.receipt_id WHERE h.venue=? AND h.kind=? AND r.status=200
               ORDER BY r.received_at DESC""", (venue_archive.POLY, KIND))}
        for row in map(dict, market_rows(db)):
            proof = source.get(row["instrument_id"], legacy.get(row["instrument_id"]))
            if proof is None or not row["event_end_at"] or not row["closed_at"]:
                continue
            climate = parse_time(row["climate_ends_at"])
            event = parse_time(row["event_end_at"])
            market = parse_time(row["closed_at"])
            close = parse_time(proof["closeSetTime"])
            trade = parse_time(proof["lastTradeSetTime"])
            samples.append({**row, "source_proof": proof,
                            "close_seconds_after_climate_end": (close - climate).total_seconds(),
                            "close_seconds_after_metadata_event_end": (close - event).total_seconds(),
                            "market_end_seconds_after_close": (market - close).total_seconds(),
                            "last_trade_seconds_after_metadata_event_end": (trade - event).total_seconds(),
                            "closed_stats_settlement_matches_close": proof["settlementSetTime"] == proof["closeSetTime"]})
    return samples


def _metrics(rows, family, station):
    near = [r for r in rows if abs(r["close_seconds_after_climate_end"]) <= 120]
    traded = [r for r in rows if r["last_trade_seconds_after_metadata_event_end"] > 0]
    difference = [r["market_end_seconds_after_close"] / 3600 for r in rows]
    return {"family": family, "station": station, "thresholds": len(rows), "events": len({x["event_id"] for x in rows}),
            "dates": len({x["day"] for x in rows}), "closes_within_two_minutes_of_local_climate_end": len(near),
            "last_historical_trade_after_event_end": len(traded), "trading_after_event_end_dates": len({r["day"] for r in traded}),
            "median_close_seconds_after_climate_end": median(r["close_seconds_after_climate_end"] for r in rows),
            "median_listed_market_end_hours_after_close": median(difference),
            "closed_settlement_stats_same_clock": sum(r["closed_stats_settlement_matches_close"] for r in rows)}


def report(data, acquisition):
    rows = collect(data)
    groups = defaultdict(list)
    for row in rows:
        groups[row["family"], row["station"]].append(row)
        groups[row["family"], "all"].append(row)
    metrics = [_metrics(values, family, station) for (family, station), values in sorted(groups.items())]
    cases, dates = [], set()
    for row in sorted(rows, key=lambda r: (r["last_trade_seconds_after_metadata_event_end"], bool(r["source_proof"].get("book_receipt_id"))), reverse=True):
        key = row["station"], row["day"]
        if key in dates or row["last_trade_seconds_after_metadata_event_end"] <= 0:
            continue
        dates.add(key)
        native = row["source_proof"]
        cases.append({"station": row["station"], "date": row["day"], "venue": row["venue"], "instrument_id": row["instrument_id"],
                      "event_id": row["event_id"], "at": iso(native["lastTradeSetTime"]),
                      "title": f'{row["station"]} last trade after the listed event time',
                      "detail": f'The closed book records a last trade at {native["lastTradeSetTime"]}. The event lists {row["event_end_at"]} and the closed book records {native["closeSetTime"]}.',
                      "data": row})
        if len(cases) == 12:
            break
    record = {"version": VERSION, "title": "Which Polymarket clock marks the end of trading?",
              "hypothesis": "The public event’s nominal end date is a reliable cutoff for exact daily temperature trading.",
              "target": "Exact native daily-high and daily-low closed books, last native trades, listed event times, and known climate-day limits.",
              "venues": [venue_archive.POLY], "date_window": {"from": START, "through": END}, "metrics": metrics, "cases": cases,
              "limits": ["A CLOSED book’s closeSetTime is the native close statistic. It is separate from verified cash payout and local receipt time.",
                         "A reported exchange last trade after the nominal event end provides evidence that the event date alone cannot describe the full session.",
                         "This sample uses one reported YES and one NO instrument per event when available, plus retained original archive receipts."],
              "provenance": {"engine": VERSION, "kind": KIND, "book_documentation": BOOK_DOCS, "certification": RULES,
                             "published_faq": FAQ, "new_acquisition": acquisition}}
    return research_model.write_record(data, record, {"matched_closed_books": rows})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=venue_archive.DEFAULT_DATA)
    parser.add_argument("--fetch", type=int, default=0, metavar="MAX_REQUESTS")
    args = parser.parse_args()
    if args.fetch < 0:
        parser.error("Provide a non-negative request budget")
    activity = acquire(args.data, args.fetch) if args.fetch else {}
    path = report(args.data, activity)
    record = json.loads(path.read_bytes())
    print(json.dumps({"path": str(path), "metrics": record["metrics"], "provenance": record["provenance"]}, indent=2))


if __name__ == "__main__":
    main()
