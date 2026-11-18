"""Audit native high-temperature combinations and test joint weather residuals."""

import argparse
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from functools import cache
import gzip
from hashlib import sha256
import json
import math
from pathlib import Path
import re
from statistics import NormalDist, mean
import sys

import numpy as np
from sklearn import __version__ as sklearn_version
from sklearn.covariance import LedoitWolf

from . import expansion_nbm, expansion_replay, research_model, venue_archive, venue_benchmark, venue_history, venue_snow_data
from .market import decimal, iso, parse_time
from .nbm import climate_window
from .venue_contracts import STATION_OFFSETS, temperature_window
from .venue_sources import PublicGET

VERSION = "multi-station-weather-natives-v1"
STATIONS = ("KAUS", "KMDW", "KDEN", "KHOU", "KMIA", "KNYC", "KPHL")
FIT, CALIBRATION, TEST = ("2024-10-01", "2024-12-11"), ("2024-12-12", "2025-01-31"), ("2025-02-01", "2025-03-04")
TERMS = "https://assets.kalshi.com/contract_terms/CITIESWEATHER.pdf"
DECISION = time(16, 55, tzinfo=timezone.utc)
FIXED_QUOTE = time(16, tzinfo=timezone.utc)
PRICE_DECISION = time(16, 5, tzinfo=timezone.utc)
SMOOTH = 8
METHODS = {"noaa_product": "Independent NOAA station probabilities", "residual_product": "Independent earlier residual marginals",
           "matched_days": "Joint errors from the same earlier days", "covariance": "Earlier shrunk covariance"}
HOBBY_CLI = re.compile(r"^(\d{12})-KHGX-CDUS\d{2}-CLIHOU$")


