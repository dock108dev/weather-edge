"""Compare preliminary midnight ASOS extrema with official climate payouts."""

import argparse
from collections import Counter, defaultdict
from datetime import date, timedelta
from functools import cache
from hashlib import sha256
import json
from pathlib import Path
import re
from statistics import mean, median

from . import expansion_metar, expansion_weather, history, research_model, strategy_high_transfer
from . import strategy_official_display, strategy_payout_timing, venue_archive
from .market import iso, parse_time
from .model import _benchmark_buckets, _limits, _outcome, _probability
from .nbm import climate_window
from .strategy_metar_high import metar_high_evidence, read_covering_observations


VERSION = "midnight-metar-extrema-v1"
FIT = (date(2026, 10, 1), date(2026, 10, 21))
TEST = (date(2026, 10, 26), date(2026, 11, 14))
STATIONS = (*strategy_high_transfer.STATIONS, "KNYC", "KMIA", "KLAX", "KDFW", "KSEA", "KSFO", "KATL")
FAMILIES = "daily-high", "daily-low"
MODELS = "Point extrema", "Point and within-day six-hour extrema", "Midnight ASOS summary extrema"
MIDNIGHT_REPORT = re.compile(r"(?<!\S)4([01])(\d{3})([01])(\d{3})(?=\s|$)")
WINDOW = timedelta(minutes=12)
ASOS_DOCUMENTATION = "https://www.weather.gov/media/asos/aum-toc.pdf#page=20"


def midnight_summary(row, station, end):
    raw = (row.get("raw_report") or row.get("raw") or row.get("report") or "").strip().rstrip("=")
    ident = expansion_metar.REPORT_START.match(raw)
    stamp = parse_time(row["at"])
    if (not ident or ident[1] != station or ident[2] != stamp.strftime("%d%H%M")
            or not end - WINDOW <= stamp < end):
        return None
    group = MIDNIGHT_REPORT.search(raw.partition(" RMK ")[2])
    if group is None:
        return None
    max_c, min_c = [int(group[index + 1]) / 10 * (-1 if group[index] == "1" else 1) for index in (1, 3)]
    high, low = (expansion_metar.integer_asos_interval(value, 0.1) for value in (max_c, min_c))
    if not high or not low or high[0] != high[1] or low[0] != low[1] or max_c < min_c:
        return None
    return {"at": row["at"], "proxy_available_at": row["available_at"], "raw_report": raw,
            "code": group[0], "max_c": max_c, "min_c": min_c, "high_f": high[0], "low_f": low[0],
            "minutes_before_climate_end": (end - stamp).total_seconds() / 60,
            "source_url": row.get("source_url")}


def _original_receipt(data, station, closing_hour, report):
    root = data / "madis-metar"
    raw_file = root / closing_hour.strftime("%Y%m%d_%H00.gz")
    if not raw_file.is_file():
        return None
    matched = history._madis_hour(closing_hour, station, root).get((report["at"], history._report_key(report["raw_report"])))
    if not matched:
        return None
    return {"raw_report": report["raw_report"], "available_at": matched[0], "source": matched[1],
            "archive_path": str(raw_file.relative_to(data)), "archive_sha256": _source_hash(raw_file)}


@cache
def _source_hash(path):
    return sha256(path.read_bytes()).hexdigest()


def _primary_candidate(observations, station, closing_hour):
    rows = [group for obs in observations if (group := midnight_summary(obs, station, closing_hour))]
    if not rows:
        return None, "no exact midnight group"
    first_stamp = min(row["at"] for row in rows)
    first = [row for row in rows if row["at"] == first_stamp]
    if len({(row["high_f"], row["low_f"]) for row in first}) != 1:
        return None, "same-time midnight-group disagreement"
    return first[0], None


def _baseline(family, observations, station, start, at):
    if family == "daily-high":
        witnesses = [reading for row in observations for reading in metar_high_evidence(row, station, start, at)]
        side, take = "lower_f", max
    else:
        witnesses = [reading for row in observations for reading in expansion_metar.metar_low_evidence(row, station, start, at)]
        side, take = "upper_f", min
    if not witnesses:
        return None
    choice = lambda row: row[side]
    current = take(witnesses, key=choice)
    point = take((row for row in witnesses if row["kind"] == "point"), key=choice, default=None)
    return (point, current, side) if point else None


