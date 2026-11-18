"""Test long-range precipitation context for exact Thanksgiving rain listings."""

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import date, datetime, timedelta
from io import BytesIO, StringIO
import json
from math import log
from pathlib import Path
import re
from statistics import mean
import xml.etree.ElementTree as xml
from zipfile import ZipFile

from sklearn.linear_model import LogisticRegression

from . import collector, history, research_model, strategy_twc, venue_archive
from .market import decimal
from .strategy_alta import _archived_proof


ROOT = Path("strategy-thanksgiving")
VERSION = "thanksgiving-cpc-exact-rain-v1"
SERIES = "KXRAINTHANKSGIVING"
ISD = "https://www.ncei.noaa.gov/pub/data/noaa/isd-history.csv"
GHCN = "https://www.ncei.noaa.gov/pub/data/ghcn/daily/all/"
CPC = "https://ftp.cpc.ncep.noaa.gov/GIS/us_tempprcpfcst/"
CPC_DOC = "https://www.cpc.ncep.noaa.gov/products/predictions/erf_info.php?outlook=814&var=p"
NS = {"k": "http://www.opengis.net/kml/2.2"}
PHASES = {"regression fit": (2014, 2018), "selection": (2019, 2020), "holdout": (2021, 2025)}
METHODS = ("Late-November station climate", "Exact Thanksgiving climate", "Calibrated November climate", "Calibrated November climate with CPC")
CLIMATE_YEARS = (1999, 2013)
TARGET_YEAR = 2026
STATION_ALIASES = {"KDJT": {"old_icao": "KPBI", "effective_at": "2026-07-09T12:00:00Z",
                            "former_climate_product": "CLIPBI", "current_climate_product": "CLIDJT",
                            "faa_source": "https://content.govdelivery.com/attachments/USAFAA/2026/07/01/file_attachments/3701099/InFO26011.pdf",
                            "nws_source": "https://mesonet.agron.iastate.edu/wx/afos/p.php?e=202607082219&pil=PNSMFL"}}


def thanksgiving(year):
    first = date(year, 11, 1)
    return first + timedelta(days=(3 - first.weekday()) % 7 + 21)


def listings(data):
    stop = thanksgiving(TARGET_YEAR).isoformat()
    native = [market for market in venue_archive.export(data, venue=venue_archive.KALSHI, family="holiday-rain")
              if market["series_id"] == SERIES and market["day"] == stop and market["station"]
              and "strictly greater than 0 inches" in market["rules_primary"]
              and "Trace" in market["rules_secondary"] and "counted as 0 inches" in market["rules_secondary"]
              and market["resolution_source"] == "The Weather Company"]
    return {item["station"]: item for item in native}


def station_directory(content, wanted):
    chosen = {}
    sources = set(wanted) | {entry["old_icao"] for station, entry in STATION_ALIASES.items() if station in wanted}
    for row in csv.DictReader(StringIO(content.decode("utf-8-sig"))):
        station, wban = row["ICAO"], row["WBAN"]
        if (station not in sources or not wban.isascii() or not wban.isdigit() or wban == "99999"
                or row["CTRY"] != "US" or not row["LAT"] or not row["LON"]):
            continue
        if station not in chosen or row["END"] > chosen[station]["END"]:
            chosen[station] = {**row, "ghcn_id": "USW000" + wban, "lat": float(row["LAT"]), "lon": float(row["LON"])}
    for native, alias in STATION_ALIASES.items():
        if native in wanted and native not in chosen and alias["old_icao"] in chosen:
            chosen[native] = {**chosen[alias["old_icao"]], "station": native, "icao_continuity": alias}
    return {station: chosen[station] for station in wanted if station in chosen}


