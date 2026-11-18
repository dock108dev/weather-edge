"""Compare a month-scale Polymarket US midpoint and NOAA update on native payouts."""

import argparse
from collections import Counter, defaultdict
from datetime import timedelta
import gzip
from hashlib import sha256
import json
from pathlib import Path

from . import research_model, strategy_anchor_extended as experiment, strategy_payout_timing, study, venue_archive
from .market import iso, parse_time
from .model import _limits, _probability, _snapshot
from .strategy_forecast_innovation import _price_entry
from .strategy_market import normalized_prices
from .strategy_rules import temperature_window


VERSION = "polymarket-month-payout-anchor-v1"
POLY = venue_archive.POLY
DISPLAY_LAG = timedelta(minutes=1)
DISPLAY_MAX_AGE = timedelta(minutes=2)
PRICE_SOURCE = "https://docs.polymarket.us/api-reference/price-history/get-price-history"


def markets(data):
    values = defaultdict(lambda: defaultdict(list))
    first, last = experiment.SPLITS["fit"][0], experiment.SPLITS["later test"][1]
    for entry in venue_archive.export(data, venue=POLY, family="daily-high", status="settled", day_from=first, day_through=last):
        if (not entry["closed_at"] or not entry["opened_at"] or entry["resolution_source"] != "NWS Daily Climate Report"
                or entry["yes_payout"] not in {"0", "1"} or not entry["settlement_source"]):
            continue
        limits = temperature_window(entry)
        if limits is None:
            continue
        values[(entry["station"], entry["day"])][entry["event_id"]].append({
            "lower_f": limits[0], "upper_f": limits[1], "instrument_id": entry["instrument_id"], "label": entry["label"],
            "payout": int(entry["yes_payout"]), "payout_source": entry["settlement_source"],
            "payout_local_receipt": entry["settlement_received_at"], "climate_starts_at": entry["climate_starts_at"],
            "climate_ends_at": entry["climate_ends_at"], "opened_at": entry["opened_at"], "closed_at": entry["closed_at"],
            "rules": entry["rules_primary"],
        })
    return values


def asof_price(db, ident, at):
    rows = db.execute("""SELECT at, receipt_id, body FROM history
        WHERE venue=? AND instrument_id=? AND kind='display-price-1m' AND at BETWEEN ? AND ? ORDER BY at DESC""",
                      (POLY, ident, iso(at - DISPLAY_MAX_AGE), iso(at - DISPLAY_LAG)))
    for row in rows:
        if item := _price_entry(POLY, parse_time(row["at"]), row["receipt_id"], json.loads(row["body"])):
            return item
    return None


def _outcomes(buckets, label):
    if label is None:
        return [row["payout"] for row in buckets]
    bounds = [_limits(row) for row in buckets]
    return [int(lo < label["amount"] < hi) for lo, hi in bounds]


def _forecast_proxy(bucket, weather):
    return _probability(_limits(bucket), [weather["noaa_high_f"]], weather["noaa_sd_f"])


def scenarios(data):
    listings = markets(data)
    cli = strategy_payout_timing.raw_climate_reports(data)
    panels, proofs = {name: [] for name in experiment.SPLITS}, {}
    gaps = {name: Counter() for name in experiment.SPLITS}
    with venue_archive.connect(data) as db:
        for index, (phase, bounds) in enumerate(experiment.SPLITS.items()):
            following = list(experiment.SPLITS)[index + 1:]
            lock = str(experiment.SPLITS[following[0]][0]) + "T00:00:00Z" if following else None
            for day in experiment.calendar_dates(bounds):
                for path in sorted((data / "archive" / day).glob("K???.json")):
                    station = path.stem
                    choices = listings.get((station, day), {})
                    if len(choices) != 1:
                        continue
                    event, buckets = next(iter(choices.items()))
                    if (study._partition(buckets) != "exhaustive" or sum(row["payout"] for row in buckets) != 1
                            or len({(b["climate_starts_at"], b["climate_ends_at"]) for b in buckets}) != 1):
                        gaps[phase]["Native event lacks a verified complete exact threshold partition"] += 1
                        continue
                    buckets.sort(key=_limits)
                    start, end = buckets[0]["climate_starts_at"], buckets[0]["climate_ends_at"]
                    if not (start and end):
                        gaps[phase]["Native exact climate window absent"] += 1
                        continue
                    labels = cli.get(((station, day, iso(start), iso(end)), "daily-high"), ())
                    known = next((row for row in labels if lock and row["at"] < lock), None)
                    if lock and not known:
                        gaps[phase]["Raw climate outcome not available before the following phase"] += 1
                        continue
                    candidate_outcomes = _outcomes(buckets, known if lock else None)
                    if sum(candidate_outcomes) != 1:
                        gaps[phase]["Climate report does not identify one winning bucket"] += 1
                        continue
                    content = path.read_bytes()
                    archive = json.loads(content)
                    proofs[str(path.relative_to(data))] = sha256(content).hexdigest()
                    begins = max(parse_time(b["opened_at"]) for b in buckets)
                    closes = min(parse_time(b["closed_at"]) for b in buckets)
                    for at in study._decision_times(day, archive["standard_utc_offset_hours"]):
                        if not begins < at < closes:
                            continue
                        context = _snapshot(station=station, day=day, as_of=at,
                                            forecasts=archive.get("forecasts", []), observations=[],
                                            standard_utc_offset_hours=archive["standard_utc_offset_hours"])
                        if context is None or not context.get("noaa_sd_f"):
                            gaps[phase]["NOAA as-issued high or spread absent"] += 1
                            continue
                        marks = [asof_price(db, b["instrument_id"], at) for b in buckets]
                        if any(mark is None for mark in marks):
                            gaps[phase]["Missing coherent bid or ask within display window"] += 1
                            continue
                        implied = normalized_prices([mark["midpoint"] for mark in marks])
                        if implied is None:
                            gaps[phase]["Market midpoint mass outside fixed normalization range"] += 1
                            continue
                        result = [{**bucket, "outcome": label, "market": value, "noaa": _forecast_proxy(bucket, context),
                                   "quote": observed} for bucket, label, value, observed in
                                  zip(buckets, candidate_outcomes, implied, marks, strict=True)]
                        forecast = context["forecast"]
                        panels[phase].append({"window": phase, "event_id": event, "station": station, "date": day, "at": iso(at),
                                              "weather_label_used_for_fit": known, "forecast_issued_at": forecast["issued_at"],
                                              "forecast_published_at": forecast["available_at"], "buckets": result})
    return panels, {phase: dict(values) for phase, values in gaps.items()}, proofs


