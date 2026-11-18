"""Replay the exact daily Arctic sea-ice extent selected by Kalshi contracts."""

import argparse
from collections import Counter
import csv
from datetime import date, datetime, time, timedelta, timezone
import gzip
from io import StringIO
import json
from pathlib import Path
import re
from statistics import NormalDist, mean, stdev
import sys

from sklearn import __version__ as sklearn_version
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from . import research_model, venue_archive, venue_storm
from .market import decimal, parse_time
from .venue_contracts import LONG_DAY, _long_day
from .venue_sources import PublicGET

VERSION = "arctic-daily-sea-ice-v2"
EARLIER = "arctic-daily-sea-ice-v1-32e7e933c8139de7"
TERMS = "https://assets.kalshi.com/contract_terms/ARCTICICE.pdf"
BASE = "https://noaadata.apps.nsidc.org/NOAA/G02135/north/daily/data/"
SOURCE = BASE + "N_seaice_extent_daily_v4.0.csv"
GUIDE = "https://nsidc.org/sites/default/files/documents/user-guide/g02135-v004-userguide.pdf"
METHOD = "https://nsidc.org/sea-ice-today/about-data"
ANNOUNCEMENT = ("https://nsidc.org/news-analyses/news-stories/arctic-sea-ice-has-reached-minimum-extent-2026-"
                "antarctic-sea-ice-maximum-most-likely-reached-well")
CITATION = ("Fetterer, Knowles, Meier, Savoie, Windnagel & Stafford (2025). Sea Ice Index G02135 Version 4, "
            "Boulder, Colorado, NSIDC, doi:10.7265/a98x-0f50. Northern Hemisphere daily extent.")
FIT, CAL, TEST = (1992, 2011), (2012, 2017), (2018, 2026)
PHASES = {"maximum": ((11, 15), (1, 15), (2, 15), (3, 1)), "minimum": ((5, 15), (6, 15), (7, 15), (8, 15), (9, 1))}
THRESHOLDS = {"maximum": ("13.8", "14", "14.2", "14.4", "14.6", "14.8", "15", "15.2"),
              "minimum": ("3.6", "3.8", "4", "4.2", "4.4", "4.6", "4.8", "5")}
MODELS = {"climate": "Earlier ten-season climatology", "analogs": "Current extent and earlier remaining seasonal change",
          "ridge": "Earlier Ridge on extent, persistence and two-week change"}
LAG = 3
NSIDC_NUMERIC = re.compile(r"^(\d+(?:\.\d+)?)(?: million(?: sq km)?)?$", re.I)


def _daytime(day):
    return datetime.combine(day, time.min, timezone.utc)


def _csv(raw):
    rows = csv.reader(StringIO(raw.decode("utf-8")), skipinitialspace=True)
    header = next(rows)
    next(rows)
    if [value.strip() for value in header[:6]] != ["Year", "Month", "Day", "Extent", "Missing", "Source Data"]:
        raise ValueError("Unexpected NSIDC daily extent columns")
    data = {}
    for year, month, day, extent, missing, inputs in rows:
        stamp = date(int(year), int(month), int(day))
        if stamp in data:
            raise ValueError(f"Duplicate date in NSIDC Northern Hemisphere daily extent: {stamp}")
        if decimal(extent) > 0:
            data[stamp] = {"extent": extent.strip(), "missing": missing.strip(), "inputs": inputs}
    return data


def _records(data):
    with venue_archive.connect(data) as db:
        return [dict(row) for row in db.execute(
            """SELECT instrument_id,series_id,event_id,opened_at,closed_at,status,operator,lower,upper,
            rules_primary,rules_secondary,rules_url,source,resolution_source,report_policy,fallback,settlement_at,
            settlement_received_at,settlement_source,yes_payout,expiration_value,receipt_id,settlement_receipt_id
            FROM markets WHERE venue=? AND family='sea-ice-extent' ORDER BY instrument_id""", (venue_archive.KALSHI,),
        )]


def _terms(contract):
    parsed = list(LONG_DAY.finditer(contract["rules_primary"] or ""))
    if len(parsed) != 2:
        return None
    first, last = (_long_day(match) for match in parsed)
    period = "maximum" if "MAX" in contract["series_id"] else "minimum" if "MIN" in contract["series_id"] else "short"
    return period, first, last


