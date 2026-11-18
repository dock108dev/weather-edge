"""Score operational named-storm wind forecasts against basin best tracks and native payouts."""

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import gzip
import json
from pathlib import Path
import re
from statistics import NormalDist, mean, stdev
import subprocess
import sys
from urllib.error import HTTPError, URLError

from sklearn import __version__ as sklearn_version
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from . import research_model, venue_archive, venue_storm
from .market import iso, parse_time
from .venue_sources import PublicGET

VERSION = "named-storm-intensity-v2"
EARLIER = "named-storm-intensity-v1-e1c8473b83dc06f7"
ATCF = "https://ftp.nhc.noaa.gov/atcf/archive/"
ATCF_FORMAT = "https://ftp.nhc.noaa.gov/atcf/README"
ISSUE = "https://www.nhc.noaa.gov/verification/verify2.shtml"
TERMS = "https://assets.kalshi.com/contract_terms/HURCAT.pdf"
NAME_LIST = "https://www.nhc.noaa.gov/aboutnames.shtml"
SOURCE = "https://www.nhc.noaa.gov/data/tcr/index.php?basin="
LIVE_FEEDS = {"North Atlantic": "atlantic", "Eastern Pacific": "epac", "Central Pacific": "cpac"}
FIT, CAL, TEST = (2010, 2017), (2018, 2019), (2020, 2025)
HOURS = (0, 24, 48)
WINDS = {74: 64, 96: 83, 111: 96, 130: 113, 157: 137}
PHYSICAL = {"TS", "SS", "HU"}
METHODS = {"prior": "Earlier storm peak climatology", "official": "Official five-day peak and earlier residuals",
           "logistic": "Earlier logistic on official forecast, current wind and trend",
           "boosted": "Earlier histogram gradient boosting"}
STORM = re.compile(r"^If (?:Storm |Tropical Storm |Hurricane )?([A-Za-z-]+) reaches maximum sustained winds", re.I)
ORIGINAL_WIND = re.compile(r"MAX SUSTAINED WINDS\s+(\d+)\s+KT", re.I)
ORIGINAL_TIME = re.compile(r"\b(\d{4}) UTC [A-Z]{3} ([A-Z]{3}) (\d{1,2}) (20\d{2})\b")
SPECIAL = ("2025-melissa", "2025-erin", "2024-helene", "2022-fiona")
CHANGES = {"HURCAT-IAN-T5": ("ian", "2022-09-28T11:00:00Z", 155, 140),
           "KXHURCAT-IMELDA-T2": ("imelda", "2025-10-01T15:00:00Z", 100, 80)}


def _archive_data(data):
    raw, receipt = venue_storm._existing(data, venue_storm.HURDAT_SOURCE.rsplit("/", 1)[-1])
    tracks = venue_storm.parse_hurdat(raw)
    basin = {}
    for year in range(venue_storm.CLIMATOLOGY_START, TEST[1] + 1):
        raw, _ = venue_storm._existing(data, f"season-summary/{year}.html")
        basin.update(venue_storm.parse_season_summary(raw, year))
    named = [track for track in tracks if (identity := track["id"]) in basin and basin[identity]["max_wind_kt"] >= 34
             and track["name"] not in ("UNNAMED", "UN-NAMED")]
    return named, basin, receipt


def _deck_locator(identity):
    file = f"a{identity.lower()}.dat.gz"
    return f"decks/{file}", f"{ATCF}{identity[-4:]}/{file}"


def _download(data, named):
    def get(track):
        source, uri = _deck_locator(track["id"])
        try:
            _, receipt = venue_storm._get(data, source, uri, folder="storm-intensity")
            return track["id"], receipt["sha256"]
        except (OSError, ValueError, HTTPError, URLError) as error:
            return track["id"], str(error)

    with ThreadPoolExecutor(max_workers=3) as pool:
        return dict(pool.map(get, [track for track in named if FIT[0] <= int(track["id"][-4:]) <= TEST[1]]))


