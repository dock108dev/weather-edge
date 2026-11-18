"""Audit the native AirNow hour and replay New York's public regional monitor series."""

import argparse
from collections import Counter, defaultdict
import csv
from datetime import date, timedelta
import gzip
from io import StringIO
import json
import math
from pathlib import Path
from statistics import mean, NormalDist, pstdev
import sys

from sklearn import __version__ as sklearn_version
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from . import research_model, venue_archive, venue_benchmark, venue_storm
from .collector import paused
from .market import iso, parse_time
from .venue_sources import CollectionPaused

VERSION = "airnow-nyc-hourly-and-daily-aqi-v2"
EARLIER_RUN = "airnow-nyc-hourly-and-daily-aqi-v1-0a1a1a3f7ec0f297"
FOLDER = "airnow-nyc"
OFFICIAL = "https://files.airnowtech.org/airnow/"
DOCS = "https://s3-us-west-1.amazonaws.com/files.airnowtech.org/airnow/docs/HourlyAQObsFactSheet.pdf"
FAQ = "https://docs.airnowapi.org/faq"
AREA = "New York City Region"
WITNESS_DATE = "2026-07-19"
BASE_HOURS, EXTRA_HOURS = (13, 16, 19), (17, 18, 20, 21)
THRESHOLDS = (60, 80, 100)
JFK = "USW00094789"
NOAA_HOURLY = "https://www.ncei.noaa.gov/oa/global-historical-climatology-network/hourly/access/by-year/"
MODELS = {"earlier_mean": "Earlier July 19:00Z mean", "persistence": "Earlier same-day AQI persistence",
          "ridge": "Ridge with same-day PM and ozone history", "weather": "Ridge with same-day air quality and JFK weather"}


def _get(data, name, source, collect, *, max_bytes=12*2**20):
    if collect and paused(data):
        raise CollectionPaused
    body, record = (venue_storm._get(data, name, source, folder=FOLDER, max_bytes=max_bytes) if collect
                    else venue_storm._existing(data, name, folder=FOLDER))
    if record["source"] != source:
        raise ValueError(f"Retained AirNow upstream identity changed for {name}")
    return body, record


def _hour(data, day, hour, collect):
    digits = day.replace("-", "")
    name = f"HourlyAQObs_{digits}{hour:02}.dat"
    source = f"{OFFICIAL}{day[:4]}/{digits}/{name}"
    raw, receipt = _get(data, name, source, collect)
    pollutants = ("OZONE_AQI", "PM10_AQI", "PM25_AQI", "NO2_AQI")
    regional = {}
    reader = csv.DictReader(StringIO(raw.decode("utf-8-sig", errors="replace")))
    valid_day = date.fromisoformat(day).strftime("%m/%d/%Y")
    for row in reader:
        areas = (row.get("ReportingArea_PipeDelimited") or "").split("|")
        if AREA not in {value.strip() for value in areas}:
            continue
        if (row["ValidDate"] != valid_day or row["ValidTime"] != f"{hour:02}:00" or row["AQSID"] in regional
                or row["CountryCode"] != "US" or row["StateName"] not in ("NY", "New York")):
            raise ValueError(f"Unexpected exact NYC regional monitor identity for {day} {hour}")
        values = {}
        for key in pollutants:
            if (amount := row[key]) and amount.isdigit() and 0 <= int(amount) <= 500:
                values[key] = int(amount)
        regional[row["AQSID"]] = {"name": row["SiteName"], "agency": row["DataSource"], "site": row["AQSID"],
                                  "reported": values, "hour": row["ValidTime"], "date": row["ValidDate"]}
    best = [(value, key, item["site"]) for item in regional.values() for key, value in item["reported"].items() if key != "NO2_AQI"]
    if not best:
        raise ValueError(f"No exact regional main-pollutant NowCast for {day} {hour}")
    result, primary, site = max(best)
    per_pollutant = {key: max((value for value, pollutant, _ in best if pollutant == key), default=None) for key in pollutants[:3]}
    at = parse_time(f"{day}T{hour:02}:00:00Z")
    return {"source_at": iso(at), "nominal_available_at": iso(at + timedelta(minutes=45)),
            "nowcast": result, "primary_pollutant": primary, "highest_monitor": regional[site], "source_site_count": len(regional),
            "per_pollutant": per_pollutant, "all_sites": list(regional.values()), "receipt": receipt}