def collect_rows(data):
    labels = strategy_payout_timing.raw_climate_reports(data, day_from=FIT[0], day_through=TEST[1])
    groups, gaps, sources = defaultdict(list), Counter(), {}
    for station in STATIONS:
        try:
            options, path = read_covering_observations(data, station, FIT[0], TEST[1] + timedelta(days=2))
        except (OSError, ValueError) as error:
            gaps[station, "raw ASOS archive", str(error)] += 1
            continue
        offset = expansion_weather.STATIONS[station][3]
        by_date = defaultdict(list)
        for row in options:
            day = (parse_time(row["at"]) + timedelta(hours=offset)).date()
            by_date[day].append(row)
        sources[station] = {"path": path, "sha256": sha256((data / path).read_bytes()).hexdigest()}
        for phase, dates in (("fit", FIT), ("later test", TEST)):
            for day in strategy_high_transfer.dates(*dates):
                start, end = climate_window(day, offset)
                obs = by_date[day]
                first, error = _primary_candidate(obs, station, end)
                if error:
                    gaps[phase, station, error] += 1
                    continue
                try:
                    proof = _original_receipt(data, station, end, first)
                except (OSError, KeyError, TypeError, ValueError, EOFError):
                    proof = None
                    gaps[phase, station, "NOAA MADIS input unreadable"] += 1
                at = parse_time(first["proxy_available_at"])
                for family in FAMILIES:
                    candidates = labels.get(((station, str(day), iso(start), iso(end)), family), [])
                    if phase == "fit":
                        lock = climate_window(TEST[0], offset)[0]
                        candidates = [label for label in candidates if parse_time(label["at"]) < lock]
                    if not candidates or parse_time(candidates[0]["at"]) <= at:
                        gaps[phase, family, "complete original CLI ahead of proxy unavailable"] += 1
                        continue
                    history_rows = _baseline(family, obs, station, start, at)
                    if history_rows is None:
                        gaps[phase, family, "no same-clock point/six-hour comparison"] += 1
                        continue
                    point, combined, field = history_rows
                    direct = first["high_f"] if family == "daily-high" else first["low_f"]
                    groups[phase].append({"station": station, "date": str(day), "family": family, "at": iso(at),
                                          "climate_ends_at": iso(end), "actual_f": candidates[-1]["amount"],
                                          "forecast": dict(zip(MODELS, (point[field], combined[field], direct), strict=True)),
                                          "point_witness": point, "six_hour_or_point_witness": combined,
                                          "midnight_group": first, "archive_receipt": proof, "official_editions": candidates})
    if any(not groups[phase] for phase in ("fit", "later test")):
        raise ValueError("Earlier and later independent dates need source-matched midnight and CLI reports")
    return groups, sources, [{"scope": " / ".join(key), "records": n} for key, n in sorted(gaps.items())]


def train(fit):
    results = defaultdict(list)
    for row in fit:
        for model in MODELS:
            error = row["actual_f"] - row["forecast"][model]
            results[row["family"], row["station"], model].append(error)
            results[row["family"], None, model].append(error)
    return results


def run(rows, residuals, venue=None):
    result = []
    for row in rows:
        family, station = row["family"], row["station"]
        market = venue.get((station, row["date"], family)) if venue is not None else None
        if venue is not None and market is None:
            continue
        direct = row["forecast"][MODELS[2]]
        buckets = market["buckets"] if market else _benchmark_buckets(round(direct))
        truth = [b["payout"] if market else _outcome(b, row["actual_f"]) for b in buckets]
        outcomes = {}
        for model in MODELS:
            point = row["forecast"][model]
            errors = residuals[family, station, model]
            if len(errors) < 5:
                errors = residuals[family, None, model]
            probability = [_probability(_limits(b), [point + error for error in errors], 0.5) for b in buckets]
            outcomes[model] = {"probabilities": probability, "brier": mean((p - y) ** 2 for p, y in zip(probability, truth, strict=True)),
                               "error_f": point - row["actual_f"], "same_bucket_as_target": all(_outcome(b, point) == y for b, y in zip(buckets, truth, strict=True))}
        result.append({**row, "estimates": outcomes, "buckets": buckets})
    return result


def summaries(result, label):
    for family in FAMILIES:
        rows = [row for row in result if row["family"] == family]
        if not rows:
            continue
        timed = [(parse_time(row["official_editions"][0]["at"]) - parse_time(row["archive_receipt"]["available_at"])).total_seconds() / 60
                 for row in rows if row["archive_receipt"]]
        proxy = [(parse_time(row["official_editions"][0]["at"]) - parse_time(row["at"])).total_seconds() / 60 for row in rows]
        for model in MODELS:
            daily = defaultdict(list)
            for row in rows:
                daily[row["date"]].append(row["estimates"][model])
            yield {"target": label, "family": family, "model": model, "station_days": len(rows), "calendar_dates": len(daily),
                   "native_buckets": sum(len(row["buckets"]) for row in rows) if label != "Official climate high and low" else None,
                   "brier": mean(mean(a["brier"] for a in d) for d in daily.values()),
                   "mae_f": mean(mean(abs(a["error_f"]) for a in d) for d in daily.values()),
                   "same_bucket_as_target_days": sum(row["estimates"][model]["same_bucket_as_target"] for row in rows),
                   "exact_madis_receipt_days": len(timed), "receipt_before_cli_days": sum(n > 0 for n in timed),
                   "median_precise_receipt_lead_minutes": median(timed) if timed else None,
                   "median_fixed_proxy_lead_minutes": median(proxy)}


