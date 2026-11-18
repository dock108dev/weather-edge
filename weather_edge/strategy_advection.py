"""Test a wind-aligned station residual against local-only daily-high estimates."""

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
import json
from math import asin, atan2, cos, degrees, radians, sin, sqrt
from pathlib import Path
from statistics import mean

from sklearn import __version__ as sklearn_version
from sklearn.feature_extraction import DictVectorizer
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline

from . import collector, expansion_nbm, expansion_weather, research_model, strategy_payout_timing, venue_archive
from .market import iso, parse_time
from .model import _limits, _probability
from .nbm import climate_window, nbm_url
from .strategy_rules import temperature_window
from .strategy_runs import write_new
from .study import _partition


VERSION = "northeast-advection-v1"
STATIONS = ("KNYC", "KEWR", "KTTN", "KPHL", "KBOS", "KPVD")
WINDOWS = {"fit": ("2026-10-01", "2026-10-17"), "calibration": ("2026-10-18", "2026-10-24"),
           "later test": ("2026-10-28", "2026-11-13")}
HOURS = (9, 13)
RADIUS_KM = 175
ALIGNMENT_DEGREES = 45
WIND_MIN_KNOTS = 7
NEIGHBOR_AGE = timedelta(minutes=50)
LOCAL_AGE = timedelta(minutes=75)
CARD_RANGE_HINTS = {"KPVD": (18_650_000, 19_250_000)}
MODELS = ("Raw NOAA TXN", "Local Ridge", "Local Ridge with nearby residual", "Local Ridge with upwind residual")
FEATURES = {
    MODELS[1]: ("station", "hour", "raw_high", "local_departure", "observed_minus_raw"),
    MODELS[2]: ("station", "hour", "raw_high", "local_departure", "observed_minus_raw", "nearby_departure"),
    MODELS[3]: ("station", "hour", "raw_high", "local_departure", "observed_minus_raw", "upwind_departure"),
}


def dates(start, end):
    return (start + timedelta(days=index) for index in range((end - start).days + 1))


def geography(station, other):
    """Bearing from the target toward a potential source of its surface wind."""
    a, b = expansion_weather.STATIONS[station], expansion_weather.STATIONS[other]
    lat_a, lat_b, dl = radians(a[0]), radians(b[0]), radians(b[1] - a[1])
    s = sin((lat_b - lat_a) / 2) ** 2 + cos(lat_a) * cos(lat_b) * sin(dl / 2) ** 2
    distance = 6371 * 2 * asin(min(1, sqrt(s)))
    bearing = degrees(atan2(sin(dl) * cos(lat_b), cos(lat_a) * sin(lat_b) - sin(lat_a) * cos(lat_b) * cos(dl))) % 360
    return distance, bearing


def direction_difference(left, right):
    return abs((left - right + 180) % 360 - 180)


def _card(data, station, issue, *, fetch):
    ready = expansion_nbm.load_card(data, station, issue)
    if ready is not None or not fetch:
        return ready
    if collector.paused(data):
        return None
    if station not in CARD_RANGE_HINTS:
        return expansion_nbm.load_card(data, station, issue, fetch=True)
    source = nbm_url(issue.date(), issue.hour, "nbs")
    card = expansion_nbm.fetch_station_card(source, station, issue, expansion_nbm.parse_nbs_evidence,
                                           CARD_RANGE_HINTS[station], "NOAA NBM NBS station bulletin")
    write_new(expansion_nbm.card_path(data, station, issue), json.dumps(card, allow_nan=False).encode() + b"\n")
    return card


def _read_cards(data, fetch):
    jobs = [(station, datetime.combine(day, time(cycle), timezone.utc))
            for bounds in WINDOWS.values() for day in dates(*map(date.fromisoformat, bounds))
            for station in STATIONS for cycle in (6, 12)]
    failures, cards = [], {}

    def call(job):
        station, issue = job
        try:
            return job, _card(data, station, issue, fetch=fetch), None
        except (OSError, KeyError, ValueError) as error:
            return job, None, str(error)

    with ThreadPoolExecutor(max_workers=4) as pool:
        for code, parsed, reason in pool.map(call, jobs):
            if parsed:
                cards[code] = parsed
            else:
                failures.append({"station": code[0], "cycle": iso(code[1]), "reason": reason or "No cached station bulletin"})
    return cards, failures


