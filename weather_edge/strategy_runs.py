"""Freeze and run comparable paper strategies on the captured weather book tape."""

import argparse
from bisect import bisect_right
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import gzip
from hashlib import sha256
import json
import math
from pathlib import Path
import statistics
from uuid import uuid4

from . import collector, study
from .capture import load_captures
from .market import decimal, iso, paper_trial, parse_time
from .paper_runs import DEFAULT_DATA, FEE_SCHEDULE, _fee_at, _freeze
from .strategy_rules import (
    FRESHNESS, RULES, StrategyRule, book_status, conservative_no, legacy_recent_observation,
    recent_observation,
)


ENGINE = "weather-strategy-study-v1"
POLYMARKET = "polymarket-us"
RUN_SPLIT = "2026-11-15T17:07:28Z"
RECHECK_SECONDS = 15


@dataclass(frozen=True)
class Settings:
    order_size: int = 25
    reaction_seconds: int = 30
    minimum_dollars: float = 0.25


# Vary one control at a time around the prior study's $25, 30-second case.
FIXED = Settings()
SWEEP = (
    FIXED,
    Settings(order_size=10),
    Settings(reaction_seconds=5),
    Settings(reaction_seconds=15),
    Settings(reaction_seconds=45),
    Settings(reaction_seconds=60),
    Settings(minimum_dollars=0.75),
)
EXECUTED_RULES = ("no-trade", "noaa-threshold", "high-no", "two-sided", "observation")
LOW_STRATEGIES = {"live-low": "primary", "joint-high-low": "primary", "guarded-low": "guarded",
                  "joint-high-guarded-low": "guarded", "joint-late-guarded-high-low": "guarded",
                  "first-receipt-low": "first-receipt", "joint-late-high-first-receipt-low": "first-receipt",
                  "joint-transfer-high-guarded-low": "guarded", "joint-dew-point-high-guarded-low": "guarded",
                  "joint-hrrr-high-guarded-low": "guarded", "joint-native-noaa-high-guarded-low": "guarded"}
LOW_ONLY = frozenset({"live-low", "guarded-low", "first-receipt-low"})
LOW_PROBABILITIES = {"primary": "low_probabilities", "guarded": "guarded_low_probabilities",
                     "first-receipt": "first_receipt_low_probabilities"}
HIGH_PROBABILITIES = {"guarded": "late_guarded_high_probabilities", "transfer": "late_transfer_high_probabilities",
                      "dew-point": "dew_point_high_probabilities", "hrrr": "hrrr_high_probabilities",
                      "native-noaa": "native_noaa_high_probabilities"}
SHADOW_RULES = {"transfer": "joint-transfer-high-guarded-low", "dew-point": "joint-dew-point-high-guarded-low",
                "hrrr": "joint-hrrr-high-guarded-low", "native-noaa": "joint-native-noaa-high-guarded-low"}
LATE_HIGH_RULES = frozenset({"joint-late-guarded-high-low", "joint-late-high-first-receipt-low", *SHADOW_RULES.values()})
SELECTION = (
    "Choose on the development window only. Without at least two fully settled station-days "
    "for a setting at the boundary, keep the predefined setting. Otherwise use settled P&L, "
    "then smaller drawdown, then the predefined grid order."
)


def json_bytes(value):
    return json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()


def digest(value):
    return sha256(json_bytes(value)).hexdigest()


