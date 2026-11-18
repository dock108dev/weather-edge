"""Compare early and late quarter heating-day estimates and their source fields."""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import gzip
import hashlib
import json
from pathlib import Path
import re
from statistics import mean
import sys
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import numpy as np

from . import expansion_weather, research_model, research_model_hdd as hdd
from .history import _write
from .nbm import iso

VERSION = "cincinnati-hdd-forecast-curve-v1"
CLOCKS = ((10, 14), (10, 31), (11, 14), (11, 30), (12, 7), (12, 14), (12, 21), (12, 28))
ACIS_DOCS = "https://docs.rcc-acis.org/acisws/"
ACIS = "https://data.rcc-acis.org/StnData"
RAW_HDD = re.compile(r"(?m)^DEGREE DAYS[ \t]*\n[ \t]*HEATING[ \t]*\n[ \t]*(YESTERDAY|TODAY)[ \t]+([0-9]+(?:\.[0-9]+)?|MM?)\b")


def _acis(data):
    params = {"sid": hdd.GHCN_STATION + " 6", "sdate": "1986-10-01", "edate": "2026-11-14",
              "elems": ["hdd65", "avgt", "maxt", "mint"], "meta": ["name", "sids", "ll", "elev"]}
    source = ACIS + "?" + urlencode({"params": json.dumps(params, separators=(",", ":"))})
    root = data / "expansion" / "hdd"
    pointer = root / "kcvg-acis-1986-2026-hdd.json"
    if pointer.is_file():
        receipt = json.loads(pointer.read_text())
        body = (data / receipt["raw_path"]).read_bytes()
        if hashlib.sha256(body).hexdigest() != receipt["body_sha256"] or receipt["source"] != source:
            raise ValueError("Archived ACIS station response failed verification")
    else:
        requested = datetime.now(timezone.utc)
        with urlopen(Request(source, headers={"Accept": "application/json", "User-Agent": "Weather Edge research"}), timeout=20) as response:
            body = response.read(3_000_001)
            received = datetime.now(timezone.utc)
            headers = {label: response.headers.get(label) for label in ("Date", "Last-Modified", "ETag")}
        content = json.loads(body)
        if not content.get("data") or not {hdd.GHCN_STATION + " 6", "KCVG 5", "72421 4"} <= set(content.get("meta", {}).get("sids", [])):
            raise ValueError("ACIS station metadata does not identify exact KCVG")
        checksum = hashlib.sha256(body).hexdigest()
        raw_path = root / f"kcvg-acis-1986-2026-hdd-{checksum[:16]}.json"
        receipt = {"source": source, "provider": "NOAA Regional Climate Centers ACIS", "request": params,
                   "requested_at": iso(requested), "received_at": iso(received), "headers": headers,
                   "raw_path": str(raw_path.relative_to(data)), "body_sha256": checksum, "station": content["meta"],
                   "publication_basis": "Historical daily observations with local retrieval clock"}
        _write(raw_path, body)
        _write(pointer, research_model._json_bytes(receipt))
    return json.loads(body), receipt


def _number(value):
    try:
        value = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return value if value.is_finite() else None


def _acis_days(content):
    output = {}
    for day, amount, average, high, low in content["data"]:
        parsed = _number(amount)
        if parsed is not None and 0 <= parsed <= 215:
            if day in output:
                raise ValueError("Duplicate date in exact-station ACIS observations")
            output[day] = {"hdd_65": float(parsed), "avg_f": average, "high_f": high, "low_f": low}
    return output


def _report(data, day, label):
    product = label["product"]
    original = expansion_weather.raw_report(data, hdd.STATION, day, product)
    if original is None:
        return None
    path = data / "expansion" / "cli" / hdd.STATION / f"{product}.txt"
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != original["sha256"]:
        return None
    match = RAW_HDD.search(raw.decode().replace("\r\n", "\n"))
    if match is None or match[1] != "YESTERDAY":
        return None
    value = _number(match[2])
    if value is None:
        return None
    return {**original, "hdd_65": float(value), "hdd_as_reported": match[2], "raw_path": str(path.relative_to(data)),
            "file_retained_at": iso(datetime.fromtimestamp(path.stat().st_mtime, timezone.utc))}


