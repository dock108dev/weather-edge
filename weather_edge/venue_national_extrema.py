"""Link contiguous-US native high-temperature contracts to the filed WPC product."""

import argparse
from datetime import date, datetime, time, timedelta, timezone
import gzip
import json
from pathlib import Path
import re
from statistics import NormalDist, mean, stdev
import sys

from sklearn import __version__ as sklearn_version
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from . import research_model, venue_archive, venue_benchmark, venue_storm
from .market import iso, parse_time
from .venue_contracts import temperature_window

VERSION = "contiguous-us-daily-extrema-v1"
WPC = "https://www.wpc.ncep.noaa.gov/discussions/hpcdiscussions.php?disc=nathilo&version=0&fmt=reg"
ARCHIVE = "https://www.wpc.ncep.noaa.gov/archives/web_pages/discussions/archive_nathilo.php"
TERMS = "https://assets.kalshi.com/contract_terms/USHIGH.pdf"
FIT, CAL, TEST = (2013, 2018), (2019, 2020), (2021, 2026)
THRESHOLDS = (90, 95, 100, 105, 110)
STATES = ("AZ", "CA", "FL", "NV", "TX")
METHODS = {"climate": "Earlier calendar climatology", "persist": "Previous WPC high and earlier residuals",
           "ridge": "Earlier Ridge on recent nationwide extrema", "region": "Earlier Ridge plus previous hottest location"}
HEADERS = re.compile(r"(High|Low) Temperature for [A-Za-z]+, ([A-Za-z]+ \d{1,2}, 20\d{2}) "
                     r"\([Aa]s received by (\d{2})(\d{2})? UTC ([A-Za-z]+ \d{1,2})\)")
EXTREMA = re.compile(r"(?<!\S)(-?\d{1,3}) at (.+?)(?=(?:\s+-?\d{1,3} at )|$)")


def _printed(raw, requested):
    doc = venue_storm.Page(raw).text()
    matches = list(HEADERS.finditer(doc))
    results = {}
    for idx, part in enumerate(matches):
        kind, reported_day, hour, minute, through = part.groups()
        shown = datetime.strptime(reported_day, "%B %d, %Y").date()
        if shown != requested or kind in results:
            raise ValueError(f"Unexpected WPC observation date {shown}")
        cutoff = datetime.strptime(f"{through} {shown.year}", "%B %d %Y").replace(
            hour=int(hour), minute=int(minute or 0), tzinfo=timezone.utc,
        )
        if cutoff.date() < shown:
            cutoff = cutoff.replace(year=shown.year + 1)
        chapter = doc[part.end():matches[idx + 1].start() if idx + 1 < len(matches) else len(doc)]
        chapter = re.split(r"(?i)\b(?:Page last modified|National Weather Service|disclaimer|colorado state university|weathergov|footer|meteorological development laboratory)\b",
                           chapter, maxsplit=1)[0]
        locations = [{"fahrenheit": int(fahrenheit), "location": location.strip()}
                     for fahrenheit, location in EXTREMA.findall(chapter.strip())]
        if not locations or len({x["fahrenheit"] for x in locations}) != 1:
            raise ValueError(f"Unverified WPC {kind} for {shown}")
        results[kind.lower()] = {"temperature_f": locations[0]["fahrenheit"], "locations": locations,
                                 "report_cutoff": iso(cutoff), "usable_after": iso(cutoff + timedelta(hours=1))}
    if set(results) != {"high", "low"}:
        raise ValueError(f"Weather Prediction Center label incomplete for {requested}")
    return results


def _reports(data):
    directory = data / "venues" / "national-extrema"
    sources, gaps = {}, {}
    for path in sorted(directory.glob("20*.html")):
        day = date.fromisoformat(path.stem)
        try:
            raw, receipt = venue_storm._existing(data, path.name, folder="national-extrema")
            row = _printed(raw, day)
        except (ValueError, OSError) as error:
            gaps[path.stem] = str(error)
            continue
        sources[day] = {**row, "receipt": receipt}
    return sources, gaps


