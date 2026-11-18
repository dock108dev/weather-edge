"""Replay Hoover Dam month-end elevations using public observations and dated scenarios."""

import argparse
from calendar import month_abbr, monthrange
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import gzip
import json
from math import cos, pi, sin
from pathlib import Path
import re
from statistics import NormalDist, mean, stdev
import subprocess
import sys
from urllib.error import HTTPError, URLError

from sklearn import __version__ as sklearn_version
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from . import research_model, venue_archive, venue_storm
from .market import decimal, iso, parse_time
from .venue_sources import PublicGET

VERSION = "lake-mead-elevation-v2"
EARLIER = "lake-mead-elevation-v1-b2a8246c80ec895f"
TERMS = "https://assets.kalshi.com/contract_terms/MEAD.pdf"
BOR = "https://www.usbr.gov/lc/region/g4000/"
MONTHLY = BOR + "hourly/mead-elv.html"
DAILY_ALTERNATE = BOR + "hourly/levels.html"
DAILY = "https://data.usbr.gov/rise/api/result/download?itemId=6123"
DAILY_CATALOG = "https://data.usbr.gov/catalog/4370/item/6123"
PROJECTIONS = BOR + "riverops/24ms-projections.html"
ARCHIVE = BOR + "24mo/"
SOURCE_DATE = "20261115"
FIT, CAL, TEST = (2010, 2017), (2018, 2020), (2021, 2026)
PHASES = {"Previous month on day 15": (-1, 15), "Target month on day 1": (0, 1),
          "Target month on day 15": (0, 15), "Target month on day 22": (0, 22)}
LATEST_SCENARIOS = {"Bureau most probable, 6 maf": "most6", "Bureau most probable, 7 maf": "most7",
                    "Bureau dry scenario": "min", "Bureau wet scenario": "max"}
LABELS = {"persistence": "Current observed elevation", "seasonal": "Current elevation and historical calendar-month change",
          "ridge": "Earlier Ridge on recent elevation changes", "bureau": "Bureau of Reclamation as-issued scenario",
          "bureau_adjusted": "Bureau scenario adjusted for observed change since month-end"}
SCENARIOS = {"most6": "24mo_6.pdf", "most7": "24mo_7.pdf", "min": "24mo_MIN.pdf", "max": "24mo_MAX.pdf"}
COMMON = {f"administrative-month-end-{SOURCE_DATE}.html": MONTHLY,
          f"alternative-daily-{SOURCE_DATE}.html": DAILY_ALTERNATE,
          f"RISE-6123-{SOURCE_DATE}.json": DAILY,
          f"latest-metadata-{SOURCE_DATE}.html": PROJECTIONS,
          **{f"issues/2026-10-{name}.pdf": BOR + path for name, path in SCENARIOS.items()}}
TABLE_TITLE = re.compile(r"^\s*Hoover Dam\s*[-–]\s*Lake Mead\s*$", re.M | re.I)
ROW = re.compile(r"^\s*(?:\*\s*)?([A-Z][a-z]{2})\s+(20\d{2})\s+(.+)$")
NUMBER = re.compile(r"[-+]?\d+(?:\.\d+)?")
MONTHS = {name: index for index, name in enumerate(month_abbr) if name}


def _obtain(data, name, url):
    return venue_storm._get(data, name, url, folder="mead")


def _documents():
    return {f"issues/{year}-{month:02}.pdf": f"{ARCHIVE}{year}/{month_abbr[month].upper()}{year%100:02}.pdf"
            for year in range(2010, 2027) for month in range(1, 13) if (year, month) <= (2026, 9)}


def _fetch(data):
    def get(pair):
        name, url = pair
        try:
            _obtain(data, name, url)
        except (HTTPError, URLError, TimeoutError, ValueError) as error:
            return name, str(error)
        return name, None

    with ThreadPoolExecutor(max_workers=3) as pool:
        return {name: error for name, error in pool.map(get, {**COMMON, **_documents()}.items()) if error}


