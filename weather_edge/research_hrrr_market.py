"""Compare the stronger intraday high weather models with matched venue display prices."""
from __future__ import annotations

import argparse
from collections import Counter
import gzip
from hashlib import sha256
import json
from pathlib import Path
from statistics import mean
import sys

from . import research_hrrr_intraday, research_model, strategy_guarded_anchor, strategy_high_transfer, venue_archive

VERSION = "hrrr-intraday-price-benchmark-v1"
METHODS = {"market": "Normalized venue display midpoint", **research_hrrr_intraday.CANDIDATES,
           "quarter_weather": "75% venue, 25% equal weather blend"}
AUTUMN_CLOCK_REPAIR = {"hrrr-intraday-high-v1-c45b48e906377332": "hrrr-intraday-price-benchmark-v1-d8ba5ff7f1fcb637"}


def _native_clocks(db, venue, row):
    buckets = []
    for bucket in row["buckets"]:
        native = db.execute("""SELECT opened_at,closed_at,settlement_at,settlement_receipt_id,receipt_id,station,day,yes_payout
                             FROM markets WHERE venue=? AND instrument_id=?""", (venue, bucket["instrument_id"])).fetchone()
        if native is None or (native["station"], native["day"], native["yes_payout"]) != (row["station"], row["date"], str(bucket["payout"])):
            return []
        buckets.append({**bucket, **dict(native)})
    return buckets


def inputs(data, source):
    record = json.loads(source.read_bytes())
    proof = record["provenance"]
    evidence = (data / proof["replay_details_path"]).read_bytes()
    if record["version"] != research_hrrr_intraday.VERSION or sha256(evidence).hexdigest() != proof["replay_details_sha256"]:
        raise ValueError("Unexpected or changed held-out intraday forecast research input")
    paired, gaps = {}, Counter()
    with venue_archive.connect(data) as db:
        cohorts = json.loads(gzip.decompress(evidence))["cohorts"]
        for venue in venue_archive.VENUES:
            paired[venue] = []
            for row in cohorts.get(venue, []):
                buckets = _native_clocks(db, venue, row)
                reference = {**row, "buckets": buckets,
                             "predictions": {strategy_high_transfer.MODELS[3]: row["estimates"]["blend"]["probabilities"]}}
                frame = strategy_guarded_anchor._price_frame(db, data, venue, reference) if buckets else None
                if frame is None:
                    gaps[f"{venue}: {row['local_hour']} LST"] += 1
                    continue
                paired[venue].append(frame)
    return {"version": VERSION, "study": record["id"], "study_sha256": proof["replay_details_sha256"],
            "windows": record["date_window"], "paired": paired, "gaps": dict(gaps)}


def estimate(row):
    market = [item["market"] for item in row["buckets"]]
    blend = [.75 * p + .25 * w for p, w in zip(market, row["estimates"]["blend"]["probabilities"], strict=True)]
    added = {name: {"probabilities": vector, "brier": mean((p - y) ** 2 for p, y in zip(vector, row["truth"], strict=True))}
             for name, vector in (("market", market), ("quarter_weather", blend))}
    return {**row, "estimates": {**row["estimates"], **added}}


def run(data, source, frozen):
    if frozen is None:
        evidence = inputs(data, source)
        frozen, digest = research_model.freeze_inputs(data, evidence)
    else:
        body = gzip.decompress(frozen.read_bytes())
        evidence, digest = json.loads(body), sha256(body).hexdigest()
    if evidence["version"] != VERSION or not any(evidence["paired"].values()):
        raise ValueError("The price benchmark needs at least one same-clock market and weather match")
    metrics, comparisons, cases = [], [], []
    matched = {venue: [estimate(row) for row in rows] for venue, rows in evidence["paired"].items()}
    for venue, rows in matched.items():
        if not rows:
            continue
        for group in ("all", 13, 17):
            group_rows = rows if group == "all" else [row for row in rows if row["local_hour"] == group]
            if not group_rows:
                continue
            name = f"{venue} · {group}"
            shape = {"group": name, "calendar_dates": len({r["date"] for r in group_rows}), "snapshots": len(group_rows),
                     "station_days": len({(r["station"], r["date"]) for r in group_rows}),
                     "bucket_decisions": sum(len(r["buckets"]) for r in group_rows)}
            metrics.extend({**shape, "method": key, "label": label,
                            "bucket_brier": mean(r["estimates"][key]["brier"] for r in group_rows)} for key, label in METHODS.items())
            comparisons.extend({"group": name, **research_model.date_bootstrap(group_rows, "market", key, "brier")}
                               for key in ("hrrr", "blend", "quarter_weather"))
        signal = max(rows, key=lambda row: max(abs(q["market"] - q["guarded"]) for q in row["buckets"]))
        i, bucket = max(enumerate(signal["buckets"]), key=lambda item: abs(item[1]["market"] - item[1]["guarded"]))
        cases.append({"station": signal["station"], "date": signal["date"], "at": signal["price_evidence_at"],
                      "venue": venue, "instrument_id": bucket["instrument_id"], "title": f"{signal['station']} weather and venue disagreement",
                      "detail": f"Venue display proxy {bucket['market']:.1%}, equal HRRR and NBM blend {bucket['guarded']:.1%}, "
                                f"fixed 25% weather combination {signal['estimates']['quarter_weather']['probabilities'][i]:.1%}. "
                                f"This native contract paid ${bucket['payout']} YES.",
                      "data": {"market": bucket, "weather_clock": signal["at"], "official_report": signal["official_report"]}})
    repair = AUTUMN_CLOCK_REPAIR.get(evidence["study"])
    return research_model.write_record(data, {"version": VERSION, "title": "Intraday HRRR and NBM guidance versus venue displays",
              "hypothesis": "Weather skill from a physically guarded HRRR blend could improve the same-case venue-only proxy.",
              "target": "Confirmed daily highs with complete exact-payout and synchronized pre-close price evidence at 13:05 and 17:05 LST.",
              "venues": sorted(matched), "date_window": evidence["windows"], "metrics": metrics, "comparisons": comparisons,
              **({"supersedes": repair} if repair else {}),
              "cases": cases, "coverage": {"excluded_weather_predictions_missing_price_or_native_clock": evidence["gaps"]},
              "limits": ["Every bucket needs a coherent native display quote inside market trading hours. Hourly Kalshi book candles and one-minute Polymarket displays are historical projections of prices.",
                         "The weather forecast remains frozen five minutes earlier. The weather fraction is fixed at 25% for sensitivity. "
                         "No parameter was fitted from these held-out prices, and archived displays provide no order size for fills."],
              "provenance": {"input": evidence["study"], "input_sha256": evidence["study_sha256"],
                             "frozen_inputs": str(frozen.relative_to(data)), "frozen_input_sha256": digest,
                             "implementation_sha256": research_model.run_sources(sys.modules[__name__], strategy_guarded_anchor)}}, matched)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data", type=Path, default=Path(".data"))
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--source", type=Path)
    source.add_argument("--frozen", type=Path)
    args = p.parse_args()
    print(run(args.data, args.source, args.frozen))


if __name__ == "__main__":
    main()
