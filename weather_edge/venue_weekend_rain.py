"""Replay measurable precipitation across the two native weekend reporting days."""

import argparse
from collections import Counter, defaultdict
from datetime import datetime, time, timedelta, timezone
from email.utils import parsedate_to_datetime
import gzip
from hashlib import sha256
import json
from pathlib import Path
import re
from statistics import mean
import sys
import time as wall
from zoneinfo import ZoneInfo

from sklearn import __version__ as sklearn_version
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from . import expansion_nbm, research_model, research_model_rain_windows, strategy_twc, venue_archive, venue_benchmark
from .capture import ReadOnlyHTTP
from .collector import paused
from .history import _write
from .market import decimal, iso, parse_time
from .nbm import climate_window, nbm_url
from .venue_contracts import STATION_OFFSETS, _precipitation_dates
from .venue_sources import CollectionPaused

VERSION = "kalshi-weekend-rain-v1"
FIT_END = "2026-10-03"
TEST_START = "2026-10-10"
FRIDAY_ISSUE = time(12, tzinfo=timezone.utc)
QUOTE_CLOCK = time(19, tzinfo=timezone.utc)
DECISION_CLOCK = time(19, 5, tzinfo=timezone.utc)
SOURCE_DOC = "https://vlab.noaa.gov/web/mdl/nbm-text-products"
NATIVE_TERMS = "https://assets.kalshi.com/contract_terms/RAINGLOBALPERIOD.pdf"
TWC = "https://weather.com/kalshi"
NBS_HEADER = re.compile(rb"^ *([A-Z0-9]{4}) +NBM[^\r\n]+\bNBS\s+GUIDANCE[^\r\n]*", re.M)
METHODS = {"station_climate": "Earlier wet weekends by station", "peak_6h": "Strongest six-hour NOAA probability",
           "union_6h": "Independent NOAA six-hour probabilities", "union_12h": "Independent NOAA twelve-hour probabilities",
           "union_6h_civil": "Six-hour probabilities across civil-day sensitivity",
           "earlier_calibration": "Earlier same-market NOAA calibration", "kalshi": "Kalshi coherent candle midpoint"}


def _forecast_receipt(data, issue, collect):
    path = data / "venues" / "weekend-rain" / "nbm" / f"{issue:%Y%m%d-%H}.txt.gz"
    manifest = path.with_suffix(".json")
    if path.exists() and manifest.exists():
        raw = gzip.decompress(path.read_bytes())
        metadata = json.loads(manifest.read_bytes())
        if sha256(raw).hexdigest() != metadata["sha256"]:
            raise ValueError(f"Original NOAA data changed at {path}")
        return raw, metadata
    if not collect:
        return None
    if paused(data):
        raise CollectionPaused
    source = nbm_url(issue.date(), issue.hour, "nbs")
    reply = ReadOnlyHTTP(wall.monotonic() + 28)(source, limit=36_000_000)
    modified = reply.headers.get("last-modified")
    if not modified:
        raise ValueError("NOAA bulletin HTTP Last-Modified unavailable")
    available = max(issue, parsedate_to_datetime(modified))
    metadata = {"source": source, "requested_at": iso(reply.requested_at), "received_at": iso(reply.at),
                "source_last_modified": modified, "available_at": iso(available), "sha256": sha256(reply.body).hexdigest(),
                "path": str(path.relative_to(data)), "headers": reply.headers}
    _write(path, gzip.compress(reply.body, compresslevel=4, mtime=0))
    _write(manifest, (json.dumps(metadata, separators=(",", ":")) + "\n").encode())
    return reply.body, metadata


