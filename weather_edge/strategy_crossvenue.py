"""Compare like weather instruments without assuming common payouts."""

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import gzip
from hashlib import sha256
import json
from pathlib import Path

from . import venue_archive, venue_fees
from .capture import load_captures
from .market import decimal, fee_coefficient, iso, parse_time, taker_fee
from .strategy_rules import book_status, temperature_window
from .strategy_runs import DEFAULT_DATA, digest, json_bytes, write_new


ENGINE = "crossvenue-weather-terms-v3"
MAX_RECEIPT_DIFFERENCE = timedelta(seconds=15)
COMPARISON_CONTRACTS = 10
IDEA_DOLLAR_FLOOR = Decimal("0.25")
FIELDS = ("venue", "instrument_id", "station", "day", "family", "operator", "lower", "upper", "units", "label",
          "resolution_source", "report_policy", "fallback", "climate_starts_at", "climate_ends_at",
          "rules_primary", "rules_secondary", "rules_url", "opened_at", "closed_at", "yes_payout", "settlement_at",
          "settlement_received_at", "settlement_source")


def matching_groups(markets):
    combined = defaultdict(lambda: {venue: [] for venue in venue_archive.VENUES})
    matched = defaultdict(lambda: {venue: [] for venue in venue_archive.VENUES})
    for row in markets:
        if not row["station"] or not row["day"]:
            continue
        group = (row["family"], row["station"], row["day"])
        combined[group][row["venue"]].append(row)
        bounds = temperature_window(row)
        if bounds is not None:
            matched[(*group, *bounds)][row["venue"]].append(row)
    common = {key: group for key, group in combined.items() if all(group.values())}
    exact = {key: group for key, group in matched.items() if all(group.values())}
    return common, exact


def comparison(left, right):
    terms = venue_archive._compare(left, right)
    terms["fields"]["inclusive_integer_threshold"] = "same"
    return {
        "station": left["station"], "date": left["day"], "family": left["family"],
        "temperature_f": list(temperature_window(left)), "terms": terms,
        "polymarket": {field: left[field] for field in FIELDS},
        "kalshi": {field: right[field] for field in FIELDS},
    }


def local_payouts(data):
    """Read previously verified public Polymarket payout responses from replay data."""
    payouts = {}

    def add(event, fallback=False):
        for bucket in event.get("buckets", []):
            source, payout = bucket.get("settlement_source"), bucket.get("archived_payout")
            if payout is None and fallback and bucket.get("result") is not None:
                payout = int(bucket["result"])
            if payout is not None and source:
                payouts[bucket["id"]] = {
                    "yes_payout": str(payout), "settlement_source": source,
                    "settlement_received_at": (event.get("settlement") or {}).get("payout_available_at"),
                }

    earlier = data / "replay.json"
    if earlier.is_file():
        for event in json.loads(earlier.read_text()).get("events", []):
            add(event, fallback=True)
    for path in (data / "archive").glob("????-??-??/K???.json"):
        add(json.loads(path.read_text()))
    return payouts


def headline_price(book, side, *, venue):
    if venue == venue_archive.POLY:
        rows = book.get("offers" if side == "YES" else "bids")
        if not rows:
            return None
        price, quantity = rows[0]
        value = decimal(price) if side == "YES" else 1 - decimal(price)
    else:
        rows = book.get("yes_offers" if side == "YES" else "no_offers")
        if not rows:
            return None
        value, quantity = rows[0]
        value = decimal(value)
    if not 0 < value < 1:
        return None
    return {"price": float(value), "size": float(quantity)}


def origin_inputs(capture):
    by_instrument = {}
    for event in capture.get("events", []):
        cutoff = parse_time(event["ends_at"])
        for bucket in event["buckets"]:
            books = [(at, book) for book in bucket["books"]
                     if (at := parse_time(book["at"])) < cutoff
                     and book_status(book, None, at, "origin-confirmed") is None]
            if books:
                by_instrument[bucket["id"]] = ([at for at, _ in books], [book for _, book in books])
    return by_instrument