def _coordinate(value, negative):
    return int(value[:-1]) / 10 * (-1 if value[-1:] == negative else 1)


def _advisories(raw, identity):
    by_issue = defaultdict(dict)
    for line in gzip.decompress(raw).splitlines():
        if b", OFCL," not in line:
            continue
        row = [cell.strip() for cell in line.decode("ascii", errors="replace").split(",")]
        if len(row) < 11 or "".join(row[:2]) != identity[:4] or row[2][:4] != identity[4:]:
            continue
        cycle = datetime.strptime(row[2], "%Y%m%d%H").replace(tzinfo=timezone.utc)
        horizon = int(row[5])
        try:
            wind = int(row[8])
            latitude, longitude = _coordinate(row[6], "S"), _coordinate(row[7], "W")
        except ValueError:
            continue
        if not 0 < wind <= 200 or not 0 <= horizon <= 120:
            continue
        item = (wind, row[10], latitude, longitude)
        by_issue[cycle].setdefault(horizon, set()).add(item)
    output = []
    for cycle, options in sorted(by_issue.items()):
        result = {horizon: next(iter(candidates)) for horizon, candidates in options.items() if len(candidates) == 1}
        zero = result.get(3) or result.get(0)
        if zero is None or zero[1] not in PHYSICAL or zero[0] < 34:
            continue
        output.append({"initialized_at": iso(cycle), "nominal_issue_at": iso(cycle + timedelta(hours=3)),
                       "current": {"wind_kt": zero[0], "type": zero[1], "latitude": zero[2], "longitude": zero[3]},
                       "lead_values": {str(hour): {"wind_kt": val[0], "type": val[1], "latitude": val[2], "longitude": val[3]}
                                       for hour, val in result.items()}})
    return output


def _snapshots(data, named, basin):
    output, gaps, receipts = [], {}, {}
    for storm in named:
        identity = storm["id"]
        year = int(identity[-4:])
        if not FIT[0] <= year <= TEST[1]:
            continue
        try:
            raw, receipt = venue_storm._existing(data, _deck_locator(identity)[0], folder="storm-intensity")
            forecasts = _advisories(raw, identity)
        except (OSError, ValueError) as error:
            gaps[identity] = str(error)
            continue
        if not forecasts:
            gaps[identity] = "No unambiguous operational named-storm OFCL cycles"
            continue
        receipts[identity] = receipt
        early = parse_time(forecasts[0]["nominal_issue_at"])
        for hours in HOURS:
            cutoff = early + timedelta(hours=hours, minutes=1)
            known = [row for row in forecasts if parse_time(row["nominal_issue_at"]) < cutoff]
            latest = known[-1]
            if cutoff - parse_time(latest["nominal_issue_at"]) > timedelta(hours=9):
                continue
            previous = next((row["current"]["wind_kt"] for row in reversed(known)
                             if parse_time(row["nominal_issue_at"]) <= cutoff - timedelta(hours=23)), known[0]["current"]["wind_kt"])
            reached = max(row["current"]["wind_kt"] for row in known)
            predicted = max([reached, *(item["wind_kt"] for item in latest["lead_values"].values() if item["type"] in PHYSICAL)])
            physical = basin[identity]
            output.append({"id": identity, "name": storm["name"], "year": year, "at": iso(cutoff), "hours": hours,
                           "forecast": latest, "official_peak_kt": predicted, "all_observed_to_now_kt": reached,
                           "trend_kt": latest["current"]["wind_kt"] - previous, "best_track_peak_kt": physical["max_wind_kt"],
                           "best_track": physical, "source": receipt})
    return output, gaps, receipts


def _features(row, threshold):
    now = row["forecast"]["current"]
    return [threshold, row["official_peak_kt"] - threshold, row["all_observed_to_now_kt"] - threshold,
            row["trend_kt"], now["latitude"], now["longitude"], now["wind_kt"], float(row["at"][5:7])]