def _cards(data, issue, stations, collect):
    local = {station: expansion_nbm.load_card(data, station, issue) for station in stations}
    absent = {station for station in local if local[station] is None}
    if not absent:
        return local, None
    receipt = _forecast_receipt(data, issue, collect)
    if receipt is None:
        return local, None
    raw, metadata = receipt
    headers = list(NBS_HEADER.finditer(raw))
    for header, following in zip(headers, headers[1:]):
        station = header[1].decode("ascii")
        if station in absent:
            if local[station] is not None:
                raise ValueError(f"Multiple forecast station cards for {station}")
            try:
                card = expansion_nbm.parse_nbs_evidence(raw[header.start():following.end()], station)
            except (KeyError, ValueError):
                continue
            if parse_time(card["issued_at"]) != issue:
                raise ValueError(f"Unexpected issue time for {station}")
            local[station] = {**card, **metadata, "offset_in_response": header.start(), "source_product": "NOAA NBM NBS station bulletin"}
    return local, metadata


def _marginals(card, station, first, last, area, at):
    if not card or not parse_time(card["issued_at"]) <= parse_time(card["available_at"]) <= at:
        return None
    start, _ = climate_window(first, STATION_OFFSETS[station])
    _, end = climate_window(last, STATION_OFFSETS[station])
    six = research_model_rain_windows._window(card, start, end, 6)
    twelve = research_model_rain_windows._window(card, start, end, 12)
    if six is None or twelve is None:
        return None
    civil_start = datetime.combine(first, time.min, ZoneInfo(area))
    civil_end = datetime.combine(last + timedelta(days=1), time.min, ZoneInfo(area))
    civil = research_model_rain_windows._window(card, civil_start, civil_end, 6)
    return {"peak_6h": six["peak_probability"], "union_6h": six["probability"], "union_12h": twelve["probability"],
            "union_6h_civil": civil["probability"] if civil else None, "analysis_start": iso(start), "analysis_end": iso(end),
            "forecast": {key: card[key] for key in ("issued_at", "available_at", "received_at", "source", "card_sha256")},
            "intervals_6h": six["intervals"], "intervals_12h": twelve["intervals"]}


def _rain(report):
    if report is None or report["provider_status"] not in ("official", "revised"):
        return None
    amount = report["precipitation_in"]
    if amount == "T":
        return False
    try:
        value = decimal(amount)
    except ValueError:
        return None
    return value > 0 if value >= 0 else None


def _market_rows(data):
    with venue_archive.connect(data) as db:
        return [dict(row) for row in db.execute(
            f"SELECT {venue_archive.PUBLIC_SELECT} FROM markets WHERE venue=? AND series_id='KXRAINWKND' ORDER BY day,instrument_id",
            (venue_archive.KALSHI,))]


def _quote(db, market, clock, decision):
    if not (market["opened_at"] and market["closed_at"] and
            parse_time(market["opened_at"]) <= clock < decision < parse_time(market["closed_at"])):
        return None
    point, _ = venue_benchmark._history_point(db, market, clock)
    return point


