"""Compare Kalshi hourly price proxies and NOAA on the venue's own settled payouts."""

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import statistics

from . import study, venue_archive
from .market import decimal, iso, parse_time
from .model import _limits, _probability
from .strategy_rules import temperature_window
from .strategy_market import BLEND_GRID, FIT_DATES, FIT_LABEL_CUTOFF, TEST_DATES, event_inputs, metrics_for, normalized_prices, score
from .strategy_runs import DEFAULT_DATA, digest, json_bytes, write_new


ENGINE = "kalshi-candle-payout-benchmark-v1"
KALSHI = venue_archive.KALSHI
DISPLAY_DELAY = timedelta(minutes=1)
FOLLOW_MINUTES = 60
PROXY_GAP = 0.05


def candle_points(data, instrument, *, kind="candle-60m"):
    entries = venue_archive.history(data, KALSHI, instrument, kind=kind, limit=200)
    closes = {}
    for entry in entries:
        bar = entry["body"]
        bid = venue_archive.kalshi_dollar_close(bar.get("yes_bid"))
        ask = venue_archive.kalshi_dollar_close(bar.get("yes_ask"))
        if bid is None or ask is None:
            continue
        low, high = decimal(bid), decimal(ask)
        if not 0 <= low < high <= 1 or (low == 0 and high == 1):
            continue
        if not isinstance(bar.get("end_period_ts"), int):
            continue
        at = parse_time(entry["at"])
        if at != datetime.fromtimestamp(bar["end_period_ts"], timezone.utc):
            continue
        closes[at + DISPLAY_DELAY] = {"mid": float((low + high) / 2), "bid": str(low), "ask": str(high),
                                      "at": iso(at), "receipt_id": entry["receipt_id"]}
    return closes


def settled_buckets(markets):
    buckets = []
    for market in markets:
        limits = temperature_window(market)
        if limits is None or market["yes_payout"] is None:
            return None
        payout = decimal(market["yes_payout"])
        if payout not in (0, 1):
            return None
        buckets.append({
            "id": market["instrument_id"], "label": market["label"], "lower_f": limits[0], "upper_f": limits[1],
            "result": int(payout), "settlement_at": market["settlement_at"], "settlement_source": market["settlement_source"],
            "resolution_source": market["resolution_source"], "rules": market["rules_primary"],
        })
    if study._partition(buckets) != "exhaustive" or sum(bucket["result"] for bucket in buckets) != 1:
        return None
    return buckets


def snapshot_at(times, books, at):
    i = bisect_right(times, at) - 1
    return books[times[i]] if i >= 0 and at - times[i] < timedelta(hours=1) else None


