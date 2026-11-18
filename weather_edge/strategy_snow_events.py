"""Replay snowfall event thresholds with raw daily reports and timestamped NOAA guidance."""

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from . import collector, expansion_weather, research_model, strategy_heat_forecast, strategy_twc, venue_archive, venue_snow_data
from .market import decimal, iso, parse_time
from .nbm import _columns, _station_fields, climate_window


VERSION = "native-snowstorm-periods-v1"
TERMS = "https://assets.kalshi.com/contract_terms/SNOWOVERTIME.pdf"
NBS_DOC = "https://vlab.noaa.gov/web/mdl/nbm-documentation"
SERIES = ("KXSNOWSTORM", "KXSNOWCITIES")
DATE_RANGE = re.compile(
    r"\b(?P<first_m>Jan|Feb|Nov) (?P<first_d>\d{1,2})(?:st|nd|rd|th)?(?:, (?P<first_y>20\d{2}))?\s*"
    r"(?:through|to|[-–])\s*(?:(?P<last_m>Jan|Feb|Nov) )?(?P<last_d>\d{1,2})(?:st|nd|rd|th)?, (?P<last_y>20\d{2})\b",
    re.I,
)
FORECAST_PRODUCTS = ("nbs", "nbe")


def window(row):
    rules = row["rules_primary"]
    if (row["rules_url"] != TERMS or row["operator"] != "above" or "strictly greater than" not in rules
            or row["station"] not in expansion_weather.STATIONS or not (match := DATE_RANGE.search(rules))):
        return None
    fields = match.groupdict()
    first = datetime.strptime(f"{fields['first_y'] or fields['last_y']} {fields['first_m']} {fields['first_d']}", "%Y %b %d").date()
    final = datetime.strptime(f"{fields['last_y']} {fields['last_m'] or fields['first_m']} {fields['last_d']}", "%Y %b %d").date()
    if not 1 <= (final - first).days <= 6:
        return None
    cli = expansion_weather.CLI_OFFICE[row["station"]][1]
    if row["series_id"] == "KXSNOWSTORM" and cli not in (row["rules_secondary"] or ""):
        return None
    if row["series_id"] == "KXSNOWCITIES" and not (f"({row['station']})" in rules and "Eastern Time" in row["rules_secondary"]):
        return None
    return first, final


def market_groups(data):
    result = defaultdict(list)
    fields = ("instrument_id", "series_id", "event_id", "status", "rules_primary", "rules_secondary", "rules_url",
              "station", "operator", "lower", "opened_at", "closed_at", "settlement_at", "settlement_received_at",
              "yes_payout", "settlement_receipt", "receipt", "yes_bid", "yes_ask", "last_price")
    with venue_archive.connect(data) as db:
        keys = db.execute("SELECT instrument_id FROM markets WHERE venue=? AND series_id IN (?,?) ORDER BY instrument_id",
                          (venue_archive.KALSHI, *SERIES)).fetchall()
    unsupported = []
    for (instrument,) in keys:
        full = venue_archive.detail(data, venue_archive.KALSHI, instrument)
        row = {key: full.get(key) for key in fields}
        if not (period := window(row)):
            unsupported.append(instrument)
            continue
        result[(row["station"], *period)].append(row)
    return result, unsupported


def report_days(start, end):
    return [start + timedelta(days=n) for n in range((end - start).days + 1)]


def daily_reports(data, station, days, through):
    earlier = [day for day in days if climate_window(day, expansion_weather.STATIONS[station][3])[1] <= through]
    earlier_keys = {str(day) for day in earlier}
    sources, days_by_date, gaps = [], {}, {}
    for year in sorted({day.year for day in earlier}):
        try:
            body, receipt = venue_snow_data.obtain_year(data, station, year)
        except (OSError, ValueError) as error:
            gaps[str(year)] = str(error)
            continue
        sources.append(receipt)
        for row in json.loads(body)["results"]:
            if row["valid"] not in earlier_keys:
                continue
            normalized = venue_snow_data._day_row(station, row, offset=expansion_weather.STATIONS[station][3])
            if normalized and parse_time(normalized["published_at"]) <= through:
                days_by_date[normalized["date"]] = normalized
    result = {}
    for day in earlier:
        annual = days_by_date.get(str(day))
        exact = None
        if annual:
            try:
                exact = expansion_weather.raw_report(data, station, day, annual["product"])
            except (OSError, ValueError) as error:
                gaps[str(day)] = str(error)
        if not exact or exact["snow_in"] is None:
            try:
                reports = expansion_weather.reports_for_day(data, station, day)
            except (OSError, ValueError) as error:
                gaps[str(day)] = str(error)
            else:
                exact = max((row for row in reports if row["snow_in"] is not None and parse_time(row["published_at"]) <= through),
                            key=lambda row: parse_time(row["published_at"]), default=None)
        if exact and exact["snow_in"] is not None:
            gaps.pop(str(day), None)
            reported = exact["snow_in"]
            result[str(day)] = {**exact, "amount_in": "0" if reported == "T" else str(decimal(reported)),
                                "kind": "trace" if reported == "T" else "measured", "mode": "individual NWS daily climate product",
                                "annual_summary": annual}
            continue
        provider = next((row for row in strategy_twc.reports(data, station=station, day=str(day), as_of=through)
                         if row["provider_status"] in {"official", "revised"} and row["snowfall_in"] is not None), None)
        if provider:
            kind, amount = venue_snow_data._snow_value(provider["snowfall_in"])
            if amount is not None:
                result[str(day)] = {**provider, "amount_in": amount, "kind": kind, "mode": "Weather Company climate dashboard",
                                    "annual_summary": annual}
        if str(day) not in result and str(day) not in gaps:
            gaps[str(day)] = "Individual daily report snowfall was unavailable"
    return result, sources, gaps


