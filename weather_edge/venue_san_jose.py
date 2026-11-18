"""Compare the real San Jose contract terms and historical temperature sampling."""

import argparse
from calendar import monthrange
from collections import defaultdict
import csv
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
import gzip
from hashlib import sha256
from io import StringIO
import json
from pathlib import Path
import re
from statistics import mean
import sys

from . import expansion_weather, research_model, strategy_twc, venue_archive, venue_snow_data, venue_storm
from .collector import paused
from .market import iso, parse_time
from .venue_sources import CollectionPaused

VERSION = "san-jose-hourly-vs-daily-minima-v2"
EARLIER = "san-jose-hourly-vs-daily-minima-v1-0ec63ce6b84e91fa"
STATION = "KSJC"
NATIVE_IDS = ((venue_archive.KALSHI, "KXSJCLOWT-26DEC31-40"), (venue_archive.POLY, "tc-temp-sjc-dec-lt40"))
LOCAL_STANDARD = timezone(timedelta(hours=-8))
MONTHS = (1, 2, 3, 11, 12)
THRESHOLDS = (32, 35, 38, 40, 42)
CLI = re.compile(r"^([0-9]{12})-KMTR-CDUS46-CLISJC$")
NOAA_GHCNH = "https://www.ncei.noaa.gov/products/global-historical-climatology-network-hourly"
NOAA_ISD = "https://www.nesdis.noaa.gov/news/service-location-change-integrated-surface-data-global-hourly"
GOOD_TEMPERATURE = {"0", "1", "4", "5"}