def scenarios(data, event, markets, artifact, window, gaps, *, label_cutoff=None):
    buckets = settled_buckets(markets)
    if not buckets:
        gaps["settled market is not a verified binary integer partition"] += 1
        return
    if any(
        label_cutoff and (not bucket["settlement_at"] or parse_time(bucket["settlement_at"]) > parse_time(label_cutoff))
        for bucket in buckets
    ):
        gaps["exchange payout clock is missing or after lock"] += 1
        return
    cutoff = min(parse_time(row["closed_at"]) for row in markets)
    prices = [candle_points(data, bucket["id"]) for bucket in buckets]
    if any(not series for series in prices):
        gaps["one or more instruments lack hourly bid and ask history"] += 1
        return
    times = [sorted(series) for series in prices]
    for at in study._decision_times(event["date"], event["standard_utc_offset_hours"]):
        at += DISPLAY_DELAY
        if at >= cutoff or (artifact is not None and at < parse_time(artifact["evidence_cutoff"])):
            continue
        snapshots = [snapshot_at(clocks, trace, at) for clocks, trace in zip(times, prices, strict=True)]
        if any(item is None for item in snapshots):
            gaps["decision lacks same-hour venue quotes"] += 1
            continue
        normalized = normalized_prices([item["mid"] for item in snapshots])
        if normalized is None:
            gaps["candidate venue vector has incoherent raw midpoint mass"] += 1
            continue
        weather = study._event_snapshot(event, at)
        if weather is None:
            gaps["decision lacks as-issued station forecast"] += 1
            continue
        if artifact is not None:
            forecasts = study._bucket_estimates(artifact, weather, "noaa", buckets)["buckets"]
        else:
            spread = weather.get("noaa_sd_f")
            if spread is None or float(spread) <= 0:
                gaps["raw NOAA probability requires an as-issued ensemble spread"] += 1
                continue
            forecasts = [{"probability": _probability(_limits(bucket), [weather["noaa_high_f"]], float(spread))}
                         for bucket in buckets]
        later = at + timedelta(minutes=FOLLOW_MINUTES)
        later_snapshots = [snapshot_at(clocks, trace, later) for clocks, trace in zip(times, prices, strict=True)] if later < cutoff else []
        later_normalized = normalized_prices([item["mid"] for item in later_snapshots]) if later_snapshots and all(later_snapshots) else None
        high = (event.get("settlement") or {}).get("high_f")
        yield {
            "window": window, "date": event["date"], "station": event["station"],
            "event_id": markets[0]["event_id"], "at": iso(at),
            "forecast_issued_at": weather["forecast"]["issued_at"], "forecast_available_at": weather["forecast"]["available_at"],
            "official_noaa_high_f": high, "price_source": "Kalshi hourly period bid-ask closes",
            "buckets": [
                {"instrument_id": bucket["id"], "title": bucket["label"], "outcome": bucket["result"],
                 "market": proxy, "noaa": guidance["probability"], "book": quote, "bounds": [bucket["lower_f"], bucket["upper_f"]],
                 "payout_source": bucket["settlement_source"], "settlement_at": bucket["settlement_at"],
                 "rules": bucket["rules"], "resolution_source": bucket["resolution_source"],
                 "later_market": later_normalized[index] if later_normalized else None,
                 "later_book": later_snapshots[index] if later_normalized else None}
                for index, (bucket, quote, proxy, guidance) in enumerate(zip(buckets, snapshots, normalized, forecasts, strict=True))
            ],
        }


def direct_high(row):
    high = row["official_noaa_high_f"]
    if high is None:
        return None
    for bucket in row["buckets"]:
        low, upper = bucket["bounds"]
        if (low is None or high >= low) and (upper is None or high <= upper):
            return bucket["outcome"] == 1
    return None


def opportunity_row(window, decisions, method, weight):
    count = Counter()
    signed_moves = []
    for row in decisions:
        strongest = max(row["buckets"], key=lambda b: abs(b["noaa"] - b["market"]) * weight)
        discrepancy = (strongest["noaa"] - strongest["market"]) * weight
        if abs(discrepancy) < PROXY_GAP:
            continue
        count["candidates"] += 1
        count["winning_side"] += ((strongest["outcome"] == 1) == (discrepancy > 0))
        if strongest["later_market"] is not None:
            signed_moves.append((strongest["later_market"] - strongest["market"]) * (1 if discrepancy > 0 else -1))
    return {
        "window": window, "method": method, "decisions": len(decisions), "candidates": count["candidates"],
        "selected_sides_that_paid_1": count["winning_side"], "candidates_with_later_prices": len(signed_moves),
        "later_moved_in_signal_direction": sum(move > 0 for move in signed_moves),
        "median_one_hour_signed_price_move_points": 100 * statistics.median(signed_moves) if signed_moves else None,
    }