def _daily(data, collect):
    receipt_name = "reportingarea-20260719.dat"
    path = f"{OFFICIAL}2026/20260719/reportingarea.dat"
    raw, receipt = _get(data, receipt_name, path, collect)
    records = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        fields = line.split("|")
        if len(fields) != 17 or fields[1] != "07/19/26" or fields[5] != "Y" or fields[7] != AREA or fields[8] != "NY":
            continue
        if fields[12].isdigit():
            records.append({"issue_date": fields[0], "valid_date": fields[1], "kind": "previous day's observed AQI", "pollutant": fields[11],
                            "value": int(fields[12]), "agency": fields[16], "raw_line": line})
    return {"records": records, "receipt": receipt}


def _jfk(data, year, collect):
    source = f"{NOAA_HOURLY}{year}/psv/GHCNh_{JFK}_{year}.psv"
    raw, receipt = _get(data, f"noaa-jfk-{year}.psv", source, collect, max_bytes=40*2**20)
    series = defaultdict(list)
    fields = ("temperature", "dew_point_temperature", "wind_speed", "wind_direction")
    accepted = {"744860-94789": {"0", "1", "4", "5"}, "ICAO-KJFK": {"", "0", "1", "4", "5"}}
    for row in csv.DictReader(StringIO(raw.decode("utf-8-sig")), delimiter="|"):
        if row.get("STATION") != JFK or row.get("Month") != "07" or row.get("Hour") not in ("13", "14", "15", "16"):
            continue
        if any(row[key + "_Report_Type"] != "FM15" or row[key + "_Quality_Code"] not in accepted.get(row[key + "_Source_Station_ID"], ())
               or not row[key] for key in fields):
            continue
        when = parse_time(row["DATE"] + "Z")
        temp, dew, wind_mps, direction = (float(row[key]) for key in fields)
        if not (-10 <= dew <= temp <= 55 and 0 <= wind_mps < 50 and 0 <= direction <= 360):
            continue
        direction_rad = math.radians(direction)
        series[when.date().isoformat()].append({"at": iso(when), "wind_speed_mps": wind_mps, "wind_from_degrees": direction,
                                               "wind_x": wind_mps * math.sin(direction_rad), "wind_y": wind_mps * math.cos(direction_rad),
                                               "temp_c": temp, "dew_c": dew, "raw": {key: row[key] for key in
                                                   ("STATION", "Station_name", *fields, *(f"{field}_Source_Station_ID" for field in fields),
                                                    *(f"{field}_Quality_Code" for field in fields))}})
    return series, receipt


def _markets(data):
    with venue_archive.connect(data) as db:
        rows = [dict(item) for item in db.execute(f"SELECT {venue_archive.PUBLIC_SELECT} FROM markets WHERE venue=? AND series_id='KXAQICITY'",
                                                 (venue_archive.KALSHI,))]
        for market in rows:
            cases = {}
            for nominal in (14, 17, 18):
                stamp = parse_time(f"{WITNESS_DATE}T{nominal:02}:00:00Z")
                quote, missing = venue_benchmark._history_point(db, market, stamp)
                cases[str(nominal)] = quote or {"unavailable": missing}
            market["fixed_price_clocks"] = cases
            if market["lower"] in ("80", "90"):
                market["native_tape"] = [dict(item) | {"body": json.loads(item["body"])} for item in db.execute(
                    """SELECT at,native_key,kind,receipt_id,body FROM history WHERE venue=? AND instrument_id=?
                    AND kind IN ('candle-60m','trade') AND at>='2026-07-19T16:00:00Z' ORDER BY at,native_key""",
                    (venue_archive.KALSHI, market["instrument_id"]),
                )]
    return sorted(rows, key=lambda market: (float(market["lower"] or 0), market["instrument_id"]))


def gather(data, *, collect=False):
    hourly, weather, weather_sources, missing = {}, {}, {}, {}
    for year in range(2023, 2027):
        try:
            rows, weather_sources[year] = _jfk(data, year, collect)
            weather.update(rows)
        except (OSError, ValueError, KeyError) as error:
            missing[f"KJFK {year}"] = str(error)
        for day in range(1, 32):
            key = date(year, 7, day).isoformat()
            for hour in ((*BASE_HOURS, *EXTRA_HOURS) if key == WITNESS_DATE else BASE_HOURS):
                try:
                    hourly.setdefault(key, {})[str(hour)] = _hour(data, key, hour, collect)
                except (OSError, ValueError, KeyError) as error:
                    missing[f"AirNow {key} {hour:02}Z"] = str(error)
    try:
        daily = _daily(data, collect)
    except (OSError, ValueError, KeyError) as error:
        daily = None
        missing["AirNow reporting-area daily AQI"] = str(error)
    return {"version": VERSION, "city_hours": hourly, "jfk_reports": weather, "jfk_receipts": weather_sources,
            "daily_witness": daily, "markets": _markets(data), "missing": missing}


