"""Compare the monthly NOAA/ERA5 nowcast with exact historical Kalshi display prices."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import timedelta
import gzip
import json
from pathlib import Path
from statistics import mean
import sys
from urllib.parse import quote

from . import research_model, venue_archive, venue_benchmark, venue_clock_study, venue_history
from .nbm import iso, utc
from .venue_sources import EXCEPTIONS, PublicGET, _rows, _scan_error

VERSION = "noaa-monthly-price-comparison-v1"
FAMILY = "KXHMONTHRANGE"
HISTORY = "https://docs.kalshi.com/api-reference/historical/get-historical-market-candlesticks"
METHODS = {"market": "Completed hourly bid/ask midpoint", "weather": "Earlier NOAA and ERA5 nowcast",
           "blend": "Fixed 75% market plus 25% weather"}
LOOKBACK = timedelta(hours=2, minutes=1)


def _archived(data, get, market, decision):
    ticker = market["instrument_id"]
    start = int((decision - LOOKBACK).timestamp())
    end = int((decision - timedelta(minutes=1)).timestamp())
    reply = None
    try:
        reply = get(venue_archive.KALSHI, "/historical/markets/" + quote(ticker, safe="") + "/candlesticks",
                    {"start_ts": start, "end_ts": end, "period_interval": 60})
        payload = reply.json()
        if payload["ticker"] != ticker:
            raise ValueError("Monthly historical candle ticker differs from request")
        entries = venue_history._candle_entries(_rows(payload, "candlesticks"), start, end)
    except EXCEPTIONS as error:
        _scan_error(data, venue_archive.KALSHI, ticker, "global-monthly-candle", "", 0, 0, error, reply)
        return 0
    with venue_archive.connect(data) as db, db:
        receipt = venue_archive.save_receipt(db, venue_archive.KALSHI, venue_history.CANDLE_KIND, reply)
        venue_archive.save_history(db, venue_archive.KALSHI, ticker, venue_history.CANDLE_KIND, entries, receipt)
        venue_archive.set_progress(db, venue_archive.KALSHI, ticker, "global-monthly-candle", "", 1, len(entries), "complete", reply.received_at)
    return len(entries)


def download(data, tests):
    get = PublicGET(data)
    outcomes = Counter()
    cutoffs = venue_history._cutoffs(data)
    cutoff = cutoffs.get("market_settled_ts")
    for row in tests:
        decision = utc(row["at"])
        ongoing = []
        for market in row["markets"]:
            if venue_history._archive_partition(market, cutoff, "market"):
                outcomes["historical_requests"] += 1
                outcomes["archived_candles"] += _archived(data, get, market, decision)
            else:
                ongoing.append(market["instrument_id"])
        if ongoing:
            outcomes["batch_requests"] += 1
            outcomes["public_candles"] += venue_clock_study._batch(data, get, decision, ongoing, 1, before=121, after=-1,
                                                                  prefix="global-monthly/", interval_minutes=60)
    return dict(outcomes)


def _rows_for_prices(data, cases):
    output = []
    reasons = Counter()
    with venue_archive.connect(data) as db:
        for row in cases:
            decision = utc(row["at"])
            distributions = {item["instrument_id"]: item for item in row["probabilities"]}
            for contract in row["markets"]:
                ticker = contract["instrument_id"]
                arrived = db.execute("""SELECT at,receipt_id,body FROM history WHERE venue=? AND instrument_id=? AND kind=?
                    AND at>=? AND at<=? ORDER BY at DESC LIMIT 1""",
                    (venue_archive.KALSHI, ticker, venue_history.CANDLE_KIND, iso(decision - LOOKBACK),
                     iso(decision - timedelta(minutes=1)))).fetchone()
                reasons["eligible native payout cases"] += 1
                if not arrived:
                    reasons["no completed candle in bounds"] += 1
                    continue
                native = json.loads(arrived["body"])
                proxy = venue_benchmark._probability_proxy(native, "kalshi")
                if proxy is None:
                    reasons["incoherent or wide display quotes"] += 1
                    continue
                expected = distributions[ticker]
                weather = expected["probabilities"]["partial_ridge"]
                market = proxy["probability"]
                output.append({"month": row["month"], "date": row["date"], "at": row["at"], "market": contract,
                               "model": row["points_c"], "result": int(expected["native_yes"]),
                               "probabilities": {"weather": weather, "market": market, "blend": 0.75 * market + 0.25 * weather},
                               "book_display": {**proxy, "raw": native, "at": arrived["at"], "source_receipt_id": arrived["receipt_id"]}})
    return output, dict(reasons)


def study(data, source, *, collect):
    original = json.loads(gzip.decompress(source.read_bytes()))
    tests = original["test"]
    acquired = download(data, tests) if collect else {}
    rows, missing = _rows_for_prices(data, tests)
    frozen, digest = research_model.freeze_inputs(data, {"version": VERSION, "rows": rows, "missing": missing,
                                                          "source": research_model._source_entry(data, source)})
    metrics = []
    for label, selected in (("All exact matched cases", rows), ("Later 2026 cohort", [row for row in rows if row["month"] >= "2026-01"])):
        if not selected:
            continue
        for method, title in METHODS.items():
            months = defaultdict(list)
            for row in selected:
                months[row["month"]].append((row["probabilities"][method] - row["result"]) ** 2)
            metrics.append({"group": label, "method": method, "label": title, "calendar_months": len(months), "bucket_cases": len(selected),
                            "bucket_brier": mean(mean(errors) for errors in months.values())})
    cases = []
    if rows:
        row = max(rows, key=lambda item: abs(item["probabilities"]["market"] - item["probabilities"]["weather"]))
        cases.append({"title": f"{row['month']} · Monthly anomaly market and weather estimates", "date": row["date"], "at": row["at"],
                      "venue": "kalshi", "instrument_id": row["market"]["instrument_id"], "region": "Global land and ocean",
                      "detail": (f"Earlier candle midpoint {row['probabilities']['market']:.1%}, NOAA and ERA5 {row['probabilities']['weather']:.1%}. "
                                 f"Native YES paid ${row['result']}."), "evidence": row})
    record = {"version": VERSION, "title": "Monthly anomaly forecasts against the earlier market",
              "hypothesis": "A fixed modest allocation to the NOAA and ERA5 nowcast may improve on the market midpoint alone.",
              "target": "Native Kalshi monthly NOAA global anomaly payouts", "venues": ["kalshi"],
              "date_window": {"sample": [min(row["month"] for row in tests), max(row["month"] for row in tests)]},
              "metrics": metrics, "cases": cases, "coverage": {**missing, **acquired, "matched_cases": len(rows)},
              "limits": ["Each archived candle had already completed at least one minute before the forecast. The capture app learned the historical candle later.",
                         "All methods score the same price cases. The midpoint and fixed blend are probability benchmarks without order-book depth.",
                         "The available monthly market sample is limited, and the ERA5 feature uses a retrospective availability convention."],
              "provenance": {"frozen_input": str(frozen.relative_to(data)), "frozen_input_sha256": digest, "history_docs": HISTORY,
                             "source_sha256": research_model.run_sources(sys.modules[__name__], venue_history, venue_benchmark)}}
    return research_model.write_record(data, record, {"cases": rows, "missing": missing})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--nowcast-details", type=Path, required=True)
    parser.add_argument("--collect", action="store_true")
    args = parser.parse_args()
    print(study(args.data, args.nowcast_details, collect=args.collect))


if __name__ == "__main__":
    main()