def _monthly(raw):
    output = {}
    for row in venue_storm.Page(raw).rows:
        if len(row) != 13 or not re.fullmatch(r"(?:19|20)\d{2}", row[0]):
            continue
        year = int(row[0])
        for month, value in enumerate(row[1:], 1):
            if NUMBER.fullmatch(value):
                output[date(year, month, 1)] = value
    if len(output) < 1_000:
        raise ValueError("Bureau monthly settlement table was incomplete")
    return output


def _daily(raw):
    source = json.loads(raw, parse_float=Decimal)
    if (source["Location"]["Name"] != "Lake Mead Hoover Dam and Powerplant" or source["Units"] != "ft" or
            source["Timestep"] != "daily" or "presented as UTC" not in source["Note"]):
        raise ValueError("Bureau RISE station, measurement or clock mismatch")
    rows = {}
    for key, row in source.items():
        if key.isdigit() and row["resultType"] == "observed" and row["timeStep"] == "day":
            timestamp = datetime.fromisoformat(row["dateTime"]).replace(tzinfo=timezone.utc)
            day = timestamp.date()
            if day in rows:
                raise ValueError(f"Bureau RISE duplicate daily row at {day}")
            rows[day] = {"at": iso(timestamp), "elevation": str(row["result"])}
    return rows


def _study_pdf(raw, issue_month):
    info = subprocess.run(["pdfinfo", "-isodates", "-"], input=raw, capture_output=True, check=True).stdout.decode()
    stamped = []
    for match in re.finditer(r"^(?:CreationDate|ModDate):\s+(\d{4}-\d{2}-\d{2})", info, re.M):
        candidate = date.fromisoformat(match[1])
        if candidate.replace(day=1) == issue_month:
            stamped.append(candidate)
    if not stamped:
        raise ValueError("Study PDF creation/modification date unavailable within labeled issue month")
    text = subprocess.run(["pdftotext", "-layout", "-", "-"], input=raw, capture_output=True, check=True).stdout.decode()
    may_be_title = re.search(r"\b[A-Z][a-z]{2,8} 20\d{2} (?:\S+ )?24.Month Study", text, re.I)
    if may_be_title is None or issue_month.strftime("%B %Y").lower() not in may_be_title[0].lower():
        raise ValueError("Study PDF internal issue month does not match archive partition")
    if not (header := TABLE_TITLE.search(text)):
        raise ValueError("Hoover Dam – Lake Mead table missing")
    # Read just the first report page for Hoover Dam. It contains at least one
    # future month plus the printed reservoir-elevation column and history.
    area = text[header.end():].split("\f", maxsplit=1)[0]
    rows = {}
    for line in area.splitlines():
        matched = ROW.match(line)
        if matched and (month := MONTHS.get(matched[1])):
            stamp = date(int(matched[2]), month, 1)
            values = NUMBER.findall(matched[3].replace(",", ""))
            if len(values) == 10 and 600 < float(values[-2]) < 1300:
                rows[stamp.isoformat()] = {"elevation": values[-2], "raw": line.strip()}
    if len(rows) < 18:
        raise ValueError("Too few rows validated in the Lake Mead projection table")
    # The PDF metadata clock is a production date. Wait three complete local days
    # as a nominal release bound and retain it as a proxy, never an actual receipt.
    available = datetime.combine(max(stamped) + timedelta(days=4), time(8), timezone.utc)
    if issue_month == date(2026, 10, 1):
        if re.search(r"\bOctober 15, 2026\b", text):
            available = datetime(2026, 10, 16, 8, tzinfo=timezone.utc)
    return {"issue_month": issue_month.isoformat(), "pdf_production_day": str(max(stamped)),
            "nominal_available_after": iso(available), "rows": rows}


def _load_forecasts(data):
    outputs, gaps = {}, {}
    for name, url in {**_documents(), **{key: val for key, val in COMMON.items() if key.endswith(".pdf")}}.items():
        month = name.rsplit("/", 1)[-1][:7]
        try:
            raw, source = venue_storm._existing(data, name, folder="mead")
            values = _study_pdf(raw, date.fromisoformat(month + "-01"))
        except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
            gaps[name] = str(error)
            continue
        outputs[Path(name).stem] = {**values, "source": source, "public_pdf": url}
    return outputs, gaps