def _read_observations(data):
    first, end = date.fromisoformat(WINDOWS["fit"][0]) - timedelta(days=1), date(2026, 11, 17)
    stations, provenance, gaps = {}, {}, {}
    for station in STATIONS:
        existing = []
        for path in (data / "expansion" / "asos").glob(f"{station}-????-??-??-????-??-??.csv"):
            start = date.fromisoformat(path.name[5:15])
            through = date.fromisoformat(path.name[16:26])
            if start <= first and through >= date.fromisoformat(WINDOWS["later test"][1]) + timedelta(days=1):
                existing.append((start, through, path))
        start_day, end_day, path = max(existing, default=(first, end, expansion_weather.observation_file(data, station, first, end)))
        if not path.is_file() and collector.paused(data):
            gaps[station] = "No local observations and live collection is stopped"
            continue
        try:
            rows = expansion_weather.observed_data(data, station, start_day, end_day)
        except (OSError, ValueError) as error:
            gaps[station] = str(error)
            continue
        times = [parse_time(row["at"]) for row in rows]
        stations[station] = times, rows
        provenance[station] = {"path": str(path.relative_to(data)), "sha256": sha256(path.read_bytes()).hexdigest(),
                               "source": rows[0]["source_url"], "historical_availability": "ASOS report timestamp plus 10 minutes"}
    return stations, provenance, gaps


def last_observation(rows, horizon, decision, age):
    times, observed = rows
    index = bisect_right(times, horizon) - 1
    if index < 0 or horizon - times[index] > age:
        return None
    for i in range(index, -1, -1):
        if horizon - times[i] > age:
            return None
        if parse_time(observed[i]["available_at"]) <= decision:
            return observed[i]
    return None


def bulletin_value(card, target, knowledge):
    if not card or parse_time(card["available_at"]) > knowledge:
        return None
    points = [(parse_time(row["valid_at"]), row["TMP"]) for row in card["points"] if row.get("TMP") is not None]
    for (start, cold), (end, warm) in zip(points, points[1:]):
        if start <= target <= end and end - start <= timedelta(hours=3):
            return cold + ((target - start) / (end - start)) * (warm - cold)
    return None


def departure(station, card, rows, horizon, as_of, age):
    if not rows or not (r := last_observation(rows, horizon, as_of, age)):
        return None
    observed_at = parse_time(r["at"])
    expected = bulletin_value(card, observed_at, observed_at)
    if expected is None:
        return None
    return {"station": station, "observation": r, "forecast_at_observation_f": expected,
            "departure_f": r["temp_f"] - expected, "forecast_issued_at": card["issued_at"],
            "forecast_published_at": card["available_at"], "forecast_card_sha256": card["card_sha256"]}


def candidates(station, at, wind, observations, morning):
    neighbors = []
    speed = wind.get("wind_speed_knots")
    direction = wind.get("wind_direction_deg")
    for other in STATIONS:
        if other == station:
            continue
        distance, bearing = geography(station, other)
        if distance > RADIUS_KM:
            continue
        ordinary = departure(other, morning[other], observations.get(other), at, at, LOCAL_AGE)
        degrees_off = direction_difference(direction, bearing) if direction is not None and 0 < direction <= 360 else None
        minutes = distance / (1.852 * speed) * 60 if speed and speed >= WIND_MIN_KNOTS else None
        directional = None
        if minutes is not None and 10 <= minutes <= 240 and degrees_off is not None and degrees_off <= ALIGNMENT_DEGREES:
            when = at - timedelta(minutes=minutes)
            directional = departure(other, morning[other], observations.get(other), when, at, NEIGHBOR_AGE)
        neighbors.append({"station": other, "distance_km": distance, "bearing_deg": bearing,
                          "degrees_off_wind": degrees_off, "estimated_transport_minutes": minutes,
                          "ordinary": ordinary, "directional": directional})
    close = min((item for item in neighbors if item["ordinary"]), key=lambda item: item["distance_km"], default=None)
    aligned = min((item for item in neighbors if item["directional"]),
                  key=lambda item: (item["degrees_off_wind"], item["distance_km"]), default=None)
    return close, aligned