def write_new(path, content):
    """Publish a reproducible artifact atomically and preserve any existing version."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{uuid4().hex}.tmp"
    try:
        tmp.write_bytes(content)
        try:
            path.hardlink_to(tmp)
        except FileExistsError:
            pass
    finally:
        tmp.unlink(missing_ok=True)


def slice_tape(capture, lower, upper):
    events = []
    for event in capture["events"]:
        buckets = [
            {**bucket, "books": [book for book in bucket["books"]
                                 if lower <= parse_time(book["at"]) <= upper]}
            for bucket in event["buckets"]
        ]
        if any(bucket["books"] for bucket in buckets):
            events.append({**event, "buckets": buckets})
    return {**capture, "events": events, "started_at": iso(lower), "captured_at": iso(upper)}


class Predictor:
    def __init__(self, artifact, anchor=None, events=(), low=None, *, high=None, shadows=None, first_observation_receipt=False):
        self.artifact = artifact
        self.cache = {}
        self.anchor = MarketAnchor(anchor, events) if anchor else None
        self.low = low or {}
        self.high = high
        self.high_shadows = {SHADOW_RULES[k]: predict for k, predict in (shadows or {}).items()}
        self.first_observation_receipt = first_observation_receipt

    def __call__(self, name):
        if name == "no-trade":
            return lambda *_: {}
        if name == "market-weather":
            return lambda event, at: (self.anchor.apply(event, at, self.estimates("noaa", event, at))
                                      if event.get("family", "daily-high") == "daily-high" else {})

        def predict(event, at):
            family = event.get("family", "daily-high")
            if family == "daily-low" and name in LOW_STRATEGIES:
                lower_method = LOW_STRATEGIES[name]
                key = lower_method, event["id"], at
                if key not in self.cache:
                    self.cache[key] = self.low[lower_method](event, at) or {}
                return self.cache[key]
            if family != "daily-high" or name in LOW_ONLY:
                return {}
            if name in self.high_shadows and (estimate := self.high_shadows[name](event, at)):
                return estimate
            if name in LATE_HIGH_RULES and self.high:
                if estimate := self.high(event, at):
                    return estimate
            return self.estimates("ridge" if name == "observation" else "noaa", event, at,
                                  buffer=name == "high-no")

        return predict

    def estimates(self, method, event, at, *, buffer=False):
        key = method, event["id"], at
        if key not in self.cache:
            self.cache[key] = self._estimate(method, event, at)
        result = self.cache[key]
        return {instrument: conservative_no(estimate) for instrument, estimate in result.items()} if buffer else result

    def _estimate(self, method, event, at):
        artifact = self.artifact
        if event["station"] not in artifact["stations"] or at < parse_time(artifact["evidence_cutoff"]):
            return {}
        snapshot = study._event_snapshot(event, at)
        if snapshot is None:
            return {}
        forecasts = study._bucket_estimates(artifact, snapshot, method, event["buckets"])["buckets"]
        by_id = {item["id"]: item for item in forecasts}
        observation_rule = recent_observation if self.first_observation_receipt else legacy_recent_observation
        observation = observation_rule(event, at) if method == "ridge" else None
        if observation:
            observed_at, received = (parse_time(observation[key]) for key in ("at", "available_at"))
            earlier = [row for row in event["observations"] if parse_time(row["at"]) != observed_at
                       or (self.first_observation_receipt and parse_time(row["available_at"]) < received)]
            counterfactual = {**event, "observations": earlier}
            without = study._event_snapshot(counterfactual, at)
            if without is not None:
                paired = study._bucket_estimates(artifact, without, method, event["buckets"])["buckets"]
                for item in paired:
                    estimate = by_id[item["id"]]
                    estimate["strategy_context"] = {
                        "observation": observation,
                        "yes_probability_before_reading": item["probability"],
                        "yes_probability_change": estimate["probability"] - item["probability"],
                    }
        forecast = snapshot["forecast"]
        return {ident: {**estimate, "model_method": method,
                        "model_version": artifact["version"], "model_vintage": artifact["vintage"],
                        "forecast_issued_at": forecast["issued_at"],
                        "forecast_available_at": forecast["available_at"]}
                for ident, estimate in by_id.items()}


class MarketAnchor:
    """Normalize simultaneous executable-depth mids across one exhaustive event."""

    def __init__(self, settings, events):
        self.settings = settings
        self.contexts = {}
        self.cache = {}
        for event in events:
            if event.get("family", "daily-high") != "daily-high" or study._partition(event["buckets"]) != "exhaustive":
                continue
            buckets = {}
            for bucket in event["buckets"]:
                samples = []
                for book in bucket["books"]:
                    at = parse_time(book["at"])
                    bids, offers = book.get("bids"), book.get("offers")
                    if bids and offers and book_status(book, None, at, "origin-confirmed") is None:
                        if decimal(bids[0][0]) < decimal(offers[0][0]):
                            samples.append((at, (float(bids[0][0]) + float(offers[0][0])) / 2))
                buckets[bucket["id"]] = ([at for at, _ in samples], [mid for _, mid in samples])
            self.contexts[event["id"]] = buckets

    def apply(self, event, at, model):
        key = event["id"], at
        if key not in self.cache:
            self.cache[key] = self._blend(event, at, model)
        return self.cache[key]

    def _blend(self, event, at, model):
        buckets = self.contexts.get(event["id"], {})
        max_lag = timedelta(seconds=self.settings["max_book_age_seconds"])
        matched = {}
        for instrument, (times, mids) in buckets.items():
            index = bisect_right(times, at) - 1
            if index >= 0 and at - times[index] <= max_lag:
                matched[instrument] = (times[index], mids[index])
        if not matched or len(matched) != len(buckets):
            return model
        mass = math.fsum(mid for _, mid in matched.values())
        low, high = self.settings["raw_mass"]
        if not low <= mass <= high:
            return model
        weight = self.settings["weather_weight"]
        return {instrument: {
            **estimate,
            "probability": estimate["probability"] * weight + matched[instrument][1] / mass * (1 - weight),
            "model_method": "market-midpoint-noaa-blend",
            "strategy_context": {
                "market_midpoint_raw": matched[instrument][1],
                "market_midpoint_normalized": matched[instrument][1] / mass,
                "market_vector_raw_mass": mass, "weather_probability": estimate["probability"],
                "weather_weight": weight, "book_oldest_at": iso(min(t for t, _ in matched.values())),
            },
        } for instrument, estimate in model.items()}


def book_audit(capture):
    statuses, ages, clocks, cache_control = Counter(), Counter(), Counter(), Counter()
    eligibility = {policy: Counter() for policy in FRESHNESS}
    source_lag, source_lag_origin, receipt_spacing = [], [], []
    dates, station_days, venues, instruments = set(), set(), set(), set()
    payout_proofs = 0
    cutoff = parse_time(capture["captured_at"])
    for event in capture["events"]:
        for bucket in event["buckets"]:
            venue = event.get("venue", POLYMARKET)
            if bucket.get("settlement") and parse_time(bucket["settlement"]["available_at"]) <= cutoff:
                payout_proofs += 1
            if not bucket["books"]:
                continue
            venues.add(venue)
            instruments.add((venue, bucket["id"]))
            station_days.add((event["station"], event["date"]))
            dates.add(event["date"])
            previous = None
            for book in bucket["books"]:
                at = parse_time(book["at"])
                http = book.get("http_cache", {})
                statuses[http.get("cf-cache-status", "unknown")] += 1
                cache_control[http.get("cache-control", "unknown")] += 1
                age = book.get("cache_age_seconds")
                ages["missing" if age is None else str(age)] += 1
                clocks[book.get("source_time_progress", "unknown")] += 1
                origin = book_status(book, None, at, "origin-confirmed")
                if book.get("source_at"):
                    lag = (at - parse_time(book["source_at"])).total_seconds()
                    source_lag.append(lag)
                    if origin is None:
                        source_lag_origin.append(lag)
                if previous is not None:
                    receipt_spacing.append((at - previous).total_seconds())
                previous = at
                eligibility["origin-confirmed"][origin or "eligible"] += 1
                eligibility["legacy"][book_status(book, None, at, "legacy") or "eligible"] += 1

    def percentiles(values):
        if not values:
            return {}
        values = sorted(values)
        return {str(p): round(values[int((len(values) - 1) * p / 100)], 3) for p in (0, 25, 50, 75, 95, 100)}

    return {
        "book_receipts": sum(statuses.values()),
        "venues": sorted(venues),
        "calendar_dates": sorted(dates),
        "station_days": len(station_days),
        "instruments": len(instruments),
        "settlement_proofs": payout_proofs,
        "http_cache_status": dict(statuses),
        "http_cache_control": dict(cache_control),
        "http_age_seconds": dict(ages),
        "exchange_timestamp_progress": dict(clocks),
        "receipt_minus_exchange_seconds_percentiles": percentiles(source_lag),
        "origin_receipt_minus_exchange_seconds_percentiles": percentiles(source_lag_origin),
        "same_instrument_poll_seconds_percentiles": percentiles(receipt_spacing),
        "eligibility": {policy: dict(counts) for policy, counts in eligibility.items()},
    }


def measure(result, *, detailed=False, portfolio=False):
    attempts, positions = result["attempts"], result["positions"]
    timeline = result["cash_over_time"]
    raw_counts = Counter(attempt["result"] for attempt in attempts)
    gaps = Counter()
    for attempt in attempts:
        if attempt["reaction_at"] is None:
            gaps.update(attempt.get("recheck_gaps", {}))
    peak_equity = result["start_bankroll"]
    max_drawdown = 0
    for point in timeline:
        equity_at_cost = point["cash"] + point["cash_committed"]
        peak_equity = max(peak_equity, equity_at_cost)
        max_drawdown = max(max_drawdown, peak_equity - equity_at_cost)
    cost_by_day = Counter()
    delays = []
    same_exchange_clock = 0
    for position in positions:
        cost_by_day[" / ".join(position["group"])] += position["max_loss"]
    for attempt in attempts:
        if attempt["reaction_at"]:
            delays.append((parse_time(attempt["reaction_at"]) - parse_time(attempt["signal_at"])).total_seconds())
            same_exchange_clock += (attempt["quote_source_at"] == attempt["reaction_quote"]["quote_source_at"])
    metrics = {
        "eligible_books": result["coverage"]["eligible_books"],
        "modeled_books": result["coverage"]["modeled_books"],
        "signals": result["signals"], "distinct_signal_sides": result["distinct_signal_sides"],
        "rechecked": result["rechecked"], "persisted": result["persisted"],
        "recheck_same_exchange_clock": same_exchange_clock,
        "median_recheck_delay_seconds": round(statistics.median(delays), 3) if delays else None,
        "missing_rechecks": raw_counts["Missing eligible recheck"],
        "awaiting_rechecks": raw_counts["Awaiting eligible recheck"],
        "price_or_probability_killed_edge": raw_counts["Edge did not persist"],
        "recheck_probability_gap": raw_counts["Recheck probability unavailable or uncertain"],
        "recheck_liquidity_gap": raw_counts["Insufficient size at recheck"],
        "fills": result["fills"], "settled_fills": result["settled"],
        "settled_station_days": result["settled_station_days"],
        "average_stake": round(statistics.mean([p["max_loss"] for p in positions]), 4) if positions else 0,
        "forecast_ev_dollars": round(sum(p["ev_dollars"] for p in positions), 4),
        "committed_at_end": result["cash_committed"],
        "peak_committed": max((point["cash_committed"] for point in timeline), default=0),
        "cash_at_end": result["cash"],
        "realized_pnl": result["realized_pnl"],
        "max_drawdown_at_cost": round(max_drawdown, 4),
        "highest_station_day_cost": round(max(cost_by_day.values(), default=0), 4),
        "highest_station_day_cost_share": round(max(cost_by_day.values()) / sum(cost_by_day.values()), 4) if cost_by_day else 0,
        "attempt_outcomes": dict(raw_counts),
        "missing_recheck_receipt_reasons": dict(gaps),
    }
    if detailed:
        matched = [attempt for attempt in attempts if attempt["reaction_at"]]
        canceled = [attempt for attempt in matched if attempt["result"] == "Edge did not persist"]
        position_prices = Counter("below 10¢" if p["entry_price"] < 0.1 else
                                  "90¢ or more" if p["entry_price"] >= 0.9 else "10¢ to below 90¢"
                                  for p in positions)
        metrics.update(
            recheck_original_odds_below_new_break_even=sum(
                attempt["probability"] <= (attempt["reaction_quote"]["break_even_probability"] or 0)
                for attempt in canceled
            ),
            signals_with_market_context=sum(a["model_method"] == "market-midpoint-noaa-blend" for a in attempts),
            fills_with_market_context=sum(p["model_method"] == "market-midpoint-noaa-blend" for p in positions),
            entry_price_bands=dict(position_prices),
            observation_events=len({(a["station"], observation["at"])
                                    for a in attempts
                                    if (observation := a.get("strategy_context", {}).get("observation"))}),
        )
    if portfolio:
        metrics.update(
            high_signals=sum(a.get("market_family", "daily-high") == "daily-high" for a in attempts),
            low_signals=sum(a.get("market_family") == "daily-low" for a in attempts),
            high_fills=sum(p.get("market_family", "daily-high") == "daily-high" for p in positions),
            low_fills=sum(p.get("market_family") == "daily-low" for p in positions),
            low_position_days=len({tuple(p["group"]) for p in positions if p.get("market_family") == "daily-low"}),
        )
    return metrics


def run_trial(capture, predictor, name, settings, freshness, *, detailed=False, portfolio=False, fee_at=None):
    result = paper_trial(
        capture, predictor(name), reaction_seconds=settings.reaction_seconds,
        order_size=settings.order_size, bankroll=500, max_station_day=25,
        max_recheck_lag=RECHECK_SECONDS, fee_at=fee_at or _fee_at(FEE_SCHEDULE),
        rules=StrategyRule(name, freshness, settings.minimum_dollars),
    )
    return measure(result, detailed=detailed, portfolio=portfolio), result


def choose_settings(results):
    eligible = [(settings, value) for settings, value in results if value["settled_station_days"] >= 2]
    if not eligible:
        return FIXED
    chosen, _ = max(eligible, key=lambda pair: (pair[1]["realized_pnl"], -pair[1]["max_drawdown_at_cost"]))
    return chosen


def case_for(attempt, result, capture, phase, policy):
    event = next(event for event in capture["events"] if event["id"] == attempt["event_id"])
    bucket = next(bucket for bucket in event["buckets"] if bucket["id"] == attempt["bucket_id"])
    position = next((p for p in result["positions"] if p["bucket_id"] == bucket["id"]
                     and p["side"] == attempt["side"] and p["signal_at"] == attempt["signal_at"]), None)
    clocks = {at for at in (attempt["signal_at"], attempt["reaction_at"]) if at is not None}
    selected_books = [b for b in bucket["books"] if iso(b["at"]) in clocks]
    return {
        "station": event["station"], "date": event["date"], "venue": event.get("venue", POLYMARKET),
        "event_id": event["id"], "instrument_id": bucket["id"], "at": attempt["signal_at"],
        "title": f"{event['station']} {bucket['label']} {attempt['side']} · {attempt['result']}",
        "detail": f"{phase} window, {policy} · ${attempt['ev_dollars']:.2f} signal EV",
        "side": attempt["side"],
        "bucket": {field: bucket.get(field) for field in ("label", "lower_f", "upper_f", "rules", "quantity_step")},
        "decision": attempt, "books": selected_books, "position": position,
    }


def representative_cases(result, capture, phase, policy):
    ordered = sorted(result["attempts"], key=lambda a: (-a["ev_dollars"], a["signal_at"], a["bucket_id"]))
    seen, cases = set(), []
    wanted = ("Paper filled", "Edge did not persist", "Missing eligible recheck", "Insufficient size at recheck")
    for kind in wanted:
        for attempt in ordered:
            if attempt["result"] != kind:
                continue
            key = (kind, attempt["station"])
            if key in seen:
                continue
            cases.append(case_for(attempt, result, capture, phase, policy))
            seen.add(key)
            if sum(case["decision"]["result"] == kind for case in cases) >= 2:
                break
    return cases


def flat_row(metrics, *, phase, name, settings, freshness):
    return {"window": phase, "strategy": name, "freshness": freshness, **asdict(settings),
            **{key: val for key, val in metrics.items() if not isinstance(val, dict)}}


def analyze(capture, artifact, split, options=None, *, low=None, high=None, shadows=None):
    start, end = parse_time(capture["started_at"]), parse_time(capture["captured_at"])
    if not start < split <= end:
        raise ValueError("The split must follow the first receipt and precede the evidence cutoff.")
    detailed = options is not None
    portfolio = bool((options or {}).get("live_low"))
    if portfolio and low is None:
        raise ValueError("The frozen daily-low predictor is required for this run")
    earlier_end = split - timedelta(microseconds=1) if detailed else split
    stages = {"development": slice_tape(capture, start, earlier_end), "forward": slice_tape(capture, split, end)}
    anchor = (options or {}).get("market_anchor")
    predictor = Predictor(artifact, anchor, capture["events"], low, high=high, shadows=shadows,
                          first_observation_receipt=(options or {}).get("first_observation_receipt", False))
    trials = {}
    metrics, cases, sweep_rows = [], [], []
    records = {}
    policy = "origin-confirmed"

    def get_trial(phase, name, settings, freshness):
        key = phase, name, settings, freshness
        if key not in trials:
            trials[key] = run_trial(stages[phase], predictor, name, settings, freshness,
                                    detailed=detailed, portfolio=portfolio)
        return trials[key]

    names = EXECUTED_RULES
    if detailed:
        names += ("two-sided-buffered",)
    if anchor:
        names += ("market-weather",)
    if (options or {}).get("mid_price"):
        names += ("mid-price", "mid-price-buffered")
    if portfolio:
        names = ("no-trade", "two-sided", "live-low", "joint-high-low")
        if "guarded" in low:
            names += ("guarded-low", "joint-high-guarded-low")
        if high is not None:
            names += ("joint-late-guarded-high-low",)
        if "first-receipt" in low:
            names += ("first-receipt-low", "joint-late-high-first-receipt-low")
        names += tuple(SHADOW_RULES[key] for key in SHADOW_RULES if key in (shadows or {}))
    for name in names:
        settings_by_metric = [(settings, get_trial("development", name, settings, policy)[0])
                              for settings in (SWEEP if not portfolio and name not in {
                                  "no-trade", "mid-price", "mid-price-buffered",
                              } else (FIXED,))]
        sweep_rows.extend(flat_row(m, phase="development", name=name, settings=s, freshness=policy)
                          for s, m in settings_by_metric)
        settings = choose_settings(settings_by_metric)
        methods = [(phase, policy) for phase in stages]
        if name == "two-sided" and not portfolio:
            methods.extend((phase, "legacy") for phase in stages)
        for phase, freshness in methods:
            measured, result = get_trial(phase, name, settings, freshness)
            metrics.append(flat_row(measured, phase=phase, name=name, settings=settings, freshness=freshness))
            records[f"{phase}/{name}/{freshness}"] = {
                "metrics": measured, "positions": result["positions"], "cash_over_time": result["cash_over_time"],
                "station_days": result["station_days"],
            }
            examples = representative_cases(result, stages[phase], phase, freshness)
            cases.extend({**case, "strategy": name, "window": phase, "freshness": freshness} for case in examples)

    delay_rows = []
    for freshness in (() if portfolio else FRESHNESS):
        for delay in (5, 15, 30, 45, 60):
            settings = Settings(reaction_seconds=delay)
            for phase in stages:
                measured, _ = get_trial(phase, "two-sided", settings, freshness)
                delay_rows.append(flat_row(measured, phase=phase, name="two-sided", settings=settings, freshness=freshness))

    comparison = {
        "metrics": metrics, "cases": cases, "sweep": sweep_rows, "delay_sensitivity": delay_rows,
        "records": records,
        "evidence": {name: book_audit(value) for name, value in stages.items()},
    }
    if detailed and not portfolio:
        comparison["matched_signal_rechecks"] = matched_signal_metrics(get_trial, stages)
    if portfolio:
        comparison["family_evidence"] = {
            phase: {family: book_audit({**stage, "events": [e for e in stage["events"]
                                                           if e.get("family", "daily-high") == family]})
                    for family in ("daily-high", "daily-low")}
            for phase, stage in stages.items()
        }
    return comparison


def matched_signal_metrics(get_trial, stages):
    rows = []
    for phase in stages:
        for delay in (5, 15, 30, 45, 60):
            attempts = {}
            for freshness in FRESHNESS:
                _, trial = get_trial(phase, "two-sided", Settings(reaction_seconds=delay), freshness)
                attempts[freshness] = {(a["bucket_id"], a["side"], a["signal_at"]): a for a in trial["attempts"]}
            common = attempts["legacy"].keys() & attempts["origin-confirmed"].keys()
            for freshness, values in attempts.items():
                selected = [values[key] for key in common]
                checked = [a for a in selected if a["reaction_at"]]
                rows.append({
                    "window": phase, "freshness": freshness, "reaction_seconds": delay,
                    "common_signals": len(selected), "rechecked": len(checked),
                    "recheck_advanced_clock": sum(a["quote_source_at"] != a["reaction_quote"]["quote_source_at"]
                                                   for a in checked),
                    "recheck_cache_status": dict(Counter(a["reaction_quote"]["http_cache_status"] or "unknown"
                                                         for a in checked)),
                })
    return rows


def create_report(data, tape, artifact, split, *, previous=None, options=None, low=None, high=None, shadows=None):
    snapshot = {"engine": ENGINE, "capture": tape, "split": iso(split),
                "model_vintage": artifact["vintage"], "fee_schedule": FEE_SCHEDULE,
                "rules": list(EXECUTED_RULES), "window_seconds": RECHECK_SECONDS,
                "grid": [asdict(settings) for settings in SWEEP], "selection": SELECTION}
    if options is not None:
        snapshot["options"] = options
    if low is not None:
        for key, predict in low.items():
            snapshot[LOW_PROBABILITIES[key]] = freeze_book_probabilities(tape, predict)
        low = frozen_predictors(snapshot, LOW_PROBABILITIES)
    high_sources = {"guarded": high, **(shadows or {})}
    for key, predict in high_sources.items():
        if predict is not None:
            snapshot[HIGH_PROBABILITIES[key]] = freeze_book_probabilities(tape, predict, family="daily-high")
    if any(high_sources.values()):
        snapshot["scheduled_high_weather_checks"] = scheduled_high_weather_checks(tape, high_sources)
    shadows = frozen_predictors(snapshot, HIGH_PROBABILITIES)
    high = shadows.pop("guarded", None)
    ident = digest(snapshot)
    evidence_path = data / "research" / "evidence" / "strategies" / f"{ident}.json.gz"
    cutoff = tape["captured_at"]
    path = data / "research" / "strategies" / f"paper-{ident[:20]}.json"
    if path.is_file():
        return json.loads(path.read_text()), path
    write_new(evidence_path, gzip.compress(json_bytes(snapshot), mtime=0))
    result = analyze(tape, artifact, split, options, low=low, high=high, shadows=shadows)
    record = {
        "id": path.stem,
        "title": "Competing strategies on the same paper bankroll" if options is not None else "One bankroll across five paper strategies",
        "hypothesis": "An after-fee NOAA or observation signal survives the first eligible delayed book under stricter freshness evidence.",
        "target": "Daily high and low temperature buckets" if (options or {}).get("live_low") else "Daily high temperature bucket",
        "created_at": iso(datetime.now(timezone.utc)),
        "venues": sorted({event.get("venue", POLYMARKET) for event in tape["events"]}),
        "date_window": {"from": tape["started_at"], "split": iso(split), "through": cutoff},
        "limits": [
            "Development stops at the predeclared timestamp. Forward estimates share the climate date and stations with development.",
            "The paper model uses displayed depth and conservative scheduled fees. Fillability beyond the observed book is unknown.",
            "Drawdown marks open positions at acquisition cost. Intraday liquidation marks require independent executable exit evidence.",
            ("Market-plus-weather is exploratory and falls back to NOAA when the complete live vector is missing."
             if (options or {}).get("market_anchor") else "Market-plus-weather requires a separate prediction check."),
            "Two-venue shopping requires exact payout equivalence.",
        ],
        "provenance": {
            "engine": ENGINE, "tape_sha256": ident, "frozen_inputs": str(evidence_path.relative_to(data)),
            "model_vintage": artifact["vintage"], "policies": FRESHNESS, "rules": RULES, "selection": SELECTION,
            "delay_window_seconds": RECHECK_SECONDS, "poll_interval_seconds": collector.BOOK_INTERVAL,
            "fee_schedule": FEE_SCHEDULE, "previous_run": previous,
            **({"options": options} if options is not None else {}),
        },
        **result,
    }
    if shadows:
        record["model_tape_coverage"] = model_tape_coverage(snapshot)
    if "scheduled_high_weather_checks" in snapshot:
        record["scheduled_high_weather_checks"] = [{"event_id": row["event_id"], "station": row["station"], "model": row["model"],
                                                      "decision_at": row["decision_at"], "probability_rows": len(row["estimates"]),
                                                      "supported_probability_rows": sum(not b.get("uncertain") for b in row["estimates"].values())}
                                                     for row in snapshot["scheduled_high_weather_checks"]]
    write_new(path, json_bytes(record) + b"\n")
    return record, path


def low_predictor(data, vintage=None, *, guarded=False, first_receipt=False):
    if first_receipt:
        from . import expansion_live_low_receipts as model
    elif guarded:
        from . import expansion_live_precision as model
    else:
        from . import expansion_live_low as model
    artifact = model.load(data, vintage=vintage)
    if artifact is None or (vintage is not None and artifact["vintage"] != vintage):
        raise ValueError("The required earlier-trained daily-low artifact is unavailable.")

    def predict(event, at):
        result = model.predict(artifact, event, at)
        if not result:
            return {}
        forecast = result.get("forecast") or {}
        context = {key: result.get(key) for key in ("observed_min_f", "latest_observation", "local_standard_decision_hour", "knowledge_at")}
        if first_receipt:
            context.update({key: result.get(key) for key in ("scheduled_knowledge_at", "scheduled_vintage", "metar_upper_bound_f",
                            "scheduled_upper_bound_f", "receipt_status", "bound_receipt_update", "qualification_study_id")})
        return {row["id"]: {
            **row, "model_method": result.get("method", "daily-low-selected"),
            "model_version": result.get("version"), "model_vintage": artifact["vintage"],
            "forecast_issued_at": forecast.get("issued_at", result.get("issued_at")),
            "forecast_available_at": forecast.get("available_at", result.get("available_at")),
            "strategy_context": {**context, "forecast": forecast},
        } for row in result["buckets"]}

    return artifact["vintage"], predict


def freeze_book_probabilities(tape, predict, *, family="daily-low"):
    probabilities = {}
    for event in tape["events"]:
        if event.get("family", "daily-high") != family:
            continue
        expiry = parse_time(event["ends_at"])
        times = {
            received for bucket in event["buckets"] for book in bucket["books"]
            if (received := parse_time(book["at"])) < expiry and book_status(book, None, received, "origin-confirmed") is None
        }
        probabilities[event["id"]] = {iso(t): predict(event, t) for t in sorted(times)}
    return probabilities


def scheduled_high_weather_checks(tape, candidates):
    """Retain the fixed late weather decision even when no book qualifies at that station."""
    available = []
    cutoff = parse_time(tape["captured_at"])
    for event in tape["events"]:
        if event.get("family", "daily-high") != "daily-high":
            continue
        marked = parse_time(event["climate_starts_at"]) + timedelta(hours=17)
        if marked > cutoff:
            continue
        for name, model in candidates.items():
            if model is not None and (estimates := model(event, marked)):
                available.append({"event_id": event["id"], "station": event["station"], "model": name,
                                  "decision_at": iso(marked), "estimates": estimates})
    return available


def frozen_low_predictor(probabilities):
    return lambda event, at: probabilities[event["id"]][iso(at)]


def frozen_predictors(snapshot, keys):
    return {name: frozen_low_predictor(snapshot[key]) for name, key in keys.items() if key in snapshot}


def late_high_predictor(data):
    from . import strategy_metar_high
    from .strategy_remaining_paper import alternate_source

    artifact = strategy_metar_high.load_runtime(data)
    if artifact is None:
        raise ValueError("The required six-hour high guard artifact is unavailable.")
    alternative = alternate_source(artifact, precision=True)

    def predict(event, at):
        earliest = parse_time(event["climate_starts_at"]) + timedelta(hours=17)
        return alternative(event, at) if at >= earliest else {}

    return artifact["vintage"], predict


def shadow_high_predictor(data, model, hours):
    artifact = model.load_runtime(data)
    if artifact is None:
        raise ValueError(f"The required {model.RUNTIME_VERSION} artifact is unavailable.")
    stations = artifact.get("stations") or artifact["residuals_at_17"]

    def run(event, anchor):
        result = model.predict_runtime(artifact, event, anchor)
        if not result:
            return {}
        remaining = result["context"]["forecast"]["remaining"]
        context = {**result["context"], "knowledge_at": result["knowledge_at"],
                   "local_standard_decision_hour": result["local_standard_decision_hour"],
                   "model_high_f": result["model_high_f"]}
        return {b["id"]: {**b, "model_method": result["method"], "model_vintage": artifact["vintage"],
                          "model_version": result["version"], "forecast_available_at": remaining["available_at"],
                          "forecast_issued_at": remaining["issued_at"], "strategy_context": context}
                for b in result["buckets"]}

    return artifact["vintage"], cached_high_shadow(stations, hours, run)


def hrrr_high_predictor(data):
    from . import expansion_live_hrrr_high as model

    artifact = model.load(data)
    if artifact is None:
        raise ValueError("The required HRRR and NBM shadow artifact is unavailable.")

    def run(event, anchor):
        decisions = model.timeline(artifact, event, anchor)
        if not decisions:
            return {}
        result = decisions[-1]
        context = {k: v for k, v in result.items() if k != "buckets"}
        by_bucket = {}
        for bucket in result["buckets"]:
            blend = bucket["blend"]
            by_bucket[bucket["id"]] = {**blend, "id": bucket["id"], "model_method": "equal-hrrr-and-nbm-remaining-high",
                "model_version": model.VERSION, "model_vintage": artifact["vintage"], "validation_days": blend["calibration_days"],
                "forecast_issued_at": result["nbm"]["issued_at"], "forecast_available_at": result["nbm"]["received_at"],
                "strategy_context": {**context, "thermal_probabilities": {k: bucket[k] for k in ("nbm", "hrrr", "blend")}}}
        return by_bucket

    return artifact["vintage"], cached_high_shadow(artifact["residuals"], artifact["hours"], run)


def native_noaa_high_predictor(data):
    from datetime import date
    from .model import _limits, _probability
    from .strategy_dewpoint import _live_nbs

    def run(event, anchor):
        weather = _live_nbs({"data": data}, event["station"], date.fromisoformat(event["date"]), anchor)
        if weather is None:
            return {}
        card, daily = weather
        point, spread = daily["TXN"], daily["XND"]
        return {b["id"]: {"id": b["id"], "probability": _probability(_limits(b), [point], spread),
                          "model_method": "raw-noaa-nbs-high-reference", "model_version": "native-daily-nbs-v1",
                          "model_vintage": card["card_sha256"], "forecast_issued_at": card["issued_at"],
                          "forecast_available_at": card["received_at"], "strategy_context": {
                              "raw_high_f": point, "raw_high_spread_f": spread, "knowledge_at": iso(anchor),
                              "source": card["source"], "forecast_published_at": card["available_at"], "valid_at": daily["valid_at"]}}
                for b in event["buckets"]}

    return cached_high_shadow(("KPHL", "KMDW"), (13, 17), run)


def cached_high_shadow(stations, hours, run):
    cache = {}

    def predict(event, at):
        if event["station"] not in stations:
            return {}
        start, end = (parse_time(event[key]) for key in ("climate_starts_at", "climate_ends_at"))
        clocks = (start + timedelta(hours=hour) for hour in hours)
        anchor = max((clock for clock in clocks if clock <= at), default=None)
        if anchor is None or at >= end:
            return {}
        ident = event["id"], anchor
        if ident not in cache:
            cache[ident] = run(event, anchor)
        return cache[ident]

    return predict


def model_tape_coverage(snapshot):
    events = {event["id"]: event for event in snapshot["capture"]["events"]}
    coverage = []
    for option, key in (*LOW_PROBABILITIES.items(), *HIGH_PROBABILITIES.items()):
        for event_id, clocks in snapshot.get(key, {}).items():
            known = [clock for clock, estimates in clocks.items() if estimates]
            if not known:
                continue
            chosen = [p for clock in known for p in clocks[clock].values()]
            coverage.append({"model": key.removesuffix("_probabilities"), "family": events[event_id].get("family", "daily-high"),
                             "event_id": event_id, "station": events[event_id]["station"], "date": events[event_id]["date"],
                             "first_local_eligible_book_with_estimate": min(known, key=parse_time),
                             "last_local_eligible_book_with_estimate": max(known, key=parse_time), "distinct_book_clocks": len(known),
                             "bucket_probability_rows": len(chosen), "supported_probability_rows": sum(not p.get("uncertain") for p in chosen),
                             "method_parameter": option})
    return coverage


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", type=Path, default=DEFAULT_DATA)
    ap.add_argument("--through", help="Cut the local evidence at an explicit aware timestamp.")
    ap.add_argument("--split", default=RUN_SPLIT)
    ap.add_argument("--rerun", type=Path, help="Reproduce metrics from a frozen input gzip file.")
    ap.add_argument("--previous", help="Identify the prior record when adding a later evidence window.")
    ap.add_argument("--detailed", action="store_true", help="Add buffered side decisions and like-for-like freshness metrics.")
    ap.add_argument("--anchor", type=Path, help="Use the locked weather weight from a saved display-anchor study.")
    ap.add_argument("--live-low", action="store_true", help="Use one paper bankroll for eligible high and low decisions.")
    ap.add_argument("--guarded-low", action="store_true", help="Compare the separate precision-guarded low model as well.")
    ap.add_argument("--late-guarded-high", action="store_true", help="Add a joint guarded arm using high guard after 17:00 local standard time.")
    ap.add_argument("--first-receipt-low", action="store_true", help="Compare the low precision shadow at its first tighter report receipt.")
    ap.add_argument("--transfer-high", action="store_true", help="Compare the separate late high shadow across more exact stations.")
    ap.add_argument("--dew-point-high", action="store_true", help="Compare the observed-dew-point high shadow with the same bankroll.")
    ap.add_argument("--hrrr-high", action="store_true", help="Compare the frozen equal HRRR and NBM high shadow on the same bankroll.")
    ap.add_argument("--native-noaa-high", action="store_true", help="Compare the as-received raw NOAA reference on Philadelphia and Midway.")
    ap.add_argument("--supplemental-midway-high", action="store_true", help="Include the separately collected native Chicago Midway high tape.")
    ap.add_argument("--mid-price", action="store_true", help="Run fixed net-EV rules on average 10–90 cent prices.")
    ap.add_argument("--first-observation-receipt", action="store_true", help="Trigger observation rules only on a new reading or revision.")
    args = ap.parse_args()
    if args.rerun:
        expected = args.rerun.name.removesuffix(".json.gz")
        snapshot = json.loads(gzip.decompress(args.rerun.read_bytes()))
        if snapshot["engine"] != ENGINE or digest(snapshot) != expected:
            ap.error("Frozen input hash or paper engine version differs.")
        artifact = study.load_selected(args.data, vintage=snapshot["model_vintage"])
        if artifact is None:
            ap.error("The frozen model vintage is missing.")
        options = snapshot.get("options")
        low = frozen_predictors(snapshot, LOW_PROBABILITIES) or None
        if low is None and (options or {}).get("live_low"):
            low = {"primary": low_predictor(args.data, options["live_low"]["vintage"])[1]}
        shadows = frozen_predictors(snapshot, HIGH_PROBABILITIES)
        high = shadows.pop("guarded", None)
        result = analyze(snapshot["capture"], artifact, parse_time(snapshot["split"]), options,
                         low=low, high=high, shadows=shadows)
        previous = args.data / "research" / "strategies" / f"paper-{expected[:20]}.json"
        if previous.is_file():
            prior = json.loads(previous.read_text())
            if any(prior.get(key) != value for key, value in result.items()):
                ap.error("Saved result differs from the reproduced result.")
        print(json.dumps({"input": str(args.rerun), "verified": previous.is_file(), "metrics": result["metrics"]}, indent=2))
        return
    capture = load_captures(args.data / "captures", through=args.through)
    artifact = study.load_selected(args.data)
    if capture is None or artifact is None:
        ap.error("The local live tape and frozen model are both required.")
    cutoff = min(parse_time(capture["captured_at"]), parse_time(args.through)) if args.through else parse_time(capture["captured_at"])
    capture["events"] = [event for event in capture["events"] if event.get("family", "daily-high") == "daily-high"]
    if args.live_low and args.mid_price:
        ap.error("Run the daily-low portfolio and the middle-price sensitivity separately.")
    if args.guarded_low and not args.live_low:
        ap.error("Use --guarded-low with --live-low.")
    if args.late_guarded_high and not args.guarded_low:
        ap.error("Use --late-guarded-high with --live-low and --guarded-low.")
    if (args.first_receipt_low or args.transfer_high or args.dew_point_high or args.hrrr_high or args.native_noaa_high) and not args.late_guarded_high:
        ap.error("The shadow comparisons require --late-guarded-high.")
    options = {} if (args.detailed or args.anchor or args.live_low or args.mid_price or args.first_observation_receipt
                     or args.supplemental_midway_high) else None
    if args.first_observation_receipt:
        options["first_observation_receipt"] = True
    if args.mid_price:
        options["mid_price"] = {"min_price": 0.10, "max_price": 0.90}
    low, high, shadows = None, None, {}
    if args.live_low:
        from .strategy_native_capture import polymarket_events

        try:
            vintage, primary = low_predictor(args.data)
            low = {"primary": primary}
            if args.guarded_low:
                guarded_vintage, low["guarded"] = low_predictor(args.data, guarded=True)
                options["guarded_low"] = {"vintage": guarded_vintage}
                if args.late_guarded_high:
                    high_vintage, high = late_high_predictor(args.data)
                    options["late_guarded_high"] = {"vintage": high_vintage, "first_local_standard_hour": 17}
                    if args.first_receipt_low:
                        receipt_vintage, low["first-receipt"] = low_predictor(args.data, first_receipt=True)
                        options["first_receipt_low"] = {"vintage": receipt_vintage, "comparison": "shadow"}
                    if args.transfer_high:
                        from . import strategy_high_transfer as transfer

                        transfer_vintage, shadows["transfer"] = shadow_high_predictor(args.data, transfer, (17,))
                        options["transfer_high"] = {"vintage": transfer_vintage, "first_local_standard_hour": 17, "comparison": "shadow"}
                    if args.dew_point_high:
                        from . import strategy_dewpoint as dewpoint

                        dew_vintage, shadows["dew-point"] = shadow_high_predictor(args.data, dewpoint, (13, 17))
                        options["dew_point_high"] = {"vintage": dew_vintage, "local_standard_hours": [13, 17], "comparison": "shadow"}
                    if args.hrrr_high:
                        hrrr_vintage, shadows["hrrr"] = hrrr_high_predictor(args.data)
                        options["hrrr_high"] = {"vintage": hrrr_vintage, "local_standard_hours": [13, 17], "comparison": "shadow"}
                    if args.native_noaa_high:
                        shadows["native-noaa"] = native_noaa_high_predictor(args.data)
                        options["native_noaa_high"] = {"sources": "NBS TXN and XND", "local_standard_hours": [13, 17],
                                                       "stations": ["KPHL", "KMDW"], "comparison": "reference"}
        except (ImportError, KeyError, ValueError) as exc:
            ap.error(str(exc))
        low_events, low_issues = polymarket_events(args.data, cutoff, since=capture["started_at"])
        capture["events"].extend(low_events)
        options["live_low"] = {"vintage": vintage, "contract_import_issues": low_issues}
    if args.anchor:
        anchor = json.loads(args.anchor.read_text())
        if not anchor["selection"]["paper_strategy_enabled"]:
            ap.error("The saved anchor study did not improve the later-date market benchmark.")
        if anchor["provenance"]["artifact_vintage"] != artifact["vintage"]:
            ap.error("The live weather artifact differs from the artifact used for anchor calibration.")
        options["market_anchor"] = {
            "study": anchor["id"], "weather_weight": anchor["selection"]["weather_weight"],
            "max_book_age_seconds": 2 * collector.BOOK_INTERVAL,
            "raw_mass": anchor["provenance"]["bounds_on_raw_price_mass"],
        }
    if args.supplemental_midway_high:
        from .strategy_native_capture import merge_supplemental, polymarket_events

        future, issues = polymarket_events(args.data, cutoff, since=capture["started_at"], family="daily-high", station="KMDW")
        exact, conflicts = merge_supplemental(capture, future)
        options["supplemental_midway_high"] = {"exact_event_ids": exact, "import_issues": [*issues, *conflicts]}
    tape = _freeze(collector.enrich_capture(args.data, capture), cutoff)
    record, path = create_report(args.data, tape, artifact, parse_time(args.split), previous=args.previous,
                                 options=options, low=low, high=high, shadows=shadows)
    print(json.dumps({"id": record["id"], "path": str(path), "date_window": record["date_window"],
                      "metrics": record["metrics"], "evidence": record["evidence"]}, indent=2))


if __name__ == "__main__":
    main()
