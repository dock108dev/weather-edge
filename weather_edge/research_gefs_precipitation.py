"""Compare complete 31-member rainfall distributions with station-card guidance."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
import gzip
from hashlib import sha256
import json
import math
from pathlib import Path
import statistics
import sys

from sklearn import __version__ as sklearn_version
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from . import expansion_nbm, expansion_weather, gefs, gefs_precipitation, research_gefs, research_model
from . import research_model_rain, research_model_rain_multi
from .archive import date_range
from .nbm import climate_window, iso, utc


VERSION = "gefs-measurable-precipitation-v1"
AUTUMN = research_gefs.WINDOWS
SUMMER = (
    ("fit", date(2026, 6, 15), date(2026, 7, 9)),
    ("calibration", date(2026, 7, 10), date(2026, 7, 21)),
    ("test", date(2026, 7, 22), date(2026, 8, 15)),
)
THRESHOLD_MM = .254
SEED = 20261115
METHODS = {
    "climatology": "Earlier station frequency",
    "nbm": "NOAA NBM P06 overlap proxy",
    "gefs": "GEFS complete member fraction",
    "nbm_logistic": "Calibrated NBM P06 and Q06",
    "gefs_logistic": "Calibrated GEFS frequency and median",
    "blend_logistic": "Calibrated NBM plus GEFS",
}


def nbm_forecast(data, station, start, end):
    for lag in (0, 6, 12, 18, 24):
        issue = start - timedelta(hours=lag)
        card = expansion_nbm.load_card(data, station, issue)
        if not card or not utc(card["issued_at"]) <= utc(card["available_at"]) <= start:
            continue
        forecast = research_model_rain.estimate_rain_window(card, start, end)
        if forecast:
            return {**forecast, "archive": str(expansion_nbm.card_path(data, station, issue).relative_to(data))}
    return None


def inputs(data, windows, price_audit=None):
    labels = {station: expansion_weather.daily_labels(data, station, windows[0][1], windows[-1][2])
              for station in gefs.CANDIDATES}
    sources = {}
    for station in gefs.CANDIDATES:
        path = expansion_weather.cli_file(data, station, windows[0][1].year)
        sources[station] = {"path": str(path.relative_to(data)), "sha256": sha256(path.read_bytes()).hexdigest()}

    def gather(job):
        split, station, day, next_date = job
        start, end = climate_window(day, gefs.STATIONS[station][3])
        label = labels[station].get(str(day))
        if label is None:
            return None, "no official full-day report found"
        nbm = nbm_forecast(data, station, start, end)
        if nbm is None:
            return None, "no complete as-issued NOAA station forecast"
        members = gefs_precipitation.day_members(data, day, station)
        if len(members) != len(gefs.MEMBERS):
            return None, "incomplete native ensemble available at decision"
        try:
            report = expansion_weather.raw_report(data, station, day, label["product"])
        except (OSError, KeyError, ValueError):
            return None, "could not verify official report text"
        if report is None:
            return None, "could not verify official report text"
        if next_date and utc(report["published_at"]) >= climate_window(next_date, gefs.STATIONS[station][3])[0]:
            return None, "report revision published after subsequent split began"
        native = report["precip_as_reported"]
        amount = 0.0 if native == "T" else expansion_weather.parse_number(native)
        if amount is None:
            return None, "official weather amount is missing"
        return {"station": station, "date": str(day), "at": iso(start), "climate_ends_at": iso(end),
                "truth": int(amount >= .01), "amount_in": amount, "official_report": report,
                "members": members, "forecast": nbm}, None

    jobs = [(split, station, day, windows[i + 1][1] if i + 1 < len(windows) else None)
            for i, (split, first, last) in enumerate(windows) for day in date_range(first, last)
            for station in gefs.CANDIDATES]
    by_split = {name: [] for name, *_ in windows}
    omitted = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for (split, station, day, _), (row, issue) in zip(jobs, pool.map(gather, jobs), strict=True):
            if row:
                by_split[split].append(row)
            else:
                omitted.append({"split": split, "station": station, "date": str(day), "reason": issue})
    matched, market_issues = research_model_rain_multi._match(data, by_split["test"])
    audit = {}
    if price_audit is not None:
        body = price_audit.read_bytes()
        audit = {"source": str(price_audit), "sha256": sha256(body).hexdigest(), "cases": json.loads(body)["cases"]}
    version = VERSION if windows == AUTUMN else "gefs-summer-measurable-precipitation-v1"
    return {"version": version, "windows": {name: [str(first), str(last)] for name, first, last in windows},
            "sets": by_split, "markets": matched, "market_issues": market_issues, "price_audit": audit,
            "omitted": omitted, "official_summary_sources": sources}


def _features(row):
    nbm = row["forecast"]
    samples = [member["liquid_mm"] for member in row["members"]]
    frequency = sum(amount >= THRESHOLD_MM for amount in samples) / len(samples)
    median = statistics.median(samples)

    def logit(p):
        p = min(.995, max(.005, p))
        return math.log(p / (1 - p))

    nbs = [logit(nbm["p06_overlap_probability"]), math.log1p(nbm["weighted_q06_inches"] / .01)]
    ensemble = [logit(frequency), math.log1p(median / THRESHOLD_MM)]
    return frequency, median, {"nbm_logistic": nbs, "gefs_logistic": ensemble, "blend_logistic": nbs + ensemble}


def _predictions(training, evaluation, *, reference):
    bins = defaultdict(list)
    for row in reference:
        bins[row["station"]].append(row["truth"])
    all_labels = [row["truth"] for row in reference]
    train = [_features(row)[2] for row in training]
    models = {}
    for method in ("nbm_logistic", "gefs_logistic", "blend_logistic"):
        estimator = make_pipeline(StandardScaler(), LogisticRegression(C=1, random_state=SEED))
        estimator.fit([row[method] for row in train], [row["truth"] for row in training])
        models[method] = estimator
    results = []
    for row in evaluation:
        frequency, median, features = _features(row)
        probabilities = {"climatology": statistics.mean(bins.get(row["station"], all_labels)),
                         "nbm": row["forecast"]["p06_overlap_probability"], "gefs": frequency}
        for method, estimator in models.items():
            probabilities[method] = float(estimator.predict_proba([features[method]])[0, 1])
        results.append({**row, "probabilities": probabilities, "gefs_median_mm": median})
    return results


def _scores(rows, group, *, display=False):
    methods = METHODS | ({"display": "Venue midpoint available before decision"} if display else {})
    return [{"group": group, "method": method, "label": label,
             "station_days": len({(row["station"], row["date"]) for row in rows}),
             "calendar_dates": len({row["date"] for row in rows}), "cases": len(rows),
             "yes_cases": sum(row["truth"] for row in rows),
             "brier": statistics.mean((row["probabilities"][method] - row["truth"]) ** 2 for row in rows)}
            for method, label in methods.items()] if rows else []


def _calibration(rows, method):
    bins = defaultdict(list)
    thresholds = [0, .05, .2, .5, .8, .95, 1.01]
    for row in rows:
        p = row["probabilities"][method]
        bucket = next((lo, hi) for lo, hi in zip(thresholds, thresholds[1:]) if lo <= p < hi)
        bins[bucket].append(row)
    return [{"method": method, "lower": lo, "upper": min(hi, 1),
             "mean_probability": statistics.mean(row["probabilities"][method] for row in cases),
             "observed_frequency": statistics.mean(row["truth"] for row in cases), "cases": len(cases),
             "station_days": len(cases), "calendar_dates": len({row["date"] for row in cases})}
            for (lo, hi), cases in sorted(bins.items())]


def _witness(row, preferred):
    market = row.get("market")
    text = (f"{sum(member['liquid_mm'] >= THRESHOLD_MM for member in row['members'])} of 31 GEFS members reached 0.01 inches. "
            f"NBM proxy {row['probabilities']['nbm']:.1%}, selected calibration {row['probabilities'][preferred]:.1%}. "
            f"Official precipitation {row['official_report']['precip_as_reported']} inches.")
    if market:
        text += f" {row['venue']} YES paid ${row['truth']}."
    return {"title": f"{row['station']} {row['date']} · Native ensemble rain", "station": row["station"],
            "date": row["date"], "at": row["at"], "venue": row.get("venue"),
            "instrument_id": market["instrument_id"] if market else None, "detail": text,
            "midpoint": row.get("market_midpoint"), "official_report": row["official_report"],
            "forecast": {"member_total_mm": [member["liquid_mm"] for member in row["members"]],
                         "ensemble_available_at": max(row["available_at"] for row in row["members"]),
                         "nbm": row["forecast"], "probabilities": row["probabilities"]}}


def _archived_prices(rows, audit):
    evidence = {(source["venue"], source["instrument_id"]): source for source in audit.get("cases", [])}
    groups = defaultdict(list)
    for row in rows:
        identity = row["venue"], row["market"]["instrument_id"]
        source = evidence.get(identity)
        if source is None or utc(source["decision_clock"]) != utc(row["at"]):
            continue
        for kind, quote in source["clocks"].items():
            if quote["status"] != "coherent":
                continue
            clock = utc(quote["at"])
            if not timedelta(0) <= utc(row["at"]) - clock <= timedelta(minutes=2):
                continue
            p = quote.get("probability")
            if p is None or not 0 <= p <= 1:
                continue
            groups[row["venue"], kind].append({**row, "market_price": quote, "market_midpoint": p,
                                               "probabilities": {**row["probabilities"], "display": p}})
    return groups


def run(data, frozen=None, *, windows=AUTUMN, price_audit=None, supersedes=None):
    if frozen is None:
        evidence = inputs(data, windows, price_audit)
        frozen, digest = research_model.freeze_inputs(data, evidence)
    else:
        body = gzip.decompress(frozen.read_bytes())
        evidence, digest = json.loads(body), sha256(body).hexdigest()
    splits = evidence["sets"]
    fit, validation, test = (splits[name] for name in ("fit", "calibration", "test"))
    selection = _predictions(fit, validation, reference=fit)
    selection_scores = _scores(selection, "Before test window")
    contenders = [row for row in selection_scores if row["method"].endswith("_logistic")]
    preferred = min(contenders, key=lambda row: row["brier"])["method"]
    scored = _predictions(fit, test, reference=[*fit, *validation])
    lookup = {(row["station"], row["date"]): row for row in scored}
    markets = [{**lookup[row["station"], row["date"]], **row,
                "probabilities": {**lookup[row["station"], row["date"]]["probabilities"],
                                  "display": row.get("market_midpoint")}} for row in evidence["markets"]]
    metrics = _scores(scored, "Held-out official weather")
    for venue in ("kalshi", "polymarket-us"):
        venue_rows = [row for row in markets if row["venue"] == venue]
        metrics.extend(_scores(venue_rows, f"Held-out {venue} verified payouts"))
        if not evidence.get("price_audit"):
            metrics.extend(_scores([row for row in venue_rows if row["probabilities"]["display"] is not None],
                                   f"Held-out {venue} matched display", display=True))
    price_groups = _archived_prices(markets, evidence.get("price_audit", {}))
    for (venue, kind), cases in sorted(price_groups.items()):
        metrics.extend(_scores(cases, f"Held-out {venue} archive {kind}", display=True))
    deltas = defaultdict(list)
    for row in scored:
        ps, y = row["probabilities"], row["truth"]
        deltas[row["date"]].append((ps[preferred] - y)**2 - (ps["nbm"] - y)**2)

    def gain(row):
        ps, y = row["probabilities"], row["truth"]
        return (ps["gefs"] - y)**2 - (ps["nbm"] - y)**2

    examples = sorted(markets or scored, key=gain)
    examples = [examples[0], examples[-1]] if len(examples) > 1 else examples
    title = "Can the 31-member GEFS rainfall forecast improve daily rain estimates?"
    if "summer" in evidence["version"]:
        title = "Summer check of 31-member GEFS rainfall forecasts"
    return research_model.write_record(data, {
        "version": evidence["version"], "title": title,
        **({"supersedes": supersedes} if supersedes else {}),
        "hypothesis": "Summing the four native six-hour precipitation windows for each ensemble member improves the chance of detecting measurable liquid precipitation over station-card probabilities and amounts alone.",
        "target": "At least 0.01 inches of liquid equivalent in the Central standard-time reporting day",
        "venues": ["kalshi", "polymarket-us"],
        "date_window": evidence.get("windows") or {name: [str(first), str(last)] for name, first, last in AUTUMN},
        "metrics": metrics, "selection": selection_scores, "pretest_selected": preferred,
        "calibration": {method: _calibration(scored, method) for method in dict.fromkeys(("nbm", "gefs", preferred))},
        "cases": [_witness(row, preferred) for row in examples],
        "limits": [
            "Four exact six-hour GRIB2 accumulations from the same member and 00Z cycle tile the fixed UTC−06 climate day. All 31 require a recorded NOAA object clock by its start.",
            "One millimeter of water equivalent is one kg/m². The numeric threshold is 0.254 mm. GEFS samples the nearest 0.5° grid cell. The outcome is a station report rounded to hundredths of an inch.",
            "Raw NWS text supplies every weather result. Trace remains trace and scores below the threshold. Missing weather amounts are ineligible. Polymarket US payouts use its printed report cutoff and fallback. Kalshi provider results stay venue-specific.",
            "P06 combines six-hour probabilities with an independence approximation. GEFS keeps the dependence of each member across time. Earlier-fit logistic models use fixed regularization. Selection uses the intervening calendar window. The date interval resamples all stations together.",
            "Market quotes measure a display comparison. Distinct weather and market outcomes stay separate when the report or contract provider differs.",
            "The supplemental display audit uses a native hourly close exactly at 06:00Z and the earlier or exact minute mark within two minutes. The archive does not establish the original publication or execution clock for those boundary marks.",
        ],
        "coverage": {"subsets": {name: len(rows) for name, rows in splits.items()}, "market_cases": len(markets),
                     "omitted_weather": dict(Counter(row["reason"] for row in evidence["omitted"])),
                     "omitted_market": len(evidence["market_issues"]),
                     "selected_minus_nbm_brier": statistics.mean(v for values in deltas.values() for v in values),
                     "selected_minus_nbm_brier_date_interval_95": research_gefs._mean_interval(deltas)},
        "provenance": {"input": str(frozen), "input_sha256": digest, "NOAA_GEFS": gefs.DOCS,
                       "supplemental_price_audit": {key: value for key, value in evidence.get("price_audit", {}).items() if key != "cases"},
                       "official_summary_sources": evidence["official_summary_sources"], "scikit_learn": sklearn_version,
                       "source_sha256": research_model.run_sources(sys.modules[__name__], gefs_precipitation,
                                                                    research_model_rain, research_model_rain_multi),
                       "reproduce": (f"python -m weather_edge.research_gefs_precipitation --inputs {frozen}"
                                     + (f" --supersedes {supersedes}" if supersedes else ""))},
    }, {"held_out_weather": scored, "held_out_payouts": markets,
        "matched_archive_prices": {"/".join(key): group for key, group in price_groups.items()}})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--inputs", type=Path)
    parser.add_argument("--summer", action="store_true")
    parser.add_argument("--price-audit", type=Path)
    parser.add_argument("--supersedes")
    args = parser.parse_args()
    print(run(args.data, args.inputs, windows=SUMMER if args.summer else AUTUMN,
              price_audit=args.price_audit, supersedes=args.supersedes))


if __name__ == "__main__":
    main()
