"""Use native NOAA snowfall percentiles to audit bounded storm-threshold probabilities."""

import argparse
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
import gzip
from hashlib import sha256
from itertools import product
import json
from pathlib import Path
import re
from statistics import mean
from time import monotonic

from . import collector, research_model, strategy_snow_events, venue_archive
from .capture import FORECAST_CAP, ReadOnlyHTTP
from .expansion_nbm import _station_text
from .market import decimal, iso, parse_time
from .nbm import AWS_NBM, _station_fields
from .strategy_kalshi_history import candle_points, snapshot_at
from .strategy_runs import write_new


VERSION = "snowstorm-native-quantiles-v1"
DOCS = "https://vlab.noaa.gov/web/mdl/nbm-text-products"
QUANTILES = (0.10, 0.25, 0.50, 0.75, 0.90)
FIELDS = ("S24P1", "S24P2", "S24P5", "S24P7", "S24P9")
WINDOW = timedelta(hours=24)
PRINT_PRECISION = Decimal("0.1")


def parse_card(raw, station):
    issue, _ = _station_fields(raw, station, "nbp")
    fields = {match[1]: line for line in raw.decode("ascii").splitlines()
              if (match := re.match(r"\s*(FHR|S24P[12579])(?=\s)", line))}
    if not all(field in fields for field in ("FHR", *FIELDS)):
        raise ValueError(f"Missing NBP snowfall quantiles for {station}")
    forecasts = []
    lead_columns = [match for match in re.finditer(r"\b\d{1,3}\b", fields["FHR"])]
    for lead in lead_columns:
        left, right = lead.end() - 4, lead.end()
        values = [fields[field][left:right].strip() for field in FIELDS]
        if any(not amount.isdigit() for amount in values):
            continue
        amounts = [Decimal(value) / 10 for value in values]
        if amounts != sorted(amounts):
            raise ValueError("NBP snowfall quantiles are not ordered")
        end = issue + timedelta(hours=int(lead[0]))
        start = end - WINDOW
        if start >= issue:
            forecasts.append({"start": iso(start), "end": iso(end), "quantiles_in": [str(value) for value in amounts]})
    if not forecasts:
        raise ValueError("NBP snowfall product has no complete forecast windows")
    return {"issued_at": iso(issue), "station": station, "quantiles": forecasts,
            "card_sha256": sha256(raw).hexdigest(), "raw_card": raw.decode("ascii")}


def read_cards(data, issue, stations):
    saved, gaps = {}, {}
    target = data / "research" / "evidence" / "nbp-snow" / f"{issue:%Y%m%dT%H}"
    for station in stations:
        path = target / f"{station}.json"
        if path.is_file():
            document = json.loads(path.read_bytes())
            local = parse_card(document["raw_card"].encode("ascii"), station)
            if document["card_sha256"] != local["card_sha256"]:
                raise ValueError("Frozen snowfall card checksum mismatch")
            saved[station] = {**document, **local}
    needed = set(stations) - set(saved)
    if not needed or collector.paused(data):
        return saved, gaps
    source = f"{AWS_NBM}/blend.{issue:%Y%m%d}/{issue:%H}/text/blend_nbptx.t{issue:%H}z"
    try:
        reply = ReadOnlyHTTP(monotonic() + 25)(source, FORECAST_CAP)
        published = parsedate_to_datetime(reply.headers["last-modified"])
        stamp = {"source": source, "published_at": iso(published), "local_receipt_at": iso(reply.at),
                 "archive_object_sha256": sha256(reply.body).hexdigest(), "request_at": iso(reply.requested_at)}
        if not issue <= published <= reply.at:
            raise ValueError("NBP archive is missing a credible publication timestamp")
    except (OSError, KeyError, ValueError) as error:
        return saved, {station: str(error) for station in needed}
    for station in sorted(needed):
        try:
            station_bytes, _ = _station_text(reply.body, station, "NBP")
            card = parse_card(station_bytes, station)
            if parse_time(card["issued_at"]) != issue:
                raise ValueError("Original station issue differs from requested NOAA object")
        except (KeyError, ValueError) as error:
            gaps[station] = str(error)
            continue
        saved[station] = {**card, **stamp}
        write_new(target / f"{station}.json", json.dumps(saved[station], sort_keys=True).encode() + b"\n")
    return saved, gaps