def snow_hours(card, station):
    product = card["run"]
    issue, fields = _station_fields(card["raw_card"].encode("ascii"), station, product)
    width, field = (6, "S06") if product == "nbs" else (12, "S12")
    leads = [int(value) for value in fields["FHR"].replace("|", " ").split()]
    if field not in fields:
        return []
    values = (_columns(fields, field, len(leads)) if product == "nbs" else
              [int(value) for value in fields[field].replace("|", " ").split()])
    if len(values) != len(leads):
        raise ValueError(f"Snowfall field does not match lead columns in {product}")
    windows = []
    for lead, amount in zip(leads, values, strict=True):
        if amount is None or amount < 0:
            continue
        end = issue + timedelta(hours=lead)
        start = end - timedelta(hours=width)
        if start >= issue:
            windows.append({"starts_at": iso(start), "ends_at": iso(end), "amount_in": str(Decimal(str(amount)) / 10),
                            "product": product, "native_field": field})
    return windows


def coverage(windows, start, end):
    """Choose disjoint native accumulations then bound snowfall at the two date edges."""
    candidates = defaultdict(list)
    for row in windows:
        left, right = parse_time(row["starts_at"]), parse_time(row["ends_at"])
        if left < end and right > start:
            candidates[left].append((right, row))
    paths = {edge: [] for edge in candidates if edge <= start}
    ending = None
    for left in sorted(set(candidates) | {right for alternatives in candidates.values() for right, _ in alternatives}):
        if left not in paths:
            continue
        if left >= end:
            ending = paths[left]
            break
        for right, row in candidates[left]:
            path = [*paths[left], row]
            if right not in paths or sum(v["product"] == "nbs" for v in path) > sum(v["product"] == "nbs" for v in paths[right]):
                paths[right] = path
    if ending is None:
        return None
    low = sum((decimal(row["amount_in"]) for row in ending if
               start <= parse_time(row["starts_at"]) and parse_time(row["ends_at"]) <= end), Decimal(0))
    high = sum((decimal(row["amount_in"]) for row in ending), Decimal(0))
    return {"lower_in": str(low), "upper_in": str(high), "native_windows": ending}


def _window_starts(station, day, modern):
    if modern:
        zone = ZoneInfo(expansion_weather.STATIONS[station][2])
        return datetime.combine(day, time.min, zone).astimezone(timezone.utc)
    return climate_window(day, expansion_weather.STATIONS[station][3])[0]


def _load_guidance(data, station, start_day, weather_start, end, as_of):
    gaps = []
    day = datetime.combine(start_day, time.min, timezone.utc)
    for issue in (day, day - timedelta(hours=6), day - timedelta(hours=12)):
        if issue > as_of:
            continue
        windows, cards = [], []
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = pool.map(lambda kind: strategy_heat_forecast.read_cards(data, issue, {station}, kind), FORECAST_PRODUCTS)
            for found, unavailable in results:
                card = found.get(station)
                if card is None:
                    gaps.append(unavailable)
                    continue
                if parse_time(card["available_at"]) > as_of:
                    continue
                try:
                    windows.extend(snow_hours(card, station))
                except (KeyError, ValueError) as error:
                    gaps.append({station: str(error)})
                    continue
                cards.append(card)
        if totals := coverage(windows, weather_start, end):
            return {"issue": iso(issue), "knowledge_at": iso(as_of), "archive_cards": cards, **totals}, gaps
    return None, gaps


