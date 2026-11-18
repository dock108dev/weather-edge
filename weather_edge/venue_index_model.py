"""Model native hourly Weather Index values and replay exact Kalshi thresholds."""

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from functools import cache, lru_cache
import gzip
import json
import math
from pathlib import Path
import re
from statistics import NormalDist, mean, stdev
import sys
from zoneinfo import ZoneInfo

from sklearn import __version__ as sklearn_version
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.feature_extraction import DictVectorizer
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from threadpoolctl import threadpool_limits

from . import research_model, venue_archive, venue_index
from .market import decimal, iso, parse_time
from .venue_contracts import INDEX_CITIES

VERSION = "kalshi-hourly-index-v1"
WINDOWS = {"fit": ("2026-10-15", "2026-11-05"), "calibration": ("2026-11-06", "2026-11-08"),
           "test": ("2026-11-10", "2026-11-14")}
DECISION_BEFORE = timedelta(minutes=15)
PUBLICATION_DEADLINE = timedelta(minutes=5)
MAX_RECENT_AGE = timedelta(minutes=15)
EXCHANGE_METHOD = "https://www.cftc.gov/filings/ptc/ptc10012630837.pdf"
METHODS = {"persistence": "Most recent canonical index", "trend": "Recent slope extrapolation",
           "ridge": "Ridge · index and time of day", "with_spread": "Ridge · member temperature spread",
           "nonlinear": "Histogram gradient boosting"}
TIMEZONES = {"EST": -5, "EDT": -4, "CST": -6, "CDT": -5, "MST": -7, "MDT": -6, "PST": -8, "PDT": -7}
CLOCK = re.compile(
    r"for ([A-Za-z]{3,9} \d{1,2}, 20\d{2})\s+(?:at\s+)?(\d{1,2})\s*(AM|PM)\s+([A-Z]{3})\b", re.I,
)


def _target_time(rules):
    match = CLOCK.search(rules)
    if match is None or match[4].upper() not in TIMEZONES or not 1 <= int(match[2]) <= 12:
        return None
    for fmt in ("%b %d, %Y", "%B %d, %Y"):
        try:
            day = datetime.strptime(match[1], fmt).date()
            break
        except ValueError:
            day = None
    if day is None:
        return None
    hour = int(match[2]) % 12 + (12 if match[3].upper() == "PM" else 0)
    local = timezone(timedelta(hours=TIMEZONES[match[4].upper()]))
    return datetime(day.year, day.month, day.day, hour, tzinfo=local).astimezone(timezone.utc)


def _case_market_rows(data, start, end):
    bounds = (start + "T00:00:00Z",
              (date.fromisoformat(end) + timedelta(days=2)).isoformat() + "T00:00:00Z")
    with venue_archive.connect(data) as db:
        return [dict(row) for row in db.execute(
            """SELECT instrument_id,event_id,series_id,title,label,rules_primary,rules_secondary,rules_url,
            source,status,opened_at,closed_at,settlement_at,settlement_received_at,settlement_source,
            yes_payout,expiration_value,lower,operator FROM markets
            WHERE venue=? AND series_id IN (?,?,?,?) AND closed_at>=? AND closed_at<?
            AND instr(rules_primary,'Synoptic Data')>0 ORDER BY event_id,instrument_id""",
            (venue_archive.KALSHI, *INDEX_CITIES, *bounds),
        )]


def _canonical_series(data, city, *, start, end, requested_after=None, as_of=None):
    versions = defaultdict(list)
    for row in venue_index.points(data, city, start=start, end=end, as_of=as_of):
        if row["status"] not in venue_index.CANONICAL or row["temperature_f"] is None:
            continue
        if requested_after and parse_time(row["requested_at"]) < requested_after:
            continue
        versions[parse_time(row["at"])].append(row)
    return sorted(versions), versions


def _unambiguous(points):
    if len({decimal(row["temperature_f"]) for row in points}) != 1:
        return None
    return next((row for row in points if row["body"].get("stations")), points[0])


def _last_point(series, by):
    times, versions = series
    for index in range(bisect_right(times, by) - 1, -1, -1):
        if row := _unambiguous(versions[times[index]]):
            return row
    return None


def _issued(series, by, *, local=False):
    times, versions = series
    latest_possible = by - PUBLICATION_DEADLINE
    for index in range(bisect_right(times, latest_possible) - 1, -1, -1):
        stamp = times[index]
        if latest_possible - stamp > MAX_RECENT_AGE:
            break
        points = versions[stamp]
        if local:
            points = [row for row in points if parse_time(row["received_at"]) <= by]
        if row := _unambiguous(points):
            return row
    return None


