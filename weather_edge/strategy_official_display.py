"""Compare first complete official temperature reports against native display prices."""

import argparse
from collections import Counter, defaultdict
from datetime import timedelta
from functools import cache
from hashlib import sha256
import json
from pathlib import Path
from statistics import mean, median

from . import collector, research_model, strategy_payout_timing, venue_archive, venue_clock_study, venue_history, venue_sources
from .market import iso, parse_time
from .model import _limits, _outcome
from .strategy_forecast_innovation import _price_entry
from .strategy_kalshi_history import candle_points, snapshot_at
from .strategy_market import normalized_prices
from .strategy_poly_anchor import asof_price
from .strategy_rules import temperature_window
from .study import _partition


VERSION = "first-official-temperature-display-v1"
DATES = {"earlier": ("2026-10-01", "2026-10-21"), "later test": ("2026-10-26", "2026-11-14")}
MINUTES = (-5, 5, 30, 60)
TEMP = ("daily-high", "daily-low")
FLOOR = 0.05
REPORT_DELAY = timedelta(minutes=1)


def normalized_event(row):
    with_bounds = []
    for b in row["buckets"]:
        bounds = temperature_window(b)
        if bounds is None or not b["opened_at"] or not b["closed_at"]:
            return None
        with_bounds.append({**b, "lower_f": bounds[0], "upper_f": bounds[1], "payout": int(b["yes_payout"])})
    if _partition(with_bounds) != "exhaustive":
        return None
    first = row["first_complete_cli"]["amount"]
    last = row["latest_local_cli"]["amount"]
    return {**row, "buckets": [{**b, "first_report_yes": _outcome(b, first), "final_retained_report_yes": _outcome(b, last)}
                               for b in sorted(with_bounds, key=_limits)]}


def known_settlement(row, lock):
    native = row["native_final_settlement_at"]
    clearing = (row["clearing_publication"] or {}).get("at")
    evidence = [native, clearing, row["all_public_payouts_received_at"]]
    return any(t and parse_time(t) < lock for t in evidence)


def says_wrong(row, source):
    return any(b[source] != b["payout"] for b in row["buckets"])


def score(events, key):
    days = defaultdict(list)
    for row in events:
        errors = [(b["payout"] - b[key]) ** 2 for b in row["buckets"]]
        days[row["date"]].append(mean(errors))
    return mean(mean(values) for values in days.values()) if days else None


def price_reader(data, db, *, kalshi_minutes=False):
    @cache
    def candles(ident, kind):
        points = candle_points(data, ident, kind=kind)
        return sorted(points), points

    def get(venue, ident, at, *, hourly=False):
        if venue == venue_archive.POLY:
            return asof_price(db, ident, at)
        if kalshi_minutes and not hourly:
            kind = venue_clock_study.MINUTE_CANDLE_KIND
            clocks, values = candles(ident, kind)
            mark = snapshot_at(clocks, values, at)
            if mark and at - parse_time(mark["at"]) <= timedelta(minutes=2):
                return {**mark, "bar_interval_minutes": 1}
        clocks, values = candles(ident, venue_history.CANDLE_KIND)
        found = snapshot_at(clocks, values, at)
        return {**found, "bar_interval_minutes": 60} if found else None

    return get


def priced_frame(row, horizon, get_price):
    original = parse_time(row["first_complete_cli"]["at"])
    at = original + timedelta(minutes=horizon)
    eligible = all(parse_time(b["opened_at"]) <= at < parse_time(b["closed_at"]) for b in row["buckets"])
    if not eligible:
        return None, "outside common native trading window"
    if row["first_native_settlement_at"] and parse_time(row["first_native_settlement_at"]) <= at:
        return None, "native payout had already begun"
    new = [get_price(row["venue"], b["instrument_id"], at) for b in row["buckets"]]
    if any(mark is None for mark in new):
        return None, "at least one instrument lacks contemporaneous display"
    if row["venue"] == venue_archive.KALSHI and len({mark["bar_interval_minutes"] for mark in new}) > 1:
        new = [get_price(row["venue"], b["instrument_id"], at, hourly=True) for b in row["buckets"]]
        if any(mark is None for mark in new):
            return None, "venue frame mixed minute and hourly bars"
    if horizon > 0 and any(parse_time(mark["at"]) <= original for mark in new):
        return None, "venue frame still includes pre-report marks"
    if any(parse_time(mark["at"]) < parse_time(b["opened_at"]) for b, mark in zip(row["buckets"], new, strict=True)):
        return None, "venue mark predates native open"
    mids = normalized_prices([mark.get("midpoint", mark.get("mid")) for mark in new])
    if not mids:
        return None, "full midpoint vector has incoherent mass"
    reports = [version for version in row["raw_cli_versions"] if parse_time(version["at"]) + REPORT_DELAY <= at]
    current = reports[-1] if reports else None
    estimates = [{**b, "market_midpoint": p, "market_quote": quote,
                  "available_report_yes": _outcome(b, current["amount"]) if current else None}
                 for b, p, quote in zip(row["buckets"], mids, new, strict=True)]
    return {**row, "buckets": estimates, "at": iso(at), "minutes_after_report": horizon, "available_report": current}, None


