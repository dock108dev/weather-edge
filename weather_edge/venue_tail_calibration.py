"""Test whether displayed extreme temperature probabilities survive later native payouts."""

import argparse
from collections import Counter, defaultdict
from datetime import date, timedelta
from functools import cache
import gzip
import json
import math
from pathlib import Path
import random
from statistics import mean
import sys

from sklearn import __version__ as sklearn_version
from sklearn.linear_model import LogisticRegression

from . import expansion_weather, research_model, venue_archive, venue_benchmark, venue_contracts
from .market import iso, parse_time

VERSION = "venue-display-tail-calibration-v1"
FIT = ("2026-05-01", "2026-09-30")
HOLD = ("2026-10-01", "2026-11-14")
CLOCKS = (9, 13, 17)
CUTS = (.95, .99)
TARGETS = {"polymarket": "poly_payout", "kalshi": "kalshi_payout"}
LABELS = {"polymarket": "Polymarket US", "kalshi": "Kalshi"}


def gather(data):
    groups = defaultdict(lambda: defaultdict(list))
    for market in venue_benchmark._source_rows(data, days=(FIT[0], HOLD[1])):
        interval = venue_contracts.temperature_window(market)
        if interval and venue_benchmark._binary_result(market) is not None:
            groups[(market["day"], market["station"], market["family"], *interval)][market["venue"]].append(market)

    @cache
    def annual(station):
        if station in expansion_weather.STATIONS and expansion_weather.cli_file(data, station, 2026).is_file():
            return expansion_weather.daily_labels(data, station, date.fromisoformat(FIT[0]), date.fromisoformat(HOLD[1]))
        return {}

    @cache
    def archive(station, day):
        path = data / "archive" / day / f"{station}.json"
        return json.loads(path.read_text()) if path.is_file() else {}

    @cache
    def label_at(station, day, family):
        return venue_benchmark._label(day, family, archive(station, day), annual(station))

    timeline, missing = [], Counter()
    available = Counter()
    with venue_archive.connect(data) as db:
        db.execute("BEGIN")
        for (day, station, family, low, high), group in sorted(groups.items(), key=lambda item: str(item[0])):
            if any(len(group[venue]) != 1 for venue in venue_archive.VENUES):
                missing["one venue lacks a unique supported pair"] += 1
                continue
            phase = "fit" if day <= FIT[1] else "later"
            left, right = group[venue_archive.POLY][0], group[venue_archive.KALSHI][0]
            if not left["climate_starts_at"] or any(not market["opened_at"] or not market["closed_at"] for market in (left, right)):
                missing["trading or climate clock unavailable"] += 1
                continue
            available[f"{phase} / {family}"] += 1
            label = label_at(station, day, family)
            value = label["actual_f"] if label else None
            physical = (int((low is None or value >= low) and (high is None or value <= high))
                        if value is not None and float(value).is_integer() else None)
            for hour in CLOCKS:
                stamp = parse_time(left["climate_starts_at"]) + timedelta(hours=hour)
                at = stamp + venue_benchmark.WAIT
                if any(not parse_time(market["opened_at"]) <= at < parse_time(market["closed_at"]) for market in (left, right)):
                    missing[f"{phase} / outside listed clock"] += 1
                    continue
                left_price, left_gap = venue_benchmark._history_point(db, left, stamp)
                right_price, right_gap = venue_benchmark._history_point(db, right, stamp)
                if left_gap or right_gap:
                    for side, gap in (("Polymarket US", left_gap), ("Kalshi", right_gap)):
                        if gap:
                            missing[f"{phase} / {side} / {gap}"] += 1
                    continue
                timeline.append({"date": day, "station": station, "family": family, "hour_lst": hour, "at": iso(at),
                                 "bounds": [low, high], "label": label, "outcome": physical,
                                 "polymarket_price": left_price, "kalshi_price": right_price,
                                 "polymarket_market": left, "kalshi_market": right,
                                 "polymarket": left_price["probability"], "kalshi": right_price["probability"],
                                 "poly_payout": venue_benchmark._binary_result(left),
                                 "kalshi_payout": venue_benchmark._binary_result(right)})
    return {"version": VERSION, "timeline": timeline, "support_before_clock_filters": dict(available),
            "missing": dict(missing), "settings": {"fit": FIT, "held": HOLD, "clocks": CLOCKS, "cutoffs": CUTS,
                                                    "maximum_width": venue_benchmark.MAX_WIDTH}}


