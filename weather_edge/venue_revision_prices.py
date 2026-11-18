"""Freeze the actual publication, price and cash clocks for the August Miami revision."""

import argparse
from collections import Counter
from datetime import timedelta
import gzip
import json
from pathlib import Path
import sys
from urllib.parse import quote

from . import research_model, venue_archive, venue_benchmark, venue_history, venue_warmseason_study
from .market import decimal, iso, parse_time
from .venue_sources import PublicGET

VERSION = "miami-climate-revision-price-clocks-v2"
EARLIER = "miami-climate-revision-price-clocks-v1-d87d2f4e395ff57d"
PAIRS = (("KXHIGHMIA-26AUG29-B90.5", "tc-temp-miahigh-2026-08-29-gte90lt91f"),
         ("KXHIGHMIA-26AUG29-T88", "tc-temp-miahigh-2026-08-29-lt88f"))
START, CLOSE = "2026-08-30T04:30:00Z", "2026-08-30T05:00:00Z"
POLY_WINDOWS = (("2026-08-30T04:45:00Z", CLOSE), ("2026-08-30T08:00:00Z", "2026-08-30T09:19:00Z"))


def _download(data):
    get = PublicGET(data)
    start, end = parse_time(START), parse_time(CLOSE)
    at_from, at_to = int(start.timestamp()), int(end.timestamp())
    cutoff = venue_history._cutoffs(data).get("trades_created_ts")
    for left, right in PAIRS:
        market = venue_archive.detail(data, venue_archive.KALSHI, left)
        source = get(venue_archive.KALSHI, f"/historical/markets/{quote(left, safe='')}/candlesticks",
                     {"start_ts": at_from, "end_ts": at_to, "period_interval": 1})

        def parse(payload):
            if payload["ticker"] != left:
                raise ValueError("Kalshi historical candle identifier differs from request")
            return venue_history._candle_entries(payload["candlesticks"], at_from, at_to)

        venue_history._save_history(data, venue_archive.KALSHI, left, "candle-1m", source, parse,
                                    scope=f"{left}/last-30m", progress_kind="candle-1m-window")
        venue_history._trades(data, get, market, cutoff=cutoff, pages=3, window=(start, end))
        for first, last in POLY_WINDOWS:
            venue_history._poly_price(data, get, {"instrument_id": right}, window=(parse_time(first), parse_time(last)))


def _rows(db, venue, instrument, kind, first, last):
    records = db.execute(
        """SELECT h.at,h.body,h.receipt_id,r.source,r.received_at FROM history h JOIN receipts r ON h.receipt_id=r.id
            WHERE h.venue=? AND h.instrument_id=? AND h.kind=? AND julianday(h.at)>=julianday(?) AND julianday(h.at)<=julianday(?)
            ORDER BY h.at,h.native_key""", (venue, instrument, kind, first, last),
    )
    return [{**dict(row), "body": json.loads(row["body"])} for row in records]


def gather(data):
    reports = venue_warmseason_study._revisions(data)
    if len(reports["NWS"]) != 2 or [row["high_f"] for row in reports["NWS"]] != [90, 85]:
        raise ValueError("The original and corrected Miami reports did not verify")
    observations = []
    with venue_archive.connect(data) as db:
        for left, right in PAIRS:
            kala = venue_archive.detail(data, venue_archive.KALSHI, left)
            poly = venue_archive.detail(data, venue_archive.POLY, right)
            bars = _rows(db, venue_archive.KALSHI, left, "candle-1m", START, CLOSE)
            trades = _rows(db, venue_archive.KALSHI, left, "trade", START, CLOSE)
            displays = [row for first, last in POLY_WINDOWS for row in _rows(
                db, venue_archive.POLY, right, "display-price-1m", first, last)]
            observations.append({"kalshi": kala, "polymarket": poly, "final_kalshi_minutes": bars,
                                 "final_kalshi_native_trades": trades, "polymarket_history": displays})
    return {"version": VERSION, "weather": reports, "observations": observations}


def _read_section(rows, first, last):
    located = [row for row in rows if first <= parse_time(row["at"]) < last]
    numbers = Counter((str(row["body"]["longPrice"]), str(row["body"]["shortPrice"])) for row in located)
    return {"first": located[0] if located else None, "last": located[-1] if located else None,
            "minutes": len(located), "distinct_prices_and_counts": [{"yes_ask": p[0], "no_ask": p[1], "minutes": n}
                                                                      for p, n in sorted(numbers.items())]}