def real_case(row):
    largest = max(row["buckets"], key=lambda bucket: abs(bucket["market"] - bucket["noaa"]))
    return {**row, "venue": POLY, "instrument_id": largest["instrument_id"],
            "title": f"{row['station']} {largest['label']} · archived native payout",
            "detail": f"Displayed midpoint proxy {largest['market']:.1%}, NOAA {largest['noaa']:.1%}, native YES paid ${largest['payout']}."}


def report(data):
    panel, gaps, evidence = scenarios(data)
    result = experiment.calculate(panel, case_builder=real_case)
    discord = {phase: len({(row["station"], row["date"]) for row in records
                          if any(b["outcome"] != b["payout"] for b in row["buckets"])}) for phase, records in panel.items()}
    record = {"version": VERSION, "title": "Can NOAA improve a larger Polymarket US price benchmark?",
              "hypothesis": "On later exact payout cases, a preset blend or earlier logistic calibration improves on the native midpoint proxy.",
              "target": "Daily-high thresholds with valid native midpoint vectors and NWS-based forecast inputs.",
              "venues": [POLY], "date_window": {"from": experiment.SPLITS["fit"][0], "through": experiment.SPLITS["later test"][1],
                                                   **experiment.SPLITS},
              "limits": ["The earlier model labels use separately timestamped complete NWS raw products. Later evaluation uses exact native payouts.",
                         "No original Polymarket settlement clock is inferred from retrospective settlement receipts.",
                         "Each market midpoint uses documented YES and opposite NO display asks. A one-minute availability delay and fixed mass range apply.",
                         "Depth and executed size are absent from displayed history. Scores are probability comparisons."],
              "provenance": {"engine": VERSION, "source": PRICE_SOURCE, "calendar_splits": experiment.SPLITS,
                             "fit_weather_labels_disagreeing_with_later_payout": discord, "gaps": gaps,
                             "study_code_sha256": sha256(Path(experiment.__file__).read_bytes()).hexdigest()},
              **result}
    return research_model.write_record(data, record, {"panels": panel, "source_archive_hashes": evidence, "gaps": gaps})


def verify(data, path):
    saved = json.loads(path.read_bytes())
    proof = saved["provenance"]
    packed = (data / proof["replay_details_path"]).read_bytes()
    if sha256(packed).hexdigest() != proof["replay_details_sha256"]:
        raise ValueError("Archived Polymarket panel checksum mismatch")
    details = json.loads(gzip.decompress(packed))
    result = experiment.calculate(details["panels"], case_builder=real_case)
    if any(saved[key] != value for key, value in result.items()):
        raise ValueError("Reproduced Polymarket anchor differs from its frozen record")
    return {"id": saved["id"], "verified": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=venue_archive.DEFAULT_DATA)
    parser.add_argument("--verify", type=Path)
    args = parser.parse_args()
    if args.verify:
        print(json.dumps(verify(args.data, args.verify)))
        return
    path = report(args.data)
    result = json.loads(path.read_bytes())
    print(json.dumps({"path": str(path), "metrics": result["metrics"], "fit": result["fit"], "cases": result["cases"]}, indent=2))


if __name__ == "__main__":
    main()