def _panel(rows, hours):
    fit = [r for r in rows if r["hours"] == hours and FIT[0] <= r["year"] <= FIT[1]]
    calibration = [r for r in rows if r["hours"] == hours and CAL[0] <= r["year"] <= CAL[1]]
    train = [(r, th, int(r["best_track_peak_kt"] >= th)) for r in fit for th in WINDS.values() if r["all_observed_to_now_kt"] < th]
    X, ys = ([_features(r, th) for r, th, _ in train], [y for _, _, y in train])
    return {"prior": [r["best_track_peak_kt"] for r in fit],
            "residuals": [r["best_track_peak_kt"] - r["official_peak_kt"] for r in calibration],
            "logistic": make_pipeline(StandardScaler(), LogisticRegression(C=0.7)).fit(X, ys),
            "boosted": HistGradientBoostingClassifier(max_iter=65, max_leaf_nodes=7, min_samples_leaf=25,
                                                      l2_regularization=1.5, random_state=7).fit(X, ys)}


def _prediction(panel, row, threshold):
    if row["all_observed_to_now_kt"] >= threshold:
        return dict.fromkeys(METHODS, 1.0)
    residuals = panel["residuals"]
    central = row["official_peak_kt"] + mean(residuals)
    probability = 1 - NormalDist(central, max(8, stdev(residuals))).cdf(threshold - 0.5)
    return {"prior": (sum(number >= threshold for number in panel["prior"]) + 0.5) / (len(panel["prior"]) + 1),
            "official": probability,
            **{k: float(panel[k].predict_proba([_features(row, threshold)])[0, 1]) for k in ("logistic", "boosted")}}


def _contracts(data):
    with venue_archive.connect(data) as db:
        return [dict(row) for row in db.execute(
            f"SELECT {venue_archive.PUBLIC_SELECT} FROM markets WHERE venue=? AND family='storm-intensity' ORDER BY instrument_id",
            (venue_archive.KALSHI,),
        )]


def _terms(contract):
    rule = STORM.search(contract["rules_primary"] or "")
    if rule and contract["day"] and contract["lower"] and int(contract["lower"]) in WINDS:
        return rule[1].upper(), int(contract["day"][:4]), int(contract["lower"])
    return None


def _prequote(data, contract, row):
    return venue_storm._pre_decision_price(data, contract, parse_time(row["at"]))


def _live_sources(data):
    raw, receipt = venue_storm._existing(data, "name-list.html", folder="storm-intensity")
    rows = venue_storm.Page(raw).rows
    if len(rows) < 3 or "Arthur" not in rows[0][0] or "Amanda" not in rows[1][0] or "Ulana" not in rows[2][0]:
        raise ValueError("Unexpected NHC naming table")
    names = {"North Atlantic": rows[0][0].split(), "Eastern Pacific": rows[1][0].split(),
             "Central Pacific": [name for group in rows[2] for name in group.split()]}
    known = {}
    for region, label in LIVE_FEEDS.items():
        raw, value = venue_storm._existing(data, f"{label}-2026.html", folder="storm-intensity")
        declared = [row for row in venue_storm.Page(raw).rows if len(row) == 10 and row[0].isdigit() and row[4].isdigit()]
        known[region] = {"reported": [{"name": row[1], "wind_kt": int(row[4]), "type": row[2], "dates": row[3], "status": row[-1]}
                                      for row in declared], "receipt": value}
    return {"season_summary": known, "regular_names": names, "naming_receipt": receipt}


