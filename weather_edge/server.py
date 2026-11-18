"""Loopback-only weather research UI. Browsing this app makes no upstream requests."""
from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from hashlib import sha256
from http.server import ThreadingHTTPServer
import json
import logging
import math
import mimetypes
from pathlib import Path
import re
from threading import Lock
from urllib.parse import parse_qs, urlsplit

from . import (
    archive, backtest, capture_index, clouds, collector, expansion_live_hdd, expansion_live_hrrr_high,
    expansion_live_low, expansion_live_precision, expansion_live_rain, expansion_nbe, expansion_precip,
    expansion_satellite, hrrr, marine, model_evidence, omo, omo_precipitation, paper_eligibility, paper_reconciler, paper_runs,
    period_replay, radar, research,
    strategy_native_capture, study, trade, trade_orders, venue_archive, venue_index, venue_replay, venue_sources,
    wave_components, waves, wind,
)
from .venue_contracts import INDEX_CITIES
from .capture import CaptureSnapshot, load_captures
from .grids import DEFAULT_CACHE, manifest
from .local_http import LocalHTTPHandler, market_filters
from .market import OPEN, paper_trial, scan
from .model import decode_model, predict

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / ".data"
BUILD = ROOT / "dist"
GRID = ROOT / DEFAULT_CACHE
CLOUDS = ROOT / clouds.DEFAULT_CACHE
WIND = ROOT / wind.DEFAULT_CACHE
PRECIPITATION = ROOT / expansion_precip.DEFAULT_CACHE
WAVES = ROOT / waves.DEFAULT_CACHE
RADAR = ROOT / radar.DEFAULT_CACHE
FRAME_ID = re.compile(r"20\d{8}-f\d{3}")
UTC = timezone.utc
CAPTURE_LOCK = Lock()
RECENT_CAPTURE_LOCK = Lock()
PAPER_FRESHNESS = trade.FRESHNESS


def read_json(path):
    if path.is_file():
        return json.loads(path.read_text())
    return None


def stamp(value):
    return datetime.fromisoformat(value)


def iso(value):
    return value.astimezone(UTC).isoformat()


MODEL_LOG = logging.getLogger("weather_edge.models")
LOW_MODELS = {"daily_low": (expansion_live_low, "joblib"),
              "daily_low_precision": (expansion_live_precision, "json")}
MODEL_FIELDS = {
    "legacy": {"stations", "ridge", "validation"},
    "study": {"stations", "selected", "operational_from", "operational_method", "estimators", "calibration"},
    "daily_low": {"stations", "widths", "method", "model", "bias", "calibration"},
    "daily_low_precision": {"groups"},
}


def _artifact_path(family, vintage=None):
    if family == "study":
        root = study.study_dir(DATA)
        return (root / "vintages" / vintage if vintage else root) / "selected.joblib"
    if family == "legacy":
        root = DATA / "model"
        return root / "vintages" / f"{vintage}.joblib" if vintage else root / "model.joblib"
    module, extension = LOW_MODELS[family]
    return DATA / module.RUNTIME / (f"{vintage}.{extension}" if vintage else "active.json")


@lru_cache(maxsize=32)
def _load_artifact(data, family, path, vintage, revision):
    """Isolate decoder failures and retry when these local bytes change."""
    try:
        if family in LOW_MODELS:
            artifact = LOW_MODELS[family][0].load(data, vintage=vintage)
            if artifact is None:
                raise FileNotFoundError(path.name)
            blob = None  # The low loaders already verify their immutable bytes.
        else:
            blob = path.read_bytes()
            digest = sha256(blob).hexdigest()
            if vintage and vintage != digest:
                raise ValueError("Artifact digest mismatch")
            artifact = (study.decode_selected(data, blob, vintage=digest) if family == "study"
                        else {**decode_model(blob), "vintage": digest})
        trade.parse_time(artifact["evidence_cutoff"])
        if not MODEL_FIELDS[family] <= artifact.keys():
            raise ValueError("Incomplete model artifact")
        if family == "legacy":
            estimators = [artifact["ridge"]]
            calibration = artifact["validation"]
            if "all" not in calibration or any(
                    not {"residuals_f", "baseline_sd_f", "days"} <= band.keys() or not band["residuals_f"]
                    for band in calibration.values()):
                raise ValueError("Incomplete legacy model calibration")
        elif family == "study":
            methods = {artifact["selected"], artifact["operational_method"]}
            if "ridge_sky" in methods:
                methods.add("ridge")  # The sky model falls back to the ordinary ridge.
            estimators = [artifact["estimators"].get(method) for method in methods & {"ridge", "ridge_sky"}]
        elif family == "daily_low" and artifact["method"] not in {"noaa", "bias"}:
            estimators = [artifact["model"]]
        else:
            estimators = []
        if any(not callable(getattr(estimator, "predict", None)) for estimator in estimators):
            raise ValueError("Invalid model estimator")
        return artifact, blob
    except Exception as error:
        # Local model deserializers can fail with EOF, pickle, compression, schema
        # or dependency errors. Keep the boundary around loading, not inference.
        MODEL_LOG.warning("%s model unavailable (%s): %s", family, type(error).__name__, path.name)
        return None, None


def _revision(stat):
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _load_family(family, vintage=None):
    try:
        if family in LOW_MODELS and vintage is None:
            vintage = json.loads(_artifact_path(family).read_bytes())["vintage"]
        if vintage is not None and not model_evidence.DIGEST.fullmatch(vintage):
            raise ValueError("Invalid model digest")
        path = _artifact_path(family, vintage)
        stat = path.stat()
    except FileNotFoundError:
        # An unconfigured optional family is normal. A missing referenced
        # vintage is an evidence gap.
        if vintage is not None:
            MODEL_LOG.warning("%s model unavailable (FileNotFoundError): %s", family, vintage)
        return None, None
    except (OSError, ValueError, KeyError, TypeError) as error:
        MODEL_LOG.warning("%s model unavailable (%s)", family, type(error).__name__)
        return None, None
    return _load_artifact(DATA, family, path, vintage, _revision(stat))


@lru_cache(maxsize=32)
def _snapshot_matches(path, revision, vintage):
    return sha256(path.read_bytes()).hexdigest() == vintage


def active_model():
    result = {}
    receipts = model_evidence.available(DATA)
    for family in ("legacy", "study", *LOW_MODELS):
        artifact, blob = _load_family(family)
        if artifact is None:
            continue
        vintage = artifact["vintage"]
        result[family] = artifact
        if family == "legacy":
            result.update(legacy_version="knyc-ridge-" + vintage[:10], legacy_vintage=vintage)
        receipt = receipts.get((family, vintage))
        try:
            path = _artifact_path(family, vintage)
            if blob is not None:
                try:
                    valid = _snapshot_matches(path, _revision(path.stat()), vintage)
                except FileNotFoundError:
                    valid = False
                if not valid:
                    model_evidence.snapshot(path, blob, vintage)
            if receipt is None:
                receipt = model_evidence.record(DATA, family, vintage, datetime.now(UTC), "local-observation",
                                                {"artifact": str(path.relative_to(DATA)), "artifact_sha256": vintage})
        except (OSError, ValueError) as error:
            MODEL_LOG.warning("%s model evidence unavailable (%s)", family, type(error).__name__)
        if receipt:
            result.setdefault("availability", {})[vintage] = receipt
    return result


def inference(model, event, at, knowledge_at=None):
    if not model or event.get("replay_no_model"):
        return None
    family = event.get("family", "daily-high")
    if family == "daily-low":
        when = knowledge_at or at
        return (expansion_live_precision.predict(model.get("daily_low_precision"), event, when)
                or expansion_live_low.predict(model.get("daily_low"), event, when))
    if family != "daily-high":
        return None
    result = study.predict(model.get("study"), event, at, knowledge_at)
    if result:
        return result
    legacy = predict(model.get("legacy"), station=event["station"], day=event["date"], as_of=at,
                     forecasts=event.get("forecasts", []), observations=event.get("observations", []),
                     buckets=event.get("buckets", []),
                     standard_utc_offset_hours=event.get("standard_utc_offset_hours", -5),
                     knowledge_at=knowledge_at)
    return {**legacy, "version": model["legacy_version"], "method": "ridge",
            "vintage": model.get("legacy_vintage")} if legacy else None