def market_cases(portfolio):
    result = []
    for venue in venue_archive.VENUES:
        for family in FAMILIES:
            rows = [row for row in portfolio[venue] if row["family"] == family]
            if not rows:
                continue
            rows.sort(key=lambda row: row["estimates"][MODELS[2]]["brier"] - row["estimates"][MODELS[1]]["brier"])
            for row in (rows[0], rows[-1]):
                bucket = next(b for b in row["buckets"] if b["payout"])
                result.append({"venue": venue, "station": row["station"], "date": row["date"],
                               "instrument_id": bucket["instrument_id"], "at": (row["archive_receipt"] or {}).get("available_at", row["at"]),
                               "title": f"{row['station']} midnight preliminary {family.removeprefix('daily-')}",
                               "detail": f"Midnight METAR {row['forecast'][MODELS[2]]}°F, earlier point/six-hour {row['forecast'][MODELS[1]]}°F, retained official {row['actual_f']:.0f}°F. Paid bucket {bucket['label']}.",
                               "data": row})
    return result


def research(data):
    input_rows, sources, gaps = collect_rows(data)
    residuals = train(input_rows["fit"])
    test = input_rows["later test"]
    priced = defaultdict(lambda: defaultdict(list))
    native, _ = strategy_payout_timing.assemble(data)
    for row in native:
        day = date.fromisoformat(row["date"])
        key = row["station"], row["date"], row["family"]
        if TEST[0] <= day <= TEST[1] and row["family"] in FAMILIES and (exact := strategy_official_display.normalized_event(row)):
            priced[row["venue"]][key].append(exact)
    portfolio = {venue: run(test, residuals, {key: rows[0] for key, rows in priced[venue].items() if len(rows) == 1})
                 for venue in venue_archive.VENUES}
    official = "Official climate high and low"
    all_results = {official: run(test, residuals), **portfolio}
    comparison = {target: [research_model.date_bootstrap([row for row in rows if row["family"] == family], MODELS[1], MODELS[2], "brier")
                           | {"family": family} for family in FAMILIES if any(row["family"] == family for row in rows)]
                  for target, rows in all_results.items()}
    record = {"version": VERSION, "title": "Could midnight ASOS extrema preview the climate report?",
              "hypothesis": "A near-end-of-day METAR summary improves on point and within-day six-hour measurements at the same clock.",
              "target": "Daily highs and lows at the exact ASOS station with local-standard climate-day cutoff and native verified payouts.",
              "venues": list(venue_archive.VENUES), "date_window": {"from": str(FIT[0]), "through": str(TEST[1]),
                                                                 "fit": list(map(str, FIT)), "later test": list(map(str, TEST))},
              "metrics": [row for label, group in all_results.items() for row in summaries(group, label)],
              "cases": market_cases(portfolio), "comparison": comparison,
              "limits": ["The midnight group is preliminary. It may end before 23:59 local standard time and differ from the completed ASOS summary or official CLI.",
                         "The source window covers the final 12 minutes of each station's exact local-standard climate day.",
                         "The model comparison uses an explicitly shared ten-minute historical observation delay. Original NOAA MADIS receipt lead is reported separately.",
                         "Native contract reporting and correction rules remain distinct. No displayed prices or paper fills are inferred."],
              "provenance": {"engine": VERSION, "provider_documentation": ASOS_DOCUMENTATION,
                             "archive_asos_sources": sources, "gaps": gaps, "fit_rows": len(input_rows["fit"]),
                             "implementation_sha256": sha256(Path(__file__).read_bytes()).hexdigest()}}
    return research_model.write_record(data, record, {"fit": input_rows["fit"], "held": all_results, "gaps": gaps})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=venue_archive.DEFAULT_DATA)
    args = parser.parse_args()
    path = research(args.data)
    result = json.loads(path.read_bytes())
    print(json.dumps({"path": str(path), "metrics": result["metrics"], "comparison": result["comparison"],
                      "provenance": result["provenance"]}, indent=2))


if __name__ == "__main__":
    main()