def _original_reports(data):
    observed = []
    for title in SPECIAL:
        for ordinal in (1, 4, 8):
            try:
                raw, receipt = venue_storm._existing(data, f"{title}-advisory-{ordinal}.html", folder="storm-intensity")
            except OSError:
                continue
            page = venue_storm.Page(raw).text()
            wind, hour = ORIGINAL_WIND.search(page), ORIGINAL_TIME.search(page)
            if wind and hour:
                issued = datetime.strptime(f"{hour[1]} {hour[2]} {hour[3]} {hour[4]}", "%H%M %b %d %Y").replace(tzinfo=timezone.utc)
                observed.append({"storm": title.split("-", 1)[1].upper(), "year": int(title[:4]), "advisory": ordinal,
                                 "issued_at": iso(issued), "original_kt": int(wind[1]), "receipt": receipt})
    return observed


def _changed_intensity(data):
    updates = {}
    for instrument, (name, at, expected_mph, expected_kt) in CHANGES.items():
        posted, receipt = venue_storm._existing(data, f"{name}-operational.html", folder="storm-intensity")
        pdf, revised = venue_storm._existing(data, f"{name}-tcr.pdf", folder="storm-intensity")
        bulletin = venue_storm.Page(posted).text()
        mph = re.search(r"MAXIMUM SUSTAINED WINDS\.{3}(\d+) MPH", bulletin)
        utc_clock = parse_time(at).strftime("%H%M") + " UTC"
        text = subprocess.run(["pdftotext", "-layout", "-", "-"], input=pdf, capture_output=True, check=True).stdout.decode()
        print_wind = re.search(fr"peak intensity of ({expected_kt}) kt", text, re.I)
        edition = re.search(r"\b\d{1,2} [A-Z][a-z]+ 20\d{2}\b", text[:800])
        if mph is None or int(mph[1]) != expected_mph or utc_clock not in bulletin or print_wind is None:
            raise ValueError(f"Named-storm operational and post-season evidence did not verify for {instrument}")
        updates[instrument] = {"operational": {"mph": expected_mph, "published_at": at, "receipt": receipt},
                               "post_season": {"knots": expected_kt, "retained_edition_date": edition[0] if edition else None,
                                               "receipt": revised}}
    return updates


def gather(data, *, candles=False):
    storms, basin, source = _archive_data(data)
    snapshots, gaps, forecast_receipts = _snapshots(data, storms, basin)
    index = {(storm["name"].upper(), int(storm["id"][-4:])): storm for storm in storms}
    by_id = defaultdict(list)
    for row in snapshots:
        by_id[row["id"]].append(row)
    current, originals = _live_sources(data), _original_reports(data)
    contracts = _contracts(data)
    if candles:
        get = PublicGET(data)
        received = 0
        for contract in contracts:
            named = _terms(contract)
            qualifies = named and (named[:2] in index or contract["status"] == "open")
            if qualifies and contract["opened_at"]:
                received += venue_storm._receive_candle(data, get, contract, first_at=parse_time(contract["opened_at"]))
        print(f"Received {received} named-storm daily candle points", flush=True)
    cases = []
    for contract in contracts:
        term = _terms(contract)
        if not term:
            continue
        name, year, mph = term
        track = index.get((name, year))
        entry = {"contract": contract, "name": name, "year": year, "threshold_mph": mph, "threshold_kts": WINDS[mph],
                 "reference_track": basin[track["id"]] if track else None, "native_payout": contract["yes_payout"], "prices": []}
        if track and contract["opened_at"]:
            summary = basin[track["id"]]
            occ = [row for row in track["observations"] if row["category"] in PHYSICAL and
                   summary["first_date"] <= row["at"][:10] <= min(contract["day"], summary["last_date"]) and
                   parse_time(row["at"]) >= parse_time(contract["opened_at"]) and row["wind_kt"] <= summary["max_wind_kt"]]
            observed = max(occ, key=lambda row: row["wind_kt"], default=None)
            if observed:
                entry["qualified_best_track_peak"] = observed
                entry["weather_implied"] = int(observed["wind_kt"] >= WINDS[mph])
            entry["prices"] = [{"forecast": frame, "display": _prequote(data, contract, frame)} for frame in by_id[track["id"]]
                               if contract["closed_at"] and parse_time(contract["opened_at"]) <= parse_time(frame["at"]) < parse_time(contract["closed_at"])]
            entry["original_advisories"] = [item for item in originals if item["storm"] == name and item["year"] == year]
        if contract["status"] == "open":
            entry["official_naming_basin_candidates"] = [region for region, choices in current["regular_names"].items()
                                                          if name in {choice.upper() for choice in choices}]
            entry["official_2026_report_matches"] = {region: [storm for storm in item["reported"] if storm["name"].upper() == name]
                                                      for region, item in current["season_summary"].items()}
        cases.append(entry)
    return {"version": VERSION, "rows": snapshots, "gaps": gaps, "forecast_receipts": forecast_receipts,
            "best_track_receipt": source, "native": cases, "current": current, "original_advisories": originals}


