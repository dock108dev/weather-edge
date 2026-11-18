"""Audit ski-opening payouts and test nearby SNOTEL observations against Alta opening dates."""

import argparse
from collections import Counter
import csv
from datetime import date, datetime, timedelta, timezone
import gzip
from hashlib import sha256
from io import StringIO
import json
from pathlib import Path
import re
from statistics import mean, median
import sys
from urllib.parse import quote, urlsplit
from urllib.request import Request, build_opener

from . import research_model, venue_archive, venue_storm
from .capture import ImmutableOrigin
from .collector import paused
from .history import _write
from .market import iso
from .venue_sources import CollectionPaused

VERSION = "ski-opening-schedule-and-snow-v2"
EARLIER_RUN = "ski-opening-schedule-and-snow-v1-fee5a77b780642ea"
OPERATOR = "https://www.alta.com"
WCC = "https://wcc.sc.egov.usda.gov"
PRIMARY = {
    "alta-2018": (OPERATOR + "/stories/a-deep-dive-into-a-deep-year", "opening day was all the way back on November 23rd"),
    "alta-2020": (OPERATOR + "/stories/2020-21-season-recap", "Alta opened on November 21st"),
    "alta-2022": (OPERATOR + "/stories/welcome-to-our-85th-winter", "opened for the season on November 18th"),
    "alta-2023": (OPERATOR + "/stories/2023-24-season-recap", "opened for the 2023"),
    "alta-2024": (OPERATOR + "/stories/2024-25-season-recap", "Opening Day of Alta's 87th season was Friday, November 22nd"),
    "alta-2025": (OPERATOR + "/stories/open-and-skiing", "Alta’s 88th season started on December 7th"),
    "alta-2019-window": (OPERATOR + "/stories/2019-20-season-recap", "Opening Weekend of the 2019"),
    "alta-2021-schedule": (OPERATOR + "/media/press-releases", "84th Season Opens November 24th"),
    "alta-current-plan": (OPERATOR + "/events/opening-day", "November 20th"),
    "alta-current-lifts": (OPERATOR + "/lift-terrain-status", None),
    "breck-early": ("https://news.vailresorts.com/2026-08-18-Is-Winter-About-to-Show-Off-Vail-Resorts-Announces-2026-27-Opening-Dates-as-Super-El-Nino-Builds-Anticipation-for-Great-Powder", "November 6"),
    "breck-later": ("https://news.vailresorts.com/BreckOpeningDay2026", "Nov. 11, 2026"),
    "keystone-plan": ("https://news.vailresorts.com/keystone-opening-2026", "October 30"),
    "copper-early": ("https://www.coppercolorado.com/tickets-passes/season-passes/copper-season-pass/", None),
    "copper-later": ("https://www.visitcoppermountain.com/copper-corner/fa0cx4rfzhd9ndx2xtjqagjiv8jxsd", "Sunday, November 8"),
    "copper-ops": ("https://www.coppercolorado.com/", "open for the 26/27 season"),
    "mammoth-plan": ("https://www.mammothmountain.com/things-to-do/events/opening-day", "November 5"),
    "mammoth-ops": ("https://www.mammothmountain.com/on-the-mountain/hours-of-operation", "Scheduled Lift Hours"),
    "winter-park-first": ("https://www.winterparkresort.com/the-mountain/mountain-report", "First day of skiing and riding is November 13, 2026"),
    "winter-park-ops": ("https://blog.winterparkresort.com/lift-updates/", None),
    "atwater-site": (WCC + "/nwcc/site?sitenum=1308", "SNOTEL Site: Atwater"),
    "snowbird-site": (WCC + "/nwcc/site?sitenum=766", "SNOTEL Site: Snowbird"),
}
OPENED_ALTA = {"alta-2018": "2018-11-23", "alta-2020": "2020-11-21", "alta-2022": "2022-11-18",
               "alta-2023": "2023-11-25", "alta-2024": "2024-11-22", "alta-2025": "2025-12-07"}
HISTORIC_NOV = ("11-22", "11-29", "12-06", "12-13", "12-20")
SOURCE_AUDITS = {
    "KXBRECKOPEN": ("2026-11-11", ("breck-later",), "revised operator announcement", "2026-11-06", "breck-early"),
    "KXCOPPEROPEN": ("2026-11-08", ("copper-later", "copper-ops"), "dated community first-day notice and later operator active status", "2026-11-06", "copper-early"),
    "KXKEYSTONEOPEN": ("2026-10-30", ("keystone-plan",), "operator announcement", None, None),
    "KXMAMMOTHOPEN": ("2026-11-05", ("mammoth-plan", "mammoth-ops"), "operator event date and operating-hours publication", None, None),
    "KXWINTERPARKOPEN": ("2026-11-13", ("winter-park-first",), "operator first-day statement", None, None),
}


