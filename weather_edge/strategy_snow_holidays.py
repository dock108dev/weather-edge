"""Compare native holiday snowfall terms with trace and snow-depth histories."""

import argparse
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
from statistics import mean

from . import research_model, strategy_snow_days, strategy_snow_events, venue_archive
from .market import parse_time


VERSION = "holiday-snowfall-trace-v1"
TERMS = strategy_snow_events.TERMS
WHITE_CHRISTMAS = "https://www.weather.gov/iwx/whitechristmas"
FIT = range(2006, 2018)
TEST = range(2018, 2026)
MODELS = ("Exact holiday snowfall", "Local seven-day snowfall climatology", "At least one inch of snow depth")
STATION = re.compile(r"\((K[A-Z]{3})\)")
NATIVE_OLDER = ("KXNYCSNOWXMAS-25DEC-0", "KXBOSSNOWXMAS-25DEC-0", "KXCHISNOWXMAS-25DEC-0")


def holiday(year, kind):
    if kind == "Christmas":
        return date(year, 12, 25)
    first = date(year, 11, 1)
    return first + timedelta(days=(3 - first.weekday()) % 7 + 21)


def native_contracts(data):
    with venue_archive.connect(data) as db:
        rows = db.execute("""SELECT instrument_id FROM markets WHERE venue=? AND rules_url=? AND (event_id=? OR
                          (series_id LIKE '%XMAS%' AND instrument_id LIKE '%25DEC26%') OR instrument_id IN (?,?,?))
                          ORDER BY instrument_id""",
                          (venue_archive.KALSHI, TERMS, "KXSNOWTHANKSGIVING-26NOV26", *NATIVE_OLDER)).fetchall()
    supported, skipped = [], []
    fields = ("instrument_id", "event_id", "station", "title", "status", "rules_url", "rules_primary", "rules_secondary",
              "yes_bid", "yes_ask", "closed_at", "opened_at", "settlement_at", "yes_payout", "receipt", "settlement_receipt")
    for (ident,) in rows:
        native = venue_archive.detail(data, venue_archive.KALSHI, ident)
        row = {key: native[key] for key in fields}
        primary, secondary = row["rules_primary"], row["rules_secondary"] or ""
        previous = ident in NATIVE_OLDER
        kind = "Thanksgiving" if row["event_id"] == "KXSNOWTHANKSGIVING-26NOV26" else "Christmas"
        station = row["station"] or (matched[1] if (matched := STATION.search(primary)) else None)
        if not (station and "strictly greater than 0 inches" in primary and (previous or (
                "trace" in (primary + secondary).lower() and ("local reporting day" in secondary or "local calendar day" in secondary)))):
            skipped.append(ident)
            continue
        day = holiday(2025 if previous else 2026, kind)
        if not row["closed_at"] or parse_time(row["closed_at"]).date() != day + timedelta(days=1):
            skipped.append(ident)
            continue
        supported.append({**row, "station": station, "kind": kind, "date": str(day),
                          "trace_qualifies": None if previous else True})
    return supported, skipped


def label(raw, day):
    point = raw.get(str(day), {})
    snowfall, kind = strategy_snow_days.reported_amount(point.get("snow"))
    depth, _ = strategy_snow_days.reported_amount(point.get("depth"))
    return {"date": str(day), "reported_snowfall": point.get("snow"), "reported_snow_depth": point.get("depth"),
            "snowfall_kind": kind, "snow_in": str(snowfall) if snowfall is not None else None,
            "yes_including_trace": snowfall is not None and (snowfall > 0 or kind == "trace"),
            "depth_in": str(depth) if depth is not None else None,
            "white_christmas_depth_proxy": depth is not None and depth >= 1}


def priors(raw, kind, years):
    anchors = [label(raw, holiday(year, kind)) for year in years]
    exact = [entry for entry in anchors if entry["snowfall_kind"]]
    depth = [entry for entry in anchors if entry["depth_in"] is not None]
    seasonal = []
    for year in years:
        neighboring = [label(raw, holiday(year, kind) + timedelta(days=day)) for day in range(-3, 4)]
        eligible = [row["yes_including_trace"] for row in neighboring if row["snowfall_kind"]]
        if len(eligible) >= 4:
            seasonal.append(mean(eligible))
    counts = (len(exact), len(seasonal), len(depth))
    wins = (sum(verify["yes_including_trace"] for verify in exact), sum(seasonal),
            sum(verify["white_christmas_depth_proxy"] for verify in depth))
    estimates = {name: (total + 0.5) / (n + 1) if n else None for name, total, n in zip(MODELS, wins, counts, strict=True)}
    return estimates, dict(zip(MODELS, counts, strict=True)), anchors


def score(samples):
    result = []
    for kind in ("Thanksgiving", "Christmas"):
        for window, methods in (("later holiday test", MODELS), ("later snowfall-only coverage", MODELS[:2])):
            common = [row for row in samples if row["kind"] == kind and row["snowfall_kind"]
                      and all(row["earlier_estimates"][model] is not None for model in methods)]
            for model in methods:
                days = defaultdict(list)
                for row in common:
                    days[row["date"]].append((row["earlier_estimates"][model] - row["yes_including_trace"]) ** 2)
                result.append({"window": window, "holiday": kind, "method": model,
                               "calendar_dates": len(days), "station_days": len(common),
                               "brier": mean(mean(values) for values in days.values()) if days else None,
                               "trace_yes_days": sum(row["snowfall_kind"] == "trace" for row in common),
                               "depth_proxy_disagrees": sum(row["depth_in"] is not None and row["white_christmas_depth_proxy"]
                                                            != row["yes_including_trace"] for row in common)})
    return result