def panel_metrics(panel, prior, phase, horizon, denominator):
    later = horizon > 0
    paired_before = {row["event_id"]: row for row in prior}
    moves = []
    differences = []
    for row in panel:
        report_favorite = next(b for b in row["buckets"] if b["first_report_yes"])
        if later:
            latest_favorite = next(b for b in row["buckets"] if b["available_report_yes"])
            differences.append(1 - latest_favorite["market_midpoint"])
        before = paired_before.get(row["event_id"])
        if later and before:
            mark = next(b for b in before["buckets"] if b["instrument_id"] == report_favorite["instrument_id"])
            moves.append(report_favorite["market_midpoint"] - mark["market_midpoint"])
    return {"window": phase, "minutes_after_original_report": horizon, "verified_report_events": denominator,
            "priced_events": len(panel), "calendar_dates": len({row["date"] for row in panel}),
            "station_days": len({(row["station"], row["date"]) for row in panel}),
            "native_buckets": sum(len(row["buckets"]) for row in panel),
            "priced_events_with_native_minute_candles": sum(row["buckets"][0]["market_quote"].get("bar_interval_minutes") == 1
                                                            for row in panel),
            "venue_midpoint_brier": score(panel, "market_midpoint"),
            "available_report_brier": score(panel, "available_report_yes") if later else None,
            "first_retained_report_wrong_events": sum(says_wrong(row, "first_report_yes") for row in panel),
            "paired_before_and_after_events": len(moves), "paired_median_report_side_move_cents": median(moves) * 100 if moves else None,
            "first_report_side_rose": sum(delta > 0 for delta in moves), "first_report_side_fell": sum(delta < 0 for delta in moves),
            "available_report_side_gt_5cent_disagreements": sum(v > FLOOR for v in differences),
            "simulated_fills": 0}


def favorite_marks(events, horizon, get_price):
    """Price the report-selected instrument even when other buckets lack a vector."""
    marked = []
    for row in events:
        original = parse_time(row["first_complete_cli"]["at"])
        at = original + timedelta(minutes=horizon)
        versions = [version for version in row["raw_cli_versions"] if parse_time(version["at"]) + REPORT_DELAY <= at]
        report = versions[-1] if versions else row["first_complete_cli"]
        favored, = [b for b in row["buckets"] if _outcome(b, report["amount"])]
        opened, closed = (parse_time(favored[key]) for key in ("opened_at", "closed_at"))
        if not opened <= at < closed:
            continue
        if row["first_native_settlement_at"] and parse_time(row["first_native_settlement_at"]) <= at:
            continue
        mark = get_price(row["venue"], favored["instrument_id"], at)
        if not mark or parse_time(mark["at"]) < opened or horizon > 0 and parse_time(mark["at"]) <= original:
            continue
        marked.append({"event_id": row["event_id"], "station": row["station"], "date": row["date"], "at": iso(at),
                       "market": favored, "midpoint": mark.get("midpoint", mark.get("mid")), "native_mark": mark, "report": report})
    return marked


