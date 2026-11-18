"""Inspect an uncertified rain shadow on the same $500 high and low tape."""

import argparse
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
import gzip
from hashlib import sha256
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from . import collector, expansion_contract, expansion_live_rain, expansion_weather, study, venue_archive
from .market import _price, decimal, iso, parse_time
from .nbm import climate_window
from .paper_runs import FEE_SCHEDULE, _fee_at, _freeze
from .strategy_rules import StrategyRule, book_status
from .strategy_runs import (
    DEFAULT_DATA, FIXED, Predictor, digest, freeze_book_probabilities, frozen_low_predictor,
    json_bytes, run_trial, slice_tape, write_new,
)


VERSION = "shared-paper-rain-shadow-v1"
POLY = venue_archive.POLY
CHOICES = {"shadow only": "rain-shadow", "held temperature strategy": "joint-high-guarded-low",
           "shared bankroll with rain shadow": "joint-with-rain-shadow"}


def native_events(data, start, cutoff):
    with venue_archive.connect(data) as db:
        db.execute("BEGIN")
        rows = db.execute("""WITH selected AS (
            SELECT DISTINCT instrument_id FROM books WHERE venue=? AND at BETWEEN ? AND ?
            ) SELECT m.* FROM selected s JOIN markets m ON m.venue=? AND m.instrument_id=s.instrument_id
              WHERE m.family='daily-rain' AND m.first_received_at<=? AND m.instrument_id LIKE 'raincli-rain-daily-%'
              ORDER BY m.day,m.station,m.instrument_id""",
                          (POLY, venue_archive.receipt_time(start), venue_archive.receipt_time(cutoff), POLY,
                           venue_archive.receipt_time(cutoff))).fetchall()
        events, errors = [], []
        for row in map(dict, rows):
            try:
                events.append(_one_event(db, row, start, cutoff))
            except (KeyError, TypeError, ValueError) as error:
                errors.append({"instrument_id": row["instrument_id"], "reason": str(error)})
    return events, errors


def _one_event(db, row, start, cutoff):
    station, ident = row["station"], row["instrument_id"]
    day = date.fromisoformat(row["day"])
    if not (row["resolution_source"] == "NWS Daily Climate Report" and row["operator"] == "at-least"
            and row["units"] == "in" and decimal(row["lower"]) == decimal("0.01")
            and expansion_contract.rain_rule_matches(day, station, row["rules_primary"])):
        raise ValueError("Exact NWS liquid precipitation threshold and rule could not be verified")
    lat, lon, zone, offset = expansion_weather.STATIONS[station]
    native = tuple(parse_time(row[key]) for key in ("climate_starts_at", "climate_ends_at"))
    if native != climate_window(day, offset):
        raise ValueError("Native rain window differs from its exact NWS reporting day")
    if not all(row[key] for key in ("native_id", "yes_side_id", "no_side_id", "opened_at", "closed_at")):
        raise ValueError("Incomplete native venue identity or trading clock")
    if row["yes_side_id"] == row["no_side_id"] or decimal(row["quantity_step"]) not in {decimal("0.01"), decimal(1)}:
        raise ValueError("Unverified side identity or quantity step")
    close = parse_time(row["closed_at"])
    publication_cutoff = datetime.combine(day + timedelta(days=1), time(14), ZoneInfo("America/New_York"))
    books = [{**book, "http_cache": book.get("http_headers", {})} for book in
             venue_archive._book_query(db, POLY, ident, start=start, end=cutoff)]
    earliest = max(parse_time(row["opened_at"]), parse_time(row["first_received_at"]))
    bucket = {"id": ident, "market_id": row["native_id"], "yes_side_id": row["yes_side_id"], "no_side_id": row["no_side_id"],
              "quantity_step": row["quantity_step"], "label": "Official liquid precipitation ≥0.01 in", "rules": row["rules_primary"],
              "books": [row for row in books if earliest <= parse_time(row["at"])], "threshold_in": 0.01}
    if (row["settlement_receipt_id"] and row["settlement_source"] and row["settlement_received_at"] and
            parse_time(row["settlement_received_at"]) <= cutoff and row["yes_payout"] is not None):
        bucket["settlement"] = {"yes_payout": row["yes_payout"], "available_at": row["settlement_received_at"],
                                "source": row["settlement_source"], "receipt_id": row["settlement_receipt_id"]}
    return {"id": ident, "native_id": row["native_event_id"], "venue": POLY, "family": "daily-rain", "station": station,
            "date": row["day"], "title": row["title"], "lat": lat, "lon": lon, "timezone": zone, "standard_utc_offset_hours": offset,
            "starts_at": iso(earliest), "ends_at": iso(min(close, publication_cutoff)), "settles_at": iso(publication_cutoff),
            "climate_starts_at": iso(native[0]), "climate_ends_at": iso(native[1]), "source": row["rules_url"],
            "native_venue_close_at": row["closed_at"], "source_publication_cutoff": iso(publication_cutoff), "buckets": [bucket],
            "observations": [], "forecasts": []}