def daily_precipitation(content, identity):
    days = {}
    for line in content.decode("ascii").splitlines():
        if len(line) < 269 or line[:11] != identity or line[15:21] != "11PRCP":
            continue
        year = int(line[11:15])
        if not 1999 <= year <= 2026:
            continue
        for day in range(1, 31):
            index = 21 + 8 * (day - 1)
            amount = int(line[index:index + 5])
            measured, quality, provider = line[index + 5:index + 8]
            if amount < 0 or quality.strip() or measured == "A":
                continue
            days[date(year, 11, day)] = {"precip_tenths_mm": amount, "measurement_flag": measured.strip(),
                                        "source_flag": provider.strip(), "reported_measurable": amount > 0 and measured != "T"}
    return days


def _coordinates(node):
    return [tuple(map(float, xy.split(",")[:2])) for xy in node.text.split()] if node is not None and node.text else []


def _inside(ring, lon, lat):
    x0, y0 = ring[-1]
    result = False
    for x1, y1 in ring:
        if (y0 > lat) != (y1 > lat) and lon < (x1 - x0) * (lat - y0) / (y1 - y0) + x0:
            result = not result
        x0, y0 = x1, y1
    return result


def parse_outlook(content, issue):
    with ZipFile(BytesIO(content)) as container:
        members = [name for name in container.namelist() if name.endswith(".kml")]
        if len(members) != 1 or container.getinfo(members[0]).file_size > 6_000_000:
            raise ValueError("Unexpected CPC KML archive layout")
        root = xml.fromstring(container.read(members[0]))
    title = root.findtext(".//k:Document/k:name", "", NS)
    displayed_dates = [datetime.strptime(value, "%m/%d/%Y").date() for value in re.findall(r"\d{2}/\d{2}/\d{4}", title)]
    if displayed_dates != [issue, issue + timedelta(days=8), issue + timedelta(days=14)]:
        raise ValueError("CPC archive issue or valid period does not match the requested fixed lead")
    polygons = []
    for place in root.findall(".//k:Placemark", NS):
        name = place.findtext("k:name", "", NS).lower()
        match = re.search(r"(\d+(?:\.\d+)?)\s*(?:%|percent)", name)
        direction = "above" if "above" in name else "below" if "below" in name else "near"
        if not match or "precipitation" not in name:
            continue
        chance = float(match[1]) / 100
        for polygon in place.findall(".//k:Polygon", NS):
            outer = _coordinates(polygon.find("k:outerBoundaryIs/k:LinearRing/k:coordinates", NS))
            if len(outer) < 4:
                continue
            voids = [_coordinates(ring) for ring in polygon.findall("k:innerBoundaryIs/k:LinearRing/k:coordinates", NS)]
            lons, lats = zip(*outer)
            polygons.append((direction, chance, (min(lons), min(lats), max(lons), max(lats)), outer, [p for p in voids if len(p) >= 4]))
    if not polygons:
        raise ValueError("CPC precipitation outlook carries no binary or equal-chance regions")
    return title, polygons


def site_forecast(polygons, lon, lat):
    matches = [(category, chance) for category, chance, (left, bottom, right, top), ring, holes in polygons
               if left <= lon <= right and bottom <= lat <= top and _inside(ring, lon, lat)
               and not any(_inside(hole, lon, lat) for hole in holes)]
    populated = {category for category, chance in matches if chance > 1/3 and category != "near"}
    if len(populated) > 1:
        return None
    if not matches:
        return None
    category, chance = max(matches, key=lambda value: value[1])
    signed = (1 if category == "above" else -1 if category == "below" else 0) * max(0.0, chance - 1/3)
    return {"seven_day_category": category, "seven_day_category_probability": chance, "signed_probability_anomaly": signed}