def _rows(corpus):
    output = []
    for day, hours in sorted(corpus["city_hours"].items()):
        if any(str(hour) not in hours for hour in BASE_HOURS):
            continue
        am, mid, target = [hours[str(hour)] for hour in BASE_HOURS]
        predictors = [am["nowcast"], mid["nowcast"], mid["nowcast"]-am["nowcast"]]
        if all(x is not None for x in (am["per_pollutant"]["OZONE_AQI"], mid["per_pollutant"]["OZONE_AQI"],
                                       am["per_pollutant"]["PM25_AQI"], mid["per_pollutant"]["PM25_AQI"])):
            predictors += [item["per_pollutant"][key] for item in (am, mid) for key in ("OZONE_AQI", "PM25_AQI")]
        else:
            continue
        decision = parse_time(mid["nominal_available_at"])
        direct = max((row for row in corpus["jfk_reports"].get(day, []) if
                      timedelta(minutes=15) <= decision - parse_time(row["at"]) <= timedelta(hours=2)),
                     key=lambda row: row["at"], default=None)
        if direct is None:
            continue
        wind = [direct[key] for key in ("wind_x", "wind_y", "wind_speed_mps", "temp_c", "dew_c")]
        output.append({"day": day, "at": iso(decision), "target": target["nowcast"], "persistence": mid["nowcast"],
                       "air_features": predictors, "combined_features": predictors+wind, "weather": direct,
                       "evidence_receipts": [item["receipt"] for item in (am, mid, target)]})
    return output


def _fit(rows):
    fit = [row for row in rows if row["day"].startswith("2023-")]
    calibration = [row for row in rows if row["day"].startswith("2024-")]
    held = [row for row in rows if row["day"] >= "2025-01-01"]
    if not fit or not calibration or not held:
        return [], {}, {}
    for row in calibration + held:
        row["earlier_mean"] = mean(item["target"] for item in fit)
    coefficients = {}
    for key, features in (("ridge", "air_features"), ("weather", "combined_features")):
        model = make_pipeline(StandardScaler(), Ridge(alpha=10)).fit([row[features] for row in fit], [row["target"] for row in fit])
        predicted = model.predict([row[features] for row in calibration + held])
        for row, estimate in zip(calibration + held, predicted, strict=True):
            row[key] = float(estimate)
        coefficients[key] = {"alpha": 10, "intercept": float(model[-1].intercept_), "coefficients": model[-1].coef_.tolist()}
    spreads = {key: max(1, pstdev(item["target"] - item[key] for item in calibration)) for key in MODELS}
    return held, spreads, {"fit_dates": len(fit), "calibration_dates": len(calibration), "coefficients": coefficients}


def _probability(center, width, level):
    return 1 - NormalDist(center, width).cdf(level + .5)


def _cases(corpus, comparable):
    hourly = corpus["city_hours"].get(WITNESS_DATE, {})
    hourly_result = hourly.get("19")
    if not hourly_result or not corpus["daily_witness"]:
        return []
    daily = max((row["value"] for row in corpus["daily_witness"]["records"]), default=None)
    matched = next((row for row in comparable if row["day"] == WITNESS_DATE), None)
    examples = []
    for market in corpus["markets"][:2]:
        trades = [{"at": row["at"], "yes": float(row["body"]["yes_price_dollars"])} for row in market.get("native_tape", [])
                  if row["kind"] == "trade" and row["body"].get("yes_price_dollars") is not None]
        asks = [{"at": row["at"], "yes": float(price)} for row in market.get("native_tape", []) if row["kind"] == "candle-60m"
                and (price := venue_archive.kalshi_dollar_close(row["body"].get("yes_ask"))) is not None]
        examples.append({"venue": market["venue"], "instrument_id": market["instrument_id"], "date": WITNESS_DATE,
                         "at": hourly_result["source_at"], "title": "The New York City hourly AQI was 66 while the daily value was 158",
                         "detail": f"At 15:00 EDT, the highest exact regional AirNow monitor NowCast was {hourly_result['nowcast']} in Queens. "
                                   f"The later daily region report had {daily}. Native YES paid ${market['yes_payout']} at the {market['lower']} threshold.",
                         "venue_price_timeline": {"series": [{"label": label, "kind": kind, "points": points} for label, kind, points in
                              (("Kalshi actual prints", "trade", trades), ("Kalshi hourly YES ask", "display", asks)) if points],
                              "markers": [{"at": market["closed_at"], "label": "Venue close and AirNow reference hour", "kind": "close"}]},
                         "data": {"market": market, "exact_airnow_hours": hourly, "daily_region_report": corpus["daily_witness"],
                                  "prior_hour_wind_model": matched}})
    return examples


