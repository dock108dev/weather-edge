"""As-issued KNYC weather records for the daily-high model."""

from __future__ import annotations

import csv
import json
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from time import sleep
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from weather_edge.nbm import climate_window, forecast_for_day, iso, load_nbm_cycle

STATION = "KNYC"
IEM_ASOS = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
IEM_CLI = "https://mesonet.agron.iastate.edu/json/cli.py"
HISTORICAL_OBSERVATION_LAG = timedelta(minutes=10)
ARCHIVE_CYCLES = (0, 6, 12, 18)


def fetch_text(url: str, path: Path) -> str:
    if path.is_file():
        return path.read_text()
    request = Request(url, headers={"User-Agent": "weather-edge local research app"})
    for attempt in range(3):
        try:
            with urlopen(request, timeout=60) as response:
                data = response.read().decode()
            break
        except HTTPError as error:
            if error.code != 429 or attempt == 2:
                raise
            delay = error.headers.get("Retry-After", "")
            sleep(max(1, min(30, int(delay))) if delay.isdecimal() else 3 * (attempt + 1))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data)
    return data


def asos_url(start: date, end: date, station: str = STATION) -> str:
    params = {
        "station": station[1:], "data": "tmpf",
        "year1": start.year, "month1": start.month, "day1": start.day,
        "year2": end.year, "month2": end.month, "day2": end.day,
        "tz": "Etc/UTC", "format": "onlycomma", "latlon": "no", "elev": "no",
        "missing": "empty", "trace": "empty", "direct": "no", "report_type": (3, 4),
    }
    return f"{IEM_ASOS}?{urlencode(params, doseq=True)}"


def load_asos(data_dir: Path, start: date, end: date, station: str = STATION) -> list[dict]:
    content = fetch_text(asos_url(start, end, station), data_dir / f"asos-{station}-{start}-{end}.csv")
    observations = {}
    for row in csv.DictReader(StringIO(content)):
        if row.get("station") != station[1:] or not row.get("tmpf"):
            continue
        try:
            valid = datetime.fromisoformat(row["valid"]).replace(tzinfo=timezone.utc)
            temperature = float(row["tmpf"])
        except (ValueError, KeyError):
            continue
        if math.isfinite(temperature):
            observations[valid] = {
                "at": iso(valid),
                "available_at": iso(valid + HISTORICAL_OBSERVATION_LAG),
                "temp_f": temperature,
                "source": f"IEM {station} ASOS",
            }
    if not observations:
        raise ValueError(f"No usable ASOS observations for {station} and {start} to {end}")
    return [observations[valid] for valid in sorted(observations)]


def load_daily_highs(data_dir: Path, start: date, end: date, station: str = STATION,
                     standard_utc_offset_hours: int = -5) -> dict[str, dict]:
    labels = {}
    for year in range(start.year, end.year + 1):
        url = f"{IEM_CLI}?station={station}&year={year}"
        rows = json.loads(fetch_text(url, data_dir / f"cli-{station}-{year}.json"))["results"]
        for row in rows:
            if row["station"] != station or not isinstance(row["high"], (int, float)) or not row["product"]:
                continue
            issued_at = datetime.strptime(row["product"][:12], "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
            # A report issued that afternoon is still a partial climate day.
            _, closes = climate_window(row["valid"], standard_utc_offset_hours)
            if issued_at < closes:
                continue
            labels[row["valid"]] = {"high_f": row["high"], "available_at": iso(issued_at), "product": row["product"]}
    if not labels:
        raise ValueError(f"No usable complete NWS CLI reports for {station}")
    return labels


def archive_days(cache_dir: Path, start: date, end: date, workers: int = 8, *,
                 station: str = STATION, standard_utc_offset_hours: int = -5) -> tuple[dict, list[dict]]:
    """Fetch station cards using compact AWS ranges. Partial data stays visible as gaps."""
    days = [start + timedelta(days=index) for index in range(-1, (end - start).days + 1)]
    cycles = [(day, cycle) for day in days for cycle in ARCHIVE_CYCLES]
    issues = {}
    gaps = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        tasks = {pool.submit(load_nbm_cycle, cache_dir, station=station, day=day, cycle=cycle): (day, cycle)
                 for day, cycle in cycles}
        for task in as_completed(tasks):
            day, cycle = tasks[task]
            try:
                cards = task.result()
            except (OSError, ValueError, KeyError) as error:
                gaps.append({"date": str(day), "cycle": cycle, "issue": str(error)})
                continue
            if cards:
                issues[(day, cycle)] = cards
            for product in set(("nbh", "nbs")) - cards.keys():
                gaps.append({"date": str(day), "cycle": cycle, "issue": f"{product} archive card absent"})
    forecast_by_day = {}
    for day in days[1:]:
        candidates = []
        for date_key in (day - timedelta(days=1), day):
            for cycle in ARCHIVE_CYCLES:
                forecast = forecast_for_day(issues.get((date_key, cycle), {}), day, standard_utc_offset_hours)
                if forecast is not None:
                    candidates.append(forecast)
        forecast_by_day[str(day)] = candidates
    return forecast_by_day, sorted(gaps, key=lambda gap: (gap["date"], gap["cycle"], gap["issue"]))
