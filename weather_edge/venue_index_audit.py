"""Check published Weather Index computations against retained station and calibration evidence."""

import argparse
from collections import defaultdict
from decimal import ROUND_HALF_UP
import gzip
import json
from pathlib import Path
from statistics import mean
import sys

from . import research_model, venue_archive, venue_index
from .market import decimal, parse_time

VERSION = "weather-index-calibration-audit-v2"
EARLIER_RUN = "weather-index-calibration-audit-v1-17fb6ad08cd2421c"
SINGLE_STATION = {"nyc": "KLGA1M", "chicago": "KORD1M", "miami": "KMIA1M", "la-coastal": "KLAX1M"}


def recreate(point, configuration):
    """Apply the filed units and offsets to the already accepted audit samples."""
    if not configuration:
        return None
    accepted = {row["station_id"]: row for row in point["body"].get("stations", ())
                if row.get("code") == "ok" and row.get("temp_f") is not None}
    configured = {row["station_id"]: row for row in configuration["stations"]}
    if not accepted or len(accepted) != point["contributors"] or not accepted.keys() <= configured.keys():
        return None
    values = []
    sum_weights = decimal(0)
    center = decimal(configuration["city_reference_c"])
    for name, source in accepted.items():
        params = configured[name]
        reading = decimal(source["temp_f"])
        correction = (center - decimal(params["offset_c"])) * decimal("1.8")
        weight = decimal(params["weight"])
        values.append((name, reading, correction, weight))
        sum_weights += weight
    if sum_weights <= 0:
        return None
    reconstructed = sum(weight * (reading + correction) for _, reading, correction, weight in values) / sum_weights
    unadjusted = sum(weight * reading for _, reading, _, weight in values) / sum_weights
    return {"reconstructed": reconstructed, "unadjusted": unadjusted,
            "per_station": [{"station": name, "reading_f": str(reading), "offset_correction_f": str(correction),
                             "weight": str(weight), "raw": accepted[name]} for name, reading, correction, weight in values],
            "all_members_present": len(values) == len(configured)}


def gather(data, corpus):
    output = []
    versions = {city: venue_index.calibrations(data, city) for city in SINGLE_STATION}
    for case in corpus["rows"]:
        if case["date"] < "2026-11-10":
            continue
        index = case["settlement_point"]
        timeline = versions[case["city"]]
        configuration = case["config"]
        if timeline:
            minute = parse_time(index["at"])
            eligible = [row for row in timeline["value"]
                        if venue_index.index_time(row["effective_at_ms"]) <= minute]
            configuration = eligible[-1] if eligible else None
        computed = recreate(index, configuration)
        if computed is None:
            continue
        actual = decimal(index["temperature_f"])
        rounded = computed["reconstructed"].quantize(decimal("0.01"), rounding=ROUND_HALF_UP)
        offsetless = computed["unadjusted"].quantize(decimal("0.01"), rounding=ROUND_HALF_UP)
        single = next((decimal(row["reading_f"]) for row in computed["per_station"]
                       if row["station"] == SINGLE_STATION[case["city"]]), None)
        provided = {}
        for market in case["prices"]:
            threshold = decimal(market["threshold_f"])
            observed = market["native_payout_yes"]
            provided[market["instrument_id"]] = {
                "market": market, "native": observed, "reconstructed": int(rounded > threshold),
                "offsetless": int(offsetless > threshold), "single": int(single > threshold) if single is not None else None,
            }
        output.append({"city": case["city"], "region": case["region"], "date": case["date"], "target_at": case["target_at"],
                       "event_id": case["event_id"], "index": index, "status": index["status"],
                       "configuration": configuration["config_version"], "calibration_receipt_id": timeline["receipt_id"] if timeline else None,
                       "actual_f": str(actual),
                       "reconstructed_f": str(rounded), "offsetless_f": str(offsetless),
                       "single_station_f": str(single) if single is not None else None,
                       "absolute_error_f": float(abs(rounded - actual)), "per_station": computed["per_station"],
                       "all_members_present": computed["all_members_present"], "markets": provided})
    total = sum(case["date"] >= "2026-11-10" for case in corpus["rows"])
    return {"version": VERSION, "rows": output, "unreconstructable_hours": total - len(output)}