def bucket_probabilities(model, event, at):
    return _probability_map(evidenced_inference(model, event, at))


def low_release_times(model, event):
    model = model or {}
    artifact = model.get("daily_low") or {}
    precision = model.get("daily_low_precision") or {}
    start = stamp(event["climate_starts_at"])
    hours = {hour for station, hour in artifact.get("widths", {}) if station == event["station"]}
    hours.update(row["hour"] for row in precision.get("groups", [])
                 if row["station"] == event["station"] and row["exceptions"] is not None)
    return [start + timedelta(hours=hour) for hour in sorted(hours)]


def trial_probability_lookup(model):
    low_times = {}
    low_predictions = {}

    def predict_at(event, at):
        if event.get("family") != "daily-low":
            return bucket_probabilities(model, event, at)
        event_id = event["id"]
        if event_id not in low_times:
            low_times[event_id] = low_release_times(model, event)
        clocks = low_times[event_id]
        pos = bisect_right(clocks, at)
        if pos == 0 or at >= stamp(event["climate_ends_at"]):
            return {}
        decision = clocks[pos - 1]
        key = event_id, decision
        if key not in low_predictions:
            low_predictions[key] = bucket_probabilities(model, event, decision)
        return low_predictions[key]
    return predict_at


def paper_order_probability(intent):
    """Reload the model reviewed on the ticket, even after the active model rotates."""
    reviewed = intent["signal_info"]
    vintage = reviewed.get("model_vintage")
    version = reviewed.get("model_version") or ""
    proof = reviewed.get("model_availability") or {}
    legacy_prefix = (reviewed.get("model_method") == "ridge" and re.fullmatch(r"knyc-ridge-[0-9a-f]{10}", version))
    families = ("legacy", "study", *LOW_MODELS)
    if vintage or legacy_prefix:
        known = model_evidence.available(DATA)
        if vintage and proof.get("family") in families:
            family = proof["family"]
        else:
            matches = [(family, digest) for family, digest in known if digest == vintage or (
                not vintage and family == "legacy" and version == f"knyc-ridge-{digest[:10]}")]
            if not vintage and legacy_prefix:
                # Some v2 orders predate model-availability receipts. The
                # reviewed Ridge prefix identifies an archived digest there.
                prefix = version.removeprefix("knyc-ridge-")
                archived = {("legacy", path.stem) for path in (DATA / "model" / "vintages").glob(f"{prefix}*.joblib")
                            if model_evidence.DIGEST.fullmatch(path.stem)}
                matches = list(set(matches) | archived)
            if len(matches) != 1:
                raise OSError(f"Reviewed model vintage is unavailable or ambiguous: {vintage or version}")
            family, vintage = matches[0]
        artifact, _ = _load_family(family, vintage)
        if artifact is None:
            raise OSError(f"Reviewed {family} model is unreadable: {vintage}")
        model = {family: artifact, "availability": {vintage: proof or known.get((family, vintage))}}
        if family == "legacy":
            model.update(legacy_version=f"knyc-ridge-{vintage[:10]}", legacy_vintage=vintage)
        return trial_probability_lookup(model)
    return trial_probability_lookup({})


def observe_model_artifacts():
    """Record current artifacts and recover earlier proof from frozen trials."""
    known = model_evidence.available(DATA)
    for (family, vintage), (saved, source) in paper_runs.model_availability_witnesses(DATA).items():
        if not model_evidence.DIGEST.fullmatch(vintage):
            continue
        current = known.get((family, vintage))
        if current is None or saved < stamp(current["available_at"]):
            known[family, vintage] = model_evidence.record(DATA, family, vintage, saved, "frozen-paper-trial", source)
    active_model()


class HistoricalModels:
    """One point-in-time rule for chart, ticket and frozen decisions."""

    def __init__(self):
        self.versions = {family: [] for family in ("study", "legacy", *LOW_MODELS)}
        for (family, vintage), receipt in model_evidence.available(DATA).items():
            artifact, _ = _load_family(family, vintage)
            if artifact is not None:
                usable = max(stamp(receipt["available_at"]), stamp(artifact["evidence_cutoff"]))
                self.versions[family].append((usable, vintage, artifact, receipt))
        for records in self.versions.values():
            records.sort(key=lambda item: item[:2], reverse=True)

    def decision(self, event, at):
        model = {}
        for family, versions in self.versions.items():
            entry = next((entry for entry in versions if entry[0] <= at), None)
            if entry is not None:
                _, vintage, model[family], receipt = entry
                model.setdefault("availability", {})[vintage] = receipt
                if family == "legacy":
                    model.update(legacy_version="knyc-ridge-" + vintage[:10], legacy_vintage=vintage)
        return evidenced_inference(model, event, at)

    def __call__(self, event, at):
        return _probability_map(self.decision(event, at))

    def clocks(self, event, end):
        low = event.get("family") == "daily-low"
        families = LOW_MODELS if low else ("study", "legacy")
        clocks = set() if low else set(release_times(event, end))
        for family in families:
            for usable, _, artifact, _ in self.versions[family]:
                if usable <= end:
                    if low:
                        clocks.update(t for t in low_release_times({family: artifact}, event) if usable <= t <= end)
                    if (stamp(event.get("climate_starts_at") or event["starts_at"]) - timedelta(days=1)
                            <= usable <= min(end, stamp(event.get("climate_ends_at") or event["ends_at"]))):
                        clocks.add(usable)
        return sorted(clocks)


def backtest_probability_lookup():
    return HistoricalModels()


def evidenced_inference(model, event, at):
    decision = inference(model, event, at)
    if decision and (receipt := (model.get("availability") or {}).get(decision.get("vintage"))):
        artifact = model[receipt["family"]]
        return {**decision, "model_availability": receipt, "model_evidence_cutoff": artifact["evidence_cutoff"]}
    return decision


def _probability_map(result):
    if not result:
        return {}
    return {bucket["id"]: {**bucket, "model_version": result["version"], "model_method": result["method"],
                            "model_vintage": result.get("vintage"), "forecast_issued_at": result["issued_at"],
                            "forecast_available_at": result["available_at"],
                            **{key: result[key] for key in ("model_availability", "model_evidence_cutoff") if key in result}}
            for bucket in result["buckets"]}


def release_times(event, end, include_prior=False):
    start = stamp(event.get("climate_starts_at") or event["starts_at"])
    stop = min(stamp(event.get("climate_ends_at") or event["ends_at"]), end)
    if include_prior:
        start = max(start - timedelta(days=1), stamp(event["starts_at"]))
    if stop < start:
        # A capture can precede the next climate day but still has a real forecast.
        return [stop]
    count = (stop - start) // timedelta(minutes=15)
    times = {start + timedelta(minutes=15 * step) for step in range(count + 1)}
    times.add(stop)
    for series in ("forecasts", "observations"):
        times.update(stamp(row["available_at"]) for row in event.get(series, [])
                     if start <= stamp(row["available_at"]) <= stop)
    if include_prior:
        # Append the last price receipt in each five-minute slot for a bounded graph. Paper
        # execution still evaluates every eligible raw book at its precise receipt time.
        quote_slots = {}
        for bucket in event.get("buckets", []):
            for book in bucket.get("books", []):
                at = stamp(book["at"])
                if start <= at <= stop:
                    slot = int(at.timestamp()) // 300
                    quote_slots[slot] = max(at, quote_slots.get(slot, at))
        times.update(quote_slots.values())
    return sorted(times)


def _screen_book(book):
    offers = book.get("offers")
    bids = book.get("bids")
    if (
        book.get("depth_kind") != "displayed_ladder" or book.get("state") not in OPEN
        or not book.get("source_at") or book.get("source_time_progress") in {"regressed", "conflicting"}
        or (book.get("cache_age_seconds") or 0) > 0 or not offers or not bids
    ):
        return None
    best_bid, best_offer = float(bids[0][0]), float(offers[0][0])
    if best_bid >= best_offer:
        return None
    return (best_bid + best_offer) / 2


def _kalshi_receipt_midpoint(book):
    bids, offers = book.get("yes_bids"), book.get("yes_offers")
    if book.get("depth_kind") != "displayed_ladder" or not bids or not offers:
        return None
    bid, offer = float(bids[0][0]), float(offers[0][0])
    return (bid + offer) / 2 if 0 <= bid < offer <= 1 else None


