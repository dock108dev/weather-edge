"""Backtest the dollar payout of native seasonal snow-day scalar contracts."""

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_FLOOR
from hashlib import sha256
import json
from pathlib import Path
import re
from statistics import mean, median
from urllib.parse import urlencode
from urllib.request import Request, build_opener

from sklearn import __version__ as sklearn_version
from sklearn.feature_extraction import DictVectorizer
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline

from . import collector, research_model, venue_archive
from .capture import ImmutableOrigin, USER_AGENT
from .market import decimal, iso, parse_time
from .strategy_runs import write_new


VERSION = "seasonal-snow-day-scalar-v1"
SOURCE = "https://data.rcc-acis.org/StnData"
DOCS = "https://docs.rcc-acis.org/acisws/"
TERMS = "https://assets.kalshi.com/contract_terms/SNOWDAYSSCALAR.pdf"
SEASONS = range(2006, 2026)
FIT = range(2006, 2018)
VALIDATION = range(2018, 2021)
TEST = range(2021, 2026)
EVENTS = ("KXSNOWDAYS-01DEC26-01APR27", "KXSNOWDAYSATL-01DEC26-01APR27-T0.1")
PAYOFF = re.compile(r"\$0\.00 at (?P<floor>\d+) qualifying days to \$1\.00 at (?P<cap>\d+) days,.*?"
                    r"at least (?P<threshold>\d+(?:\.\d+)?) inches .*?\((?:[^(),]+, )?(?P<station>K[A-Z]{3})\)", re.I)
OFFSET = re.compile(r"UTC-(\d\d):00\)")
METHODS = ("Earlier fixed seasonal mean", "Earlier recent-weighted seasonal mean", "Earlier year-trend Ridge")


def terms(row):
    primary, secondary = row["rules_primary"], row["rules_secondary"]
    if not (row["rules_url"] == TERMS and primary and secondary and (fields := PAYOFF.search(primary))
            and (clock := OFFSET.search(secondary)) and "Trace snow" in secondary
            and "December 1, 2026 and April 1, 2027" in primary and "inclusive" in secondary
            and "rounded down once to $0.01" in primary):
        return None
    floor, cap = int(fields["floor"]), int(fields["cap"])
    if not floor < cap:
        return None
    offset = -int(clock[1])
    end = datetime.combine(date(2027, 4, 2), time.min, timezone.utc) - timedelta(hours=offset)
    if not row["closed_at"] or parse_time(row["closed_at"]) != end - timedelta(minutes=1):
        return None
    return {"station": fields["station"], "lower_payout_days": floor, "upper_payout_days": cap,
            "min_snow_in": fields["threshold"], "local_standard_utc_offset_hours": offset,
            "starts_on": "2026-12-01", "ends_on": "2027-04-01", "climate_period_ends_at": iso(end)}


def native_listings(data):
    with venue_archive.connect(data) as db:
        ids = db.execute("SELECT instrument_id FROM markets WHERE venue=? AND event_id IN (?,?) ORDER BY instrument_id",
                         (venue_archive.KALSHI, *EVENTS)).fetchall()
    supported, gaps = [], []
    keys = ("instrument_id", "event_id", "title", "status", "rules_primary", "rules_secondary", "rules_url", "opened_at",
            "closed_at", "yes_bid", "yes_ask", "settlement_at", "yes_payout", "receipt", "settlement_receipt")
    for (ident,) in ids:
        full = venue_archive.detail(data, venue_archive.KALSHI, ident)
        row = {key: full[key] for key in keys}
        if parsed := terms(row):
            supported.append({**row, "terms": parsed})
        else:
            gaps.append(ident)
    return supported, gaps


