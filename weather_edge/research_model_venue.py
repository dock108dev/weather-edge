"""Compare complete climate reports with cross-venue temperature payouts."""

from __future__ import annotations

import argparse
import gzip
import json
import math
import statistics
import sys
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

from . import research_model, research_model_high, research_model_low_market, venue_archive
from .model import _limits, _outcome, _probability
from .nbm import iso, utc

VERSION = "temperature-settlement-audit-v1"
PRICES = "https://docs.kalshi.com/api-reference/market/get-market-candlesticks"
PROVIDER = "https://weather.com/kalshi"


def integer_bucket(row):
    """Translate explicit contract operators into inclusive integer endpoints."""
    lo = float(row["lower"]) if row.get("lower") is not None else None
    hi = float(row["upper"]) if row.get("upper") is not None else None
    operator = row["operator"]
    if operator in ("between-inclusive", "equal") and lo is not None and hi is not None:
        return math.ceil(lo), math.floor(hi)
    if operator == "below" and hi is not None:
        return None, math.ceil(hi) - 1
    if operator == "at-most" and hi is not None:
        return None, math.floor(hi)
    if operator == "above" and lo is not None:
        return math.floor(lo) + 1, None
    if operator == "at-least" and lo is not None:
        return math.ceil(lo), None
    return None


def _rows(data, venue, family, dates):
    for row in venue_archive.export(data, venue=venue, family=family):
        if ((row["station"], row["day"]) in dates and row["yes_payout"] in ("0", "1")
                and row["units"] in ("F", "°F") and integer_bucket(row)):
            yield row


def _raw_forecasts(data, path):
    return json.loads(gzip.decompress(path.read_bytes()))


def _load_runs(data, high_path=None, low_path=None):
    if high_path is None:
        high_path = research_model_high.study(data)
    if low_path is None:
        low_path, _, _ = research_model.study(data)
    reports = {}
    predictions = {}
    for family, path in (("daily-high", high_path), ("daily-low", low_path)):
        result = json.loads(path.read_text())
        details = data / result["provenance"]["replay_details_path"]
        predictions[family] = _raw_forecasts(data, details)["test"]
        reports[family] = research_model._source_entry(data, path)
    return reports, predictions


def _midpoint(data, row, cache, decision):
    instrument = row["instrument_id"]
    if instrument not in cache:
        cache[instrument] = venue_archive.history(data, "kalshi", instrument, kind="candle-60m", limit=200)
    eligible = [entry for entry in cache[instrument]
                if timedelta(minutes=1) <= decision - utc(entry["at"]) <= timedelta(minutes=61)]
    if not eligible:
        return None
    source = max(eligible, key=lambda entry: entry["at"])
    body = source["body"]
    bid = body.get("yes_bid", {}).get("close_dollars")
    ask = body.get("yes_ask", {}).get("close_dollars")
    if bid in (None, ""):
        bid = body.get("yes_bid", {}).get("close")
    if ask in (None, ""):
        ask = body.get("yes_ask", {}).get("close")
    if bid in (None, "") or ask in (None, ""):
        return None
    bid, ask = float(bid), float(ask)
    return {"midpoint": (bid + ask) / 2, "yes_bid": bid, "yes_ask": ask,
            "at": source["at"], "receipt_id": source["receipt_id"], "body": body} if 0 <= bid <= ask <= 1 else None


def _direct_audit(data, forecasts, family, markets):
    cache = {}
    groups = defaultdict(list)
    for row in forecasts:
        groups[row["station"], row["date"]].append(row)
    climate_cases = {}
    skipped = defaultdict(int)
    rows = []
    for market in markets:
        key = market["station"], market["day"]
        bucket_lo, bucket_hi = integer_bucket(market)
        bucket = {"lower_f": bucket_lo, "upper_f": bucket_hi}
        actual = groups[key][0]["actual_f"]
        yes = int(market["yes_payout"])
        detail = venue_archive.detail(data, "kalshi", market["instrument_id"])
        if not detail:
            skipped["missing archive detail"] += 1
            continue
        raw = detail["raw_market"]
        identity = raw.get("ticker") == market["instrument_id"]
        observed = raw.get("expiration_value")
        try:
            official = float(observed)
        except (TypeError, ValueError):
            official = None
        if not identity or official is None or not math.isfinite(official) or official % 1:
            skipped["no verified numeric provider label"] += 1
            continue
        if _outcome(bucket, official) != yes:
            skipped["provider label and normalized native payout disagree"] += 1
            continue
        if yes == 1:
            climate_cases[(family, *key)] = {
                "station": key[0], "date": key[1], "venue": "kalshi", "instrument_id": market["instrument_id"],
                "nws_f": actual, "provider_f": official, "delta_f": official - actual,
                "rules_primary": market["rules_primary"], "rules_secondary": market["rules_secondary"],
                "weather_source": market["resolution_source"], "settlement_at": market["settlement_at"],
                "settlement_source": market["settlement_source"], "native_payout": yes,
                "receipt_id": (detail["settlement_receipt"] or {}).get("id"),
                "official_report": groups[key][0].get("official_report") or groups[key][0].get("label"),
            }
        open_clock, close_clock = raw.get("open_time"), raw.get("close_time")
        if not open_clock or not close_clock:
            skipped["no instrument trading clocks"] += 1
            continue
        open_at, close_at = utc(open_clock), utc(close_clock)
        for forecast in groups[key]:
            at = utc(forecast["at"])
            if not open_at <= at < close_at:
                continue
            chances = {method: _probability(_limits(bucket), [estimate["mean_f"]], estimate["scale_f"])
                       for method, estimate in forecast["estimates"].items()}
            quote = _midpoint(data, market, cache, at)
            rows.append({"family": family, "station": key[0], "date": key[1], "at": forecast["at"],
                         "instrument_id": market["instrument_id"], "bucket": bucket, "payout_yes": yes,
                         "nws_bucket_yes": _outcome(bucket, actual), "weather_provider_f": official,
                         "forecast": forecast["forecast"], "price_proxy": quote, "probabilities": chances})
    return rows, climate_cases, dict(skipped)