def _shift(month, delta):
    year, index = divmod(month.year * 12 + month.month - 1 + delta, 12)
    return date(year, index + 1, 1)


def _exact_dated_forecast(forecasts, target, clock):
    for offset in range(0, -4, -1):
        month = _shift(target, offset)
        key = month.isoformat()[:7]
        report = forecasts.get(key) or forecasts.get(f"{key}-most6")
        if report and target.isoformat() in report["rows"] and parse_time(report["nominal_available_after"]) <= clock:
            return {"issue": report["issue_month"], "availability": report["nominal_available_after"],
                    "receipt": report["source"], "path": report["rows"], **report["rows"][target.isoformat()]}
    return None


def _read(rows, stamp):
    row = rows.get(stamp)
    return None if row is None else float(row["elevation"])


def _frame(daily, actual, reports, month, phase, *, live_receipt=None):
    offset, planned_day = PHASES[phase]
    clock = datetime.combine(_shift(month, offset).replace(day=planned_day), time(18), timezone.utc)
    if live_receipt:
        clock = parse_time(live_receipt["received_at"])
    last = clock.date() - timedelta(days=2 if live_receipt is None else 0)
    while last not in daily and (clock.date() - last).days < 7:
        last -= timedelta(days=1)
    levels = [_read(daily, last - timedelta(days=lag)) for lag in (0, 7, 14, 28)]
    if any(level is None for level in levels):
        return None
    published = _exact_dated_forecast(reports, month, clock)
    ending = month.replace(day=monthrange(month.year, month.month)[1])
    now = levels[0]
    adjusted = None
    if published:
        path = published.pop("path")
        last_month = last.replace(day=1)
        prior_level, next_level = [path.get(_shift(last_month, offset).isoformat()) for offset in (-1, 0)]
        if prior_level and next_level:
            fraction = last.day / monthrange(last.year, last.month)[1]
            planned = (1 - fraction) * float(prior_level["elevation"]) + fraction * float(next_level["elevation"])
            adjusted = float(published["elevation"]) + 0.6 * (now - planned)
    return {"month": month.isoformat(), "year": month.year, "phase": phase, "clock": iso(clock),
            "actual": float(actual[month]) if month in actual else None,
            "now": now, "delta7": now - levels[1], "delta14": now - levels[2], "delta28": now - levels[3],
            "days_left": (ending - last).days, "weather_observation": daily[last], "bureau": published,
            "bureau_adjusted": adjusted}


def _features(row):
    angle = 2 * pi * int(row["month"][5:7]) / 12
    return [row["now"], row["delta7"], row["delta14"], row["delta28"], row["days_left"], sin(angle), cos(angle)]


def _holdouts(rows):
    return {name: [row for row in rows if first <= row["year"] <= last and row["actual"] is not None]
            for name, (first, last) in {"fit": FIT, "cal": CAL, "test": TEST}.items()}


def _model(rows):
    parts = _holdouts(rows)
    training = parts["fit"]
    model = make_pipeline(StandardScaler(), Ridge(alpha=2.0)).fit(
        [_features(row) for row in training], [row["actual"] - row["now"] for row in training],
    )
    changes = {month: mean(row["actual"] - row["now"] for row in training if int(row["month"][5:7]) == month)
               for month in range(1, 13)}
    return model, changes, parts["cal"]


def _point_predictions(row, model, changes):
    now = row["now"]
    return {"persistence": now, "seasonal": now + changes[int(row["month"][5:7])],
            "ridge": now + float(model.predict([_features(row)])[0]),
            "bureau": float(row["bureau"]["elevation"]) if row["bureau"] else None,
            "bureau_adjusted": row["bureau_adjusted"]}