def _live_sources(data, live, station_days):
    first, last = hdd.FIRST_LIVE_DAY, date.fromisoformat(live["through"])
    summaries = expansion_weather.daily_labels(data, hdd.STATION, first, last)

    def retrieve(row):
        day = date.fromisoformat(row["date"])
        summary = summaries.get(row["date"])
        if summary is None:
            return {**row, "nws": None, "acis": station_days.get(row["date"])}
        try:
            official = _report(data, day, summary)
        except (OSError, ValueError, KeyError) as error:
            return {**row, "nws": None, "nws_gap": str(error), "acis": station_days.get(row["date"])}
        return {**row, "nws": official, "acis": station_days.get(row["date"])}

    with ThreadPoolExecutor(max_workers=4) as pool:
        return list(pool.map(retrieve, live["received_days"]))


def _seasons(station_days, prior):
    output = []
    for season in prior:
        year = season["year"]
        days = hdd._span(date(year, 10, 1), date(year, 12, 31))
        values = [station_days.get(str(day)) for day in days]
        if all(values):
            numbers = [row["hdd_65"] for row in values]
            output.append({"year": year, "daily_hdd": numbers, "ncei_daily_hdd": season["daily_hdd"],
                           "total": sum(numbers), "ncei_total": season["total"]})
    return output


def gather(data, baseline):
    seed = json.loads(gzip.decompress(baseline.read_bytes()))
    if seed["version"] != hdd.VERSION:
        raise ValueError("Expected archived original Cincinnati HDD inputs")
    native = hdd._live(data, datetime.now(timezone.utc))
    acis, original = _acis(data)
    station_days = _acis_days(acis)
    source_comparison = _live_sources(data, native, station_days)
    return {"version": VERSION, "history": _seasons(station_days, seed["history"]), "live": native,
            "latest_recorded_at": iso(datetime.now(timezone.utc)),
            "live_source_comparison": source_comparison,
            "acis_source": original, "reference": research_model._source_entry(data, baseline)}


def _at(rows, stage, field="daily_hdd"):
    output = []
    for row in rows:
        year = row["year"]
        values = row[field]
        through = date(year, *stage)
        count = (through - date(year, 10, 1)).days + 1
        output.append({**row, "through": str(through), "observed": sum(values[:count]),
                       "recent_14": sum(values[max(0, count - 14):count]), "remaining": sum(values[count:]), "total": sum(values)})
    return output


def _curve(seasons, thresholds, market_input):
    all_scores, comparisons, metrics = {}, [], []
    model_at_live_date = None
    for stage in CLOCKS:
        key = f"{stage[0]:02}-{stage[1]:02}"
        sample, model, residual = hdd._calibrate(_at(seasons, stage))
        test = hdd._score(sample, model, residual, thresholds)
        all_scores[key] = test
        if stage == hdd.AS_OF_DAY and len(market_input["received_days"]) == market_input["expected_days"]:
            live = {"observed": market_input["total"], "recent_14": market_input["recent_14"]}
            model_at_live_date = hdd._prediction(live, sample["fit"], model, residual, thresholds)
        for name, label in hdd.METHODS.items():
            errors = [row["estimates"][name]["brier"] for row in test]
            metrics.append({"group": f"Through {key}", "method": name, "label": label, "independent_seasons": len(test),
                            "strike_cases": len(test) * len(thresholds), "bucket_brier": mean(errors),
                            "mae_hdd": mean(row["estimates"][name]["mae_hdd"] for row in test)})
            if name == "quarter":
                continue
            delta = [row["estimates"][name]["brier"] - row["estimates"]["quarter"]["brier"] for row in test]
            simulated = np.random.default_rng(20261115).choice(delta, size=(3000, len(delta))).mean(axis=1)
            comparisons.append({"group": f"Through {key}", "method": name, "baseline": "quarter", "delta": mean(delta),
                                "season_bootstrap_ci_95": np.quantile(simulated, (0.025, 0.975)).tolist()})
    return metrics, comparisons, all_scores, model_at_live_date


