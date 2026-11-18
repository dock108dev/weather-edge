"""Measure receipt-timed station temperature surprises against later display prices."""
from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import defaultdict
from datetime import date, timedelta
import gzip
from hashlib import sha256
import json
import math
from pathlib import Path
import statistics
import sys

from sklearn.feature_extraction import DictVectorizer
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from . import archive, research_hrrr, research_model, strategy_market, study, venue_archive, venue_replay
from .market import iso, parse_time

VERSION = "historical-observation-surprise-v1"
FIT = ("2026-11-03", "2026-11-07")
TEST = ("2026-11-08", "2026-11-13")
EXTENDED = {
    "fit": ("2026-10-01", "2026-10-14"),
    "calibration": ("2026-10-15", "2026-10-21"),
    "test": ("2026-10-22", "2026-10-28"),
}
INTERVALS = ((8, 11), (11, 14), (14, 17))
HORIZONS = (15, 30, 60)


def _forecast_at(event, observation):
    valid_at = parse_time(observation["at"])
    available = sorted((row for row in event["forecasts"]
                        if parse_time(row["available_at"]) <= valid_at and parse_time(row["issued_at"]) <= valid_at),
                       key=lambda row: parse_time(row["issued_at"]), reverse=True)
    for forecast in available:
        hourly = sorted((parse_time(row["valid_at"]), row["temp_f"]) for row in forecast.get("hourly", [])
                        if row.get("temp_f") is not None)
        if not hourly:
            continue
        index = bisect_right([row[0] for row in hourly], valid_at)
        if index and hourly[index - 1][0] == valid_at:
            return forecast, hourly[index - 1][1]
        if 0 < index < len(hourly):
            left, right = hourly[index - 1], hourly[index]
            gap = right[0] - left[0]
            if timedelta() < gap <= timedelta(hours=3):
                fraction = (valid_at - left[0]) / gap
                return forecast, left[1] + fraction * (right[1] - left[1])
    return None, None


def _market_snapshot(histories, at):
    rounded = at.replace(second=0, microsecond=0)
    values = []
    for times, history in histories:
        index = bisect_right(times, rounded) - 1
        if index < 0:
            return None
        receipt, source, proxy = history[index]
        if rounded - source > strategy_market.MAX_LAG:
            return None
        values.append((receipt, source, proxy))
    normal = strategy_market.normalized_prices([row[2] for row in values])
    if normal is None:
        return None
    return {"sample_cutoff": iso(rounded), "source_at": [iso(row[1]) for row in values], "probabilities": normal}


def _choose_threshold(forecast, buckets):
    boundaries = [row["lower_f"] for row in buckets if row["lower_f"] is not None]
    if not boundaries:
        return None
    return min(boundaries, key=lambda value: (abs(value - forecast), value))


def _looks_observed(row):
    return (row.get("receipt_source") and not row.get("timing_proxy") and not row.get("display_only")
            and type(row.get("temp_f")) in {int, float})