def favorite_metrics(rows, prior, phase, venue, family, horizon):
    labeled = {(row["event_id"], row["market"]["instrument_id"]): row for row in prior}
    moves = [row["midpoint"] - old["midpoint"] for row in rows
             if horizon > 0 and (old := labeled.get((row["event_id"], row["market"]["instrument_id"])))]
    return {"window": phase, "venue": venue, "family": family, "minutes_after_original_report": horizon,
            "priced_report_selected_instruments": len(rows), "calendar_dates": len({row["date"] for row in rows}),
            "instruments_with_minute_candle": sum(row["native_mark"].get("bar_interval_minutes") == 1 for row in rows),
            "paired_opportunities_with_pre_report_marks": len(moves),
            "median_report_selected_quote_move_cents": median(moves) * 100 if moves else None,
            "report_selected_instruments_that_paid_yes": sum(row["market"]["payout"] for row in rows)}


def continuous_poly_marks(db, events):
    samples = []
    for row in events:
        original = parse_time(row["first_complete_cli"]["at"])
        first, last = iso(original - timedelta(minutes=6)), iso(original + timedelta(minutes=59))
        for b in row["buckets"]:
            archive = db.execute("""SELECT at,receipt_id,body FROM history WHERE venue=? AND instrument_id=?
                                     AND kind='display-price-1m' AND at BETWEEN ? AND ? ORDER BY at""",
                                 (venue_archive.POLY, b["instrument_id"], first, last)).fetchall()
            timeline = [mark for item in archive
                        if (mark := _price_entry(venue_archive.POLY, parse_time(item["at"]), item["receipt_id"], json.loads(item["body"])))]
            distinct = {(mark["yes_bid"], mark["yes_ask"]) for mark in timeline}
            samples.append({"station": row["station"], "date": row["date"], "event_id": row["event_id"], "family": row["family"],
                            "instrument_id": b["instrument_id"], "price_changed": len(distinct) > 1, "marks": timeline})
    totals = {"events": len(events), "instrument_series": len(samples), "priced_minute_marks": sum(len(s["marks"]) for s in samples),
              "series_with_displayed_price_change": sum(s["price_changed"] for s in samples),
              "events_with_displayed_price_change": len({s["event_id"] for s in samples if s["price_changed"]})}
    return totals, samples


def cases(panel, *, limit=8):
    entries = []
    used = set()
    for row in sorted(panel, key=lambda row: (says_wrong(row, "available_report_yes"),
                                             max(abs(b["available_report_yes"] - b["market_midpoint"])
                                                 for b in row["buckets"])), reverse=True):
        ident = row["venue"], row["event_id"]
        if ident in used:
            continue
        used.add(ident)
        first = row["first_complete_cli"]
        current = row["available_report"]
        best = next(b for b in row["buckets"] if b["available_report_yes"])
        entries.append({"station": row["station"], "date": row["date"], "venue": row["venue"], "event_id": row["event_id"],
                        "instrument_id": best["instrument_id"], "at": row["at"],
                        "title": f"{row['station']} public report {row['family'].removeprefix('daily-')}",
                        "detail": f"First report {first['amount']:.0f}°F at {first['at']}. Available report {current['amount']:.0f}°F, midpoint {best['market_midpoint']:.1%}, verified YES ${best['payout']}.",
                        "data": row})
        if len(entries) == limit:
            break
    return entries


def spread_dates(events, limit):
    dates = defaultdict(list)
    for row in events:
        dates[row["date"]].append(row)
    if not dates:
        return []
    return [by_day[i] for i in range(max(map(len, dates.values()))) for by_day in dates.values() if i < len(by_day)][:limit]


def fetch_display_windows(data, pool, limit):
    with venue_archive.connect(data) as db:
        known = {r["scope"]: r["rows"] for r in db.execute(
            "SELECT scope,rows FROM progress WHERE venue=? AND kind=? AND state='complete'",
            (venue_archive.POLY, venue_history.DISPLAY_KIND + "-window"))}
    fetch = venue_sources.PublicGET(data)
    result = Counter()
    for (phase, venue, family), events in sorted(pool.items()):
        if venue != venue_archive.POLY:
            continue
        for row in spread_dates(events, limit):
            report = parse_time(row["first_complete_cli"]["at"])
            for bucket in row["buckets"]:
                earliest = max(parse_time(bucket["opened_at"]), report - timedelta(minutes=7))
                end = min(parse_time(bucket["closed_at"]), report + timedelta(minutes=60))
                kind = phase, family
                if earliest >= end:
                    result[(*kind, "invalid window")] += 1
                    continue
                scope = f"{bucket['instrument_id']}/{iso(earliest)}/{iso(end)}"
                if scope in known:
                    result[(*kind, "windows previously complete")] += 1
                    continue
                if collector.paused(data):
                    result[(*kind, "collection paused")] += 1
                    return result
                points = venue_history._poly_price(data, fetch, bucket, window=(earliest, end))
                result[(*kind, "windows requested")] += 1
                result[(*kind, "reported minute observations")] += points
    return result