def _nrcs_urls(station, begin):
    station_key = quote(f'{station}:UT:SNTL|id=""|name', safe="")
    interval = quote(f"{begin},2026-11-15", safe="")
    elements = quote("WTEQ::value,SNWD::value", safe="")
    return f"{WCC}/reportGenerator/view_csv/customSingleStationReport/daily/{station_key}/{interval}/{elements}"


def _public(data, alias, url, collect):
    root = data / "venues" / "ski-opening"
    archive = root / (alias + ".response")
    manifest = root / (alias + ".receipt.json")
    if archive.is_file() and manifest.is_file():
        raw, record = archive.read_bytes(), json.loads(manifest.read_bytes())
        if sha256(raw).hexdigest() != record["sha256"] or record["source"] != url:
            raise ValueError(f"Retained resort source changed for {alias}")
        return raw, record
    if not collect:
        raise FileNotFoundError(archive)
    if paused(data):
        raise CollectionPaused
    begin = iso(datetime.now(timezone.utc))
    if urlsplit(url).scheme != "https":
        raise ValueError("Unexpected resort evidence URL")
    opener = build_opener(ImmutableOrigin())
    request = Request(url, headers={"User-Agent": "Weather Edge public local research", "Accept-Encoding": "identity"}, method="GET")
    with opener.open(request, timeout=18) as response:
        raw = response.read(12_000_001)
        if len(raw) > 12_000_000:
            raise ValueError("Resort evidence exceeds the bounded source file")
        record = {"source": url, "requested_at": begin, "received_at": iso(datetime.now(timezone.utc)), "status": response.status,
                  "http_last_modified": response.headers.get("Last-Modified"), "sha256": sha256(raw).hexdigest(),
                  "path": str(archive.relative_to(data))}
    _write(archive, raw)
    _write(manifest, (json.dumps(record, separators=(",", ":")) + "\n").encode())
    return raw, record


def _nrcs(body, station, name):
    content = body.decode("utf-8-sig")
    if f"SNOTEL {station}: {name}, UT" not in content or "Instantaneous - Start of Day" not in content:
        raise ValueError(f"Unexpected Utah SNOTEL report for {station}")
    lines = [line for line in content.splitlines() if line and not line.startswith("#")]
    reader = csv.DictReader(StringIO("\n".join(lines)))
    result = {}
    for row in reader:
        key = row.get("Date")
        if not key or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", key):
            continue
        values = list(row.values())
        numbers = [float(item) if item and re.fullmatch(r"\d+(?:\.\d+)?", item) else None for item in values[1:]]
        if len(numbers) != 2 or key in result:
            raise ValueError(f"Unexpected SNOTEL row or duplicate date for {station}")
        result[key] = {"snow_water_equivalent_in": numbers[0], "depth_in": numbers[1]}
    if not result:
        raise ValueError("SNOTEL daily values not found")
    return result


def _with_receipt(data, market):
    detail = venue_archive.detail(data, market["venue"], market["instrument_id"])
    return {**market, "settlement_receipt": detail["settlement_receipt"],
            "recent_book": next((book for book in detail["books"] if book["depth_kind"] == "displayed_ladder"), None)}


def gather(data, *, collect=False):
    sources, missing, conditions = {}, {}, {}
    for alias, (url, phrase) in PRIMARY.items():
        try:
            raw, receipt = _public(data, alias, url, collect)
            text = venue_storm.Page(raw).text()
            if phrase:
                cheap = lambda value: " ".join(value.lower().replace("’", "'").split())
                if cheap(phrase) not in cheap(text):
                    missing[alias] = "Named operator evidence unavailable in the retained page"
                    continue
            hit = re.search(r"(conditions permitting|scheduled|projected|kicked off|opened)", text, re.I)
            sources[alias] = {**receipt, "required_phrase": phrase, "context_found": hit[1].lower() if hit else None}
        except (OSError, ValueError) as error:
            missing[alias] = str(error)
    for number, name, start in (("766", "Snowbird", "2018-09-01"), ("1308", "Atwater", "2022-09-01")):
        try:
            raw, receipt = _public(data, f"snotel-{number}", _nrcs_urls(number, start), collect)
            points = _nrcs(raw, number, name)
            conditions[name] = {"points": points, "receipt": receipt, "station_id": f"{number}:UT:SNTL"}
        except (OSError, ValueError) as error:
            missing[name] = str(error)
    with venue_archive.connect(data) as db:
        markets = [venue_archive._market(row) for row in db.execute(
            f"SELECT {venue_archive.PUBLIC_SELECT} FROM markets WHERE venue=? AND family='ski-opening' ORDER BY series_id,day",
            (venue_archive.KALSHI,))]
    for i, market in enumerate(markets):
        if market["series_id"] in SOURCE_AUDITS or market["series_id"] == "KXALTAOPEN":
            markets[i] = _with_receipt(data, market)
    return {"version": VERSION, "markets": markets, "source_receipts": sources, "source_gaps": missing, "nearby_snotel": conditions}