def acquisition(data, native, fetch):
    root = data / ROOT
    site_file = root / "isd-history.csv"
    if not site_file.is_file() and fetch and not collector.paused(data):
        history._fetch(site_file, ISD)
    sites = station_directory(site_file.read_bytes(), native)
    jobs = {}
    for entry in sites.values():
        name = entry["ghcn_id"] + ".dly"
        jobs[name] = GHCN + name
    for year in range(PHASES["regression fit"][0], TARGET_YEAR + 1):
        issue = thanksgiving(year) - timedelta(days=11)
        name = f"814prcp_{issue:%Y%m%d}.kmz"
        jobs[name] = CPC + name

    def retain(item):
        name, url = item
        path = root / name
        if path.is_file():
            return name, None
        if not fetch or collector.paused(data):
            return name, "Local source file unavailable"
        try:
            history._fetch(path, url)
        except OSError as error:
            return name, str(error)
        return name, None

    with ThreadPoolExecutor(max_workers=4) as workers:
        status = dict(workers.map(retain, jobs.items()))
    return root, sites, jobs, {name: issue for name, issue in status.items() if issue is not None}


def _prior(rows, first, last, holiday_only=False):
    subset = [r for day, r in rows.items() if first <= day.year <= last and
              (day == thanksgiving(day.year) if holiday_only else 22 <= day.day <= 28)]
    return (sum(r["reported_measurable"] for r in subset) + 0.5) / (len(subset) + 1) if subset else None


def _model_features(p, anomaly):
    return [log(p / (1 - p)), anomaly]


def collect(data, fetch):
    native = listings(data)
    root, stations, jobs, gaps = acquisition(data, native, fetch)
    station_obs, outlooks, proof = {}, {}, {}
    for station, metadata in stations.items():
        name = metadata["ghcn_id"] + ".dly"
        if (path := root / name).is_file():
            station_obs[station] = daily_precipitation(path.read_bytes(), metadata["ghcn_id"])
            proof[name] = _archived_proof(path, data, jobs[name])
    for year in range(PHASES["regression fit"][0], TARGET_YEAR + 1):
        target = thanksgiving(year)
        issue = target - timedelta(days=11)
        name = f"814prcp_{issue:%Y%m%d}.kmz"
        if (path := root / name).is_file():
            try:
                title, polygons = parse_outlook(path.read_bytes(), issue)
            except ValueError as error:
                gaps[name] = str(error)
                continue
            forecast = {station: outlook for station, point in stations.items()
                        if (outlook := site_forecast(polygons, point["lon"], point["lat"])) is not None}
            outlooks[year] = forecast, {"title": title, "issue_date": issue.isoformat(),
                                        "target_covered": target.isoformat(), "filename": name}
            proof[name] = _archived_proof(path, data, jobs[name])
    metadata_file = root / "isd-history.csv"
    proof["isd-history.csv"] = _archived_proof(metadata_file, data, ISD)
    return native, stations, station_obs, outlooks, proof, gaps


def evaluate(weather, outlooks):
    pieces = defaultdict(list)
    for phase, (first, last) in PHASES.items():
        for year in range(first, last + 1):
            if year not in outlooks:
                continue
            region, edition = outlooks[year]
            target = thanksgiving(year)
            for station, precip in weather.items():
                if station not in region or target not in precip:
                    continue
                climate = _prior(precip, *CLIMATE_YEARS)
                exact = _prior(precip, *CLIMATE_YEARS, holiday_only=True)
                if None in (climate, exact):
                    continue
                proxy = region[station]
                weather_row = precip[target]
                pieces[phase].append({"station": station, "date": target.isoformat(), "year": year, "outlook": proxy, "edition": edition,
                                      "observed_precipitation": weather_row, "historical_measurable_rain": weather_row["reported_measurable"],
                                      "probability": {METHODS[0]: climate, METHODS[1]: exact},
                                      "feature": _model_features(climate, proxy["signed_probability_anomaly"])})
    if any(not pieces[phase] for phase in PHASES):
        raise ValueError("A chronological phase lacks same-station wet/dry labels and valid CPC outlooks")
    fit = pieces["regression fit"]
    models = {}
    for name, width in zip(METHODS[2:], (1, 2), strict=True):
        fitted = LogisticRegression(C=1).fit([row["feature"][:width] for row in fit], [int(row["historical_measurable_rain"]) for row in fit])
        models[name] = fitted
        for rows in pieces.values():
            for row, forecast in zip(rows, fitted.predict_proba([row["feature"][:width] for row in rows]), strict=True):
                row["probability"][name] = float(forecast[1])
    return pieces, models