def _spread(point):
    usable = [float(member["temp_f"]) for member in point["body"].get("stations") or []
              if isinstance(member.get("temp_f"), (float, int)) and member.get("code") in ("ok", "fallback")]
    return max(usable) - min(usable) if len(usable) >= 2 else None


def _versions(data, city):
    stored = venue_index.calibrations(data, city)
    versions = stored["value"] if stored else []
    return [dict(row, at=iso(venue_index.index_time(row["effective_at_ms"]))) for row in versions]


def _seasoning(at, zone):
    hour = at.astimezone(ZoneInfo(zone)).hour
    angle = 2 * math.pi * hour / 24
    return {"hour": hour, "diurnal_sin": math.sin(angle), "diurnal_cos": math.cos(angle)}


def _snapshot(city, target, series, zone, *, local=False):
    decision = target - DECISION_BEFORE
    current = _issued(series, decision, local=local)
    recent = _issued(series, decision - timedelta(minutes=10), local=local)
    older = _issued(series, decision - timedelta(minutes=30), local=local)
    if not (current and recent and older):
        return None
    values = [float(point["temperature_f"]) for point in (current, recent, older)]
    period = (parse_time(current["at"]) - parse_time(recent["at"])).total_seconds() / 60
    if period <= 0:
        return None
    slope = (values[0] - values[1]) / period
    projected = values[0] + slope * (target - parse_time(current["at"])).total_seconds() / 60
    return {
        "city": city, "at": iso(decision), "target_at": iso(target), "temperature_f": values[0],
        "trend_f": min(150, max(-150, projected)), "delta_10_f": values[0] - values[1],
        "delta_30_f": values[0] - values[2], "member_spread_f": _spread(current),
        "contributors": current["contributors"], "latest": current, "ten_minute": recent, "thirty_minute": older,
        **_seasoning(target, zone),
    }


def _thresholds(group, actual, settled_as_of, missed, *, require_local_payout=False):
    selected = []
    for market in group:
        if market["status"] != "settled" or market["yes_payout"] not in ("0", "1"):
            missed["no binary venue payout"] += 1
            continue
        if market["operator"] != "above" or market["lower"] is None:
            missed["unsupported hourly threshold operator"] += 1
            continue
        if not market["settlement_at"] or parse_time(market["settlement_at"]) > settled_as_of:
            missed["native payout published after sample cutoff"] += 1
            continue
        if require_local_payout and (not market["settlement_received_at"] or
                                     parse_time(market["settlement_received_at"]) > settled_as_of):
            missed["native payout received after sample cutoff"] += 1
            continue
        try:
            threshold = decimal(market["lower"])
        except ValueError:
            missed["missing threshold"] += 1
            continue
        payout = int(market["yes_payout"])
        weather = int(actual > threshold)
        if payout != weather:
            missed["native binary payout disagrees with indexed value"] += 1
        selected.append({**market, "threshold_f": str(threshold), "physical_outcome": weather, "native_payout_yes": payout})
    return selected