def _raw_high(cards, at):
    target = datetime.combine(at.date() + timedelta(days=1), time.min, timezone.utc)
    for card in cards:
        if not card or parse_time(card["available_at"]) > at:
            continue
        for row in card["points"]:
            if parse_time(row["valid_at"]) == target and row.get("TXN") is not None and row.get("XND", 0) > 0:
                return row["TXN"], row["XND"], card
    return None


def assemble(data, fetch):
    cards, card_gaps = _read_cards(data, fetch)
    observed, observed_inputs, observed_gaps = _read_observations(data)
    cli = strategy_payout_timing.raw_climate_reports(data)
    target_highs, label_gaps = {}, {}
    for station in STATIONS:
        try:
            target_highs[station] = expansion_weather.daily_labels(data, station, date(2026, 10, 1), date(2026, 11, 13))
        except (OSError, ValueError) as error:
            label_gaps[station] = str(error)
    rows, omitted = [], Counter()
    for phase, bounds in WINDOWS.items():
        next_phase = {"fit": "calibration", "calibration": "later test"}.get(phase)
        for day in dates(*map(date.fromisoformat, bounds)):
            early = datetime.combine(day, time(6), timezone.utc)
            noon = datetime.combine(day, time(12), timezone.utc)
            morning = {station: cards.get((station, early)) for station in STATIONS}
            for station, signals in observed.items():
                start, end = climate_window(day, expansion_weather.STATIONS[station][3])
                raw = cli.get(((station, str(day), iso(start), iso(end)), "daily-high"), ())
                summary = target_highs.get(station, {}).get(str(day))
                if next_phase:
                    next_start, _ = climate_window(date.fromisoformat(WINDOWS[next_phase][0]), expansion_weather.STATIONS[station][3])
                    lock = next_start + timedelta(hours=min(HOURS))
                    raw = [row for row in raw if parse_time(row["at"]) < lock]
                    if summary and parse_time(summary["published_at"]) >= lock:
                        summary = None
                high = raw[-1]["amount"] if raw else summary.get("high_f") if summary else None
                if high is None or not summary and not raw:
                    omitted[phase, "Official station-day high unavailable"] += len(HOURS)
                    continue
                for hour in HOURS:
                    at = start + timedelta(hours=hour)
                    own = departure(station, morning[station], signals, at, at, LOCAL_AGE)
                    forecast = _raw_high((cards.get((station, noon)), morning[station]), at)
                    if own is None or forecast is None:
                        omitted[phase, "Local as-issued forecast or timely ASOS absent"] += 1
                        continue
                    times, readings = signals
                    lower = bisect_right(times, start - timedelta(microseconds=1))
                    upper = bisect_right(times, at - timedelta(minutes=10))
                    prior_high = max((x["temp_f"] for x in readings[lower:upper]), default=own["observation"]["temp_f"])
                    closest, upwind = candidates(station, at, own["observation"], observed, morning)
                    if upwind is None or closest is None:
                        omitted[phase, "No compatible upwind and nearby residuals"] += 1
                    raw_high, sd, guidance = forecast
                    rows.append({"window": phase, "station": station, "date": str(day), "at": iso(at), "hour": str(hour),
                                 "official_high": high, "official_report": raw[-1] if raw else summary,
                                 "source_grade": "individual original CLI" if raw else "NWS annual CLI summary",
                                 "raw_high": raw_high, "raw_sigma": sd, "own": own, "closest": closest, "upwind": upwind,
                                 "local_departure": own["departure_f"], "observed_maximum": prior_high,
                                 "observed_minus_raw": prior_high - raw_high,
                                 "nearby_departure": closest["ordinary"]["departure_f"] if closest else None,
                                 "upwind_departure": upwind["directional"]["departure_f"] if upwind else None,
                                 "forecast": {k: guidance[k] for k in ("issued_at", "available_at", "card_sha256", "source")}})
    receipt_summary = {"forecast_station_cards": len(cards), "forecast_gaps": card_gaps, "observation_receipts": observed_inputs,
                       "observation_gaps": observed_gaps, "annual_summary_gaps": label_gaps,
                       "forecast_receipts": [{"station": s, "issued_at": iso(t), "source": card["source"],
                                              "station_sha256": card["card_sha256"], "published_at": card["available_at"],
                                              "requested_file": str(expansion_nbm.card_path(data, s, t).relative_to(data))}
                                             for (s, t), card in cards.items()]}
    return rows, {"/".join(k): value for k, value in omitted.items()}, receipt_summary


