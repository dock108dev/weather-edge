"""Transfer the six-hour observed-high comparison to eight additional stations."""

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone
import gzip
from hashlib import sha256
import json
from pathlib import Path
import re
from statistics import mean

from . import collector, expansion_nbh, expansion_nbm, expansion_weather, research_model, venue_archive
from .history import _write
from .market import iso, parse_time
from .model import MIN_BAND_DAYS, MIN_TAIL_SUPPORT_DAYS, _benchmark_buckets, _limits, _outcome, _probability
from .nbm import AWS_NBM, climate_window, nbm_url
from .strategy_metar_high import disagreement_reserve, guarded_probability, metar_high_evidence, read_covering_observations
from .strategy_payout_timing import raw_climate_reports
from .strategy_rules import temperature_window
from .strategy_runs import digest, json_bytes, write_new
from .study import _partition


VERSION = "six-hour-high-station-transfer-v1"
STATIONS = ("KPHL", "KBOS", "KPVD", "KORD", "KDEN", "KMDW", "KEWR", "KTTN")
NBH_OFFSETS = {"KPHL": 18_130_000, "KBOS": 11_525_000, "KPVD": 18_420_000, "KORD": 17_740_000,
               "KDEN": 12_620_000, "KMDW": 16_425_000, "KEWR": 13_390_000, "KTTN": 20_060_000}
CALIBRATION = (date(2026, 9, 28), date(2026, 10, 22))
TEST = (date(2026, 10, 28), date(2026, 11, 13))
WARM_CALIBRATION = (date(2026, 8, 24), date(2026, 9, 16))
WARM_TEST = (date(2026, 9, 18), date(2026, 9, 27))
HOURS = (13, 17)
MODELS = ("Raw NOAA NBS spread", "Point plus remaining NBH", "Six-hour plus remaining NBH", "Six-hour with disagreement reserve")
RUNTIME_PATH = Path("research/runtime/daily-high-station-transfer")
RUNTIME_VERSION = "daily-high-station-transfer-shadow-v1"


def dates(first, last):
    return (first + timedelta(days=index) for index in range((last - first).days + 1))


def _hourly(data, station, issue, *, fetch):
    card = expansion_nbh.load(data, station, issue)
    if card is not None or not fetch or collector.paused(data):
        return card
    center = NBH_OFFSETS[station]
    source = nbm_url(issue.date(), issue.hour, "nbh")
    card = expansion_nbm.fetch_station_card(source, station, issue, expansion_nbh.parse_evidence,
                                           (center - 180_000, center + 180_000), "NOAA NBM NBH station bulletin")
    write_new(expansion_nbh.path(data, station, issue), json.dumps(card, separators=(",", ":")).encode() + b"\n")
    return card


def weather_cards(data, fetch, windows=(CALIBRATION, TEST)):
    arguments = [(station, datetime.combine(day, time(hour), timezone.utc), kind)
                 for bounds in windows for day in dates(*bounds)
                 for station in STATIONS for hour in (12, 18) for kind in ("nbh", "nbs")]
    rows, gaps = {}, []

    def obtain(key):
        station, issue, kind = key
        try:
            if kind == "nbh":
                card = _hourly(data, station, issue, fetch=fetch)
            else:
                card = expansion_nbm.load_card(data, station, issue, fetch=fetch and not collector.paused(data))
        except (OSError, KeyError, ValueError) as error:
            return key, None, str(error)
        return key, card, None

    with ThreadPoolExecutor(max_workers=4) as pool:
        for key, card, why in pool.map(obtain, arguments):
            if card:
                rows[key] = card
            else:
                gaps.append({"station": key[0], "at": iso(key[1]), "kind": key[2], "reason": why or "No local card"})
    return rows, gaps


def _known(cards, station, day, kind, at):
    for hour in (18, 12):
        issue = datetime.combine(day, time(hour), timezone.utc)
        if row := cards.get((station, issue, kind)):
            if issue <= parse_time(row["available_at"]) <= at:
                yield row


def _prior_forecast(cards, station, day, *, at, close):
    raw, remaining = None, None
    utc_midnight = datetime.combine(day + timedelta(days=1), time.min, timezone.utc)
    for card in _known(cards, station, day, "nbs", at):
        at_valid = [point for point in card["points"] if parse_time(point["valid_at"]) == utc_midnight
                    and point.get("TXN") is not None and point.get("XND", 0) > 0]
        if at_valid:
            raw = (card, at_valid[0]["TXN"], at_valid[0]["XND"])
            break
    hours = [at + timedelta(hours=n) for n in range(round((close - at).total_seconds() / 3600))]
    for card in _known(cards, station, day, "nbh", at):
        points = {parse_time(point["valid_at"]): point["temp_f"] for point in card["hourly"]}
        if hours and all(t in points for t in hours):
            remaining = card, max(points[t] for t in hours)
            break
    return raw, remaining


