"""Compare matched native minute marks around the same exact colder station report."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import timedelta
from decimal import Decimal, InvalidOperation
import gzip
import json
from pathlib import Path
from statistics import mean
import sys
from urllib.parse import quote

from . import research_model, research_model_low_receipt_prices as study, research_model_venue
from . import strategy_forecast_innovation as quotes, venue_benchmark, venue_history
from .collector import paused
from .market import iso, parse_time
from .model import _outcome
from .venue_archive import KALSHI, POLY, connect
from .venue_sources import CollectionPaused, EXCEPTIONS, PublicGET

VERSION = "daily-low-two-venue-original-receipts-v1"
KIND = "candle-1m"
SCOPE = KIND + "-original-receipt-window"
DOC = "https://docs.kalshi.com/api-reference/market/get-market-candlesticks"
ARCHIVED_DOC = "https://docs.kalshi.com/api-reference/historical/get-historical-market-candlesticks"


def _native_pairs(data, report_rows):
    cache, pairs = {}, []
    missing = Counter()
    with connect(data) as db:
        for row in report_rows:
            station, day, at = row["station"], row["date"], parse_time(row["at"])
            poly = row["market"]
            if not row["before"] or abs(row["signal"]) < study.MIN_MOVE:
                continue
            if (station, day) not in cache:
                cache[station, day] = [dict(item) for item in db.execute("""SELECT instrument_id, series_id, station, day,
                    rules_primary, rules_secondary, resolution_source, rules_url, operator, lower, upper, expiration_value,
                    opened_at, closed_at, yes_payout, settlement_at, settlement_receipt_id, source FROM markets
                    WHERE venue=? AND family='daily-low' AND station=? AND day=? AND status='settled'
                      AND yes_payout IN ('0','1') AND settlement_receipt_id IS NOT NULL""", (KALSHI, station, day))]
            target = poly["lower_f"], poly["upper_f"]
            choices = [item for item in cache[station, day] if research_model_venue.integer_bucket(item) == target]
            if len(choices) != 1:
                missing["no unique identical integer strike"] += 1
                continue
            kalshi = choices[0]
            if not kalshi["opened_at"] or not kalshi["closed_at"] or not parse_time(kalshi["opened_at"]) < at < parse_time(kalshi["closed_at"]):
                missing["matching market not open at receipt"] += 1
                continue
            actual = row["weather"]["official_report"]["low_f"]
            try:
                native = Decimal(kalshi["expiration_value"])
            except (InvalidOperation, TypeError, ValueError):
                missing["no native numerical ruling"] += 1
                continue
            payout = int(kalshi["yes_payout"])
            if native != actual or payout != _outcome({"lower_f": target[0], "upper_f": target[1]}, actual) or payout != row["outcome"]:
                missing["native labels or binary payouts disagree"] += 1
                continue
            pairs.append({**row, "kalshi": kalshi})
    return pairs, missing


def _intervals(pairs, *, padding=None):
    windows = {}
    padding = padding if padding is not None else timedelta(minutes=max(study.HORIZONS)) + study.AGE + quotes.POINT_LAG
    for row in pairs:
        market = row["kalshi"]
        ticker = market["instrument_id"]
        when = parse_time(row["at"])
        start, end = when - padding, when + padding
        if ticker in windows:
            _, left, right = windows[ticker]
            start, end = min(start, left), max(end, right)
        windows[ticker] = market, start, end
    return windows


def _acquire(data, pairs, *, padding=None, scope_kind=SCOPE):
    windows = _intervals(pairs, padding=padding)
    get = PublicGET(data)
    omissions = Counter()
    cutoff = venue_history._cutoffs(data).get("market_settled_ts")
    for ticker, (market, start, end) in windows.items():
        if paused(data):
            omissions["collection paused"] += 1
            break
        scope = f"{ticker}/{iso(start)}/{iso(end)}"
        if venue_history._scan_state(data, KALSHI, scope, scope_kind, "", True)[-1] == "complete":
            continue
        if venue_history._archive_partition(market, cutoff, "market"):
            route = f"/historical/markets/{quote(ticker, safe='')}/candlesticks"
        else:
            route = f"/series/{quote(market['series_id'], safe='')}/markets/{quote(ticker, safe='')}/candlesticks"
        left, right = int(start.timestamp()), int(end.timestamp())
        try:
            response = get(KALSHI, route, {"start_ts": left, "end_ts": right, "period_interval": 1})
        except CollectionPaused:
            omissions["collection paused"] += 1
            break
        except EXCEPTIONS as error:
            venue_history._scan_error(data, KALSHI, scope, scope_kind, "", 0, 0, error)
            omissions[type(error).__name__] += 1
            continue

        def parse(payload):
            if payload["ticker"] != ticker:
                raise ValueError("Kalshi candle ticker disagrees with request")
            return venue_history._candle_entries(venue_history._rows(payload, "candlesticks"), left, right)

        stored = venue_history._save_history(data, KALSHI, ticker, KIND, response, parse, scope=scope, progress_kind=scope_kind)
        if not stored:
            omissions[f"HTTP {response.status}, zero retained records"] += 1
    return {"instruments": len(windows), **omissions}


def _price_history(db, venue, market, start, end):
    points = {}
    for row in db.execute("""SELECT h.at,h.receipt_id,h.body,r.source,r.received_at,r.digest FROM history AS h
                             JOIN receipts AS r ON r.id=h.receipt_id
                             WHERE h.venue=? AND h.instrument_id=? AND h.kind=? AND h.at BETWEEN ? AND ? ORDER BY h.at""",
                          (venue, market["instrument_id"], KIND if venue == KALSHI else venue_history.DISPLAY_KIND, iso(start), iso(end))):
        raw, at = json.loads(row["body"]), parse_time(row["at"])
        mark = venue_benchmark._probability_proxy(raw, venue)
        if mark and quotes._price_entry(venue, at, row["receipt_id"], raw):
            ready = at + quotes.POINT_LAG
            points[ready] = {**mark, "at": row["at"], "available_at_under_lag": iso(ready), "receipt_id": row["receipt_id"],
                             "received_at": row["received_at"], "source": row["source"], "body_sha256": row["digest"]}
    return list(points), points


def _window(trace, opened, closed, when, minutes):
    times, points = trace
    span = timedelta(minutes=minutes)
    if not opened < when - span < when + span < closed:
        return None
    preceding = quotes.latest(times, points, when - span, study.AGE)
    before = quotes.latest(times, points, when - timedelta(microseconds=1), study.AGE)
    after = quotes.latest(times, points, when + span, study.AGE)
    if not preceding or not before or not after or not opened <= parse_time(preceding["at"]) <= parse_time(before["at"]) < when < parse_time(after["at"]):
        return None
    return {"preceding": preceding, "before": before, "after": after,
            "prior_move": before["probability"] - preceding["probability"],
            "after_move": after["probability"] - before["probability"]}


def _matched_windows(db, pair):
    when = parse_time(pair["at"])
    padding = timedelta(minutes=max(study.HORIZONS)) + study.AGE + quotes.POINT_LAG
    markets = {POLY: pair["market"], KALSHI: pair["kalshi"]}
    traces = {venue: _price_history(db, venue, market, when - padding, when + padding) for venue, market in markets.items()}
    matching = []
    for covered in pair["movement"]:
        minutes = covered["minutes"]
        windows = {venue: _window(traces[venue], parse_time(market["opened_at"]), parse_time(market["closed_at"]), when, minutes)
                   for venue, market in markets.items()}
        if all(windows.values()):
            matching.append({"minutes": minutes, "windows": windows})
    return matching, {venue: len(times) for venue, (times, _) in traces.items()}


def gather(data, reference, *, fetch=False):
    record = json.loads(reference.read_bytes())
    if record["version"] != study.VERSION:
        raise ValueError("Expected the exact colder-report Polymarket price study")
    details = data / record["provenance"]["replay_details_path"]
    if research_model._digest(details) != record["provenance"]["replay_details_sha256"]:
        raise ValueError("Previously retained exact Polymarket history digest disagrees")
    rows = json.loads(gzip.decompress(details.read_bytes()))["verified_native_buckets"]
    pairs, missing = _native_pairs(data, rows)
    acquired = _acquire(data, pairs) if fetch else {"instruments": len(_intervals(pairs))}
    with connect(data) as db:
        for row in pairs:
            row["windows"], row["strict_mark_counts"] = _matched_windows(db, row)
    return {"version": VERSION, "date_window": record["date_window"], "pairs": pairs, "gaps": dict(missing),
            "acquisition": acquired, "source": research_model._source_entry(data, reference)}


def _aligned(row, venue, field):
    direction = 1 if row["signal"] > 0 else -1
    return direction * row["windows"][venue][field]


def _case(row, venue):
    market = row["kalshi"] if venue == KALSHI else row["market"]
    selected = row["windows"][venue]
    alt_venue = POLY if venue == KALSHI else KALSHI
    alt = row["windows"][alt_venue]
    return {"station": row["station"], "date": row["date"], "at": row["at"], "venue": venue,
            "instrument_id": market["instrument_id"], "title": f"{row['station']} · Matching daily-low strike at both venues",
            "detail": (f"The lower station report moved the weather estimate {row['signal'] * 100:+.1f} points. "
                       f"Over the next {row['minutes']} minutes, strict display marks moved {selected['after_move'] * 100:+.1f} "
                       f"points here and {alt['after_move'] * 100:+.1f} points at the other venue. "
                       f"Both contracts paid ${row['outcome']} on YES."), "evidence": row}


def evaluate(data, inputs):
    if inputs["version"] != VERSION:
        raise ValueError("Unexpected two-venue low-receipt input version")
    frozen, digest = research_model.freeze_inputs(data, inputs)
    pairs = inputs["pairs"]
    metrics, contrasts, examples, support = [], [], {}, {}
    for minutes in study.HORIZONS:
        rows = [{**pair, **window} for pair in pairs for window in pair["windows"] if window["minutes"] == minutes]
        for title, cohort in (("Largest weather move", [row for row in rows if row["qualifying_signal"]]),
                              ("Five-point or larger matched weather moves", rows)):
            group = f"{title} · {minutes}-minute windows"
            support[group] = research_model._independent_counts(cohort)
            if not cohort:
                continue
            for venue in (POLY, KALSHI):
                for field in ("prior_move", "after_move"):
                    metrics.append({"group": group, "venue": venue, "method": field, "label": f"{venue} {field.replace('_', ' ')}",
                                    **support[group], "mean_aligned_move_cents": 100 * mean(_aligned(row, venue, field) for row in cohort)})
                if support[group]["calendar_dates"] > 1:
                    contrast = study._interval(cohort, lambda row: _aligned(row, venue, "after_move") - _aligned(row, venue, "prior_move"))
                    contrasts.append({"group": group, "venue": venue, "method": "after_move", "baseline": "prior_move", **contrast})
            if title == "Largest weather move" or not examples:
                strongest = max(cohort, key=lambda row: abs(row["signal"]))
                for venue in (POLY, KALSHI):
                    case = _case(strongest, venue)
                    examples[venue, case["instrument_id"], case["at"]] = case
    record = {"version": VERSION, "title": "Two venues after the same colder station report",
              "hypothesis": "Matching exact integer strikes and original station receipt clocks reveals whether both displays move with the same weather update.",
              "target": "Polymarket US and Kalshi native daily-low payout-equivalent strikes, same station and climate day",
              "date_window": inputs["date_window"], "venues": [POLY, KALSHI], "metrics": metrics, "cases": list(examples.values()),
              "coverage": {"matched_native_signals": len(pairs), "acquisition": inputs["acquisition"], "matched_price_windows": support},
              "missing": inputs["gaps"], "uncertainty": contrasts,
              "limits": ["Identical integer endpoints and each venue's archived payout are required. Polymarket US uses the daily NWS report. The matching Kalshi numerical ruling must independently agree with the complete raw NWS low for this sample.",
                         "Both venues need two-sided display marks strictly inside 0 to 100 cents, at most 30 cents wide, at the same three trace clocks. This excludes markets whose history goes one-sided or becomes stale.",
                         "Each completed native minute mark carries a one-minute availability allowance and a two-minute maximum age. The original historical book receipt and executable size are unknown.",
                         "The original report and paired weather controls come from the prior single-venue study. Intervals with other qualifying lower receipts or scheduled model changes are already excluded. A date bootstrap on this small matching sample is descriptive."],
              "provenance": {"frozen_input": str(frozen.relative_to(data)), "frozen_input_sha256": digest,
                             "earlier_study": inputs["source"], "candlestick_documentation": [DOC, ARCHIVED_DOC],
                             "source_sha256": research_model.run_sources(sys.modules[__name__], study, venue_history, venue_benchmark)}}
    return research_model.write_record(data, record, {"paired_price_windows": pairs})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--receipt-price-study", type=Path)
    source.add_argument("--input", type=Path)
    parser.add_argument("--fetch", action="store_true", help="Collect only the bounded matching native Kalshi minute windows")
    args = parser.parse_args()
    if args.fetch and args.input:
        parser.error("Native collection requires --receipt-price-study")
    inputs = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else gather(args.data, args.receipt_price_study, fetch=args.fetch)
    print(evaluate(args.data, inputs))


if __name__ == "__main__":
    main()