def assemble(data, through):
    markets, unsupported = market_groups(data)
    rows = []
    for (station, first, finish), group in sorted(markets.items()):
        current = any(row["series_id"] == "KXSNOWCITIES" for row in group)
        days = report_days(first, finish)
        labels, sources, gaps = daily_reports(data, station, days, through)
        end = _window_starts(station, finish + timedelta(days=1), current)
        consecutive = []
        for day in days:
            if str(day) not in labels:
                break
            consecutive.append(labels[str(day)])
        realized = sum((decimal(row["amount_in"]) for row in consecutive), Decimal(0))
        final = len(consecutive) == len(days) and through >= end
        scenario_day = first + timedelta(days=len(consecutive)) if current else first
        start = _window_starts(station, scenario_day, current)
        as_of = through if current else start
        forecast, forecast_gaps = _load_guidance(data, station, scenario_day, start, end, as_of) if start < end else (None, [])
        prefix = realized if current else Decimal(0)
        if forecast:
            forecast["total_lower_in"] = str(prefix + decimal(forecast["lower_in"]))
            forecast["total_upper_in"] = str(prefix + decimal(forecast["upper_in"]))
        scenarios = []
        for row in sorted(group, key=lambda row: decimal(row["lower"])):
            strike = decimal(row["lower"])
            payout = row["yes_payout"] if row["settlement_receipt"] else None
            official = int(realized > strike) if final else None
            scenarios.append({**row, "weather_yes": official, "verified_yes_payout": payout,
                              "conditioned_lower_in": forecast["total_lower_in"] if forecast else None,
                              "conditioned_upper_in": forecast["total_upper_in"] if forecast else None,
                              "earlier_days_exceed_threshold": realized > strike})
        rows.append({"station": station, "first": str(first), "last": str(finish), "inclusive_days": len(days),
                     "complete_days": len(consecutive), "observed_total_in": str(realized), "final": final,
                     "report_days": consecutive, "report_sources": sources, "report_gaps": gaps,
                     "forecast": forecast, "forecast_gaps": forecast_gaps, "scenarios": scenarios, "live": current})
    return rows, unsupported


def report(data, through):
    rows, unsupported = assemble(data, through)
    historical = [row for row in rows if not row["live"]]
    judged = [(group, row) for group in historical for row in group["scenarios"]
              if row["weather_yes"] is not None and row["verified_yes_payout"] in ("0", "1")]
    live = [row for row in rows if row["live"]]
    metrics = [{"window": "Settled native snowstorms", "station_windows": len(historical),
                "independent_weather_start_dates": len({row["first"] for row in historical}),
                "verified_contract_payouts_with_complete_weather": len(judged),
                "weather_matches_payout": sum(row["weather_yes"] == int(row["verified_yes_payout"]) for _, row in judged),
                "thresholds_paid_yes": sum(row["verified_yes_payout"] == "1" for _, row in judged),
                "complete_forecast_windows": sum(bool(row["forecast"]) for row in historical)},
               {"window": "Active northeast snowstorm", "station_windows": len(live),
                "classified_thresholds": sum(len(group["scenarios"]) for group in live),
                "complete_forecast_windows": sum(bool(row["forecast"]) for row in live),
                "paper_fills": 0,
                "traces_in_complete_daily_reports": sum(r["kind"] == "trace" for row in live for r in row["report_days"])}]
    cases = []
    for row in rows:
        nearest = min(row["scenarios"], key=lambda item: abs(decimal(item["lower"]) - decimal(row["observed_total_in"])))
        forecast = row["forecast"]
        detail = f"{row['complete_days']} of {row['inclusive_days']} days reported {row['observed_total_in']} inches."
        if forecast:
            detail += f" NOAA scenario {forecast['total_lower_in']}–{forecast['total_upper_in']} inches."
        if nearest["verified_yes_payout"] is not None:
            detail += f" Venue YES paid ${nearest['verified_yes_payout']} at the {nearest['lower']}-inch strike."
        cases.append({"venue": "kalshi", "station": row["station"], "date": row["last"],
                      "instrument_id": nearest["instrument_id"], "at": nearest["settlement_at"] or iso(through),
                      "title": f"{row['station']} snowfall from {row['first']} through {row['last']}", "detail": detail})
    evidence = {"station_windows": rows, "unmapped_native_contracts": unsupported}
    result = {"version": VERSION, "title": "Native snowfall storm windows and verified thresholds",
              "hypothesis": "Daily snowfall and source-timed NOAA guidance account for the whole native date window, including local midnight edges.",
              "target": "Observed new snowfall on exact calendar dates and strict-above Kalshi thresholds.",
              "venues": ["kalshi"], "date_window": {"from": min((row["first"] for row in rows), default=str(through.date())),
                                                     "through": iso(through)},
              "metrics": metrics, "cases": cases,
              "limits": ["Sub-measurable trace remains visible and contributes zero tenths to quantified storm accumulation.",
                         "Forecast accumulation uses disjoint native windows. The lower and upper amounts bound modeled snow at overlapping local-date edges. Forecast error can exceed those bounds.",
                         "Historical NOAA availability uses published archive clocks. The final current day needs its own full official report and native payout.",
                         "Several native thresholds share the same storm. The short outcome sample does not establish forecast calibration or trading performance."],
              "provenance": {"engine": VERSION, "native_terms": TERMS, "model_documentation": NBS_DOC,
                             "unmapped_native_contracts": unsupported,
                             "implementation_sha256": sha256(Path(__file__).read_bytes()).hexdigest()}}
    return research_model.write_record(data, result, evidence)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=venue_archive.DEFAULT_DATA)
    parser.add_argument("--through", type=parse_time, default=datetime.now(timezone.utc))
    args = parser.parse_args()
    if collector.paused(args.data):
        parser.exit(0, "Live collection is stopped.\n")
    path = report(args.data, args.through)
    result = json.loads(path.read_bytes())
    print(json.dumps({"path": str(path), "metrics": result["metrics"], "cases": result["cases"]}, indent=2))


if __name__ == "__main__":
    main()