def _brier(rows, method):
    date_rows = defaultdict(list)
    for row in rows:
        date_rows[row["date"]].append((row["probability"][method] - row["historical_measurable_rain"]) ** 2)
    return mean(mean(values) for values in date_rows.values())


def current_provider_check(data, archive):
    matched = []
    for native in strategy_twc.reports(data, start="2026-11-01", end="2026-11-14"):
        station, day = native["station"], date.fromisoformat(native["label_date"])
        actual = archive.get(station, {}).get(day)
        precipitation = native["precipitation_in"]
        if actual is None or precipitation is None or native["provider_status"] not in {"official", "revised"}:
            continue
        try:
            target = precipitation != "T" and decimal(precipitation) > 0
        except ValueError:
            continue
        matched.append({"station": station, "date": day.isoformat(), "ghcn": actual,
                        "twc_precipitation_in": precipitation, "twc_native_wet_label": target,
                        "threshold_agrees": target == actual["reported_measurable"],
                        "twc_report_proof": {key: native[key] for key in ("source_url", "body_sha256", "receipt_id",
                                                                      "provider_issued_at", "available_at")}})
    return matched


def report(data, fetch):
    native, stations, weather, outlooks, proof, gaps = collect(data, fetch)
    phases, models = evaluate(weather, outlooks)
    method = min(METHODS, key=lambda candidate: _brier(phases["selection"], candidate))
    metrics = [{"window": phase, "method": candidate, "selection_favorite": candidate == method,
                "independent_holidays": len({x["date"] for x in rows}), "station_days": len(rows),
                "wet_station_days": sum(x["historical_measurable_rain"] for x in rows), "brier": _brier(rows, candidate),
                "tenths_mm_positive_below_0p01_in": sum(0 < r["observed_precipitation"]["precip_tenths_mm"] < 3 for r in rows)}
               for phase, rows in phases.items() if phase != "regression fit" for candidate in METHODS]
    current, examples = [], []
    outlook, edition = outlooks.get(TARGET_YEAR, ({}, None))
    for station, matched in native.items():
        if station not in weather:
            continue
        reports = weather[station]
        values = {METHODS[0]: _prior(reports, *CLIMATE_YEARS), METHODS[1]: _prior(reports, *CLIMATE_YEARS, holiday_only=True)}
        if (prior := values[METHODS[0]]) is not None:
            values[METHODS[2]] = float(models[METHODS[2]].predict_proba([_model_features(prior, 0.0)[:1]])[0, 1])
            if station in outlook:
                values[METHODS[3]] = float(models[METHODS[3]].predict_proba([_model_features(prior, outlook[station]["signed_probability_anomaly"])])[0, 1])
        bid, ask = [decimal(matched[key]) if matched[key] is not None else None for key in ("yes_bid", "yes_ask")]
        midpoint = (float((ask + bid) / 2) if bid is not None and ask is not None and 0 <= bid <= ask <= 1
                    and (bid, ask) != (0, 1) else None)
        current.append({"station": station, "probability": values, "source_site": stations[station], "venue_market": matched,
                        "midpoint_proxy": midpoint, "CPC_region": outlook.get(station), "CPC_edition": edition})
    scored = [row for row in current if row["probability"].get(method) is not None]
    for row in sorted(scored, key=lambda r: abs((r["midpoint_proxy"] if r["midpoint_proxy"] is not None else r["probability"][method])
                                              - r["probability"][method]), reverse=True)[:12]:
        site, mark = row["station"], row["venue_market"]
        p = row["probability"][method]
        shown = f' Native displayed midpoint {row["midpoint_proxy"]:.0%}.' if row["midpoint_proxy"] is not None else ""
        examples.append({"station": site, "date": mark["day"], "at": mark["last_received_at"], "venue": mark["venue"],
                         "instrument_id": mark["instrument_id"], "title": f"{site} Thanksgiving measurable precipitation",
                         "detail": f"The earlier-selected physical estimate was {p:.0%}.{shown} The November 26 contract is pending.",
                         "data": row})
    provider_check = current_provider_check(data, weather)
    archive_evidence = {"native_contracts": len(native), "mapped_icao_stations": len(stations), "historical_station_files": len(weather),
                        "matched_2026_weather_projections": len(scored), "priced_2026_display_proxies": sum(r["midpoint_proxy"] is not None for r in scored),
                        "CPC_forecast_outlook_editions": len(outlooks), "historical_closed_venue_results_for_this_listing": 0,
                        "recent_source_check_station_days": len(provider_check),
                        "recent_source_check_differing_wet_labels": sum(not row["threshold_agrees"] for row in provider_check)}
    for name, source in (("faa-kdjt-in-26011.pdf", STATION_ALIASES["KDJT"]["faa_source"]),
                         ("nws-kdjt-pnsmfl.html", STATION_ALIASES["KDJT"]["nws_source"])):
        path = data / ROOT / name
        if path.is_file():
            proof[name] = _archived_proof(path, data, source)
    record = {"version": VERSION, "title": "Thanksgiving precipitation from long-range NOAA evidence",
              "hypothesis": "CPC’s issued week-two precipitation category improves an exact Thanksgiving-day rain probability beyond local climate.",
              "target": "Same-station Thanksgiving daily measurable precipitation. Current native rules pay on strictly positive reported accumulation and treat trace as zero.",
              "venues": [venue_archive.KALSHI], "date_window": {"from": "1999", "through": str(thanksgiving(TARGET_YEAR)), **PHASES},
              "metrics": [*metrics, archive_evidence], "cases": examples,
              "limits": ["CPC supplies the probability category for seven-day total precipitation. Logistic calibration learns its association with a separate one-day occurrence.",
                         "Historical weather uses NOAA quality-checked exact-identified station reports. The current contract names The Weather Company and is unresolved.",
                         "The two providers’ recent daily wet/dry comparison has actual report provenance and uses dates separate from the historical Thanksgiving test.",
                         "Historic CPC maps use their original issue date and declared valid window. The geospatial archive in this comparison begins in 2014.",
                         "Current venue midpoints are descriptive. No historical trading evidence for this current listing was assumed."],
              "provenance": {"engine": VERSION, "sources": proof, "source_gaps": gaps, "CPC_documentation": CPC_DOC,
                             "chosen_on_earlier_holidays": method, "forecast_lead_days": 11, "fit_years": PHASES["regression fit"], "climate_years": CLIMATE_YEARS,
                             "station_identity_policy": "Current and documented former ICAO codes matched to NOAA WBAN",
                             "verified_station_aliases": STATION_ALIASES,
                             "regression": {name: {"coefficients": model.coef_[0].tolist(), "intercept": float(model.intercept_[0])}
                                            for name, model in models.items()}}}
    return research_model.write_record(data, record, {"retained": phases, "current": current, "open_market_inventory": native,
                                                       "recent_provider_agreement": provider_check})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=venue_archive.DEFAULT_DATA)
    parser.add_argument("--fetch", action="store_true")
    args = parser.parse_args()
    saved = report(args.data, args.fetch)
    record = json.loads(saved.read_bytes())
    print(json.dumps({"path": str(saved), "metrics": record["metrics"], "source_gaps": record["provenance"]["source_gaps"]}, indent=2))


if __name__ == "__main__":
    main()