def _pair_audit(poly_markets, kalshi_markets, family):
    by_key = defaultdict(list)
    for row in kalshi_markets:
        by_key[(row["station"], row["day"], integer_bucket(row))].append(row)
    pairs = []
    for poly in poly_markets:
        station, day = poly["station"], poly["day"]
        for kalshi in by_key[(station, day, integer_bucket(poly))]:
            p = int(poly["yes_payout"])
            k = int(kalshi["yes_payout"])
            pairs.append({"family": family, "station": station, "date": day,
                          "lower_upper_inclusive": integer_bucket(poly),
                          "polymarket_instrument": poly["instrument_id"], "polymarket_payout": p,
                          "polymarket_resolution_source": poly["resolution_source"],
                          "kalshi_instrument": kalshi["instrument_id"], "kalshi_payout": k,
                          "kalshi_resolution_source": kalshi["resolution_source"],
                          "polymarket_rules_primary": poly["rules_primary"],
                          "kalshi_rules_primary": kalshi["rules_primary"],
                          "kalshi_rules_secondary": kalshi["rules_secondary"],
                          "payout_disagreement": p != k})
    return pairs


def _same_case_prices(data, rows, pairs):
    related = defaultdict(list)
    for pair in pairs:
        if not pair["payout_disagreement"]:
            related[pair["kalshi_instrument"]].append(pair)
    cache = {}
    matches = []
    for row in rows:
        if not row["price_proxy"]:
            continue
        for pair in related[row["instrument_id"]]:
            symbol = pair["polymarket_instrument"]
            if symbol not in cache:
                history = []
                entries = venue_archive.history(data, "polymarket-us", symbol, kind="display-price-1m", limit=3605)
                for item in reversed(entries):
                    body = item["body"]
                    yes, no = (float(body[key]) if body.get(key) is not None else None
                               for key in ("longPrice", "shortPrice"))
                    if yes is None or no is None or not 0 <= yes <= 1 or not 0 <= no <= 1:
                        continue
                    history.append({"at": item["at"], "available_at": iso(utc(item["at"]) + timedelta(minutes=1)),
                                    "yes": yes, "no": no, "receipt_id": item["receipt_id"]})
                cache[symbol] = history, [utc(item["available_at"]) for item in history]
            history, clocks = cache[symbol]
            mark = research_model_low_market.matching_price(history, clocks, utc(row["at"]))
            p = research_model_low_market.display_probability(mark)
            if p is not None:
                matches.append({**row, "polymarket_instrument": symbol, "polymarket_price": mark, "polymarket_midpoint": p})
    return matches


def _score(rows, family, group):
    if not rows:
        return []
    names = research_model_high.METHODS if family == "daily-high" else research_model.METHODS
    methods = {**names}
    if all(row["price_proxy"] for row in rows):
        methods["market_midpoint"] = "Kalshi hourly candle midpoint"
    if all("polymarket_midpoint" in row for row in rows):
        methods["polymarket_midpoint"] = "Polymarket US minute display midpoint"
    dates = len({row["date"] for row in rows})
    station_days = len({(row["station"], row["date"]) for row in rows})
    snapshots = len({(row["station"], row["date"], row["at"]) for row in rows})
    def probability(row, method):
        if method == "market_midpoint":
            return row["price_proxy"]["midpoint"]
        if method == "polymarket_midpoint":
            return row["polymarket_midpoint"]
        return row["probabilities"][method]

    return [{"method": method, "label": label, "group": f"{family} · {group}",
             "calendar_dates": dates, "station_days": station_days, "snapshots": snapshots, "bucket_cases": len(rows),
             "brier": statistics.mean((probability(row, method) - row["payout_yes"]) ** 2 for row in rows)}
            for method, label in methods.items()]


