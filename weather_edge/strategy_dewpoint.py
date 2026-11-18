"""Test observed dew-point depression on the physical daily-high model."""

import argparse
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
import gzip
from hashlib import sha256
import json
from pathlib import Path
import re
from statistics import mean

from sklearn import __version__ as sklearn_version
from sklearn.feature_extraction import DictVectorizer
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from . import expansion_metar, expansion_nbm, expansion_weather, research_model, strategy_high_transfer, venue_archive
from .history import _write
from .market import iso, parse_time
from .model import MIN_BAND_DAYS, MIN_TAIL_SUPPORT_DAYS, _benchmark_buckets, _limits, _outcome, _probability
from .nbm import climate_window, nbm_url
from .strategy_metar_high import disagreement_reserve, guarded_probability, metar_high_evidence
from .strategy_runs import digest, json_bytes, write_new


VERSION = "observed-dewpoint-high-v1"
PHASES = {"fit": ("2026-09-28", "2026-10-16"), "calibration": ("2026-10-17", "2026-10-22"),
          "later test": ("2026-10-28", "2026-11-13")}
WARM_PHASES = {"fit": ("2026-08-24", "2026-09-09"), "calibration": ("2026-09-11", "2026-09-16"),
               "later test": ("2026-09-18", "2026-09-27")}
MODELS = "Raw NOAA", "Physical Ridge", "Physical Ridge with dew point"
MAX_OBSERVATION_AGE = timedelta(minutes=80)
PRECISION_PAIR = re.compile(r"(?<!\S)T([01])(\d{3})([01])(\d{3})(?=\s|$)")
RUNTIME = Path("research/runtime/daily-high-dewpoint")
RUNTIME_VERSION = "daily-high-dew-point-shadow-v1"


def precise_pair(row, station, start, decision):
    raw = (row.get("raw_report") or row.get("report") or row.get("raw") or "").strip().rstrip("=")
    ident = expansion_metar.REPORT_START.match(raw)
    if not ident or ident[1] != station:
        return None
    stamp, receipt = map(parse_time, (row["at"], row["available_at"]))
    if (not start <= stamp <= receipt <= decision or decision - stamp > MAX_OBSERVATION_AGE
            or stamp.strftime("%d%H%M") != ident[2]):
        return None
    pair = PRECISION_PAIR.search(raw.partition(" RMK ")[2])
    if not pair:
        return None
    temp, dew = [int(pair[index + 1]) / 10 * (-1 if pair[index] == "1" else 1) for index in (1, 3)]
    if not -60 <= dew <= temp <= 70:
        return None
    return {"temperature_c": temp, "dewpoint_c": dew, "depression_f": 1.8 * (temp - dew),
            "at": row["at"], "available_at": row["available_at"], "raw_report": raw, "source_url": row.get("source_url")}


def load_observations(data, sources):
    output = defaultdict(list)
    for station, meta in sources["raw_observation_archives"].items():
        path = data / meta["observation_file"]
        if sha256(path.read_bytes()).hexdigest() != meta["sha256"]:
            raise ValueError(f"Original {station} ASOS archive checksum changed")
        first, last = map(date.fromisoformat, (path.name[5:15], path.name[16:26]))
        offset = expansion_weather.STATIONS[station][3]
        for row in expansion_weather.observed_data(data, station, first, last):
            day = (parse_time(row["at"]) + timedelta(hours=offset)).date().isoformat()
            output[station, day].append(row)
    return output