def evaluate(data, corpus):
    frozen, digest = research_model.freeze_inputs(data, {**corpus, "version": VERSION})
    rows = _rows(corpus)
    held, spreads, fitting = _fit(rows)
    hourly = corpus["city_hours"].get(WITNESS_DATE, {}).get("19", {}).get("nowcast")
    daily = max((row["value"] for row in (corpus["daily_witness"] or {}).get("records", [])), default=None)
    native = [row for row in corpus["markets"] if row["yes_payout"] in ("0", "1") and row["operator"] == "above" and row["lower"] is not None]
    scores = []
    for label, group in (("Both untouched July periods", held), ("July 2025", [row for row in held if row["day"].startswith("2025-")]),
                         ("July 2026", [row for row in held if row["day"].startswith("2026-")])):
        for key, name in MODELS.items():
            if group:
                scores.append({"group": label, "model": name, "calendar_dates": len(group),
                               "mae_aqi": mean(abs(row["target"] - row[key]) for row in group),
                               "bucket_cases": len(group)*len(THRESHOLDS),
                               "brier": mean((_probability(row[key], spreads[key], threshold) - int(row["target"] > threshold))**2
                                             for row in group for threshold in THRESHOLDS)})
    counts = Counter(item["day"][:4] for item in rows)
    record = {"version": VERSION, "supersedes": EARLIER_RUN, "title": "New York City AirNow hourly AQI and daily reporting are different targets",
              "hypothesis": "JFK wind and moisture can improve a later regional AirNow hourly forecast beyond observed pollutants.",
              "target": "19:00Z regional maximum hourly AirNow pollutant NowCast and actual native payout where available",
              "venues": ["Kalshi"], "date_window": {"fit": ["2023-07-01", "2023-07-31"], "calibration": ["2024-07-01", "2024-07-31"],
                                                      "held": [["2025-07-01", "2025-07-31"], ["2026-07-01", "2026-07-31"]]},
              "coverage": {"retained_hourly_reports": sum(len(hours) for hours in corpus["city_hours"].values()),
                           "weather_and_aqi_dates_by_year": dict(counts), "held_dates": len(held), "native_payouts": len(native),
                           "hourly_source_compatible_payouts": sum(int(hourly > float(row["lower"])) == int(row["yes_payout"]) for row in native) if hourly is not None else None,
                           "daily_substitution_cash_sign_changes": sum(int(daily > float(row["lower"])) != int(row["yes_payout"]) for row in native) if daily is not None else None,
                           "native_same_clock_display_samples": sum("probability" in point for market in native for point in market["fixed_price_clocks"].values())},
              "metrics": scores, "cases": _cases(corpus, held), "missing": corpus["missing"], "fitting": fitting,
              "limits": ["The AirNow archive specifies a usual update around 35 minutes past the hour. The replay waits 45 minutes. First historical publication and revision lineage remain unverified.",
                         "The archived HTTP Last-Modified is retained and may be the following day. This leaves no original execution-time receipt.",
                         "The New York City Region covers all monitors using that exact AirNow reporting-area identifier. New York City municipal boundaries are a separate geography.",
                         "The three main NowCast pollutants are kept separate from the NO₂ site AQI and the later daily summary.",
                         "JFK features use a prior routine airport report. The station is a weather covariate and separate from the payout source."],
              "provenance": {"AirNow file format": DOCS, "AirNow reporting areas": FAQ, "scikit_learn": sklearn_version,
                             "probability_cutoffs": THRESHOLDS, "decisions_at": "16:45Z", "frozen_input": str(frozen.relative_to(data)),
                             "frozen_input_sha256": digest, "source_sha256": research_model.run_sources(sys.modules[__name__], venue_benchmark, venue_storm)}}
    return research_model.write_record(data, record, {"held": held, "cash_scope": native, "calibrated_error_sd_aqi": spreads})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--collect", action="store_true")
    parser.add_argument("--input", type=Path)
    args = parser.parse_args(argv)
    try:
        corpus = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else gather(args.data, collect=args.collect)
        print(evaluate(args.data, corpus))
    except CollectionPaused:
        print("Collection is stopped. Retained receipts are available.")
        return 2


if __name__ == "__main__":
    sys.exit(main())
