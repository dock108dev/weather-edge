"""Test whether comparable temperature marks converge over the next hour."""

import argparse
from collections import Counter, defaultdict
from datetime import timedelta
import json
from pathlib import Path
from statistics import mean

import numpy as np
from sklearn.linear_model import Ridge

from . import research_model, venue_archive
from .market import decimal, iso, parse_time
from .strategy_rules import temperature_window


VERSION = "native-temperature-venue-convergence-v2"
WINDOWS = {"fit": ("2026-08-14", "2026-10-13"), "selection": ("2026-10-14", "2026-10-25"),
           "test": ("2026-10-29", "2026-11-14")}
HOURS = (13, 17)
FEATURES = {"unchanged": (), "own previous hour": ("own_change",),
            "cross-venue gap": ("other_mark_gap",), "own change and cross-venue gap": ("own_change", "other_mark_gap")}
SOURCES = {venue_archive.POLY: "https://docs.polymarket.us/api-reference/price-history/get-price-history",
           venue_archive.KALSHI: "https://docs.kalshi.com/api-reference/market/get-market-candlesticks"}
KINDS = {venue_archive.POLY: "display-price-1m", venue_archive.KALSHI: "candle-60m"}


def _numbers(venue, body):
    if venue == venue_archive.POLY:
        long, short = body.get("longPrice"), body.get("shortPrice")
        bid, ask = (1 - decimal(short) if short is not None else None), (decimal(long) if long is not None else None)
    else:
        bid, ask = (venue_archive.kalshi_dollar_close(body.get(name)) for name in ("yes_bid", "yes_ask"))
        bid, ask = (decimal(bid) if bid is not None else None), (decimal(ask) if ask is not None else None)
    if bid is None or ask is None or not 0 <= bid <= ask <= 1 or (bid == 0 and ask == 1):
        return None
    return float(bid), float(ask)


def _marks(db, market, ticks):
    venue = market["venue"]
    clocks = [iso(t) for t in ticks]
    rows = db.execute(f"""SELECT at,receipt_id,body FROM history
                          WHERE venue=? AND instrument_id=? AND kind=? AND at IN ({','.join('?' for _ in clocks)})""",
                      (venue, market["instrument_id"], KINDS[venue], *clocks))
    output = {}
    for row in rows:
        body = json.loads(row["body"])
        quote = _numbers(venue, body)
        if quote is None:
            continue
        bid, ask = quote
        output[parse_time(row["at"])] = {"at": row["at"], "available_proxy_at": iso(parse_time(row["at"]) + timedelta(minutes=1)),
                                        "yes_bid": bid, "yes_ask": ask, "midpoint": (bid + ask) / 2, "raw": body,
                                        "receipt_id": row["receipt_id"]}
    return output


def _markets(db):
    start, end = WINDOWS["fit"][0], WINDOWS["test"][1]
    rows = db.execute("""SELECT venue, instrument_id, family, station, day, label, operator, lower, upper, units,
                         climate_starts_at, climate_ends_at, opened_at, closed_at, settlement_at, yes_payout, rules_primary, resolution_source
                         FROM markets WHERE day BETWEEN ? AND ? AND family IN ('daily-high','daily-low')
                           AND station IS NOT NULL
                           AND opened_at IS NOT NULL AND closed_at IS NOT NULL""", (start, end))
    groups = defaultdict(lambda: defaultdict(list))
    for row in map(dict, rows):
        bounds = temperature_window(row)
        if bounds is not None:
            key = (row["family"], row["station"], row["day"], bounds)
            groups[key][row["venue"]].append(row)
    for key, offers in groups.items():
        if all(len(offers[venue]) == 1 for venue in venue_archive.VENUES):
            yield key, {venue: offers[venue][0] for venue in venue_archive.VENUES}