def fetch_kalshi_windows(data, pool, limit):
    with venue_archive.connect(data) as db:
        known = {r["scope"] for r in db.execute("SELECT scope FROM progress WHERE venue=? AND kind=? AND state='complete'",
                                                (venue_archive.KALSHI, venue_clock_study.MINUTE_CANDLE_KIND + "-window"))}
    fetch = venue_sources.PublicGET(data)
    result = Counter()
    for (phase, venue, family), events in sorted(pool.items()):
        if venue != venue_archive.KALSHI:
            continue
        eligible = []
        for row in events:
            first = parse_time(row["first_complete_cli"]["at"])
            close = min(parse_time(b["closed_at"]) for b in row["buckets"])
            if first + timedelta(minutes=5) < close:
                minutes_open = int((close - first).total_seconds() / 60)
                eligible.append({**row, "minutes_open": minutes_open})
        for row in spread_dates(eligible, limit):
            if collector.paused(data):
                result[phase, family, "collection paused"] += 1
                return result
            stamp = parse_time(row["first_complete_cli"]["at"])
            prefix = f"first-official-temperature/{row['event_id']}/"
            if f"{prefix}{iso(stamp)}#1" in known:
                result[phase, family, "groups previously complete"] += 1
                continue
            tickers = [b["instrument_id"] for b in row["buckets"]]
            n = venue_clock_study._batch(data, fetch, stamp, tickers, 1, before=7,
                                         after=min(60, row["minutes_open"]), prefix=prefix)
            result[phase, family, "groups requested"] += 1
            result[phase, family, "reported minute candles"] += n
    return result