def mrms_station_points(event):
    region = "-chicago" if event["station"] in {"KMDW", "KORD"} else ""
    path = DATA / "expansion" / "mrms" / "days" / f"{event['date']}{region}.json"
    try:
        record = read_json(path)
        if not record:
            return []
        start = stamp(event["climate_starts_at"])
        end = stamp(event["climate_ends_at"])
        if record["date"] != event["date"] or (stamp(record["climate_start"]), stamp(record["climate_end"])) != (start, end):
            return []
        stations = []
        for frame in record["frames"]:
            point = frame["points"].get(event["station"])
            if not point:
                continue
            measured = point["amount_mm"]
            window, valid, published, received = (
                stamp(frame[key]) for key in ("accumulation_starts_at", "valid_at", "available_at", "received_at")
            )
            if (frame["units"] == "mm" and start <= window < valid <= end and published >= valid and received >= published
                    and (valid - window) == timedelta(hours=1) and type(measured) in (int, float)
                    and math.isfinite(measured) and measured >= 0):
                stations.append({"amount_mm": measured, "distance_km": point["distance_km"], **{
                    key: frame[key] for key in ("product", "source", "accumulation_starts_at", "valid_at",
                                                "available_at", "received_at", "raw_sha256")
                }})
        return stations
    except (OSError, TypeError, KeyError, ValueError):
        return []


def _late_high_shadow(event, through):
    if (event.get("family", "daily-high") != "daily-high" or not event.get("buckets")
            or event.get("venue", venue_archive.POLY) != venue_archive.POLY or not event.get("climate_starts_at")):
        return []
    start = stamp(event["climate_starts_at"])
    if through < start + timedelta(hours=17):
        return []
    from . import strategy_high_transfer, strategy_metar_high

    for runtime in (strategy_metar_high, strategy_high_transfer):
        model = _optional_shadow(runtime.__name__.rsplit(".", 1)[-1], runtime.load_runtime)
        if model is None:
            continue
        hours = [hour for hour in model.get("decision_hours", (17,)) if hour >= 17 and start + timedelta(hours=hour) <= through]
        checks = [decision for hour in hours
                  if (decision := runtime.predict_runtime(model, event, start + timedelta(hours=hour))) is not None]
        if checks:
            report = model.get("source_study") or "six-hour-high-e030a3f9f934e9b068ac"
            return [{**decision, "study_id": report} for decision in checks]
    return []


def _low_receipt_shadow(event, through):
    if event.get("family") != "daily-low" or event.get("venue", venue_archive.POLY) != venue_archive.POLY:
        return []
    from . import expansion_live_low_receipts

    model = _optional_shadow("daily_low_receipts", expansion_live_low_receipts.load)
    if model is None:
        return []
    return [item for clock in expansion_live_low_receipts.decision_clocks(model, event, through)
            if (item := expansion_live_low_receipts.predict(model, event, clock)) is not None
            and item.get("bound_receipt_update")]


def _optional_shadow(family, loader):
    try:
        return loader(DATA)
    except Exception as error:
        MODEL_LOG.warning("%s model unavailable (%s)", family, type(error).__name__)
        return None


def _frontend_event(event, model, end, mode, *, fixture=False, historical=None):
    result = dict(event)
    result["place"] = collector.station_place(event["station"])
    if mode == "live" and event.get("family") == "daily-rain" and event.get("venue") == venue_archive.POLY:
        rain = _optional_shadow("daily_rain", expansion_live_rain.load)
        times = release_times(event, end, include_prior=True) if rain else []
        result["live_shadow_decisions"] = [{**decision, "at": iso(clock)} for clock in times
                               if (decision := expansion_live_rain.predict(rain, event, clock)) is not None]
        result["decisions"] = []
    elif event.get("replay_no_model"):
        result["decisions"] = []
    elif historical is not None:
        result["decisions"] = [item for clock in historical.clocks(event, end)
                               if (item := historical.decision(event, clock)) is not None]
        # The current live decision is separate from the point-in-time series.
        if mode == "live":
            result["live_decision"] = evidenced_inference(model, event, end)
    elif event.get("family") == "daily-low":
        clocks = (time for time in low_release_times(model, event) if time <= end)
        result["decisions"] = [item for t in clocks if (item := inference(model, event, t)) is not None]
    else:
        clocks = release_times(event, end, include_prior=mode == "live")
        result["decisions"] = [item for t in clocks if (item := inference(model, event, t)) is not None]
    result["coverage"] = archive.coverage(event)
    if not fixture and event.get("climate_starts_at") and event.get("climate_ends_at"):
        start = min(stamp(event["climate_starts_at"]), end)
        if outlooks := expansion_nbe.station_outlooks(DATA, event["station"], start, end, original_release=mode == "replay"):
            result["extended_outlooks"] = outlooks
        result["hrrr_temperature"] = [point for cycle in (0, *hrrr.INTRADAY_LEADS) for point in hrrr.station_trace(
            DATA, event["station"], event["date"], event["climate_starts_at"], event["climate_ends_at"], cycle=cycle,
        )]
        result["mrms_precipitation"] = mrms_station_points(event)
        result["madis_airport_temperature"] = omo.station_trace(
            DATA, event["station"], event["climate_starts_at"], event["climate_ends_at"], original_receipts=mode == "live",
        )
        if event.get("family") == "daily-rain":
            result["madis_airport_rain"] = omo_precipitation.station_trace(
                DATA, event["station"], event["climate_starts_at"], event["climate_ends_at"], original_receipts=mode == "live",
            )
        result["satellite_observations"] = expansion_satellite.observed_station(
            DATA, event["station"], date.fromisoformat(event["date"]), as_of=end, original_receipts=mode == "live",
        )
    if mode == "live":
        result["station_name"] = collector.STATION_NAMES.get(event["station"], event["station"])
        if not fixture:
            result["late_high_shadow"] = _late_high_shadow(event, end)
            if event.get("family", "daily-high") == "daily-high" and event.get("buckets"):
                hrrr_model = _optional_shadow("hrrr_high", expansion_live_hrrr_high.load)
                result["hrrr_high_shadow"] = expansion_live_hrrr_high.timeline(hrrr_model, event, end)
            result["low_receipt_shadow"] = _low_receipt_shadow(event, end)
        if event.get("buckets") and not event.get("rules_source") and event.get("family") not in {"daily-low", "daily-rain"}:
            result["rules_source"] = "https://www.cftc.gov/IndustryOversight/IndustryFilings/TradingOrganizationProducts/60333"
        elif event.get("family") == "daily-low" and not event.get("rules_source"):
            result["rules_source"] = event.get("source")
        midpoint = _kalshi_receipt_midpoint if event.get("venue") == venue_archive.KALSHI else _screen_book
        result["buckets"] = []
        for bucket in event["buckets"]:
            clean = {key: value for key, value in bucket.items() if key != "books"}
            clean["prices"] = bucket.get("prices", []) + [
                {"at": book["at"], "yes": midpoint(book), "no": None, "source_at": book.get("source_at"),
                 "cache_age_seconds": book.get("cache_age_seconds"), "is_book": book.get("depth_kind") == "displayed_ladder"}
                for book in bucket.get("books", [])
            ]
            result["buckets"].append(clean)
    return result


@lru_cache(maxsize=2)
def _fixture_snapshot(folder, revision):
    return load_captures(folder, fixture=True)


@lru_cache(maxsize=1)
def _live_snapshot(folder):
    return CaptureSnapshot(folder)


def current_capture(fixture=False, *, include_low=False, weather=None, summarize_low=False):
    folder = DATA / ("fixtures" if fixture else "captures")
    with CAPTURE_LOCK:
        try:
            revision = folder.stat().st_mtime_ns
        except FileNotFoundError:
            record = None
        else:
            record = _fixture_snapshot(folder, revision) if fixture else _live_snapshot(folder).load(revision)
    if include_low and not fixture:
        record = _include_low_capture(record, summarize_books=summarize_low)
    if record and not fixture:
        return collector.enrich_capture(DATA, record, weather=weather)
    return record


