"""Backtest the first Kalshi daily-low seasons with contemporaneous NOAA and ASOS."""

import argparse
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from datetime import date, timedelta
from functools import cache
import gzip
from hashlib import file_digest
import json
from pathlib import Path
from statistics import mean, stdev
import sys

from sklearn import __version__ as sklearn_version
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from . import expansion_weather, research_model, venue_archive, venue_benchmark, venue_snow_data
from .market import iso, parse_time
from .model import _limits, _probability
from .nbm import climate_window
from .venue_contracts import STATION_OFFSETS, temperature_window

VERSION = "kalshi-daily-lows-across-seasons-v2"
EARLIER = "kalshi-daily-lows-across-seasons-v1-98847b0794db4dad"
STATIONS = ("KNYC", "KMDW")
FIT, CALIBRATION, TEST = ("2025-12-13", "2026-03-31"), ("2026-04-01", "2026-04-30"), ("2026-06-01", "2026-10-14")
FOLLOW_UP = ("2026-10-15", "2026-11-14")
ASOS_RANGES = (("2025-12-12", "2026-05-02"), ("2026-05-31", "2026-10-16"),
               ("2026-10-16", "2026-10-17"), ("2026-10-17", "2026-11-16"))
HOURS = (3, 9)
METHODS = {"market": "Kalshi coherent candle midpoint", "noaa": "As-issued NOAA TXN + XND",
           "bias": "Earlier station and hour bias", "ridge": "Earlier direct-temperature Ridge",
           "residual": "Earlier NOAA residual correction with the forecast slope fixed",
           "quarter_weather": "75% market and 25% direct Ridge", "learned": "Earlier market and direct Ridge logistic",
           "learned_residual": "Earlier market and NOAA residual correction logistic"}


def _source(data, path):
    with path.open("rb") as file:
        return {"path": str(path.relative_to(data)), "sha256": file_digest(file, "sha256").hexdigest()}


def _labels(data, station):
    rows, sources = {}, []
    for year in (2025, 2026):
        path = data / "venues" / "snow-climate" / station / f"{year}.json"
        if not path.is_file():
            continue
        receipt = json.loads(path.with_suffix(".json.receipt.json").read_bytes())
        if _source(data, path)["sha256"] != receipt["sha256"]:
            raise ValueError(f"Changed archived official climate source {path}")
        sources.append(receipt)
        for native in json.loads(path.read_bytes())["results"]:
            label = venue_snow_data._day_row(station, native, offset=STATION_OFFSETS[station])
            if label and type(label["low_f"]) in (float, int) and -50 < label["low_f"] < 110:
                if label["date"] in rows:
                    raise ValueError(f"Multiple official low records for {station} {label['date']}")
                rows[label["date"]] = label
    return rows, sources


def _observations(data, station):
    rows, sources = [], []
    for first, last in ASOS_RANGES:
        a, b = date.fromisoformat(first), date.fromisoformat(last)
        path = expansion_weather.observation_file(data, station, a, b)
        if path.is_file():
            rows.extend(expansion_weather.observed_data(data, station, a, b))
            sources.append(_source(data, path))
    rows.sort(key=lambda row: row["at"])
    return rows, [row["at"] for row in rows], sources


def _snap(decision, bounds, series, forecast):
    chosen = research_model.available_forecast(forecast, decision)
    if not (chosen and chosen["sd_f"] is not None and chosen["sd_f"] > 0):
        return None
    first, last = bounds
    observations, times = series[:2]
    inclusive = decision - timedelta(minutes=10)
    stored = observations[bisect_left(times, iso(first)):bisect_right(times, iso(inclusive))]
    records = expansion_weather.observations_as_of(stored, first, decision, delay_minutes=10)
    minimum = min((row["temp_f"] for row in records), default=None)
    latest = records[-1] if records else None
    return {"guidance": chosen, "observed_minimum": minimum, "latest": latest, "retained_observations": len(records),
            "exact_weather_start": iso(first), "exact_weather_end": iso(last)}