def phase_rows(data, previous, phases=PHASES):
    prior, source = strategy_high_transfer._archived(data, previous)
    observations = load_observations(data, source["sources"])
    groups, withheld = defaultdict(list), Counter()
    for row in [*source["calibration"], *source["test"]]:
        station, day = row["station"], row["date"]
        offset = expansion_weather.STATIONS[station][3]
        start, _ = climate_window(day, offset)
        group = next((name for name, (first, last) in phases.items() if first <= day <= last), None)
        if group is None:
            continue
        if group != "later test":
            next_group = "calibration" if group == "fit" else "later test"
            lock = climate_window(phases[next_group][0], offset)[0]
            report = row["official_report"]
            if parse_time(report.get("at") or report["published_at"]) >= lock:
                withheld[group, "label published after the next window began"] += 1
                continue
        candidates = observations[station, day]
        at = parse_time(row["at"])
        chosen = next((evidence for obs in reversed(candidates) if (evidence := precise_pair(obs, station, start, at))), None)
        if not chosen:
            withheld[group, "exact same-station dew-point report unavailable"] += 1
            continue
        groups[group].append({**row, "humidity": chosen})
    if any(not groups[phase] for phase in phases):
        raise ValueError("Each independent date window needs exact paired temperature and dew-point reports")
    return prior, groups, {"/".join(key): value for key, value in withheld.items()}


def predictors(row, name):
    observed = row["all_observations_bound"]["lower_f"]
    values = {"station": row["station"], "clock": str(row["local_hour"]), "physical_high_f": row["physical_with_six_f"],
              "expected_warming_f": max(0, row["expected_remaining_max_f"] - observed),
              "raw_minus_physical_f": row["raw_f"] - row["physical_with_six_f"]}
    if name == MODELS[2]:
        values["dewpoint_depression_f"] = row["humidity"]["depression_f"]
    return values


def fit(cohorts):
    training, cal = cohorts["fit"], cohorts["calibration"]
    errors = [row["actual_f"] - row["physical_with_six_f"] for row in training]
    models, residuals, terms = {}, defaultdict(list), {}
    for name in MODELS[1:]:
        model = make_pipeline(DictVectorizer(sparse=False), StandardScaler(), Ridge(alpha=5))
        model.fit([predictors(row, name) for row in training], errors)
        models[name] = model
        columns = model.named_steps["dictvectorizer"].get_feature_names_out()
        scales = model.named_steps["standardscaler"].scale_
        terms[name] = {key: float(value / scale) for key, value, scale in
                       zip(columns, model.named_steps["ridge"].coef_, scales, strict=True)}
        for row, correction in zip(cal, model.predict([predictors(row, name) for row in cal]), strict=True):
            residuals[name, row["local_hour"]].append(float(row["actual_f"] - row["physical_with_six_f"] - correction))
    return models, residuals, terms


def evaluation(rows, models, residuals, reserve, market=None):
    result = []
    for row in rows:
        if market is None:
            buckets = _benchmark_buckets(round(row["raw_f"]))
        else:
            buckets = market.get((row["station"], row["date"]))
            if not buckets or any(_outcome(b, row["actual_f"]) != b["payout"] for b in buckets):
                continue
        lower = row["all_observations_bound"]["lower_f"]
        estimates = {}
        for name in MODELS:
            if name == MODELS[0]:
                point = row["raw_f"]
                p = [_probability(_limits(b), [point], row["raw_sigma"]) for b in buckets]
            else:
                remaining = residuals[name, row["local_hour"]]
                center = row["physical_with_six_f"] + float(models[name].predict([predictors(row, name)])[0])
                centers = [center + value for value in remaining]
                point = max(lower, mean(centers))
                p = [guarded_probability(_limits(b), centers, 0.5, lower, reserve["upper_one_sided_wilson"]) for b in buckets]
            errors = [(value - _outcome(b, row["actual_f"])) ** 2 for value, b in zip(p, buckets, strict=True)]
            estimates[name] = {"high_f": point, "error_f": point - row["actual_f"], "brier": mean(errors), "probabilities": p}
        result.append({**row, "buckets": buckets, "estimates": estimates})
    return result


def metrics(rows, target):
    for clock in (None, *strategy_high_transfer.HOURS):
        matched = [row for row in rows if clock is None or row["local_hour"] == clock]
        if not matched:
            continue
        for name in MODELS:
            daily = defaultdict(list)
            for row in matched:
                daily[row["date"]].append(row["estimates"][name])
            yield {"target": target, "local_standard_hour": clock if clock is not None else "all", "model": name,
                   "calendar_dates": len(daily), "station_days": len({(r["station"], r["date"]) for r in matched}),
                   "frames": len(matched), "buckets": sum(len(row["buckets"]) for row in matched),
                   "brier": mean(mean(e["brier"] for e in day) for day in daily.values()),
                   "mae_f": mean(mean(abs(e["error_f"]) for e in day) for day in daily.values())}