def _metrics(rows, name):
    if not rows:
        return []
    return [{"cohort": name, "model": METHODS.get(method, method), "n": len(rows),
             "storms": len({row["id"] for row in rows}), "seasons": len({row["year"] for row in rows}),
             "brier": mean((row["p"][method] - row["outcome"])**2 for row in rows)} for method in rows[0]["p"]]


def evaluate(data, corpus):
    if "governing_source_changes" not in corpus:
        corpus["governing_source_changes"] = _changed_intensity(data)
    corpus["version"] = VERSION
    frozen, digest = research_model.freeze_inputs(data, corpus)
    fitted = {hours: _panel(corpus["rows"], hours) for hours in HOURS}
    test = [{**row, "threshold": threshold, "outcome": int(row["best_track_peak_kt"] >= threshold),
             "p": _prediction(fitted[row["hours"]], row, threshold)} for row in corpus["rows"]
            if TEST[0] <= row["year"] <= TEST[1] for threshold in WINDS.values()]
    selective = [row for row in test if row["all_observed_to_now_kt"] < row["threshold"]]
    metrics = _metrics(test, "Equal earlier-initiation and later fixed storm phases") + _metrics(selective, "Before the observed threshold")
    for phase in HOURS:
        metrics += _metrics([row for row in selective if row["hours"] == phase], f"{phase} hours after first qualifying advisory")
    priced, payout_counts = [], Counter()
    for row in corpus["native"]:
        if row["native_payout"] in ("0", "1"):
            payout_counts["native payouts"] += 1
            if row["reference_track"]:
                payout_counts["exact named Atlantic history"] += 1
            if "weather_implied" in row:
                payout_counts["qualified best-track comparison"] += 1
                payout_counts["best-track versus native difference"] += int(row["weather_implied"] != int(row["native_payout"]))
            for price in row["prices"]:
                if price["display"] and TEST[0] <= row["year"] <= TEST[1]:
                    frame = price["forecast"]
                    priced.append({**frame, "native_instrument": row["contract"]["instrument_id"], "threshold": row["threshold_kts"],
                                   "outcome": int(row["native_payout"]), "candle": price["display"],
                                   "p": {**_prediction(fitted[frame["hours"]], frame, row["threshold_kts"]),
                                         "Kalshi candle midpoint": price["display"]["probability"]}})
    metrics += _metrics(priced, "Same native payout and market clock")
    candidates = {"HURCAT-FIONA-T3", "HURCAT-HELENE-T4", "KXHURCAT-MELISSA-T5", "KXHURCAT-ERIN-T5",
                  "KXHURCAT-26KYLE-T1", "KXHURCAT-26ULANA-T1", "KXHURCAT-26WINNIE-T1", *CHANGES}
    cases = []
    for row in corpus["native"]:
        contract = row["contract"]
        if contract["instrument_id"] not in candidates:
            continue
        if known := row.get("qualified_best_track_peak"):
            text = (f"NHC best track recorded {known['wind_kt']} knots within the eligible named-storm window. "
                    f"Native YES paid ${row['native_payout']}.")
        else:
            base = ", ".join(row.get("official_naming_basin_candidates", [])) or "no name-list match"
            text = f"NHC regular naming table includes {row['name'].title()} in {base}. Native settlement is pending."
        if sources := corpus["governing_source_changes"].get(contract["instrument_id"]):
            original = sources["operational"]
            text = (f"The operational NHC advisory gave {original['mph']} mph at {original['published_at']}. "
                    f"Native YES paid ${row['native_payout']}. The later NHC report gives {sources['post_season']['knots']} knots "
                    "and would imply the opposite result at the native category threshold.")
        cases.append({"title": f"{row['name'].title()} ≥{row['threshold_mph']} mph", "venue": venue_archive.KALSHI,
                      "instrument_id": contract["instrument_id"], "date": contract["day"], "at": contract["settlement_at"],
                      "detail": text, "data": {**row, "independent_public_source_evidence": sources}})
    live = [item for item in corpus["native"] if item["contract"]["status"] == "open"]
    result = {"version": VERSION, "title": "Named-storm intensity from issued forecasts and best tracks",
              "hypothesis": "Operational intensity trajectories improve over earlier storm climatology before a category is reached.",
              "target": "Native wind category thresholds and NHC Atlantic named-storm peak", "venues": ["Kalshi"],
              "supersedes": EARLIER,
              "date_window": {"fit seasons": FIT, "calibration seasons": CAL, "held seasons": TEST},
              "coverage": {"ATCF storm forecasts": len(corpus["forecast_receipts"]), "held_cases": len(test),
                           "before_threshold_cases": len(selective), "held_storms": len({r["id"] for r in test}),
                           "native_strikes": len(corpus["native"]), "historical_payout_audit": dict(payout_counts),
                           "native_quoted_decisions": len(priced), "native_open_strikes": len(live),
                           "open_named_storms_on_2026_coverage": len({r["name"] for r in live if any(r["official_2026_report_matches"].values())}),
                           "confirmed_original_public_advisories": len(corpus["original_advisories"]) + len(corpus["governing_source_changes"])},
              "missing": {"invalid, unavailable, or non-named ATCF storm records": len(corpus["gaps"])},
              "metrics": metrics, "cases": cases,
              "limits": ["Forecast clocks use the documented NHC routine +3 hours after the synoptic cycle. Special advisory and original historical receipt times may differ.",
                         "Best track is a retrospective physical observation and can differ from operational governing reports. Native payouts remain separate.",
                         "The forecast benchmarks begin after a named storm exists. A listed name with no current forecast requires a separate formation probability.",
                         "The series filing lists a TWC and NHC hierarchy. Older native primary text explicitly identifies NHC. Cash payouts take priority in the venue comparison.",
                         "Candle midpoint is a display probability. Historical executable size is unavailable."],
              "provenance": {"frozen_input": str(frozen.relative_to(data)), "frozen_input_sha256": digest, "terms": TERMS,
                             "operational_source": ATCF, "format": ATCF_FORMAT, "issue_schedule": ISSUE,
                             "best_track": corpus["best_track_receipt"], "current": corpus["current"],
                             "scikit_learn": sklearn_version, "source_sha256": research_model.run_sources(sys.modules[__name__], venue_storm)}}
    return research_model.write_record(data, result, {"held_thresholds": test, "native": corpus["native"],
                                                      "native_display_and_payouts": priced, "official_gaps": corpus["gaps"],
                                                      "original_advisories": corpus["original_advisories"],
                                                      "governing_source_changes": corpus["governing_source_changes"]})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--input", type=Path)
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--candles", action="store_true")
    args = parser.parse_args(argv)
    if args.download:
        named, _, _ = _archive_data(args.data)
        print(f"{len(_download(args.data, named))} official deck source paths checked", flush=True)
    corpus = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else gather(args.data, candles=args.candles)
    print(evaluate(args.data, corpus), flush=True)


if __name__ == "__main__":
    main()