def evaluate(data, inputs):
    frozen, digest = research_model.freeze_inputs(data, inputs)
    live, history = inputs["live"], inputs["history"]
    thresholds = sorted({float(row["upper"]) for row in live["markets"]})
    if not thresholds:
        raise ValueError("No exact archived Cincinnati HDD strikes")
    metrics, intervals, scored, live_estimates = _curve(history, thresholds, live)
    common = [row for row in inputs["live_source_comparison"]
              if row["nws"] and row["acis"] and row["hourly_counterfactual_hdd"] is not None]
    totals = {field: sum(float(getter(row)) for row in common) for field, getter in (
        ("TWC HDD (65)", lambda row: row["hdd_65"]), ("NWS complete CLI", lambda row: row["nws"]["hdd_65"]),
        ("ACIS hdd65", lambda row: row["acis"]["hdd_65"]), ("TWC hourly counterfactual", lambda row: row["hourly_counterfactual_hdd"]))}
    live_compare = Counter((float(row["hdd_65"]) == row["nws"]["hdd_65"], row["acis"]["hdd_65"] == row["nws"]["hdd_65"])
                           for row in common)
    metrics.extend({"group": "Live common station days", "method": "source_audit", "label": source,
                    "station_days": len(common), "accumulated_hdd": total} for source, total in totals.items())
    same = [row for row in history if hdd.WINDOWS["test"][0] <= row["year"] <= hdd.WINDOWS["test"][1]]
    history_comparison = {"complete_acis_and_ncei_seasons": len(history), "independent_test_seasons": len(same),
                          "historical_test_strike_disagreements": sum((row["total"] < strike) != (row["ncei_total"] < strike)
                             for row in same for strike in thresholds),
                          "mean_abs_quarter_total_difference_hdd": mean(abs(row["total"] - row["ncei_total"]) for row in same)}
    central = (live_estimates or {}).get("observed", {}).get("point_hdd", 1400)
    market = min(live["markets"], key=lambda row: abs(float(row["upper"]) - central))
    mismatch = sorted(common, key=lambda row: abs(float(row["hdd_65"]) - row["nws"]["hdd_65"]) +
                            abs(float(row["hdd_65"]) - float(row["hourly_counterfactual_hdd"])), reverse=True)
    example = mismatch[0] if mismatch else None
    cases = [{"station": hdd.STATION, "date": live["through"], "at": inputs["latest_recorded_at"], "venue": "kalshi",
              "instrument_id": market["instrument_id"], "title": "Cincinnati · Same-day heating fields and the live quarter",
              "detail": (f"The {len(common)} common labeled days total {totals['TWC HDD (65)']:.2f} HDD in the governing field and "
                         f"{totals['NWS complete CLI']:.2f} from complete raw NWS reports. Calculating every day from published hourly means "
                         f"would give {totals['TWC hourly counterfactual']:.2f}."),
              "market": market, "current_day_witness": example, "model_estimates": live_estimates, "thresholds": thresholds}]
    record = {"version": VERSION, "title": "When Cincinnati heating-day forecasts become more informative",
              "hypothesis": "As more daily heating values become known, earlier weather-conditioned quarter estimates should improve. "
                            "The same-station official daily field audit tests the impact of the contract's fallback hierarchy.",
              "target": "Cincinnati/Northern Kentucky International Airport HDD(65) between October 1 and December 31",
              "venues": ["kalshi"], "date_window": {**hdd.WINDOWS, "native": [str(hdd.FIRST_LIVE_DAY), str(hdd.LAST_LIVE_DAY)]},
              "coverage": {"live_received_days": len(live["received_days"]), "live_full_source_days": len(common),
                           "live_twc_nws_daily_agreement": sum(count for key, count in live_compare.items() if key[0]),
                           "live_acis_nws_daily_agreement": sum(count for key, count in live_compare.items() if key[1]),
                           **history_comparison},
              "metrics": metrics, "cases": cases, "uncertainty": intervals,
              "limits": ["The historical ACIS station HDD is a retrospective weather proxy. Original publication clocks and historical native quarter payouts are unavailable.",
                         "All consecutive checkpoints reuse the same independent seasons. The season bootstrap treats an entire season as one independent unit.",
                         "The native provider's daily HDD field has priority. The hourly mean is a counterfactual on these days and becomes eligible only if direct HDD is absent and the hourly value is Final.",
                         "The NWS fallback values are current verified copies of complete raw climate reports. Their WFO issue time and local retained-copy timestamp remain separate.",
                         "The 2026 quarter is open. These probabilities and sampled values do not establish its eventual settlement."],
              "provenance": {"frozen_input": str(frozen.relative_to(data)), "frozen_input_sha256": digest,
                             "filing": hdd.FILING, "acis_docs": ACIS_DOCS, "source": inputs["acis_source"],
                             "source_sha256": research_model.run_sources(sys.modules[__name__], hdd, expansion_weather)}}
    return research_model.write_record(data, record, {"replay": scored, "current_daily_fields": inputs["live_source_comparison"]})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    sources = parser.add_mutually_exclusive_group(required=True)
    sources.add_argument("--baseline", type=Path)
    sources.add_argument("--input", type=Path)
    args = parser.parse_args()
    inputs = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else gather(args.data, args.baseline)
    print(evaluate(args.data, inputs))


if __name__ == "__main__":
    main()