def source_windows(rows, start, end):
    exact = []
    cursor = start
    for row in rows:
        first, last = parse_time(row["start"]), parse_time(row["end"])
        if last <= start or end <= first:
            continue
        if first > cursor or (exact and first != cursor):
            return None
        cursor = last
        exact.append({**row, "whole_contract_interval": start <= first and last <= end})
    return exact if cursor >= end else None


def _marginal_arrays(row):
    amounts = list(map(decimal, row["quantiles_in"]))
    low = [(Decimal(0), 1.0)] + [(max(Decimal(0), value - PRINT_PRECISION), 1 - probability)
                                for value, probability in zip(amounts, QUANTILES, strict=True) if value > PRINT_PRECISION]
    high = [(value + PRINT_PRECISION, 1 - probability) for value, probability in zip(amounts, QUANTILES, strict=True)]
    return low, high


def bounds(windows, threshold, observed=Decimal(0)):
    """Bonferroni bounds using published marginals without assuming independence."""
    if observed > threshold:
        return 1.0, 1.0
    remaining = threshold - observed
    ordered = [_marginal_arrays(row) for row in windows]
    lower_inputs = [low for row, (low, _) in zip(windows, ordered, strict=True) if row["whole_contract_interval"]]
    upper_inputs = [high for _, high in ordered]
    lower = max((max(0, sum(item[1] for item in combo) - (len(combo) - 1)) for combo in product(*lower_inputs)
                 if sum(item[0] for item in combo) > remaining), default=0.0)
    upper = min((min(1.0, sum(item[1] for item in combo)) for combo in product(*upper_inputs)
                 if sum(item[0] for item in combo) <= remaining), default=1.0)
    if lower > upper + 1e-8:
        raise ValueError("Published snowfall quantiles produced contradictory bounds")
    return lower, upper


def source_storms(data, saved):
    old = json.loads(saved.read_bytes())
    detail = old["provenance"]
    payload = (data / detail["replay_details_path"]).read_bytes()
    if sha256(payload).hexdigest() != detail["replay_details_sha256"]:
        raise ValueError("Storm outcome evidence checksum mismatch")
    groups = []
    for row in json.loads(gzip.decompress(payload))["station_windows"]:
        first = date.fromisoformat(row["first"]) + timedelta(days=row["complete_days"] if row["live"] else 0)
        start = strategy_snow_events._window_starts(row["station"], first, row["live"])
        end = strategy_snow_events._window_starts(row["station"], date.fromisoformat(row["last"]) + timedelta(days=1), row["live"])
        midnight = datetime.combine(first, time.min, timezone.utc)
        cycle = midnight - timedelta(days=1) + timedelta(hours=7)
        at = parse_time(old["date_window"]["through"]) if row["live"] else start
        groups.append({**row, "first_issue": cycle, "decision": at, "forecast_start": start, "forecast_end": end})
    return groups, old["id"]


