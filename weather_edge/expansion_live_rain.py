"""Apply an earlier gauge-guarded daily-rain shadow to local weather receipts."""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
import gzip
import hashlib
import json
from pathlib import Path

from . import expansion_contract, expansion_live_low, expansion_nbm, expansion_weather, research_model
from . import research_model_rain, research_model_rain_multi, research_model_rain_update
from .history import _write
from .nbm import climate_window, iso, utc

VERSION = "daily-rain-shadow-v1"
RUNTIME = Path("research/runtime/daily-rain")
SUPPORTED_WEATHER = research_model_rain_multi.STATIONS
GAUGE_STATIONS = research_model.STATIONS
PROBABILITY_EDGES = (0.05, 0.20, 0.50, 0.80, 0.95, 1.0)


def _probability_band(value):
    return next(edge for edge in PROBABILITY_EDGES if value <= edge)


def freeze(data, source):
    data = Path(data)
    raw_input = gzip.decompress(Path(source).read_bytes())
    inputs = json.loads(raw_input)
    if inputs["version"] != research_model_rain_update.VERSION:
        raise ValueError("Expected earlier rain gauge research inputs")
    rows = inputs["rows"]["calibration"]
    wet_evidence = {(row["station"], row["date"]): row["truth"] for row in rows if row["gauge_witness"]}
    failures = sum(1 - exact for exact in wet_evidence.values())
    reserve = expansion_live_low._wilson(failures, len(wet_evidence))[1]
    support = defaultdict(dict)
    for row in rows:
        edge = _probability_band(row["pre_day"]["p06_overlap_probability"])
        support[(row["station"], edge)][row["date"]] = row["truth"]
    bands = [{"station": station, "upper_edge": edge, "calendar_days": len(outcomes), "wet": sum(outcomes.values()),
              "interval": expansion_live_low._wilson(sum(outcomes.values()), len(outcomes))}
             for (station, edge), outcomes in sorted(support.items())]
    settings = {"version": VERSION, "evidence_cutoff": max(row["official_report"]["published_at"] for row in rows),
                "climate_days": len({(row["station"], row["date"]) for row in rows}),
                "gauge_cases": len(wet_evidence), "gauge_conflicts": failures, "rain_error_allowance": reserve,
                "forecast_bands": bands,
                "calibration_window": inputs["windows"]["calibration"], "calibration_input_sha256": hashlib.sha256(raw_input).hexdigest(),
                "code_sha256": {**research_model.run_sources(expansion_contract, research_model_rain, research_model_rain_update),
                                Path(__file__).name: research_model._digest(Path(__file__))}}
    body = research_model._json_bytes(settings)
    vintage = hashlib.sha256(body).hexdigest()
    path = data / RUNTIME / f"{vintage}.json"
    if not path.exists():
        _write(path, body)
    _write(data / RUNTIME / "active.json", research_model._json_bytes({"vintage": vintage, "path": str(path.relative_to(data))}))
    return path


def load(data, *, vintage=None):
    data = Path(data)
    root = data / RUNTIME
    if vintage is None:
        pointer = root / "active.json"
        if not pointer.is_file():
            return None
        vintage = json.loads(pointer.read_bytes())["vintage"]
    if not isinstance(vintage, str) or len(vintage) != 64 or any(letter not in "0123456789abcdef" for letter in vintage):
        raise ValueError("Invalid daily-rain model vintage")
    path = root / f"{vintage}.json"
    if not path.is_file():
        return None
    raw = path.read_bytes()
    payload = json.loads(raw)
    if hashlib.sha256(raw).hexdigest() != vintage or payload["version"] != VERSION:
        raise ValueError("Unexpected daily-rain capture or model vintage")
    return {**payload, "vintage": vintage, "data": data}


def _cards(data, station, day, as_of):
    candidates = []
    for shift in (-2, -1, 0):
        for hour in research_model.NBS_CYCLES:
            issue = datetime.combine(day + timedelta(days=shift), time(hour), timezone.utc)
            card = expansion_nbm.load_card(data, station, issue)
            if not card:
                continue
            published, receipt = (utc(card[key]) for key in ("available_at", "received_at"))
            if issue <= published <= receipt <= as_of:
                candidates.append(card)
    return sorted(candidates, key=lambda row: row["issued_at"], reverse=True)


