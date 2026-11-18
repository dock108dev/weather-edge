"""Evaluate and replay the Central Park measurable precipitation threshold."""

from __future__ import annotations

import argparse
import math
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

import numpy as np
from sklearn import __version__ as sklearn_version
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from . import expansion_contract, expansion_nbm, expansion_weather, research_model, research_model_low_market
from .nbm import climate_window, iso, utc

VERSION = "central-park-rain-v1"
STATION = "KNYC"
HOUR_LST = -3
METHODS = {
    "climatology": "Earlier measurable precipitation frequency",
    "nbm": "NOAA P06 overlap proxy",
    "calibrated": "Logistic calibration of NOAA P06",
    "qpf": "Calibrated P06 + NOAA Q06 total",
}


def estimate_rain_window(card, starts, ends):
    """Overlap P06/Q06 intervals with the exact CLI liquid-equivalent day."""
    pieces = []
    for row in card["points"]:
        if "P06" not in row or "Q06" not in row or not 0 <= row["P06"] <= 100 or not 0 <= row["Q06"] < 999:
            continue
        until = utc(row["valid_at"])
        since = until - timedelta(hours=6)
        begin, end = max(starts, since), min(ends, until)
        if begin < end:
            pieces.append((begin, end, since, until, row["P06"], row["Q06"]))
    pieces.sort()
    cursor = starts
    for begin, end, *_ in pieces:
        if cursor != begin:
            return None
        cursor = end
    if cursor != ends:
        return None
    fractions = [(end - begin).total_seconds() / 21600 for begin, end, *_ in pieces]
    ratio = 1 - math.prod((1 - pop / 100) ** fraction
                          for fraction, (*_, pop, qpf) in zip(fractions, pieces, strict=True))
    total = sum(qpf / 100 * fraction for fraction, (*_, qpf) in zip(fractions, pieces, strict=True))
    return {
        "issued_at": card["issued_at"], "available_at": card["available_at"],
        "source": card["source"], "card_sha256": card["card_sha256"], "p06_overlap_probability": ratio,
        "weighted_q06_inches": total,
        "source_periods": [{"starts_at": iso(since), "ends_at": iso(until), "pop_percent": pop,
                            "amount_inches": qpf / 100, "overlap_fraction": fraction}
                           for fraction, (_, _, since, until, pop, qpf) in zip(fractions, pieces, strict=True)],
    }