def _fetch_candles(data, contracts):
    get = PublicGET(data)
    received = 0
    for row in contracts:
        if (period := _terms(row)) is not None:
            earliest = min(period[1], date(period[2].year - 1, 11, 1)) if period[0] == "maximum" else period[1]
            received += venue_storm._receive_candle(data, get, row, first_at=_daytime(earliest))
    return received


def _sample(days, first, last, kind, *, complete):
    if last < first:
        return None
    rows = [(first + timedelta(days=n), days.get(first + timedelta(days=n))) for n in range((last-first).days + 1)]
    if complete and any(row is None for _, row in rows):
        return None
    available = [(day, row) for day, row in rows if row is not None]
    if not available:
        return None
    extreme = min if kind == "minimum" else max
    day, row = extreme(available, key=lambda pair: decimal(pair[1]["extent"]))
    return {"day": day.isoformat(), **row}


def _event_bounds(kind, season):
    if kind == "minimum":
        return date(season - 1, 12, 19), date(season, 10, 1)
    return date(season - 1, 12, 18), date(season, 4, 1)


def _snapshot(days, kind, season, month, day, *, first=None, last=None, received=None):
    first, last = (first, last) if first and last else _event_bounds(kind, season)
    decision = date(season - (month >= 11), month, day)
    before = min(decision - timedelta(days=LAG), last) if received is None else min(date.fromisoformat(received[:10]), last)
    eligible = [day for day in days if before - timedelta(days=5) <= day <= before]
    if not eligible:
        return None
    latest = max(eligible)
    fortnight = latest - timedelta(days=14)
    if fortnight not in days:
        return None
    recent = float(days[latest]["extent"])
    seen = _sample(days, first, latest, kind, complete=True)
    outcome = _sample(days, first, last, kind, complete=True)
    return {"kind": kind, "season": season, "date": decision.isoformat(), "phase": decision.strftime("%m-%d"),
            "last_input": latest.isoformat(), "days_left": (last - latest).days, "current": recent,
            "previous_14_day_change": recent - float(days[fortnight]["extent"]),
            "seen_extreme": float(seen["extent"]) if seen else None,
            "observed": float(outcome["extent"]) if outcome else None, "weather_receipt_at": received}


def _inputs(row):
    return [row["current"], row["seen_extreme"] if row["seen_extreme"] is not None else row["current"],
            row["previous_14_day_change"]]


def _training(days):
    output = {}
    for kind, phases in PHASES.items():
        for month, day in phases:
            panels = {name: [row for season in range(low, high + 1)
                             if (row := _snapshot(days, kind, season, month, day)) and row["observed"] is not None]
                      for name, (low, high) in {"fit": FIT, "calibrate": CAL, "test": TEST}.items()}
            if not all(panels.values()):
                raise ValueError(f"Incomplete NSIDC chronology for {kind} {month}-{day}")
            model = make_pipeline(StandardScaler(), Ridge(alpha=3.0)).fit(
                [_inputs(row) for row in panels["fit"]], [row["observed"] for row in panels["fit"]],
            )
            residuals = [row["observed"] - predicted for row, predicted in zip(
                panels["calibrate"], model.predict([_inputs(row) for row in panels["calibrate"]]), strict=True)]
            training = panels["fit"] + panels["calibrate"]
            earlier = training[-10:]
            output[kind, month, day] = {"model": model, "residuals": residuals, "climate": earlier,
                                        "analog": training, "test": panels["test"]}
    return output


def _beyond(value, op, threshold):
    if value is None or op not in ("above", "below"):
        return None
    return int(decimal(value) > decimal(threshold) if op == "above" else decimal(value) < decimal(threshold))