def report(data, saved):
    storm_groups, prior = source_storms(data, saved)
    by_issue = defaultdict(list)
    for event in storm_groups:
        by_issue[event["first_issue"]].append(event)
    weather, unresolved = {}, []
    for issue, events in by_issue.items():
        stations = {event["station"] for event in events}
        for cycle in (issue, issue - timedelta(hours=6)):
            cards, gaps = read_cards(data, cycle, stations)
            for event in events:
                station = event["station"]
                card = cards.get(station)
                matches = source_windows(card["quantiles"], event["forecast_start"], event["forecast_end"]) if card else None
                if matches is not None and parse_time(card["published_at"]) <= event["decision"]:
                    key = station, event["first"], event["last"]
                    weather.setdefault(key, (card, matches))
            if not gaps and all((event["station"], event["first"], event["last"]) in weather for event in events):
                break
            unresolved.extend({"issue": iso(cycle), "station": station, "reason": reason} for station, reason in gaps.items())
    paired, scenarios, cases = [], [], []
    for group in storm_groups:
        key = group["station"], group["first"], group["last"]
        if key not in weather:
            unresolved.append({"station": key[0], "date": key[1], "reason": "No contiguous predecision NBP accumulation series"})
            continue
        card, windows = weather[key]
        realized_prefix = decimal(group["observed_total_in"]) if group["live"] else Decimal(0)
        decisions = []
        for row in group["scenarios"]:
            at = group["decision"]
            lo, hi = bounds(windows, decimal(row["lower"]), realized_prefix)
            estimates = candle_points(data, row["instrument_id"]) if not group["live"] else {}
            candle = snapshot_at(sorted(estimates), estimates, at) if estimates and parse_time(row["opened_at"]) < at < parse_time(row["closed_at"]) else None
            witness = {"station": key[0], "date": key[2], "first": key[1], "at": iso(at), "venue": venue_archive.KALSHI,
                       "instrument_id": row["instrument_id"], "threshold_in": row["lower"], "probability_low": lo,
                       "probability_high": hi, "market_proxy": candle, "payout": row["verified_yes_payout"],
                       "point_weather_lower": row["conditioned_lower_in"], "point_weather_upper": row["conditioned_upper_in"],
                       "observed_inches": group["observed_total_in"], "forecast": {name: value for name, value in card.items()
                                                                                    if name not in {"raw_card", "quantiles"}},
                       "partially_overlapping_windows": sum(not value["whole_contract_interval"] for value in windows),
                       "native_windows": windows}
            decisions.append(witness)
            if candle and witness["payout"] in ("0", "1"):
                paired.append(witness)
        scenarios.extend(decisions)
        pick = min(decisions, key=lambda row: abs(decimal(row["threshold_in"]) - decimal(group["observed_total_in"])))
        unit = "inch" if decimal(pick["threshold_in"]) == 1 else "inches"
        cases.append({**pick, "title": f"{key[0]} snowfall above {pick['threshold_in']} {unit}",
                      "detail": f"Archive-derived marginal bounds {pick['probability_low']:.0%} to {pick['probability_high']:.0%}. Native point forecast {pick['point_weather_lower']}–{pick['point_weather_upper']} inches."})
    metrics = [{"window": "Verified hourly market and payout", "instrument_rows": len(paired),
                "station_windows": len({(row["station"], row["first"], row["date"]) for row in paired}),
                "independent_start_dates": len({row["first"] for row in paired}),
                "market_brier": mean((row["market_proxy"]["mid"] - int(row["payout"])) ** 2 for row in paired) if paired else None,
                "nbp_bound_brier_min": mean((row["probability_low"] if row["payout"] == "0" else 1 - row["probability_high"]) ** 2 for row in paired) if paired else None,
                "nbp_bound_brier_max": mean((row["probability_high"] if row["payout"] == "0" else 1 - row["probability_low"]) ** 2 for row in paired) if paired else None,
                "midpoints_outside_bounds": sum(not row["probability_low"] <= row["market_proxy"]["mid"] <= row["probability_high"] for row in paired),
                "paper_fills": 0},
               {"window": "Open contemporary conditions", "instrument_rows": sum(row["payout"] is None for row in scenarios),
                "complete_issued_station_windows": len(weather)}]
    result = {"version": VERSION, "title": "NOAA snowfall probability bounds and market prices",
              "hypothesis": "Using native snowfall quantiles exposes the uncertainty hidden in a single accumulated amount.",
              "target": "Kalshi total snowfall thresholds and verified climate stations during exact native date ranges.",
              "venues": [venue_archive.KALSHI], "date_window": {"from": min(row["first"] for row in storm_groups),
                                                               "through": max(row["last"] for row in storm_groups)},
              "metrics": metrics, "cases": cases,
              "limits": ["The probability interval uses dependence-agnostic bounds on the five NOAA marginal quantiles.",
                         "Partly overlapping 24-hour totals contribute to an upper bound and are excluded from the lower bound. One displayed tenth allows precision uncertainty.",
                         "These bounds characterize the available guidance only. Historical calibration, native prices and payout evidence are distinct inputs.",
                         "The archived object gives the original publication time. The local retrieval time is later and preserved separately.",
                         "Hourly candle prices provide no execution-grade depth."],
              "provenance": {"engine": VERSION, "noaa_card_docs": DOCS, "previous_storm_outcomes": prior,
                             "input_quantiles": list(QUANTILES), "source_gaps": unresolved,
                             "implementation_sha256": sha256(Path(__file__).read_bytes()).hexdigest()}}
    return research_model.write_record(data, result, {"scenarios": scenarios, "forecast_cards": [card for card, _ in weather.values()]})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=venue_archive.DEFAULT_DATA)
    parser.add_argument("storm_record", type=Path)
    args = parser.parse_args()
    path = report(args.data, args.storm_record)
    output = json.loads(path.read_bytes())
    print(json.dumps({"path": str(path), "metrics": output["metrics"], "gaps": output["provenance"]["source_gaps"]}, indent=2))


if __name__ == "__main__":
    main()
