"""Check how retrospective one-minute station archives represent daily extremes."""

from __future__ import annotations

import argparse
import math
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

from . import archive, expansion_contract, expansion_weather, history, research_model
from .nbm import climate_window, iso, utc

VERSION = "minute-extreme-check-v1"
DEFAULT = (
    ("KNYC", date(2025, 12, 14)), ("KNYC", date(2026, 1, 2)), ("KNYC", date(2026, 4, 15)),
    ("KNYC", date(2026, 11, 12)), ("KMDW", date(2026, 11, 13)), ("KMDW", date(2026, 11, 14)),
)


def rolling_five(points, start, end):
    by_time = {utc(row["at"]): row["temp_f"] for row in points}
    output = []
    for stamp in sorted(at for at in by_time if start <= at < end):
        clocks = [stamp - timedelta(minutes=lag) for lag in range(5)]
        if all(clock in by_time for clock in clocks):
            avg = statistics.mean(by_time[clock] for clock in clocks)
            rounded = math.copysign(math.floor(abs(avg) + 0.5), avg)
            output.append({"at": iso(stamp), "temp_f": int(rounded)})
    return output


def _case(data, station, day):
    record = expansion_weather.daily_labels(data, station, day, day).get(str(day))
    if not record:
        raise ValueError("No complete climate-summary identity for this day")
    official = expansion_weather.raw_report(data, station, day, record["product"])
    if not official:
        raise ValueError("Complete original climate product unavailable")
    offset = expansion_weather.STATIONS[station][3]
    start, finish = climate_window(day, offset)
    spec = replace(history.EVENTS["nyc"], station=station, day=day)
    trace = archive._archive_minute_trace(spec, start - timedelta(minutes=4), finish,
                                          data / "expansion" / "minute-audit", False)
    if not trace:
        raise ValueError("One-minute archive contains no samples for this climate day")
    raw = [row for row in trace["points"] if start <= utc(row["at"]) < finish]
    rolled = rolling_five(trace["points"], start, finish)
    amounts = {"method": "one_minute", "station": station, "group": f"{station} · {day}",
               "raw_minutes": len(raw), "complete_windows": len(rolled), "possible_minutes": 1440,
               "cli_low_f": official["low_f"], "cli_high_f": official["high_f"]}
    for name, values in (("raw", raw), ("rolling", rolled)):
        if values:
            amounts[f"{name}_low_f"] = min(row["temp_f"] for row in values)
            amounts[f"{name}_high_f"] = max(row["temp_f"] for row in values)
    minimum = min(rolled or raw, key=lambda row: row["temp_f"])
    text = (f"Independent minute archive gave {len(raw)}/1,440 readings and {len(rolled)} complete five-minute windows. "
            f"Raw min {amounts.get('raw_low_f')}°F, rolling min {amounts.get('rolling_low_f')}°F, "
            f"complete CLI min {official['low_f']}°F.")
    return amounts, {"station": station, "date": str(day), "at": minimum["at"],
                     "title": f"{station} one-minute station coverage", "detail": text,
                     "official_report": official, "minute_source": {key: value for key, value in trace.items() if key != "points"}}, {
                         "station": station, "date": str(day), "raw_trace": trace, "rolling_trace": rolled,
                         "official_report": official}


def study(data, requests):
    data = Path(data)
    def retrieve(pair):
        station, day = pair
        try:
            return _case(data, station, day), None
        except (OSError, ValueError, KeyError, TypeError) as error:
            return None, {"station": station, "date": str(day), "reason": expansion_contract.availability_error(error)}

    artifacts, gaps = [], []
    with ThreadPoolExecutor(max_workers=2) as pool:
        for case, gap in pool.map(retrieve, requests):
            if case:
                artifacts.append(case)
            else:
                gaps.append(gap)
    metric_rows, cases, details = map(list, zip(*artifacts, strict=True)) if artifacts else ([], [], [])
    result = {
        "version": VERSION, "title": "One-minute readings and the official daily temperature",
        "hypothesis": "A dense retrospective station trace can explain where the daily extreme lies between conventional reports.",
        "target": "Exact-station delayed one-minute ASOS and the governing 5-minute rolling climate statistic",
        "venues": [], "date_window": {"cases": sorted({str(day) for _, day in requests})},
        "metrics": metric_rows, "cases": cases,
        "limits": [
            "The one-minute archive is a delayed bulk source. Its original public availability is unavailable, so it serves retrospective weather exploration.",
            "Rolling values use only five contiguous minute records and reconstruct a comparable statistic from the archive. The official NWS climate report governs the label.",
            "Daily low and high on days with missing observations can lie outside the accessible trace. The cases preserve actual coverage instead of interpolating holes.",
        ],
        "provenance": {"source_sha256": research_model.run_sources(sys.modules[__name__], archive, expansion_weather),
                       "archive_docs": "https://mesonet.agron.iastate.edu/request/asos/1min.phtml",
                       "cli_method": "https://www.weather.gov/lot/weather_observations_faq"},
        "missing": gaps,
    }
    return research_model.write_record(data, result, details)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path(".data"))
    parser.add_argument("--case", nargs=2, action="append", metavar=("STATION", "DATE"))
    args = parser.parse_args()
    requests = [(station.upper(), date.fromisoformat(day)) for station, day in args.case] if args.case else DEFAULT
    if any(station not in expansion_weather.STATIONS for station, _ in requests):
        parser.error("Unknown station")
    print(study(args.data_dir, requests))


if __name__ == "__main__":
    main()
