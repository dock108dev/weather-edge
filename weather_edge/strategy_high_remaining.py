"""Compare an observed-plus-remaining daily high with NOAA's full-day TXN."""

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta, timezone, datetime
import gzip
from hashlib import sha256
import json
from pathlib import Path
import statistics
from types import SimpleNamespace
from uuid import uuid4

from sklearn.feature_extraction import DictVectorizer
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline

from . import archive, history, study
from .market import iso, parse_time
from .model import (
    MIN_BAND_DAYS, MIN_TAIL_SUPPORT_DAYS, _benchmark_buckets, _limits, _observed_temperatures,
    _outcome, _probability, _snapshot,
)
from .nbm import climate_window
from .strategy_market import event_inputs
from .strategy_runs import DEFAULT_DATA, POLYMARKET, digest, json_bytes, write_new


ENGINE = "remaining-day-high-v1"
HOURS = (9, 13, 17, 21)
STATIONS = study.STATIONS
RIDGE_ALPHA = 10
METHODS = ("NOAA TXN native spread", "NOAA TXN historical residuals", "Observed plus remaining NOAA NBH", "Ridge correction to physical high")
RUNTIME_FAMILY = "daily-high-remaining"
RUNTIME_ENGINE = "remaining-day-high-shadow-v1"


def official(data, station, day, label):
    product = label["product"]
    source = f"{history.IEM}/api/1/nwstext/{product}"
    live_path = data / "archive-raw" / "cli" / station / f"{product}.txt"
    study_path = data / "research" / "evidence" / "high-physical" / "raw-cli" / station / f"{product}.txt"
    raw = history._fetch(live_path if live_path.is_file() else study_path, source)
    local_day = date.fromisoformat(day)
    _, end = climate_window(local_day, STATIONS[station])
    report = archive._parse_cli(raw.decode(), product, SimpleNamespace(day=local_day), end)
    if report is None:
        raise ValueError("Original NWS climate product lacks a complete station-date maximum")
    return {**report, "raw_sha256": sha256(raw).hexdigest(), "summary_high_f": label["high_f"],
            "summary_mismatch": report["high_f"] != label["high_f"]}