def _snotel(corpus, station, day):
    return ((corpus["nearby_snotel"].get(station) or {}).get("points") or {}).get(day) or {}


def _thanksgiving(y):
    return date(y, 11, 1) + timedelta(days=(3 - date(y, 11, 1).weekday()) % 7 + 21)


def _history(corpus):
    history = []
    for alias, text in OPENED_ALTA.items():
        source = corpus["source_receipts"].get(alias)
        if not source:
            continue
        opening = date.fromisoformat(text)
        early = _snotel(corpus, "Snowbird", f"{opening.year}-11-14")
        prior = _snotel(corpus, "Atwater", f"{opening.year}-11-14")
        history.append({"calendar_year": opening.year, "actual_day": text, "operator_receipt": source,
                        "thanksgiving_offset": (opening - _thanksgiving(opening.year)).days,
                        "nov14_snowbird": early, "nov14_atwater": prior})
    return history


def _predict(history, values, threshold):
    sense = lambda row: row["thanksgiving_offset"] < threshold
    base = (sum(sense(row) for row in history) + 1) / (len(history) + 2)
    depth = values.get("depth_in")
    available = [row for row in history if row["nov14_snowbird"].get("depth_in") is not None]
    if depth is None or not available:
        return {"earlier_dates": base}
    nearest = sorted(available, key=lambda row: abs(row["nov14_snowbird"]["depth_in"] - depth))[:2]
    chance = (sum(sense(row) for row in nearest) + 2 * base) / (len(nearest) + 2)
    return {"earlier_dates": base, "nov14_snow_depth_neighbors": chance}


def _native_cases(corpus):
    rows = []
    for market in corpus["markets"]:
        if not market["day"] or market["series_id"] not in SOURCE_AUDITS or market["yes_payout"] not in ("0", "1"):
            continue
        opening, needed, grade, initial, original = SOURCE_AUDITS[market["series_id"]]
        sources = [corpus["source_receipts"].get(key) for key in needed]
        if any(source is None for source in sources):
            continue
        initial_source = corpus["source_receipts"].get(original) if original else None
        earlier_guess = initial < market["day"] if initial and initial_source else None
        native_truth = market["yes_payout"] == "1"
        rows.append({"native": market, "opening_reference_day": opening, "reported_date_implies_yes": opening < market["day"],
                     "proof_grade": grade, "references": sources, "initial_announcement": initial if initial_source else None,
                     "initial_source": initial_source, "initial_announcement_implies_yes": earlier_guess, "verified_native_result": native_truth})
    return rows


