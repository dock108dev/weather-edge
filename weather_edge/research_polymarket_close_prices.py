"""Compare the display marks at listed and verified native temperature closes."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import timedelta
import gzip
from hashlib import sha256
import json
from pathlib import Path
from statistics import mean
import sys

from . import research_gefs, research_model, venue_archive
from .market import iso, parse_time

VERSION = "polymarket-native-close-price-v1"
MAX_PRICE_AGE = timedelta(minutes=5)
MIN_CLOCK_DIFFERENCE = timedelta(minutes=30)


def _display_mark(raw):
    try:
        bid, ask = 1 - float(raw["shortPrice"]), float(raw["longPrice"])
        if 0 <= bid <= ask <= 1 and ask - bid <= .30:
            return (bid + ask) / 2
    except (KeyError, TypeError, ValueError):
        pass
    return None


def _at(points, cutoff):
    return next((point for point in reversed(points)
                 if cutoff - MAX_PRICE_AGE <= parse_time(point["available_at"]) <= cutoff), None)


def _freeze(data, study):
    source = json.loads(study.read_bytes())
    payload = (data / source["provenance"]["replay_details_path"]).read_bytes()
    checksum = sha256(payload).hexdigest()
    if checksum != source["provenance"]["replay_details_sha256"]:
        raise ValueError("Parent CLOSED-book input checksum differs")
    audited = json.loads(gzip.decompress(payload))["matched_closed_books"]
    rows, omissions = [], Counter()
    with venue_archive.connect(data) as db:
        db.execute("BEGIN")
        for book in audited:
            earlier = parse_time(book["event_end_at"])
            close = parse_time(book["source_proof"]["closeSetTime"])
            if close - earlier < MIN_CLOCK_DIFFERENCE or book["yes_payout"] not in ("0", "1"):
                omissions["Less than 30 minutes between clocks or no verified binary payout"] += 1
                continue
            points = []
            for at, raw, receipt_id in db.execute(
                """SELECT at,body,receipt_id FROM history WHERE kind='display-price-1m' AND venue=?
                AND instrument_id=? AND at>=? AND at<=? ORDER BY at""",
                (venue_archive.POLY, book["instrument_id"], iso(earlier - MAX_PRICE_AGE - timedelta(minutes=1)), iso(close)),
            ):
                evidence = json.loads(raw)
                if (price := _display_mark(evidence)) is not None:
                    points.append({"at": at, "available_at": iso(parse_time(at) + timedelta(minutes=1)),
                                   "yes": price, "receipt_id": receipt_id, "original_display": evidence})
            nominal, native = _at(points, earlier), _at(points, close)
            if nominal is None or native is None:
                omissions["No recent coherent display mark at both clocks"] += 1
                continue
            rows.append({"contract": book, "nominal": nominal, "native": native, "display": points})
    return {"version": VERSION, "parent_id": source["id"], "parent_proof_sha256": checksum,
            "audited_contracts": len(audited), "omissions": dict(omissions), "rows": rows}


def _case(row):
    book = row["contract"]
    old, new = row["nominal"]["yes"], row["native"]["yes"]
    return {"venue": book["venue"], "instrument_id": book["instrument_id"], "station": book["station"],
            "date": book["day"], "at": book["event_end_at"], "title": f"{book['station']} {book['day']} · {book['label']}°F",
            "detail": f"The display midpoint was {old:.1%} at the nominal event end and {new:.1%} before the observed native close. Verified native YES paid ${book['yes_payout']}.",
            "venue_price_timeline": {"markers": [
                {"label": "Nominal event end", "at": book["event_end_at"]},
                {"label": "Native last trade", "at": book["source_proof"]["lastTradeSetTime"]},
                {"label": "Observed native close", "at": book["source_proof"]["closeSetTime"]},
            ], "series": [{"label": "Historical display midpoint", "points": row["display"]}]},
            "data": {key: row[key] for key in ("nominal", "native", "contract")}}


def run(data, *, source=None, frozen=None):
    if frozen is None:
        inputs = _freeze(data, source)
        frozen, digest = research_model.freeze_inputs(data, inputs)
    else:
        data_in = gzip.decompress(frozen.read_bytes())
        digest, inputs = sha256(data_in).hexdigest(), json.loads(data_in)
    if inputs["version"] != VERSION or not (rows := inputs["rows"]):
        raise ValueError("No exact paired price and payout evidence")
    station_days = {(row["contract"]["station"], row["contract"]["day"]) for row in rows}
    dates = sorted({day for _, day in station_days})
    scores, deltas = [], defaultdict(list)
    for key, title in (("nominal", "Display at nominal event end"), ("native", "Display before native close")):
        errors = [(row[key]["yes"] - int(row["contract"]["yes_payout"])) ** 2 for row in rows]
        scores.append({"method": key, "label": title, "contracts": len(rows), "calendar_dates": len(dates),
                       "station_days": len(station_days), "bucket_brier": mean(errors)})
    for row in rows:
        truth = int(row["contract"]["yes_payout"])
        deltas[row["contract"]["day"]].append((row["native"]["yes"] - truth) ** 2 - (row["nominal"]["yes"] - truth) ** 2)
    extremes = sorted(rows, key=lambda row: row["native"]["yes"] - row["nominal"]["yes"])
    return research_model.write_record(data, {
        "version": VERSION, "title": "Polymarket US price evidence after the nominal event end",
        "hypothesis": "The later native closing clock changes the quality of the last available historical probability proxy.",
        "target": "Exact Polymarket US daily high and low payout outcomes with independently observed native close.",
        "venues": [venue_archive.POLY], "date_window": {"evaluated": [dates[0], dates[-1]]}, "metrics": scores,
        "comparisons": [{"baseline": "nominal", "method": "native", "metric": "brier", "calendar_dates": len(dates),
                         "delta": mean(mean(day) for day in deltas.values()),
                         "date_bootstrap_interval_95": research_gefs._mean_interval(deltas)}],
        "coverage": {"audited_closed_books": inputs["audited_contracts"], "missing": inputs["omissions"],
                     "five_point_moves": sum(abs(row["native"]["yes"] - row["nominal"]["yes"]) >= .05 - 1e-10 for row in rows)},
        "cases": [_case(extremes[0]), _case(extremes[-1])],
        "limits": ["Native CLOSED book statistics are used in retrospect to locate a comparison clock. They are not presumed to be known before the actual close.",
                   "Displayed minute marks use a one-minute availability assumption and a maximum five-minute age at either clock. A valid two-sided display proxy has at most 30¢ spread.",
                   "The probability comparison uses verified venue payouts. Display records provide no executable depth."],
        "provenance": {"frozen_inputs": str(frozen.relative_to(data)), "frozen_input_sha256": digest,
                       "parent_close_audit": inputs["parent_id"], "parent_proof_sha256": inputs["parent_proof_sha256"],
                       "run_sources": research_model.run_sources(sys.modules[__name__], venue_archive)},
    }, inputs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    args = parser.add_mutually_exclusive_group(required=True)
    args.add_argument("--source", type=Path)
    args.add_argument("--frozen", type=Path)
    opts = parser.parse_args()
    print(run(opts.data, source=opts.source, frozen=opts.frozen))


if __name__ == "__main__":
    main()