def _include_low_capture(record, *, summarize_books=False):
    now = datetime.now(UTC)
    low, issues = strategy_native_capture.polymarket_events(
        DATA, now, since=(record or {}).get("started_at"), summarize_books=summarize_books,
    )
    if not low:
        return {**record, "issues": [*record.get("issues", []), *issues]} if record and issues else record
    clocks = [clock for event in low for bucket in event["buckets"]
              for clock in strategy_native_capture.book_clocks(bucket)]
    clocks.extend(stamp(bucket["settlement"]["available_at"]) for event in low for bucket in event["buckets"]
                  if bucket.get("settlement"))
    first = min(clocks)
    latest = max(clocks)
    original = record or {"kind": "live", "events": [], "issues": [], "started_at": iso(first), "captured_at": iso(latest)}
    return {
        **original, "events": [*original["events"], *low],
        "started_at": iso(min(first, stamp(original["started_at"]))),
        "captured_at": iso(max(latest, stamp(original["captured_at"]))),
        "issues": [*original.get("issues", []), *issues],
    }


def live_weather(capture, weather):
    known_days = {(e["station"], e["date"]) for e in capture.get("events", [])}
    additional = [event for event in weather if (event["station"], event["date"]) not in known_days]
    receipts = [
        stamp(row["available_at"]) for event in weather
        for source in ("observations", "forecasts", "research_observations", "official_reports")
        for row in event.get(source, [])
    ]
    if not receipts:
        return capture
    first = min(receipts + ([stamp(capture["started_at"])] if capture.get("started_at") else []))
    latest = max(receipts + ([stamp(capture["captured_at"])] if capture.get("captured_at") else []))
    return {**capture, "events": [*capture.get("events", []), *additional],
            "captured_at": iso(latest), "started_at": iso(first)}


def _focus_supplemental_high(capture, station, instrument, weather):
    supplemental, notes = strategy_native_capture.polymarket_events(
        DATA, datetime.now(UTC), since=capture["started_at"], family="daily-high", station=station,
    )
    matches = [event for event in supplemental if any(bucket["id"] == instrument for bucket in event["buckets"])]
    if not matches:
        return capture
    merged = {**capture, "events": list(capture["events"])}
    added, conflicts = strategy_native_capture.merge_supplemental(merged, matches)
    if not added:
        return {**capture, "issues": [*capture.get("issues", []), *notes, *conflicts]}
    clocks = [stamp(capture["captured_at"])]
    clocks.extend(stamp(book["at"]) for event in matches if event["id"] in added
                  for bucket in event["buckets"] for book in bucket["books"])
    merged["captured_at"] = iso(max(clocks))
    merged["issues"] = [*capture.get("issues", []), *notes, *conflicts]
    return collector.enrich_capture(DATA, merged, weather=weather) if any(
        event["id"] in added and not event["forecasts"] for event in merged["events"]
    ) else merged


def _live_archive_market(capture, identity, *, weather=None, exact_book=None, chart=False):
    venue, instrument = identity
    if venue not in {venue_archive.KALSHI, venue_archive.POLY} or not capture.get("captured_at"):
        return capture
    with venue_archive.connect(DATA) as db:
        row = db.execute("SELECT day,family,station,first_received_at FROM markets WHERE venue=? AND instrument_id=?",
                         (venue, instrument)).fetchone()
    if row is None or not row["day"] or row["family"] not in venue_replay.FAMILIES:
        return capture
    if venue == venue_archive.POLY:
        if row["family"] == "daily-high" and row["station"]:
            capture = _focus_supplemental_high(capture, row["station"], instrument, weather)
        if any(event.get("venue", venue_archive.POLY) == venue and
               any(bucket["id"] == instrument for bucket in event["buckets"]) for event in capture["events"]):
            return capture
    event = venue_replay.load(DATA, venue, instrument, row["day"], capture["events"], archive_prices=False)
    if event is None or (venue == venue_archive.POLY and not event["replay_market_window_exact"]):
        return capture
    basis = "YES midpoint at local receipt" if venue == venue_archive.KALSHI else "YES book midpoint"
    event = {**event, "ends_at": event["trading_closed_at"], "price_basis": basis,
             "source": event.get("rules_source") or event["market_source"]}
    clocks = [stamp(capture["captured_at"])]
    latest = datetime.now(UTC)
    with venue_archive.connect(DATA) as db:
        db.execute("BEGIN")
        start = capture["started_at"]
        for bucket in event["buckets"]:
            ident = bucket["id"]
            if exact_book is not None:
                books = [exact_book] if ident == instrument else []
            elif chart:
                books = venue_archive.book_images(db, venue, ident, start=start, end=latest)
            else:
                books = venue_archive._book_query(db, venue, ident, start=start, end=latest)
            bucket["books"] = books
            clocks.extend(stamp(book["at"]) for book in books)
            if (payout := bucket.get("archived_payout")) and payout["archive_received_at"]:
                received = stamp(payout["archive_received_at"])
                if received <= latest:
                    bucket["settlement"] = {**payout, "available_at": iso(received), "timeline_basis": "local payout receipt"}
                    clocks.append(received)
    event["starts_at"] = iso(max(stamp(event["starts_at"]), stamp(row["first_received_at"])))
    return {**capture, "events": [event, *capture["events"]], "captured_at": iso(max(clocks))}


def _replay_end(event):
    times = [stamp(event["ends_at"])]
    if event.get("price_basis") and event.get("trading_closed_at"):
        times.append(stamp(event["trading_closed_at"]))
    settlement = event.get("settlement") or {}
    times.extend(stamp(time) for time in (event.get("settles_at"), settlement.get("available_at"),
                                         settlement.get("payout_available_at")) if time)
    times.extend(stamp(report["available_at"]) for report in event.get("official_reports", []))
    times.extend(stamp(bucket["settlement"]["available_at"]) for bucket in event.get("buckets", [])
                 if bucket.get("settlement", {}).get("available_at"))
    return max(times)


def _grid_window(frames, start, through):
    return [row for row in frames if start - timedelta(hours=6) <= stamp(row["valid_at"]) <= through + timedelta(hours=36)
            and stamp(row["available_at"]) <= through]


def _forecast_layers(fixture=False):
    wave_frames = waves.manifest(WAVES) if not fixture else []
    wave_frames = [{**frame, "components": {key: part[key] for key in ("available_at", "ingested_at", "sha256")}}
                   if (part := wave_components.metadata(DATA, frame)) else frame for frame in wave_frames]
    return {"grids": manifest(GRID), "clouds": clouds.manifest(CLOUDS), "winds": wind.manifest(WIND),
            "precipitation": expansion_precip.manifest(PRECIPITATION) if not fixture else [],
            "waves": wave_frames, "radar": radar.manifest(RADAR) if not fixture else []}


def _archive_calendar():
    catalog = archive.catalog(DATA)
    days = {row["date"]: row for row in catalog["days"]}
    today = datetime.now(UTC).date()
    for layer in _forecast_layers().values():
        for frame in layer:
            valid = stamp(frame["valid_at"]).astimezone(UTC).date()
            if valid <= today and stamp(frame["available_at"]).astimezone(UTC).date() <= valid:
                days.setdefault(valid.isoformat(), {"date": valid.isoformat(), "stations": [], "global_weather": True})
    return {**catalog, "days": [days[day] for day in sorted(days, reverse=True)]}


def _station_entry(event):
    keys = ("id", "station", "date", "family", "venue", "title", "lat", "lon", "timezone",
            "starts_at", "ends_at", "weather_only")
    return {**{key: event[key] for key in keys if key in event},
            "station_name": collector.STATION_NAMES.get(event["station"], event["station"]),
            "place": collector.station_place(event["station"]),
            "buckets": [{"id": bucket["id"], "label": bucket["label"]} for bucket in event["buckets"]],
            "summary_only": True}


def _session_context(mode, fixture, day, market, *, chart=False, summarize_low=False):
    if mode == "replay":
        own = archive.load_day(DATA, day)
        if market and not fixture:
            venue, instrument = market
            existing = next((event for event in own["events"] if venue == venue_archive.POLY and
                             any(row["id"] == instrument for row in event["buckets"])), None)
            if not existing:
                market_event = venue_replay.load(DATA, venue, instrument, day, own["events"])
                if market_event is not None:
                    own["events"] = [market_event, *own["events"]]
    else:
        weather = None if fixture else collector.weather_events(DATA)
        own = current_capture(fixture, include_low=True, weather=weather, summarize_low=summarize_low) or {}
        if not fixture:
            own = live_weather(own, weather)
            if market:
                own = _live_archive_market(own, market, weather=weather, chart=chart)
    return own