def match_live_quotes(pair, poly_inputs, kalshi_books):
    poly = pair["polymarket"]["instrument_id"]
    if poly not in poly_inputs:
        return
    times, books = poly_inputs[poly]
    for kalshi_book in kalshi_books:
        at = parse_time(kalshi_book["at"])
        pos = bisect_right(times, at) - 1
        if pos < 0 or at - times[pos] > MAX_RECEIPT_DIFFERENCE:
            continue
        poly_book = books[pos]
        poly_quotes = {side: headline_price(poly_book, side, venue=venue_archive.POLY) for side in ("YES", "NO")}
        kalshi_quotes = {side: headline_price(kalshi_book, side, venue=venue_archive.KALSHI) for side in ("YES", "NO")}
        same_sides = [side for side in ("YES", "NO") if poly_quotes[side] and kalshi_quotes[side]]
        if any(poly_quotes.values()) and any(kalshi_quotes.values()):
            yield {
                "at": iso(at), "polymarket_at": iso(times[pos]),
                "receipt_difference_seconds": (at - times[pos]).total_seconds(), "polymarket_quotes": poly_quotes,
                "kalshi_quotes": kalshi_quotes, "same_sides": same_sides, "kalshi_receipt_id": kalshi_book["receipt_id"],
                "kalshi_source_at": kalshi_book.get("source_at"),
                "kalshi_http_date": kalshi_book.get("http_date"),
                "kalshi_cache_age_seconds": kalshi_book.get("cache_age_seconds"),
                "kalshi_http_headers": kalshi_book.get("http_headers"),
                "polymarket_source_at": poly_book.get("source_at"),
                "polymarket_http_cache": poly_book.get("http_cache"),
            }


def quote_cash(quote, terms, poly_side, kalshi_side):
    """Price the common visible whole-contract size under both fee schedules."""
    poly = quote["polymarket_quotes"].get(poly_side)
    kalshi = quote["kalshi_quotes"].get(kalshi_side)
    if not poly or not kalshi or not terms.get("available"):
        return None
    size = min(COMPARISON_CONTRACTS, int(poly["size"]), int(kalshi["size"]))
    if size <= 0:
        return None
    first, second = decimal(poly["price"]), decimal(kalshi["price"])
    poly_fee = taker_fee([(first, size)], fee_coefficient(quote["polymarket_at"]))
    kalshi_rate = decimal(terms["taker_rate"]) * decimal(terms["multiplier"])
    kalshi_fee_micros = venue_fees.kalshi_taker_fee(second, size, kalshi_rate)
    kalshi_debit = venue_fees.kalshi_buy_debit(size * second, kalshi_fee_micros)
    poly_debit = size * first + poly_fee
    return {"common_whole_contracts": size,
            "polymarket": {"side": poly_side, "displayed_notional": str(size * first), "fee": str(poly_fee), "debit": str(poly_debit)},
            "kalshi": {"side": kalshi_side, "displayed_notional": str(size * second), "fee": str(kalshi_debit - size * second), "debit": str(kalshi_debit)},
            "kalshi_minus_polymarket_cash": str(kalshi_debit - poly_debit)}


def comparable_quote_cash(quote, terms):
    return [{**cash, "side": side} for side in quote["same_sides"]
            if (cash := quote_cash(quote, terms, side, side)) is not None]


def complementary_quote_cash(quote, terms):
    estimates = []
    for poly_side, kalshi_side in (("YES", "NO"), ("NO", "YES")):
        estimate = quote_cash(quote, terms, poly_side, kalshi_side)
        if estimate is not None:
            debit = decimal(estimate["polymarket"]["debit"]) + decimal(estimate["kalshi"]["debit"])
            estimates.append({**estimate, "combined_displayed_cash": str(debit),
                              "difference_if_payouts_are_complementary": str(estimate["common_whole_contracts"] - debit)})
    return estimates