def _fit(earlier, held, venue):
    if len({row["outcome"] for row in earlier}) != 2:
        return None
    counts = Counter(row["date"] for row in earlier)
    weights = [len(earlier) / len(counts) / counts[row["date"]] for row in earlier]
    model = LogisticRegression(C=1).fit([[venue_benchmark._logit(row[venue])] for row in earlier],
                                       [row["outcome"] for row in earlier], sample_weight=weights)
    if held:
        probabilities = model.predict_proba([[venue_benchmark._logit(row[venue])] for row in held])
        for row, (_, calibrated) in zip(held, probabilities, strict=True):
            row[venue + "_fit"] = float(calibrated)
    return {"intercept": float(model.intercept_[0]), "logit_slope": float(model.coef_[0][0]), "C": 1,
            "training_target": "Published whole-degree physical threshold"}


def _tail_summary(rows, venue, direction, cutoff):
    target = TARGETS[venue]
    sample = [(row, round(row[venue] if direction == "YES" else 1 - row[venue], 10),
               row[target] if direction == "YES" else 1 - row[target]) for row in rows]
    chosen = [(row, p, y) for row, p, y in sample if p >= cutoff]
    by_date = defaultdict(list)
    for row, forecast, settled in chosen:
        by_date[row["date"]].append((forecast, settled))
    dates = [mean(int(settled == 0) for _, settled in group) for _, group in sorted(by_date.items())]
    rng = random.Random(63)
    bootstraps = sorted(mean(rng.choices(dates, k=len(dates))) for _ in range(1200)) if any(dates) else []
    return {"confidence_cutoff": cutoff, "side": direction, "venue": LABELS[venue],
            **venue_benchmark._stats([row for row, _, _ in chosen]),
            "raw_misses": sum(y == 0 for _, _, y in chosen),
            "calendar_dates_with_a_miss": sum(item > 0 for item in dates),
            "expected_raw_misses": sum(1 - p for _, p, _ in chosen),
            "mean_display_probability": mean(p for _, p, _ in chosen) if chosen else None,
            "observed_native_win_rate": mean(y for _, _, y in chosen) if chosen else None,
            "equal_date_miss_rate": mean(dates) if dates else None,
            "descriptive_95_pct_date_bootstrap": [bootstraps[30], bootstraps[1169]] if bootstraps else None,
            "zero_event_one_sided_95_pct_date_bound": 1 - math.pow(.05, 1 / len(dates)) if dates and not any(dates) else None}


def _case(row, venue, confidence, actual, direction):
    left, right = (row["polymarket_market"], row["kalshi_market"]) if venue == "polymarket" else (
        row["kalshi_market"], row["polymarket_market"],
    )
    return {"venue": left["venue"], "related_venue": right["venue"], "instrument_id": left["instrument_id"],
            "related_instrument_id": right["instrument_id"], "station": row["station"], "date": row["date"], "at": row["at"],
            "title": f"{row['station']} · {left['label']} · {direction} {confidence:.1%}",
            "detail": (f"{LABELS[venue]} showed a {confidence:.1%} {direction} midpoint proxy at {row['hour_lst']:02}:05 "
                       f"local standard time. The independently verified {direction} payout was ${actual}."), "data": row}