def _forecast(panel, row, op, threshold):
    limit = float(threshold)
    extreme = min if op == "below" else max
    observed = row["seen_extreme"]
    analogs = [row["current"] + (prior["observed"] - prior["current"]) for prior in panel["analog"]]
    if observed is not None:
        analogs = [extreme(observed, guess) for guess in analogs]
        if _beyond(observed, op, threshold):
            matched = {"climate": row["seen_extreme"], "analogs": mean(analogs), "ridge": float(panel["model"].predict([_inputs(row)])[0])}
            return {name: {"probability": 1.0, "expected_extent": extreme(observed, matched[name])} for name in MODELS}
    climate = [prior["observed"] for prior in panel["climate"]]
    ridge = float(panel["model"].predict([_inputs(row)])[0]) + mean(panel["residuals"])
    spread = max(0.10, stdev(panel["residuals"]))
    ridge_dist = NormalDist(ridge, spread)
    return {
        "climate": {"probability": (sum(_beyond(v, op, threshold) for v in climate) + 0.5) / (len(climate) + 1),
                    "expected_extent": mean(climate)},
        "analogs": {"probability": (sum(_beyond(v, op, threshold) for v in analogs) + 0.5) / (len(analogs) + 1),
                    "expected_extent": mean(analogs)},
        "ridge": {"probability": ridge_dist.cdf(limit) if op == "below" else 1 - ridge_dist.cdf(limit),
                  "expected_extent": extreme(observed, ridge) if observed is not None else ridge},
    }


def _obs(days, contract):
    if (terms := _terms(contract)) is None:
        return {}
    kind, first, last = terms
    native = contract["expiration_value"] or ""
    naked = NSIDC_NUMERIC.fullmatch(native)
    number = naked[1] if naked else None
    op, threshold = contract["operator"], contract["lower"] or contract["upper"]
    archive = _sample(days, first, last, kind, complete=True)
    return {"kind": kind, "first": first.isoformat(), "last": last.isoformat(), "operator": op, "threshold": threshold,
            "native_printed": native or None, "native_exact": number,
            "native_implied": _beyond(number, op, threshold),
            "archive_extreme": archive, "archive_implied": _beyond(archive["extent"], op, threshold) if archive else None}


def _replays(data, days, contracts, panels, receipt, fixed_quotes=None):
    replay, current = [], []
    received = parse_time(receipt["received_at"])
    for contract in contracts:
        observed = _obs(days, contract)
        evidence = {"contract": contract, "audit": observed, "forecasts": []}
        replay.append(evidence)
        if not observed or not observed["threshold"] or observed["kind"] not in PHASES:
            continue
        first, last = date.fromisoformat(observed["first"]), date.fromisoformat(observed["last"])
        kind, op, threshold = observed["kind"], observed["operator"], observed["threshold"]
        for month, day in PHASES[kind]:
            clock = date(last.year - (month >= 11), month, day)
            if clock > received.date():
                continue
            frozen_receipt = receipt["received_at"] if clock == received.date() and contract["status"] == "open" else None
            decision = received if frozen_receipt else _daytime(clock)
            if not (contract["opened_at"] and contract["closed_at"] and
                    parse_time(contract["opened_at"]) <= decision < parse_time(contract["closed_at"])):
                continue
            snapshot = _snapshot(days, kind, last.year, month, day, first=first, last=last, received=frozen_receipt)
            if snapshot is None:
                continue
            models = _forecast(panels[kind, month, day], snapshot, op, threshold)
            decision_at = decision.isoformat()
            display = (venue_storm._pre_decision_price(data, contract, decision) if fixed_quotes is None else
                       fixed_quotes.get(contract["instrument_id"], {}).get(decision_at))
            forecast = {**snapshot, "models": models, "display": display, "decision_at": decision_at,
                        "threshold": threshold, "instrument_id": contract["instrument_id"], "payout": contract["yes_payout"]}
            evidence["forecasts"].append(forecast)
            if frozen_receipt:
                current.append(forecast)
    return replay, current


def _score(rows, label):
    metrics = []
    if not rows:
        return metrics
    for name in rows[0]["models"]:
        values = [row["models"][name] for row in rows]
        brier = mean((p["probability"] - row["outcome"])**2 for p, row in zip(values, rows, strict=True))
        mae = (mean(abs(p["expected_extent"] - row["observed"]) for p, row in zip(values, rows, strict=True))
               if all(p["expected_extent"] is not None for p in values) else None)
        metrics.append({"cohort": label, "model": MODELS.get(name, name), "n": len(rows), "brier": brier,
                        "mae": mae, "independent_years": len({row["season"] for row in rows})})
    return metrics