def assemble(data):
    found, coverage = [], Counter()
    with venue_archive.connect(data) as db:
        db.execute("BEGIN")
        for key, markets in _markets(db):
            family, station, day, limits = key
            phase = next((phase for phase, (first, last) in WINDOWS.items() if first <= day <= last), None)
            if phase is None:
                continue
            coverage[phase, family, "exact shared instrument pairs"] += 1
            poly_row, kalshi_row = (markets[venue] for venue in (venue_archive.POLY, venue_archive.KALSHI))
            if not poly_row["climate_starts_at"] or not poly_row["climate_ends_at"]:
                coverage[phase, family, "missing local-standard decision anchor"] += 1
                continue
            start, day_end = (parse_time(poly_row[field]) for field in ("climate_starts_at", "climate_ends_at"))
            verified_common_window = all(kalshi_row[field] and parse_time(kalshi_row[field]) == parse_time(poly_row[field])
                                         for field in ("climate_starts_at", "climate_ends_at"))
            opened = max(parse_time(m["opened_at"]) for m in markets.values())
            closed = min(day_end, *(parse_time(m["closed_at"]) for m in markets.values()),
                         *(parse_time(m["settlement_at"]) for m in markets.values() if m["settlement_at"]))
            ticks = [start + timedelta(hours=hour + offset) for hour in HOURS for offset in (-1, 0, 1)]
            marks = {venue: _marks(db, market, ticks) for venue, market in markets.items()}
            for hour in HOURS:
                instant = start + timedelta(hours=hour)
                check = (instant - timedelta(hours=1), instant, instant + timedelta(hours=1))
                if check[0] < opened or check[-1] + timedelta(minutes=1) >= closed:
                    coverage[phase, family, "outside shared pre-close window"] += 1
                    continue
                if any(clock not in marks[venue] for venue in venue_archive.VENUES for clock in check):
                    coverage[phase, family, "missing coherent marks"] += 1
                    continue
                observed = {venue: [marks[venue][clock] for clock in check] for venue in venue_archive.VENUES}
                found.append({"phase": phase, "family": family, "station": station, "date": day, "local_standard_hour": hour,
                              "at": iso(instant + timedelta(minutes=1)), "temperature_f": list(limits),
                              "verified_common_climate_window": verified_common_window, "markets": markets, "marks": observed})
                coverage[phase, family, "paired windows"] += 1
    return found, coverage


def _changes(row, venue):
    other, = (candidate for candidate in venue_archive.VENUES if candidate != venue)
    previous, current, following = [x["midpoint"] for x in row["marks"][venue]]
    return {"level": current, "target": following, "own_change": current - previous,
            "other_mark_gap": row["marks"][other][1]["midpoint"] - current}


def _daily(rows, value):
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["date"]].append(value(row))
    return [mean(cases) for cases in grouped.values()]


def _day_mean(rows, value):
    daily = _daily(rows, value)
    return mean(daily) if daily else None


def _date_interval(rows, value):
    daily = _daily(rows, value)
    if not daily:
        return None
    sampled = np.random.default_rng(20261115).choice(daily, size=(2500, len(daily)))
    return np.quantile(sampled.mean(axis=1), (0.025, 0.975)).tolist()


def evaluate(rows, venue):
    for row in rows:
        changes = _changes(row, venue)
        row.setdefault("forecasts", {})[venue] = changes
    fit = [row["forecasts"][venue] for row in rows if row["phase"] == "fit"]
    models = {name: Ridge(alpha=5, fit_intercept=False).fit([[r[key] for key in keys] for r in fit],
              [r["target"] - r["level"] for r in fit]) for name, keys in FEATURES.items() if fit and keys}
    for row in rows:
        features = row["forecasts"][venue]
        current = features["level"]
        features["projections"] = {"unchanged": current}
        for name, model in models.items():
            projected = current + float(model.predict([[features[key] for key in FEATURES[name]]])[0])
            features["projections"][name] = min(1.0, max(0.0, projected))

    def err(row, model):
        result = row["forecasts"][venue]
        return abs(result["projections"][model] - result["target"])

    early = [row for row in rows if row["phase"] == "selection"]
    methods = ("unchanged", *models)
    chosen = min(methods, key=lambda name: _day_mean(early, lambda row: err(row, name))) if early else "unchanged"
    metrics = []
    for phase in WINDOWS:
        phase_rows = [row for row in rows if row["phase"] == phase]
        for method in methods:
            metrics.append({"venue": venue, "window": phase, "model": method, "earlier_selected": method == chosen,
                            "rows": len(phase_rows), "dates": len({r["date"] for r in phase_rows}),
                            "station_days": len({(r["station"], r["date"]) for r in phase_rows}),
                            "date_weighted_midpoint_mae": _day_mean(phase_rows, lambda row: err(row, method)),
                            "date_block_mae_delta_interval_vs_unchanged": _date_interval(
                                phase_rows, lambda row: err(row, method) - err(row, "unchanged")),
                            "coefficients": dict(zip(FEATURES[method], models[method].coef_.tolist())) if method in models else {}})
    return metrics, chosen


