"""Measure live displayed quantity across exact slower-moving weather listings."""

import argparse
from collections import defaultdict
from decimal import Decimal
import gzip
import json
from pathlib import Path
from statistics import median
import sys

from . import research_model, venue_archive, venue_sources
from .market import decimal, iso

VERSION = "seasonal-weather-book-sample-v1"
SCOPE = "Seasonal weather study books"
SERIES = ("KXCO2LEVEL", "KXELNINOSTRENGTH", "KXGTEMP", "KXMEAD", "KXARCTICICEMAX", "KXHDDCVG")
FAMILIES = ("enso", "annual-temperature")
EVENTS = ("KXRAINNYCM-26NOV", "KXRAINCHIM-26NOV", "KXNYCSNOWM-26DEC", "KXCHISNOWM-26DEC", "KXHMONTHRANGE-26NOV")
ORDERS = (5, 20)


def _markets(data):
    now = iso(venue_sources.clock())
    choice = lambda values: ",".join("?" for _ in values)
    with venue_archive.connect(data) as db:
        return [dict(row) for row in db.execute(
            f"""SELECT {venue_archive.PUBLIC_SELECT} FROM markets WHERE venue=? AND status='open' AND opened_at<=? AND closed_at>?
            AND (series_id IN ({choice(SERIES)}) OR family IN ({choice(FAMILIES)}) OR event_id IN ({choice(EVENTS)}))
            ORDER BY family,event_id,instrument_id""", (venue_archive.KALSHI, now, now, *SERIES, *FAMILIES, *EVENTS),
        )]


def gather(data, *, collect=False):
    markets = _markets(data)
    instruments = [row["instrument_id"] for row in markets]
    if collect:
        if len(instruments) > 100:
            raise ValueError("Seasonal source coverage exceeds one documented public batch")
        expected = f"{len(instruments)} native symbols"
        count = venue_sources.sample_kalshi_books(data, instruments, scope=SCOPE)
        print(f"Received {count} of {expected}", flush=True)
    if not instruments:
        raise ValueError("No active seasonal weather contracts selected")
    values = ",".join("?" for _ in instruments)
    with venue_archive.connect(data) as db:
        expected = db.execute("SELECT updated_at FROM progress WHERE venue=? AND kind='book' AND scope=?",
                              (venue_archive.KALSHI, SCOPE)).fetchone()
        if expected is None:
            raise ValueError("No retained seasonal sample exists")
        at = expected["updated_at"]
        rows = db.execute(f"""SELECT instrument_id,b.body,r.id,r.requested_at,r.received_at,r.headers,r.source,r.status
            FROM books b JOIN receipts r ON r.id=b.receipt_id WHERE b.venue=? AND b.at=? AND b.instrument_id IN ({values})""",
                          (venue_archive.KALSHI, at, *instruments))
        found = {row["instrument_id"]: {**dict(row), "body": json.loads(row["body"]), "headers": json.loads(row["headers"])}
                 for row in rows}
    return {"version": VERSION, "sample_at": at, "markets": [{"market": row, "native": found.get(row["instrument_id"])} for row in markets]}


def _cost(ladder, target):
    remaining = Decimal(target)
    consideration = Decimal(0)
    for one, lots in ladder or ():
        price, quantity = decimal(one), decimal(lots)
        take = min(remaining, quantity)
        consideration += take * price
        remaining -= take
        if remaining <= 0:
            return str(consideration)
    return None


def _shape(row):
    native = row["native"]
    family = row["market"]["family"]
    if not native or native["body"].get("depth_kind") != "displayed_ladder":
        return {"family": family, "received": False, "ask": None, "bid": None, "spread": None, "tiers": {}, "costs": {}}
    book = native["body"]
    asks, bids = book.get("yes_offers"), book.get("yes_bids")
    ask, bid = (decimal(asks[0][0]) if asks else None), (decimal(bids[0][0]) if bids else None)
    spread = ask - bid if ask is not None and bid is not None else None
    return {"family": family, "received": True, "ask": str(ask) if ask is not None else None,
            "bid": str(bid) if bid is not None else None, "spread": str(spread) if spread is not None else None,
            "tiers": {"YES": len(asks or ()), "NO": len(book.get("no_offers") or ())},
            "costs": {str(size): {side: _cost(book.get(f"{side.lower()}_offers"), size) for side in ("YES", "NO")} for size in ORDERS}}