def _fit_panel(rows):
    model, changes, calibration = _model(rows)
    errors = {name: [] for name in LABELS}
    for row in calibration:
        for name, point in _point_predictions(row, model, changes).items():
            if point is not None:
                errors[name].append(row["actual"] - point)
    return model, changes, errors


def _predictions(row, fitted, threshold):
    model, changes, errors = fitted
    estimates = _point_predictions(row, model, changes)
    predictions = {}
    for name, point in estimates.items():
        samples = errors[name]
        if point is not None and len(samples) >= 5:
            corrected = point + mean(samples) if name in ("ridge", "bureau_adjusted") else point
            spread = max(0.18, stdev(samples))
            predictions[name] = {"probability": 1 - NormalDist(corrected, spread).cdf(threshold), "center": corrected,
                                 "residual_sigma": spread, "calibration_months": len(samples)}
    return predictions


def _contracts(data):
    with venue_archive.connect(data) as db:
        return [dict(row) for row in db.execute(
            f"SELECT {venue_archive.PUBLIC_SELECT} FROM markets WHERE venue=? AND family='reservoir-elevation' ORDER BY day,instrument_id",
            (venue_archive.KALSHI,),
        )]


def _audit(contract, monthly, daily):
    month = date.fromisoformat(contract["day"])
    expected = monthly.get(month)
    day = month.replace(day=monthrange(month.year, month.month)[1])
    observed = daily.get(day)
    native = (contract["expiration_value"] or "").strip()
    threshold = contract["lower"] or contract["upper"]
    p = contract["yes_payout"]
    implied = None if p is None or expected is None or threshold is None else int(
        decimal(expected) > decimal(threshold) if contract["operator"] == "above" else decimal(expected) < decimal(threshold))
    return {"native_payout": p, "primary_observed": expected, "native_expiration_value": native or None,
            "verified_numeric_match": decimal(expected) == decimal(native) if expected and NUMBER.fullmatch(native) else None,
            "bureau_rise_month_end_observation": observed, "primary_rule_matches_native_payout": implied == int(p) if implied is not None else None}


def _score(rows, name):
    if not rows:
        return []
    models = tuple(rows[0]["predictions"])
    return [{"cohort": name, "model": LABELS.get(method, method), "n": len(rows), "months": len({row["month"] for row in rows}),
             "calendar_years": len({row["year"] for row in rows}),
             "mae": mean(abs(row["predictions"][method]["center"] - row["actual"]) for row in rows)
                    if all(row["predictions"][method]["center"] is not None for row in rows) else None,
             "brier": mean((row["predictions"][method]["probability"] - row["outcome"])**2 for row in rows)}
            for method in models]


def _case(row, prospective, predictions):
    contract, audit = row["contract"], row["audit"]
    label = contract["instrument_id"]
    if observed := audit["primary_observed"]:
        description = f"Bureau month-end observation {observed} ft. Native YES paid ${contract['yes_payout']}."
    else:
        description = f"December 2026 is pending. Bureau scenarios range {prospective['extent_min']}–{prospective['extent_max']} ft."
    if contract["operator"] == "conflicting":
        description += " Primary rule and native label disagree on strictness at equality."
    return {"title": label, "venue": venue_archive.KALSHI, "instrument_id": label, "date": contract["day"],
            "at": contract["settlement_at"], "detail": description,
            "data": {**row, "prospective_predictions": predictions, "current_scenarios": prospective["official_scenarios"] if predictions else None}}