def convergence(rows):
    def gap(row, column):
        return round(row["marks"][venue_archive.KALSHI][column]["midpoint"] - row["marks"][venue_archive.POLY][column]["midpoint"], 8)

    def contraction(row):
        return abs(gap(row, 2)) - abs(gap(row, 1))

    def toward_peer(row, venue):
        signs = 1 if gap(row, 1) >= 0 else -1
        if venue == venue_archive.KALSHI:
            signs *= -1
        _, current, following = [r["midpoint"] for r in row["marks"][venue]]
        return signs * (following - current)

    def disjoint_at_decision(row):
        first, second = [row["marks"][venue][1] for venue in venue_archive.VENUES]
        return max(first["yes_bid"], second["yes_bid"]) > min(first["yes_ask"], second["yes_ask"])

    def positive_kalshi_volume(row):
        body = row["marks"][venue_archive.KALSHI][1]["raw"]
        amount = body.get("volume_fp", body.get("volume"))
        return amount is not None and decimal(amount) > 0

    metrics, cases = [], []
    for phase in WINDOWS:
        group = [row for row in rows if row["phase"] == phase]
        wide = [row for row in group if abs(gap(row, 1)) >= 0.05]
        metrics.append({"window": phase, "price_pairs": len(group), "gap_at_least_five_points": len(wide),
                        "gap_cohort_independent_dates": len({r["date"] for r in wide}),
                        "gap_cohort_station_days": len({(r["station"], r["date"]) for r in wide}),
                        "nonoverlapping_bid_ask_ranges": sum(disjoint_at_decision(r) for r in wide),
                        "kalshi_prior_hour_with_positive_transaction_volume": sum(positive_kalshi_volume(r) for r in wide),
                        "gap_narrowed_next_hour": sum(abs(gap(r, 2)) < abs(gap(r, 1)) for r in wide),
                        "gap_widened_next_hour": sum(abs(gap(r, 2)) > abs(gap(r, 1)) for r in wide),
                        "date_weighted_abs_gap_delta": _day_mean(wide, contraction),
                        "date_block_gap_delta_interval": _date_interval(wide, contraction),
                        "polymarket_mean_directional_move_toward_peer": _day_mean(wide, lambda row: toward_peer(row, venue_archive.POLY)),
                        "kalshi_mean_directional_move_toward_peer": _day_mean(wide, lambda row: toward_peer(row, venue_archive.KALSHI))})
        if phase != "test":
            continue
        seen = set()
        for row in sorted(wide, key=lambda row: -abs(abs(gap(row, 2)) - abs(gap(row, 1)))):
            key = row["station"], row["date"]
            if key in seen:
                continue
            seen.add(key)
            market = row["markets"][venue_archive.POLY]
            other = row["markets"][venue_archive.KALSHI]
            cases.append({"station": row["station"], "date": row["date"], "at": row["at"], "venue": venue_archive.POLY,
                          "instrument_id": market["instrument_id"], "related_instrument_id": other["instrument_id"],
                          "title": f'{row["station"]} {market["label"]} · next-hour gap',
                          "detail": f'The absolute venue midpoint gap changed from {abs(gap(row, 1)):.1%} to {abs(gap(row, 2)):.1%} after one hour.',
                          "data": {**row, "bid_ask_ranges_disjoint": disjoint_at_decision(row),
                                   "kalshi_prior_hour_positive_volume": positive_kalshi_volume(row)}})
            if len(cases) == 6:
                break
    return metrics, cases


def report(data):
    rows, coverage = assemble(data)
    metrics, selected = [], {}
    for venue in venue_archive.VENUES:
        outcomes, chosen = evaluate(rows, venue)
        metrics.extend(outcomes)
        selected[venue] = chosen
    gap, cases = convergence(rows)
    record = {"version": VERSION, "title": "Do comparable temperature prices converge after one hour?",
              "hypothesis": "The other venue’s price improves an earlier-selected forecast of the next midpoint for the same temperature interval.",
              "target": "Hourly change of exact native daily-high or daily-low midpoint proxies.",
              "venues": list(venue_archive.VENUES), "date_window": {"from": WINDOWS["fit"][0], "through": WINDOWS["test"][1], **WINDOWS},
              "metrics": [*metrics, *gap], "cases": cases, "coverage": [
                  {"window": phase, "family": family, "disposition": reason, "rows": number}
                  for (phase, family, reason), number in sorted(coverage.items())],
              "limits": ["Exact station, date and whole-degree thresholds align. Polymarket US’s climate day supplies the decision clock. Kalshi’s settlement reporting and fallback rules remain independent.",
                         "Venue archives preserve event times and later local retrieval. A common 60-second proxy is used for quote availability.",
                         "Archived display marks and candle closes provide price evidence without execution clocks and depth."],
              "provenance": {"engine": VERSION, "sources": SOURCES, "local_standard_hours": HOURS, "ridge_alpha": 5,
                             "selection_freeze": "Earlier date-weighted midpoint MAE", "selected_before_test": selected,
                             "comparison_gap_floor": 0.05, "date_bootstrap_draws": 2500, "date_bootstrap_seed": 20261115}}
    return research_model.write_record(data, record, {"paired_price_windows": rows, "coverage": record["coverage"]})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=venue_archive.DEFAULT_DATA)
    args = parser.parse_args()
    path = report(args.data)
    record = json.loads(path.read_bytes())
    print(json.dumps({"path": str(path), "metrics": record["metrics"], "coverage": record["coverage"]}, indent=2))


if __name__ == "__main__":
    main()