def _compact_event(own, market, event_id, station):
    events = own.get("events", [])
    if not events:
        return None
    selected = None
    if market:
        venue, instrument = market
        selected = next((event for event in events if event.get("venue", venue_archive.POLY) == venue
                         and any(bucket["id"] == instrument for bucket in event["buckets"])), None)
    selected = selected or next((event for event in events if event["id"] == event_id), None)
    active = [event for event in events if stamp(event["ends_at"]) > stamp(own["captured_at"])]
    return (selected
            or next((event for event in active if event["station"] == station and event["buckets"]), None)
            or next((event for event in active if event["station"] == station), None)
            or next((event for event in active if event["buckets"]), None)
            or next(iter(active), events[-1]))


def _summarized_books(event):
    return any("_book_bounds" in bucket for bucket in event["buckets"])


def session_data(mode, fixture=False, day=None, market=None, *, compact=False, event_id=None, station=None):
    scoped = compact and mode == "live" and not fixture
    own = _session_context(mode, fixture, day, market, chart=True, summarize_low=scoped)
    selected = _compact_event(own, market, event_id, station) if scoped else None
    if selected and _summarized_books(selected):
        # A low timeline needs its actual ladders. Reassemble on one full
        # read, keeping its clocks and available receipts from the same pass.
        own = _session_context(mode, fixture, day, market, chart=True)
        selected = _compact_event(own, market, event_id, station)
    mode_end = _replay_end if mode == "replay" else lambda event: stamp(own["captured_at"])
    model = active_model() if mode == "live" else None
    historical = None if fixture else HistoricalModels()
    layers = _forecast_layers(fixture)
    events = own.get("events", [])
    weather_window = None
    if mode == "replay" and events:
        start = min(stamp(event.get("climate_starts_at") or event["starts_at"]) for event in events)
        end = max(_replay_end(event) for event in events)
        layers = {kind: _grid_window(frames, start, end) for kind, frames in layers.items()}
    elif mode == "replay" and day and not fixture:
        start = datetime.combine(date.fromisoformat(day), time.min, UTC)
        end = min(start + timedelta(days=1) - timedelta(microseconds=1), datetime.now(UTC))
        layers = {kind: _grid_window(frames, start, end) for kind, frames in layers.items()}
        if start < end and any(start <= stamp(frame["valid_at"]) <= end for frames in layers.values() for frame in frames):
            weather_window = {"starts_at": iso(start), "ends_at": iso(end)}
    elif mode == "live" and fixture:
        sessions = own.get("sessions", [])
        for kind in layers:
            frame_ids = {frame for session in sessions for frame in session.get(kind, {}).get("frames", [])}
            layers[kind] = [row for row in layers[kind] if row["id"] in frame_ids]
    elif mode == "live" and own.get("captured_at"):
        end = stamp(own["captured_at"])
        start = max(stamp(own["started_at"]), end - timedelta(days=2))
        layers = {kind: _grid_window(frames, start, end) for kind, frames in layers.items()}
    else:
        layers = {kind: [] for kind in layers}
    return {
        "events": [_station_entry(event) if selected is not None and event is not selected else
                   {**_frontend_event(event, model, mode_end(event), mode, fixture=fixture, historical=historical),
                    "timeline_ends_at": iso(mode_end(event))} for event in events],
        "detail_event": selected["id"] if selected else None,
        "captured_at": own.get("captured_at"),
        "started_at": own.get("started_at"),
        "issues": own.get("issues", []),
        "sessions": [{"started_at": record.get("started_at"), "captured_at": record["captured_at"],
                      "rounds": record.get("rounds")} for record in own.get("sessions", []) if record.get("captured_at")],
        **layers,
        "weather_window": weather_window,
        "fixture": fixture and mode == "live",
        "cached": bool(events or own.get("captured_at") or weather_window),
    }


def paper_data(fixture, at, size, reaction, *, include_trial=True, compact=False, freshness=None, historical=True):
    capture = current_capture(fixture, include_low=True)
    if capture is None:
        return {"rows": [], "trial": None}
    clock_basis = paper_eligibility.RECEIPT_CLOCK if fixture else paper_eligibility.APP_CLOCK
    if not fixture:
        capture = strategy_native_capture.with_listing_evidence(DATA, capture, min(at, stamp(capture["captured_at"])))
    models = HistoricalModels() if not fixture else None
    model = active_model() if fixture or not historical else None
    decide = models.decision if historical and models else lambda event, clock: evidenced_inference(model, event, clock)
    pool = {}
    decisions = {}
    for event in capture["events"]:
        if decision := decide(event, at):
            decisions[event["id"]] = decision
            pool.update(_probability_map(decision))
    trial = None
    if include_trial:
        trial = paper_trial(capture, models or trial_probability_lookup(model),
                            reaction_seconds=reaction, order_size=size, as_of=min(at, stamp(capture["captured_at"])),
                            freshness=freshness, clock_basis=clock_basis)
        if compact:
            attempts = trial["attempts"]
            trial = {**trial, "attempts": attempts[-300:], "attempt_count": len(attempts),
                     "signal_rejections": trial["signal_rejections"][-300:],
                     "attempt_results": dict(Counter(attempt["result"] for attempt in attempts))}
    return {
        "rows": scan(capture, at, pool, order_size=size, freshness=freshness, clock_basis=clock_basis),
        "trial": trial,
        "latest_market_at": capture["captured_at"],
        "decisions": decisions,
    }


def _model_report():
    legacy = read_json(DATA / "model" / "report.json")
    report = read_json(study.study_dir(DATA) / "report.json")
    return {"legacy": legacy, "study": report}


def _paper_settings(at, size, reaction):
    cutoff = stamp(at)
    size = float(size)
    interval = int(reaction)
    if cutoff.utcoffset() is None or not 1 <= size <= 500 or not 1 <= interval <= 120:
        raise ValueError("Invalid paper-execution parameters")
    return cutoff, size, interval


@lru_cache(maxsize=2)
def _recent_trade_capture(directory, revision, since):
    return load_captures(directory, since=since, versioned_windows=True)


def _kalshi_live_ticket_capture(market, cutoff):
    # A published ladder is already enough for this archived preview. Use the
    # exact native event and same station day; do not assemble the live board's
    # Polymarket tape just to find another copy of the same book.
    venue, instrument = market
    end = min(cutoff, datetime.now(UTC))
    with venue_archive.connect(DATA) as db:
        db.execute("BEGIN")
        row = db.execute("SELECT day,station FROM markets WHERE venue=? AND instrument_id=?",
                         market).fetchone()
        if not row or not row["day"] or not row["station"]:
            return None
        # Old receipts can omit the fractional part. Across that boundary the
        # broad path compares parsed clocks instead of sorting timestamp text.
        if db.execute("SELECT 1 FROM books WHERE venue=? AND instrument_id=? AND at=?",
                      (*market, end.replace(microsecond=0).isoformat().replace("+00:00", "Z"))).fetchone():
            return None
        books = venue_archive._book_query(db, venue, instrument, end=end, limit=1)
    if not books or books[0]["depth_kind"] != "displayed_ladder":
        return None
    book = books[0]
    if not stamp(book["at"]).microsecond or (book.get("available_at") and stamp(book["available_at"]) > cutoff):
        return None
    weather = collector.weather_events(DATA, station=row["station"], start=row["day"], end=row["day"])
    if not weather:
        return None
    scoped = {"events": weather, "started_at": book["at"], "captured_at": book["at"]}
    capture = _live_archive_market(scoped, market, weather=weather, exact_book=book)
    return capture if capture is not scoped else None