def gather(data, *, start=WINDOWS["fit"][0], end=WINDOWS["test"][1], strict_local=False, cutoff=None,
           original_receipts_from=None):
    misses = Counter()
    events = defaultdict(list)
    for row in _case_market_rows(data, start, end):
        events[row["event_id"]].append(row)
    cutoff = cutoff or datetime.now(timezone.utc)

    @lru_cache(maxsize=8)
    def index(city, utc_day):
        day = datetime.combine(utc_day, datetime.min.time(), timezone.utc)
        return _canonical_series(data, city, start=day - timedelta(hours=3), end=day + timedelta(days=1),
                                 requested_after=original_receipts_from, as_of=cutoff if original_receipts_from else None)

    @cache
    def configurations(city):
        return _versions(data, city)

    rows = []
    for event_id, group in events.items():
        series_id = group[0]["series_id"]
        targets = {_target_time(row["rules_primary"]) for row in group}
        if len(targets) != 1 or None in targets:
            misses["inconsistent human-readable settlement clock"] += 1
            continue
        target = targets.pop()
        city, region, zone = INDEX_CITIES[series_id]
        local_day = target.astimezone(ZoneInfo(zone)).date().isoformat()
        if not start <= local_day <= end:
            continue
        series = index(city, target.date())
        matched = _snapshot(city, target, series, zone, local=strict_local)
        if not matched:
            misses["no eligible canonical index before decision"] += 1
            continue
        point = _last_point(series, target)
        if point is None or target - parse_time(point["at"]) > timedelta(hours=2):
            misses["no inspectable canonical settlement point"] += 1
            continue
        inspect = []
        for market in group:
            if market["status"] == "settled" and market["expiration_value"]:
                try:
                    inspect.append(decimal(market["expiration_value"]))
                except ValueError:
                    continue
        if len(set(inspect)) != 1:
            misses["missing or conflicting native temperature expiration values"] += 1
            continue
        actual = inspect[0]
        if actual != decimal(point["temperature_f"]):
            misses["canonical index point differs from latest native expiration"] += 1
            continue
        captured = _thresholds(group, actual, cutoff, misses, require_local_payout=original_receipts_from is not None)
        if not captured:
            continue
        current_configs = [row for row in configurations(city) if row["at"] <= point["at"]]
        config = current_configs[-1] if current_configs else None
        rows.append({**matched, "event_id": event_id, "region": region, "date": local_day, "actual_f": float(actual),
                     "settlement_point": point, "prices": captured, "threshold_count": len(captured),
                     "config": config, "local_receipt_required": strict_local})
    rows.sort(key=lambda row: (row["target_at"], row["event_id"]))
    return {"version": VERSION, "rows": rows, "missing": dict(misses),
            "settings": {"windows": WINDOWS, "decision_lead_minutes": DECISION_BEFORE.total_seconds() / 60,
                         "availability_clock": "original local receipt" if strict_local else "documented event minute + 300 seconds"}}


def _features(row, include_spread=False):
    base = {key: row[key] for key in ("city", "temperature_f", "delta_10_f", "delta_30_f", "diurnal_sin", "diurnal_cos")}
    if include_spread:
        spread = row["member_spread_f"]
        base["member_spread_f"] = spread if spread is not None else 0.0
        base["member_spread_known"] = float(spread is not None)
    return base


def _regress(train, *, method, include_spread=False):
    model = make_pipeline(DictVectorizer(sparse=False), method)
    features = [_features(row, include_spread) for row in train]
    differences = [row["actual_f"] - row["temperature_f"] for row in train]
    with threadpool_limits(limits=2):
        model.fit(features, differences)
    return model


def _fit(train):
    return {
        "ridge": (_regress(train, method=Ridge(alpha=5)), False),
        "with_spread": (_regress(train, method=Ridge(alpha=5), include_spread=True), True),
        "nonlinear": (_regress(train, method=HistGradientBoostingRegressor(max_iter=60, max_leaf_nodes=8,
                                                                           random_state=42)), False),
    }


def _predictions(rows, models):
    selected = [dict(row, persistence=row["temperature_f"], trend=row["trend_f"]) for row in rows]
    with threadpool_limits(limits=2):
        for kind, (model, include_spread) in models.items():
            if selected:
                features = [_features(row, include_spread) for row in selected]
                for row, delta in zip(selected, model.predict(features), strict=True):
                    row[kind] = row["temperature_f"] + float(delta)
    return selected


def _partition(cases):
    buckets = {}
    for name, (start, end) in WINDOWS.items():
        buckets[name] = [row for row in cases if start <= row["date"] <= end]
    # Only outcomes already released at the first following split can train or calibrate.
    names = tuple(WINDOWS)
    for before, after in zip(names, names[1:]):
        if buckets[after]:
            first = min(row["at"] for row in buckets[after])
            buckets[before] = [row for row in buckets[before] if max(price["settlement_at"] for price in row["prices"]) < first]
    return buckets


def _calibrate(rows):
    stats = {}
    for method in METHODS:
        errors = [row["actual_f"] - row[method] for row in rows]
        stats[method] = {"bias": mean(errors), "std": max(0.15, stdev(errors)), "hours": len(rows)}
    return stats


def _prob(row, method, threshold, calibration):
    terms = calibration[method]
    dist = NormalDist(row[method] + terms["bias"], terms["std"])
    return 1 - dist.cdf(float(threshold) + 0.005)


def _coverage(rows):
    return {"bucket_cases": sum(row["threshold_count"] for row in rows), "hourly_events": len(rows),
            "calendar_dates": len({row["date"] for row in rows}),
            "city_days": len({(row["date"], row["city"]) for row in rows})}