def gather(data, *, collect=False):
    native = _market_rows(data)
    by_day, absent = defaultdict(list), Counter()
    for market in native:
        first, last = _precipitation_dates(market["rules_primary"], market["rules_secondary"])
        if not first or not last or (last - first).days != 1 or market["station"] not in STATION_OFFSETS:
            absent["Native exact station and two named reporting days unavailable"] += 1
            continue
        by_day[(first, last)].append(market)
    reports = {(row["station"], row["label_date"]): row for row in strategy_twc.reports(
        data, start=str(min(first for first, _ in by_day)), end=str(max(last for _, last in by_day)))} if by_day else {}
    rows, sources = [], []
    for (first, last), markets in sorted(by_day.items()):
        if collect and paused(data):
            raise CollectionPaused
        friday = first - timedelta(days=1)
        issue = datetime.combine(friday, FRIDAY_ISSUE)
        candle = datetime.combine(friday, QUOTE_CLOCK)
        decision = datetime.combine(friday, DECISION_CLOCK)
        try:
            cards, receipt = _cards(data, issue, {row["station"] for row in markets}, collect)
        except (OSError, ValueError, TimeoutError) as error:
            absent[f"NOAA {issue:%Y-%m-%d %HZ}: {error}"] += len(markets)
            cards, receipt = {}, None
        if receipt:
            sources.append(receipt)
            print(f"{issue:%Y-%m-%d %HZ} {sum(card is not None for card in cards.values())}/{len(cards)} NOAA station cards", flush=True)
        with venue_archive.connect(data) as db:
            for market in markets:
                station = market["station"]
                pair = [reports.get((station, str(day))) for day in (first, last)]
                measure = [_rain(report) for report in pair]
                truth = True if True in measure else False if measure == [False, False] else None
                complete = all(value is not None for value in measure)
                area = next((report["timezone"] for report in pair if report and report["timezone"]), None)
                weather = _marginals(cards.get(station), station, first, last, area, decision) if area else None
                archive_truth = market["yes_payout"]
                quote = _quote(db, market, candle, decision)
                rows.append({"market": market, "first": str(first), "last": str(last), "at": iso(decision), "weather": weather,
                             "source_reports": pair, "weather_truth": truth, "complete_reports": complete, "price": quote,
                             "payout": int(archive_truth) if archive_truth in ("0", "1") else None})
    return {"version": VERSION, "rows": rows, "catalog": len(native), "gaps": dict(absent), "source_receipts": sources}


def _earlier_probabilities(fit):
    events = defaultdict(list)
    base = (sum(row["payout"] for row in fit) + 1) / (len(fit) + 2)
    for row in fit:
        events[row["market"]["station"]].append(row["payout"])
    return {station: (sum(sample) + 6 * base) / (len(sample) + 6) for station, sample in events.items()}, base


def _features(row):
    return [row["weather"][key] for key in ("peak_6h", "union_6h", "union_12h")]


def _score(rows, key, target):
    dates = defaultdict(list)
    for row in rows:
        dates[row["first"]].append((row["probabilities"][key] - int(row[target])) ** 2)
    return mean(mean(errors) for errors in dates.values()) if dates else None


def _metric(rows, models, target, label):
    return [{"group": label, "model": METHODS[key], "brier": _score(rows, key, target), "native_contracts": len(rows),
             "calendar_weekends": len({row["first"] for row in rows}), "stations": len({row["market"]["station"] for row in rows})}
            for key in models] if rows else []


def _case(row, detail, title=None):
    market = row["market"]
    return {"venue": market["venue"], "instrument_id": market["instrument_id"], "station": market["station"],
            "date": row["first"], "at": row["at"], "title": title or market["title"], "detail": detail, "data": row}