def _trade_context(fixture, *, archived=False, day=None, at=None, history_mode=None, market=None):
    if fixture:
        capture = current_capture(True)
        if not capture:
            return None, datetime.now(UTC)
        # Start at the first complete book round, leaving the retained reaction
        # receipts available for the independently labeled fixture ledger.
        first = [stamp(bucket["books"][0]["at"]) for event in capture["events"]
                 for bucket in event["buckets"] if bucket["books"]]
        return capture, trade.parse_time(at) if archived else max(first, default=stamp(capture["captured_at"]))
    if archived:
        cutoff = trade.parse_time(at)
        mode = "live" if history_mode == "live" else "replay"
        if mode == "live" and market and market[0] == venue_archive.KALSHI:
            if capture := _kalshi_live_ticket_capture(market, cutoff):
                return capture, cutoff
        context = _session_context(mode, False, day, market, summarize_low=mode == "live" and bool(market))
        if market and any(_summarized_books(event) and
                          event.get("venue", venue_archive.POLY) == market[0] and
                          any(bucket["id"] == market[1] for bucket in event["buckets"])
                          for event in context.get("events", [])):
            context = _session_context(mode, False, day, market)
        return context, cutoff
    now = datetime.now(UTC)
    # The capture index restores source-clock high water marks across the window.
    # Older exact contracts remain available on demand via the market archive.
    start = now.replace(second=0, microsecond=0) - timedelta(minutes=3)
    directory = DATA / "captures"
    # A cold full-tape merge must not make a three-minute board read wait.
    # SQLite still coordinates the index writes in the two windows.
    with RECENT_CAPTURE_LOCK:
        capture = (_recent_trade_capture(directory, directory.stat().st_mtime_ns, start)
                   if directory.is_dir() else None)
    low, _ = strategy_native_capture.polymarket_events(DATA, now, since=start, versioned=False)
    merged = {**(capture or {}), "kind": "live", "events": [*(capture or {}).get("events", []), *low]}
    return collector.enrich_capture(DATA, merged), datetime.now(UTC)


def _trade_selection(params):
    return trade.identity(params["venue"], params["instrument"], params["side"])


def _trade_ticket(fixture, params, selected, budget):
    archived = "at" in params or params.get("archived") == "1" or params.get("forecast") == "1"
    if selected[0] == venue_archive.KALSHI and not fixture and not archived:
        # Live Kalshi previews use the exact archived listing and book. There
        # is no matching event in the Polymarket tape or model to evaluate.
        capture, at, lookup = None, datetime.now(UTC), None
    else:
        capture, at = _trade_context(fixture, archived=archived, day=params.get("day"), at=params.get("at"),
                                     history_mode=params.get("history_mode"), market=selected[:2])
        lookup = HistoricalModels() if archived and not fixture else trial_probability_lookup(active_model())
    rows, witness = trade.market_rows(DATA, capture, lookup, at,
                                      fixture=fixture, selected=selected, budget=budget, archived=archived)
    return (rows[0] if rows else None), witness


def _market_weather_links():
    archived = archive.catalog(DATA)
    matches = {(place["station"], row["date"]) for row in archived["days"] for place in row["stations"]
               if any(place["coverage"][key] for key in ("observations", "forecasts", "official_reports"))}
    matches.update((row["station"], row["date"]) for row in collector.weather_events(DATA)
                   if row["observations"] or row["forecasts"] or row["official_reports"])
    with venue_archive.connect(DATA) as db:
        db.execute("CREATE TEMP TABLE weather_days (station TEXT,day TEXT, PRIMARY KEY (station,day)) WITHOUT ROWID")
        db.executemany("INSERT INTO weather_days VALUES (?,?)", sorted(matches))
        result = db.execute("""SELECT m.venue,m.family,count(*) FROM weather_days w
            CROSS JOIN markets m ON m.station=w.station AND m.day=w.day
            WHERE m.family IN ('daily-high','daily-low','daily-rain') GROUP BY m.venue,m.family""").fetchall()
    return {(venue, family): count for venue, family, count in result}


def _archive_market(venue, instrument, *, book_limit=20):
    if venue not in venue_archive.VENUES or not 0 < len(instrument) <= 200:
        raise ValueError("Invalid market identity")
    market = venue_archive.detail(DATA, venue, instrument)
    if market is not None:
        if venue == venue_archive.POLY and market["family"] == "daily-high":
            market["books"] = capture_index.latest_books(DATA / "captures", instrument, limit=book_limit)
        elif book_limit != 20:
            market["books"] = venue_archive.books(DATA, venue, instrument, limit=book_limit)
    return market


def _index_replay(market, decision):
    city = INDEX_CITIES[market["series_id"]][0]
    close = stamp(market["closed_at"])
    now = datetime.now(UTC)
    latest = min(close, now)
    if decision is not None:
        decision = stamp(decision)
        if (decision.utcoffset() is None or decision > latest or decision < close - timedelta(hours=6)):
            raise ValueError("Choose a zoned decision during the six hours before the contract close")
    else:
        decision = close - timedelta(minutes=15) if close <= now else now
    start = decision - timedelta(hours=2)
    rows = venue_index.points(DATA, city, start=start, end=latest)
    points, last_version = [], {}
    for row in rows:
        version = row["temperature_f"], row["contributors"], row["status"]
        if last_version.get(row["at"]) == version:
            continue
        points.append({key: row[key] for key in ("at", "temperature_f", "contributors", "status", "received_at", "source", "receipt_id")})
        last_version[row["at"]] = version
    canonical = [row for row in points if row["temperature_f"] is not None]
    at_close = next((row for row in reversed(canonical) if stamp(row["at"]) == close), None)
    known = next((row for row in reversed(canonical) if stamp(row["at"]) <= decision
                  and stamp(row["received_at"]) <= decision), None)
    return {"city": city, "points": points, "target_at": iso(close), "at": iso(decision),
            "starts_at": iso(start), "ends_at": iso(min(now, close + timedelta(minutes=15))),
            "captured_by_decision": known, "target_point": at_close}


def _market_detail(venue, instrument, index_at=None):
    market = _archive_market(venue, instrument)
    if market is not None:
        market["research_cases"] = research.cases_for_market(DATA, venue, instrument)
        if market.get("family") == "monthly-rain":
            market["monthly_rain"] = period_replay.monthly_rain(DATA, market)
        if market.get("family") in ("weekend-rain", "holiday-rain"):
            market["reporting_days_rain"] = period_replay.reporting_days_rain(DATA, market)
        if market.get("family") == "holiday-rain":
            market["holiday_rain_outlook"] = period_replay.holiday_rain_outlook(DATA, market, datetime.now(UTC))
        if market.get("family") == "heating-degree-days":
            market["hdd_projection"] = expansion_live_hdd.market_projection(DATA, market, datetime.now(UTC))
    if (market and market.get("resolution_source") == "Kalshi Weather Index / Synoptic Data"
            and market.get("series_id") in INDEX_CITIES and market.get("closed_at")):
        market["weather_index"] = _index_replay(market, index_at)
    if market is not None and market.get("station") and market.get("day"):
        replay = archive.load_day(DATA, market["day"])
        event = next((row for row in replay["events"] if row["station"] == market["station"]), None)
        if event is not None:
            market["weather_replay"] = {
                "date": event["date"],
                "station": event["station"],
                "coverage": archive.coverage(event),
                "bucket": next((item["id"] for item in event["buckets"] if item["id"] == instrument), None),
            }
    return market


def _nearby_venue_books(left, right):
    from . import strategy_crossvenue, venue_fees
    from .strategy_rules import book_status

    by_venue = {market["venue"]: market for market in (left, right)}
    if set(by_venue) != {venue_archive.KALSHI, venue_archive.POLY}:
        return None
    poly, kalshi = (by_venue[venue] for venue in (venue_archive.POLY, venue_archive.KALSHI))
    native = sorted(((stamp(book["at"]), book) for book in poly["books"]
                     if book_status(book, None, stamp(book["at"]), "origin-confirmed") is None),
                    key=lambda item: item[0])
    times, books = map(list, zip(*native)) if native else ([], [])
    pair = {"polymarket": poly, "kalshi": kalshi}
    pairs = strategy_crossvenue.match_live_quotes(pair, {poly["instrument_id"]: (times, books)}, kalshi["books"])
    recent = next(pairs, None)
    price_cash = []
    if recent and (bounds := strategy_crossvenue.temperature_window(poly)) is not None:
        if (bounds == strategy_crossvenue.temperature_window(kalshi)
                and (poly["station"], poly["day"], poly["family"]) == (kalshi["station"], kalshi["day"], kalshi["family"])):
            schedule = venue_fees.kalshi_terms(DATA, kalshi["instrument_id"], recent["at"])
            price_cash = strategy_crossvenue.comparable_quote_cash(recent, schedule)
    return {"latest": recent, "max_receipt_gap_seconds": strategy_crossvenue.MAX_RECEIPT_DIFFERENCE.total_seconds(),
            "qualified_polymarket_books": len(books), "kalshi_books": len(kalshi["books"]), "same_side_cash": price_cash}