def meteorology(data, fetch, *, calibration=CALIBRATION, test=TEST):
    cards, card_gaps = weather_cards(data, fetch, (calibration, test))
    cached_cli = raw_climate_reports(data, day_from=calibration[0], day_through=test[1])
    rows, paths, omissions, source_errors = [[], []], {}, Counter(), {}
    forecast_cards = {}
    for station in STATIONS:
        offset = expansion_weather.STATIONS[station][3]
        try:
            observations, path = read_covering_observations(data, station, calibration[0], test[1] + timedelta(days=2))
            summaries = expansion_weather.daily_labels(data, station, calibration[0], test[1])
        except (OSError, KeyError, ValueError) as error:
            source_errors[station] = str(error)
            continue
        paths[station] = {"observation_file": path, "sha256": sha256((data / path).read_bytes()).hexdigest()}
        observations_by_day = defaultdict(list)
        for value in observations:
            date_ = (parse_time(value["at"]) + timedelta(hours=offset)).date()
            observations_by_day[date_].append(value)
        for group, bounds in enumerate((calibration, test)):
            phase = "calibration" if group == 0 else "test"
            for day in dates(*bounds):
                start, close = climate_window(day, offset)
                raw = cached_cli.get(((station, str(day), iso(start), iso(close)), "daily-high"), ())
                summary = summaries.get(str(day))
                if group == 0:
                    cutoff = climate_window(test[0], offset)[0] + timedelta(hours=min(HOURS))
                    raw = [value for value in raw if parse_time(value["at"]) < cutoff]
                    if summary and parse_time(summary["published_at"]) >= cutoff:
                        summary = None
                actual = raw[-1]["amount"] if raw else (summary or {}).get("high_f")
                if actual is None:
                    omissions[phase, "complete published climate label absent"] += len(HOURS)
                    continue
                for hour in HOURS:
                    at = start + timedelta(hours=hour)
                    guide, remainder = _prior_forecast(cards, station, day, at=at, close=close)
                    if guide is None or remainder is None:
                        omissions[phase, "earlier short or hourly NOAA forecast unavailable"] += 1
                        continue
                    observed = [sample for observation in observations_by_day[day]
                                for sample in metar_high_evidence(observation, station, start, at)]
                    point = max((reading for reading in observed if reading["kind"] == "point"),
                                key=lambda row: row["lower_f"], default=None)
                    if point is None:
                        omissions[phase, "no exact METAR point maximum"] += 1
                        continue
                    full = max(observed, key=lambda row: row["lower_f"])
                    details = {}
                    for card in (guide[0], remainder[0]):
                        kind = "nbh" if "hourly" in card else "nbs"
                        path_obj = expansion_nbh.path if kind == "nbh" else expansion_nbm.card_path
                        metadata = {key: card.get(key) for key in ("source", "issued_at", "available_at", "received_at", "card_sha256")}
                        details[kind] = metadata
                        forecast_cards[station, kind, card["issued_at"]] = {
                            **metadata, "path": str(path_obj(data, station, parse_time(card["issued_at"])).relative_to(data)),
                        }
                    rows[group].append({"window": phase, "station": station, "date": str(day), "local_hour": hour,
                                        "at": iso(at), "actual_f": actual, "raw_f": guide[1], "raw_sigma": guide[2],
                                        "physical_point_f": max(point["lower_f"], remainder[1]),
                                        "physical_with_six_f": max(full["lower_f"], remainder[1]),
                                        "observed_point_bound": point, "all_observations_bound": full,
                                        "expected_remaining_max_f": remainder[1], "official_report": raw[-1] if raw else summary,
                                        "official_source_grade": "exact raw CLI" if raw else "annual CLI summary",
                                        "weather": details, "six_hour_report_count": sum(o["kind"] == "six-hour high" for o in observed)})
    return rows, {"/".join(k): v for k, v in omissions.items()}, {
        "raw_observation_archives": paths, "raw_forecast_cards": list(forecast_cards.values()),
        "card_gaps": card_gaps, "whole_station_gaps": source_errors,
    }