def rain_model(data):
    artifact = expansion_live_rain.load(data)
    if artifact is None:
        raise ValueError("Required earlier daily-rain shadow artifact unavailable")

    def predict(event, at):
        if not (value := expansion_live_rain.predict(artifact, event, at)):
            return {}
        estimate, = value["buckets"]
        info = {key: value.get(key) for key in ("gauge_witness", "gauge_error_allowance", "gauge_validation_days",
                                                "gauge_conflicts", "preceding_forecast_band_support", "forecast", "knowledge_at")}
        return {estimate["id"]: {**estimate, "uncertain": True, "source_model_uncertain": estimate.get("uncertain"),
                                "model_version": value["version"], "model_vintage": artifact["vintage"],
                                "model_method": value["method"], "forecast_available_at": value["available_at"],
                                "forecast_issued_at": value["issued_at"], "strategy_context": info}}

    return artifact["vintage"], predict


def quote_diagnostics(tape, probabilities, phase):
    checks, regions, best = Counter(), set(), {}
    fee = _fee_at(FEE_SCHEDULE)
    rules = StrategyRule("rain-shadow", "origin-confirmed", FIXED.minimum_dollars)
    for event in tape["events"]:
        if event.get("family") != "daily-rain":
            continue
        bucket, = event["buckets"]
        for book in bucket["books"]:
            at = parse_time(book["at"])
            checks["rain_book_receipts"] += 1
            if at >= parse_time(event["ends_at"]):
                checks["after_report_cutoff"] += 1
                continue
            if gap := book_status(book, None, at, "origin-confirmed"):
                checks[gap] += 1
                continue
            checks["origin_qualified"] += 1
            estimate = probabilities(event, at).get(bucket["id"])
            if not estimate:
                checks["model_inputs_unavailable"] += 1
                continue
            regions.add((event["station"], event["date"]))
            checks["origin_with_model_probability"] += 1
            checks["uncertain_model_probability"] += bool(estimate.get("uncertain"))
            checks["measurable_gauge_witness_at_local_receipt"] += bool(estimate["strategy_context"].get("gauge_witness"))
            for side in ("YES", "NO"):
                quoted = _price(event, bucket, side, book, estimate, at, decimal(25), 0, fee_at=fee, rules=rules)
                if not quoted.get("quantity"):
                    continue
                checks["priced_side_contexts"] += 1
                lower = estimate["yes_lower"] if side == "YES" else 1 - estimate["no_lower"]
                guarded = _price(event, bucket, side, book, {**estimate, "probability": lower}, at, decimal(25), 0,
                                 fee_at=fee, rules=rules)
                if (quoted["ev_dollars"] or 0) >= FIXED.minimum_dollars:
                    checks["point_estimate_positive_after_fee_contexts"] += 1
                if (guarded["ev_dollars"] or 0) >= FIXED.minimum_dollars:
                    checks["interval_lower_positive_after_fee_contexts"] += 1
                ident = event["station"]
                if ident not in best or quoted["ev_dollars"] > best[ident]["decision"]["ev_dollars"]:
                    best[ident] = {"station": ident, "date": event["date"], "at": iso(at), "venue": POLY,
                                   "event_id": event["id"], "instrument_id": bucket["id"], "title": f"{ident} {side} rain shadow",
                                   "detail": f"Displayed $25-cap side sees {quoted['probability']:.1%} point probability and ${quoted['ev_dollars']:.2f} modeled net EV. The probability is flagged uncertain.",
                                   "decision": quoted, "lower_support_quote": guarded, "books": [book], "window": phase}
    return {"window": phase, "modeled_station_days": len(regions), **dict(checks)}, list(best.values())


def assess(base, rain_evidence, baseline_model):
    original = base["capture"]
    combined = {**original, "events": [*original["events"], *rain_evidence["events"]]}
    start, split, end = map(parse_time, (combined["started_at"], base["split"], combined["captured_at"]))
    low = {"guarded": frozen_low_predictor(base["guarded_low_probabilities"]),
           "primary": frozen_low_predictor(base["low_probabilities"])}
    guarded_high = frozen_low_predictor(base["late_guarded_high_probabilities"]) if "late_guarded_high_probabilities" in base else None
    high_low = Predictor(baseline_model, low=low, high=guarded_high)(
        "joint-late-guarded-high-low" if guarded_high else "joint-high-guarded-low")
    predict_rain = frozen_low_predictor(rain_evidence["probabilities"])

    def strategy(name):
        def predict(event, at):
            if event.get("family") == "daily-rain":
                return {} if name == "joint-high-guarded-low" else predict_rain(event, at)
            return {} if name == "rain-shadow" else high_low(event, at)

        return predict

    metrics, cases, ledger, checks = [], [], {}, []
    for phase, tape in (("development", slice_tape(combined, start, split - timedelta(microseconds=1))),
                        ("forward", slice_tape(combined, split, end))):
        quote_stats, sample = quote_diagnostics(tape, predict_rain, phase)
        checks.append(quote_stats)
        cases.extend(sample)
        for label, name in CHOICES.items():
            result, trial = run_trial(tape, strategy, name, FIXED, "origin-confirmed", detailed=True, portfolio=True)
            metrics.append({"window": phase, "strategy": label, **result,
                            "rain_fills": sum(row.get("market_family") == "daily-rain" for row in trial["positions"]),
                            "rain_signals": sum(row.get("market_family") == "daily-rain" for row in trial["attempts"])})
            ledger[f"{phase}/{name}"] = {"positions": trial["positions"], "cash_over_time": trial["cash_over_time"]}
    return metrics, cases, checks, ledger


