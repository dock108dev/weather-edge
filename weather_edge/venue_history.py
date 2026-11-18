"""Backfill public payouts, display prices, candles, trades, and event metadata."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from itertools import batched
import json
from pathlib import Path
from urllib.parse import quote

from .market import iso, parse_time
from .venue_archive import (
    DEFAULT_DATA, KALSHI, POLY, connect, save_event, save_history, save_metadata, save_receipt,
    set_progress,
)
from .venue_contracts import native_id
from .venue_sources import (
    CollectionPaused, EXCEPTIONS, PublicGET, _known_series, _metadata_reply, _poly_payout,
    _rows, _scan_error, _scan_state, clock, recover_kalshi_aliases,
)

WEATHER_STUDY_FAMILIES = ("daily-high", "daily-low", "daily-rain")
CANDLE_KIND = "candle-60m"
DISPLAY_KIND = "display-price-1m"
MAX_KALSHI_CANDLES = 10_000


def _market_selection(data, venue, kind, start, end, limit, *, stations=None, families=None, payout=False):
    conditions = ["m.venue=?", "m.status='settled'", "m.day>=?", "m.day<=?"]
    values = [kind, venue, start, end]
    for column, selected in (("station", stations), ("family", families)):
        if selected:
            conditions.append("m." + column + " IN (" + ",".join("?" for _ in selected) + ")")
            values.extend(selected)
    if payout:
        conditions.append("m.yes_payout IS NULL")
    conditions.append("(p.state IS NULL OR p.state != 'complete')")
    if payout:
        # Let newly eligible symbols through even when an upstream 404 is persistent.
        conditions.append("(p.updated_at IS NULL OR p.updated_at<?)")
        values.append(iso(clock() - timedelta(hours=1)))
    with connect(data) as db:
        return [dict(row) for row in db.execute(
            """SELECT m.instrument_id,m.event_id,m.series_id,m.day,m.station,m.family,m.opened_at,
                m.closed_at,m.climate_starts_at,m.climate_ends_at,m.settlement_at,m.source
            FROM markets AS m LEFT JOIN progress AS p ON p.venue=m.venue AND p.scope=m.instrument_id AND p.kind=?
            WHERE """ + " AND ".join(conditions) + """
            ORDER BY m.day DESC,m.family,m.station,m.instrument_id LIMIT ?""", [*values, limit],
        )]


def _epochs(row, *, display=False):
    if display and row["climate_starts_at"] and row["climate_ends_at"]:
        start = parse_time(row["climate_starts_at"])
        end = parse_time(row["climate_ends_at"])
    else:
        start = parse_time(row["opened_at"])
        end = parse_time(row["closed_at"])
    if end <= start:
        raise ValueError("Nonpositive venue price-history interval")
    return int(start.timestamp()), int(end.timestamp())


def _at(value):
    if type(value) is not int:
        raise ValueError("Expected a UNIX-second timestamp")
    return iso(datetime.fromtimestamp(value, timezone.utc))


def _save_history(data, venue, instrument, kind, reply, parse, *, scope=None, progress_kind=None):
    scope = scope or instrument
    progress_kind = progress_kind or kind
    try:
        entries = parse(reply.json())
    except EXCEPTIONS as error:
        _scan_error(data, venue, scope, progress_kind, "", 0, 0, error, reply)
        return 0
    with connect(data) as db, db:
        receipt_id = save_receipt(db, venue, kind, reply)
        save_history(db, venue, instrument, kind, entries, receipt_id)
        set_progress(db, venue, scope, progress_kind, "", 1, len(entries), "complete", reply.received_at)
    return len(entries)


def _poly_price(data, get, market, *, window=None):
    instrument = market["instrument_id"]
    scope = instrument if window is None else f"{instrument}/{iso(window[0])}/{iso(window[1])}"
    progress_kind = DISPLAY_KIND if window is None else DISPLAY_KIND + "-window"
    try:
        start, end = (_epochs(market, display=True) if window is None
                      else (int(window[0].timestamp()), int(window[1].timestamp())))
        if start >= end:
            raise ValueError("Invalid display price history window")
        reply = get(POLY, "/v1/price-history", {
            "symbol": instrument, "timestamp.startTimestamp": start, "timestamp.endTimestamp": end, "fidelity": 1,
        })
    except EXCEPTIONS as error:
        _scan_error(data, POLY, scope, progress_kind, "", 0, 0, error)
        return 0

    def parse(payload):
        entries = []
        for item in _rows(payload, "history"):
            stamp = item["timestamp"]
            if start <= stamp <= end:
                entries.append((stamp, _at(stamp), item))
        return entries

    return _save_history(data, POLY, instrument, DISPLAY_KIND, reply, parse, scope=scope, progress_kind=progress_kind)


def _payout(data, get, market):
    instrument = market["instrument_id"]
    try:
        status = _poly_payout(data, get, instrument)
    except EXCEPTIONS as error:
        _scan_error(data, POLY, instrument, "settlement-check", "", 0, 0, error)
        return 0
    with connect(data) as db, db:
        state = "complete" if status == 200 else "error"
        set_progress(db, POLY, instrument, "settlement-check", "", 1, int(status == 200), state,
                     iso(clock()), None if status == 200 else f"HTTP {status}")
    return int(status == 200)


def _candle_entries(items, start, end):
    entries = []
    for item in items:
        stamp = item["end_period_ts"]
        if start <= stamp <= end:
            entries.append((stamp, _at(stamp), item))
    return entries


def _archive_partition(market, cutoff, kind):
    if kind == "market":
        # The row's actual retrieval route is stronger than testing wall clock.
        return "/historical/markets?" in (market["source"] or "") or (
            cutoff and market["settlement_at"] and parse_time(market["settlement_at"]) < parse_time(cutoff)
        )
    return bool(cutoff and parse_time(market["closed_at"]) <= parse_time(cutoff))


def _candle_limit(start, end):
    return (end - start) // 3600 + 2


def _candle_windows(start, end):
    while start <= end:
        finishes = min(end, start + (MAX_KALSHI_CANDLES - 2) * 3600)
        yield start, finishes
        start = finishes + 1


def _candle_chunks(data, instrument, start, end, take, *, partial):
    kind = CANDLE_KIND + "-window"
    scopes = []
    count = 0
    for left, right in _candle_windows(start, end):
        scope = f"{instrument}/{_at(left)}/{_at(right)}"
        scopes.append(scope)
        count += take(left, right, scope, kind)
    with connect(data) as db, db:
        placeholders = ",".join("?" for _ in scopes)
        completed = db.execute(
            f"SELECT count(*) FROM progress WHERE venue=? AND kind=? AND state='complete' AND scope IN ({placeholders})",
            (KALSHI, kind, *scopes),
        ).fetchone()[0]
        if completed == len(scopes):
            state, error = ("incomplete" if partial else "complete"), None
        else:
            state, error = "error", f"{completed}/{len(scopes)} hourly windows completed"
        set_progress(db, KALSHI, instrument, CANDLE_KIND, "", len(scopes), count, state, iso(clock()), error)
    return count


def _archive_candle_slice(data, get, instrument, start, end, scope, progress_kind):
    try:
        reply = get(KALSHI, "/historical/markets/" + quote(instrument, safe="") + "/candlesticks",
                    {"start_ts": start, "end_ts": end, "period_interval": 60})
    except EXCEPTIONS as error:
        _scan_error(data, KALSHI, scope, progress_kind, "", 0, 0, error)
        return 0

    def parse(payload):
        if payload["ticker"] != instrument:
            raise ValueError("Candlestick ticker mismatch")
        return _candle_entries(_rows(payload, "candlesticks"), start, end)

    return _save_history(data, KALSHI, instrument, CANDLE_KIND, reply, parse, scope=scope, progress_kind=progress_kind)


def _archive_candle(data, get, market):
    instrument = market["instrument_id"]
    try:
        start, end = _epochs(market)
    except EXCEPTIONS as error:
        _scan_error(data, KALSHI, instrument, CANDLE_KIND, "", 0, 0, error)
        return 0
    if _candle_limit(start, end) <= MAX_KALSHI_CANDLES:
        return _archive_candle_slice(data, get, instrument, start, end, instrument, CANDLE_KIND)
    return _candle_chunks(data, instrument, start, end,
                          lambda left, right, scope, kind: _archive_candle_slice(data, get, instrument, left, right, scope, kind),
                          partial=False)


def _live_candles(data, get, markets, *, through=None):
    windows = {}
    for market in markets:
        instrument = market["instrument_id"]
        try:
            start, end = _epochs(market)
        except EXCEPTIONS as error:
            _scan_error(data, KALSHI, instrument, CANDLE_KIND, "", 0, 0, error)
            continue
        stop = min(end, int(through.timestamp())) if through is not None else end
        if stop < start:
            with connect(data) as db, db:
                set_progress(db, KALSHI, instrument, CANDLE_KIND, "", 0, 0, "incomplete", iso(clock()), "No elapsed trading interval")
            continue
        windows[instrument] = start, stop
    return _live_candle_batches(data, get, windows, partial=through is not None)


def _live_candle_batches(data, get, windows, *, partial):
    if not windows:
        return 0
    start, end = min(value[0] for value in windows.values()), max(value[1] for value in windows.values())
    if len(windows) * _candle_limit(start, end) <= MAX_KALSHI_CANDLES:
        return _live_candle_batch(data, get, windows, partial=partial)
    if len(windows) == 1:
        instrument = next(iter(windows))
        return _candle_chunks(data, instrument, start, end,
                              lambda a, b, scope, kind: _live_candle_batch(data, get, {instrument: (a, b)},
                                                                          partial=False, scope=scope, kind=kind),
                              partial=partial)
    entries = list(windows.items())
    midpoint = len(entries) // 2
    return _live_candle_batches(data, get, dict(entries[:midpoint]), partial=partial) + _live_candle_batches(
        data, get, dict(entries[midpoint:]), partial=partial)


def _live_candle_batch(data, get, windows, *, partial, scope=None, kind=CANDLE_KIND):
    tickers = list(windows)
    try:
        start, end = min(x[0] for x in windows.values()), max(x[1] for x in windows.values())
        reply = get(KALSHI, "/markets/candlesticks", {
            "market_tickers": ",".join(tickers), "start_ts": start, "end_ts": end, "period_interval": 60,
        })
        returned = _rows(reply.json(), "markets")
        parsed = {}
        for item in returned:
            ticker = native_id(item["market_ticker"])
            if ticker not in windows or ticker in parsed:
                raise ValueError("Unexpected batch candlestick identity")
            parsed[ticker] = _candle_entries(_rows(item, "candlesticks"), *windows[ticker])
    except EXCEPTIONS as error:
        with connect(data) as db, db:
            if "reply" in locals():
                save_receipt(db, KALSHI, CANDLE_KIND, reply)
            for ticker in tickers:
                set_progress(db, KALSHI, scope or ticker, kind, "", 0, 0, "error", iso(clock()), str(error))
        return 0
    with connect(data) as db, db:
        receipt_id = save_receipt(db, KALSHI, CANDLE_KIND, reply)
        for ticker in tickers:
            if ticker in parsed:
                entries = parsed[ticker]
                save_history(db, KALSHI, ticker, CANDLE_KIND, entries, receipt_id)
                state = "incomplete" if partial else "complete"
                set_progress(db, KALSHI, scope or ticker, kind, "", 1, len(entries), state, reply.received_at)
            else:
                set_progress(db, KALSHI, scope or ticker, kind, "", 1, 0, "error", reply.received_at,
                             "Ticker absent from batch response")
    return sum(len(entries) for entries in parsed.values())


def _trades(data, get, market, *, cutoff, pages, window=None):
    instrument = market["instrument_id"]
    ranges = [("trade-live", "/markets/trades", False), ("trade-archive", "/historical/trades", True)]
    count = 0
    opened, closed = _epochs(market)
    scope = instrument
    if window is not None:
        if window[1] <= window[0]:
            raise ValueError("Invalid public trade history window")
        opened = max(opened, int(window[0].timestamp()))
        closed = min(closed, int(window[1].timestamp()) + 1)
        if opened > closed:
            return 0
        scope = f"{instrument}/{iso(window[0])}/{iso(window[1])}"
    cutoff_epoch = int(parse_time(cutoff).timestamp()) if cutoff else None
    for kind, path, archived in ranges:
        if cutoff_epoch and ((not archived and closed < cutoff_epoch) or (archived and opened >= cutoff_epoch)):
            continue
        progress_kind = kind + "-window" if window is not None else kind
        cursor, completed, previous_count, state = _scan_state(data, KALSHI, scope, progress_kind, "", True)
        if state == "complete":
            continue
        seen = {cursor}
        for _ in range(pages):
            reply = None
            try:
                params = {"ticker": instrument, "limit": 1000}
                if window is not None:
                    params.update(min_ts=opened, max_ts=closed)
                if cursor:
                    params["cursor"] = cursor
                reply = get(KALSHI, path, params)
                payload = reply.json()
                after = payload["cursor"]
                if not isinstance(after, str) or after and after in seen:
                    raise ValueError("Invalid or repeated trade cursor")
                entries = []
                for trade in _rows(payload, "trades"):
                    if trade["ticker"] != instrument:
                        raise ValueError("Trade ticker mismatch")
                    stamp = parse_time(trade["created_time"])
                    if window is None or window[0] <= stamp <= window[1]:
                        entries.append((native_id(trade["trade_id"]), iso(stamp), trade))
                with connect(data) as db, db:
                    receipt_id = save_receipt(db, KALSHI, kind, reply)
                    save_history(db, KALSHI, instrument, "trade", entries, receipt_id)
                    completed += 1
                    previous_count += len(entries)
                    set_progress(db, KALSHI, scope, progress_kind, after, completed, previous_count,
                                 "incomplete" if after else "complete", reply.received_at)
                count += len(entries)
                if not after:
                    break
                seen.add(after)
                cursor = after
            except EXCEPTIONS as error:
                _scan_error(data, KALSHI, scope, progress_kind, cursor, completed, previous_count, error, reply)
                break
    return count


def _kalshi_event_scan(data, get, series_id):
    kind = "events"
    cursor, pages, count, state = _scan_state(data, KALSHI, series_id, kind, "", True)
    if state == "complete":
        return
    seen = {cursor}
    while True:
        reply = None
        try:
            params = {"series_ticker": series_id, "limit": 200}
            if cursor:
                params["cursor"] = cursor
            reply = get(KALSHI, "/events", params)
            payload = reply.json()
            rows = _rows(payload, kind)
            after = payload["cursor"]
            if not isinstance(after, str) or after and after in seen:
                raise ValueError("Invalid or repeated event cursor")
            with connect(data) as db, db:
                receipt_id = save_receipt(db, KALSHI, kind, reply)
                for row in rows:
                    event_id = native_id(row["event_ticker"])
                    if row["series_ticker"] != series_id:
                        raise ValueError("Event series relationship mismatch")
                    save_event(db, KALSHI, event_id, series_id, row.get("title"), row, receipt_id)
                    db.execute(
                        "UPDATE markets SET event_title=? WHERE venue=? AND event_id=? AND event_title IS NULL",
                        (row.get("title"), KALSHI, event_id),
                    )
                pages += 1
                count += len(rows)
                set_progress(db, KALSHI, series_id, kind, after, pages, count,
                             "incomplete" if after else "complete", reply.received_at)
            if not after:
                return
            seen.add(after)
            cursor = after
        except EXCEPTIONS as error:
            _scan_error(data, KALSHI, series_id, kind, cursor, pages, count, error, reply)
            return


def event_inventory(data=DEFAULT_DATA, series_ids=None):
    get = PublicGET(data)
    for row in _known_series(data):
        ticker = row["ticker"]
        if not series_ids or ticker in series_ids:
            _kalshi_event_scan(data, get, ticker)
    recover_kalshi_aliases(data, series_ids)


def fee_history(data=DEFAULT_DATA):
    get = PublicGET(data)
    _metadata_reply(data, get, KALSHI, "/series/fee_changes", "fee-series", {"show_historical": "true"})
    cursor = ""
    seen = set()
    receipt_ids = []
    changes = []
    while True:
        params = {"limit": 1000}
        if cursor:
            params["cursor"] = cursor
        reply = get(KALSHI, "/events/fee_changes", params)
        with connect(data) as db, db:
            receipt_id = save_receipt(db, KALSHI, "fee-event", reply)
        payload = reply.json()
        changes.extend(_rows(payload, "event_fee_changes"))
        receipt_ids.append(receipt_id)
        cursor = payload["cursor"]
        if not cursor:
            with connect(data) as db, db:
                save_metadata(db, KALSHI, "fee-event-complete", {
                    "event_fee_changes": changes, "source_receipts": receipt_ids,
                }, receipt_id)
            return
        if not isinstance(cursor, str) or cursor in seen:
            raise ValueError("Invalid or repeated event fee cursor")
        seen.add(cursor)


def _cutoffs(data):
    with connect(data) as db:
        row = db.execute(
            "SELECT value FROM metadata WHERE venue=? AND key='historical-cutoff'", (KALSHI,),
        ).fetchone()
    return json.loads(row["value"]) if row else {}


def _map(operation, markets, workers):
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return sum(pool.map(operation, markets))


def backfill(data, action, start, end, limit, *, stations=None, families=None, pages=1, workers=3):
    get = PublicGET(data)
    if action == "payouts":
        selection = _market_selection(data, POLY, "settlement-check", start, end, limit, stations=stations,
                                      families=families, payout=True)
        return len(selection), _map(lambda row: _payout(data, get, row), selection, workers)
    if action == "display":
        selection = _market_selection(data, POLY, DISPLAY_KIND, start, end, limit, stations=stations, families=families)
        return len(selection), _map(lambda row: _poly_price(data, get, row), selection, workers)
    cutoffs = _cutoffs(data)
    if action == "candles":
        selection = _market_selection(data, KALSHI, CANDLE_KIND, start, end, limit, stations=stations, families=families)
        archived, live = [], {}
        for row in selection:
            if _archive_partition(row, cutoffs.get("market_settled_ts"), "market"):
                archived.append(row)
            else:
                live.setdefault(row["day"], []).append(row)
        batches = [batch for rows in live.values() for batch in batched(rows, 40)]
        total = _map(lambda batch: _live_candles(data, get, batch), batches, workers)
        return len(selection), total + _map(lambda row: _archive_candle(data, get, row), archived, workers)
    selection = _market_selection(data, KALSHI, "trade-complete", start, end, limit * 6,
                                  stations=stations, families=families)
    sample = {}
    for row in selection:
        sample.setdefault(row["event_id"], row)
    targets = list(sample.values())[:limit]
    return len(targets), _map(
        lambda row: _trades(data, get, row, cutoff=cutoffs.get("trades_created_ts"), pages=pages), targets, workers,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    subs = parser.add_subparsers(dest="command", required=True)
    for name in ("payouts", "display", "candles", "trades"):
        action = subs.add_parser(name)
        action.add_argument("--start", default="2026-11-01")
        action.add_argument("--end", default=clock().date().isoformat())
        action.add_argument("--limit", type=int, default=200)
        action.add_argument("--station", action="append", dest="stations")
        action.add_argument("--family", action="append", dest="families")
        action.add_argument("--pages", type=int, default=1)
        action.add_argument("--workers", type=int, default=3)
    events = subs.add_parser("events")
    events.add_argument("--series", action="append")
    subs.add_parser("fees")
    args = parser.parse_args(argv)
    try:
        if args.command == "events":
            event_inventory(args.data, args.series)
        elif args.command == "fees":
            fee_history(args.data)
        else:
            targets, added = backfill(
                args.data, args.command, args.start, args.end, args.limit, stations=args.stations,
                families=args.families, pages=args.pages, workers=args.workers,
            )
            print(f"{args.command}: {targets} selected instruments; {added} returned evidence records")
    except CollectionPaused:
        print("Collection is stopped. Saved receipts and cursors are available.")


if __name__ == "__main__":
    main()