def _metrics(rows):
    markets = [item for row in rows for item in row["markets"].values()]
    gaps = [row["absolute_error_f"] for row in rows]
    return {"hourly_events": len(rows), "calendar_dates": len({row["date"] for row in rows}),
            "city_days": len({(row["city"], row["date"]) for row in rows}), "thresholds": len(markets),
            "exact_value_matches": sum(value == 0 for value in gaps),
            "within_one_cent_F": sum(value <= 0.01 for value in gaps),
            "mean_absolute_reconstruction_error_F": mean(gaps) if gaps else None,
            "native_payout_changes_reconstructed": sum(item["native"] != item["reconstructed"] for item in markets),
            "native_payout_changes_no_offsets": sum(item["native"] != item["offsetless"] for item in markets),
            "native_payout_changes_single_station": sum(item["single"] is not None and item["native"] != item["single"] for item in markets),
            "single_station_covered_thresholds": sum(item["single"] is not None for item in markets)}


def evaluate(data, corpus):
    data = Path(data)
    frozen, digest = research_model.freeze_inputs(data, corpus)
    rows = corpus["rows"]
    panels = defaultdict(list)
    for row in rows:
        panels[row["status"]].append(row)
        panels[row["city"]].append(row)
    coverage = {"all reconstructable hours": _metrics(rows), **{key: _metrics(panel) for key, panel in panels.items()}}
    examples = []
    seen = set()
    for row in sorted(rows, key=lambda item: (-sum(case["native"] != case["single"]
                                                   for case in item["markets"].values() if case["single"] is not None),
                                               item["date"], item["target_at"])):
        if row["city"] in seen:
            continue
        items = row["markets"].values()
        alternative = next((case for case in items if case["single"] is not None and case["single"] != case["native"]), None)
        if alternative is None:
            continue
        market = alternative["market"]
        examples.append({
            "title": f"{row['region']} · same city, different hourly temperature",
            "date": row["date"], "at": row["target_at"], "venue": venue_archive.KALSHI,
            "instrument_id": market["instrument_id"], "data": {"hour": row, "market": market},
            "detail": f"Verified metro index {row['actual_f']}°F. The selected airport was {row['single_station_f']}°F. "
                      f"Native YES paid ${alternative['native']}.",
        })
        seen.add(row["city"])
    result = {"version": VERSION, "supersedes": EARLIER_RUN, "title": "Stations, offsets and the real hourly payout",
              "hypothesis": "Published station inputs and the effective configuration reproduce canonical index values. "
                            "One airport's temperature can resolve the same threshold differently.",
              "target": "Native hourly index and settlement thresholds", "venues": ["Kalshi"],
              "date_window": {"audited afternoons and overnight hours": ["2026-11-10", "2026-11-14"]},
              "coverage": coverage, "missing": {"ambiguous accepted members": corpus["unreconstructable_hours"]},
              "cases": examples, "limits": [
                  "The calculation uses the provider's accepted member readings. It does not rerun exchange quality control.",
                  "Calibration is expressed in Celsius and applied before Fahrenheit rounding. The audit tolerates one "
                  "hundredth Fahrenheit in its second precision count and reports the exact result separately.",
                  "An airport comparison is a weather illustration. The native metro index and payout remain authoritative.",
              ],
              "provenance": {"frozen_input": str(frozen.relative_to(data)), "frozen_input_sha256": digest,
                             "calibration_api": venue_index.CALIBRATION_DOCS,
                             "source_sha256": research_model.run_sources(sys.modules[__name__])}}
    return research_model.write_record(data, result, {"hourly_payout_audits": rows})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--input", type=Path, required=True)
    args = parser.parse_args(argv)
    corpus = json.loads(gzip.decompress(args.input.read_bytes()))
    replay = corpus if corpus["version"] == VERSION else gather(args.data, corpus)
    print(evaluate(args.data, replay))


if __name__ == "__main__":
    main()