def _timeline(observation, first, correction):
    kala, poly = observation["kalshi"], observation["polymarket"]
    series = []
    for label, kind, rows, field in (
        ("Kalshi public YES trades", "trade", observation["final_kalshi_native_trades"], lambda row: row.get("yes_price_dollars")),
        ("Kalshi minute candle YES ask", "display", observation["final_kalshi_minutes"],
         lambda row: venue_archive.kalshi_dollar_close(row.get("yes_ask"))),
        ("Polymarket US historical YES ask", "display", observation["polymarket_history"], lambda row: row.get("longPrice")),
    ):
        points = []
        for row in rows:
            try:
                price = decimal(field(row["body"]))
            except ValueError:
                continue
            if 0 <= price <= 1:
                points.append({"at": row["at"], "yes": float(price)})
        if points:
            series.append({"label": label, "kind": kind, "points": points})
    markers = [
        {"at": kala["closed_at"], "kind": "close", "label": "Kalshi trading close"},
        {"at": iso(first), "kind": "report", "label": "First complete NWS report at 90°F"},
        {"at": poly["closed_at"], "kind": "close", "label": "Polymarket US scheduled trading close"},
        {"at": iso(correction), "kind": "report", "label": "Corrected NWS report at 85°F"},
    ]
    return {"series": series, "markers": sorted(markers, key=lambda row: row["at"])}


def evaluate(data, corpus):
    corpus["version"] = VERSION
    frozen, digest = research_model.freeze_inputs(data, corpus)
    first, correction = (parse_time(row["published_at"]) for row in corpus["weather"]["NWS"])
    cases = []
    for evidence in corpus["observations"]:
        kala, poly = evidence["kalshi"], evidence["polymarket"]
        stop = parse_time(poly["closed_at"])
        displays = evidence["polymarket_history"]
        summary = {"before_first_report": _read_section(displays, parse_time(POLY_WINDOWS[0][0]), first),
                   "after_first_report_before_scheduled_close": _read_section(displays, first, stop),
                   "after_scheduled_close_through_revision": _read_section(displays, stop, correction + timedelta(minutes=1)),
                   "after_corrected_report": _read_section(displays, correction + timedelta(minutes=1), parse_time(POLY_WINDOWS[-1][-1]) + timedelta(minutes=1))}
        observation = {**evidence, "report_clock_summary": summary}
        live_counterpart = summary["after_first_report_before_scheduled_close"]
        first_price = live_counterpart["distinct_prices_and_counts"][0]["yes_ask"] if len(live_counterpart["distinct_prices_and_counts"]) == 1 else None
        text = (f"The complete Miami report changed from 90°F at {first:%H:%M} UTC to 85°F at {correction:%H:%M} UTC. "
                f"Kalshi YES paid ${kala['yes_payout']}. Polymarket US YES paid ${poly['yes_payout']}. ")
        if first_price is not None:
            text += (f"Polymarket US history prints a constant {float(first_price)*100:g}¢ YES ask after the first report "
                     f"through its {stop:%H:%M} UTC scheduled close. ")
        text += "The retained displays carry no original book or available quantity."
        cases.append({"title": f"Miami {kala['label']} · report and close clocks", "date": kala["day"], "station": kala["station"],
                      "at": iso(correction), "venue": venue_archive.KALSHI, "instrument_id": kala["instrument_id"],
                      "related_venue": venue_archive.POLY, "related_instrument_id": poly["instrument_id"],
                      "venue_price_timeline": _timeline(observation, first, correction),
                      "detail": text, "data": {**observation, "source_reports": corpus["weather"]}})
    result = {"version": VERSION, "title": "Miami revision published between the two venues' trading and payout clocks",
              "hypothesis": "Align both venues' original terms, close times, archived displays and the two exact report issuances before attributing the payout disagreement to a tradeable window.",
              "target": "Two exact matched 2026-08-29 high thresholds at Miami International Airport", "venues": ["Kalshi", "Polymarket US"],
              "supersedes": EARLIER,
              "date_window": {"station day": "2026-08-29", "first complete report": iso(first), "corrected report": iso(correction)},
              "coverage": {"native verified payouts": 4, "paired thresholds with different payouts": 2, "independent station-days": 1,
                           "Polymarket US historical displayed minutes": sum(len(e["polymarket_history"]) for e in corpus["observations"]),
                           "Kalshi minute candles before close": sum(len(e["final_kalshi_minutes"]) for e in corpus["observations"]),
                           "Kalshi reported trades before close": sum(len(e["final_kalshi_native_trades"]) for e in corpus["observations"])},
              "metrics": [], "cases": cases,
              "limits": ["Kalshi market close and settlement publication are distinct. Polymarket US carries the catalog's scheduled close and a later verified payout receipt.",
                         "The provider displays the same prices before and after the observed reports and carries the marks beyond its scheduled close. Original quote time and executable size are unavailable.",
                         "The separately collected TWC report shows 90°F. Its original publication timestamp is unavailable."],
              "provenance": {"frozen_input": str(frozen.relative_to(data)), "frozen_input_sha256": digest,
                             "Polymarket US endpoint": venue_benchmark.POLY_PRICE_DOCS,
                             "Kalshi endpoint": "https://docs.kalshi.com/api-reference/historical/get-historical-market-candlesticks",
                             "source_sha256": research_model.run_sources(sys.modules[__name__], venue_warmseason_study)}}
    return research_model.write_record(data, result, {"recorded_reports": corpus["weather"], "paired_observations": cases})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--input", type=Path)
    parser.add_argument("--download", action="store_true")
    args = parser.parse_args(argv)
    if args.download:
        _download(args.data)
    corpus = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else gather(args.data)
    print(evaluate(args.data, corpus))


if __name__ == "__main__":
    main()