def evaluate(data, corpus):
    frozen, digest = research_model.freeze_inputs(data, corpus)
    history = _history(corpus)
    fit = [row for row in history if row["calendar_year"] <= 2022 and row["nov14_snowbird"].get("depth_in") is not None]
    test = [row for row in history if row["calendar_year"] >= 2023 and row["nov14_snowbird"].get("depth_in") is not None]
    details, cases = [], []
    for row in test:
        y = row["calendar_year"]
        for marker in HISTORIC_NOV:
            day = date.fromisoformat(f"{y}-{marker}")
            predictions = _predict(fit, row["nov14_snowbird"], (day - _thanksgiving(y)).days)
            details.append({"year": y, "threshold": str(day), "physical_opened_before": row["actual_day"] < str(day),
                            "forecast_probabilities": predictions, "nov14_snowbird": row["nov14_snowbird"]})
    metrics = []
    if fit and details:
        for mode in ("earlier_dates", "nov14_snow_depth_neighbors"):
            errors = [((row["forecast_probabilities"][mode] - row["physical_opened_before"]) ** 2)
                      for row in details if mode in row["forecast_probabilities"]]
            if errors:
                metrics.append({"group": "Alta held historical first-opening dates", "model": mode, "brier": mean(errors),
                                "threshold_checks": len(errors), "independent_seasons": len(test)})
        calendar_errors = [abs(row["thanksgiving_offset"] - median([item["thanksgiving_offset"] for item in fit])) for row in test]
        metrics.append({"group": "Alta held historical first-opening dates", "model": "Typical Thanksgiving-relative date", "mae_days": mean(calendar_errors),
                        "independent_seasons": len(test)})
    audited = _native_cases(corpus)
    for row in audited:
        market = row["native"]
        if market["instrument_id"] not in ("KXCOPPEROPEN-27-08NOV26", "KXBRECKOPEN-27-08NOV26", "KXWINTERPARKOPEN-27-15NOV26"):
            continue
        detail = f"The later public opening reference is {row['opening_reference_day']}. Native YES paid ${market['yes_payout']}."
        if row["initial_announcement"]:
            detail = f"The first announced date was {row['initial_announcement']}. " + detail
        cases.append({"venue": market["venue"], "instrument_id": market["instrument_id"], "date": market["day"], "at": market["settlement_at"],
                      "title": market["title"], "detail": detail, "data": row})
    for market in corpus["markets"]:
        if market["instrument_id"] != "KXALTAOPEN-27-22NOV26":
            continue
        today = _snotel(corpus, "Snowbird", "2026-11-14")
        atwater = _snotel(corpus, "Atwater", "2026-11-14")
        p = _predict(fit, today, (date.fromisoformat(market["day"]) - _thanksgiving(2026)).days)
        cases.append({"venue": market["venue"], "instrument_id": market["instrument_id"], "date": market["day"],
                      "at": (market["recent_book"] or {}).get("at"), "title": "Alta's published date is still conditional",
                      "detail": "Alta plans to run its first qualifying lifts on November 20 if conditions permit. The native result is pending.",
                      "data": {"market": market, "operator_schedule": corpus["source_receipts"].get("alta-current-plan"),
                               "operator_lift_status_page": corpus["source_receipts"].get("alta-current-lifts"),
                               "nov14_snotel": {"Snowbird": today, "Atwater": atwater}, "weather_analogs": p}})
    coverage = {"cataloged_native_markets": len(corpus["markets"]),
                "native_resort_series": len({row["series_id"] for row in corpus["markets"]}),
                "native_verified_payouts": sum(row["yes_payout"] in ("0", "1") for row in corpus["markets"]),
                "payouts_with_public_named_date_reference": len(audited),
                "matches_with_reference": sum(row["reported_date_implies_yes"] == row["verified_native_result"] for row in audited),
                "by_evidence_grade": dict(Counter(row["proof_grade"] for row in audited)),
                "initial_announced_date_changes_cash_test": sum(row["initial_announcement_implies_yes"] != row["verified_native_result"]
                     for row in audited if row["initial_announcement_implies_yes"] is not None),
                "verified_historical_alta_opening_days": len(history), "alta_fit_seasons": len(fit), "alta_held_seasons": len(test),
                "nearby_snotel_days": {name: len(item["points"]) for name, item in corpus["nearby_snotel"].items()}}
    record = {"version": VERSION, "supersedes": EARLIER_RUN, "title": "Ski openings depend on actual operations and snow",
              "hypothesis": "An announcement can misclassify settlement. Nearby early snow depth may improve the opening-date forecast.",
              "target": "First eligible public ski lift operations and the actual native before-date outcome",
              "venues": ["Kalshi"], "date_window": {"historic Alta": ["2018-11-01", "2025-12-07"], "native opening season": ["2026-10-01", "2027-02-01"]},
              "coverage": coverage, "metrics": metrics, "cases": cases, "missing": corpus["source_gaps"],
              "limits": ["The announcement, operator's dated first-day indication and observed activity remain distinct evidence grades.",
                         "The model has a very small held Alta sample. Nearby USDA sensors measure snowpack away from the Collins venue snowfall plot, without snowmaking, lift or staffing information.",
                         "Early venue dates require Eastern Time and an exclusive cutoff. The book's origin image timestamp remains unavailable."],
              "provenance": {"source": OPERATOR, "NRCS daily archive": WCC, "frozen_input": str(frozen.relative_to(data)),
                             "frozen_input_sha256": digest, "source_sha256": research_model.run_sources(sys.modules[__name__], venue_storm)}}
    return research_model.write_record(data, record, {"alta_actual_dates": history, "alta_held_forecasts": details, "native_matches": audited})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--input", type=Path)
    parser.add_argument("--collect", action="store_true")
    args = parser.parse_args(argv)
    try:
        corpus = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else gather(args.data, collect=args.collect)
        print(evaluate(args.data, corpus))
    except CollectionPaused:
        print("Collection is stopped. Retained receipts are available.")
        return 2


if __name__ == "__main__":
    sys.exit(main())