def evaluate(data, corpus):
    frozen, digest = research_model.freeze_inputs(data, corpus)
    rows = corpus["rows"]
    historical = [row for row in rows if row["payout"] is not None]
    paired = [row for row in historical if row["weather_truth"] is not None]
    disagreements = [row for row in paired if row["weather_truth"] != row["payout"]]
    test = [row for row in historical if row["first"] >= TEST_START and row["weather"]
            and row["weather"]["union_6h_civil"] is not None]
    first_test = min((row["at"] for row in test), default=None)
    fit = [row for row in historical if row["first"] <= FIT_END and row["weather"] and row["market"]["settlement_at"]
           and first_test and row["market"]["settlement_at"] < first_test]
    climates, base = _earlier_probabilities(fit)
    trained = make_pipeline(StandardScaler(), LogisticRegression(C=0.5)).fit(
        [_features(row) for row in fit], [row["payout"] for row in fit]) if len({row["payout"] for row in fit}) == 2 else None
    predicted = []
    for row in test:
        values = {key: row["weather"][key] for key in ("peak_6h", "union_6h", "union_12h", "union_6h_civil")}
        values["station_climate"] = climates.get(row["market"]["station"], base)
        if trained is not None:
            values["earlier_calibration"] = float(trained.predict_proba([_features(row)])[0, 1])
        if row["price"]:
            values["kalshi"] = row["price"]["probability"]
        predicted.append({**row, "probabilities": values})
    matched = [row for row in predicted if row["price"]]
    physical = [row for row in predicted if row["complete_reports"]]
    models = tuple(key for key in METHODS if key != "kalshi" and (trained is not None or key != "earlier_calibration"))
    metrics = _metric(predicted, models, "payout", "Same held native payouts")
    metrics += _metric(physical, models, "weather_truth", "Same held two complete source reporting days")
    metrics += _metric(matched, (*models, "kalshi"), "payout", "Same held native payouts with coherent pre-weekend quotes")
    cases = [_case(row, "A later official source snapshot and actual native payout differ. The original provider publication clock is unavailable.",
                   "The retrospective weather report and native payout disagree") for row in disagreements[:3]]
    for row in sorted(matched, key=lambda item: abs(item["probabilities"]["union_6h"] - item["price"]["probability"]), reverse=True)[:3]:
        cases.append(_case(row, f"Friday NOAA union {row['weather']['union_6h']:.0%}, Kalshi midpoint {row['price']['probability']:.0%}, "
                           f"actual YES paid ${row['payout']} across the two reporting days."))
    live = [row for row in rows if row["market"]["status"] == "open"]
    crossed = [row for row in live if row["weather_truth"] is True]
    if crossed:
        row = max(crossed, key=lambda item: item["weather"] is not None)
        cases.append(_case(row, "A retained official source day already shows measurable precipitation. Native payout verification is pending.",
                           "Weekend rain crossed on the first official day"))
    missing = dict(corpus["gaps"])
    coverage = {"listed": corpus["catalog"], "indexed": len(rows), "binary_payouts": len(historical),
                "source_supported_payouts": len(paired), "two_complete_source_days": sum(row["complete_reports"] for row in historical),
                "source_payout_disagreements": len(disagreements), "forecast_fit": len(fit), "held_native_forecast": len(predicted),
                "held_clock_matched_price": len(matched), "open": len(live), "open_with_reported_measurable_precipitation": len(crossed)}
    result = {"version": VERSION, "title": "Two-day Kalshi weekend rain",
              "hypothesis": "A single Friday forecast may overstate rain likelihood when consecutive six-hour probabilities are treated as independent.",
              "target": "Measurable precipitation on either named source reporting day, scored against both complete reports and native payouts",
              "venues": ["Kalshi"], "date_window": {"fit": [min((row["first"] for row in fit), default=None), FIT_END],
              "held weekend": [TEST_START, max((row["first"] for row in test), default=None)]},
              "coverage": coverage, "metrics": metrics, "cases": cases, "missing": missing,
              "limits": ["An exact pair of dates and station are in the rules. The NOAA interval analysis tests local standard days and a civil-midnight sensitivity because archived native wording does not specify UTC boundaries.",
                         "The historical TWC report bytes are retained with local receipt time. Original provider issue clocks are often absent and a later report cannot prove which report settled a historical contract.",
                         "Chronology locks a calendar-weekend split with a small number of independent weather events. A station missing earlier uses the earlier pooled climate rate.",
                         "A historical midpoint is a probability proxy. The endpoint gives no executable quantity."],
              "provenance": {"terms": NATIVE_TERMS, "NWS forecast format": SOURCE_DOC, "observation source": TWC,
                             "frozen_input": str(frozen.relative_to(data)), "frozen_input_sha256": digest, "sklearn": sklearn_version,
                             "source_sha256": research_model.run_sources(sys.modules[__name__], research_model_rain_windows)}}
    return research_model.write_record(data, result, {"test": predicted, "open_markets": live, "payout_disagreements": disagreements})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--collect", action="store_true")
    parser.add_argument("--input", type=Path)
    args = parser.parse_args(argv)
    try:
        corpus = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else gather(args.data, collect=args.collect)
        print(evaluate(args.data, corpus))
    except CollectionPaused:
        print("Collection is stopped. Retained receipts are available.")
        return 2


if __name__ == "__main__":
    sys.exit(main())