def native_buckets(data, tested, *, window=TEST):
    stations_days = {(row["station"], row["date"]) for row in tested}
    grouped = defaultdict(list)
    for row in venue_archive.export(data, family="daily-high", status="settled", day_from=str(window[0]), day_through=str(window[1])):
        if (row["station"], row["day"]) not in stations_days or row["yes_payout"] not in ("0", "1") or not row["settlement_source"]:
            continue
        if b := temperature_window(row):
            grouped[row["venue"], row["station"], row["day"]].append({"lower_f": b[0], "upper_f": b[1],
                "instrument_id": row["instrument_id"], "payout": int(row["yes_payout"]), "payout_receipt": row["settlement_received_at"],
                "payout_source": row["settlement_source"], "settlement_at": row["settlement_at"], "label": row["label"],
                "event_id": row["event_id"], "opened_at": row["opened_at"], "closed_at": row["closed_at"],
                "climate_starts_at": row["climate_starts_at"], "climate_ends_at": row["climate_ends_at"]})
    return {key: items for key, items in grouped.items() if len({row["event_id"] for row in items}) == 1
            and sum(row["payout"] for row in items) == 1
            and _partition(items) == "exhaustive"}


def evaluate(calibration, tested, market=None):
    if not calibration or not tested:
        raise ValueError("Both independent windows require exact physical forecast and observation matches")
    fields = ("raw_f", "physical_point_f", "physical_with_six_f", "physical_with_six_f")
    saved = defaultdict(list)
    for row in calibration:
        for name, field in zip(MODELS[1:3], fields[1:3], strict=True):
            saved[name, row["station"], row["local_hour"]].append(row["actual_f"] - row[field])
    reserve = disagreement_reserve(calibration)
    results = []
    for row in tested:
        if not all(saved[name, row["station"], row["local_hour"]] for name in MODELS[1:3]):
            continue
        if market:
            buckets = market.get((row["station"], row["date"]))
            if not buckets or any(_outcome(b, row["actual_f"]) != b["payout"] for b in buckets):
                continue
        else:
            buckets = _benchmark_buckets(round(row["raw_f"]))
        probabilities = {}
        for name, field in zip(MODELS, fields, strict=True):
            lower = row["all_observations_bound"]["lower_f"]
            if name == MODELS[0]:
                centers, scale = [row[field]], row["raw_sigma"]
            else:
                errors = saved[MODELS[2] if name == MODELS[3] else name, row["station"], row["local_hour"]]
                centers, scale = [row[field] + delta for delta in errors], 0.5
            probabilities[name] = [guarded_probability(_limits(b), centers, scale, lower, reserve["upper_one_sided_wilson"])
                                   if name == MODELS[3] else _probability(_limits(b), centers, scale) for b in buckets]
        scores = {name: mean((prediction - _outcome(b, row["actual_f"])) ** 2 for prediction, b in zip(vector, buckets, strict=True))
                  for name, vector in probabilities.items()}
        results.append({**row, "scores": scores, "buckets": buckets, "predictions": probabilities})
    return results, reserve


def measures(rows, label):
    result = []
    labels = [("all", rows)] + [(str(t), [r for r in rows if r["local_hour"] == t]) for t in HOURS]
    for group, found in labels:
        if not found:
            continue
        for name, field in zip(MODELS, ("raw_f", "physical_point_f", "physical_with_six_f", "physical_with_six_f"), strict=True):
            by_day = defaultdict(list)
            for row in found:
                by_day[row["date"]].append(row["scores"][name])
            result.append({"window": label, "local_standard_hour": group, "model": name, "dates": len(by_day),
                           "station_days": len({(row["station"], row["date"]) for row in found}), "decision_rows": len(found),
                           "brier": mean(mean(values) for values in by_day.values()),
                           "mae_f": mean(abs(row["actual_f"] - row[field]) for row in found),
                           "complete_raw_cli_rows": sum(row["official_source_grade"] == "exact raw CLI" for row in found),
                           "stated_six_hour_coverage_rows": sum(row["six_hour_report_count"] > 0 for row in found)})
    return result


