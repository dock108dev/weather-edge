"""Versioned weather evidence for the six-clock daily-high forecasting study."""

from __future__ import annotations

import gzip
import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile

from .model_data import archive_days, asos_url, load_asos, load_daily_highs

VERSION = "v1"
STATIONS = {"KNYC": -5, "KMIA": -5, "KLAX": -8}
WINDOWS = {
    "fit": (date(2026, 6, 1), date(2026, 9, 26)),
    "calibration": (date(2026, 9, 28), date(2026, 10, 13)),
    "selection": (date(2026, 10, 15), date(2026, 10, 27)),
    "test": (date(2026, 10, 29), date(2026, 11, 13)),
}
OBSERVATION_LAGS = (0, 10, 30)
DEFAULT_LAG = 10


def study_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "study" / VERSION


def _iem_evidence(station, folder):
    start, end = WINDOWS["fit"][0], WINDOWS["test"][1]
    obs_end = end + timedelta(days=2)
    offset = STATIONS[station]
    observations = load_asos(folder, start, obs_end, station)
    labels = load_daily_highs(folder, start, end, station, offset)
    print(f"{station}: {len(observations)} archived ASOS reports", flush=True)
    return {"observations": observations,
            "labels": {day: row for day, row in labels.items() if str(start) <= day <= str(end)},
            "standard_utc_offset_hours": offset,
            "sources": {"asos": asos_url(start, obs_end, station),
                        "cli": f"https://mesonet.agron.iastate.edu/json/cli.py?station={station}&year=2026"}}


def prepare_inputs(data_dir: Path) -> dict:
    root = study_dir(data_dir)
    frozen = root / "inputs.json.gz"
    if frozen.is_file():
        with gzip.open(frozen, "rt") as handle:
            return json.load(handle)
    cards = root / "nbm-cards"
    cards.mkdir(parents=True, exist_ok=True)
    # TXN cards can be reused from the prior version. NBH cards need the SKY extension.
    for card in (Path(data_dir) / "nbm-cards").glob("*-nbs.json"):
        target = cards / card.name
        if card.name.split("-")[0] in STATIONS and not target.is_file():
            shutil.copy2(card, target)
    with ThreadPoolExecutor(max_workers=len(STATIONS)) as pool:
        start, end = WINDOWS["fit"][0], WINDOWS["test"][1]
        jobs = {station: pool.submit(archive_days, cards, start, end, 7, station=station,
                                     standard_utc_offset_hours=offset) for station, offset in STATIONS.items()}
        stations = {station: _iem_evidence(station, root) for station in STATIONS}
        for station, job in jobs.items():
            forecasts, gaps = job.result()
            print(f"{station}: {len(forecasts)} climate dates examined, {len(gaps)} archive gaps", flush=True)
            stations[station].update(forecasts=forecasts, archive_gaps=gaps)
    inputs = {"version": VERSION, "stations": stations,
              "windows": {split: [str(begin), str(end)] for split, (begin, end) in WINDOWS.items()}}
    with NamedTemporaryFile(dir=root, prefix=".inputs-", delete=False) as handle:
        temporary = Path(handle.name)
        try:
            content = json.dumps(inputs, separators=(",", ":"), allow_nan=False).encode()
            handle.write(gzip.compress(content, mtime=0))
            handle.flush()
            temporary.replace(frozen)
        finally:
            temporary.unlink(missing_ok=True)
    return inputs