def candidate_pairs(data, capture, pairs, cutoff):
    inputs = origin_inputs(capture)
    observed, details = [], []
    for pair in pairs:
        poly, kalshi = pair["polymarket"]["instrument_id"], pair["kalshi"]["instrument_id"]
        if poly not in inputs:
            continue
        instruments = pair["polymarket"], pair["kalshi"]
        if any(not listed["opened_at"] or not listed["closed_at"] for listed in instruments):
            continue
        both_open = max(parse_time(listed["opened_at"]) for listed in instruments)
        first_close = min(parse_time(listed["closed_at"]) for listed in instruments)
        raw = venue_archive.books(data, venue_archive.KALSHI, kalshi,
                                  start=max(inputs[poly][0][0], both_open), end=min(first_close, cutoff))
        matches = [quote for quote in match_live_quotes(pair, inputs, raw)
                   if both_open <= parse_time(quote["polymarket_at"]) <= parse_time(quote["at"]) < first_close]
        if matches:
            last = matches[-1]
            for quote in matches:
                schedule = venue_fees.kalshi_terms(data, kalshi, quote["at"])
                quote["same_side_cash"] = comparable_quote_cash(quote, schedule)
                quote["complementary_cash"] = complementary_quote_cash(quote, schedule)
                quote["kalshi_fee_inputs"] = schedule
            details.append({"polymarket_id": poly, "kalshi_id": kalshi, "matches": matches})
            observed.append({
                **pair, "matched_receipt_pairs": len(matches),
                "matched_sides": sum(len(quote["same_sides"]) for quote in matches),
                "missing_kalshi_exchange_clock": sum(q["kalshi_source_at"] is None for q in matches),
                "first_pair": matches[0], "last_pair": last,
                "kalshi_fee_example": last["kalshi_fee_inputs"], "same_side_quote_cash": last["same_side_cash"],
            })
    return observed, details