def research(data, source, phases=PHASES):
    prior, cohorts, withheld = phase_rows(data, source, phases)
    models, residuals, terms = fit(cohorts)
    reserve = disagreement_reserve(cohorts["calibration"])
    test = cohorts["later test"]
    splits = {"Official daily high": evaluation(test, models, residuals, reserve)}
    payouts = strategy_high_transfer.native_buckets(data, test, window=tuple(map(date.fromisoformat, phases["later test"])))
    for venue in venue_archive.VENUES:
        scopes = {(station, day): bucket for (v, station, day), bucket in payouts.items() if v == venue}
        if scopes:
            splits[venue] = evaluation(test, models, residuals, reserve, scopes)

    metrics_rows = [item for target, rows in splits.items() for item in metrics(rows, target)]
    comparison = {target: [research_model.date_bootstrap(rows, MODELS[1], MODELS[2], metric) for metric in ("brier", "mae")]
                  for target, rows in splits.items() if rows}
    actual = [(venue, row) for venue in venue_archive.VENUES for row in splits.get(venue, [])]
    actual.sort(key=lambda pair: pair[1]["estimates"][MODELS[2]]["brier"] - pair[1]["estimates"][MODELS[1]]["brier"])
    cases = []
    for venue, row in ([actual[0], actual[-1]] if actual else []):
        estimate = row["estimates"]
        bucket = next(b for b in row["buckets"] if b["payout"])
        cases.append({"station": row["station"], "date": row["date"], "at": row["at"], "venue": venue,
                      "instrument_id": bucket["instrument_id"], "title": f"{row['station']} dew-point depression",
                      "detail": f"Observed temperature–dew point difference {row['humidity']['depression_f']:.1f}°F. Physical Ridge {estimate[MODELS[1]]['high_f']:.1f}°F, with dew point {estimate[MODELS[2]]['high_f']:.1f}°F, official {row['actual_f']:.0f}°F.",
                      "data": row})
    record = {"version": VERSION, "title": "Observed dew point and the rest of the daily high",
              "hypothesis": "The exact station's latest dew-point depression improves remaining daily-high calibration after temperature and forecast controls.",
              "target": "Official high temperatures and their matching native paid buckets at 13:00 and 17:00 local standard time.",
              "venues": list(venue_archive.VENUES), "date_window": {**phases, "from": phases["fit"][0], "through": phases["later test"][1]},
              "metrics": metrics_rows, "cases": cases, "comparison": comparison,
              "limits": ["Historical METAR report time plus ten minutes is an availability assumption used only for weather research.",
                         "Both Ridge arms use the same observations and earlier settings. No decision is simulated from price history.",
                         "The native scores retain stations and dates whose official highs reproduce the archived venue payout.",
                         "Calendar dates are the independent uncertainty unit across stations and decision hours."],
              "provenance": {"source_study": prior["id"], "source_detail_sha256": prior["provenance"]["replay_details_sha256"],
                             "row_counts": {key: len(rows) for key, rows in cohorts.items()}, "withheld": withheld,
                             "standardized_ridge_alpha": 5, "sklearn_version": sklearn_version, "original_units": "tenths °C",
                             "source_metar_format": "https://aviationweather.gov/help/data/", "guard_reserve": reserve,
                             "implementation_sha256": sha256(Path(__file__).read_bytes()).hexdigest()}}
    details = {"training_inputs": cohorts["fit"], "calibration_inputs": cohorts["calibration"], "heldout": splits,
               "coefficients_f_per_input_unit": terms, "calibration_residuals": {str(key): r for key, r in residuals.items()}}
    return research_model.write_record(data, record, details)