def _event_inputs(event, cutoff, gaps):
    buckets = sorted(event["buckets"], key=lambda row: row["lower_f"] if row["lower_f"] is not None else -math.inf)
    if study._partition(buckets) != "exhaustive":
        gaps["station-day without complete exact bucket partition"] += 1
        return
    settlement = event.get("settlement") or {}
    high = settlement.get("high_f")
    winner = settlement.get("winning_bucket")
    proof = settlement.get("payout_available_at")
    if high is None or not winner or not proof:
        gaps["station-day without matching official high and payout"] += 1
        return
    label_at = settlement.get("verified_climate_publication_at") or proof
    if cutoff and parse_time(label_at) >= cutoff:
        gaps["earlier outcome evidence unavailable at fit cutoff"] += 1
        return
    start = parse_time(event.get("climate_starts_at") or event["starts_at"])
    close = parse_time(event.get("trading_closed_at") or event.get("market_end_at") or event["ends_at"])
    originals = [(parse_time(row["available_at"]), parse_time(row["at"]), row)
                 for row in event["observations"] if _looks_observed(row)]
    originals.sort(key=lambda row: (row[0], row[1]))
    obs = {}
    for available, valid, row in originals:
        if start <= valid <= available:
            obs.setdefault(valid, (available, row))
    histories = [strategy_market.eligible_prices(bucket, midpoint=True) for bucket in buckets]
    prices = [([item[0] for item in history], history) for history in histories]
    for first, last in INTERVALS:
        lo, hi = start + timedelta(hours=first), start + timedelta(hours=last)
        cases = ((available, valid, row) for valid, (available, row) in obs.items() if lo <= available < hi and available < close)
        selected = None
        for available, valid, reading in cases:
            forecast, hourly = _forecast_at(event, reading)
            guidance = [row for row in event["forecasts"] if row.get("high_f") is not None
                        and parse_time(row["available_at"]) <= available]
            if forecast and guidance:
                newest = max(guidance, key=lambda row: parse_time(row["issued_at"]))
                selected = available, valid, reading, forecast, hourly, newest
                break
        if not selected:
            gaps["no receipt-matched forecast and observation in fixed window"] += 1
            continue
        available, valid, reading, forecast, hourly, newest = selected
        peak = max(row["temp_f"] for observed_at, (received, row) in obs.items()
                   if start <= observed_at <= valid and received <= available)
        threshold = _choose_threshold(newest["high_f"], buckets)
        relevant = [index for index, bucket in enumerate(buckets)
                    if bucket["lower_f"] is not None and bucket["lower_f"] >= threshold]
        at = lambda time: _market_snapshot(prices, time) if time < close else None
        prior = at(available - timedelta(microseconds=1))
        with_market = {"pre": prior} | {str(minutes): at(available + timedelta(minutes=minutes)) for minutes in HORIZONS}
        for proof_value in with_market.values():
            if proof_value:
                proof_value["threshold_probability"] = math.fsum(proof_value["probabilities"][index] for index in relevant)
        selected_instrument = next(bucket for bucket in buckets if bucket["lower_f"] == threshold)
        yield {
            "station": event["station"], "date": event["date"], "at": iso(available), "hour_lst": first,
            "actual_f": high, "bucket": selected_instrument["id"], "threshold_f": threshold,
            "observation": reading, "hourly_forecast_f": hourly, "innovation_f": reading["temp_f"] - hourly,
            "forecast": {key: forecast.get(key) for key in ("available_at", "issued_at", "source", "hourly_source")},
            "noaa_high_f": newest["high_f"], "observed_peak_f": peak, "payout_available_at": proof,
            "official_source": settlement.get("source"), "climate_publication_at": label_at,
            "market_sources": [bucket.get("archived_payout") for bucket in buckets],
            "quotes": with_market,
        }


def _archived_market_events(data, day, weather):
    for event in weather:
        reports = [row for row in event.get("official_reports", []) if row.get("high_f") is not None]
        if not reports:
            continue
        label = max(reports, key=lambda row: parse_time(row["available_at"]))
        matches = venue_archive.query(data, venue=venue_archive.POLY, family="daily-high", place=event["station"],
                                      day=day, status="settled", limit=40)["rows"]
        representative = next((row for row in matches if row["station"] == event["station"] and row["yes_payout"] == "1"), None)
        if representative is None:
            continue
        market = venue_replay.load(data, venue_archive.POLY, representative["instrument_id"], day, [event])
        if market is None or not market["replay_same_climate_report"]:
            continue
        buckets = market["buckets"]
        if not all(bucket["archived_payout"] for bucket in buckets):
            continue
        observed = label["high_f"]
        hits = [bucket for bucket in buckets
                if (bucket["lower_f"] is None or bucket["lower_f"] <= observed)
                and (bucket["upper_f"] is None or observed <= bucket["upper_f"])]
        if len(hits) != 1 or hits[0]["id"] != representative["instrument_id"]:
            continue
        if sum(bucket["archived_payout"]["yes_payout"] for bucket in buckets) != 1:
            continue
        market["settlement"] = {
            **label, "winning_bucket": representative["instrument_id"],
            "payout_available_at": hits[0]["archived_payout"]["archive_received_at"],
            "verified_climate_publication_at": label["available_at"],
        }
        yield market