def research(data, *, fetch_poly=0, fetch_kalshi=0, kalshi_minutes=False):
    audit, raw_gaps = strategy_payout_timing.assemble(data)
    pool = defaultdict(list)
    rejected = Counter()
    for row in audit:
        if row["family"] not in TEMP:
            continue
        phase = next((name for name, bounds in DATES.items() if bounds[0] <= row["date"] <= bounds[1]), None)
        if phase is None:
            continue
        if exact := normalized_event(row):
            pool[phase, row["venue"], row["family"]].append(exact)
        else:
            rejected[phase, row["venue"], row["family"], "native open clock or exact integer partition unavailable"] += 1
    acquisition = fetch_display_windows(data, pool, fetch_poly) if fetch_poly else Counter()
    kalshi_acquisition = fetch_kalshi_windows(data, pool, fetch_kalshi) if fetch_kalshi else Counter()
    metrics, later, all_panels, report_outcomes, favored_metrics, favored_details = [], [], {}, [], [], {}
    continuous_metrics, continuous_details = [], {}
    test_lock = parse_time(DATES["later test"][0] + "T00:00:00Z")
    with venue_archive.connect(data) as db:
        db.execute("BEGIN")
        get_price = price_reader(data, db, kalshi_minutes=bool(kalshi_minutes or fetch_kalshi))
        for (phase, venue, family), events in sorted(pool.items()):
            panel, justified = {}, [r for r in events if known_settlement(r, test_lock)] if phase == "earlier" else events
            favorites = {}
            report_outcomes.append({"window": phase, "venue": venue, "family": family, "verified_report_events": len(events),
                                    "native_label_known_by_earlier_lock": len(justified) if phase == "earlier" else None,
                                    "calendar_dates": len({e["date"] for e in events}),
                                    "original_report_did_not_match_payout": sum(says_wrong(e, "first_report_yes") for e in events),
                                    "final_retained_report_did_not_match_payout": sum(says_wrong(e, "final_retained_report_yes") for e in events),
                                    "at_least_one_report_revision": sum(len(e["raw_cli_versions"]) > 1 for e in events)})
            for horizon in MINUTES:
                favorites[horizon] = favorite_marks(events, horizon, get_price)
                favored_metrics.append(favorite_metrics(favorites[horizon], favorites[MINUTES[0]], phase, venue, family, horizon))
                panel[horizon] = []
                for row in events:
                    priced, gap = priced_frame(row, horizon, get_price)
                    if priced:
                        panel[horizon].append(priced)
                    else:
                        rejected[phase, venue, family, f"{horizon} min", gap] += 1
                stats = panel_metrics(panel[horizon], panel[MINUTES[0]], phase, horizon, len(events))
                metrics.append({**stats, "venue": venue, "family": family})
                if phase == "later test" and horizon > 0:
                    later.extend(panel[horizon])
            all_panels["/".join((phase, venue, family))] = panel
            favored_details["/".join((phase, venue, family))] = favorites
            if venue == venue_archive.POLY and phase == "later test":
                totals, retained = continuous_poly_marks(db, panel[60])
                continuous_metrics.append({"venue": venue, "family": family, **totals})
                continuous_details[family] = retained
    coverage = [{"category": " / ".join(key), "events": value} for key, value in sorted(rejected.items())]
    report = {"version": VERSION, "title": "How did prices reflect the first complete temperature bulletin?",
              "hypothesis": "A complete first daily climate bulletin can add information even when venue display marks were already near the eventual payout.",
              "target": "Original exact-station high and low bulletins within native trading windows, coherent market displays, and verified payouts.",
              "venues": list(venue_archive.VENUES), "date_window": {**DATES, "from": DATES["earlier"][0], "through": DATES["later test"][1]},
              "metrics": metrics, "cases": cases(later), "official_report_matches": report_outcomes, "price_coverage_gaps": coverage,
              "report_selected_instrument_metrics": favored_metrics,
              "continuous_later_polymarket_mark_metrics": continuous_metrics,
              "limits": ["The earliest retained archived complete edition may follow an earlier unretained public report.",
                         "One minute after the documented issue is used as a historical access assumption. Native display marks are not order books.",
                         "An after-report price requires every component's documented mark time after publication and a common native trading window.",
                         "A 5¢ display discrepancy ignores fees and is only descriptive. No fills, cash gains, or priority are inferred from history.",
                         "Kalshi uses a complete minute vector where available under the minute option, with fallback to a complete hourly vector. NWS climate times can be a same-station/date comparison only."],
              "provenance": {"engine": VERSION, "horizons_minutes": MINUTES, "minimum_display_disagreement": FLOOR,
                             "source_cli_roots": list(strategy_payout_timing.RAW_ROOTS), "report_gap_counts": raw_gaps,
                             "targeted_polymarket_backfill": {" / ".join(k): v for k, v in acquisition.items()},
                             "targeted_kalshi_minute_backfill": {" / ".join(k): v for k, v in kalshi_acquisition.items()},
                             "kalshi_minute_prices_enabled": bool(kalshi_minutes or fetch_kalshi),
                             "implementation_sha256": sha256(Path(__file__).read_bytes()).hexdigest()}}
    return research_model.write_record(data, report, {"panels": all_panels, "report_selected_marks": favored_details,
                                                       "continuous_later_polymarket_marks": continuous_details,
                                                       "exact_report_events": list(pool.values()), "price_gaps": coverage})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=venue_archive.DEFAULT_DATA)
    parser.add_argument("--fetch-poly-events", type=int, default=0,
                        help="Backfill this many report windows per Polymarket family and paper date split")
    parser.add_argument("--fetch-kalshi-events", type=int, default=0,
                        help="Backfill minute candles for this many trading groups per Kalshi family and paper date split")
    parser.add_argument("--kalshi-minute", action="store_true", help="Use archived minute candles where available")
    args = parser.parse_args()
    if min(args.fetch_poly_events, args.fetch_kalshi_events) < 0:
        parser.error("The per-family event limit must be nonnegative")
    path = research(args.data, fetch_poly=args.fetch_poly_events, fetch_kalshi=args.fetch_kalshi_events,
                    kalshi_minutes=args.kalshi_minute)
    result = json.loads(path.read_bytes())
    print(json.dumps({"path": str(path), "metrics": result["metrics"], "official_report_matches": result["official_report_matches"],
                      "price_coverage_gaps": result["price_coverage_gaps"], "provenance": result["provenance"],
                      "report_selected_instrument_metrics": result["report_selected_instrument_metrics"],
                      "continuous_later_polymarket_mark_metrics": result["continuous_later_polymarket_mark_metrics"]}, indent=2))


if __name__ == "__main__":
    main()
