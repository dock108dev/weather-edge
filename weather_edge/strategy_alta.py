"""Read official Alta snowfall histories for the native December market."""

import argparse
from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
from http.cookiejar import CookieJar
import json
from pathlib import Path
import re
from statistics import mean
import time
from urllib.parse import unquote
from urllib.request import HTTPCookieProcessor, Request, build_opener

from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from . import collector, history, research_model, venue_archive
from .capture import ImmutableOrigin
from .market import iso
from .strategy_runs import json_bytes, write_new


ROOT = Path("strategy-snow/alta")
BASE = "https://www.alta.com"
WEATHER_URL = BASE + "/weather"
DAILY_HISTORY_URL = BASE + "/ajax/snowfall-history"
RULES_URL = "https://assets.kalshi.com/contract_terms/ALTASNOW.pdf"
SERIES = "KXALTASNOW"
UA = "Weather Edge public weather research"
MAX_BODY = 2_000_000
VERSION = "alta-december-snowfall-v1"
PHASES = {"fit": (2004, 2012), "selection": (2013, 2018), "later test": (2019, 2025)}
METHODS = ("Earlier December climatology", "Closest three by mid-November snowfall", "Closest five by mid-November snowfall",
           "Ridge on earlier mid-November snowfall")


def inches(value):
    return Decimal(str(value))


def read_page(content):
    body = content.decode()
    state = re.search(r"\bwindow\.Alta\s*=\s*", body)
    if not state:
        raise ValueError("Alta's public weather data were absent from its page")
    obj, _ = json.JSONDecoder().raw_decode(body[state.end():])
    totals = obj["weather"]["snowfall"]
    seasons = [row["value"] for row in totals["seasons"]]
    if not seasons or seasons != sorted(set(seasons), reverse=True):
        raise ValueError("The resort season identities were missing or unordered")
    return obj, seasons


def _receipt(path, payload, *, url, status, requested, received, headers):
    body_hash = sha256(payload).hexdigest()
    if path.is_file() and sha256(path.read_bytes()).hexdigest() != body_hash:
        path = path.with_name(f"{path.stem}-{body_hash[:12]}{path.suffix}")
    write_new(path, payload)
    record = {"source": url, "status": status, "requested_at": iso(requested), "received_at": iso(received),
              "body_sha256": body_hash,
              "headers": {k.lower(): v for k, v in headers.items() if k.lower() in {"age", "cache-control", "content-type", "date", "last-modified"}}}
    write_new(path.with_suffix(path.suffix + ".receipt.json"), json_bytes(record) + b"\n")


def public_weather_session(root):
    jar = CookieJar()
    opener = build_opener(ImmutableOrigin(), HTTPCookieProcessor(jar))
    begin = datetime.now(timezone.utc)
    request = Request(WEATHER_URL, headers={"User-Agent": UA, "Accept": "text/html"})
    with opener.open(request, timeout=15) as response:
        page = response.read(MAX_BODY + 1)
        if len(page) > MAX_BODY:
            raise ValueError("Alta's public weather page exceeded its size bound")
        _receipt(root / "alta-weather.html", page, url=WEATHER_URL, status=response.status, requested=begin,
                 received=datetime.now(timezone.utc), headers=response.headers)
    readable = page.decode()
    token = re.search(r'<meta\s+name="csrf-token"\s+content="([^"]+)"', readable)
    public_cookie = next((unquote(cookie.value) for cookie in jar if cookie.name == "XSRF-TOKEN"), None)
    headers = {"User-Agent": UA, "Accept": "application/json", "Content-Type": "application/json",
               "Referer": WEATHER_URL, "Origin": BASE, "X-Requested-With": "XMLHttpRequest"}
    if token:
        headers["X-CSRF-TOKEN"] = token[1]
    if public_cookie:
        headers["X-XSRF-TOKEN"] = public_cookie
    return opener, headers


def read_history(payload, season):
    rows = json.loads(payload)["snowfallHistory"]
    if not rows or any(row.get("season") != season for row in rows):
        raise ValueError("Alta's history response did not identify the requested season")
    dates = [row["reported_at"] for row in rows]
    if len(dates) != len(set(dates)):
        raise ValueError("Alta supplied duplicate daily records without a unique edition")
    return sorted(rows, key=lambda row: row["reported_at"])