def report(data):
    listings, gaps = native_contracts(data)
    weather, weather_gaps = strategy_snow_days.parallel_weather(data, {row["station"] for row in listings})
    historical, current, payouts, cases = [], [], [], []
    station_sets = {(row["station"], row["kind"]) for row in listings}
    for station, kind in sorted(station_sets):
        if station not in weather:
            continue
        raw = weather[station]["days"]
        earlier, _, _ = priors(raw, kind, FIT)
        deploy, counts, anchors = priors(raw, kind, (*FIT, *TEST))
        historical.extend({**label(raw, holiday(year, kind)), "station": station, "kind": kind,
                           "earlier_estimates": earlier} for year in TEST)
        for market in listings:
            if market["station"] != station or market["kind"] != kind:
                continue
            if market["status"] == "open":
                current.append({**market, "history": anchors, "marginal_probabilities": deploy, "sample_sizes": counts})
            if market["settlement_receipt"] and market["yes_payout"] in ("0", "1"):
                day = date.fromisoformat(market["date"])
                actual, _, report_gaps = strategy_snow_events.daily_reports(data, station, [day], datetime.now(timezone.utc))
                total = actual.get(str(day))
                payouts.append({**market, "official": total, "report_gaps": report_gaps})
    for entry in current:
        station = entry["station"]
        estimates = entry["marginal_probabilities"]
        if estimates[MODELS[0]] is None:
            continue
        if station in {"KNYC", "KDEN", "KORD", "KMSP", "KSEA"}:
            snow, depth = estimates[MODELS[0]], estimates[MODELS[2]]
            detail = f"Exact-day historical snowfall with trace {snow:.0%}."
            if depth is not None:
                detail += f" Historical 1-inch snow-depth proxy {depth:.0%}."
            cases.append({"venue": venue_archive.KALSHI, "station": station, "date": entry["date"],
                          "instrument_id": entry["instrument_id"],
                          "at": entry["receipt"]["received_at"] if entry["receipt"] else entry["opened_at"],
                          "title": entry["title"], "detail": detail})
    for entry in payouts:
        if not entry["official"]:
            continue
        cases.append({"venue": venue_archive.KALSHI, "station": entry["station"], "date": entry["date"],
                      "instrument_id": entry["instrument_id"], "at": entry["settlement_at"],
                      "title": f"{entry['station']} official Christmas payout",
                      "detail": f"Raw daily report snow {entry['official']['snow_in']}. Native YES paid ${entry['yes_payout']}."})
    trace = next((row for row in reversed(historical) if row["snowfall_kind"] == "trace" and row["depth_in"] == "0"), None)
    depth = next((row for row in reversed(historical) if row["snowfall_kind"] == "measured"
                  and not row["yes_including_trace"] and row["white_christmas_depth_proxy"]), None)
    if trace:
        cases.append({"station": trace["station"], "date": trace["date"], "title": "Trace snowfall with zero snow depth",
                      "detail": "The station reported new trace snowfall and zero inches on the ground."})
    if depth:
        cases.append({"station": depth["station"], "date": depth["date"], "title": "Snow on the ground with zero new snowfall",
                      "detail": f"The station reported {depth['depth_in']} inches of depth and zero new snowfall."})
    metrics = score(historical)
    metrics.append({"window": "Native holiday contracts", "open_markets": sum(row["status"] == "open" for row in listings),
                    "current_with_climatology": sum(row["marginal_probabilities"][MODELS[0]] is not None for row in current),
                    "distinct_stations_with_history": len(weather), "past_public_payouts": len(payouts),
                    "past_payouts_with_individual_cli": sum(row["official"] is not None for row in payouts),
                    "paper_fills": 0})
    settings = {"engine": VERSION, "fit": list(FIT), "test": list(TEST), "native_terms": TERMS,
                "daily_source": strategy_snow_days.DOCS, "white_christmas_definition": WHITE_CHRISTMAS,
                "weather_gaps": weather_gaps, "unsupported_exact_rules": gaps,
                "implementation_sha256": sha256(Path(__file__).read_bytes()).hexdigest()}
    result = {"version": VERSION, "title": "Holiday snowfall, trace reports and snow depth",
              "hypothesis": "A station's trace-inclusive holiday snowfall history better matches exact venue terms than a snow-depth climatology.",
              "target": "Kalshi Thanksgiving and Christmas new snowfall above zero, including explicitly listed trace.",
              "venues": [venue_archive.KALSHI], "date_window": {"from": "2006-11-01", "through": "2026-12-25"},
              "metrics": metrics, "cases": cases, "current": current,
              "limits": ["ACIS retrospectively reports historical snowfall and daily snow depth. An exact clock for individual snow-depth measurements is absent.",
                         "The modern listings count trace as YES and leave absent reports unknown. Historical venue payouts retain their original rules.",
                         "The first current holiday has no completed observation yet. Seasonal climate proportions and display prices establish zero fills."],
              "provenance": settings}
    return research_model.write_record(data, result, {"native_catalog": listings, "verified_native_payouts": payouts,
                                                        "historical": historical, "weather_receipts": {s: v["receipt"] for s, v in weather.items()}})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=venue_archive.DEFAULT_DATA)
    args = parser.parse_args()
    path = report(args.data)
    result = json.loads(path.read_bytes())
    print(json.dumps({"path": str(path), "metrics": result["metrics"], "cases": result["cases"],
                      "provenance": result["provenance"]}, indent=2))


if __name__ == "__main__":
    main()