def evaluate(data, corpus):
    frozen, digest = research_model.freeze_inputs(data, corpus)
    earlier = [row for row in corpus["timeline"] if row["date"] <= FIT[1] and row["family"] == "daily-high" and row["outcome"] is not None
               and row["label"]["publication_at"] < HOLD[0] + "T00:00:00Z"]
    held = [dict(row) for row in corpus["timeline"] if HOLD[0] <= row["date"] <= HOLD[1]]
    highs = [row for row in held if row["family"] == "daily-high"]
    fit = {venue: _fit(earlier, highs, venue) for venue in TARGETS}
    groups = {"All later held exact pairs": held, "Daily high only": highs,
              "Daily low only": [row for row in held if row["family"] == "daily-low"]}
    for hour in CLOCKS:
        groups[f"{hour:02}:05 local standard time"] = [row for row in held if row["hour_lst"] == hour]
    for station in sorted({row["station"] for row in held}):
        groups[station] = [row for row in held if row["station"] == station]
    panels = [{"group": group, **_tail_summary(rows, venue, side, cutoff)}
              for group, rows in groups.items() for venue in TARGETS for side in ("YES", "NO") for cutoff in CUTS]
    metrics = []
    for venue, target in TARGETS.items():
        for method, suffix in ((venue, "raw midpoint"), (venue + "_fit", "earlier fitted calibration")):
            for name, rows in (("Same later high native payout", highs),
                               ("Same later high native payout / raw confidence ≥95%", [row for row in highs if max(row[venue], 1-row[venue]) >= .95])):
                selected = [row for row in rows if method in row]
                if selected:
                    metrics.append({"group": name, "model": f"{LABELS[venue]} {suffix}",
                                    "brier": mean(venue_benchmark._date_errors(selected, method, target).values()),
                                    **venue_benchmark._stats(selected)})
    candidates = []
    for row in held:
        for venue, target in TARGETS.items():
            yes = row[venue] >= .5
            p, actual, direction = (row[venue], row[target], "YES") if yes else (1-row[venue], 1-row[target], "NO")
            if p >= .95:
                candidates.append((actual, -p, venue, direction, row))
    cases = []
    seen = set()
    for actual, neg, venue, direction, row in sorted(candidates, key=lambda item: item[:4]):
        if (row["station"], row["date"]) in seen:
            continue
        cases.append(_case(row, venue, -neg, actual, direction))
        seen.add((row["station"], row["date"]))
        if len(cases) == 4:
            break
    record = {"version": VERSION, "title": "How reliable were the two venues' 95% and 99% temperature prices?",
              "hypothesis": "Earlier physical-outcome calibration may make extreme displayed probabilities more reliable on later native payouts.",
              "target": "Either venue's verified YES and NO cash outcomes for exact matched temperature thresholds",
              "date_window": {"fit dates": FIT, "untouched later dates": HOLD}, "venues": ["Polymarket US", "Kalshi"],
              "coverage": {"earlier official physical training": venue_benchmark._stats(earlier),
                           "held matched prices and both cash outcomes": venue_benchmark._stats(held),
                           "later pairs with differing cash outcomes": sum(row["poly_payout"] != row["kalshi_payout"] for row in held),
                           "paired native instruments before exact clock filters": corpus["support_before_clock_filters"]},
              "metrics": metrics, "reliability": panels, "fit": fit, "missing": corpus["missing"], "cases": cases,
              "limits": ["The public display midpoint requires a coherent bid and ask within 30¢. Historical depth and available order size remain unobserved.",
                         "Date-grouped counts keep repeated thresholds, multiple cities and multiple clocks in one calendar-date cluster.",
                         "A no-miss upper bound concerns the chance of at least one miss on a sampled date, assuming independent dates. Synoptic weather can persist across dates.",
                         "The calibration fits earlier published daily physical highs. Later validation separately uses each venue's native cash threshold. Later daily lows show descriptives only."],
              "provenance": {"frozen_input": str(frozen.relative_to(data)), "frozen_input_sha256": digest,
                             "source_sha256": research_model.run_sources(sys.modules[__name__], venue_benchmark),
                             "scikit_learn": sklearn_version, "settings": corpus["settings"]}}
    return research_model.write_record(data, record, {"reliability": panels, "held": held})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--input", type=Path)
    args = parser.parse_args(argv)
    corpus = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else gather(args.data)
    print(evaluate(args.data, corpus))


if __name__ == "__main__":
    main()