def _weather(data, day, station=STATION):
    starts, ends = climate_window(day, expansion_weather.STATIONS[station][3])
    decision = starts + timedelta(hours=HOUR_LST)
    for hours in (0, -6, -12, -18, -24, -30, -36):
        issue = decision.replace(hour=(decision.hour // 6) * 6) + timedelta(hours=hours)
        card = expansion_nbm.load_card(data, station, issue)
        if card is None or not utc(card["issued_at"]) <= utc(card["available_at"]) <= decision:
            continue
        forecast = estimate_rain_window(card, starts, ends)
        if forecast:
            return {"station": station, "date": str(day), "at": iso(decision), "forecast": forecast,
                    "climate_starts_at": iso(starts), "climate_ends_at": iso(ends)}
    return None


def _rule_report(data, day, label):
    report, _, _ = expansion_contract.rain_report(data, day, label["product"])
    return report


def _inputs(data):
    windows = research_model.WINDOWS
    first, end = windows["fit"][0], research_model.FOLLOW_UP
    source = expansion_weather.daily_labels(data, STATION, first, end)
    research = {key: [] for key in windows}
    gaps = []
    candidates = [(name, day) for name, (first_day, stop) in windows.items() for day in research_model._days(first_day, stop)]
    candidates.append(("case_only", end))

    def build(job):
        split, day = job
        label = source.get(str(day))
        forecast = _weather(data, day)
        if not label or not forecast:
            return split, day, None, "Missing complete label or full-day NBS window"
        # Verify every selection and test threshold against its governing text.
        if split in {"selection", "test", "case_only"}:
            try:
                label = _rule_report(data, day, label)
            except (OSError, KeyError, TypeError, ValueError) as error:
                return split, day, None, expansion_contract.availability_error(error)
        result = expansion_contract.rain_outcome(label["precip_as_reported"]) if label else None
        if result is None:
            return split, day, None, "No governing climate report or no interpretable precipitation field"
        return split, day, {**forecast, "truth": result, "official_report": label}, None

    research["case_only"] = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        for split, day, row, gap in pool.map(build, candidates):
            if row:
                research[split].append(row)
            else:
                gaps.append({"split": split, "date": str(day), "reason": gap})
    return {"version": VERSION, "windows": research_model._window_strings(),
            "rows": research, "missing": gaps,
            "official_summary": research_model._source_entry(data, expansion_weather.cli_file(data, STATION, first.year))}


def _features(row, qpf):
    forecast = row["forecast"]
    p = min(0.995, max(0.005, forecast["p06_overlap_probability"]))
    values = [math.log(p / (1 - p))]
    if qpf:
        values.append(math.log1p(forecast["weighted_q06_inches"] / 0.01))
    return values


def _predict(fit_rows, rows):
    targets = [row["truth"] for row in fit_rows]
    output = {
        "climatology": [statistics.mean(targets)] * len(rows),
        "nbm": [row["forecast"]["p06_overlap_probability"] for row in rows],
    }
    for method, qpf in (("calibrated", False), ("qpf", True)):
        model = make_pipeline(StandardScaler(), LogisticRegression(C=1, random_state=20261115))
        model.fit([_features(row, qpf) for row in fit_rows], targets)
        output[method] = [float(p) for _, p in model.predict_proba([_features(row, qpf) for row in rows])]
    return [{**row, "probabilities": {method: values[index] for method, values in output.items()}}
            for index, row in enumerate(rows)]


def _scores(rows, group):
    methods = list(METHODS.items())
    if rows and all(row.get("market_midpoint") is not None for row in rows):
        methods.append(("market_midpoint", "Polymarket US display midpoint"))
    return [{"method": method, "label": label, "group": group, "calendar_dates": len(rows),
             "station_days": len(rows), "yes_days": sum(row["truth"] for row in rows),
             "brier": statistics.mean((p - y) ** 2 for p, y in
                                      [(row["market_midpoint"] if method == "market_midpoint"
                                        else row["probabilities"][method], row["truth"]) for row in rows])}
            for method, label in methods] if rows else []


def _market_cases(data, rows, refresh_day=None):
    cases, gaps = [], []
    for row in rows:
        day = date.fromisoformat(row["date"])
        at = utc(row["at"])
        try:
            market = expansion_contract.rain_market(data, day, row["official_report"], refresh=day == refresh_day)
        except (OSError, KeyError, TypeError, ValueError) as error:
            gaps.append({"date": row["date"], "scope": "market payout", "error": expansion_contract.availability_error(error)})
            continue
        bucket = market["buckets"][0]
        quote = None
        if utc(market["starts_at"]) <= at < utc(market["scheduled_end_at"]):
            try:
                prices, source = expansion_contract.load_price_history(data, market["id"], bucket,
                                                                        at - timedelta(minutes=5), at + timedelta(minutes=5))
                quote = research_model_low_market.matching_price(prices, [utc(px["available_at"]) for px in prices], at)
                bucket["price_source"] = source
                if quote is None:
                    gaps.append({"date": row["date"], "scope": "price history", "error": "No display observation at decision"})
            except (OSError, KeyError, TypeError, ValueError) as error:
                gaps.append({"date": row["date"], "scope": "price history", "error": expansion_contract.availability_error(error)})
        cases.append({**row, "market": bucket, "official_report": market["official_report"],
                      "truth": bucket["payout_yes"], "market_display": quote, "market_detail": market,
                      "market_midpoint": research_model_low_market.display_probability(quote)})
    return cases, gaps


def study(data, input_path=None, refresh_day=None):
    import gzip
    import json

    data = Path(data)
    inputs = json.loads(gzip.decompress(input_path.read_bytes())) if input_path else _inputs(data)
    frozen, digest = research_model.freeze_inputs(data, inputs)
    rows = inputs["rows"]
    report_groups = {split: _predict(rows["fit"], cases)
                     for split, cases in rows.items() if split in {"calibration", "selection", "test", "case_only"} and cases}
    selection = _scores(report_groups["selection"], "pretest selection")
    chosen = min(selection, key=lambda rec: rec["brier"])["method"]
    test = report_groups["test"]
    bets, issues = _market_cases(data, [*test, *report_groups.get("case_only", [])], refresh_day)
    test_markets = [row for row in bets if row["date"] <= inputs["windows"]["test"][1]]
    matched = [row for row in test_markets if row["market_midpoint"] is not None]
    summary = [*_scores(test, "all eligible weather days"), *_scores(test_markets, "verified test payouts"),
               *_scores(matched, "matched test display quotes")]
    cases = []
    for row in reversed(bets[-6:]):
        quote = row["market_midpoint"]
        text = (f"Forecast {row['probabilities'][chosen]:.1%} at 21:00 LST. "
                f"Verified CLI precipitation {row['official_report']['precip_as_reported']} inches. "
                f"YES payout ${row['truth']}.")
        if quote is not None:
            text += f" Display midpoint {quote:.1%}."
        cases.append({"station": row["station"], "date": row["date"], "venue": "polymarket-us",
                      "instrument_id": row["market"]["id"], "at": row["at"],
                      "title": f"Central Park {row['date']} · 0.01 inches",
                      "detail": text, "forecast": row["forecast"], "market": row["market"],
                      "official_report": row["official_report"], "market_display": row["market_display"]})
    day_diffs = np.asarray([(row["probabilities"][chosen] - row["truth"]) ** 2 -
                            (row["probabilities"]["nbm"] - row["truth"]) ** 2 for row in test])
    bootstrap = np.random.default_rng(20261115).choice(day_diffs, size=(2500, len(day_diffs))).mean(axis=1)
    record = {
        "version": VERSION, "title": "Measurable precipitation at Central Park",
        "hypothesis": "Calibrating NOAA six-hour probability and amount improves the probability of at least 0.01 inches in the exact Central Park climate day.",
        "target": "Central Park liquid-equivalent precipitation ≥ 0.01 inches",
        "venues": ["polymarket-us"], "date_window": inputs["windows"], "metrics": summary, "cases": cases,
        "limits": [
            "All forecasts are available by 21:00 LST on the prior day. NOAA P06/Q06 are six-hour windows. Edge intervals get proportional overlap and the P06 baseline assumes independent interval probabilities and uniform within-interval risk.",
            "Threshold verification uses the governing raw NWS CLI available by 14:00 ET next day, with the first eligible later product fallback. Trace and M/MM count as No. Liquid-equivalent precipitation includes frozen forms.",
            "Training labels use IEM's frozen summary records. Selection and test labels use separately checked original CLI reports. Historical marks have no eligible depth for simulated fills.",
            "The single-station sample is small. The date bootstrap is an uncertainty estimate, especially for wet events.",
        ],
        "provenance": {"input_path": str(frozen.relative_to(data)), "input_sha256": digest, "pretest_selected": chosen,
                       "decision_hour_lst": HOUR_LST, "versions": {"scikit-learn": sklearn_version, "numpy": np.__version__},
                       "source_sha256": research_model.run_sources(sys.modules[__name__], expansion_contract,
                                                                  expansion_weather, expansion_nbm),
                       "settings": {"logistic_C": 1, "logit_bound": [0.005, 0.995], "bootstrap_seed": 20261115}},
        "coverage": {split: len(group) for split, group in rows.items()} |
                    {"verified_market_payouts": len(bets), "matched_test_prices": len(matched)},
        "selection": selection,
        "selected_delta_brier_vs_nbm": {"delta": float(day_diffs.mean()),
                                         "interval_95": [float(x) for x in np.quantile(bootstrap, [0.025, 0.975])]},
        "missing": [*inputs["missing"], *issues],
    }
    return research_model.write_record(data, record, {"by_split": report_groups, "market_cases": bets})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path(".data"))
    parser.add_argument("--from-input", type=Path)
    parser.add_argument("--refresh-day", type=date.fromisoformat)
    args = parser.parse_args()
    print(study(args.data_dir, args.from_input, args.refresh_day))


if __name__ == "__main__":
    main()