def make_report(data, cutoff, *, supplemental_midway=False):
    families = ("daily-high", "daily-low")
    catalog = [row for family in families for row in venue_archive.export(data, family=family)]
    proof = local_payouts(data)
    for row in catalog:
        if row["venue"] == venue_archive.POLY and row["yes_payout"] is None:
            row.update(proof.get(row["instrument_id"], {}))
    weather_groups, exact_groups = matching_groups(catalog)
    pairs = [comparison(left, right) for rows in exact_groups.values()
             for left in rows[venue_archive.POLY] for right in rows[venue_archive.KALSHI]]
    settled = [pair for pair in pairs if pair["polymarket"]["yes_payout"] is not None and pair["kalshi"]["yes_payout"] is not None]
    disagreements = [pair for pair in settled if decimal(pair["polymarket"]["yes_payout"]) != decimal(pair["kalshi"]["yes_payout"])]
    equivalent = sum(pair["terms"]["payout_equivalent"] is True for pair in pairs)
    capture = load_captures(data / "captures", through=cutoff)
    if capture:
        from .strategy_native_capture import merge_supplemental, polymarket_events

        lows, _ = polymarket_events(data, cutoff, since=capture["started_at"])
        capture["events"].extend(lows)
        if supplemental_midway:
            supplemental, _ = polymarket_events(data, cutoff, since=capture["started_at"], family="daily-high", station="KMDW")
            merge_supplemental(capture, supplemental)
    observed, full_matches = candidate_pairs(data, capture, pairs, cutoff) if capture else ([], [])
    totals = {
        "market_inventory_by_venue": dict(Counter(row["venue"] for row in catalog)),
        "station_days_both_venues": len({(key[1], key[2]) for key in weather_groups}),
        "family_station_days_both_venues": len(weather_groups),
        "exact_integer_threshold_pairs": len(pairs),
        "exact_threshold_station_days": len({(pair["station"], pair["date"]) for pair in pairs}),
        "exact_threshold_calendar_dates": len({pair["date"] for pair in pairs}),
        "settled_pairs_both_venues": len(settled),
        "settled_pairs_disagreeing": len(disagreements),
        "confirmed_payout_equivalent_pairs": equivalent,
        "live_pairs_with_depth_on_both_venues": len(observed),
        "live_receipt_pairs": sum(item["matched_receipt_pairs"] for item in observed),
        "live_same_sides": sum(item["matched_sides"] for item in observed),
        "matched_kalshi_books_missing_source_time": sum(item["missing_kalshi_exchange_clock"] for item in observed),
        "same_side_fee_comparisons_at_latest_pair": sum(len(item["same_side_quote_cash"]) for item in observed),
    }
    terms_counts = {
        "resolution_source_differences": sum(p["terms"]["fields"]["resolution_source"] == "different" for p in pairs),
        "unknown_climate_window_pairs": sum("unknown" in {p["terms"]["fields"]["climate_starts_at"],
                                                         p["terms"]["fields"]["climate_ends_at"]} for p in pairs),
    }
    money = [entry for match in full_matches for quote in match["matches"] for entry in quote["same_side_cash"]]
    complements = [entry for match in full_matches for quote in match["matches"] for entry in quote["complementary_cash"]]
    directions = [decimal(entry["kalshi_minus_polymarket_cash"]) for entry in money]
    opportunity_counts = {"fee_certified_same_side_at_common_size": len(money),
                          "polymarket_cash_lower_by_at_least_25c": sum(gap >= IDEA_DOLLAR_FLOOR for gap in directions),
                          "kalshi_cash_lower_by_at_least_25c": sum(gap <= -IDEA_DOLLAR_FLOOR for gap in directions),
                          "fee_certified_nominal_complements": len(complements),
                          "nominal_complements_clear_25c_if_equivalent": sum(
                              decimal(row["difference_if_payouts_are_complementary"]) >= IDEA_DOLLAR_FLOOR for row in complements),
                          "simulated_cross_venue_fills": 0}
    per_family = []
    for family in families:
        related = [item for item in observed if item["family"] == family]
        case_ids = {(item["station"], item["date"]) for item in related}
        per_family.append({"window": family, "exact_threshold_pairs": sum(item["family"] == family for item in pairs),
                           "paired_payouts": sum(item["family"] == family for item in settled),
                           "payout_disagreements": sum(item["family"] == family for item in disagreements),
                           "live_instruments": len(related), "local_book_pairs": sum(item["matched_receipt_pairs"] for item in related),
                           "station_days": len(case_ids),
                           "fee_certified_sides": sum(len(item["same_side_quote_cash"]) for item in related),
                           "native_kalshi_exchange_clock_missing": sum(item["missing_kalshi_exchange_clock"] for item in related)})
    cases = []
    selected_observations = [item for family in families for item in sorted(
        (row for row in observed if row["family"] == family),
        key=lambda row: (-row["matched_receipt_pairs"], row["station"]))[:4]]
    for item in selected_observations:
        quote = item["last_pair"]
        poly, kalshi = item["polymarket"], item["kalshi"]
        same_side = next(iter(quote["same_sides"]), None)
        poly_side = same_side or next(side for side, level in quote["polymarket_quotes"].items() if level)
        kalshi_side = same_side or next(side for side, level in quote["kalshi_quotes"].items() if level)
        cases.append({
            "title": f"{item['station']} {poly['label']} · same observed threshold",
            "detail": f"{poly_side} offer {quote['polymarket_quotes'][poly_side]['price']:.2f} on Polymarket US. "
                      f"{kalshi_side} offer {quote['kalshi_quotes'][kalshi_side]['price']:.2f} on Kalshi. "
                      f"Sources are {poly['resolution_source']} and {kalshi['resolution_source']}.",
            "date": item["date"], "station": item["station"], "venue": venue_archive.POLY,
            "instrument_id": poly["instrument_id"], "at": quote["polymarket_at"],
            "related_instrument_id": kalshi["instrument_id"], "data": item,
        })
    by_id = {(item["polymarket"]["instrument_id"], item["kalshi"]["instrument_id"]): item for item in observed}
    quote_discounts = [(abs(gap), pair, quote, cash, gap) for pair in full_matches for quote in pair["matches"]
                       for cash in quote["same_side_cash"] if (gap := decimal(cash["kalshi_minus_polymarket_cash"])) != 0]
    if quote_discounts:
        discount, match, quote, cash, gap = max(quote_discounts, key=lambda row: row[0])
        pair = by_id[match["polymarket_id"], match["kalshi_id"]]
        cheaper = "Polymarket US" if gap > 0 else "Kalshi"
        cases.insert(0, {"station": pair["station"], "date": pair["date"], "venue": venue_archive.POLY,
                         "instrument_id": match["polymarket_id"], "related_instrument_id": match["kalshi_id"],
                         "at": quote["polymarket_at"], "title": f"{pair['station']} {cash['side']} · largest compared cash difference",
                         "detail": f"The same {cash['common_whole_contracts']} displayed contracts cost ${discount:.2f} less on {cheaper} after modeled fees. The two venues use different resolution terms.",
                         "data": {"pair": pair, "matched_quote": quote, "same_side_cash": cash}})
    selected_settled = disagreements[:8] if disagreements else settled[:3]
    for item in selected_settled:
        poly, kalshi = item["polymarket"], item["kalshi"]
        differs = decimal(poly["yes_payout"]) != decimal(kalshi["yes_payout"])
        cases.append({
            "title": f"{item['station']} {poly['label']} · {'different' if differs else 'equal'} actual payouts",
            "detail": f"Polymarket US YES paid {decimal(poly['yes_payout']):.2f}. "
                      f"Kalshi YES paid {decimal(kalshi['yes_payout']):.2f}.",
            "date": item["date"], "station": item["station"], "venue": venue_archive.POLY,
            "instrument_id": poly["instrument_id"], "at": poly["settlement_received_at"],
            "related_instrument_id": kalshi["instrument_id"], "data": item,
        })
    frozen = gzip.compress(json_bytes({"exact_pairs": pairs, "live_receipt_pairs": full_matches}), mtime=0)
    checksum = sha256(frozen).hexdigest()
    computation = {"engine": ENGINE, "through": iso(cutoff), "totals": totals, "opportunities": opportunity_counts,
                   "cases": cases, "snapshot": checksum, "supplemental_midway": supplemental_midway,
                   "receipt_difference_seconds": int(MAX_RECEIPT_DIFFERENCE.total_seconds()), "term_checks": terms_counts}
    data_paths = {"catalog": "venues.sqlite3", "polymarket_tape": "capture-index.sqlite3"}
    ident = "crossvenue-" + digest(computation)[:20]
    snapshot = data / "research" / "evidence" / "crossvenue" / f"{ident}.json.gz"
    write_new(snapshot, frozen)
    report = {
        "id": ident,
        "title": "Which weather prices can be shopped across venues?",
        "hypothesis": "Different live prices are available for paired instruments with the same settlement terms.",
        "target": "Daily high and low temperature buckets",
        "venues": list(venue_archive.VENUES),
        "created_at": iso(datetime.now(timezone.utc)),
        "date_window": {"from": min((key[2] for key in weather_groups), default=None), "through": iso(cutoff)},
        "metrics": [*({"measure": name, "count": value} for name, value in (totals | opportunity_counts).items() if isinstance(value, int)), *per_family],
        "cases": cases,
        "evidence": {**totals, **opportunity_counts, "terms_checks": terms_counts,
                     "observed_examples": observed, "payout_disagreements": disagreements},
        "limits": [
            "A shared temperature threshold only matches the measured weather. Exact payout equivalence requires matching report windows, source, revisions, and fallback rules.",
            "The public Kalshi depth capture lacks an exchange book-image timestamp. Local receipt times are used to compare price evidence.",
            "Both quote receipts must fall inside the intersection of native open intervals. The most recent qualifying Polymarket US quote can arrive up to 15 seconds before the Kalshi receipt.",
            "Same-side and complementary costs use up to ten whole contracts at one displayed level. The Kalshi estimate assumes one equivalent fill and a non-direct cent-aligned cash debit.",
            "A complementary dollar comparison assumes one contract pays per matched unit. It remains hypothetical unless the payout terms prove that relationship.",
            "This screen supplies zero simulated two-leg fills. The paired sample measures comparable weather and price evidence.",
        ],
        "provenance": {"engine": ENGINE, "files": data_paths, "frozen_evidence": str(snapshot.relative_to(data)),
                       "replay_details_path": str(snapshot.relative_to(data)), "supplemental_midway": supplemental_midway,
                       "frozen_evidence_sha256": checksum,
                       "receipt_difference_seconds": computation["receipt_difference_seconds"]},
    }
    path = data / "research" / "strategies" / f"{report['id']}.json"
    write_new(path, json_bytes(report) + b"\n")
    return report, path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--through", help="Latest allowed live quote receipt time.")
    parser.add_argument("--supplemental-midway-high", action="store_true", help="Include separately collected native Chicago Midway high ladders.")
    args = parser.parse_args()
    cutoff = parse_time(args.through) if args.through else datetime.now(timezone.utc)
    report, path = make_report(args.data, cutoff, supplemental_midway=args.supplemental_midway_high)
    print(json.dumps({"file": str(path), "metrics": report["metrics"], "cases": [
        {key: item[key] for key in ("title", "detail", "station", "date", "instrument_id", "at")} for item in report["cases"]
    ]}, indent=2))


if __name__ == "__main__":
    main()