def _metrics(rows, calibration, group):
    if not rows:
        return []
    stats = []
    for method in METHODS:
        by_day = defaultdict(list)
        errors_by_day = defaultdict(list)
        for row in rows:
            bucket_errors = [(_prob(row, method, market["threshold_f"], calibration) - market["physical_outcome"]) ** 2
                             for market in row["prices"]]
            by_day[row["date"]].append(mean(bucket_errors))
            errors_by_day[row["date"]].append(abs(row[method] + calibration[method]["bias"] - row["actual_f"]))
        stats.append({"group": group, "label": METHODS[method], "method": method, **_coverage(rows),
                      "mae_f": mean(mean(errors) for errors in errors_by_day.values()),
                      "bucket_brier": mean(mean(errors) for errors in by_day.values())})
    return stats


def _cases(rows, calibration):
    chosen = []
    seen = set()
    for row in sorted(rows, key=lambda r: abs(r["persistence"] - r["actual_f"]), reverse=True):
        if row["city"] in seen:
            continue
        market = min(row["prices"], key=lambda item: abs(float(item["threshold_f"]) - row["actual_f"]))
        estimates = {method: _prob(row, method, market["threshold_f"], calibration) for method in METHODS}
        chosen.append({"title": f"{row['region']} · {row['target_at']} · index above {market['threshold_f']}°F",
                       "venue": venue_archive.KALSHI, "instrument_id": market["instrument_id"],
                       "date": row["date"], "at": row["at"], "data": {"clock": row, "market": market, "estimates": estimates},
                       "detail": f"Published index {row['actual_f']:.2f}°F. Prior index {row['persistence']:.2f}°F. "
                                 f"Native YES paid ${market['native_payout_yes']}."})
        seen.add(row["city"])
    return chosen


def evaluate(data, corpus, *, later=None):
    data = Path(data)
    corpus = {**corpus, "later_evidence": later}
    frozen, digest = research_model.freeze_inputs(data, corpus)
    samples = _partition(corpus["rows"])
    if len(samples["fit"]) < 2 or len(samples["calibration"]) < 2:
        raise ValueError("Missing independent fit or calibration evidence")
    estimators = _fit(samples["fit"])
    calibration = _calibrate(_predictions(samples["calibration"], estimators))
    test = _predictions(samples["test"], estimators)
    later_rows = _predictions(later["rows"], estimators) if later else []
    metrics = _metrics(test, calibration, "Untouched hourly events")
    for city in venue_index.ACTIVE_CITIES:
        metrics += _metrics([row for row in test if row["city"] == city], calibration, city)
    metrics += _metrics(later_rows, calibration, "Later original local receipts")
    record = {"version": VERSION, "title": "Forecasting the hourly Weather Index",
              "hypothesis": "Recent change and local hour may improve on index persistence. Member temperature spread tests "
                            "whether a metro-wide disagreement adds prediction signal.",
              "target": "Canonical Kalshi city index at the contract's named minute and verified payouts",
              "venues": ["Kalshi"], "date_window": WINDOWS,
              "coverage": {**{key: _coverage(rows) for key, rows in samples.items()}, "later": _coverage(later_rows)},
              "missing": {**corpus["missing"], **({f"later: {k}": v for k, v in later["missing"].items()} if later else {})},
              "metrics": metrics, "cases": _cases(later_rows, calibration) + _cases(test, calibration),
              "limits": [
                  "The historical feature cutoff uses the filed publication convention of event minute plus 300 seconds. "
                  "Historical original local receipt is unavailable.",
                  "Only canonical numeric points are inputs. A missing settlement minute selects the prior canonical point "
                  "under exchange rules. The selected numeric value is independently checked against expiration value.",
                  "The later panel uses the actual recorded local receipt before the decision. No archived quote creates a fill.",
              ],
              "provenance": {"frozen_input": str(frozen.relative_to(data)), "frozen_input_sha256": digest,
                             "settings": corpus["settings"], "methodology": EXCHANGE_METHOD,
                             "source": venue_index.DOCS, "scikit_learn": sklearn_version,
                             "calibration": calibration,
                             "source_sha256": research_model.run_sources(sys.modules[__name__], venue_index)}}
    return research_model.write_record(data, record, {"test": test, "later": later_rows})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--input", type=Path)
    parser.add_argument("--include-later", action="store_true")
    args = parser.parse_args(argv)
    corpus = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else gather(args.data)
    later = corpus.get("later_evidence")
    if args.include_later:
        now = datetime.now(timezone.utc)
        today_et = now.astimezone(ZoneInfo("America/New_York")).date().isoformat()
        later = gather(args.data, start=today_et, end=today_et, strict_local=True, cutoff=now)
    print(evaluate(args.data, corpus, later=later))


if __name__ == "__main__":
    main()