def _case(row):
    contract, audit = row["contract"], row["audit"]
    label = contract["instrument_id"]
    day = audit.get("last")
    verdict = "Native payout pending" if contract["yes_payout"] is None else f"Native YES paid ${contract['yes_payout']}"
    if audit.get("native_printed"):
        verdict += f" with expiration value {audit['native_printed']}"
    if obs := audit.get("archive_extreme"):
        verdict += f". The current daily archive shows {obs['extent']} million km² on {obs['day']}"
    if contract["operator"] == "conflicting":
        verdict += ". The primary rule and native listing specify different directions"
    if headline := audit.get("announcement"):
        verdict += (f". NSIDC's September 23 headline prints a rounded minimum of {headline['reported_extent']} million km². "
                    f"Using that printed number as the strict threshold result would make YES {headline['threshold_result']}")
    return {"title": label, "venue": venue_archive.KALSHI, "instrument_id": label, "date": day,
            "at": contract["settlement_at"], "detail": verdict + ".", "data": row}


def _announcement(data, days):
    text, receipt = venue_storm._get(data, "nsidc-2026-09-23.html", ANNOUNCEMENT, folder="arctic")
    page = venue_storm.Page(text).text()
    found = re.search(r"minimum extent for the year, at (\d+\.\d+) million square kilometers .*? on September 12, 2026", page)
    if found is None or "SEPTEMBER 23, 2026" not in page.upper():
        raise ValueError("NSIDC's public release did not verify the announced Arctic minimum")
    report_day = date(2026, 9, 12)
    values = [decimal(days[report_day - timedelta(days=n)]["extent"]) for n in range(5)]
    return {"issued_date": "2026-09-23", "observation_label_date": str(report_day), "reported_extent": found[1],
            "unsmoothed_on_report_label": days[report_day]["extent"],
            "current_file_5day_trailing_average_on_report_label": str(sum(values) / len(values)),
            "receipt": receipt, "method": METHOD}


