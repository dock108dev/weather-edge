"""Model NOAA global monthly departures and replay the exact Kalshi monthly contract."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import gzip
import json
from math import cos, pi, sin, sqrt
from pathlib import Path
import re
from statistics import mean, NormalDist
import sys

import numpy as np
from sklearn import __version__ as sklearn_version
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from . import expansion_global_monthly, research_model, venue_archive
from .nbm import iso, utc

VERSION = "noaa-global-monthly-anomaly-v1"
SERIES = "KXHMONTHRANGE"
FILING = "https://kalshi-public-docs.s3.us-east-1.amazonaws.com/regulatory/product-certifications/HMONTHRANGE.pdf"
RECENT_MONTHS = ("2023-01", "2026-10")
WINDOWS = {"fit": ("1985-01", "2017-12"), "calibration": ("2018-02", "2021-12"),
           "selection": ("2022-02", "2024-04"), "test": ("2024-05", "2026-10")}
RULE_DATE = re.compile(r"temperature index for ([A-Za-z]+) (20\d{2})", re.I)
METHODS = {"trend": "Trailing 12 months", "persistence": "Latest prior monthly release",
           "running_3": "Three prior months", "ridge": "Earlier Ridge with recent-month and seasonal structure"}


def _month(key):
    return date.fromisoformat(key + "-01")


def _step(value, amount):
    months = value.year * 12 + value.month - 1 + amount
    return date(months // 12, months % 12 + 1, 1)


def _key(day):
    return day.strftime("%Y-%m")


def _months(first, last):
    current, end = _month(first), _month(last)
    while current <= end:
        yield _key(current)
        current = _step(current, 1)


def _truth(value, market):
    lower = Decimal(market["lower"]) if market["lower"] is not None else None
    upper = Decimal(market["upper"]) if market["upper"] is not None else None
    if market["operator"] == "above" and lower is not None:
        return int(value > lower)
    if market["operator"] == "below" and upper is not None:
        return int(value < upper)
    if market["operator"] == "between-inclusive" and lower is not None and upper is not None:
        return int(lower <= value <= upper)
    raise ValueError("Unverified monthly anomaly operator")


def _native(data):
    with venue_archive.connect(data) as db:
        rows = db.execute("""SELECT instrument_id,event_id,title,label,operator,lower,upper,rules_primary,rules_url,source,status,
            opened_at,closed_at,settlement_at,settlement_received_at,yes_payout,expiration_value,settlement_receipt_id
            FROM markets WHERE venue=? AND series_id=? ORDER BY event_id,instrument_id""", ("kalshi", SERIES))
        terms = [dict(row) for row in rows]
    by_month = defaultdict(list)
    for row in terms:
        match = RULE_DATE.search(row["rules_primary"] or "")
        if not match or row["rules_url"] != "https://assets.kalshi.com/contract_terms/HMONTHRANGE.pdf":
            continue
        try:
            token = next(datetime.strptime(match[1] + " " + match[2], fmt).strftime("%Y-%m")
                         for fmt in ("%b %Y", "%B %Y") if len(match[1]) == 3 or fmt.startswith("%B"))
            _truth(Decimal(0), row)
        except (StopIteration, ValueError, InvalidOperation):
            continue
        by_month[token].append(row)
    return by_month


def _known(table, reports, token, cutoff, *, require_report):
    report = reports.get(token)
    if report:
        return (float(report["departure_c"]), "archived displayed release", report) if utc(report["displayed_report_at"]) <= cutoff else None
    if require_report:
        return None
    if token in table:
        return float(table[token]), "current CAG revision", None
    return None


def _features(table, releases, period, at, *, require_latest):
    day = _month(period)
    lags = [_key(_step(day, -index)) for index in range(1, 13)]
    values = [_known(table, releases, token, at, require_report=require_latest and index == 0) for index, token in enumerate(lags)]
    if not all(values):
        return None
    return {"lag": [value[0] for value in values], "latest_prior_release": values[0][2],
            "feature_source_kinds": [value[1] for value in values]}


def gather(data, cutoff):
    table, receipt = expansion_global_monthly.cag(data, cutoff.year)
    sources = expansion_global_monthly.releases(data, list(_months(*RECENT_MONTHS)))
    native = _native(data)
    cases = {key: [] for key in WINDOWS}
    gaps = Counter()
    for split, window in WINDOWS.items():
        for period in _months(*window):
            first = _month(period)
            decision = datetime(first.year, first.month, 16, tzinfo=timezone.utc)
            if decision >= cutoff or period not in table:
                continue
            features = _features(table, sources, period, decision, require_latest=split == "test")
            if features is None:
                gaps[f"{split}: latest prior published article unavailable by decision"] += 1
                continue
            article = sources.get(period)
            outcome_at_publish = float(article["departure_c"]) if article else None
            markets = [row for row in native.get(period, []) if row["status"] == "settled" and row["yes_payout"] in ("0", "1")
                       and row["opened_at"] and utc(row["opened_at"]) <= decision < utc(row["closed_at"])
                       and row["settlement_at"] and utc(row["settlement_at"]) <= cutoff]
            cases[split].append({"month": period, "date": str(first), "at": iso(decision), **features,
                                 "actual_c": outcome_at_publish if outcome_at_publish is not None else float(table[period]),
                                 "current_cag_c": table[period], "archived_release": article, "markets": markets})
    current = cutoff.strftime("%Y-%m")
    next_markets = native.get(current, [])
    live = _features(table, sources, current, cutoff, require_latest=True)
    if live:
        live.update(month=current, date=str(_month(current)), at=iso(cutoff), markets=next_markets)
    audit = []
    for period, markets in native.items():
        paid = [row for row in markets if row["status"] == "settled" and row["yes_payout"] in ("0", "1")]
        if not paid or period not in table:
            continue
        value = Decimal(table[period])
        source = sources.get(period)
        source_value = Decimal(source["departure_c"]) if source else None
        audit.append({"date": str(_month(period)), "month": period, "archived_release": source, "current_cag_c": str(value), "markets": paid,
                      "current_cag_payout_disagreements": sum(_truth(value, row) != int(row["yes_payout"]) for row in paid),
                      "archived_release_payout_disagreements": (sum(_truth(source_value, row) != int(row["yes_payout"]) for row in paid)
                                                                if source_value is not None else None)})
    return {"version": VERSION, "cag_receipt": receipt, "cases": cases, "audit": audit, "omitted": dict(gaps), "live": live,
            "cutoff": iso(cutoff)}


def _design(row):
    last = row["lag"]
    season = 2 * pi * _month(row["month"]).month / 12
    return [last[0] - mean(last), mean(last[:3]) - mean(last), last[0] - last[1], mean(last), sin(season), cos(season)]


def _points(model, row):
    lags = row["lag"]
    return {"trend": mean(lags), "persistence": lags[0], "running_3": mean(lags[:3]),
            "ridge": lags[0] + float(model.predict([_design(row)])[0])}


def _calibrate(data):
    fit = data["fit"]
    regressor = make_pipeline(StandardScaler(), Ridge(alpha=10))
    regressor.fit([_design(row) for row in fit], [row["actual_c"] - row["lag"][0] for row in fit])
    errors = defaultdict(list)
    for row in data["calibration"]:
        for method, point in _points(regressor, row).items():
            errors[method].append(row["actual_c"] - point)
    scales = {method: {"bias_c": mean(values), "sd_c": max(0.02, sqrt(mean((v - mean(values)) ** 2 for v in values)))}
              for method, values in errors.items()}
    return regressor, scales


def _probability(normal, bucket):
    lower, upper = (float(bucket[key]) if bucket[key] is not None else None for key in ("lower", "upper"))
    if bucket["operator"] == "above":
        return 1 - normal.cdf(lower + 0.005)
    if bucket["operator"] == "below":
        return normal.cdf(upper - 0.005)
    return normal.cdf(upper + 0.005) - normal.cdf(lower - 0.005)


def _score(rows, model, scale):
    output = []
    for row in rows:
        points = _points(model, row)
        distributions = {method: NormalDist(point + scale[method]["bias_c"], scale[method]["sd_c"]) for method, point in points.items()}
        probabilities = [{"instrument_id": market["instrument_id"], "native_yes": market["yes_payout"],
                          "probabilities": {method: _probability(normal, market) for method, normal in distributions.items()}}
                         for market in row["markets"]]
        output.append({**row, "points_c": points, "probabilities": probabilities})
    return output


def _metrics(rows, group):
    scores = []
    payouts = [entry for row in rows for entry in row["probabilities"] if entry["native_yes"] is not None]
    for method, label in METHODS.items():
        scores.append({"group": group, "method": method, "label": label, "calendar_months": len(rows), "payout_cases": len(payouts),
                       "mae_c": mean(abs(row["points_c"][method] - row["actual_c"]) for row in rows),
                       **({"bucket_brier": mean((entry["probabilities"][method] - int(entry["native_yes"])) ** 2
                                                for entry in payouts)} if payouts else {})})
    return scores


def _case(row, evidence):
    paid = next((entry for entry in row["markets"] if entry["yes_payout"] == "1"), None)
    market = paid or next((entry for entry in row["markets"] if entry["operator"] == "above"), None)
    if not market:
        return None
    point = row["points_c"]["ridge"]
    if paid:
        detail = (f"Earlier monthly estimate {point:.2f}°C. NOAA publishes {row['actual_c']:.2f}°C. "
                  f"The native expiration reads {paid['expiration_value']} and this strike paid YES.")
    else:
        detail = f"The latest dated NOAA release for the preceding month supplies {row['lag'][0]:.2f}°C. The Ridge point forecast is {point:.2f}°C."
    return {"title": f"NOAA global anomaly · {row['month']}", "date": row["date"], "at": row["at"], "venue": "kalshi",
            "instrument_id": market["instrument_id"], "region": "Global land and ocean", "detail": detail,
            "market": market, "month_evidence": row, "settlement_audit": evidence}


def _revision_case(audit):
    report = audit["archived_release"]
    winner = next((entry for entry in audit["markets"] if entry["yes_payout"] == "1"), None)
    if not report or not winner:
        return None
    return {"title": f"NOAA global anomaly · Release and subsequent revision", "date": audit["date"],
            "at": report["displayed_report_at"], "venue": "kalshi", "instrument_id": winner["instrument_id"],
            "region": "Global land and ocean", "market": winner, "settlement_audit": audit,
            "detail": (f"The dated NOAA article reported {report['departure_c']}°C and this native strike paid YES. "
                       f"The latest Climate at a Glance table contains {audit['current_cag_c']}°C, which falls outside the winning strike.")}


def evaluate(data, inputs):
    path, digest = research_model.freeze_inputs(data, inputs)
    model, calibration = _calibrate(inputs["cases"])
    earlier, test = (_score(inputs["cases"][key], model, calibration) for key in ("selection", "test"))
    if not test:
        raise ValueError("No NOAA as-of release and native monthly cases")
    cases = []
    native_audit = {row["month"]: row for row in inputs["audit"]}
    live = inputs["live"]
    if live:
        live_case = _case(_score([live], model, calibration)[0], None)
        if live_case:
            cases.append(live_case)
    revisions = [row for row in inputs["audit"] if row["current_cag_payout_disagreements"] and row["archived_release"]
                 and row["archived_release_payout_disagreements"] == 0]
    if revisions:
        case = _revision_case(max(revisions, key=lambda row: abs(float(row["current_cag_c"]) - float(row["archived_release"]["departure_c"]))))
        if case:
            cases.append(case)
    ranked = sorted((row for row in test if row["markets"]), key=lambda row: abs(row["points_c"]["ridge"] - row["actual_c"]), reverse=True)
    october = next((row for row in ranked if row["month"] == "2026-10"), None)
    witnesses = ([october] if october else []) + [row for row in ranked if row != october][:3]
    cases.extend(case for row in witnesses if (case := _case(row, native_audit.get(row["month"]))))
    block = defaultdict(list)
    for row in test:
        key = _month(row["month"])
        block[key.year, (key.month - 1) // 3].append(abs(row["points_c"]["ridge"] - row["actual_c"])
                                                    - abs(row["points_c"]["persistence"] - row["actual_c"]))
    errors = [mean(values) for values in block.values()]
    uncertainty = np.random.default_rng(20261115).choice(errors, size=(2500, len(errors))).mean(axis=1)
    current_gaps = sum(row["current_cag_payout_disagreements"] for row in inputs["audit"])
    release_gaps = sum(row["archived_release_payout_disagreements"] or 0 for row in inputs["audit"])
    record = {"version": VERSION, "title": "NOAA monthly global anomaly and actual Kalshi payouts",
              "hypothesis": "Persistence from a dated prior month and recent climate pattern may outperform the trailing annual anomaly on the same monthly cases.",
              "target": "NOAA CAG one-month global land-and-ocean departure in °C against 1901–2000", "venues": ["kalshi"],
              "date_window": WINDOWS, "metrics": _metrics(earlier, "Earlier climate selection") + _metrics(test, "Later months and actual binary payouts"),
              "coverage": {"native_months_audited": len(inputs["audit"]), "current_cag_payout_disagreements": current_gaps,
                           "source_article_months": sum(case["archived_release"] is not None for case in inputs["audit"]),
                           "source_article_payout_disagreements": release_gaps,
                           "newest_prior_article_required": len(test), "training_months": len(inputs["cases"]["fit"])},
              "uncertainty": {"ridge_minus_persistence_mae_c": mean(errors),
                              "quarter_block_ci_95": np.quantile(uncertainty, (0.025, 0.975)).tolist()},
              "cases": cases, "missing": inputs["omitted"],
              "limits": ["The filed source is NOAA Climate at a Glance. NASA GISTEMP has a different data product and reference period.",
                         "The later decisions require the displayed dated preceding NOAA release before the 16th. Historical archive pages can be updated and preserve retrieval clocks separately.",
                         "Earlier months and older lag values can come from a current revision of the NOAA table. Some historical sources remain retrospective.",
                         "Several native settlement values are interval-censored. Payout scoring uses the actual binary receipts rather than substituting the latest published decimal."],
              "provenance": {"frozen_input": str(path.relative_to(data)), "frozen_input_sha256": digest,
                             "noaa_current_receipt": inputs["cag_receipt"], "monthly_rule": FILING, "calibration": calibration,
                             "scikit_learn": sklearn_version,
                             "source_sha256": research_model.run_sources(sys.modules[__name__], expansion_global_monthly)}}
    return research_model.write_record(data, record, {"selection": earlier, "test": test, "native_audit": inputs["audit"], "live": live})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--input", type=Path)
    args = parser.parse_args()
    inputs = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else gather(args.data, datetime.now(timezone.utc))
    print(evaluate(args.data, inputs))


if __name__ == "__main__":
    main()