def gather(data):
    monthly, source_month = venue_storm._existing(data, f"administrative-month-end-{SOURCE_DATE}.html", folder="mead")
    daily, source_day = venue_storm._existing(data, f"RISE-6123-{SOURCE_DATE}.json", folder="mead")
    months, days = _monthly(monthly), _daily(daily)
    reports, gaps = _load_forecasts(data)
    frames = [row for month in sorted(months) if FIT[0] <= month.year <= TEST[1]
              for phase in PHASES if (row := _frame(days, months, reports, month, phase))]
    target = date(2026, 12, 1)
    live = _frame(days, months, reports, target, "Previous month on day 15", live_receipt=source_day)
    scenarios = {name: {**rows["rows"][target.isoformat()], "receipt": rows["source"]} for name, suffix in LATEST_SCENARIOS.items()
                 if (rows := reports.get(f"2026-10-{suffix}")) and target.isoformat() in rows["rows"]}
    shown = {"extent_min": min((choice["elevation"] for choice in scenarios.values()), key=decimal, default=None),
             "extent_max": max((choice["elevation"] for choice in scenarios.values()), key=decimal, default=None),
             "official_scenarios": scenarios, "observation_forecast_frame": live, "active_native_estimates": {}}
    contracts = _contracts(data)
    audits = [{"contract": contract, "audit": _audit(contract, months, days)} for contract in contracts if contract["day"]]
    for row in audits:
        contract = row["contract"]
        if contract["status"] == "open":
            right = parse_time(source_day["received_at"])
            row["book"] = venue_archive.books(data, venue_archive.KALSHI, contract["instrument_id"],
                                              start=right - timedelta(minutes=30), end=right + timedelta(minutes=10), limit=2)
            row["candle"] = venue_storm._pre_decision_price(data, contract, right)
        elif audit := row.get("audit"):
            if audit["primary_rule_matches_native_payout"]:
                row["candle_clocks"] = {frame["clock"]: point for frame in frames if frame["month"] == contract["day"]
                                        if (point := venue_storm._pre_decision_price(data, contract, parse_time(frame["clock"])))}
    return {"version": VERSION, "sources": {"monthly": source_month, "daily": source_day},
            "daily_rows": len(days), "months": len(months), "frozen_reports": len(reports), "gaps": gaps,
            "frames": frames, "contracts": audits, "prospective": shown}


