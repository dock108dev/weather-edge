"""Test whether dense whole-Celsius airport samples improve same-case daily highs."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import timedelta
import gzip
from hashlib import sha256
import json
import math
from pathlib import Path
import sys

from . import omo, research_hrrr_intraday, research_model, strategy_high_transfer
from .market import parse_time

VERSION = "madis-dense-station-high-v1"
FIT = ("2026-10-01", "2026-10-18")
TEST = ("2026-10-29", "2026-11-13")
METHODS = {"nbm": "Hourly METAR and NBM", "published": "Add eligible public-file sample",
           "earlier_proxy": "Add earlier sample with 10-minute publication proxy",
           "direct_round": "Earlier whole-Celsius sample rounded directly"}


def inputs(data, source):
    record, parent = strategy_high_transfer._archived(data, source)
    selected, manifests, cached = {}, {}, {}
    missing = Counter()
    for split, dates in (("calibration", FIT), ("test", TEST)):
        selected[split] = []
        for row in parent[split]:
            if not dates[0] <= row["date"] <= dates[1]:
                continue
            decision = parse_time(row["at"])
            start = decision - timedelta(hours=6)
            station = row["station"]
            points = []
            for index in range(6):
                hour = start + timedelta(hours=index)
                if hour not in cached:
                    cached[hour] = omo.load(data, hour)
                frame = cached[hour]
                if frame is None:
                    continue
                if rows := frame["stations"].get(station):
                    manifests[frame["raw"]] = {key: frame[key] for key in ("source", "sha256", "available_at", "ingested_at")}
                    points.extend({**point, "source": frame["source"], "available_at": frame["available_at"],
                                   "archive": frame["raw"]} for point in rows)
            proxy, published = {}, {}
            for point in points:
                observed = parse_time(point["at"])
                lower_bound = max(parse_time(point["madis_received_at"]), parse_time(point["madis_modified_at"]))
                if lower_bound + timedelta(minutes=10) <= decision:
                    proxy[observed] = point
                if max(lower_bound, parse_time(point["available_at"])) <= decision:
                    published[observed] = point
            if len(proxy) < 24 or sum(at >= decision - timedelta(minutes=90) for at in proxy) < 6:
                missing[f"{split}: {station} · {row['local_hour']} LST, insufficient exact dense readings"] += 1
                continue
            selected[split].append({**row, "sample_count": len(proxy), "public_sample_count": len(published),
                                    "proxy_sample": max(proxy.values(), key=lambda p: p["temp_f"]),
                                    "public_sample": max(published.values(), key=lambda p: p["temp_f"], default=None)})
    return {"version": VERSION, **selected, "manifests": manifests, "gaps": dict(missing),
            "payouts": {venue: {f"{row['station']} {row['at']}": row["buckets"] for row in rows}
                        for venue, rows in parent["payout_cases"].items()}, "parent": record["id"],
            "parent_sha256": record["provenance"]["replay_details_sha256"]}


def conservative_low(sample):
    # The transmission uses whole Celsius. Allow the full one-degree conversion interval.
    return math.floor(32 + (sample["temp_c_whole"] - 1) * 1.8 + 1e-9)


def centers(row):
    baseline = row["physical_with_six_f"]
    proxy, public = row["proxy_sample"], row["public_sample"]
    return {"nbm": baseline, "published": max(baseline, conservative_low(public)) if public else baseline,
            "earlier_proxy": max(baseline, conservative_low(proxy)),
            "direct_round": max(baseline, int(math.floor(proxy["temp_f"] + .5)))}


def example(venue, row):
    winning = next(b for b in row["buckets"] if b["payout"] == 1)
    return {"station": row["station"], "date": row["date"], "at": row["at"], "venue": venue,
            "instrument_id": winning["instrument_id"], "title": f"{row['station']} five-minute observations",
            "detail": (f"The dense source reported {row['proxy_sample']['temp_c_whole']}°C, roughly "
                       f"{row['proxy_sample']['temp_f']:.1f}°F, versus a {row['all_observations_bound']['lower_f']}°F "
                       f"coherent METAR floor. The conservative dense floor was {conservative_low(row['proxy_sample'])}°F. "
                       f"The completed climate maximum was {row['actual_f']:.0f}°F."),
            "official_report": row["official_report"], "data": {key: row[key] for key in (
                "proxy_sample", "public_sample", "all_observations_bound", "estimates", "sample_count", "public_sample_count",
            )}, "bucket": winning}


def run(data, source, frozen):
    if frozen is None:
        evidence = inputs(data, source)
        frozen, digest = research_model.freeze_inputs(data, evidence)
    else:
        raw = gzip.decompress(frozen.read_bytes())
        evidence, digest = json.loads(raw), sha256(raw).hexdigest()
    if evidence["version"] != VERSION or not evidence["calibration"] or not evidence["test"]:
        raise ValueError("Both chronological windows need enough exact dense station readings")
    cohorts, reserve = research_hrrr_intraday._evaluate(evidence["calibration"], evidence["test"], evidence["payouts"], candidates=centers)
    scores, comparisons, cases = [], [], []
    for name, results in sorted(cohorts.items()):
        for group in ("all", 13, 17):
            rows = results if group == "all" else [row for row in results if row["local_hour"] == group]
            if not rows:
                continue
            grouping = f"{name} · {group}"
            scores.extend({**result, "bucket_decisions": sum(len(row["buckets"]) for row in rows)}
                          for result in research_model.metrics(rows, grouping, METHODS))
            comparisons.extend({**research_model.date_bootstrap(rows, "nbm", method, "brier"), "group": grouping}
                               for method in METHODS if method != "nbm")
        if name != "official":
            best = min(results, key=lambda row: row["estimates"]["earlier_proxy"]["brier"] - row["estimates"]["nbm"]["brier"])
            false_floor = next((row for row in results if row["proxy_sample"]["temp_f"] >= row["actual_f"] + .5), None)
            cases.append(example(name, best))
            if false_floor and false_floor != best:
                cases.append(example(name, false_floor))
    held = cohorts.get("official", [])
    record = {"version": VERSION, "title": "Five-minute MADIS source and exact-station high temperature",
              "hypothesis": "Dense observations can improve the estimated daily high before the coarse report rolls over.",
              "target": "Official daily highs and complete native daily-high payouts at 13 and 17 local standard time.",
              "venues": sorted(evidence["payouts"]), "date_window": {"calibration": FIT, "held": TEST},
              "metrics": scores, "comparisons": comparisons, "cases": cases,
              "coverage": {"native_granules": len(evidence["manifests"]), "earlier_matches": len(evidence["calibration"]),
                           "public_file_tightened_centers": sum(centers(row)["published"] > centers(row)["nbm"] for row in held),
                           "receipt_proxy_tightened_centers": sum(centers(row)["earlier_proxy"] > centers(row)["nbm"] for row in held),
                           "direct_round_above_official": sum(row["proxy_sample"]["temp_f"] >= row["actual_f"] + .5 for row in held),
                           "conservative_above_official": sum(conservative_low(row["proxy_sample"]) > row["actual_f"] for row in held),
                           "missing": evidence["gaps"]},
              "limits": ["The sampled public files contain five-minute station readings recovered in whole degrees Celsius. "
                         "Direct conversion cannot resolve adjacent integer Fahrenheit contracts. The alternate one-degree envelope affects the model center only.",
                         "The native internal MADIS receipt and modification clocks plus ten minutes are a sensitivity proxy. "
                         "Only the preserved archive file's publication time establishes its known public availability in the conservative comparison. "
                         "The live collector's local receipt is a distinct clock.",
                         "Current center changes retain the old matched METAR guard and disagreement reserve. Exact payouts must agree with the completed station report."],
              "provenance": {"frozen_inputs": str(frozen.relative_to(data)), "frozen_input_sha256": digest, "parent": evidence["parent"],
                             "parent_sha256": evidence["parent_sha256"], "source_docs": omo.DOCS, "guard_reserve": reserve,
                             "run_sources": research_model.run_sources(sys.modules[__name__], omo, research_hrrr_intraday)}}
    return research_model.write_record(data, record, {"native_granules": evidence["manifests"], "cohorts": cohorts})


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