def freeze_runtime(data, study):
    record = json.loads(study.read_bytes())
    details_ref = record["provenance"]
    raw = (data / details_ref["replay_details_path"]).read_bytes()
    if (record.get("version") != VERSION or sha256(raw).hexdigest() != details_ref["replay_details_sha256"]
            or record["date_window"]["later test"] != list(PHASES["later test"])):
        raise ValueError("The dew-point shadow requires the checksum-verified current cool-season study")
    evidence = json.loads(gzip.decompress(raw))
    models, residuals, coefficients = fit({"fit": evidence["training_inputs"], "calibration": evidence["calibration_inputs"]})
    selected = MODELS[2]
    term = coefficients[selected]
    sources = evidence["training_inputs"]
    ridge = models[selected].named_steps
    columns = ridge["dictvectorizer"].get_feature_names_out()
    averages = ridge["standardscaler"].mean_
    intercept = float(ridge["ridge"].intercept_ - sum(term[key] * value for key, value in zip(columns, averages, strict=True)))
    model = {"version": RUNTIME_VERSION, "study": record["id"], "study_created_at": record["created_at"],
             "stations": sorted({row["station"] for row in sources}), "fit_dates": PHASES["fit"], "calibration_dates": PHASES["calibration"],
             "coefficients": term, "intercept": intercept,
             "residuals": {str(hour): residuals[selected, hour] for hour in strategy_high_transfer.HOURS},
             "calibration_days": len({row["date"] for row in evidence["calibration_inputs"]}),
             "report_reserve": details_ref["guard_reserve"]["upper_one_sided_wilson"]}
    vintage = digest(model)
    path = data / RUNTIME / f"{vintage}.json"
    write_new(path, json_bytes(model) + b"\n")
    _write(data / RUNTIME / "active.json", json_bytes({"vintage": vintage}))
    return vintage


def load_runtime(data, *, vintage=None):
    data = Path(data)
    root = data / RUNTIME
    if vintage is None and (root / "active.json").is_file():
        vintage = json.loads((root / "active.json").read_bytes())["vintage"]
    if not isinstance(vintage, str) or re.fullmatch(r"[0-9a-f]{64}", vintage) is None:
        return None
    path = root / f"{vintage}.json"
    if not path.is_file():
        return None
    obj = json.loads(path.read_bytes())
    if obj["version"] != RUNTIME_VERSION or digest(obj) != vintage:
        raise ValueError("Dew-point shadow artifact checksum mismatch")
    if "study_created_at" not in obj:
        obj["study_created_at"] = obj.pop("approved_from")
    return {**obj, "vintage": vintage, "data": data}


def _live_nbs(artifact, station, day, anchor):
    valid = datetime.combine(day + timedelta(days=1), time.min, timezone.utc)
    for hour in (18, 12):
        issued = datetime.combine(day, time(hour), timezone.utc)
        card = expansion_nbm.load_card(artifact["data"], station, issued)
        if not card or card.get("source") != nbm_url(day, hour, "nbs") or not card.get("received_at"):
            continue
        if not issued <= parse_time(card["available_at"]) <= parse_time(card["received_at"]) <= anchor:
            continue
        daily = next((p for p in card["points"] if parse_time(p["valid_at"]) == valid
                      and p.get("TXN") is not None and p.get("XND", 0) > 0), None)
        if daily:
            return card, daily
    return None


def predict_runtime(artifact, event, as_of):
    station = event.get("station")
    if artifact is None or event.get("family", "daily-high") != "daily-high" or station not in artifact["stations"]:
        return None
    day = date.fromisoformat(event["date"])
    start, end = climate_window(day, expansion_weather.STATIONS[station][3])
    at = parse_time(as_of)
    if not start <= at < end or (start, end) != tuple(parse_time(event[key]) for key in ("climate_starts_at", "climate_ends_at")):
        return None
    for hour in reversed(strategy_high_transfer.HOURS):
        anchor = start + timedelta(hours=hour)
        if parse_time(artifact["study_created_at"]) <= anchor <= at:
            if estimate := _live_at(artifact, event, day, start, end, anchor, hour):
                return {**estimate, "at": iso(at)}
    return None


