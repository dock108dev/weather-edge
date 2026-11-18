"""Replay settled native daily-low instruments from the frozen weather model."""

from __future__ import annotations

import argparse
import statistics
from bisect import bisect_right
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

from . import expansion_contract, research_model
from .nbm import climate_window, utc

VERSION = "daily-low-market-v1"
MAX_SAMPLE_AGE = timedelta(seconds=60)
PRICE_DOCS = "https://docs.polymarket.us/api-reference/price-history/get-price-history"


def display_probability(observation):
    """A midpoint inside the provider's documented implied bid and ask."""
    if observation is None or observation["yes"] is None or observation["no"] is None:
        return None
    bid = 1 - observation["no"]
    ask = observation["yes"]
    if bid > ask + 1e-9:
        return None
    return (bid + ask) / 2


def matching_price(rows, stamps, at):
    index = bisect_right(stamps, at) - 1
    if index < 0 or at - utc(rows[index]["at"]) > MAX_SAMPLE_AGE:
        return None
    return rows[index]


def _selectors(model_path):
    import json

    data = json.loads(model_path.read_text())
    return min(data["selection_metrics"], key=lambda row: row["bucket_brier"])["method"]


def _contract_cases(data, inputs):
    start, end = map(date.fromisoformat, inputs["windows"]["test"])
    followup = date.fromisoformat(inputs["follow_up_case_only"])
    requests = [(station, day, inputs["evidence"][station]["days"][str(day)]["label"])
                for day in (*research_model._days(start, end), followup)
                for station in research_model.STATIONS]
    def retrieve(job):
        station, day, label = job
        try:
            contract = expansion_contract.low_market(data, station, day, label)
        except (OSError, KeyError, TypeError, ValueError) as error:
            return None, [{"station": station, "date": str(day), "scope": "market and payout",
                           "error": expansion_contract.availability_error(error)}]
        opens, _ = climate_window(day, research_model.expansion_weather.STATIONS[station][3])
        first = opens + timedelta(hours=min(research_model.HOURS), minutes=-1)
        last = opens + timedelta(hours=max(research_model.HOURS))
        proven = min(last, utc(contract["event_end_at"]), utc(contract["official_report"]["published_at"]))
        gaps = []
        for bucket in contract["buckets"]:
            try:
                prices, url = expansion_contract.load_price_history(data, contract["id"], bucket, first, proven)
            except (OSError, KeyError, TypeError, ValueError) as error:
                gaps.append({"station": station, "date": str(day), "instrument": bucket["id"],
                             "scope": "price history", "error": expansion_contract.availability_error(error)})
                continue
            bucket.update({"prices": prices, "price_source": url})
            if not prices:
                gaps.append({"station": station, "date": str(day), "instrument": bucket["id"],
                             "scope": "price history", "error": "Empty public history"})
        return contract, gaps

    with ThreadPoolExecutor(max_workers=4) as pool:
        yield from pool.map(retrieve, requests)


def _replay(fitted, inputs, event):
    station, day = event["station"], event["date"]
    weather = inputs["evidence"][station]["days"][day]
    begins, _ = climate_window(day, research_model.expansion_weather.STATIONS[station][3])
    buckets = [{key: value for key, value in bucket.items() if key != "prices"} for bucket in event["buckets"]]
    official = event["official_report"]
    upper_clock = min(utc(event["event_end_at"]), utc(official["published_at"]))
    decisions = []
    for hour in research_model.HOURS:
        at = begins + timedelta(hours=hour)
        if not utc(event["starts_at"]) <= at < upper_clock:
            continue
        snap = research_model.make_snapshot(station, date.fromisoformat(day), at,
                                            weather["forecasts"], weather["observations"])
        if snap:
            decisions.append({**snap, "actual_f": official["low_f"]})
    forecasts = research_model.predictions(fitted, decisions, {(station, day): buckets})
    history = {}
    for bucket in event["buckets"]:
        prices = bucket.get("prices", [])
        history[bucket["id"]] = (prices, [utc(row["available_at"]) for row in prices])
    rows = []
    for row in forecasts:
        at = utc(row["at"])
        for index, bucket in enumerate(buckets):
            prices, stamps = history[bucket["id"]]
            price = matching_price(prices, stamps, at)
            rows.append({"venue": "polymarket-us", "station": station, "date": day, "at": row["at"],
                         "instrument_id": bucket["id"], "bucket": bucket, "payout_yes": bucket["payout_yes"],
                         "market_display": price, "market_midpoint": display_probability(price),
                         "forecast": row["forecast"], "observed_min_f": row["observed_min_f"],
                         "latest_observation": row["latest_observation"], "official_report": official,
                         "probabilities": {key: estimate["probabilities"][index]
                                           for key, estimate in row["estimates"].items()}})
    return rows


def _metrics(rows, group):
    if not rows:
        return []
    truth = [row["payout_yes"] for row in rows]
    methods = list(research_model.METHODS.items())
    if all(row["market_midpoint"] is not None for row in rows):
        methods.append(("market_midpoint", "Polymarket US display midpoint"))
    counts = {"calendar_dates": len({row["date"] for row in rows}),
              "station_days": len({(row["station"], row["date"]) for row in rows}),
              "snapshots": len({(row["station"], row["date"], row["at"]) for row in rows}), "bucket_cases": len(rows)}
    output = []
    for method, label in methods:
        p = [row["market_midpoint"] if method == "market_midpoint" else row["probabilities"][method] for row in rows]
        output.append({"method": method, "label": label, "group": group, **counts,
                       "bucket_brier": statistics.mean((prob - y) ** 2 for prob, y in zip(p, truth, strict=True))})
    return output