def _market_comparison(query):
    first = query["first_venue"][0], query["first_instrument"][0]
    second = query["second_venue"][0], query["second_instrument"][0]
    if first == second:
        raise ValueError("Choose two distinct contracts")
    limit = 100 if first[0] != second[0] else 20
    left = _archive_market(*first, book_limit=limit)
    right = _archive_market(*second, book_limit=limit)
    if left is None or right is None:
        return None
    return {
        "left": {key: left.get(key) for key in (*venue_archive.EXPORT_COLUMNS, "clocks")},
        "right": {key: right.get(key) for key in (*venue_archive.EXPORT_COLUMNS, "clocks")},
        "comparison": venue_archive._compare(left, right),
        "books": _nearby_venue_books(left, right),
    }


class UIHandler(LocalHTTPHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(BUILD), **kwargs)

    def do_GET(self):
        parsed = urlsplit(self.path)
        path = parsed.path
        if not path.startswith("/api/"):
            return super().do_GET()
        if not self.local_host():
            return self.send_json({"error": "The research API is loopback-only"}, 403)
        query = parse_qs(parsed.query)
        fixture = query.get("fixture") == ["1"]
        try:
            if path == "/api/session":
                mode = query.get("mode", ["replay"])[0]
                if mode not in ("replay", "live"):
                    raise ValueError("Unknown data mode")
                market = (query["venue"][0], query["instrument"][0]) if "venue" in query and "instrument" in query else None
                if market and len(market[1]) > 200:
                    raise ValueError("Invalid replay market identity")
                return self.send_json(session_data(
                    mode, fixture, query.get("day", [None])[0], market,
                    compact=query.get("compact") == ["1"], event_id=query.get("event", [None])[0],
                    station=query.get("station", ["KNYC"])[0],
                ))
            if path == "/api/decision":
                params = {key: values[0] for key, values in query.items()}
                mode = params.get("mode", "replay")
                if mode not in ("replay", "live"):
                    raise ValueError("Unknown data mode")
                at = trade.parse_time(params["at"])
                if at > datetime.now(UTC) and not fixture:
                    raise ValueError("A historical decision clock cannot be in the future")
                market = (_trade_selection({**params, "side": "YES"})[:2]
                          if "venue" in params and "instrument" in params else None)
                def matches(event):
                    return event["id"] == params["event"] and (
                        not market or (event.get("venue", venue_archive.POLY) == market[0] and
                                       any(bucket["id"] == market[1] for bucket in event["buckets"]))
                    )

                day = params.get("day")
                capture = _session_context(mode, fixture, day, market, summarize_low=mode == "live" and not fixture)
                event = next(filter(matches, capture.get("events", [])), None)
                if event and _summarized_books(event):
                    capture = _session_context(mode, fixture, day, market)
                    event = next(filter(matches, capture.get("events", [])), None)
                decision = None
                if event:
                    decision = (evidenced_inference(active_model(), event, at) if fixture
                                else HistoricalModels().decision(event, at))
                return self.send_json({"decision": decision, "at": iso(at)})
            if path == "/api/model":
                return self.send_json(_model_report())
            if path == "/api/trade":
                params = {key: values[0] for key, values in query.items()}
                if "instrument" in params:
                    ticket, _ = _trade_ticket(fixture, params, _trade_selection(params),
                                              trade.order_size(params.get("budget", trade.DEFAULT_BUDGET)))
                    return self.send_json(ticket or {"error": "Exact contract evidence is unavailable"},
                                          200 if ticket else 404)
                capture, at = _trade_context(fixture)
                budget = trade.order_size(params.get("budget", trade.DEFAULT_BUDGET))
                rows, _ = trade.market_rows(DATA, capture, trial_probability_lookup(active_model()), at,
                                            fixture=fixture, budget=budget)
                board = trade.opportunity_board(rows, at, fixture, budget)
                if params.get("summary") == "1":
                    board = {**{key: board[key] for key in ("at", "fixture", "blockers", "coverage")},
                             "ready_count": len(board["ready"]), "watch_count": len(board["watch"])}
                    if params.get("funnel") == "1":
                        board["funnel"] = trade.funnel_snapshot(rows, data=None if fixture else DATA, at=at, budget=budget,
                                                                fixture=fixture)
                return self.send_json(board)
            if path == "/api/trade/orders":
                return self.send_json(trade_orders.orders(DATA, fixture))
            if path == "/api/archive":
                return self.send_json(_archive_calendar())
            if path == "/api/collection":
                return self.send_json(collector.status(DATA))
            if path == "/api/marine":
                cutoff = stamp(query["at"][0])
                if cutoff.utcoffset() is None:
                    raise ValueError("Marine observations require an offset-aware cutoff")
                return self.send_json({"stations": [] if fixture else marine.snapshot(DATA, cutoff),
                                       "at": iso(cutoff), "source": marine.SOURCE})
            if path == "/api/marine/archive" and not fixture:
                result = marine.wave_archive_measurement(DATA, query["station"][0], query["at"][0])
                return self.send_json({"observation": result})
            if path == "/api/wave-point" and not fixture and FRAME_ID.fullmatch(query["frame"][0]):
                ident = query["frame"][0]
                lat, lon = float(query["lat"][0]), float(query["lon"][0])
                sample = waves.point(WAVES, ident, lat, lon)
                if sample is not None and "at" in query:
                    height = waves._cached(WAVES / (ident + ".json"))
                    sample["components"] = wave_components.point(DATA, height, lat, lon, query["at"][0],
                                                                local_receipt=query.get("local") == ["1"])
                return self.send_json(sample if sample is not None else {"error": "Native wave frame not found"},
                                      200 if sample is not None else 404)
            if path == "/api/hrrr-point" and not fixture:
                sample = hrrr.point(DATA, float(query["lat"][0]), float(query["lon"][0]), query["at"][0],
                                    query["valid"][0], local_receipt=query.get("local") == ["1"],
                                    pinned_issue=query.get("issue", [None])[0])
                return self.send_json({"sample": sample})
            if path == "/api/markets":
                limit = int(query.get("limit", ["50"])[0])
                offset = int(query.get("offset", ["0"])[0])
                return self.send_json(venue_archive.query(DATA, limit=limit, offset=offset, **market_filters(query, venue_archive.VENUES)))
            if path == "/api/markets/coverage":
                coverage = venue_archive.coverage(DATA)
                previous = capture_index.coverage(DATA / "captures")
                if previous:
                    coverage["books"].append(previous)
                links = _market_weather_links()
                for row in coverage.get("families", []):
                    row["linked_station_weather"] = links.get((row["venue"], row["family"]))
                    if previous and (row["venue"], row["family"]) == (venue_archive.POLY, "daily-high"):
                        row["in_direct_capture_index"] = previous["instruments"]
                return self.send_json(coverage)
            if path == "/api/markets/detail":
                market = _market_detail(query["venue"][0], query["instrument"][0], query.get("index_at", [None])[0])
                return self.send_json(market if market is not None else {"error": "Market receipt not found"},
                                      200 if market is not None else 404)
            if path == "/api/markets/compare":
                comparison = _market_comparison(query)
                return self.send_json(comparison if comparison is not None else {"error": "Compare receipts not found"},
                                      200 if comparison is not None else 404)
            if path in ("/api/markets/export.csv", "/api/markets/export.json"):
                rows = venue_archive.export(DATA, **market_filters(query, venue_archive.VENUES))
                return self.send_market_export(path.rsplit(".", 1)[1], rows,
                                               venue_archive.EXPORT_COLUMNS, venue_archive.csv_row, venue_archive.dumps)
            if path == "/api/research":
                return self.send_json(research.catalog(DATA))
            if path.startswith("/api/research/"):
                parts = path.removeprefix("/api/research/").split("/")
                if len(parts) in (2, 3) and (record := research.load(DATA, *parts[:2])) is not None:
                    evidence = research.evidence_file(DATA, record)
                    if len(parts) == 2:
                        filename = f"{record['id']}.json" if query.get("download") == ["1"] else None
                        return self.send_json({**record, "evidence_download": evidence is not None}, filename=filename)
                    if parts[2] == "evidence.gz" and evidence is not None:
                        return self.send_gzip_attachment(evidence, f"{record['id']}-evidence.json.gz")
            if path == "/api/runs":
                return self.send_json(paper_runs.list_runs(DATA, "fixture" if fixture else "live"))
            if path == "/api/backtests":
                return self.send_json({
                    "runs": paper_runs.list_runs(backtest.folder(DATA, fixture), "fixture" if fixture else "live"),
                    "captures": [] if fixture else capture_index.recent_sessions(DATA / "captures"),
                    "max_days": backtest.MAX_DAYS,
                })
            if path.startswith("/api/backtests/"):
                run = paper_runs.load_run(backtest.folder(DATA, fixture), path.removeprefix("/api/backtests/"))
                offset = max(0, int(query.get("offset", [0])[0]))
                case = int(query["case"][0]) if "case" in query else None
                return self.send_json(backtest.describe(run, offset, case))
            if path.startswith("/api/runs/"):
                return self.send_json(paper_runs.describe_run(paper_runs.load_run(DATA, path.removeprefix("/api/runs/"))))
            if path == "/api/paper":
                at, size, reaction = _paper_settings(query["at"][0], query.get("size", ["25"])[0],
                                                     query.get("reaction", ["5"])[0])
                browser = query.get("compact") == ["1"]
                return self.send_json(paper_data(
                    fixture, at, size, reaction, include_trial=query.get("trial") != ["0"], compact=browser,
                    freshness=None if fixture else PAPER_FRESHNESS, historical=query.get("live") != ["1"],
                ))
            for prefix, directory in (("/api/grid/", GRID), ("/api/cloud/", CLOUDS), ("/api/wind/", WIND),
                                      ("/api/precipitation/", PRECIPITATION), ("/api/wave/", WAVES), ("/api/radar/", RADAR)):
                if path.startswith(prefix) and FRAME_ID.fullmatch(path.removeprefix(prefix)):
                    data = read_json(directory / (path.removeprefix(prefix) + ".json"))
                    if data is not None:
                        return self.send_json(data)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            return self.send_json({"error": str(exc)}, 400)
        return self.send_json({"error": "Not found"}, 404)

    def do_POST(self):
        if not self.local_write():
            return self.send_json({"error": "Use controls on the local Weather Edge page"}, 403)
        try:
            request = self.read_json_object()
            path = urlsplit(self.path).path
            if path == "/api/trade/practice":
                return self.send_json(trade_orders.practice())
            if path == "/api/backtests":
                predict = None if request.get("fixture") is True else backtest_probability_lookup()
                run = backtest.save(DATA, request, predict, datetime.now(UTC))
                return self.send_json(backtest.describe(run))
            if path == "/api/collection":
                if request.get("action") == "stop":
                    return self.send_json(collector.stop(DATA))
                elif request.get("action") == "start":
                    return self.send_json(collector.start(DATA, explicit=True))
                else:
                    raise ValueError("Unknown collector control")
            if path == "/api/runs":
                fixture = request.get("fixture") is True
                capture = current_capture(fixture, include_low=True)
                if not capture:
                    raise ValueError("No captured book data to save")
                at, size, reaction = _paper_settings(request["at"], request["size"], request["reaction"])
                if not fixture:
                    capture = strategy_native_capture.with_listing_evidence(DATA, capture, min(at, stamp(capture["captured_at"])))
                lookup = trial_probability_lookup(active_model()) if fixture else HistoricalModels()
                run = paper_runs.create_run(capture, lookup,
                                            as_of=at, order_size=size, reaction_seconds=reaction,
                                            freshness=None if fixture else PAPER_FRESHNESS)
                return self.send_json(paper_runs.describe_run(paper_runs.store_run(DATA, run)))
            if path.endswith("/evaluate"):
                parts = path.strip("/").split("/")
                if len(parts) == 4 and parts[0] == "api" and parts[1] in {"runs", "backtests"}:
                    fixture = request.get("fixture") is True
                    root = backtest.folder(DATA, fixture) if parts[1] == "backtests" else DATA
                    original = paper_runs.load_run(root, parts[2])
                    child = paper_runs.store_run(root, paper_runs.evaluate_old(original))
                    return self.send_json(backtest.describe(child) if parts[1] == "backtests"
                                          else paper_runs.describe_run(child))
            if path == "/api/trade/orders":
                fixture = request.get("fixture") is True
                selection = _trade_selection(request)
                size = trade.order_size(request["budget"])
                request_id = trade_orders.validate_request_id(request["request_id"])
                review_key = request.get("review_key")
                prior = trade_orders.existing(DATA, fixture, request_id, selection, size, review_key)
                if prior:
                    return self.send_json(prior)
                ticket, witness = _trade_ticket(fixture, {}, selection, size)
                if not ticket or ticket["blocker"] or review_key != ticket["review_key"]:
                    # Another identical submission can commit while the quote
                    # is being loaded or expiring.
                    prior = trade_orders.existing(DATA, fixture, request_id, selection, size, review_key)
                    if prior:
                        return self.send_json(prior)
                    return self.send_json({"error": "The quote or its evidence changed. Review the refreshed ticket."}, 409)
                record = trade_orders.record(DATA, fixture, request_id, ticket, witness)
                if wake := getattr(self.server, "wake_paper_reconciler", None):
                    wake()
                return self.send_json(record)
            if path == "/api/trade/book":
                venue, instrument, side = _trade_selection(request)
                if request.get("fixture") or collector.paused(DATA):
                    raise ValueError("Public refresh requires live collection")
                now = datetime.now(UTC)
                market = trade._catalog(DATA, now, (venue, instrument, side)).get((venue, instrument))
                if (not market or market["status"] != "open" or not market["opened_at"] or not market["closed_at"]
                        or trade._open_status(None, None, market, now)):
                    raise ValueError("Select an open market with a retained native listing")
                sample = venue_sources.sample_kalshi_books if venue == venue_archive.KALSHI else venue_sources.watch_poly_book_ids
                return self.send_json({"received": sample(DATA, [instrument], scope=venue_archive.MANUAL_BOOK)})
            if path.startswith("/api/runs/") and path.endswith("/payouts"):
                run = paper_runs.load_run(DATA, path.removeprefix("/api/runs/").removesuffix("/payouts"))
                latest = current_capture(run["inputs"]["evidence"]["kind"] == "fixture", include_low=True)
                if not latest:
                    raise ValueError("No later capture is available")
                child = paper_runs.follow_payouts(run, latest)
                return self.send_json(paper_runs.describe_run(paper_runs.store_run(DATA, child)))
        except trade_orders.RequestConflict as exc:
            return self.send_json({"error": str(exc)}, 409)
        except (ValueError, KeyError, TypeError, OSError, TimeoutError) as exc:
            return self.send_json({"error": str(exc)}, 400)
        return self.send_json({"error": "Not found"}, 404)


def main():
    global DATA, GRID, CLOUDS, WIND, PRECIPITATION, WAVES, RADAR
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-collect", action="store_true", help="Open the cached app without starting the collector")
    parser.add_argument("--data", type=Path, help="Use an isolated local data root")
    args = parser.parse_args()
    if args.data is not None:
        DATA = args.data.resolve()
        GRID, CLOUDS, WIND, PRECIPITATION, WAVES, RADAR = (
            DATA / name for name in ("grids", "clouds", "wind", "precipitation", "waves", "radar")
        )
    if not (BUILD / "index.html").is_file():
        parser.exit(1, "Run npm ci and npm run build first.\n")
    mimetypes.add_type("text/javascript", ".js")
    try:
        observe_model_artifacts()
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"Could not record model availability: {type(error).__name__}", flush=True)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), UIHandler)
    print(f"Weather Edge is at http://127.0.0.1:{server.server_port}/", flush=True)
    logging.basicConfig()
    paper_reconciler.LOG.setLevel(logging.INFO)
    with server, paper_reconciler.running(
        DATA, lambda: datetime.now(UTC), lambda: current_capture(True), paper_order_probability,
    ) as wake:
        server.wake_paper_reconciler = wake
        if not args.no_collect:
            collector.start(DATA)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