def acquire(data):
    root = data / ROOT
    if collector.paused(data):
        return {"requested_histories": 0, "issues": {"collection": "Collection stopped locally"}}
    opener, headers = public_weather_session(root)
    _, seasons = read_page(newest_page(root).read_bytes())
    if not collector.paused(data):
        history._fetch(root / "kalshi-ALTASNOW.pdf", RULES_URL)
    issues, seen, rows = {}, 0, 0
    for season in seasons:
        path = root / f"season-{season}.json"
        if path.is_file():
            continue
        if collector.paused(data):
            issues[str(season)] = "Collection stopped locally"
            break
        # This is the anonymous season selector used by Alta's public page.
        request = Request(DAILY_HISTORY_URL, data=json_bytes({"season": season}), headers=headers)
        begin = datetime.now(timezone.utc)
        try:
            with opener.open(request, timeout=15) as response:
                payload = response.read(MAX_BODY + 1)
                if len(payload) > MAX_BODY:
                    raise ValueError("Alta's daily history exceeded its size bound")
                found = read_history(payload, season)
                _receipt(path, payload, url=DAILY_HISTORY_URL, status=response.status, requested=begin,
                         received=datetime.now(timezone.utc), headers=response.headers)
        except (OSError, TypeError, KeyError, ValueError) as error:
            issues[str(season)] = str(error)
        else:
            seen += 1
            rows += len(found)
        time.sleep(0.25)
    return {"requested_histories": seen, "daily_rows": rows, "issues": issues}


def _archived_proof(path, data, source):
    digest = sha256(path.read_bytes()).hexdigest()
    record = path.with_suffix(path.suffix + ".receipt.json")
    transport = json.loads(record.read_bytes()) if record.is_file() else None
    if transport and transport["body_sha256"] != digest:
        transport = None
    return {"path": str(path.relative_to(data)), "source": source, "sha256": digest, "transport": transport,
            "cache_mtime": iso(datetime.fromtimestamp(path.stat().st_mtime, timezone.utc))}


def newest_page(root):
    pages = list(root.glob("alta-weather*.html"))
    if not pages:
        raise FileNotFoundError("No retained official Alta weather page")
    return max(pages, key=lambda path: (path.stat().st_mtime_ns, path.name))


def december(year, rows, published):
    before = [row for row in rows if row["reported_at"] < f"{year}-12-01"]
    period = [row for row in rows if f"{year}-12-01" <= row["reported_at"] < f"{year + 1}-01-01"]
    after = [row for row in rows if row["reported_at"] >= f"{year + 1}-01-01"]
    chronological = [*before[-1:], *period, *after[:1]]
    continuity = bool(before and period and after) and all(
        inches(current["ytd_snow"]) - inches(prior["ytd_snow"]) == inches(current["new_snow"])
        for prior, current in zip(chronological, chronological[1:]))
    summed = sum((inches(row["new_snow"]) for row in period), Decimal(0))
    if published is not None and inches(published) != summed:
        return None, "Resort monthly table and retained daily snow differ"
    if published is not None or len(period) == 31 or continuity:
        return {"december_in": float(summed), "december_report_dates": len(period), "direct_monthly_table": published,
                "cumulative_series_continuity": continuity, "december_rows": period,
                "boundary_reports": [*before[-1:], *after[:1]]}, None
    return None, "No monthly total or verified cumulative series through December"


def as_of_november(year, rows):
    datecut = f"{year}-11-14"
    morning = datetime.fromisoformat(f"{year}-11-15T12:00:00")
    known = [row for row in rows if row["reported_at"] <= datecut and all(
        isinstance(row.get(clock), str) and datetime.fromisoformat(row[clock]) <= morning for clock in ("created_at", "updated_at"))]
    if not known:
        return None
    active = known[-1]
    age = (date.fromisoformat(datecut) - date.fromisoformat(active["reported_at"])).days
    return {"mid_november_ytd_in": float(inches(active["ytd_snow"])), "report_day_lag": age,
            "as_of_provider_local": morning.isoformat(), "report": active}