def _x(row, method):
    return {key: row[key] for key in FEATURES[method]}


def _supported(row):
    return row["nearby_departure"] is not None and row["upwind_departure"] is not None


def analyze(rows, *, matching):
    frame = ([row for row in rows if _supported(row)] if matching else rows)
    methods = MODELS if matching else MODELS[:2]
    panel = {phase: [row for row in frame if row["window"] == phase] for phase in WINDOWS}
    fits = panel["fit"]
    descriptors = {"paired upwind and nearby": matching, "fit_frames": len(fits),
                   "fit_dates": len({r["date"] for r in fits}),
                   "calibration_dates": len({r["date"] for r in panel["calibration"]})}
    if not fits or not panel["calibration"] or not panel["later test"]:
        return [], [], descriptors
    models = {}
    for name in methods[1:]:
        box = make_pipeline(DictVectorizer(sparse=False), Ridge(alpha=10))
        box.fit([_x(row, name) for row in fits], [row["official_high"] - row["raw_high"] for row in fits])
        models[name] = box
    calculated = {}
    for phase in ("calibration", "later test"):
        calculated[phase] = []
        for row in panel[phase]:
            predictions = {MODELS[0]: row["raw_high"]}
            for method, model in models.items():
                predictions[method] = row["raw_high"] + float(model.predict([_x(row, method)])[0])
            calculated[phase].append({**row, "predictions": predictions})
    calibration = calculated["calibration"]
    residuals = {method: [row["official_high"] - row["predictions"][method] for row in calibration] for method in methods}
    evaluated = calculated["later test"]
    stats = []
    for group, evidence in (("all", evaluated), *((h, [r for r in evaluated if r["hour"] == str(h)]) for h in HOURS)):
        if not evidence:
            continue
        for method in methods:
            errors = defaultdict(list)
            for row in evidence:
                errors[row["date"]].append(abs(row["official_high"] - row["predictions"][method]))
            stats.append({"window": "later test", "cohort": "paired wind" if matching else "all local cases",
                          "local_standard_hour": group, "method": method, "calendar_dates": len(errors),
                          "station_days": len({(r["station"], r["date"]) for r in evidence}), "frames": len(evidence),
                          "mae_f": mean(mean(items) for items in errors.values())})
    descriptors["fitted_coefficients"] = {method: dict(zip(model[0].get_feature_names_out(), model[1].coef_.tolist(), strict=True))
                                          for method, model in models.items()}
    descriptors["calibration_residuals"] = residuals
    return stats, evaluated, descriptors


def market_brier(data, evaluated, calibration):
    indices = {(row["station"], row["date"]): row for row in evaluated}
    buckets = defaultdict(list)
    for venue in venue_archive.VENUES:
        for row in venue_archive.export(data, venue=venue, family="daily-high", status="settled"):
            where = row["station"], row["day"]
            if where not in indices or row["yes_payout"] not in ("0", "1") or row["units"] != "°F":
                continue
            ends = temperature_window(row)
            if ends is not None:
                buckets[venue, *where].append({"lower_f": ends[0], "upper_f": ends[1], "payout": int(row["yes_payout"]),
                                               "instrument_id": row["instrument_id"], "settlement_source": row["settlement_source"],
                                               "settlement_at": row["settlement_at"], "settlement_receipt_at": row["settlement_received_at"]})
    metrics, details = [], []
    for venue in venue_archive.VENUES:
        decisions = []
        for row in evaluated:
            actual = buckets.get((venue, row["station"], row["date"]), [])
            if sum(bucket["payout"] for bucket in actual) != 1:
                continue
            if _partition(actual) != "exhaustive":
                continue
            source = next(b for b in actual if b["payout"])
            low, high = _limits(source)
            if not low < row["official_high"] < high:
                continue
            values = []
            for bucket in actual:
                probabilities = {}
                for model, predicted in row["predictions"].items():
                    circles = [predicted] if model == MODELS[0] else [predicted + delta for delta in calibration[model]]
                    scale = row["raw_sigma"] if model == MODELS[0] else 0.8
                    probabilities[model] = _probability(_limits(bucket), circles, scale)
                values.append({**bucket, "probabilities": probabilities})
            decisions.append({"venue": venue, "station": row["station"], "date": row["date"], "at": row["at"], "buckets": values})
        details.extend(decisions)
        for method in calibration:
            if not decisions:
                continue
            groups = defaultdict(list)
            for row in decisions:
                groups[row["date"]].append(mean((b["probabilities"][method] - b["payout"]) ** 2 for b in row["buckets"]))
            metrics.append({"window": "Exact native payout buckets on paired weather cases", "venue": venue, "method": method,
                            "dates": len(groups), "station_days": len({(r["station"], r["date"]) for r in decisions}),
                            "decisions": len(decisions), "brier": mean(mean(items) for items in groups.values())})
    return metrics, details