def gather(data):
    observations = {station: _observations(data, station) for station in STATIONS}
    labels, sources = {}, {}
    for station in STATIONS:
        labels[station], sources[station] = _labels(data, station)

    @cache
    def _forecasts(station, day):
        return research_model.day_forecasts(data, station, day)

    @cache
    def _weather(station, day, hour):
        window = climate_window(day, STATION_OFFSETS[station])
        nominal = window[0] + timedelta(hours=hour)
        return _snap(nominal + venue_benchmark.WAIT, window, observations[station], _forecasts(station, day))

    misses = Counter()
    rows = []
    with venue_archive.connect(data) as db:
        markets = db.execute(f"SELECT {venue_archive.PUBLIC_SELECT} FROM markets WHERE venue=? AND family='daily-low' AND "
                          "status='settled' AND station IN (?,?) AND day BETWEEN ? AND ? AND yes_payout IS NOT NULL "
                          "ORDER BY day,station,instrument_id", (venue_archive.KALSHI, *STATIONS, FIT[0], FOLLOW_UP[1]))
        for raw in markets:
            contract = dict(raw)
            day_text, station = contract["day"], contract["station"]
            if CALIBRATION[1] < day_text < TEST[0]:
                continue
            band = temperature_window(contract)
            if band is None or contract["yes_payout"] not in ("0", "1"):
                misses["unsupported contract thresholds or scalar cash outcome"] += len(HOURS)
                continue
            label = labels[station].get(day_text)
            if label is None:
                misses["no exact official station minimum"] += len(HOURS)
                continue
            day = date.fromisoformat(day_text)
            first = climate_window(day, STATION_OFFSETS[station])[0]
            lower, upper = band
            actual = label["low_f"]
            y = int((lower is None or lower <= actual) and (upper is None or actual <= upper))
            if y != int(contract["yes_payout"]):
                misses["official annual low versus native cash payout disagrees"] += 1
            for hour in HOURS:
                nominal = first + timedelta(hours=hour)
                clock = nominal + venue_benchmark.WAIT
                if not (contract["opened_at"] and contract["closed_at"] and
                        parse_time(contract["opened_at"]) <= clock < parse_time(contract["closed_at"])):
                    misses["market was outside actual trading interval"] += 1
                    continue
                outcome_clock = parse_time(label["published_at"])
                if outcome_clock <= clock:
                    misses["daily outcome already published"] += 1
                    continue
                weather = _weather(station, day, hour)
                display, gap = venue_benchmark._history_point(db, contract, nominal)
                if weather is None:
                    misses["no as-issued forecast before decision"] += 1
                if display is None:
                    misses[gap] += 1
                if weather is None or display is None:
                    continue
                rows.append({"station": station, "date": day_text, "family": "daily-low", "hour_lst": hour, "at": iso(clock),
                             "outcome": y, "payout": int(contract["yes_payout"]), "label": label, "band": band,
                             "snapshot": weather, "quote": display, "market": display["probability"], "contract": contract})
    return {"version": VERSION, "rows": rows, "missing": dict(misses), "station_article_sources": sources,
            "station_observation_sources": {station: sources[2] for station, sources in observations.items()}}


def _obs_key(row):
    return row["station"], row["date"], row["hour_lst"]


def _features(row):
    snapshot = row["snapshot"]
    predicted = snapshot["guidance"]["mean_f"]
    minimum, last = snapshot["observed_minimum"], snapshot["latest"]
    dewpoint = last["dewpoint_f"] if last and last["dewpoint_f"] is not None else predicted
    return [predicted, snapshot["guidance"]["sd_f"], (minimum - predicted) if minimum is not None else 0,
            dewpoint - predicted, int(minimum is not None), int(row["station"] == "KNYC"), row["hour_lst"]]