def study(data, high_path=None, low_path=None):
    data = Path(data)
    reports, forecasts = _load_runs(data, high_path, low_path)
    scores, case_details, disagreements = [], [], []
    details = {}
    for family, predictions in forecasts.items():
        dates = {(row["station"], row["date"]) for row in predictions}
        kalshi = list(_rows(data, "kalshi", family, dates))
        poly = list(_rows(data, "polymarket-us", family, dates))
        rows, weather, coverage_gaps = _direct_audit(data, predictions, family, kalshi)
        pairs = _pair_audit(poly, kalshi, family)
        matches = [row for row in rows if row["price_proxy"]]
        scores += _score(rows, family, "verified payout") + _score(matches, family, "matched archived market midpoint")
        two_venue_prices = _same_case_prices(data, matches, pairs)
        scores += _score(two_venue_prices, family, "both venue quotes and matching payout")
        used = list(weather.values())
        disagreement = [row for row in pairs if row["payout_disagreement"]]
        delta = [row for row in used if row["delta_f"] != 0]
        site_days = len({(row["station"], row["date"]) for row in pairs})
        scores.extend([
            {"method": "source_audit", "label": "Provider versus same-date NWS CLI",
             "group": family, "calendar_dates": len({row["date"] for row in used}), "station_days": len(used),
             "same_report_value": len(used) - len(delta), "different_report_value": len(delta),
             "mean_abs_label_difference_f": statistics.mean(abs(row["delta_f"]) for row in used) if used else None},
            {"method": "payout_audit", "label": "Identical integer strike, same station-date",
             "group": family, "calendar_dates": len({row["date"] for row in pairs}), "station_days": site_days,
             "bucket_pairs": len(pairs), "agreed_payout": len(pairs) - len(disagreement),
             "disagreed_payout": len(disagreement)},
        ])
        disagreements.extend(disagreement)
        for case in sorted(used, key=lambda row: (abs(row["delta_f"]), row["date"]), reverse=True)[:3]:
            possible = [row for row in matches if row["instrument_id"] == case["instrument_id"]]
            if not possible:
                possible = [row for row in rows if row["instrument_id"] == case["instrument_id"]]
            at = max(possible, key=lambda example: example["at"])["at"] if possible else None
            case_details.append({**case, "at": at, "title": f"{case['station']} {case['date']} · {family}",
                                 "detail": f"Reported Kalshi ruling value {case['provider_f']:.0f}°F. NWS archive {case['nws_f']:.0f}°F."})
        details[family] = {"kalshi_markets": kalshi, "polymarket_markets": poly, "cross_venue_pairs": pairs,
                           "weather_label_cases": used, "excluded": coverage_gaps, "scored_rows": rows,
                           "both_venue_quote_cases": two_venue_prices}
    record = {
        "version": VERSION, "title": "Temperature forecasts and two-venue settlement checks",
        "hypothesis": "A same-station daily CLI temperature can inform both venues, but payout equivalence depends on the exact report and settlement terms.",
        "target": "Kalshi and Polymarket US daily high and low outcomes",
        "venues": ["kalshi", "polymarket-us"], "date_window": research_model._window_strings(),
        "metrics": scores, "cases": case_details,
        "limits": [
            "Weather scores use archived exchange settlements. Kalshi prices use the most recent uncrossed hourly bid/ask close at least one minute before decision. These are price comparisons with unknown order depth.",
            "Kalshi names The Weather Company in its sampled daily rules. Its provider describes original government CLI observations with CF6 fallback. Polymarket US uses its own NWS CLI and review/fallback terms.",
            "Identical station, date, family and strikes support a payout history comparison. The comparison leaves payout equivalence unapproved because each exchange can select different revisions or fallback values.",
            "Matched venue prices keep the minute Polymarket display midpoint and completed hourly Kalshi midpoint on the same decision. Their historical freshness is different.",
            "The official weather model has separate labels from native payouts. The archive can omit instruments or historical candles, and available prices can overrepresent active contracts.",
        ],
        "provenance": {"forecast_reports": reports, "kalshi_candlestick_docs": PRICES, "kalshi_source_provider": PROVIDER,
                       "polymarket_display_docs": research_model_low_market.PRICE_DOCS,
                       "source_sha256": research_model.run_sources(sys.modules[__name__]),
                       "price_matching": "1–61 minutes elapsed after hourly candle end"},
        "payout_disagreements": disagreements,
    }
    return research_model.write_record(data, record, details)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path(".data"))
    parser.add_argument("--high-report", type=Path)
    parser.add_argument("--low-report", type=Path)
    args = parser.parse_args()
    print(study(args.data_dir, args.high_report, args.low_report))


if __name__ == "__main__":
    main()