def snow_history(data, station, *, first=date(2006, 11, 1), last=date(2026, 4, 1)):
    if not re.fullmatch(r"K[A-Z]{3}", station):
        raise ValueError("ACIS request did not contain a specific ICAO station")
    values = {"sid": f"{station} 5", "sdate": str(first), "edate": str(last),
              "elems": [{"name": "snow", "add": "f"}, {"name": "snwd", "add": "f"}],
              "meta": ["name", "state", "sids", "ll", "elev"]}
    url = SOURCE + "?" + urlencode({"params": json.dumps(values, separators=(",", ":"))})
    relative = Path("research", "sources", "acis", f"{station}-{first}-{last}.json")
    path = data / relative
    sidecar = path.with_suffix(".receipt.json")
    if path.is_file() and sidecar.is_file():
        body, receipt = path.read_bytes(), json.loads(sidecar.read_bytes())
        if receipt["sha256"] != sha256(body).hexdigest() or receipt["url"] != url:
            raise ValueError(f"Historical snow evidence changed for {station}")
    else:
        if collector.paused(data):
            raise RuntimeError("Live collection is stopped")
        requested = datetime.now(timezone.utc)
        request = Request(url, method="GET", headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        with build_opener(ImmutableOrigin()).open(request, timeout=15) as response:
            size_limit = 3 * 1024 * 1024
            body = response.read(size_limit + 1)
            headers = {key.lower(): value for key, value in response.headers.items()}
        if len(body) > size_limit:
            raise ValueError("ACIS response exceeds the request limit")
        receipt = {"url": url, "requested_at": iso(requested), "received_at": iso(datetime.now(timezone.utc)),
                   "sha256": sha256(body).hexdigest(), "headers": headers, "path": str(relative)}
        validate_snow_response(station, body, first, last)
        write_new(path, body)
        write_new(sidecar, json.dumps(receipt, sort_keys=True).encode())
    raw = validate_snow_response(station, body, first, last)
    days = {row[0]: {"snow": row[1], "depth": row[2]} for row in raw["data"]}
    return {"station": raw["meta"], "receipt": receipt, "days": days}


def validate_snow_response(station, body, first, last):
    parsed = json.loads(body)
    if not isinstance(parsed, dict) or f"{station} 5" not in parsed.get("meta", {}).get("sids", []):
        raise ValueError(f"ACIS exact ICAO station unavailable for {station}")
    dates = [row[0] for row in parsed.get("data", []) if len(row) == 3]
    if not dates or dates[0] != str(first) or dates[-1] != str(last) or len(dates) != len(set(dates)):
        raise ValueError(f"ACIS date coverage or identity mismatch at {station}")
    return parsed


def parallel_weather(data, stations):
    def read(station):
        try:
            return station, snow_history(data, station), None
        except (OSError, ValueError) as failure:
            return station, None, str(failure)

    output, failures = {}, {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        for station, result, reason in pool.map(read, sorted(stations)):
            if reason:
                failures[station] = reason
            else:
                output[station] = result
    return output, failures


def reported_amount(pair):
    if not pair or not (isinstance(pair, list) and len(pair) == 2):
        return None, None
    raw, flag = pair
    flag = flag.strip()
    if raw == "T" and flag in {"", "T"} or flag == "T" and raw in {"0", "0.0", "0.00"}:
        return Decimal(0), "trace"
    if raw == "M" or flag or str(raw).endswith("A"):
        return None, None
    try:
        amount = decimal(raw)
    except ValueError:
        return None, None
    return (amount, "measured") if amount >= 0 else (None, None)


def scalar_payout(qualifying_days, rules):
    floor, cap = rules["lower_payout_days"], rules["upper_payout_days"]
    paid = min(Decimal(1), max(Decimal(0), (decimal(qualifying_days) - floor) / (cap - floor)))
    return paid.quantize(Decimal("0.01"), rounding=ROUND_FLOOR)


def seasonal_outcome(raw, rules, year):
    first, last = date(year, 12, 1), date(year + 1, 4, 1)
    qualifying, missing, trace = [], [], []
    threshold = decimal(rules["min_snow_in"])
    for index in range((last - first).days + 1):
        day = str(first + timedelta(days=index))
        point = raw.get(day, {}).get("snow")
        amount, kind = reported_amount(point)
        if amount is None:
            missing.append(day)
        elif kind == "trace":
            trace.append(day)
        elif amount >= threshold:
            qualifying.append({"date": day, "snow_in": str(amount)})
    lo, hi = scalar_payout(len(qualifying), rules), scalar_payout(len(qualifying) + len(missing), rules)
    return {"year": year, "complete": not missing, "payout_known": lo == hi,
            "qualifying": qualifying, "missing": missing, "trace": trace, "payout_lower": float(lo), "payout_upper": float(hi),
            "payout": float(lo) if lo == hi else None}


def _weather_features(row):
    return {"contract": row["contract"], "years_from_2017": (row["year"] - 2017) / 10}


def _fit_models(rows, years):
    panel = [row for row in rows if row["year"] in years and row["payout_known"]]
    by_contract = defaultdict(list)
    for row in panel:
        by_contract[row["contract"]].append(row)
    models = None
    if panel:
        models = make_pipeline(DictVectorizer(sparse=False), Ridge(alpha=1.0))
        models.fit([_weather_features(row) for row in panel], [row["payout"] for row in panel])

    def predict(row):
        prior = by_contract[row["contract"]]
        if not prior or models is None:
            return {}
        latest = max(prior, key=lambda point: point["year"])["year"]
        recent_weight = [2 ** ((p["year"] - latest) / 5) for p in prior]
        recent = sum(p["payout"] * w for p, w in zip(prior, recent_weight, strict=True)) / sum(recent_weight)
        trend = float(models.predict([_weather_features(row)])[0])
        return dict(zip(METHODS, (mean(p["payout"] for p in prior), recent, min(1.0, max(0.0, trend))), strict=True))

    return predict, panel


def _season_score(pairings, model):
    by_year = defaultdict(list)
    for row in pairings:
        by_year[row["year"]].append(abs(row["payout"] - row["estimates"][model]))
    return mean(mean(values) for values in by_year.values()) if by_year else None


def score(rows):
    predict, fit = _fit_models(rows, FIT)
    tests, metrics = {}, []
    for phase, years in (("validation", VALIDATION), ("later test", TEST)):
        cases = [{**row, "estimates": estimates} for row in rows if row["year"] in years and row["payout_known"]
                 if (estimates := predict(row))]
        tests[phase] = cases
        for model in METHODS:
            metrics.append({"window": phase, "model": model, "season_balanced_payout_mae": _season_score(cases, model),
                            "station_seasons": len({(row["station"], row["year"]) for row in cases}),
                            "seasons": len({row["year"] for row in cases}), "scored_payoffs": len(cases),
                            "complete_seasons_with_no_missing_days": sum(row["complete"] for row in cases)})
    validation = [row for row in metrics if row["window"] == "validation" and row["season_balanced_payout_mae"] is not None]
    selected = min(validation, key=lambda row: row["season_balanced_payout_mae"])["model"] if validation else METHODS[0]
    return metrics, selected, tests, fit


def run(data):
    listings, rules_gaps = native_listings(data)
    weather, context_gaps = parallel_weather(data, {row["terms"]["station"] for row in listings})
    canonical = defaultdict(list)
    for row in listings:
        rule = row["terms"]
        canonical[(rule["station"], rule["min_snow_in"], rule["lower_payout_days"], rule["upper_payout_days"])].append(row)
    rows = []
    contracts = {}
    for (station, _, _, _), members in canonical.items():
        if station not in weather:
            continue
        member = members[0]
        contracts[member["instrument_id"]] = members
        for year in SEASONS:
            outcome = seasonal_outcome(weather[station]["days"], member["terms"], year)
            rows.append({**outcome, "contract": member["instrument_id"], "station": station})
    metrics, selected, tests, fit = score(rows)
    deploy, training = _fit_models(rows, SEASONS)
    cases, current = [], []
    for ident, group in contracts.items():
        station = group[0]["terms"]["station"]
        old = [row for row in rows if row["contract"] == ident and row["payout_known"]]
        continuation = deploy({"contract": ident, "year": 2026})
        if not continuation:
            continue
        for market in group:
            estimate = continuation[selected]
            current.append({**market, "weather_expected_yes_payout": estimate, "alternatives": continuation,
                            "qualifying_calendar_seasons": len(old), "station_metadata": weather[station]["station"],
                            "median_observed_qualifying_days_complete_seasons": median([len(row["qualifying"]) for row in old if row["complete"]])
                            if any(row["complete"] for row in old) else None})
            if len(group) > 1 or station in {"KORD", "KDEN", "KNYC", "KSEA"}:
                cases.append({"venue": venue_archive.KALSHI, "station": station, "date": "2026-12-01",
                              "at": market["receipt"]["received_at"] if market["receipt"] else market["opened_at"],
                              "instrument_id": market["instrument_id"], "title": market["title"],
                              "detail": f"Earlier verified station files imply modeled average YES payout ${estimate:.3f} across {len(old)} usable seasonal outcomes. Native display bid/ask {market['yes_bid']}/{market['yes_ask']}."})
    coverage = {"window": "Native 2026–27 listings", "actual_listings": len(listings) + len(rules_gaps),
                "exact_rule_parses": len(listings), "weather_estimates": len(current), "independent_payoff_rules": len(contracts),
                "exact_station_daily_archives": len(weather), "fit_cases": len(fit), "prior_refit_outcomes": len(training),
                "later_weather_labels": len(tests["later test"]), "verified_live_payouts": 0, "paper_fills": 0}
    record = {"version": VERSION, "title": "Seasonal snowfall days and directional dollar payouts",
              "hypothesis": "A recent-weighted or trend-estimated station climate improves exact rounded snow-day scalar payouts over an earlier fixed mean.",
              "target": "Calendar day counts above native 0.1 or 1.0 inch thresholds, inclusive December 1–April 1 in local standard time.",
              "date_window": {"from": "2006-12-01", "through": "2027-04-01"}, "venues": [venue_archive.KALSHI],
              "metrics": [*metrics, coverage], "cases": cases, "current": current,
              "limits": ["Weather-only analogs use retrospective exact ICAO summaries. The current venue may use The Weather Company's separate publication, revision, and fallback rules.",
                         "Trace contributes zero to 0.1- and 1-inch days. Missing or accumulated reports are unknown. Coverage-limited seasons count only when both payout bounds agree.",
                         "A scalar YES outcome is an expected dollar payout, with flooring applied to each historical count. Display odds and anchor estimates establish no fills.",
                         "Commercial display marks may imply different assumptions or longer-term outlooks. The current windows have no observed qualifying days or verified settlement yet."],
              "provenance": {"engine": VERSION, "weather_source_docs": DOCS, "kalshi_terms": TERMS, "fit_years": list(FIT),
                             "validation_years": list(VALIDATION), "held_years": list(TEST), "locked_validation_selection": selected,
                             "source_gaps": context_gaps, "native_rule_gaps": rules_gaps, "scikit_learn": sklearn_version,
                             "implementation_sha256": sha256(Path(__file__).read_bytes()).hexdigest()}}
    details = {"weather_sources": {station: {"metadata": obj["station"], **obj["receipt"]} for station, obj in weather.items()},
               "weather_outcomes": rows, "evaluation": tests, "native_listings": listings,
               "rules_gaps": rules_gaps, "station_source_gaps": context_gaps}
    return research_model.write_record(data, record, details)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=venue_archive.DEFAULT_DATA)
    args = parser.parse_args()
    path = run(args.data)
    report = json.loads(path.read_bytes())
    print(json.dumps({"path": str(path), "metrics": report["metrics"], "cases": report["cases"],
                      "provenance": report["provenance"]}, indent=2))


if __name__ == "__main__":
    main()