def _frames(source):
    output = []
    for day, label in sorted(source.items()):
        if day.month not in (9, 10):
            continue
        cutoff = datetime.combine(day, time(20), timezone.utc)
        before = [source.get(day - timedelta(days=lag)) for lag in (1, 2, 3)]
        if not all(before) or any(parse_time(row["high"]["usable_after"]) > cutoff for row in before):
            continue
        last = before[0]
        low = last["low"] if parse_time(last["low"]["usable_after"]) <= cutoff else None
        if low is None:
            continue
        places = last["high"]["locations"]
        signatures = sorted({match[1] for item in places if (match := re.search(r", ([A-Z]{2})\b", item["location"]))})
        output.append({"date": str(day), "season_day": (day.month - 9) * 30 + day.day, "year": day.year, "at": iso(cutoff),
                       "observed": label["high"]["temperature_f"], "prior_high": last["high"]["temperature_f"],
                       "prior_low": low["temperature_f"], "prior_high_states": signatures,
                       "prior_three_day_high_average": mean(row["high"]["temperature_f"] for row in before),
                       "label": label, "available_prior": before})
    return output


def _features(row, geography):
    values = [row["season_day"], row["prior_high"], row["prior_low"], row["prior_three_day_high_average"]]
    return [*values, *(int(abbr in row["prior_high_states"]) for abbr in STATES)] if geography else values


def _fit(frames):
    fit = [row for row in frames if FIT[0] <= row["year"] <= FIT[1]]
    cal = [row for row in frames if CAL[0] <= row["year"] <= CAL[1]]
    candidates = {}
    for key, geo in (("ridge", False), ("region", True)):
        model = make_pipeline(StandardScaler(), Ridge(alpha=5)).fit([_features(row, geo) for row in fit], [row["observed"] for row in fit])
        residuals = [row["observed"] - prediction for row, prediction in zip(
            cal, model.predict([_features(row, geo) for row in cal]), strict=True)]
        candidates[key] = (model, residuals, geo)
    return fit, candidates, [row["observed"] - row["prior_high"] for row in cal]


def _forecaster(fit, alternatives, persist, row):
    season = [past["observed"] for past in fit if abs(row["season_day"] - past["season_day"]) <= 12]
    estimated = {key: (float(model.predict([_features(row, geo)])[0]) + mean(residuals), max(0.75, stdev(residuals)))
                 for key, (model, residuals, geo) in alternatives.items()}
    estimated["persist"] = row["prior_high"] + mean(persist), max(0.75, stdev(persist))
    return season, estimated


def _probabilities(temperature, bounds):
    samples, estimates = temperature
    lower, upper = bounds
    return {"climate": (sum(_hit(sample, bounds) for sample in samples) + 0.5) / (len(samples) + 1),
            **{key: (NormalDist(mu, spread).cdf(upper + .5) if upper is not None else 1) -
                     (NormalDist(mu, spread).cdf(lower - .5) if lower is not None else 0) for key, (mu, spread) in estimates.items()}}


def _hit(value, bounds):
    return int((bounds[0] is None or value >= bounds[0]) and (bounds[1] is None or value <= bounds[1]))


def _contracts(data, frames, reports):
    output = []
    by_day = {row["date"]: row for row in frames}
    with venue_archive.connect(data) as db:
        for row in db.execute(f"SELECT {venue_archive.PUBLIC_SELECT} FROM markets WHERE venue=? AND family='national-daily-high'",
                              (venue_archive.KALSHI,)):
            market = dict(row)
            period = market["day"]
            if not period:
                continue
            label = reports.get(date.fromisoformat(period))
            bounds = temperature_window(market)
            frame = by_day.get(period)
            clock = parse_time(frame["at"]) if frame else None
            display = None
            if clock and market["opened_at"] and market["closed_at"] and parse_time(market["opened_at"]) <= clock < parse_time(market["closed_at"]):
                display, _ = venue_benchmark._history_point(db, market, clock)
            output.append({"contract": market, "label": label, "label_implies_binary": _hit(label["high"]["temperature_f"], bounds)
                           if label and bounds else None, "bounds": bounds, "display": display})
    return output


def gather(data):
    reports, gaps = _reports(data)
    frames = _frames(reports)
    latest = [{"day": str(day), **row} for day, row in sorted(reports.items())[-2:]]
    return {"version": VERSION, "samples": frames, "gaps": gaps, "source_dates": len(reports),
            "contracts": _contracts(data, frames, reports), "most_recent_weather": latest}


def _brier(rows, group):
    if not rows:
        return []
    return [{"cohort": group, "model": METHODS.get(key, key), "n": len(rows), "dates": len({row["date"] for row in rows}),
             "brier": mean((row["p"][key] - row["outcome"])**2 for row in rows)} for key in rows[0]["p"]]