def inputs(data, extended=False):
    data = Path(data)
    windows = EXTENDED if extended else {"fit": FIT, "test": TEST}
    results = {window: [] for window in windows}
    gaps = defaultdict(int)
    source = {}
    pieces = tuple(windows)
    for index, (group, span) in enumerate(windows.items()):
        cutoff = parse_time(windows[pieces[index + 1]][0] + "T00:00:00Z") if index + 1 < len(pieces) else None
        for day in archive.date_range(*map(date.fromisoformat, span)):
            day_text = str(day)
            weather = archive.load_day(data, day)["events"]
            events = _archived_market_events(data, day_text, weather) if extended else weather
            for event in events:
                if not event["buckets"]:
                    continue
                path = (data / "replay.json" if day_text == "2026-11-05"
                        else data / "archive" / day_text / (event["station"] + ".json"))
                if path.is_file():
                    source[str(path.relative_to(data))] = sha256(path.read_bytes()).hexdigest()
                results[group].extend(_event_inputs(event, cutoff, gaps))
    return {"version": "historical-observation-surprise-v2" if extended else VERSION,
            "windows": windows, "sets": results, "gaps": dict(gaps), "sources": source}


def _features(row, include_innovation):
    terms = {
        "station": row["station"], "clock": row["hour_lst"], "nbm_f": row["noaa_high_f"],
        "observed_peak_minus_nbm_f": row["observed_peak_f"] - row["noaa_high_f"],
    }
    if include_innovation:
        terms["innovation_f"] = row["innovation_f"]
    return terms


def _fit(rows, innovation, *, calibration=None):
    model = make_pipeline(DictVectorizer(sparse=False), StandardScaler(), Ridge(alpha=10))
    matrix = [_features(row, innovation) for row in rows]
    actual = [row["actual_f"] for row in rows]
    target = [label - row["noaa_high_f"] for row, label in zip(rows, actual, strict=True)] if calibration else actual
    model.fit(matrix, target)
    if calibration:
        def predictions(cases):
            offsets = model.predict([_features(row, innovation) for row in cases])
            return [max(row["observed_peak_f"] - .5, row["noaa_high_f"] + offset)
                    for row, offset in zip(cases, offsets, strict=True)]
        sd = math.sqrt(statistics.mean((estimate - row["actual_f"]) ** 2
                                       for row, estimate in zip(calibration, predictions(calibration), strict=True)))
        return predictions, max(.5, sd)
    # Residual width comes entirely from the fit window. The test compares
    # methods on identical cases, with all label clocks frozen before fit.
    sd = math.sqrt(statistics.mean((pred - result) ** 2 for pred, result in zip(model.predict(matrix), actual, strict=True)))
    return lambda cases: model.predict([_features(row, innovation) for row in cases]), max(.5, sd)


def _probability(threshold, mean, sd):
    return 1 - research_hrrr._cdf(threshold - .5, mean, sd)


def _summary(rows, method, predictions):
    losses, errors = [], []
    for row, (mean, probability) in zip(rows, predictions, strict=True):
        losses.append((probability - (row["actual_f"] >= row["threshold_f"])) ** 2)
        errors.append(abs(mean - row["actual_f"]))
    return {"comparison": "weather", "method": method, "rows": len(rows),
            "calendar_dates": len({row["date"] for row in rows}),
            "station_days": len({(row["date"], row["station"]) for row in rows}),
            "threshold_brier": statistics.mean(losses), "mae_f": statistics.mean(errors)}


def _market_response(rows):
    stats = []
    candidates = []
    for minutes in HORIZONS:
        common = [row for row in rows if row["quotes"]["pre"] and row["quotes"][str(minutes)]]
        changes = []
        for row in common:
            before = row["quotes"]["pre"]["threshold_probability"]
            after = row["quotes"][str(minutes)]["threshold_probability"]
            movement = after - before
            signed = movement * (1 if row["innovation_f"] > 0 else -1 if row["innovation_f"] < 0 else 0)
            changes.append(signed)
            candidates.append((abs(signed), row, minutes, movement))
        nonzero = [value for value in changes if abs(value) > 1e-10]
        stats.append({"comparison": "market response", "horizon_minutes": minutes, "rows": len(common),
                      "calendar_dates": len({row["date"] for row in common}),
                      "station_days": len({(row["date"], row["station"]) for row in common}),
                      "flat": len(changes) - len(nonzero), "aligned": sum(value > 0 for value in nonzero),
                      "mean_aligned_move_points": statistics.mean(changes) * 100 if changes else None})
    selected = []
    used = set()
    for _, row, minutes, move in sorted(candidates, key=lambda item: item[0], reverse=True):
        identity = row["station"], row["date"]
        if identity in used:
            continue
        used.add(identity)
        selected.append({
            "station": row["station"], "date": row["date"], "at": row["at"],
            "instrument_id": row["bucket"], "venue": "polymarket-us",
            "title": f"{row['station']} {row['date']} · exact observation and {minutes}m market proxy",
            "detail": (f"Observation was {row['innovation_f']:+.1f}°F from the as-issued estimate. "
                       f"Displayed probability of at least {row['threshold_f']}°F moved {move * 100:+.1f} points."),
            "forecast": row["forecast"], "observation": row["observation"],
            "quotes": row["quotes"], "payout_available_at": row["payout_available_at"],
        })
        if len(selected) == 4:
            break
    return stats, selected