def dataset(data):
    root = data / ROOT
    page_path = newest_page(root)
    page, seasons = read_page(page_path.read_bytes())
    public = page["weather"]["snowfall"]
    month = next(row for row in public["monthlySnowTotals"] if row["monthReported"] == 12)
    inputs, gaps, raw, current = [], [], {}, None
    for column, season in enumerate(seasons, 1):
        year, suffix = divmod(season, 10_000)
        if suffix != year + 1:
            raise ValueError("Nonconsecutive Alta season identity")
        path = root / f"season-{season}.json"
        if not path.is_file():
            gaps.append({"year": year, "reason": "Official report snapshot missing"})
            continue
        rows = read_history(path.read_bytes(), season)
        raw[year] = _archived_proof(path, data, DAILY_HISTORY_URL)
        reports = as_of_november(year, rows)
        if column == 1:
            current = {"year": year, "mid_november": reports, "latest_daily_report": rows[-1], "resort_snow_report": page["conditions"]}
            continue
        total, reason = december(year, rows, month.get(f"snow{column}"))
        if reason:
            gaps.append({"year": year, "reason": reason})
            continue
        inputs.append({"year": year, "season": season, **total, "mid_november": reports})
    return inputs, current, gaps, raw, _archived_proof(page_path, data, WEATHER_URL)


def listings(data):
    with venue_archive.connect(data) as db:
        rows = db.execute("""SELECT instrument_id,title,status,opened_at,closed_at,day,yes_bid,yes_ask,last_received_at,
                                 receipt_id,settlement_at,yes_payout,operator,lower,units,rules_primary,rules_url
                             FROM markets WHERE venue=? AND series_id=? ORDER BY lower+0 ASC""", (venue_archive.KALSHI, SERIES)).fetchall()
    result = []
    for row in map(dict, rows):
        if (row["operator"] == "above" and row["units"] == "in" and row["rules_url"] == RULES_URL
                and row["day"] == "2026-12-01" and "at Alta Ski Area in December 2026" in row["rules_primary"]):
            result.append({**row, "threshold_in": float(inches(row["lower"]))})
    if not result:
        raise ValueError("No exact native Alta December threshold listings")
    return result


def feature(row):
    return [row["mid_november"]["mid_november_ytd_in"]]


def probability(samples, threshold):
    return (sum(value > threshold for value in samples) + 0.5) / (len(samples) + 1)


def fit(history):
    targets = [row["december_in"] for row in history]
    model = make_pipeline(StandardScaler(), Ridge(alpha=3))
    model.fit(list(map(feature, history)), targets)
    deviations = [actual - float(predicted) for actual, predicted in zip(targets, model.predict(list(map(feature, history))), strict=True)]
    return model, deviations, targets


def estimators(training, weather, fitted=None):
    if not weather.get("mid_november"):
        return {METHODS[0]: [x["december_in"] for x in training]}
    model, errors, targets = fitted or fit(training)
    observed = feature(weather)[0]
    nearest = sorted(training, key=lambda row: (abs(feature(row)[0] - observed), row["year"]))
    center = float(model.predict([feature(weather)])[0])
    return {METHODS[0]: targets, METHODS[1]: [row["december_in"] for row in nearest[:3]],
            METHODS[2]: [row["december_in"] for row in nearest[:5]], METHODS[3]: [max(0, center + error) for error in errors]}


def panel(training, weather, thresholds):
    fitted = fit(training)
    return [{"year": row["year"], "december_in": row["december_in"], "mid_november": row["mid_november"],
             "p": {name: [probability(values, threshold) for threshold in thresholds]
                   for name, values in estimators(training, row, fitted).items()}}
            for row in weather if row["mid_november"]]


def brier(rows, model, thresholds):
    return mean(mean((p - (row["december_in"] > t)) ** 2 for p, t in zip(row["p"][model], thresholds, strict=True)) for row in rows) if rows else None