def evaluate(data, corpus):
    corpus["version"] = VERSION
    frozen, digest = research_model.freeze_inputs(data, corpus)
    frames = corpus["frames"]
    models = {phase: _fit_panel([row for row in frames if row["phase"] == phase]) for phase in PHASES}
    test = _holdouts(frames)["test"]
    outcomes = []
    for row in test:
        fitted = models[row["phase"]]
        for shift in (-3, -1.5, 0, 1.5, 3):
            threshold = round(row["now"] * 2) / 2 + shift
            predictions = _predictions(row, fitted, threshold)
            if len(predictions) == len(LABELS):
                outcomes.append({**row, "threshold": threshold, "outcome": int(row["actual"] > threshold), "predictions": predictions})
    metrics = _score(outcomes, "All identical later threshold cases with agency and weather")
    for phase in PHASES:
        metrics += _score([row for row in outcomes if row["phase"] == phase], phase)
    verified = Counter()
    native_forecasts, priced = [], []
    latest = corpus["prospective"]
    current = latest["observation_forecast_frame"]
    prospective = {}
    for row in corpus["contracts"]:
        contract, audit = row["contract"], row["audit"]
        if audit["native_payout"] in ("0", "1"):
            verified["verified binary payouts"] += 1
            verified["native rule conflicts"] += int(contract["operator"] == "conflicting")
        for field in ("verified_numeric_match", "primary_rule_matches_native_payout"):
            if audit[field] is not None:
                verified[field + " checked"] += 1
                verified[field + " disagreements"] += int(not audit[field])
        if current and contract["status"] == "open" and contract["lower"] is not None:
            prospective[contract["instrument_id"]] = _predictions(
                current, models[current["phase"]], float(contract["lower"]),
            )
        if not (audit["primary_rule_matches_native_payout"] and contract["operator"] in ("above", "below")):
            continue
        lower = contract["lower"] if contract["operator"] == "above" else contract["upper"]
        for observed in test:
            if observed["month"] != contract["day"] or not contract["opened_at"] <= observed["clock"] < contract["closed_at"]:
                continue
            estimates = _predictions(observed, models[observed["phase"]], float(lower))
            if contract["operator"] == "below":
                estimates = {key: {**value, "probability": 1 - value["probability"]} for key, value in estimates.items()}
            if len(estimates) == len(LABELS):
                native_forecasts.append({**observed, "instrument_id": contract["instrument_id"],
                                         "predictions": estimates, "outcome": int(audit["native_payout"])})
                if candle := row.get("candle_clocks", {}).get(observed["clock"]):
                    priced.append({**native_forecasts[-1], "candle": candle, "predictions": {**estimates,
                                   "Kalshi candle midpoint": {"probability": candle["probability"], "center": None}}})
    metrics += _score(native_forecasts, "Same verified native payout thresholds")
    metrics += _score(priced, "Same quoted native payout thresholds")
    picks = {"MEAD-22OCT-T1067", "KXMEAD-25JUL-1050", "KXMEAD-26MAY-A1050", "KXMEAD-26JUN-A1045", "KXMEAD-26DEC-A1035"}
    cases = [_case(row, latest, prospective.get(row["contract"]["instrument_id"]))
             for row in corpus["contracts"] if row["contract"]["instrument_id"] in picks]
    result = {"version": VERSION, "title": "Lake Mead end-of-month elevation and Bureau outlooks",
              "hypothesis": "Bureau operations guidance adds predictive value beyond recent observed elevation and seasonal change.",
              "target": "Hoover Dam observed month-end elevation in feet", "venues": ["Kalshi"],
              "supersedes": EARLIER,
              "date_window": {"fit": FIT, "calibration": CAL, "untouched later years": TEST},
              "coverage": {"daily observations": corpus["daily_rows"], "official month-end values": corpus["months"],
                           "verified past agency publications": corpus["frozen_reports"], "matched held threshold cases": len(outcomes),
                           "monthly outcomes": len({row["month"] for row in outcomes}), "native contracts": len(corpus["contracts"]),
                           "native threshold decisions": len(native_forecasts), "native candle decisions": len(priced),
                           "payout_audit": dict(verified),
                           "active native forecasts": len(prospective), "public scenario values": latest["official_scenarios"]},
              "missing": {"unavailable or unverified archived PDFs": len(corpus["gaps"])}, "cases": cases, "metrics": metrics,
              "limits": ["The PDFs are dated operational projections. Their production dates plus a conservative delay stand in for unavailable original publication timestamps.",
                         "Historical daily RISE values remain revisable. The replay uses a nominal two-day observation lag and separates native cash payouts.",
                         "Current wet and dry cases are agency hydrologic and release scenarios. Their spread is distinct from the calibrated probability model.",
                         "The older exchange's narrow set of listings gives only a few independent monthly outcomes."],
              "provenance": {"frozen_input": str(frozen.relative_to(data)), "frozen_input_sha256": digest, "terms": TERMS,
                             "official_scenarios": PROJECTIONS, "historical_scenario_archive": ARCHIVE, "daily_catalog": DAILY_CATALOG,
                             "settlement_table": MONTHLY, "daily_alternate": DAILY_ALTERNATE, "scikit_learn": sklearn_version,
                             "source_sha256": research_model.run_sources(sys.modules[__name__], venue_storm)}}
    return research_model.write_record(data, result, {"held_thresholds": outcomes, "native_decisions": native_forecasts,
                                                      "native_price_comparison": priced,
                                                      "native_payout_audits": corpus["contracts"],
                                                      "pending_forecasts": {**latest, "active_native_estimates": prospective},
                                                      "unavailable_forecasts": corpus["gaps"]})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--input", type=Path)
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--candles", action="store_true")
    args = parser.parse_args(argv)
    if args.download:
        gaps = _fetch(args.data)
        print(f"{len(gaps)} documented source paths returned gaps", flush=True)
    if args.candles:
        get = PublicGET(args.data)
        counts = sum(venue_storm._receive_candle(args.data, get, row, first_at=parse_time(row["opened_at"]))
                     for row in _contracts(args.data) if row["opened_at"])
        print(f"{counts} daily native candle points", flush=True)
    corpus = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else gather(args.data)
    print(evaluate(args.data, corpus), flush=True)


if __name__ == "__main__":
    main()