def report(data, *, fetch):
    contexts, discarded, sources = assemble(data, fetch)
    local, _, local_fit = analyze(contexts, matching=False)
    comparable, wind, paired_fit = analyze(contexts, matching=True)
    scores, markets = market_brier(data, wind, paired_fit["calibration_residuals"]) if wind else ([], [])
    cases = []
    if wind:
        changes = sorted(wind, key=lambda row: abs(row["predictions"][MODELS[1]] - row["official_high"])
                         - abs(row["predictions"][MODELS[3]] - row["official_high"]))
        for row in (changes[0], changes[-1]):
            own, other = row["own"]["observation"], row["upwind"]
            match = next((m for m in markets if (m["station"], m["date"], m["at"]) == (row["station"], row["date"], row["at"])), None)
            instrument = next((b["instrument_id"] for b in match["buckets"] if b["payout"]), None) if match else None
            cases.append({"station": row["station"], "date": row["date"], "at": row["at"], "instrument_id": instrument,
                          "venue": match["venue"] if match else None, "title": f"{row['station']} wind from {own['wind_direction_deg']:.0f}°",
                          "detail": f"{other['station']} was {row['upwind_departure']:+.1f}°F from its earlier guidance. Local forecast {row['predictions'][MODELS[1]]:.1f}°F, upwind {row['predictions'][MODELS[3]]:.1f}°F, final {row['official_high']:.0f}°F.",
                          "data": row})
    summary = {"version": VERSION, "title": "Does upwind station temperature improve the local forecast?",
               "hypothesis": "An earlier upwind station departure outperforms both local residual correction and the nearest simultaneous neighbor.",
               "target": "Northeast station-day maximum and exact native payouts at two prespecified local hours.",
               "venues": list(venue_archive.VENUES), "date_window": {"from": WINDOWS["fit"][0], "through": WINDOWS["later test"][1], **WINDOWS},
               "metrics": [*local, *comparable, *scores], "cases": cases,
               "limits": ["The archived ASOS times use a 10-minute availability proxy. Live-executable weather needs the original local receipt.",
                          "Wind direction is local reported surface wind. The simple aligned travel time is a statistical feature.",
                          "Exact payout scores require the native winning bucket to contain the official report. Weather groups use the same dates and cases across methods."],
               "provenance": {"engine": VERSION, "station_set": STATIONS, "decision_hours_lst": HOURS, "window": WINDOWS,
                              "cross_station_distance_km": RADIUS_KM, "target_wind_min_knots": WIND_MIN_KNOTS,
                              "alignment_degrees": ALIGNMENT_DEGREES, "scikit_learn": sklearn_version,
                              "source_gaps": discarded, "paired_fit": paired_fit, "local_fit": local_fit,
                              "implementation_sha256": sha256(Path(__file__).read_bytes()).hexdigest()}}
    path = research_model.write_record(data, summary, {"case_contexts": contexts, "forecasts": wind, "native_cases": markets,
                                                       "sources": sources, "fit": {"paired": paired_fit, "local": local_fit}})
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=venue_archive.DEFAULT_DATA)
    parser.add_argument("--fetch", action="store_true", help="fill missing station forecast cards from official NOAA objects")
    args = parser.parse_args()
    path = report(args.data, fetch=args.fetch)
    output = json.loads(path.read_bytes())
    print(json.dumps({"path": str(path), "metrics": output["metrics"], "gaps": output["provenance"]["source_gaps"]}, indent=2))


if __name__ == "__main__":
    main()
