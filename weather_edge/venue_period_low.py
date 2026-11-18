"""Model an exact multi-day minimum from complete official daily labels."""

import argparse
from datetime import date, datetime, time, timedelta, timezone
import gzip
from hashlib import sha256
import json
from pathlib import Path
from statistics import NormalDist, mean
import sys

from sklearn import __version__ as sklearn_version
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from . import research_model, strategy_twc, venue_archive, venue_snow_data
from .market import iso, parse_time

VERSION = "four-day-temperature-minimum-v1"
STATIONS = ("KBOS", "KEWR", "KNYC", "KPHL", "KPVD")
START, DAYS = date(2026, 11, 12), 4
FIT, CAL, TEST = (2010, 2017), (2018, 2020), (2021, 2025)
THRESHOLDS = {"KBOS": (28, 30, 32, 34, 36), "KEWR": (32, 34, 36, 38, 40), "KNYC": (36, 38, 40, 42, 44),
              "KPHL": (32, 34, 36, 38, 40), "KPVD": (30, 32, 34, 36, 38)}
PHASES = (-1, 0, 1, 2, 3)
MODELS = {"climate": "Earlier station/calendar climatology conditioned on observed minimum",
          "analog": "Earlier similar paths and remaining days", "logistic": "Earlier logistic threshold model",
          "boosted": "Earlier histogram gradient boosting"}


def _period(first):
    return [first + timedelta(days=offset) for offset in range(DAYS)]


def _annual(data, station):
    labels, sources, missing = {}, [], []
    for year in range(FIT[0], START.year + 1):
        path = data / "venues" / "snow-climate" / station / f"{year}.json"
        try:
            raw = path.read_bytes()
            receipt = json.loads(path.with_suffix(".json.receipt.json").read_bytes())
        except FileNotFoundError:
            missing.append(f"{station} {year}")
            continue
        if sha256(raw).hexdigest() != receipt["sha256"]:
            raise ValueError(f"NWS annual source integrity changed for {station} {year}")
        sources.append(receipt)
        for original in json.loads(raw)["results"]:
            row = venue_snow_data._day_row(station, original, offset=-5)
            if row and type(row["low_f"]) in (int, float) and -50 <= row["low_f"] <= 100:
                if row["date"] in labels:
                    raise ValueError(f"Duplicate NWS calendar day for {station} {row['date']}")
                labels[row["date"]] = row
    return labels, sources, missing


def _snapshot(station, first, clock, phase, labels, weather, *, source):
    periods = _period(first)
    history_days = [first - timedelta(days=n) for n in (3, 2, 1)]
    known = {str(day): labels[str(day)] for day in periods if str(day) in labels and
             parse_time(labels[str(day)]["published_at"]) <= clock}
    before = {str(day): weather[str(day)] for day in history_days if str(day) in weather and
              parse_time(weather[str(day)]["published_at"]) <= clock}
    expected = [weather[str(day)] for day in periods if str(day) in weather]
    return {"station": station, "start": str(first), "at": iso(clock), "phase": phase, "year": first.year,
            "season_day": (first.month - 11) * 30 + first.day, "issued": known, "prior": before,
            "min_known": min((row["low_f"] for row in known.values()), default=None),
            "actual": min((row["low_f"] for row in expected), default=None) if len(expected) == DAYS and source == "annual NWS" else None,
            "values": [row["low_f"] for row in expected] if len(expected) == DAYS else [], "label_source": source}


def _historical(station, weather):
    for year in range(FIT[0], TEST[1] + 1):
        start = date(year, 11, 8)
        for days in range(0, 52, DAYS):
            first = start + timedelta(days=days)
            periods = _period(first)
            if periods[-1].year != year or not all(str(day) in weather for day in periods):
                continue
            for phase in PHASES:
                clock = datetime.combine(first + timedelta(days=phase), time(18), timezone.utc)
                yield _snapshot(station, first, clock, phase, weather, weather, source="annual NWS")


def _features(row, threshold):
    prior = [day["low_f"] for day in row["prior"].values()]
    newest = next(reversed(row["issued"].values()))["low_f"] if row["issued"] else prior[-1] if prior else 45
    features = [threshold, row["season_day"], newest, mean(prior) if prior else 45,
                row["min_known"] if row["min_known"] is not None else 48,
                len(row["issued"]), len(prior)]
    return [*features, *(int(row["station"] == station) for station in STATIONS)]


def _examples(rows):
    return [({"threshold": threshold, "outcome": int(row["actual"] < threshold)}, row)
            for row in rows for threshold in THRESHOLDS[row["station"]] if row["actual"] is not None]