def case_for(row):
    strongest = max(row["buckets"], key=lambda b: abs(b["noaa"] - b["market"]))
    return {
        **row, "venue": KALSHI, "instrument_id": strongest["instrument_id"],
        "title": f"{row['station']} {strongest['title']} · Kalshi historical payout",
        "detail": f"Venue midpoint proxy {strongest['market']:.1%}, NOAA {strongest['noaa']:.1%}, "
                  f"YES paid {strongest['outcome']:.2f}.",
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", type=Path, default=DEFAULT_DATA)
    args = ap.parse_args()
    data = args.data
    artifact = study.load_selected(data)
    if artifact is None:
        ap.error("Missing archived NOAA benchmark model.")
    market_groups = defaultdict(list)
    for row in venue_archive.export(data, venue=KALSHI, family="daily-high", status="settled"):
        if (row.get("station") in artifact["stations"] and row.get("day")
                and FIT_DATES[0] <= row["day"] <= TEST_DATES[1] and row.get("closed_at")):
            market_groups[(row["day"], row["station"], row["event_id"])].append(row)
    by_station_day = defaultdict(list)
    for (day, station, _), group in market_groups.items():
        by_station_day[(day, station)].append(group)
    inputs, panels = {}, {"development": [], "later test": []}
    gaps = {window: Counter() for window in panels}
    for path, file_digest, event in event_inputs(data):
        if event["station"] not in artifact["stations"]:
            continue
        day, station = event["date"], event["station"]
        if FIT_DATES[0] <= day <= FIT_DATES[1]:
            window = "development"
        elif TEST_DATES[0] <= day <= TEST_DATES[1]:
            window = "later test"
        else:
            continue
        inputs[str(path.relative_to(data))] = file_digest
        groups = by_station_day[day, station]
        if len(groups) != 1:
            gaps[window]["exact station/day Kalshi event absent or ambiguous"] += 1
            continue
        panels[window].extend(scenarios(data, event, groups[0], artifact, window, gaps[window],
                                        label_cutoff=FIT_LABEL_CUTOFF if window == "development" else None))
    selected = min(BLEND_GRID, key=lambda w: score(panels["development"], w)) if panels["development"] else 0
    metrics = [{**metrics_for(window, rows, weight), "method": method}
               for window, rows in panels.items()
               for method, weight in (("Market midpoint proxy", 0.0), ("Locked blend", selected), ("NOAA physical forecast", 1.0))]
    opportunities = [opportunity_row(window, rows, method, weight) for window, rows in panels.items()
                     for method, weight in (("NOAA", 1), ("Locked blend", selected))]
    label_agreement = {}
    for window, rows in panels.items():
        by_day = {(day["station"], day["date"]): direct_high(day) for day in rows}
        label_agreement[window] = {"both_labels": sum(value is not None for value in by_day.values()),
                                   "agree": sum(value is True for value in by_day.values()),
                                   "different": sum(value is False for value in by_day.values())}
    settings = {
        "engine": ENGINE, "artifact_vintage": artifact["vintage"], "weather_sources": inputs,
        "fit_dates": FIT_DATES, "test_dates": TEST_DATES, "fit_payout_cutoff": FIT_LABEL_CUTOFF,
        "weight_grid": BLEND_GRID, "candle_proxy_lag_seconds": DISPLAY_DELAY.total_seconds(),
        "follow_minutes": FOLLOW_MINUTES, "minimum_proxy_difference": PROXY_GAP,
    }
    inputs = {"settings": settings, "panels": panels, "gaps": {window: dict(value) for window, value in gaps.items()}}
    ident = "kalshi-proxy-" + digest(inputs)[:20]
    snapshot = data / "research" / "evidence" / "kalshi" / f"{ident}.json"
    write_new(snapshot, json_bytes(inputs) + b"\n")
    report = {
        "id": ident, "title": "Kalshi historical prices versus NOAA station forecasts",
        "hypothesis": "A locked weather blend improves the hourly Kalshi market proxy against Kalshi's verified binary payouts.",
        "target": "Daily high temperature bucket",
        "venues": [KALSHI], "created_at": iso(datetime.now(timezone.utc)),
        "date_window": {"from": FIT_DATES[0], "fit_through": FIT_DATES[1],
                        "test_from": TEST_DATES[0], "through": TEST_DATES[1]},
        "metrics": metrics, "price_only_opportunities": opportunities,
        "cases": [case_for(min(rows, key=lambda row: min((b["market"] for b in row["buckets"] if b["outcome"]), default=1)))
                  for rows in (panels["later test"], panels["development"]) if rows],
        "limits": [
            "Hourly closes are historical bid/ask estimates and do not supply sized depth or fills.",
            "NOAA supplies a physical station-day forecast. The contracts use Kalshi's own terms and actual payouts.",
            "The venue settled timestamp is a retrospective label clock. Original on-wire publication receipts were unavailable.",
        ],
        "evidence": {"gaps": inputs["gaps"], "nws_climate_high_bucket_vs_kalshi_payout": label_agreement},
        "provenance": {**settings, "frozen_inputs": str(snapshot.relative_to(data))},
    }
    path = data / "research" / "models" / f"{ident}.json"
    write_new(path, json_bytes(report) + b"\n")
    print(json.dumps({"path": str(path), "metrics": metrics, "evidence": report["evidence"], "opportunities": opportunities}, indent=2))


if __name__ == "__main__":
    main()