def run(data, frozen=None, *, extended=False):
    if frozen is None:
        evidence = inputs(data, extended=extended)
        frozen, digest = research_model.freeze_inputs(Path(data), evidence)
    else:
        body = gzip.decompress(Path(frozen).read_bytes())
        evidence = json.loads(body)
        digest = sha256(body).hexdigest()
    fit, test = evidence["sets"]["fit"], evidence["sets"]["test"]
    calibration = evidence["sets"].get("calibration")
    scores = []
    if fit and test:
        noaa_sd = math.sqrt(statistics.mean((row["actual_f"] - row["noaa_high_f"]) ** 2 for row in (calibration or fit)))
        noaa = [(row["noaa_high_f"], _probability(row["threshold_f"], row["noaa_high_f"], max(.5, noaa_sd)))
                for row in test]
        scores.append(_summary(test, "NOAA NBM", noaa))
        for name, innovation in (("Ridge with station peak", False), ("Ridge plus forecast surprise", True)):
            predict, sd = _fit(fit, innovation, calibration=calibration)
            prediction = [(mean, _probability(row["threshold_f"], mean, sd)) for row, mean in
                          zip(test, predict(test), strict=True)]
            scores.append(_summary(test, name, prediction))
    market, cases = _market_response(test)
    return research_model.write_record(data, {
        "version": evidence["version"],
        "title": "Do receipt-timed station surprises improve high forecasts or lead displayed prices?",
        "hypothesis": "The deviation from an already issued hourly forecast adds predictive value beyond the observed peak, and subsequent price moves align with the deviation.",
        "target": "daily high threshold and display probability",
        "venues": ["polymarket-us"],
        "date_window": evidence.get("windows", {"fit": FIT, "test": TEST}),
        "metrics": [*scores, *market], "cases": cases,
        "limits": [
            "Each clock window uses the first qualifying exact-station observation with a matched original MADIS receipt and an earlier as-issued forecast.",
            "The threshold is a native whole-degree bucket boundary nearest the contemporary NBM high. Weather probability scores use identical station-days and times.",
            "The market midpoint proxy uses both contemporaneous sides across the full partition. Minute display samples use the existing one-minute availability lag and coherent probability-mass screen.",
            "Market moves describe outcomes observed after the receipt. Other news and market changes can contribute during the same window. Display marks provide no executable size.",
            *(["The October comparison fits residuals around NBM and applies the same conservative observed floor to both Ridge variants. It uses an independent calibration block.",
               "Earlier official station reports are the training labels. Actual settlement receipts verify the venue outcomes retrospectively."] if calibration else []),
        ],
        "provenance": {
            "input_path": str(frozen), "input_sha256": digest, "gaps": evidence["gaps"], "ridge_alpha": 10,
            "fit_station_days": len({(row["station"], row["date"]) for row in fit}), "fit_rows": len(fit),
            "calibration_rows": len(calibration or ()),
            "local_standard_clock_windows": INTERVALS, "horizons_minutes": HORIZONS,
            "source_code": research_model.run_sources(sys.modules[__name__], strategy_market),
            "reproduce": f"python -m weather_edge.research_historical_surprise --inputs {frozen}",
        },
    })


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--inputs", type=Path)
    parser.add_argument("--extended", action="store_true")
    args = parser.parse_args()
    print(run(args.data, args.inputs, extended=args.extended))


if __name__ == "__main__":
    main()