def _fit_weather(fit, cal):
    all_fit, all_cal = (list({_obs_key(row): row for row in items}.values()) for items in (fit, cal))
    residuals = defaultdict(list)
    for row in all_fit:
        residuals[(row["station"], row["hour_lst"])].append(row["label"]["low_f"] - row["snapshot"]["guidance"]["mean_f"])
    bias = {key: mean(values) for key, values in residuals.items()}
    ridge = make_pipeline(StandardScaler(), Ridge(alpha=8)).fit([_features(row) for row in all_fit],
                                                                [row["label"]["low_f"] for row in all_fit])
    correction = make_pipeline(StandardScaler(), Ridge(alpha=8)).fit(
        [_features(row)[1:] for row in all_fit],
        [row["label"]["low_f"] - row["snapshot"]["guidance"]["mean_f"] for row in all_fit],
    )
    sigmas = defaultdict(list)
    for row, pred, added in zip(all_cal, ridge.predict([_features(row) for row in all_cal]),
                                correction.predict([_features(row)[1:] for row in all_cal]), strict=True):
        key = row["station"], row["hour_lst"]
        truth = row["label"]["low_f"]
        raw = row["snapshot"]["guidance"]["mean_f"]
        sigmas[key].append((truth - pred, truth - (raw + bias[key]), truth - (raw + added)))
    spreads = {key: tuple(max(0.7, stdev(series)) for series in zip(*vals, strict=True))
               for key, vals in sigmas.items() if len(vals) >= 5}
    return ridge, correction, bias, spreads


def _probability_rows(rows, fitted):
    model, correction, bias, spread = fitted
    if not rows:
        return []
    output = []
    for row, ridge, delta in zip(rows, model.predict([_features(row) for row in rows]),
                                correction.predict([_features(row)[1:] for row in rows]), strict=True):
        key = row["station"], row["hour_lst"]
        if key not in spread:
            continue
        first, last = row["band"]
        limits = _limits({"lower_f": first, "upper_f": last})
        guidance = row["snapshot"]["guidance"]
        fixed, sigma = guidance["mean_f"], guidance["sd_f"]
        learned = _probability(limits, [ridge], spread[key][0])
        output.append({**row, "noaa": _probability(limits, [fixed], sigma),
                       "bias": _probability(limits, [fixed + bias[key]], spread[key][1]),
                       "ridge": learned, "quarter_weather": row["market"] * 0.75 + learned * 0.25,
                       "residual": _probability(limits, [fixed + delta], spread[key][2]),
                       "point_estimates": {"noaa": fixed, "bias": fixed + bias[key], "ridge": float(ridge), "residual": float(fixed + delta)}})
    return output


def _anchor(earlier, later, weather_method, output_name):
    counts = Counter(row["date"] for row in earlier)
    weights = [len(earlier) / len(counts) / counts[row["date"]] for row in earlier]
    features = lambda row: [venue_benchmark._logit(row[key]) for key in ("market", weather_method)]
    model = LogisticRegression(C=1).fit([features(row) for row in earlier], [row["outcome"] for row in earlier], sample_weight=weights)
    for row, (_, p) in zip(later, model.predict_proba([features(row) for row in later]), strict=True):
        row[output_name] = float(p)
    return {"intercept": model.intercept_.tolist(), "coefficients": model.coef_.tolist()}


def _scores(rows, name, outcome):
    if not rows:
        return []
    frames = list({_obs_key(row): row for row in rows}.values())
    return [{"cohort": name, "model": label, **venue_benchmark._stats(rows),
             "brier": mean(venue_benchmark._date_errors(rows, method, outcome).values()),
             "temperature_mae_f": mean(abs(row["point_estimates"][method] - row["label"]["low_f"]) for row in frames)
                                  if method in ("noaa", "bias", "ridge", "residual") else None} for method, label in METHODS.items()]


def _case(row):
    contract = row["contract"]
    return {"venue": venue_archive.KALSHI, "instrument_id": contract["instrument_id"], "title": f"{row['station']} · {contract['label']}",
            "date": row["date"], "station": row["station"], "at": row["at"],
            "detail": f"Venue display {row['market']:.0%}. NOAA {row['noaa']:.0%}. Native YES paid ${row['payout']}. "
                      f"The annual NWS climate minimum for this station-day was {row['label']['low_f']:g}°F.", "data": row}