def report(data, *, fetch, calibration=CALIBRATION, test=TEST):
    (earlier, held), omissions, sources = meteorology(data, fetch, calibration=calibration, test=test)
    weather, reserve = evaluate(earlier, held)
    scores = measures(weather, "Held official daily highs")
    payouts = native_buckets(data, held, window=test)
    contracts, cases = {}, []
    for venue in venue_archive.VENUES:
        supported = {(s, day): rows for (v, s, day), rows in payouts.items() if v == venue}
        if supported:
            verified, _ = evaluate(earlier, held, supported)
            scores.extend(measures(verified, f"Held exact {venue} payouts"))
            contracts[venue] = verified
    if weather:
        examples = sorted(weather, key=lambda row: row["scores"][MODELS[3]] - row["scores"][MODELS[1]])
        for row in (examples[0], examples[-1]):
            exact = next(((venue, item) for venue, rows in contracts.items() for item in rows
                          if (item["station"], item["at"]) == (row["station"], row["at"])), None)
            symbol = next((b["instrument_id"] for b in exact[1]["buckets"] if b["payout"])) if exact else None
            cases.append({"station": row["station"], "date": row["date"], "at": row["at"],
                          "venue": exact[0] if exact else None, "instrument_id": symbol,
                          "title": f"{row['station']} point and six-hour high evidence",
                          "detail": f"Point-based high {row['physical_point_f']:.0f}°F, six-hour-based {row['physical_with_six_f']:.0f}°F, official {row['actual_f']:.0f}°F.",
                          "data": row})
    result = {"version": VERSION, "title": "Six-hour high observations across eight additional stations",
              "hypothesis": "Whole-period METAR maximums and a calibration reserve transfer to other exact climate stations.",
              "target": "North and Central U.S. station-day highs with native confirmed payouts where applicable.",
              "venues": list(venue_archive.VENUES), "date_window": {"from": str(calibration[0]), "through": str(test[1]),
                                                                  "calibration": list(map(str, calibration)), "held": list(map(str, test))},
              "metrics": scores, "cases": cases,
              "limits": ["Historical ASOS receipts use a ten-minute proxy. Native forecast bulletin publication and local retrieval clocks remain separate.",
                         "Six-hour maxima are eligible only when both possible report-clock windows lie completely inside the climate day.",
                         "The reserve preserves probability of a different final climate report. The native payout scores only include station-days where that payout matches the official winning interval."],
              "provenance": {"engine": VERSION, "station_ids": STATIONS, "clock_hours": HOURS, "guard_reserve": reserve,
                             "source_gaps": omissions, "archived_guidance_cards": len(sources["raw_forecast_cards"]),
                             "implementation_sha256": sha256(Path(__file__).read_bytes()).hexdigest()}}
    return research_model.write_record(data, result, {"calibration": earlier, "test": weather, "payout_cases": contracts, "sources": sources})


def _archived(data, path):
    record = json.loads(path.read_bytes())
    proof = record["provenance"]
    raw = (data / proof["replay_details_path"]).read_bytes()
    if record["version"] != VERSION or sha256(raw).hexdigest() != proof["replay_details_sha256"]:
        raise ValueError("Unexpected or changed high transfer research inputs")
    return record, json.loads(gzip.decompress(raw))


def freeze_runtime(data, source):
    record, verified = _archived(data, source)
    history = verified["calibration"]
    residuals = defaultdict(list)
    for row in history:
        if row["local_hour"] == 17:
            residuals[row["station"]].append(row["actual_f"] - row["physical_with_six_f"])
    model = {"version": RUNTIME_VERSION, "source_study": record["id"], "residuals_at_17": dict(residuals),
             "report_reserve": record["provenance"]["guard_reserve"]["upper_one_sided_wilson"],
             "published_before": max((row["official_report"].get("at") or row["official_report"]["published_at"]) for row in history),
             "fit_dates": record["date_window"]["calibration"], "first_decision_hour": 17}
    vintage = digest(model)
    path = data / RUNTIME_PATH / f"{vintage}.json"
    write_new(path, json_bytes(model) + b"\n")
    _write(data / RUNTIME_PATH / "active.json", json_bytes({"vintage": vintage, "path": str(path.relative_to(data))}))
    return vintage


def load_runtime(data, *, vintage=None):
    root = Path(data) / RUNTIME_PATH
    if vintage is None and (root / "active.json").is_file():
        vintage = json.loads((root / "active.json").read_bytes())["vintage"]
    if not isinstance(vintage, str) or re.fullmatch(r"[0-9a-f]{64}", vintage) is None:
        return None
    path = root / f"{vintage}.json"
    if not path.is_file():
        return None
    obj = json.loads(path.read_bytes())
    if obj["version"] != RUNTIME_VERSION or digest(obj) != vintage:
        raise ValueError("High station transfer artifact checksum mismatch")
    return {**obj, "vintage": vintage, "data": Path(data)}