def evaluate(data, corpus):
    frozen, digest = research_model.freeze_inputs(data, corpus)
    by_family = defaultdict(list)
    for row in corpus["markets"]:
        shape = _shape(row)
        by_family[shape["family"]].append({**row, "shape": shape})
    metrics, cases = [], []
    ordered = sorted(by_family.items())
    for family, rows in ordered:
        shapes = [row["shape"] for row in rows]
        spreads = [float(row["spread"]) for row in shapes if row["spread"] is not None]
        metrics.append({"market_family": family, "requested": len(shapes), "valid_ladders": sum(row["received"] for row in shapes),
                        "both_sides": len(spreads), "median_spread_cents": 100 * median(spreads) if spreads else None,
                        "at_most_5c_spread": sum(0 <= amount <= .05 for amount in spreads),
                        **{f"{count}_both_side_displayed": sum(all(v is not None for v in row["costs"].get(str(count), {}).values())
                                                             for row in shapes if row["received"]) for count in ORDERS}})
        chosen = next((row for row in rows if row["shape"]["spread"] is not None and float(row["shape"]["spread"]) <= .20), rows[0])
        market, shape = chosen["market"], chosen["shape"]
        if shape["spread"] is None:
            detail = "The retained snapshot has fewer than two displayed sides."
        else:
            detail = f"Displayed YES bid {float(shape['bid'])*100:g}¢ and ask {float(shape['ask'])*100:g}¢. Native image clock unavailable."
        cases.append({"title": market["title"], "venue": venue_archive.KALSHI, "instrument_id": market["instrument_id"],
                      "date": market["day"], "at": corpus["sample_at"], "detail": detail, "data": chosen})
    obtained = [row["native"] for row in corpus["markets"] if row["native"]]
    clocks = {row["id"]: {key: row[key] for key in ("requested_at", "received_at", "headers", "source", "status")} for row in obtained}
    result = {"version": VERSION, "title": "Live depth on seasonal weather markets",
              "hypothesis": "Seasonal markets that support weather replay may still have little displayed two-sided quantity at one current receipt.",
              "target": "Current Kalshi fixed-point displayed ladders", "venues": ["Kalshi"],
              "date_window": {"receipt": corpus["sample_at"]}, "coverage": {"requested": len(corpus["markets"]),
                           "observed_ladders": sum(row["valid_ladders"] for row in metrics), "both_sides": sum(row["both_sides"] for row in metrics),
                           "available_native_image_clock": sum(bool(row["body"].get("source_at")) for row in obtained)},
              "metrics": metrics, "cases": cases,
              "limits": ["A local HTTP receipt and transport headers do not timestamp the venue image.",
                         "Displayed consideration walks native offer levels before fees. Executability and fills require stricter clocks.",
                         "This single sampled book batch does not establish typical market liquidity."],
              "provenance": {"frozen_input": str(frozen.relative_to(data)), "frozen_input_sha256": digest, "original_receipts": clocks,
                             "public_api": "https://docs.kalshi.com/api-reference/market/get-multiple-market-orderbooks",
                             "source_sha256": research_model.run_sources(sys.modules[__name__], venue_sources)}}
    return research_model.write_record(data, result, {"families": dict(ordered)})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--input", type=Path)
    parser.add_argument("--collect", action="store_true")
    args = parser.parse_args(argv)
    corpus = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else gather(args.data, collect=args.collect)
    print(evaluate(args.data, corpus), flush=True)


if __name__ == "__main__":
    main()