def report(data, source):
    base = json.loads(gzip.decompress(source.read_bytes()))
    base_hash = digest(base)
    if base_hash != source.name.removesuffix(".json.gz") or "guarded_low_probabilities" not in base:
        raise ValueError("Expected the hashed high and guarded-low paper evidence")
    artifact = study.load_selected(data, vintage=base["model_vintage"])
    if artifact is None:
        raise ValueError("The original high benchmark vintage is unavailable")
    start, cutoff = parse_time(base["capture"]["started_at"]), parse_time(base["capture"]["captured_at"])
    events, gaps = native_events(data, start, cutoff)
    anchored = _freeze(collector.enrich_capture(data, {"kind": "live", "started_at": iso(start), "captured_at": iso(cutoff),
                                                        "events": events}), cutoff)
    vintage, predict = rain_model(data)
    probabilities = freeze_book_probabilities(anchored, predict, family="daily-rain")
    evidence = {"engine": VERSION, "base_sha256": base_hash, "rain_vintage": vintage, "events": anchored["events"],
                "probabilities": probabilities, "native_adapter_gaps": gaps}
    metrics, cases, checks, ledger = assess(base, evidence, artifact)
    ident = "paper-rain-" + digest(evidence)[:20]
    snapshot = data / "research" / "evidence" / "rain-paper" / f"{ident}.json.gz"
    write_new(snapshot, gzip.compress(json_bytes({**evidence, "ledger": ledger}), mtime=0))
    output = {"id": ident, "title": "Daily-rain shadow on the shared $500 portfolio",
              "hypothesis": "Native measurable rain shadow probabilities can create after-fee display context while current support keeps them out of paper fills.",
              "target": "Exact Polymarket US rain threshold, temperature controls, and one station-day cash ledger.",
              "created_at": iso(datetime.now(timezone.utc)), "venues": [POLY],
              "date_window": {"from": iso(start), "split": base["split"], "through": iso(cutoff)},
              "metrics": [*metrics, *checks], "cases": cases,
              "limits": ["The trained rain shadow marks every probability uncertain. The paper broker withholds unqualified entries.",
                         "Point and interval-lower expected dollars are displayed independently of paper participation.",
                         "The normal source cutoff is 2 p.m. Eastern on the next day. Verified later revisions and exceptional rule contingencies can affect final payouts.",
                         "The original high and low portfolios and the added rain shadow all use the same frozen tape cutoff."],
              "provenance": {"engine": VERSION, "native_adapter_gaps": gaps, "base_input_sha256": base_hash,
                             "rain_vintage": vintage, "original_paper_evidence": str(source.resolve().relative_to(data.resolve())),
                             "frozen_rain_evidence": str(snapshot.relative_to(data)),
                             "frozen_rain_file_sha256": sha256(snapshot.read_bytes()).hexdigest()}}
    path = data / "research" / "strategies" / f"{ident}.json"
    write_new(path, json_bytes(output) + b"\n")
    return path, metrics, checks


def verify(data, record):
    expected = json.loads(record.read_bytes())
    proof = expected["provenance"]
    base = json.loads(gzip.decompress((data / proof["original_paper_evidence"]).read_bytes()))
    if digest(base) != proof["base_input_sha256"]:
        raise ValueError("Frozen high and low evidence differs from the original hash")
    blob = (data / proof["frozen_rain_evidence"]).read_bytes()
    if sha256(blob).hexdigest() != proof["frozen_rain_file_sha256"]:
        raise ValueError("Frozen rain evidence differs from the original hash")
    rain = json.loads(gzip.decompress(blob))
    saved_ledger = rain.pop("ledger")
    original = study.load_selected(data, vintage=base["model_vintage"])
    if original is None:
        raise ValueError("Original temperature model vintage unavailable")
    metrics, cases, checks, ledger = assess(base, rain, original)
    if expected["metrics"] != [*metrics, *checks] or expected["cases"] != cases or saved_ledger != ledger:
        raise ValueError("Shared rain shadow differs from the frozen strategy result")
    return {"id": expected["id"], "verified": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("evidence", type=Path, nargs="?")
    parser.add_argument("--verify", type=Path)
    args = parser.parse_args()
    if args.verify:
        print(json.dumps(verify(args.data, args.verify)))
        return
    if not args.evidence:
        parser.error("The original hashed high and low paper inputs are required")
    path, metrics, checks = report(args.data, args.evidence)
    print(json.dumps({"path": str(path), "metrics": metrics, "rain_book_evidence": checks}, indent=2))


if __name__ == "__main__":
    main()