def evaluate(data, raw, receipt, contracts, *, announcement=None, fixed_quotes=None):
    days = _csv(raw)
    panels = _training(days)
    evaluation, metrics = [], []
    for kind, month, day in panels:
        panel = panels[kind, month, day]
        op = "below" if kind == "minimum" else "above"
        rows = [{**row, "threshold": threshold, "outcome": _beyond(row["observed"], op, threshold),
                 "models": _forecast(panel, row, op, threshold)} for row in panel["test"] for threshold in THRESHOLDS[kind]]
        evaluation.extend(rows)
        metrics.extend(_score(rows, f"{kind.title()} {month:02}-{day:02}"))
    metrics = _score(evaluation, "All later calendar-date and threshold cases") + metrics
    replays, current = _replays(data, days, contracts, panels, receipt, fixed_quotes=fixed_quotes)
    if announcement is None:
        announcement = _announcement(data, days)
    for row in replays:
        if row["contract"]["event_id"] == "KXARCTICICEMIN-26OCT01":
            audit = row["audit"]
            audit["announcement"] = {**announcement, "threshold_result": _beyond(
                announcement["reported_extent"], audit["operator"], audit["threshold"])}
    matched = [{**f, "outcome": int(f["payout"]), "models": {**f["models"], "Kalshi candle midpoint": {
        "probability": f["display"]["probability"], "expected_extent": None}}} for row in replays for f in row["forecasts"]
               if f["payout"] in ("0", "1") and f["display"]]
    metrics += _score(matched, "Same historical decision clock and actual Kalshi payout")
    counts = Counter()
    for row in replays:
        contract, observed = row["contract"], row["audit"]
        payout = contract["yes_payout"]
        if payout not in ("0", "1"):
            continue
        counts["verified binary payouts"] += 1
        counts["primary rule/native direction conflicts"] += int(contract["operator"] == "conflicting")
        for key in ("native_implied", "archive_implied"):
            if observed.get(key) is not None:
                counts[key + " checked"] += 1
                counts[key + " differs"] += int(observed[key] != int(payout))
        if publication := observed.get("announcement"):
            counts["headline substitution checked"] += 1
            counts["headline substitution changes outcome"] += int(publication["threshold_result"] != int(payout))
    picked = {"ARCTICICEMAX-24APR01-T15.2", "ARCTICICEMIN-23OCT01-T4.3", "KXARCTICICEMAX-25APR01-T14.4",
              "KXARCTICICEMAX-26APR01-T14.3", "KXARCTICICEMAX-27APR01-T14.4", "KXARCTICICEMIN-25OCT01-T4.3",
              "KXARCTICICEMIN-26OCT01-T4.6"}
    corpus = {"version": VERSION, "source_receipt": receipt, "contracts": contracts, "announcement": announcement,
              "matching_candles": {row["contract"]["instrument_id"]: {
                  event["decision_at"]: event["display"] for event in row["forecasts"] if event["display"]} for row in replays}}
    frozen, digest = research_model.freeze_inputs(data, corpus)
    result = {
        "version": VERSION, "title": "Arctic daily sea-ice extent above and below native thresholds",
        "hypothesis": "Current extent and growth or melt sharpen climatology for the actual Arctic seasonal criterion.",
        "target": "NSIDC Northern Hemisphere individual daily extent in million km²", "venues": ["Kalshi"], "supersedes": EARLIER,
        "date_window": {"model fit seasons": FIT, "probability calibration seasons": CAL, "held seasons": TEST,
                        "source labels": (str(min(days)), str(max(days)))},
        "coverage": {"official daily rows": len(days), "historical threshold cases": len(evaluation),
                     "forecast seasons": len({row["season"] for row in evaluation}), "market instruments": len(contracts),
                     "settlement_audit": dict(counts), "historical price comparisons": len(matched),
                     "open contracts with same-day observed forecast": len(current),
                     "open contracts with coherent price": sum(bool(row["display"]) for row in current)},
        "missing": {"native direction conflicts": counts["primary rule/native direction conflicts"],
                    "source original publication times": "Unavailable as a historical series"},
        "metrics": metrics, "cases": [_case(row) for row in replays if row["contract"]["instrument_id"] in picked],
        "limits": ["Earlier daily extents come from the current Version 4 file. Original day-level publication clocks and earlier file vintages are unavailable.",
                   "The historical replay waits three nominal days because NSIDC may replace a day using the following day's satellite inputs.",
                   "NSIDC moved the input sensor to AMSR2 for observations from 2025 onward. Individual readings remain sensitive to grid and retrieval method.",
                   "Historical melt and freeze outcomes use the exact unsmoothed published extent and are grouped by season and date.",
                   "Native market directions that conflict with primary terms and expiration values expressed as bounds carry no inferred numeric payout.",
                   "Kalshi archived daily candle closing prices provide display probabilities with no historical executable quantity."],
        "provenance": {"frozen_input": str(frozen.relative_to(data)), "frozen_input_sha256": digest,
                       "source": receipt, "citation": CITATION, "manual": GUIDE, "terms": TERMS, "headline": announcement,
                       "scikit_learn": sklearn_version,
                       "source_sha256": research_model.run_sources(sys.modules[__name__], venue_storm)},
    }
    return research_model.write_record(data, result, {"historical_weather": evaluation, "native_contracts": replays,
                                                       "current_forecasts": current})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--source-file")
    parser.add_argument("--input", type=Path)
    parser.add_argument("--candles", action="store_true")
    args = parser.parse_args(argv)
    if args.input:
        corpus = json.loads(gzip.decompress(args.input.read_bytes()))
        name = Path(corpus["source_receipt"]["path"]).name
        raw, receipt = venue_storm._existing(args.data, name, folder="arctic")
        if receipt != corpus["source_receipt"]:
            raise ValueError("Daily NSIDC source receipt differs from the frozen experiment")
        print(evaluate(args.data, raw, receipt, corpus["contracts"], announcement=corpus["announcement"],
                       fixed_quotes=corpus["matching_candles"]), flush=True)
        return
    contracts = _records(args.data)
    if args.candles:
        print(f"Received {_fetch_candles(args.data, contracts)} daily Kalshi candle points", flush=True)
    name = args.source_file or datetime.now(timezone.utc).strftime("nsidc-north-v4-%Y%m%dT%H.csv")
    raw, receipt = venue_storm._get(args.data, name, SOURCE, folder="arctic")
    print(evaluate(args.data, raw, receipt, contracts), flush=True)


if __name__ == "__main__":
    main()