def _daily_reports(data):
    reports, sources, gaps = {}, [], []
    for year in range(1997, 2027):
        path = data / "venues" / "snow-climate" / STATION / f"{year}.json"
        if not path.is_file():
            gaps.append(f"Missing annual CLI archive for {year}")
            continue
        manifest = json.loads(path.with_suffix(".json.receipt.json").read_bytes())
        raw = path.read_bytes()
        if sha256(raw).hexdigest() != manifest["sha256"]:
            raise ValueError(f"Archived climate data changed for {year}")
        kept = 0
        for item in json.loads(raw)["results"]:
            match = CLI.fullmatch(item.get("product") or "")
            daily_low = item.get("low")
            if item.get("station") != STATION or not match or type(daily_low) not in (int, float):
                continue
            day = date.fromisoformat(item["valid"])
            released = datetime.strptime(match[1], "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
            through = datetime.combine(day + timedelta(days=1), time.min, tzinfo=LOCAL_STANDARD)
            if through <= released < through + timedelta(days=7):
                if day in reports:
                    raise ValueError(f"Duplicate climate day {day}")
                reports[day] = {"minimum_f": daily_low, "published_at": iso(released), "product": item["product"],
                                "minimum_time_local_standard": item.get("low_time"), "source": venue_snow_data.IEM_TEXT + item["product"]}
                kept += 1
        if not kept:
            gaps.append(f"Annual CLI archive had no complete KSJC reports for {year}")
        sources.append(manifest)
    return reports, sources, gaps


def _isd_rows(raw, *, extended):
    reader = csv.DictReader(StringIO(raw.decode("utf-8-sig")), delimiter="|" if extended else ",")
    for row in reader:
        if extended:
            if row["STATION"] != "USW00023293" or row["temperature_Source_Station_ID"] != "724945-23293":
                continue
            text, quality = row["temperature"], row["temperature_Quality_Code"]
            report = row["temperature_Report_Type"]
        else:
            if row["STATION"] != "72494523293" or row["CALL_SIGN"].strip() not in ("KSJC", "SJC"):
                continue
            text, _, quality = row["TMP"].partition(",")
            report = row["REPORT_TYPE"].strip().replace("-", "")
        if quality not in GOOD_TEMPERATURE or report not in ("FM15", "FM16") or not text:
            continue
        value = Decimal(text) if extended else Decimal(text) / 10
        if not -30 <= value <= 55:
            continue
        at = parse_time(row["DATE"] + "Z")
        yield at, value * Decimal("1.8") + 32, report


def _hourly(data):
    groups, sources, gaps = defaultdict(dict), [], []
    for year in range(1997, 2027):
        extended = year >= 2025
        try:
            path = Path(f"{year}-ghcnh.psv" if extended else f"{year}-global-hourly.csv")
            raw, metadata = venue_storm._existing(data, path, folder="san-jose")
        except FileNotFoundError:
            gaps.append(f"Missing NOAA hourly station archive for {year}")
            continue
        sources.append(metadata)
        for at, temp, report in _isd_rows(raw, extended=extended):
            loc = at.astimezone(LOCAL_STANDARD)
            # November through March supports independent December and
            # neighbouring-month cohorts. Keep routine and special minima apart.
            if loc.month not in MONTHS:
                continue
            day = groups[loc.date()]
            key = (loc.hour, report)
            previous = day.get(key)
            if previous is None or temp < previous[0]:
                day[key] = (temp, at, metadata["sha256"])
    return groups, sources, gaps


def _weather_frames(daily, hourly):
    pairs = []
    for year in range(1997, 2026):
        for month in MONTHS:
            n_days = monthrange(year, month)[1]
            dates = [date(year, month, number) for number in range(1, n_days + 1)]
            delivered = [daily[day] for day in dates if day in daily]
            slots = [point for day in dates for (hour, report), point in hourly.get(day, {}).items() if report == "FM15"]
            special = [point for day in dates for (hour, report), point in hourly.get(day, {}).items() if report == "FM16"]
            if len(delivered) != n_days or len(slots) < 0.95 * n_days * 24:
                continue
            best_daily = min(dates, key=lambda day: daily[day]["minimum_f"])
            routine = min(slots)
            any_point = min(slots + special)
            prev_month = dates[0] - timedelta(days=1)
            # This candidate uses antecedent observations with generous
            # publication margin even for December in the current session.
            previous = [daily[key]["minimum_f"] for i in range(1, 15) if (key := prev_month.replace(day=i)) in daily
                        and parse_time(daily[key]["published_at"]) <= datetime.combine(prev_month.replace(day=15), time(16), timezone.utc)]
            pairs.append({"year": year, "month": month, "first": str(dates[0]), "daily_low_f": daily[best_daily]["minimum_f"],
                          "daily_point": {"date": str(best_daily), **daily[best_daily]}, "routine_low_f": str(routine[0]),
                          "routine_rounded_low_f": int(routine[0].quantize(Decimal("1"), rounding=ROUND_HALF_UP)),
                          "routine_point": {"at": iso(routine[1]), "archive_sha256": routine[2]},
                          "any_report_low_f": str(any_point[0]), "any_point": {"at": iso(any_point[1]), "archive_sha256": any_point[2]},
                          "routine_hours_covered": len(slots), "expected_hours": n_days * 24,
                          "antecedent_daily_min_mean_f": mean(previous) if len(previous) >= 10 else None,
                          "antecedent_daily_report_count": len(previous)})
    return pairs


def _recent_evidence(data):
    return strategy_twc.reports(data, station=STATION, start="2026-11-01", end="2026-11-14")


def _original(data, report, collect):
    if not report:
        return None
    name = report["product"]
    relative = Path(STATION) / (name + ".txt")
    path = data / "venues" / "snow-climate" / relative
    if not collect and not path.is_file():
        return None
    if collect and paused(data):
        raise CollectionPaused
    raw, receipt = venue_snow_data._obtain(data, relative, report["source"])
    body = raw.decode().replace("\r\n", "\n")
    reported = expansion_weather.CLI_DATE.search(body)
    if not reported or not re.search(r"^CLISJC\s*$", body, re.M) or not re.search(r"^\s*YESTERDAY\b", body, re.M):
        return None
    month, day, year = reported.groups()
    if date(int(year), expansion_weather.MONTHS[month], int(day)).isoformat() != report["date"]:
        return None
    minimum = next(((int(value), at) for field, value, at in expansion_weather.TEMPERATURE.findall(body)
                    if field == "MINIMUM" and value not in ("M", "MM")), None)
    return {"observed_minimum_f": minimum[0], "minimum_time": minimum[1], **receipt} if minimum else None


def _book(data, venue, instrument):
    latest = venue_archive.books(data, venue, instrument, limit=1)
    if not latest:
        return None
    book = latest[0]
    return {key: book.get(key) for key in ("at", "requested_at", "source_at", "cache_age_seconds", "http_headers", "source", "receipt_id")} | {
        key: (book.get(key) or [None])[0] for key in ("yes_bids", "yes_offers", "no_bids", "no_offers")}


def gather(data, *, collect=False):
    daily, cli_sources, cli_gaps = _daily_reports(data)
    hourly, noaa_sources, noaa_gaps = _hourly(data)
    pairs = _weather_frames(daily, hourly)
    markets = {}
    for venue, instrument in NATIVE_IDS:
        market = venue_archive.detail(data, venue, instrument)
        if market and market["station"] == STATION:
            markets[venue] = {"native": {key: market[key] for key in (*venue_archive.MARKET_COLUMNS, "receipt", "settlement_receipt")},
                              "book": _book(data, venue, instrument)}
    eligible = [row for row in pairs if (_passes(row, "daily", 40) != _passes(row, "routine", 40))]
    eligible.sort(key=lambda row: (row["month"] == 12, row["year"]), reverse=True)
    selected = eligible[:6]
    if not selected:
        selected = sorted((row for row in pairs if row["month"] == 12), key=lambda row: row["daily_low_f"], reverse=True)[:4]
    originals = {}
    for row in selected:
        try:
            original = _original(data, row["daily_point"], collect)
        except (OSError, ValueError):
            original = None
        if original:
            originals[row["first"]] = original
    return {"version": VERSION, "pairs": pairs, "markets": markets, "recent_twc": _recent_evidence(data),
            "source_receipts": cli_sources + noaa_sources, "complete_originals": originals,
            "missing": cli_gaps + noaa_gaps}


def _passes(row, target, threshold):
    field = {"daily": "daily_low_f", "routine": "routine_low_f", "rounded_routine": "routine_rounded_low_f",
             "routine_and_special": "any_report_low_f"}[target]
    return Decimal(str(row[field])) < Decimal(threshold)


def _candidate(fit, row, target, threshold):
    eligible = [item for item in fit if item["month"] == row["month"]]
    base = (sum(_passes(item, target, threshold) for item in eligible) + 1) / (len(eligible) + 2)
    value = row["antecedent_daily_min_mean_f"]
    nearby = [item for item in eligible if value is not None and item["antecedent_daily_min_mean_f"] is not None]
    closest = sorted(nearby, key=lambda item: abs(item["antecedent_daily_min_mean_f"] - value))[:5]
    conditional = (sum(_passes(item, target, threshold) for item in closest) + 3 * base) / (len(closest) + 3)
    return {"Earlier same-month frequency": base, "Prior climate matched by recent minimum": conditional}


def evaluate(data, corpus):
    frozen, digest = research_model.freeze_inputs(data, corpus)
    frames = corpus["pairs"]
    fits, held = [row for row in frames if row["year"] <= 2016], [row for row in frames if row["year"] >= 2017]
    methods = ("daily", "routine", "rounded_routine", "routine_and_special")
    audit = {}
    for label, subset in (("complete cold-season months", frames), ("complete Decembers", [row for row in frames if row["month"] == 12])):
        audit[label] = {"dates": len(subset), "thresholds": {
            str(t): {**{method: sum(_passes(row, method, t) for row in subset) for method in methods},
                     "daily versus routine disagreements": sum(_passes(row, "daily", t) != _passes(row, "routine", t) for row in subset)}
            for t in THRESHOLDS}}
    predictions = []
    for row in held:
        if row["antecedent_daily_min_mean_f"] is None:
            continue
        for target in ("daily", "routine"):
            for threshold in THRESHOLDS:
                for name, value in _candidate(fits, row, target, threshold).items():
                    predictions.append({"method": name, "target": target, "threshold_f": threshold,
                                        "case": row["first"], "month": row["month"], "year": row["year"], "probability": value,
                                        "outcome": _passes(row, target, threshold)})
    metrics = []
    for label, subset in (("All held cold-season months", predictions), ("Held Decembers at the exact 40°F strike",
                           [item for item in predictions if item["month"] == 12 and item["threshold_f"] == 40])):
        groups = defaultdict(list)
        for row in subset:
            groups[(row["method"], row["target"])].append(row)
        for (name, target), cases in groups.items():
            metrics.append({"group": label, "model": name, "target": target,
                            "brier": mean((row["probability"] - row["outcome"]) ** 2 for row in cases),
                            "cases": len(cases), "months": len({row["case"] for row in cases}),
                            "independent_calendar_years": len({row["year"] for row in cases})})
    cases = []
    markets = corpus["markets"]
    left, right = markets.get(venue_archive.KALSHI), markets.get(venue_archive.POLY)
    all_40_diffs = [row for row in frames if _passes(row, "daily", 40) != _passes(row, "routine", 40)]
    if left and right:
        primary, other = left["native"], right["native"]
        cases.append({"venue": primary["venue"], "instrument_id": primary["instrument_id"], "related_venue": other["venue"],
                      "related_instrument_id": other["instrument_id"], "station": STATION, "date": "2026-12-01",
                      "at": (left["book"] or {}).get("at"), "title": "Same airport and strike, different temperature sampling",
                      "detail": "Kalshi requires a TWC hourly value below 40°F. Polymarket US uses the complete NWS daily report. Both are still open.",
                      "data": {"native_evidence": markets, "historical_processing": audit,
                               "complete_original_source_examples": corpus["complete_originals"]}})
        for row in sorted(all_40_diffs, key=lambda item: (item["month"] == 12, item["year"]), reverse=True)[:4]:
            cases.append({"venue": primary["venue"], "instrument_id": primary["instrument_id"], "related_venue": other["venue"],
                          "related_instrument_id": other["instrument_id"], "station": STATION, "date": row["daily_point"]["date"],
                          "at": row["daily_point"]["published_at"], "title": f"Historical KSJC whole-month threshold on {row['first']}",
                          "detail": f"NWS daily minimum {row['daily_low_f']}°F and NOAA routine hourly minimum {row['routine_low_f']}°F "
                                    "fall on different sides of 40°F.",
                          "data": {**row, "original_nws_report": corpus["complete_originals"].get(row["first"])}})
    record = {"version": VERSION, "supersedes": EARLIER, "title": "San Jose daily lows and hourly cold readings",
              "hypothesis": "The same airport and below-40°F threshold can produce different outcomes with hourly sampling and daily extrema.",
              "target": "Complete airport CLI daily lows and separately sourced NOAA routinely reported hourly temperatures",
              "date_window": {"fit years": [2008, 2016], "held years": [2017, 2025], "current rule": ["2026-12-01", "2026-12-31"]},
              "venues": ["Kalshi", "Polymarket US"], "coverage": audit, "metrics": metrics, "cases": cases,
              "missing": corpus["missing"],
              "limits": ["NOAA routine and special observations supply independent weather evidence. Historical official TWC hourly observations were unavailable for the domestic station.",
                         "The samples retain raw tenths Celsius converted to Fahrenheit, plus a whole-degree nearest value sensitivity.",
                         "A month has all NWS daily minima and at least 95% of routine observation hours. Absence in a missing hour remains unverified.",
                         "At least ten of the first fourteen daily observations in the prior month must have published before the antecedent comparison.",
                         "No outcomes from either December 2026 contract were available. Public book source clocks determine usability separately."],
              "provenance": {"NOAA hourly files": NOAA_ISD, "NOAA new hourly documentation": NOAA_GHCNH,
                             "frozen_input": str(frozen.relative_to(data)), "frozen_input_sha256": digest,
                             "source_sha256": research_model.run_sources(sys.modules[__name__], venue_snow_data, venue_storm)}}
    return research_model.write_record(data, record, {"paired_months": frames, "held_predictions": predictions, "native": markets})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--input", type=Path)
    parser.add_argument("--collect", action="store_true", help="Retain complete NWS source reports for selected examples")
    args = parser.parse_args(argv)
    try:
        corpus = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else gather(args.data, collect=args.collect)
        print(evaluate(args.data, corpus))
    except CollectionPaused:
        print("Collection is stopped. Retained receipts are available.")
        return 2


if __name__ == "__main__":
    sys.exit(main())