def _fit(rows):
    models = {}
    for phase in PHASES:
        stage = [row for row in rows if row["phase"] == phase]
        training = _examples(stage)
        xs = [_features(row, example["threshold"]) for example, row in training if not _already(row, example["threshold"])]
        ys = [example["outcome"] for example, row in training if not _already(row, example["threshold"])]
        models[phase] = {
            "logistic": make_pipeline(StandardScaler(), LogisticRegression(C=1.0)).fit(xs, ys),
            "boosted": HistGradientBoostingClassifier(max_iter=90, max_leaf_nodes=11, l2_regularization=1.0,
                                                      min_samples_leaf=45, random_state=7).fit(xs, ys),
            "historical": stage,
        }
    return models


def _already(row, threshold):
    return row["min_known"] is not None and row["min_known"] < threshold


def _temperature_distance(target, sample):
    current, known = _features(target, 0), _features(sample, 0)
    return 0.30 * abs(current[1] - known[1]) + abs(current[2] - known[2]) + 0.60 * abs(current[3] - known[3])


def _rate(examples, threshold, getter):
    return (sum(getter(sample) < threshold for sample in examples) + 0.5) / (len(examples) + 1)


def _predict(data, row, threshold):
    if _already(row, threshold):
        return {method: 1.0 for method in MODELS}
    pool = [sample for sample in data["historical"] if sample["station"] == row["station"]
            and abs(sample["season_day"] - row["season_day"]) <= 21]
    available = [date.fromisoformat(day) for day in row["issued"]]
    first = date.fromisoformat(row["start"])
    remaining = [index for index in range(DAYS) if first + timedelta(days=index) not in available]
    if not remaining:
        return {method: 0.0 for method in MODELS}
    choose = lambda sample: min(sample["values"][index] for index in remaining)
    neighbors = sorted(pool, key=lambda sample: _temperature_distance(row, sample))[:24]
    return {"climate": _rate(pool, threshold, choose), "analog": _rate(neighbors, threshold, choose),
            **{method: float(data[method].predict_proba([_features(row, threshold)])[0, 1])
               for method in ("logistic", "boosted")}}


def _live(data, yearly, contracts, decision):
    output = {}
    for station in STATIONS:
        weather = yearly[station]
        official = strategy_twc.reports(data, station=station, start=str(START), end=str(_period(START)[-1]), as_of=decision)
        eligible = {row["label_date"]: {**row, "low_f": row["temperature_low_f"], "published_at": row["available_at"]}
                    for row in official if row["provider_status"] in ("official", "revised") and row["temperature_low_f"] is not None}
        phase = max(-1, min(DAYS - 1, (decision.date() - START).days))
        features = _snapshot(station, START, decision, phase, eligible, weather, source="The Weather Company")
        forecast = {}
        for day in _period(START):
            if str(day) not in eligible:
                nearest = research_model.available_forecast(research_model.day_forecasts(data, station, day), decision)
                if nearest and nearest["sd_f"] and nearest["sd_f"] > 0:
                    forecast[str(day)] = nearest
        books = {}
        for market in contracts:
            if market["station"] == station:
                found = venue_archive.books(data, venue_archive.KALSHI, market["instrument_id"],
                                            start=decision - timedelta(hours=1), end=decision, limit=1)
                if found:
                    books[market["instrument_id"]] = found[0]
        output[station] = {"features": features, "official": official, "forecast": forecast, "books": books}
    return output


def gather(data, decision):
    by_station, source, missing, historical = {}, {}, [], []
    for station in STATIONS:
        weather, source[station], absent = _annual(data, station)
        by_station[station] = weather
        missing.extend(absent)
        historical.extend(_historical(station, weather))
    with venue_archive.connect(data) as db:
        contracts = [dict(row) for row in db.execute(f"SELECT {venue_archive.PUBLIC_SELECT} FROM markets WHERE venue=? AND "
                       "family='period-low' AND day=? ORDER BY instrument_id", (venue_archive.KALSHI, str(START)))]
    return {"version": VERSION, "historical": historical, "source": source, "station_year_gaps": missing,
            "contracts": contracts, "as_of": iso(decision), "live": _live(data, by_station, contracts, decision)}


def _scores(rows, label):
    if not rows:
        return []
    return [{"cohort": label, "model": title, "n": len(rows),
             "independent_start_dates": len({row["start"] for row in rows}), "station_periods": len({(row["start"], row["station"]) for row in rows}),
             "brier": mean((row["probability"][method] - row["outcome"])**2 for row in rows)} for method, title in MODELS.items()]


def _noaa(bundle, threshold):
    features, forecasts = bundle["features"], bundle["forecast"]
    if _already(features, threshold):
        return 1.0
    if len(forecasts) != 1 or len(features["issued"]) != DAYS - 1:
        return None
    card = next(iter(forecasts.values()))
    return NormalDist(card["mean_f"], card["sd_f"]).cdf(threshold - 0.5)