def _cases(events, decisions, selected):
    by_id = defaultdict(list)
    for row in decisions:
        by_id[row["instrument_id"]].append(row)
    examples = []
    # Open the later exact payouts first if there is also timestamped market evidence.
    ordered = sorted(events, key=lambda event: (event["date"], event["station"]), reverse=True)
    for event in ordered:
        winning = next(bucket for bucket in event["buckets"] if bucket["payout_yes"])
        timeline = by_id[winning["id"]]
        if not timeline:
            continue
        decision = max(timeline, key=lambda row: (row["market_midpoint"] is not None, row["at"]))
        p = decision["probabilities"][selected]
        dwell = decision["market_midpoint"]
        text = f"NWS low {event['official_report']['low_f']}°F. {winning['label']} paid YES $1. Model assigned {p:.1%}."
        if dwell is not None:
            text += f" Market display midpoint was {dwell:.1%}."
        examples.append({"station": event["station"], "date": event["date"], "venue": "polymarket-us",
                         "instrument_id": winning["id"], "at": decision["at"],
                         "title": f"{event['station']} {event['date']} · {winning['label']}°F",
                         "detail": text, "official_report": event["official_report"], "market": decision["bucket"],
                         "forecast": decision["forecast"], "market_display": decision["market_display"],
                         "decision_timeline": [{"at": row["at"], "selected_yes": row["probabilities"][selected],
                                                "noaa_yes": row["probabilities"]["noaa"], "market_midpoint": row["market_midpoint"],
                                                "observed_min_f": row["observed_min_f"], "forecast": row["forecast"],
                                                "market_display": row["market_display"]} for row in timeline]})
        if len(examples) == 5:
            break
    return examples


def study(data, input_path=None):
    model_path, fitted, inputs = research_model.study(data, input_path)
    selected = _selectors(model_path)
    events, missing, decisions = [], [], []
    for event, issues in _contract_cases(data, inputs):
        missing.extend(issues)
        if event:
            events.append(event)
            decisions.extend(_replay(fitted, inputs, event))
    rows = [row for row in decisions if row["date"] <= inputs["windows"]["test"][1]]
    paired = [row for row in rows if row["market_midpoint"] is not None]
    market_metrics = _metrics(rows, "verified payout")
    market_metrics.extend(_metrics(paired, "matched display quote"))
    for station in research_model.STATIONS:
        market_metrics.extend(_metrics([row for row in paired if row["station"] == station],
                                      f"{station} · matched display quote"))
    record = {
        "version": VERSION, "title": "Polymarket US daily-low replay",
        "hypothesis": "The weather model selected on earlier dates improves on NOAA and can be compared with the exchange on identical settled buckets.",
        "target": "Exact daily minimum markets at KNYC and KMDW", "venues": ["polymarket-us"],
        "date_window": {**inputs["windows"], "case-only follow-up": [inputs["follow_up_case_only"]]},
        "metrics": market_metrics, "cases": _cases(events, decisions, selected),
        "limits": [
            "Exchange payouts come from the explicit binary settlement endpoint. Contract market-end metadata is scheduled; actual payout availability is absent.",
            "We retain the complete CLI published by scheduled instrument expiry, compare every listed bucket with its exact terms, and require every exchange payout to agree.",
            "Display history is book-derived. We use the uncrossed midpoint of YES ask and one minus NO ask with a one-minute availability allowance. There is no historical depth or paper fill in this report.",
            "The frozen low model uses an assumed ten-minute receipt lag for archived ASOS readings. Model selection uses October 15–27 bucket Brier, before the market test dates.",
        ],
        "provenance": {"weather_model_report": str(model_path.relative_to(data)),
                       "weather_model_sha256": research_model._digest(model_path), "pretest_selected": selected,
                       "price_history_docs": PRICE_DOCS,
                       "price_sample_max_age_seconds": MAX_SAMPLE_AGE.total_seconds(),
                       "source_sha256": research_model.run_sources(__import__(__name__, fromlist=["study"]), expansion_contract)},
        "coverage": {"requested_station_days": len(research_model.STATIONS) * (1 + (date.fromisoformat(inputs["windows"]["test"][1])
                     - date.fromisoformat(inputs["windows"]["test"][0])).days + 1),
                     "verified_station_days": len(events), "verified_instruments": sum(len(e["buckets"]) for e in events),
                     "stored_native_display_rows": sum(len(b.get("prices", [])) for e in events for b in e["buckets"]),
                     "test_bucket_cases": len(rows), "matched_display_cases": len(paired)},
        "missing": missing,
    }
    return research_model.write_record(data, record, {"events": events, "decisions": decisions})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path(".data"))
    parser.add_argument("--from-input", type=Path)
    args = parser.parse_args()
    print(study(args.data_dir, args.from_input))


if __name__ == "__main__":
    main()