def _live_at(artifact, event, day, start, end, anchor, hour):
    station = event["station"]
    valid = [row for row in event.get("observations", []) if row.get("source") in {"NWS", "Aviation Weather Center"}
             and not row.get("timing_proxy")]
    witnesses = [w for row in valid for w in metar_high_evidence(row, station, start, anchor)]
    paired = [w for row in valid if (w := precise_pair(row, station, start, anchor))]
    remaining = strategy_high_transfer._live_remaining(artifact, event, anchor, end)
    nbs = _live_nbs(artifact, station, day, anchor)
    if not (remaining and nbs and witnesses and paired):
        return None
    witness = max(witnesses, key=lambda row: (row["lower_f"], -parse_time(row["available_at"]).timestamp()))
    humidity = max(paired, key=lambda row: (parse_time(row["at"]), -parse_time(row["available_at"]).timestamp()))
    card, remainder = remaining
    short, daily = nbs
    raw = daily["TXN"]
    known_f = witness["lower_f"]
    physical = max(remainder, known_f)
    terms = {f"station={station}": 1, f"clock={hour}": 1, "physical_high_f": physical,
             "expected_warming_f": max(0, remainder - known_f), "raw_minus_physical_f": raw - physical,
             "dewpoint_depression_f": humidity["depression_f"]}
    center = physical + artifact["intercept"] + sum(coef * terms.get(key, 0) for key, coef in artifact["coefficients"].items())
    samples = [center + error for error in artifact["residuals"][str(hour)]]
    reserve = artifact["report_reserve"]
    n = artifact["calibration_days"]
    probabilities = []
    for bucket in event.get("buckets", []):
        bounds = _limits(bucket)
        yes = guarded_probability(bounds, samples, 0.5, known_f, reserve)
        count = sum(bounds[0] <= value < bounds[1] for value in samples)
        other = len(samples) - count
        probabilities.append({"id": bucket["id"], "probability": yes, "no_probability": 1 - yes,
                              "validation_days": n, "validation_residuals": len(samples), "yes_tail_support": count,
                              "no_tail_support": other, "uncertain": n < MIN_BAND_DAYS or min(count, other) < MIN_TAIL_SUPPORT_DAYS})
    return {"version": RUNTIME_VERSION, "vintage": artifact["vintage"], "mode": "shadow", "method": "physical-ridge-dew-point",
            "target": "daily-high", "knowledge_at": iso(anchor), "local_standard_decision_hour": hour,
            "model_high_f": max(known_f, mean(samples)), "available_at": max(witness["available_at"], humidity["available_at"],
                short["received_at"], card["available_at"], key=parse_time), "issued_at": card["issued_at"], "buckets": probabilities,
            "context": {"temperature_witness": witness, "dewpoint_observation": humidity, "expected_remaining_max_f": remainder,
                        "raw_high_f": raw, "physical_before_correction_f": physical, "climate_report_disagreement_reserve": reserve,
                        "forecast": {"remaining": {"source": card["hourly_source"], "issued_at": card["issued_at"],
                                                     "available_at": card["available_at"]},
                                     "short_range": {"source": short["source"], "issued_at": short["issued_at"],
                                                     "available_at": short["received_at"], "published_at": short["available_at"],
                                                     "card_sha256": short["card_sha256"]}}}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=venue_archive.DEFAULT_DATA)
    parser.add_argument("--warm", action="store_true", help="Use the locked warmer date split")
    parser.add_argument("--freeze-runtime", type=Path, help="Prepare a separate shadow using qualified cool-season evidence")
    parser.add_argument("prior_transfer_record", type=Path, nargs="?")
    args = parser.parse_args()
    if args.freeze_runtime:
        print(freeze_runtime(args.data, args.freeze_runtime))
        return
    if args.prior_transfer_record is None:
        parser.error("Provide the exact prior station-transfer record")
    path = research(args.data, args.prior_transfer_record, WARM_PHASES if args.warm else PHASES)
    result = json.loads(path.read_bytes())
    print(json.dumps({"path": str(path), "metrics": result["metrics"], "comparison": result["comparison"],
                      "provenance": result["provenance"]}, indent=2))


if __name__ == "__main__":
    main()