def evaluate(data, corpus):
    fitted = _fit([row for row in corpus["historical"] if FIT[0] <= row["year"] <= FIT[1]])
    records = []
    for year_range, label in ((CAL, "earlier check"), (TEST, "untouched later seasons")):
        for example, weather in _examples(row for row in corpus["historical"] if year_range[0] <= row["year"] <= year_range[1]):
            methods = _predict(fitted[weather["phase"]], weather, example["threshold"])
            records.append({**weather, **example, "probability": methods, "group": label})
    held = [row for row in records if row["group"] == "untouched later seasons"]
    meaningful = [row for row in held if not _already(row, row["threshold"])]
    metrics = _scores(held, "All later four-day minima") + _scores(meaningful, "Still below-threshold uncertainty only")
    for phase in PHASES:
        metrics += _scores([row for row in meaningful if row["phase"] == phase], f"Decision day {phase:+d} at 18 UTC")
    current = []
    for contract in corpus["contracts"]:
        station, threshold = contract["station"], int(contract["upper"])
        bundle = corpus["live"][station]
        features = bundle["features"]
        probabilities = _predict(fitted[features["phase"]], features, threshold)
        current.append({"contract": contract, "features": features, "weather": bundle["official"],
                        "forecast": bundle["forecast"], "probability": probabilities, "noaa_single_remaining_day": _noaa(bundle, threshold),
                        "book": bundle["books"].get(contract["instrument_id"]), "observed_payout": contract["yes_payout"]})
    featured = {("KNYC", "38"), ("KNYC", "40"), ("KPHL", "40"), ("KPVD", "34"), ("KBOS", "36"), ("KEWR", "38")}
    cases = []
    for row in current:
        contract, features = row["contract"], row["features"]
        if (contract["station"], contract["upper"]) not in featured:
            continue
        support = len(features["issued"])
        description = (f"{support} of 4 official daily minima received by {corpus['as_of']}. "
                       f"Minimum among those received was {features['min_known']:g}°F. " if support else "No official daily minima received. ")
        description += (f"Native YES paid ${row['observed_payout']}." if row["observed_payout"] is not None else
                        "Native payout remains pending.")
        cases.append({"title": f"{contract['station']} four-day low below {contract['upper']}°F", "venue": venue_archive.KALSHI,
                      "instrument_id": contract["instrument_id"], "date": str(START), "at": corpus["as_of"],
                      "station": contract["station"], "detail": description, "data": row})
    frozen, digest = research_model.freeze_inputs(data, corpus)
    result = {"version": VERSION, "title": "Four-day Northeast minimum from complete climate reports",
              "hypothesis": "Independent daily label clocks and familiar early-period weather improve a four-day threshold over seasonal climatology.",
              "target": "Minimum of four complete Eastern calendar daily minima", "venues": ["Kalshi"],
              "date_window": {"fit": FIT, "earlier check": CAL, "held": TEST, "live dates": (str(START), str(_period(START)[-1]))},
              "coverage": {"held_exact_weather": len(held), "held_before_threshold_known": len(meaningful),
                           "held_independent_start_dates": len({row["start"] for row in held}),
                           "held_station_periods": len({(row["start"], row["station"]) for row in held}),
                           "open_contract_audit": len(current), "active_with_governing_report_already_below": sum(
                               _already(row["features"], int(row["contract"]["upper"])) for row in current),
                           "active_with_archived_coherently_identified_book": sum(bool(row["book"]) for row in current),
                           "positive_partial_cases_with_native_payout": sum(row["observed_payout"] is not None for row in current),
                           "one_day_NOAA_support": sum(bool(row["forecast"]) for row in current)},
              "metrics": metrics, "cases": cases, "missing": {"station-year labels unavailable": corpus["station_year_gaps"]},
              "limits": ["The official historic climate bulletin day uses local standard time. Scored windows begin after the Eastern autumn clock change.",
                         "Annual NWS transport may reflect a correction. Publication clocks come from the source bulletin identity.",
                         "A crossing in one complete TWC daily report is weather evidence. Full contract outcomes require native payout and allow material-error review.",
                         "One missing daily label permits a single available NOAA minimum forecast check. Multiple missing days would require a joint forecast.",
                         "Public Kalshi quote receipts omit native image clocks."],
              "provenance": {"frozen_input": str(frozen.relative_to(data)), "frozen_input_sha256": digest,
                             "climate_source_receipts": corpus["source"], "scikit_learn": sklearn_version,
                             "source_sha256": research_model.run_sources(sys.modules[__name__], research_model)}}
    return research_model.write_record(data, result, {"historical": records, "live": current})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--input", type=Path)
    parser.add_argument("--as-of")
    args = parser.parse_args(argv)
    decision = parse_time(args.as_of) if args.as_of else datetime.now(timezone.utc)
    corpus = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else gather(args.data, decision)
    print(evaluate(args.data, corpus), flush=True)


if __name__ == "__main__":
    main()