def evaluate(data, corpus):
    corpus["version"] = VERSION
    windows = {"fit": FIT, "calibration": CALIBRATION, "warm test and follow-up diagnosis": TEST, "later transfer check": FOLLOW_UP}
    splits = {label: [row for row in corpus["rows"] if start <= row["date"] <= end] for label, (start, end) in windows.items()}
    fit = [row for row in splits["fit"] if row["label"]["published_at"] < CALIBRATION[0] + "T00:00:00Z"]
    cal = [row for row in splits["calibration"] if row["label"]["published_at"] < TEST[0] + "T00:00:00Z"]
    if not all((fit, cal, splits["warm test and follow-up diagnosis"])):
        raise ValueError("All three calendar blocks need matched quote and forecast evidence")
    weather = _fit_weather(fit, cal)
    earlier = _probability_rows(cal, weather)
    later = _probability_rows(splits["warm test and follow-up diagnosis"], weather)
    follow = _probability_rows(splits["later transfer check"], weather)
    anchor = {"direct": _anchor(earlier, [*later, *follow], "ridge", "learned"),
              "residual": _anchor(earlier, [*later, *follow], "residual", "learned_residual")}
    metrics = _scores(later, "Same held native cash payouts", "payout") + _scores(later, "Same held official NWS daily low", "outcome")
    metrics += _scores(follow, "Later October 15 to November 14 native payouts", "payout")
    metrics += _scores(follow, "Later October 15 to November 14 official NWS daily lows", "outcome")
    for source in sorted({row["contract"]["resolution_source"] for row in later if row["contract"]["resolution_source"]}):
        scope = [row for row in later if row["contract"]["resolution_source"] == source]
        metrics += _scores(scope, source + " native rules and cash result", "payout")
    for hour in HOURS:
        metrics += _scores([row for row in later if row["hour_lst"] == hour], f"{hour:02}:05 local standard time and cash result", "payout")
    revisions = [row for row in later if row["outcome"] != row["payout"]]
    ranked = sorted(later, key=lambda row: abs(row["market"] - row["ridge"]), reverse=True)
    examples, covered = [], set()
    for row in [*revisions, *ranked]:
        key = row["station"], row["date"]
        if key not in covered:
            examples.append(_case(row))
            covered.add(key)
            if len(examples) >= 7:
                break
    frozen, digest = research_model.freeze_inputs(data, corpus)
    record = {"version": VERSION, "title": "Older daily lows before the two venues overlapped",
              "hypothesis": "An earlier-season NOAA and station model may add information to Kalshi after a long later-date split.",
              "target": "Daily minimum from exact station climate reports and native Kalshi cents", "venues": ["Kalshi"],
              "supersedes": EARLIER,
              "date_window": windows, "coverage": {"weather_fit": venue_benchmark._stats(fit), "earlier_price_fit": venue_benchmark._stats(earlier),
                "later": venue_benchmark._stats(later), "native_and_official_disagreement_rows": len(revisions),
                "later_transfer_check": venue_benchmark._stats(follow),
                "native_and_official_disagreement_station_days": len({(r["station"], r["date"]) for r in revisions}),
                "within_10c_spread": venue_benchmark._stats([r for r in later if r["quote"]["width"] <= 0.10])},
              "metrics": metrics, "calibration": venue_benchmark._tails(later, METHODS), "cases": examples, "missing": corpus["missing"],
              "limits": ["The native payout is a separate verified target. The Weather Company replaced NWS in both native series on August 14.",
                         "The fixed-slope residual challenger follows inspection of the first warm held block. Its October to November check was outside that run.",
                         "NBM clocks use archived issue plus server availability. ASOS measurement time is delayed ten minutes in the historical replay.",
                         "The same rows are tested by date. A repeated strike at two decision clocks shares a station-day outcome.",
                         "Candle midpoint provides display probability only and gives no executable size."],
              "provenance": {"frozen_input": str(frozen.relative_to(data)), "frozen_input_sha256": digest,
                             "archive_sources": {"CLIs": corpus["station_article_sources"], "ASOS": corpus["station_observation_sources"]},
                             "anchor": anchor, "decision_hours": HOURS, "scikit_learn": sklearn_version,
                             "source_sha256": research_model.run_sources(sys.modules[__name__], venue_benchmark, research_model)}}
    return research_model.write_record(data, record, {"held": later, "calibration": earlier, "later_transfer_check": follow,
                                                      "payout_disagreements": revisions})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--input", type=Path)
    args = parser.parse_args(argv)
    corpus = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else gather(args.data)
    print(evaluate(args.data, corpus), flush=True)


if __name__ == "__main__":
    main()
