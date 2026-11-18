"""Test whether as-issued NOAA improves a coherent historical market probability."""

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import math
from pathlib import Path
import statistics

from . import study
from .market import iso, parse_time
from .strategy_runs import DEFAULT_DATA, POLYMARKET, digest, json_bytes, write_new


ENGINE = "market-anchor-study-v1"
FIT_DATES = ("2026-11-03", "2026-11-08")
TEST_DATES = ("2026-11-10", "2026-11-13")
FIT_LABEL_CUTOFF = "2026-11-10T00:00:00Z"
BLEND_GRID = (0.0, 0.25, 0.5, 0.75, 1.0)
MIN_SUM, MAX_SUM = 0.8, 1.2
MAX_LAG = timedelta(minutes=1)


def event_inputs(data):
    paths = sorted((data / "archive").glob("????-??-??/K???.json"))
    for path in paths:
        if not FIT_DATES[0] <= path.parent.name <= TEST_DATES[1]:
            continue
        content = path.read_bytes()
        yield path, sha256(content).hexdigest(), json.loads(content)


def eligible_prices(bucket, *, midpoint=False):
    quotes = []
    for quote in bucket.get("prices", []):
        yes = quote.get("yes")
        if yes is None or not 0 <= yes <= 1:
            continue
        if midpoint:
            no = quote.get("no")
            if no is None or not 0 <= no <= 1:
                continue
            bid = 1 - no
            if bid > yes + 1e-9 or (bid == 0 and yes == 1):
                continue
            yes = (bid + yes) / 2
        at = parse_time(quote["at"])
        receipt = parse_time(quote["available_at"]) if quote.get("available_at") else at + MAX_LAG
        if at <= receipt:
            quotes.append((receipt, at, yes))
    return sorted(quotes)


def normalized_prices(raw):
    total = math.fsum(raw)
    if MIN_SUM <= total <= MAX_SUM:
        return [value / total for value in raw]
    return None


def event_cases(event, artifact, window, rejected, *, midpoint=False):
    buckets = event["buckets"]
    if event["station"] not in artifact["stations"] or study._partition(buckets) != "exhaustive":
        rejected["event without a complete exact-bucket weather match"] += 1
        return
    winner = (event.get("settlement") or {}).get("winning_bucket")
    if winner is None or any(bucket.get("result") is None for bucket in buckets) or sum(b["result"] for b in buckets) != 1:
        rejected["event without verified binary payout"] += 1
        return
    if window == "development":
        published = event["settlement"].get("payout_available_at")
        if not published or parse_time(published) > parse_time(FIT_LABEL_CUTOFF):
            rejected["development payout publication unavailable at lock"] += 1
            return
    price_histories = [eligible_prices(bucket, midpoint=midpoint) for bucket in buckets]
    times = [[q[0] for q in history] for history in price_histories]
    end = parse_time(event.get("trading_closed_at") or event.get("market_end_at") or event["ends_at"])
    begin = parse_time(event["starts_at"])
    for at in study._decision_times(event["date"], event["standard_utc_offset_hours"]):
        if not begin <= at < end or at < parse_time(artifact["evidence_cutoff"]):
            continue
        chosen = []
        for history, history_times in zip(price_histories, times, strict=True):
            index = bisect_right(history_times, at) - 1
            if index >= 0 and at - history[index][1] <= MAX_LAG:
                chosen.append(history[index])
        if len(chosen) != len(buckets):
            rejected["decision without timely price on each bucket"] += 1
            continue
        raw = [q[2] for q in chosen]
        normalized = normalized_prices(raw)
        if normalized is None:
            rejected["decision with incomplete-looking display price mass"] += 1
            continue
        snapshot = study._event_snapshot(event, at)
        if snapshot is None:
            rejected["decision without as-issued exact station forecast"] += 1
            continue
        noaa = study._bucket_estimates(artifact, snapshot, "noaa", buckets)["buckets"]
        yield {
            "window": window, "date": event["date"], "station": event["station"], "event_id": event["id"],
            "at": iso(at), "quote_available_at": iso(max(q[0] for q in chosen)),
            "forecast_issued_at": snapshot["forecast"]["issued_at"],
            "forecast_available_at": snapshot["forecast"]["available_at"],
            "raw_mass": math.fsum(raw),
            "payout_available_at": event["settlement"].get("payout_available_at"),
            "buckets": [
                {"instrument_id": bucket["id"], "title": bucket["label"], "outcome": float(bucket["result"]),
                 "raw_yes": q[2], "market": market, "noaa": estimate["probability"]}
                for bucket, q, market, estimate in zip(buckets, chosen, normalized, noaa, strict=True)
            ],
        }


def bucket_probability(row, weight):
    return (1 - weight) * row["market"] + weight * row["noaa"]


def score(decisions, weight):
    """Equal station-day weighting, then equal decision and bucket weighting."""
    groups = defaultdict(list)
    for decision in decisions:
        losses = [(bucket_probability(bucket, weight) - bucket["outcome"]) ** 2
                  for bucket in decision["buckets"]]
        groups[(decision["date"], decision["station"])].append(statistics.mean(losses))
    return statistics.mean(statistics.mean(values) for values in groups.values()) if groups else None