def _station_label(station, item):
    if station == "KHOU":
        product = item.get("product", "")
        match = HOBBY_CLI.fullmatch(product)
        if item.get("station") != station or not match:
            return None
        day = date.fromisoformat(item["valid"])
        stamped = datetime.strptime(match[1], "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
        _, end = climate_window(day, STATION_OFFSETS[station])
        if not end <= stamped <= end + timedelta(days=15):
            return None
        return {"date": str(day), "high_f": item.get("high"), "product": product, "published_at": iso(stamped),
                "source": venue_snow_data.IEM_TEXT + product}
    return venue_snow_data._day_row(station, item, offset=STATION_OFFSETS[station])


def _labels(data):
    by_station, sources, missed = {}, {}, []
    for station in STATIONS:
        parsed, loc = {}, []
        for year in (2024, 2025):
            path = data / "venues" / "snow-climate" / station / f"{year}.json"
            try:
                raw = path.read_bytes()
                receipt = json.loads(path.with_suffix(".json.receipt.json").read_bytes())
            except FileNotFoundError:
                missed.append(f"{station} {year}")
                continue
            if sha256(raw).hexdigest() != receipt["sha256"]:
                raise ValueError(f"Changed registered source receipt {path}")
            loc.append(receipt)
            for item in json.loads(raw)["results"]:
                label = _station_label(station, item)
                if label and type(label["high_f"]) in (int, float) and -70 < label["high_f"] < 140:
                    if label["date"] in parsed:
                        raise ValueError(f"Duplicate official date at {station} {label['date']}")
                    parsed[label["date"]] = label
        by_station[station], sources[station] = parsed, loc
    return by_station, sources, missed


def _forecast(data, station, day):
    local = climate_window(day, STATION_OFFSETS[station])
    decision = datetime.combine(day, DECISION)
    issue = datetime.combine(day, time(12, tzinfo=timezone.utc))
    card = expansion_nbm.load_card(data, station, issue)
    if not card or not parse_time(card["issued_at"]) <= parse_time(card["available_at"]) <= decision:
        return None
    forecast = expansion_replay._high_forecast(card, *local)
    if forecast is None or not forecast["high_sd_f"] or forecast["high_sd_f"] <= 0:
        return None
    return {key: forecast[key] for key in ("issued_at", "available_at", "valid_at", "high_f", "high_sd_f", "source", "card_sha256")}


def _all_markets(data):
    with venue_archive.connect(data) as db:
        native = [dict(row) for row in db.execute(
            f"SELECT {venue_archive.PUBLIC_SELECT},raw_market FROM markets WHERE venue=? AND family='multi-station-high' "
            "ORDER BY day,instrument_id", (venue_archive.KALSHI,),
        )]
        legs = {row["instrument_id"]: dict(row) for row in db.execute(
            f"SELECT {venue_archive.PUBLIC_SELECT} FROM markets WHERE venue=? AND family='daily-high' AND day BETWEEN ? AND ?",
            (venue_archive.KALSHI, CALIBRATION[0], TEST[1]),
        )}
    for market in native:
        raw = json.loads(market.pop("raw_market"))
        identity = venue_archive._component_ids(raw)
        components, off_day = [], []
        for symbol, event, side in identity:
            row = legs.get(symbol)
            confirmed = bool(row and row["event_id"] == event and row["day"] == market["day"] and row["station"])
            if confirmed:
                components.append({"market": row, "side": side, "bounds": temperature_window(row)})
            elif row and row["event_id"] == event and row["day"] != market["day"] and row["station"]:
                off_day.append({"instrument_id": symbol, "event_id": event, "station": row["station"], "day": row["day"],
                                "side": side, "native_side_payout": row[side + "_payout"], "bounds": temperature_window(row)})
        yield {"market": market, "native_components": identity, "resolved_components": components,
               "full_identity": bool(identity and len(identity) == len(components)), "other_day_components": off_day,
               "reported_total_volume": str(raw.get("volume_fp") or raw.get("volume") or 0)}


def _download_quotes(data, markets):
    get = PublicGET(data)
    selections = {}
    for raw in markets:
        market = raw["market"]
        if not raw["full_identity"] or not TEST[0] <= market["day"] <= TEST[1] or not market["opened_at"] or not market["closed_at"]:
            continue
        clock = datetime.combine(date.fromisoformat(market["day"]), FIXED_QUOTE)
        if parse_time(market["opened_at"]) <= clock and datetime.combine(date.fromisoformat(market["day"]), PRICE_DECISION) < parse_time(market["closed_at"]):
            scope = market["day"], len(raw["native_components"])
            volume = decimal(raw["reported_total_volume"])
            if scope not in selections or volume > selections[scope][0]:
                selections[scope] = volume, market
    points = 0
    for _, row in selections.values():
        points += venue_history._archive_candle(data, get, row)
    return len(selections), points


def gather(data, *, prices=False):
    native = list(_all_markets(data))
    if prices:
        selection, points = _download_quotes(data, native)
        print(f"{selection} preselected market instruments returned {points} hourly candles", flush=True)
    labels, sources, absent = _labels(data)

    @cache
    def forecast(station, day):
        return _forecast(data, station, date.fromisoformat(day))

    dates = []
    start, stop = date.fromisoformat(FIT[0]), date.fromisoformat(TEST[1])
    for idx in range((stop - start).days + 1):
        day = str(start + timedelta(days=idx))
        draw = {station: {"official": labels[station].get(day), "forecast": forecast(station, day)} for station in STATIONS}
        dates.append({"day": day, "observations": draw})
    retained = {}
    with venue_archive.connect(data) as db:
        for row in native:
            combo = row["market"]
            if combo["opened_at"] and combo["closed_at"] and TEST[0] <= combo["day"] <= TEST[1]:
                clock = datetime.combine(date.fromisoformat(combo["day"]), FIXED_QUOTE)
                if parse_time(combo["opened_at"]) <= clock < parse_time(combo["closed_at"]):
                    display, _ = venue_benchmark._history_point(db, combo, clock)
                    if display:
                        retained[combo["instrument_id"]] = display
    return {"version": VERSION, "weather_days": dates, "native": native, "quoted": retained,
            "climate_source_receipts": sources, "station_year_gaps": absent}


def _condition(value, bounds, side):
    yes = np.logical_and(bounds[0] is None or value >= bounds[0] - .5, bounds[1] is None or value < bounds[1] + .5)
    return yes if side == "yes" else np.logical_not(yes)


def _fit(frames):
    samples = []
    for frame in frames:
        if not FIT[0] <= frame["day"] <= FIT[1]:
            continue
        weather = frame["observations"]
        if any(not weather[station]["official"] or not weather[station]["forecast"] or
               weather[station]["official"]["published_at"] >= CALIBRATION[0] + "T00:00:00Z" for station in STATIONS):
            continue
        samples.append([np.clip((weather[s]["official"]["high_f"] - weather[s]["forecast"]["high_f"]) /
                                weather[s]["forecast"]["high_sd_f"], -8, 8) for s in STATIONS])
    if len(samples) < 15:
        raise ValueError(f"Only {len(samples)} full earlier weather station-days are available for joint calibration")
    residuals = np.array(samples)
    model = LedoitWolf().fit(residuals)
    draws = np.random.default_rng(284).multivariate_normal(model.location_, model.covariance_, size=2048)
    return residuals, draws, model


def _combinations(row, modeled, residuals, draws):
    data, tables = [], []
    for item in row["resolved_components"]:
        market, span, side = item["market"], item["bounds"], item["side"]
        station = market["station"]
        weather = modeled.get(station)
        if not weather or not weather["official"] or not weather["forecast"] or span is None:
            return None
        forecast, actual = weather["forecast"], weather["official"]["high_f"]
        if parse_time(weather["official"]["published_at"]) <= datetime.combine(date.fromisoformat(market["day"]), DECISION):
            return None
        column = STATIONS.index(station)
        mu, sigma = forecast["high_f"], forecast["high_sd_f"]
        empirical = _condition(mu + sigma * residuals[:, column], span, side)
        gaussian = _condition(mu + sigma * draws[:, column], span, side)
        xs = NormalDist(mu, sigma)
        noaa = (1 if span[1] is None else xs.cdf(span[1] + .5)) - (0 if span[0] is None else xs.cdf(span[0] - .5))
        if side == "no":
            noaa = 1 - noaa
        tables.append((empirical, gaussian))
        data.append({"station": station, "forecast": forecast, "label": weather["official"], "bound": span, "side": side,
                     "reported": bool(_condition(actual, span, side)), "noaa": noaa,
                     "historical_marginal": (float(np.sum(empirical)) + 0.5) / (len(residuals) + 1)})
    empirical_marginals = math.prod(part["historical_marginal"] for part in data)
    joint = (float(np.sum(np.logical_and.reduce([point[0] for point in tables]))) + SMOOTH * empirical_marginals) / (len(residuals) + SMOOTH)
    corr = (float(np.sum(np.logical_and.reduce([point[1] for point in tables]))) + SMOOTH * empirical_marginals) / (len(draws) + SMOOTH)
    return {"components": data, "physical_yes": int(all(part["reported"] for part in data)),
            "probabilities": {"noaa_product": math.prod(part["noaa"] for part in data), "residual_product": empirical_marginals,
                              "matched_days": joint, "covariance": corr}}


def _audit(row):
    if not row["full_identity"]:
        return None
    payouts = [leg["market"][leg["side"] + "_payout"] for leg in row["resolved_components"]]
    if any(value not in ("0", "1") for value in payouts) or row["market"]["yes_payout"] not in ("0", "1"):
        return None
    parent = int(row["market"]["yes_payout"])
    binary = int(all(value == "1" for value in payouts))
    return {"observed_combo_yes": parent, "logical_and_of_native_leg_payouts": binary, "agrees": parent == binary}


def _metrics(rows, name, outcome):
    if not rows:
        return []
    all_days = Counter(row["date"] for row in rows)
    def result(key, logarithmic):
        by_date = defaultdict(list)
        for row in rows:
            p, y = row["probabilities"][key], row[outcome]
            by_date[row["date"]].append(-math.log(max(1e-6, p if y else 1-p)) if logarithmic else (p-y)**2)
        return mean(mean(errors) for errors in by_date.values())
    labels = {**METHODS, "Kalshi candle midpoint": "Kalshi candle midpoint"}
    return [{"cohort": name, "model": labels[key], "n": len(rows), "calendar_dates": len(all_days),
             "yes_outcomes": sum(row[outcome] for row in rows), "brier": result(key, False), "log_loss": result(key, True)}
            for key in rows[0]["probabilities"]]


def evaluate(data, corpus):
    frozen, digest = research_model.freeze_inputs(data, corpus)
    frames = corpus["weather_days"]
    can_fit = sum(FIT[0] <= row["day"] <= FIT[1] and all(part["official"] and part["forecast"]
                  for part in row["observations"].values()) for row in frames) >= 15
    fitted = _fit(frames) if can_fit else None
    weather = {row["day"]: row["observations"] for row in frames}
    cash_counts = Counter()
    claims, off_day, observations, wide = [], [], [], []
    for row in corpus["native"]:
        combo = row["market"]
        symbol, day = combo["instrument_id"], combo["day"]
        limits = datetime.combine(date.fromisoformat(day), DECISION)
        settlement = _audit(row)
        if row.get("other_day_components"):
            off_day.append(row)
        if settlement:
            cash_counts["all native leg payouts resolved"] += 1
            cash_counts["different verified joint payouts"] += int(not settlement["agrees"])
            if not settlement["agrees"]:
                claims.append({**row, "audit": settlement})
        if not fitted or not settlement or not TEST[0] <= day <= TEST[1] or not combo["opened_at"] or not combo["closed_at"] or not (
                parse_time(combo["opened_at"]) <= limits < parse_time(combo["closed_at"])):
            continue
        if values := _combinations(row, weather[day], *fitted[:2]):
            observations.append({"date": day, "native": row, "settlement": settlement, "actual_payout": settlement["observed_combo_yes"],
                                 "clock": iso(limits), **values})
            candle = corpus["quoted"].get(symbol)
            comparison = datetime.combine(date.fromisoformat(day), PRICE_DECISION)
            if candle and all(parse_time(part["forecast"]["available_at"]) <= comparison for part in values["components"]):
                wide.append({**observations[-1], "display": candle, "clock": iso(comparison),
                             "probabilities": {**values["probabilities"], "Kalshi candle midpoint": candle["probability"]}})
    metrics = _metrics(observations, "Same native cash payout", "actual_payout") + _metrics(
        observations, "Same recomputed physical threshold", "physical_yes") + _metrics(wide, "Same selected native quote and payout", "actual_payout")
    for legs in range(2, 8):
        metrics += _metrics([row for row in observations if len(row["components"]) == legs], f"{legs} native components", "actual_payout")
    cases, seen = [], set()
    ordered = [*off_day, *claims, *(row["native"] for row in observations if row["actual_payout"] == 1), *corpus["native"]]
    for candidate in ordered:
        combo = candidate["market"]
        paid = _audit(candidate)
        day = combo["day"]
        off = candidate.get("other_day_components") or []
        if day in seen or (paid is None and not off):
            continue
        detail = (f"Native combo YES paid ${paid['observed_combo_yes']}. All component payouts imply ${paid['logical_and_of_native_leg_payouts']}."
                  if paid else f"The primary rule uses {day}. Linked native component IDs refer to {off[0]['day']}. "
                               f"Observed native combo YES paid ${combo['yes_payout']}.")
        cases.append({"title": f"{len(candidate['native_components'])} cities · {day}", "venue": venue_archive.KALSHI,
                      "instrument_id": combo["instrument_id"], "date": day, "at": combo["settlement_at"],
                      "detail": detail, "data": candidate})
        seen.add(day)
        if len(cases) >= 6:
            break
    result = {"version": VERSION, "title": "Exact multi-station temperature contracts and dependence",
              "hypothesis": "Retaining the joint errors of a shared weather day can change probabilities compared with multiplying independent station estimates.",
              "target": "All weather thresholds in one native multi-station high contract", "venues": ["Kalshi"],
              "date_window": {"fit weather dates": FIT, "untouched later dates": TEST},
              "coverage": {"native combinations": len(corpus["native"]), "native calendar_dates": len({r["market"]["day"] for r in corpus["native"]}),
                           "known component identities": sum(row["full_identity"] for row in corpus["native"]), "verified cash audit": dict(cash_counts),
                           "raw component dates conflict with rule day": len(off_day),
                           "earlier complete weather days": len(fitted[0]) if fitted else 0, "later identical probabilities": len(observations),
                           "later independent calendar_dates": len({r["date"] for r in observations}), "native coherent prices": len(wide)},
              "metrics": metrics, "cases": cases, "missing": {"missing exact station-year CLIs": corpus["station_year_gaps"]},
              "limits": ["Weather and payout observations share calendar dates. Many related native combinations do not provide independent outcome trials.",
                         "The public catalog contains exact native component IDs and requested sides. Compound payouts are independently observed.",
                         "Native associated tickers that point at a different day are excluded from the aligned compound payout audit.",
                         "A single earlier NOAA 12 UTC issuance anchors each station. Original archive receipt is later than the archived source publication clock.",
                         "A sparse historical price sample does not establish book depth or portfolio fill capacity."],
              "provenance": {"frozen_input": str(frozen.relative_to(data)), "frozen_input_sha256": digest, "terms": TERMS,
                             "historical_residual_covariance": fitted[2].covariance_.tolist() if fitted else None,
                             "station_order": STATIONS, "decision_utc": str(DECISION), "decision_for_quote_comparison_utc": str(PRICE_DECISION),
                             "scikit_learn": sklearn_version,
                             "source_sha256": research_model.run_sources(sys.modules[__name__], venue_archive)}}
    return research_model.write_record(data, result, {"atomic_payout_disagreements": claims, "date_conflicts": off_day, "same_native_test": observations,
                                                      "same_native_price_clocks": wide})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--input", type=Path)
    parser.add_argument("--candles", action="store_true")
    args = parser.parse_args(argv)
    corpus = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else gather(args.data, prices=args.candles)
    print(evaluate(args.data, corpus), flush=True)


if __name__ == "__main__":
    main()