def _forecast(cards, begin, end, issuing_cutoff, as_of):
    for card in cards:
        published = utc(card["available_at"])
        if published > issuing_cutoff or issuing_cutoff - utc(card["issued_at"]) > timedelta(hours=36):
            continue
        forecast = research_model_rain.estimate_rain_window(card, begin, end)
        if forecast:
            return {**forecast, "source_published_at": iso(published), "available_at": card["received_at"],
                    "availability_basis": "local_receipt", "known_at": iso(as_of)}
    return None


def weather(data, station, day, at):
    """Return full and remaining precipitation forecasts at an original receipt clock."""
    if station not in SUPPORTED_WEATHER:
        return None
    target = date.fromisoformat(day) if isinstance(day, str) else day
    as_of = utc(at)
    start, end = climate_window(target, expansion_weather.STATIONS[station][3])
    pre_day_clock = start + timedelta(hours=research_model_rain.HOUR_LST)
    if not pre_day_clock <= as_of < end:
        return None
    cards = _cards(data, station, target, as_of)
    full_day = _forecast(cards, start, end, pre_day_clock, as_of)
    if not full_day:
        return None
    remaining = _forecast(cards, max(start, as_of), end, as_of, as_of)
    return {"station": station, "date": str(target), "threshold_in": 0.01, "climate_starts_at": iso(start),
            "climate_ends_at": iso(end), "forecast_origin_at": iso(pre_day_clock), "at": iso(as_of),
            "forecast": full_day, "remaining_forecast": remaining, "weather_assumptions": [
                "Probability combines overlapping six-hour guidance with independent-period and uniform-within-period assumptions.",
                "The remaining forecast covers time after this decision. It is separate from the chance for the entire climate day."]}


def predict(artifact, event, at):
    """Return one exact Polymarket US precipitation bucket, for inspection only."""
    if artifact is None or event.get("family") != "daily-rain" or event.get("venue") != "polymarket-us":
        return None
    try:
        station = event["station"]
        day = date.fromisoformat(event["date"])
        as_of = utc(at)
        if as_of < utc(artifact["evidence_cutoff"]) or station not in GAUGE_STATIONS or len(event["buckets"]) != 1:
            return None
        bucket = event["buckets"][0]
        if bucket["id"] != f"raincli-rain-daily-{day}-{station.lower()}" or not expansion_contract.rain_rule_matches(day, station, bucket["rules"]):
            return None
        weather_context = weather(artifact["data"], station, day, as_of)
        if not weather_context:
            return None
        start = utc(weather_context["climate_starts_at"])
        observations = [row for row in event.get("observations", []) if row.get("source") in {"NWS", "Aviation Weather Center"}
                        and not row.get("timing_proxy")]
        witness = research_model_rain_update.gauge_witness(observations, station, start, as_of)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    probability = weather_context["forecast"]["p06_overlap_probability"]
    reserve = artifact["rain_error_allowance"]
    guarded = (1 - reserve) + reserve * probability if witness else probability
    band = next((entry for entry in artifact.get("forecast_bands", []) if entry["station"] == station
                 and entry["upper_edge"] == _probability_band(probability)), None)
    lower, upper = (1 - reserve, 1.0) if witness else (band["interval"] if band else (0.0, 1.0))
    validation = artifact["gauge_cases"] if witness else band["calendar_days"] if band else 0
    receipt = max(weather_context["forecast"]["available_at"], witness["available_at"]) if witness else weather_context["forecast"]["available_at"]
    estimate = {"id": bucket["id"], "probability": guarded, "probability_no": 1 - guarded,
                "yes_lower": min(lower, guarded), "yes_upper": max(upper, guarded),
                "no_lower": 1 - max(upper, guarded), "no_upper": 1 - min(lower, guarded),
                "threshold_in": 0.01, "validation_days": validation, "uncertain": True}
    return {**weather_context, "version": VERSION, "vintage": artifact["vintage"], "mode": "shadow", "method": "p06_and_gauge",
            "target": "daily-rain", "knowledge_at": iso(as_of), "issued_at": weather_context["forecast"]["issued_at"],
            "available_at": receipt, "forecast_source": "NOAA NBM NBS P06", "gauge_witness": witness,
            "gauge_validation_days": artifact["gauge_cases"], "gauge_conflicts": artifact["gauge_conflicts"],
            "gauge_error_allowance": reserve, "pre_day_probability": probability,
            "preceding_forecast_band_support": band, "buckets": [estimate]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--input", type=Path, required=True)
    args = parser.parse_args()
    print(freeze(args.data, args.input))


if __name__ == "__main__":
    main()