def metrics_for(window, records, weight):
    return {
        "window": window, "weather_weight": weight, "brier": score(records, weight),
        "calendar_dates": len({r["date"] for r in records}),
        "station_days": len({(r["date"], r["station"]) for r in records}),
        "decisions": len(records), "buckets": sum(len(r["buckets"]) for r in records),
    }


def report(data, *, midpoint=False):
    artifact = study.load_selected(data)
    if artifact is None:
        raise ValueError("The saved NOAA benchmark model is required.")
    source = {}
    rejected = {"development": Counter(), "later test": Counter()}
    panels = {name: [] for name in rejected}
    for path, file_hash, event in event_inputs(data):
        if FIT_DATES[0] <= event["date"] <= FIT_DATES[1]:
            window = "development"
        elif TEST_DATES[0] <= event["date"] <= TEST_DATES[1]:
            window = "later test"
        else:
            continue
        source[str(path.relative_to(data))] = file_hash
        panels[window].extend(event_cases(event, artifact, window, rejected[window], midpoint=midpoint))
    settings = {"engine": ENGINE, "artifact_vintage": artifact["vintage"], "sources": source,
                "fit_dates": FIT_DATES, "test_dates": TEST_DATES, "fit_payout_cutoff": FIT_LABEL_CUTOFF,
                "weight_grid": BLEND_GRID, "bounds_on_raw_price_mass": (MIN_SUM, MAX_SUM),
                "receipt_lag_seconds": MAX_LAG.total_seconds()}
    if midpoint:
        settings["price_proxy"] = "midpoint of YES display and one minus NO display"
    selected = min(BLEND_GRID, key=lambda weight: score(panels["development"], weight)) if panels["development"] else 0
    test = panels["later test"]
    market, blend = score(test, 0), score(test, selected)
    enabled = blend is not None and market is not None and selected > 0 and blend < market
    cases = []
    closest = sorted(test, key=lambda row: (row["date"], row["station"], row["at"]))
    for station in sorted({r["station"] for r in closest}):
        choices = [row for row in closest if row["station"] == station]
        worst = max(choices, key=lambda r: max(abs(b["market"] - b["noaa"]) for b in r["buckets"]))
        instrument = max(worst["buckets"], key=lambda b: abs(b["market"] - b["noaa"]))
        cases.append({**worst, "venue": POLYMARKET, "instrument_id": instrument["instrument_id"],
                      "title": f"{worst['station']} {instrument['title']} · historical display disagreement",
                      "detail": f"Market proxy {instrument['market']:.1%}, NOAA {instrument['noaa']:.1%}, "
                                f"actual YES payout {instrument['outcome']:.2f}."})
    overall = [
        {**metrics_for(window, values, weight),
         "method": label}
        for window, values in panels.items()
        for label, weight in (("Market proxy", 0), ("Locked blend", selected), ("NOAA", 1))
    ]
    by_day = [
        {**metrics_for(day, subset, selected), "market_brier": score(subset, 0), "noaa_brier": score(subset, 1)}
        for day in sorted({r["date"] for r in test})
        if (subset := [row for row in test if row["date"] == day])
    ]
    stamped = iso(datetime.now(timezone.utc))
    result = {
        "id": "anchor-" + digest(settings)[:20],
        "title": ("Does NOAA improve Polymarket US display midpoints?" if midpoint
                  else "Does NOAA improve Polymarket US display asks?"),
        "hypothesis": "On later exact settled buckets, a development-selected NOAA blend improves the market proxy Brier score.",
        "target": "Daily high temperature bucket",
        "venues": [POLYMARKET],
        "created_at": stamped,
        "date_window": {"from": FIT_DATES[0], "fit_through": FIT_DATES[1],
                        "test_from": TEST_DATES[0], "through": TEST_DATES[1]},
        "metrics": overall,
        "cases": cases,
        "limits": [
            "Display prices are a probability proxy. They carry no fill-size evidence.",
            ("An exhaustive bucket vector uses contemporaneous YES asks and NO asks to reconstruct YES midpoints."
             if midpoint else "The market comparison normalizes contemporaneous YES display asks across an exhaustive bucket partition."),
            "Historical dates were reused from the broader NOAA benchmark. The later dates were untouched by this blend selection.",
        ],
        "evidence": {"excluded": {k: dict(v) for k, v in rejected.items()}},
        "selection": {"weather_weight": selected, "paper_strategy_enabled": enabled},
        "development_sweep": [metrics_for("development", panels["development"], w) for w in BLEND_GRID],
        "later_dates": by_day,
        "provenance": settings,
        "panels": panels,
    }
    path = data / "research" / "models" / f"{result['id']}.json"
    write_new(path, json_bytes(result) + b"\n")
    return result, path


def main():

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", type=Path, default=DEFAULT_DATA)
    ap.add_argument("--midpoint", action="store_true", help="Benchmark reconstructed bid–ask midpoints from both display sides.")
    args = ap.parse_args()
    result, path = report(args.data, midpoint=args.midpoint)
    fields = ("id", "metrics", "selection", "development_sweep", "later_dates", "evidence")
    print(json.dumps({"path": str(path), **{field: result[field] for field in fields}}, indent=2))


if __name__ == "__main__":
    main()