def _live_remaining(artifact, event, cutoff, end):
    target = date.fromisoformat(event["date"])
    station = event["station"]
    candidates = [row for row in event.get("forecasts", [])
                  if (row.get("hourly_source") or "").startswith(AWS_NBM + "/") and not row.get("timing_proxy")]
    for issue_hour in (12, 18):
        issue = datetime.combine(target, time(issue_hour), timezone.utc)
        card = expansion_nbh.load(artifact["data"], station, issue)
        if card and "received_at" in card and parse_time(card["available_at"]) <= parse_time(card["received_at"]):
            candidates.append({**card, "available_at": card["received_at"], "hourly_source": card["source"]})
    required = [cutoff + timedelta(hours=n) for n in range(round((end - cutoff).total_seconds() / 3600))]
    for row in sorted(candidates, key=lambda value: parse_time(value["issued_at"]), reverse=True):
        if not parse_time(row["issued_at"]) <= parse_time(row["available_at"]) <= cutoff:
            continue
        points = {parse_time(value["valid_at"]): value["temp_f"] for value in row.get("hourly", [])}
        if required and all(x in points for x in required):
            return row, max(points[x] for x in required)
    return None


def predict_runtime(artifact, event, as_of):
    station = event.get("station")
    if artifact is None or event.get("family", "daily-high") != "daily-high" or station not in artifact["residuals_at_17"]:
        return None
    start, end = climate_window(event["date"], expansion_weather.STATIONS[station][3])
    at = parse_time(as_of)
    effective = start + timedelta(hours=17)
    if (not effective <= at < end or effective < parse_time(artifact["published_before"]) or
            (start, end) != tuple(parse_time(event[key]) for key in ("climate_starts_at", "climate_ends_at"))):
        return None
    observations = [row for row in event.get("observations", []) if row.get("source") in {"NWS", "Aviation Weather Center"}
                    and not row.get("timing_proxy")]
    witnesses = [w for row in observations for w in metar_high_evidence(row, station, start, effective)]
    forecast = _live_remaining(artifact, event, effective, end)
    if not witnesses or forecast is None:
        return None
    witness = max(witnesses, key=lambda row: (row["lower_f"], -parse_time(row["available_at"]).timestamp()))
    card, remainder = forecast
    high = max(remainder, witness["lower_f"])
    centers = [high + value for value in artifact["residuals_at_17"][station]]
    estimates = []
    for bucket in event.get("buckets", []):
        limits = _limits(bucket)
        yes = guarded_probability(limits, centers, 0.5, witness["lower_f"], artifact["report_reserve"])
        count = sum(limits[0] <= value < limits[1] for value in centers)
        estimates.append({"id": bucket["id"], "probability": yes, "no_probability": 1 - yes,
                          "validation_days": len(centers), "validation_residuals": len(centers),
                          "yes_tail_support": count, "no_tail_support": len(centers) - count,
                          "uncertain": len(centers) < MIN_BAND_DAYS or min(count, len(centers) - count) < MIN_TAIL_SUPPORT_DAYS})
    return {"version": RUNTIME_VERSION, "vintage": artifact["vintage"], "mode": "shadow",
            "method": "metar-precision-guarded-high", "target": "daily-high", "at": iso(at),
            "knowledge_at": iso(effective), "local_standard_decision_hour": 17, "model_high_f": high,
            "available_at": max(witness["available_at"], card["available_at"], key=parse_time),
            "issued_at": card["issued_at"], "buckets": estimates,
            "context": {"temperature_witness": witness, "expected_remaining_max_f": remainder,
                        "climate_report_disagreement_reserve": artifact["report_reserve"],
                        "forecast": {"remaining": {"source": card["hourly_source"], "issued_at": card["issued_at"],
                                                     "available_at": card["available_at"]}}}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=venue_archive.DEFAULT_DATA)
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--warm", action="store_true", help="Replay the separate warmer August–September date window")
    parser.add_argument("--freeze-runtime", type=Path, help="Prepare a separate late shadow from an audited transfer record")
    args = parser.parse_args()
    if args.freeze_runtime:
        print(freeze_runtime(args.data, args.freeze_runtime))
        return
    windows = (WARM_CALIBRATION, WARM_TEST) if args.warm else (CALIBRATION, TEST)
    path = report(args.data, fetch=args.fetch, calibration=windows[0], test=windows[1])
    row = json.loads(path.read_bytes())
    print(json.dumps({"path": str(path), "metrics": row["metrics"], "provenance": row["provenance"]}, indent=2))


if __name__ == "__main__":
    main()