def evaluate(data, corpus):
    frozen, digest = research_model.freeze_inputs(data, corpus)
    frames = corpus["samples"]
    fit, alternatives, persist = _fit(frames)
    projections, scored = {}, []
    for row in frames:
        if TEST[0] <= row["year"] <= TEST[1]:
            projection = _forecaster(fit, alternatives, persist, row)
            projections[row["date"]] = projection
            for threshold in THRESHOLDS:
                scored.append({**row, "threshold": threshold, "p": _probabilities(projection, (threshold, None)),
                               "outcome": int(row["observed"] >= threshold)})
    priced, verified, differences = [], [], []
    for raw in corpus["contracts"]:
        market, implied = raw["contract"], raw["label_implies_binary"]
        payout = market["yes_payout"]
        if payout in ("0", "1") and implied is not None:
            verified.append(raw)
            if int(payout) != implied:
                differences.append(raw)
        if raw["display"] and raw["bounds"] and payout in ("0", "1") and market["day"] in projections:
            priced.append({"date": market["day"], "contract": market, "outcome": int(payout), "display": raw["display"],
                           "p": {**_probabilities(projections[market["day"]], raw["bounds"]),
                                 "Kalshi candle midpoint": raw["display"]["probability"]}})
    metrics = _brier(scored, "Same held WPC temperature thresholds") + _brier(priced, "Same native payout and price clocks")
    cases = []
    rich = {"HIGHUS-23SEP07-T110", "HIGHUS-23SEP08-B112.5", "HIGHUS-23OCT20-T105"}
    claimed = set()
    for row in [*differences, *[r for r in corpus["contracts"] if r["contract"]["instrument_id"] in rich], *verified]:
        contract = row["contract"]
        if contract["day"] in claimed or not row["label"] or row["label_implies_binary"] is None:
            continue
        label = row["label"]["high"]
        detail = (f"WPC archived {label['temperature_f']}°F at {label['locations'][0]['location']}. "
                  f"Native YES paid ${contract['yes_payout']}.")
        cases.append({"title": f"Contiguous-US high · {contract['day']}", "venue": venue_archive.KALSHI,
                      "instrument_id": contract["instrument_id"], "date": contract["day"], "at": contract["settlement_at"],
                      "detail": detail, "data": row})
        claimed.add(contract["day"])
        if len(cases) >= 5:
            break
    record = {"version": VERSION, "title": "Contiguous-US high temperatures from the Weather Prediction Center",
              "hypothesis": "Nationwide recent extrema outperform the calendar baseline. The last hottest state could add information after weather shifts.",
              "target": "Highest quality-controlled local daily temperature in the contiguous United States", "venues": ["Kalshi"],
              "date_window": {"fit seasons": FIT, "calibration seasons": CAL, "held seasons": TEST},
              "coverage": {"Weather Prediction Center dates": corpus["source_dates"], "held_thresholds": len(scored),
                           "dates_in_held_test": len({r["date"] for r in scored}), "native_market_contracts": len(corpus["contracts"]),
                           "verified_physical_and_native_payments": len(verified), "different_physical_and_native_outcomes": len(differences),
                           "same_clock_native_price_and_payout": len(priced)},
              "metrics": metrics, "cases": cases, "missing": {"unverified current or archived daily reports": len(corpus["gaps"])},
              "limits": ["The filed WPC product draws from several observing networks and is limited to the contiguous United States.",
                         "The sources show their observation cutoff. Historical original receipt clocks are unavailable, so replay waits one hour beyond the printed cutoff.",
                         "A day's WPC values are preliminary and can be revised. Native market payout remains a separate source.",
                         "The weather model has no current-day station or numerical weather forecast input. Native candles have no historical booked quantity."],
              "provenance": {"frozen_input": str(frozen.relative_to(data)), "frozen_input_sha256": digest,
                             "governing_product": WPC, "daily_archive": ARCHIVE, "native_terms": TERMS,
                             "scikit_learn": sklearn_version, "source_sha256": research_model.run_sources(sys.modules[__name__], venue_storm)}}
    return research_model.write_record(data, record, {"historical_weather": scored, "native_contracts": corpus["contracts"],
        "matched_price_score": priced, "market_vs_weather_disagreements": differences, "most_recent_weather": corpus["most_recent_weather"],
        "gaps": corpus["gaps"]})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--input", type=Path)
    args = parser.parse_args(argv)
    corpus = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else gather(args.data)
    print(evaluate(args.data, corpus), flush=True)


if __name__ == "__main__":
    main()