def research(data, acquisition):
    complete, current, gaps, raw, when = dataset(data)
    native = listings(data)
    cuts = [row["threshold_in"] for row in native]
    matched = {group: [row for row in complete if bounds[0] <= row["year"] <= bounds[1] and row["mid_november"]]
               for group, bounds in PHASES.items()}
    if not all(matched.values()) or not current:
        raise ValueError("Earlier fit, selection and held seasons need verified Alta reports")
    training = matched["fit"]
    choice = panel(training, matched["selection"], cuts)
    test = panel(training, matched["later test"], cuts)
    locked = min(METHODS, key=lambda name: brier(choice, name, cuts))
    metrics = [{"window": group, "method": name, "independent_seasons": len(rows), "native_thresholds": len(cuts),
                "threshold_tests": len(rows) * len(cuts), "brier": brier(rows, name, cuts), "selected_on_earlier_data": name == locked}
               for group, rows in (("selection", choice), ("later test", test)) for name in METHODS]
    models = estimators([row for row in complete if row["mid_november"]], current)
    predictions = [{**market, "probability_by_method": {name: probability(values, market["threshold_in"]) for name, values in models.items()}}
                   for market in native]
    for row in predictions:
        row["all_prior_seasons_climatology"] = probability([r["december_in"] for r in complete], row["threshold_in"])
    cases = []
    for row in predictions:
        threshold = row["threshold_in"]
        baseline = row["all_prior_seasons_climatology"]
        estimate = row["probability_by_method"][locked]
        title = f"Alta above {threshold:g} inches in December"
        cases.append({"region": "Alta Ski Area, Collins Study Plot", "date": row["day"], "venue": venue_archive.KALSHI,
                      "instrument_id": row["instrument_id"], "at": row["last_received_at"], "title": title,
                      "detail": f"Earlier-selected {locked.lower()} probability {estimate:.0%}. All verified historical seasons imply {baseline:.0%} with count smoothing.",
                      "data": {"listing": row, "current_weather": current}})
    parent = data / ROOT
    clauses = _archived_proof(parent / "kalshi-ALTASNOW.pdf", data, RULES_URL)
    record = {"version": VERSION, "title": "Alta December snowfall on native strict thresholds",
              "hypothesis": "By mid-November, official Alta season snowfall improves probability forecasts of the active December thresholds.",
              "target": "New snowfall in December from Alta's official Collins Study Plot series, in inches.",
              "venues": [venue_archive.KALSHI], "date_window": {"from": str(PHASES["fit"][0]), "through": str(current["year"]), **PHASES},
              "metrics": metrics, "cases": cases, "limits": ["Earlier amounts are official retrospective source records. The provider's reported, created, and updated times were checked for the optional mid-November predictors.",
                         "Historical thresholds use official snowfall values. The December 2026 exchange contracts are open.",
                         "Today's probabilities use the earlier-selected method and all completed physical seasons with comparable inputs. The small holdout limits inference.",
                         "The governing terms prioritize the resort's initial non-preliminary amount and distinguish snowfall from base depth. Quotes are current catalog displays only."],
              "provenance": {"engine": VERSION, "site": WEATHER_URL, "primary_station": "Collins Study Plot, 9662 feet", "source_rule": clauses,
                             "season_evidence": raw, "source_web_page": when, "source_gaps": gaps, "acquisition": acquisition,
                             "confirmed_historical_decembers": len(complete), "later_weather_only_holdout_seasons": len(test),
                             "complete_daily_december_windows": sum(row["december_report_dates"] == 31 for row in complete),
                             "matched_official_monthly_table_rows": sum(row["direct_monthly_table"] is not None for row in complete),
                             "selected_model": locked, "model_implementation_sha256": sha256(Path(__file__).read_bytes()).hexdigest()}}
    return research_model.write_record(data, record, {"historical_decembers": complete, "selection": choice, "holdout": test,
                                                       "current_weather": current, "native_predictions": predictions})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=venue_archive.DEFAULT_DATA)
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--acquire-only", action="store_true")
    args = parser.parse_args()
    result = acquire(args.data) if args.fetch else {}
    if args.acquire_only:
        print(json.dumps(result, indent=2))
        return
    path = research(args.data, result)
    summary = json.loads(path.read_bytes())
    print(json.dumps({"path": str(path), "metrics": summary["metrics"], "provenance": summary["provenance"]}, indent=2))


if __name__ == "__main__":
    main()