def day_context(station, day, forecasts, observations, at):
    start, end = climate_window(day, STATIONS[station])
    primary = _snapshot(station=station, day=day, as_of=at, forecasts=forecasts, observations=observations,
                        standard_utc_offset_hours=STATIONS[station])
    if primary is None:
        return None, "No eligible as-issued NOAA TXN high"
    known = _observed_temperatures(observations, start, end, at)
    if not known:
        return None, "No station reading as of decision"
    next_hour = at.replace(minute=0, second=0, microsecond=0)
    if next_hour < at:
        next_hour += timedelta(hours=1)
    hours = {next_hour + timedelta(hours=i) for i in range(int((end - next_hour).total_seconds() // 3600))}
    candidate_cards = sorted(
        [fc for fc in forecasts if fc.get("hourly_source") and parse_time(fc["issued_at"]) <= parse_time(fc["available_at"]) <= at],
        key=lambda c: (parse_time(c["issued_at"]), parse_time(c["available_at"])), reverse=True,
    )
    for card in candidate_cards:
        temperatures = {parse_time(x["valid_at"]): float(x["temp_f"]) for x in card["hourly"]}
        if hours and hours <= temperatures.keys():
            future_maximum = max(temperatures[hour] for hour in hours)
            observed_maximum = max(temp for _, temp in known)
            return {
                "station": station, "date": day, "at": iso(at),
                "local_hour": (at - start).total_seconds() / 3600,
                "raw_f": primary["noaa_high_f"], "raw_sd_f": primary["noaa_sd_f"],
                "expected_remaining_max_f": future_maximum,
                "measured_max_f": observed_maximum, "physical_f": max(future_maximum, observed_maximum),
                "observations": len(known), "hours_left": len(hours),
                "forecast": {"raw": {k: primary["forecast"].get(k) for k in ("issued_at", "available_at", "high_source", "source")},
                             "remaining": {k: card.get(k) for k in ("issued_at", "available_at", "hourly_source")}},
            }, None
    return None, "No eligible NBH issue covering the full remaining climate day"


def official_labels(data, weather):
    jobs = []
    dates = {str(day) for name in ("fit", "calibration", "selection", "test")
             for day in archive.date_range(*study.WINDOWS[name]) if str(day) >= "2026-08-01"}
    for station, lane in weather["stations"].items():
        jobs.extend((station, day, label) for day, label in lane["labels"].items() if day in dates)

    def get(job):
        station, day, label = job
        try:
            return (station, day), official(data, station, day, label), None
        except (KeyError, OSError, ValueError) as error:
            return (station, day), None, str(error)

    missing, prepared = {}, {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        for key, report, error in pool.map(get, jobs):
            if report is not None:
                prepared[key] = report
            else:
                missing["/".join(key)] = error
    return prepared, missing


def dataset(weather, labels):
    rows = {}
    omissions = Counter()
    for split in ("fit", "calibration", "selection", "test"):
        first, last = (str(day) for day in study.WINDOWS[split])
        next_phase = study._split_label_cutoff(split)
        rows[split] = []
        for station, lane in weather["stations"].items():
            offset = STATIONS[station]
            by_day = defaultdict(list)
            for read in lane["observations"]:
                at = parse_time(read["at"])
                by_day[str((at + timedelta(hours=offset)).date())].append(read)
            for day, guidance in lane["forecasts"].items():
                if day < "2026-08-01" or not first <= day <= last:
                    continue
                label = labels.get((station, day))
                if not label or (next_phase is not None and parse_time(label["available_at"]) >= next_phase):
                    omissions[f"{split}/no timely raw climate maximum"] += 1
                    continue
                for hour in HOURS:
                    start, _ = climate_window(day, offset)
                    at = start + timedelta(hours=hour)
                    picked, reason = day_context(station, day, guidance, by_day[day], at)
                    if picked:
                        rows[split].append({**picked, "label": label, "actual_f": label["high_f"]})
                    else:
                        omissions[f"{split}/{reason}"] += 1
    return rows, dict(omissions)


def features(row):
    return {"station": row["station"], "raw_f": row["raw_f"], "physical_f": row["physical_f"],
            "measured_max_f": row["measured_max_f"], "relative_heating_f": row["expected_remaining_max_f"] - row["measured_max_f"],
            "local_hour": row["local_hour"], "hours_left": row["hours_left"]}


def train(rows):
    model = make_pipeline(DictVectorizer(), Ridge(alpha=RIDGE_ALPHA))
    counts = Counter((r["station"], r["date"]) for r in rows["fit"])
    model.fit([features(r) for r in rows["fit"]], [r["actual_f"] - r["physical_f"] for r in rows["fit"]],
              ridge__sample_weight=[1 / counts[r["station"], r["date"]] for r in rows["fit"]])

    def means(row):
        ridge = max(row["measured_max_f"], row["physical_f"] + float(model.predict([features(row)])[0]))
        return dict(zip(METHODS, (row["raw_f"], row["raw_f"], row["physical_f"], ridge), strict=True))

    calibration = defaultdict(list)
    for row in rows["calibration"]:
        for method, mean in means(row).items():
            calibration[method, row["station"], row["local_hour"]].append(row["actual_f"] - mean)

    def forecast(row, bounds):
        expected = means(row)
        output = {}
        for method, mean in expected.items():
            errors = calibration[method, row["station"], row["local_hour"]]
            if not errors:
                continue
            if method == "NOAA TXN native spread" and row.get("raw_sd_f") and row["raw_sd_f"] > 0:
                centers, scale = [mean], row["raw_sd_f"]
            else:
                centers, scale = [mean + error for error in errors], 0.5
            output[method] = {
                "mean_f": mean, "validation_station_days": len(errors),
                "probs": [_probability(_limits(bucket), centers, scale) for bucket in bounds],
            }
        return output

    return forecast


def measure(rows, forecast, bucket_sets=None):
    measurements = []
    for row in rows:
        selected = _benchmark_buckets(round(row["raw_f"])) if bucket_sets is None else bucket_sets.get((row["station"], row["date"]))
        if not selected:
            continue
        estimates = forecast(row, selected)
        if len(estimates) != len(METHODS):
            continue
        outcomes = [_outcome(bucket, row["actual_f"]) for bucket in selected]
        for method, prediction in estimates.items():
            measurements.append({
                "method": method, "station": row["station"], "date": row["date"], "at": row["at"], "hour": row["local_hour"],
                "actual_f": row["actual_f"], "expected_f": prediction["mean_f"], "bucket_count": len(selected),
                "brier": statistics.mean((p - y) ** 2 for p, y in zip(prediction["probs"], outcomes, strict=True)),
                "probs": prediction["probs"], "outcomes": outcomes, "forecast": row["forecast"], "label": row["label"],
                "measured_max_f": row["measured_max_f"], "expected_remaining_max_f": row["expected_remaining_max_f"],
                "buckets": selected,
            })
    return measurements


def summary(rows, group):
    output = []
    for method in METHODS:
        values = [r for r in rows if r["method"] == method]
        if not values:
            continue
        output.append({
            "group": group, "method": method, "station_days": len({(r["station"], r["date"]) for r in values}),
            "dates": len({r["date"] for r in values}), "decisions": len(values),
            "bucket_cases": sum(r["bucket_count"] for r in values),
            "mae_f": statistics.mean(abs(r["expected_f"] - r["actual_f"]) for r in values),
            "bucket_brier": statistics.mean(r["brier"] for r in values),
        })
    return output


def actual_contracts(data, labels):
    verified = {}
    for _, _, supplied in event_inputs(data):
        event_key = supplied["station"], supplied["date"]
        actual = labels.get(event_key)
        buckets = supplied.get("buckets")
        if not actual or not buckets or study._partition(buckets) != "exhaustive":
            continue
        if any(b["result"] is None or bool(_outcome(b, actual["high_f"])) != b["result"] for b in buckets):
            continue
        verified[event_key] = [{key: b[key] for key in ("id", "label", "lower_f", "upper_f")} for b in buckets]
    return verified


def freeze_runtime(data, ident, rows):
    calibration = rows["calibration"]
    by_station = {}
    for row in calibration:
        entry = by_station.setdefault(row["station"], {}).setdefault(str(int(row["local_hour"])), [])
        entry.append(row["actual_f"] - row["physical_f"])
    model = {"engine": RUNTIME_ENGINE, "source_study": ident, "published_before": max(r["label"]["available_at"] for r in calibration),
             "remaining_residuals": by_station, "decision_hours": HOURS}
    root = data / "research" / "runtime" / RUNTIME_FAMILY
    vintage = digest(model)
    write_new(root / f"{vintage}.json", json_bytes(model) + b"\n")
    staging = root / f".{uuid4().hex}.tmp"
    staging.write_bytes(json_bytes({"vintage": vintage}) + b"\n")
    staging.replace(root / "active.json")
    return {"vintage": vintage, "source_study": ident}


def load_runtime(data, vintage=None):
    root = data / "research" / "runtime" / RUNTIME_FAMILY
    if vintage is None and (root / "active.json").is_file():
        vintage = json.loads((root / "active.json").read_text())["vintage"]
    if not isinstance(vintage, str) or len(vintage) != 64 or any(c not in "0123456789abcdef" for c in vintage):
        return None
    path = root / f"{vintage}.json"
    if not path.is_file():
        return None
    model = json.loads(path.read_text())
    if model.get("engine") != RUNTIME_ENGINE or digest(model) != vintage:
        raise ValueError("Unsupported or changed remaining-day high artifact")
    return {**model, "vintage": vintage}


def predict_runtime(model, event, as_of):
    if not model or event.get("family", "daily-high") != "daily-high":
        return None
    station = event["station"]
    if station not in model["remaining_residuals"]:
        return None
    at = parse_time(as_of)
    start, end = climate_window(event["date"], STATIONS[station])
    clocks = [hour for hour in model["decision_hours"] if start + timedelta(hours=hour) <= at < end]
    if not clocks:
        return None
    hour = max(clocks)
    lookup = str(hour)
    knowledge = start + timedelta(hours=hour)
    if knowledge < parse_time(model["published_before"]):
        return None
    observations = [row for row in event["observations"]
                    if row.get("source") in {"NWS", "Aviation Weather Center"} and not row.get("timing_proxy")]
    context, _ = day_context(station, event["date"], event["forecasts"], observations, knowledge)
    residuals = model["remaining_residuals"][station].get(lookup, [])
    if not context or not residuals:
        return None
    mean = context["physical_f"]
    centers = [mean + error for error in residuals]
    bins = []
    for bucket in event["buckets"]:
        bounds = lo, hi = _limits(bucket)
        support = sum(lo <= center < hi for center in centers)
        bins.append({
            "id": bucket["id"], "probability": _probability(bounds, centers, 0.5),
            "uncertain": len(residuals) < MIN_BAND_DAYS or min(support, len(residuals) - support) < MIN_TAIL_SUPPORT_DAYS,
            "validation_days": len(residuals), "validation_residuals": len(residuals),
            "yes_tail_support": support, "no_tail_support": len(residuals) - support,
        })
    return {
        "version": RUNTIME_ENGINE, "vintage": model["vintage"], "method": "observed-plus-remaining-nbh", "target": "daily-high",
        "at": iso(at), "knowledge_at": iso(knowledge), "available_at": context["forecast"]["remaining"]["available_at"],
        "issued_at": context["forecast"]["remaining"]["issued_at"], "local_standard_decision_hour": hour,
        "model_high_f": mean, "buckets": bins, "context": context,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", type=Path, default=DEFAULT_DATA)
    args = ap.parse_args()
    data = args.data
    input_path = study.study_dir(data) / "inputs.json.gz"
    raw = input_path.read_bytes()
    weather = json.loads(gzip.decompress(raw))
    labels, archives_missing = official_labels(data, weather)
    rows, omissions = dataset(weather, labels)
    if not all(rows.values()):
        ap.error("A fixed calendar stage has no complete matched source cases.")
    model = train(rows)
    selection = measure(rows["selection"], model)
    held_out = measure(rows["test"], model)
    matched = measure(rows["test"], model, actual_contracts(data, labels))
    highlights = []
    for station in sorted({r["station"] for r in matched}):
        selected = [r for r in matched if r["station"] == station and r["method"] == "Observed plus remaining NOAA NBH"]
        bad = max(selected, key=lambda r: abs(r["expected_f"] - r["actual_f"]))
        index = max(range(len(bad["buckets"])), key=lambda i: abs(bad["probs"][i] - bad["outcomes"][i]))
        bucket = bad["buckets"][index]
        highlights.append({
            **bad, "venue": POLYMARKET, "instrument_id": bucket["id"],
            "title": f"{station} {bad['date']} · remaining-day forecast miss",
            "detail": f"Measured maximum {bad['measured_max_f']:.1f}°F, remaining outlook {bad['expected_remaining_max_f']:.1f}°F,"
                      f" verified climate high {bad['actual_f']:.0f}°F.",
        })
    provenance = {"engine": ENGINE, "weather_input_sha256": sha256(raw).hexdigest(), "hours": HOURS,
                  "model_alpha": RIDGE_ALPHA, "windows": {k: [str(d) for d in study.WINDOWS[k]] for k in rows},
                  "observation_clocks": "IEM ASOS observation time plus fixed original 10-minute lag"}
    prepared = {"provenance": provenance, "fit_and_split_inputs": rows, "missing_raw_reports": archives_missing,
                "selected_predictions": selection, "test_predictions": held_out, "exact_payout_predictions": matched}
    ident = "high-remaining-" + digest(prepared)[:20]
    snapshot = data / "research" / "evidence" / "high-physical" / f"{ident}.json.gz"
    write_new(snapshot, gzip.compress(json_bytes(prepared), mtime=0))
    by_hour = [value for hour in HOURS for value in
               summary([r for r in held_out if r["hour"] == hour], f"{hour}:00 local standard")]
    report = {
        "id": ident, "title": "Does the measured high plus remaining guidance beat daily TXN?",
        "hypothesis": "A climate-day observed maximum paired with as-issued future hourly maximum improves held-out daily highs and bucket probabilities.",
        "target": "Daily high temperature bucket", "venues": [POLYMARKET],
        "created_at": iso(datetime.now(timezone.utc)),
        "date_window": {"from": min(r["date"] for r in rows["fit"]), "through": max(r["date"] for r in rows["test"])},
        "metrics": [*summary(selection, "selection weather"), *summary(held_out, "later-weather holdout"),
                    *summary(matched, "later exact payout buckets")],
        "local_hours": by_hour, "cases": highlights,
        "limits": [
            "Raw NWS climate text verifies each included physical label. Annual summary conflicts are retained separately.",
            "Weather-only calibration uses conditional benchmark buckets. Exact venue scores require both source and payout agreement.",
            "Historic observation receipt uses the input's fixed proxy lag. Prospective paper execution must use local observed receipts.",
            "Sparse station-days near extreme probabilities still limit reliability.",
        ],
        "evidence": {"omitted_decisions": omissions, "raw_reports_missing": len(archives_missing),
                     "raw_summary_mismatches": [f"{station}/{day}" for (station, day), row in labels.items() if row["summary_mismatch"]]},
        "provenance": {**provenance, "frozen_evidence": str(snapshot.relative_to(data))},
    }
    report["shadow_runtime"] = freeze_runtime(data, ident, rows)
    path = data / "research" / "models" / f"{ident}.json"
    write_new(path, json_bytes(report) + b"\n")
    print(json.dumps({"path": str(path), **{k: report[k] for k in ("metrics", "evidence", "local_hours")}}, indent=2))


if __name__ == "__main__":
    main()
