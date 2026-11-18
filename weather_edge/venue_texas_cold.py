"""Replay Texas cold spells across the two airport minima and exact four-day test."""

import argparse
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
import gzip
from hashlib import sha256
import json
from pathlib import Path
import re
from statistics import mean
import sys

from . import expansion_weather, research_model, venue_archive, venue_ghcn, venue_snow_data, venue_storm
from .collector import paused
from .market import decimal, iso, parse_time
from .venue_sources import CollectionPaused

VERSION = "texas-two-station-cold-spell-v1"
TERMS = "https://www.cftc.gov/filings/ptc/ptc07222611168.pdf"
STATION_IDS = {"KDFW": "USW00003927", "KIAH": "USW00012960"}
STATION_PRODUCTS = {"KDFW": ("KFWD", "CLIDFW"), "KIAH": ("KHGX", "CLIIAH")}
FIRST, FIT_END, HELD_END = 1975, 2002, 2025
THRESHOLDS = (20, 22, 25, 28)
GHCN = "https://www.ncei.noaa.gov/pub/data/ghcn/daily/all/"
METHODS = {"earlier_period": "Earlier identical calendar periods", "independent": "Independent daily cold risks",
           "persistent": "Daily cold risks with persistence"}


def _station(data, station, collect):
    identity = STATION_IDS[station]
    name = Path(identity + ".dly")
    raw, receipt = (venue_storm._get(data, name, GHCN + name.name, folder="texas-cold") if collect
                    else venue_storm._existing(data, name, folder="texas-cold"))
    values, invalid = {}, Counter()
    for day, raw_value, measure, quality, source in venue_ghcn.daily_entries(raw, identity, "TMIN"):
        if not -600 <= raw_value <= 600 or quality != " " or source == " " or measure in {"L", "H"}:
            invalid["missing or unsuitable extrema"] += 1
            continue
        if day in values:
            raise ValueError(f"Duplicate daily minimum for {station} {day}")
        temp = decimal(raw_value) * Decimal("0.18") + 32
        values[day] = {"fahrenheit": str(temp), "nearest_whole_f": int(temp.quantize(Decimal("1"), rounding=ROUND_HALF_UP)),
                       "source_flag": source, "measurement_flag": measure}
    return values, receipt, dict(invalid)


def _average(first, second):
    return ((decimal(first) + decimal(second)) / 2).quantize(Decimal("0.1"), rounding=ROUND_DOWN)


def _daily_pairs(data, collect):
    locations, provenance, gaps = {}, {}, {}
    for station in STATION_IDS:
        locations[station], provenance[station], gaps[station] = _station(data, station, collect)
    first, second = locations.values()
    pairs = {}
    for day in sorted(first.keys() & second.keys()):
        pair = [first[day], second[day]]
        pairs[day] = {"date": str(day), "airport_minima": dict(zip(STATION_IDS, pair, strict=True)),
                      "average_f": str(_average(*(point["nearest_whole_f"] for point in pair))),
                      "average_f_before_rounding": str(_average(*(point["fahrenheit"] for point in pair)))}
    return pairs, provenance, gaps


def _sequence(pairs, first, last, *, field="average_f"):
    return [(first + timedelta(days=i), decimal(pairs[first + timedelta(days=i)][field])
             if first + timedelta(days=i) in pairs else None) for i in range((last - first).days + 1)]


def _hits(series, threshold):
    recent, cases = [], []
    for day, value in series:
        if value is None or value >= threshold:
            continue
        if recent and (day - recent[-1]).days <= 3:
            cases.append((recent[-1], day))
        recent.append(day)
    return cases


def _daily_climate(pairs, threshold):
    earlier = {day: decimal(row["average_f"]) < threshold for day, row in pairs.items() if FIRST <= day.year <= FIT_END}
    baserate, transitions = defaultdict(list), defaultdict(list)
    for day, yes in earlier.items():
        baserate[day.month].append(yes)
        if (prior := day - timedelta(days=1)) in earlier:
            transitions[day.month, earlier[prior]].append(yes)
    return {month: {
        "independent": (sum(baserate[month]) + 0.5) / (len(baserate[month]) + 1),
        "persistent": {state: (sum(choices) + 0.5) / (len(choices) + 1)
                       for state in (True, False) if (choices := transitions[month, state])}
    } for month in range(1, 13)}


def _risk(climate, method, first, last, before):
    # Until the first success pair, the last three days can contain at most
    # one threshold crossing. The index is the age of that crossing.
    state = [0.0, 0.0, 0.0, 1.0]
    for distance in (1, 2, 3):
        if before.get(first - timedelta(days=distance)) is True:
            state = [float(index == distance - 1) for index in range(4)]
            break
    for index in range((last - first).days + 1):
        day = first + timedelta(days=index)
        group = climate[day.month]
        baseline = group["independent"]
        next_state = [0.0, 0.0, 0.0, 0.0]
        for age, chance in enumerate(state):
            conditional = group["persistent"].get(age == 0, baseline) if method == "persistent" else baseline
            next_state[min(age + 1, 3)] += chance * (1 - conditional)
            if age == 3:
                next_state[0] += chance * conditional
        state = next_state
    return min(1.0, max(0.0, 1 - sum(state)))


def _original_cli(data, station, day, collect):
    office, pil = STATION_PRODUCTS[station]
    year = date.fromisoformat(day).year
    anna = data / "venues" / "snow-climate" / station / f"{year}.json"
    if not anna.is_file():
        return None
    annual_raw = anna.read_bytes()
    meta = json.loads(anna.with_suffix(".json.receipt.json").read_bytes())
    if sha256(annual_raw).hexdigest() != meta["sha256"]:
        raise ValueError("Archived annual climate source changed")
    item = next((row for row in json.loads(annual_raw)["results"] if row["valid"] == day and row["station"] == station), None)
    name = (item or {}).get("product")
    if not name or re.fullmatch(rf"\d{{12}}-{office}-CDUS\d{{2}}-{pil}", name) is None:
        return None
    relative = Path(station) / (name + ".txt")
    if not collect and not (data / "venues" / "snow-climate" / relative).exists():
        return None
    if collect and paused(data):
        raise CollectionPaused
    raw, receipt = venue_snow_data._obtain(data, relative, venue_snow_data.IEM_TEXT + name)
    text = raw.decode().replace("\r\n", "\n")
    if not re.search(r"^\s*YESTERDAY\b", text, re.M) or not re.search(rf"^{pil}\s*$", text, re.M):
        return None
    named = expansion_weather.CLI_DATE.search(text)
    reported = next((int(value) for field, value, _ in expansion_weather.TEMPERATURE.findall(text)
                     if field == "MINIMUM" and value not in ("M", "MM")), None)
    if named:
        month, number, year = named.groups()
        if date(int(year), expansion_weather.MONTHS[month], int(number)).isoformat() != day:
            return None
    return {**receipt, "published_at": iso(datetime.strptime(name[:12], "%Y%m%d%H%M").replace(tzinfo=timezone.utc)),
            "minimum_f": reported, "valid": day} if named and reported is not None else None


def _native(data):
    with venue_archive.connect(data) as db:
        rows = [venue_archive._market(row) for row in db.execute(
            f"SELECT {venue_archive.PUBLIC_SELECT} FROM markets WHERE venue=? AND series_id='KXTXURI' ORDER BY day",
            (venue_archive.KALSHI,))]
    choices = []
    for row in rows:
        if not row["day"] or row["operator"] != "two-days-in-four-below" or row["upper"] != "20":
            continue
        readonly = venue_archive.detail(data, row["venue"], row["instrument_id"])
        book = next((point for point in readonly["books"] if point.get("depth_kind") == "displayed_ladder"), None)
        retained = None if not book else {key: book.get(key) for key in ("at", "source_at", "receipt_id", "source",
                                                        "yes_bids", "yes_offers", "no_bids", "no_offers")}
        choices.append({"rule": row, "book": retained, "created_at": readonly["raw_market"].get("created_time")})
    return choices


def gather(data, *, collect=False):
    pairs, sources, gaps = _daily_pairs(data, collect)
    native = _native(data)
    checkpoints = {}
    cold_samples = ("2021-02-15", "2021-02-16", "2022-12-23", "2022-12-24")
    for day in cold_samples:
        observed = {}
        for station in STATION_IDS:
            try:
                if report := _original_cli(data, station, day, collect):
                    observed[station] = report
            except (OSError, ValueError):
                pass
        if observed:
            checkpoints[day] = observed
    progress = []
    for block in native:
        first = parse_time(block["created_at"]).date() + timedelta(days=1)
        observed = [day for day in pairs if first <= day <= date(2026, 11, 14)]
        if observed:
            end = max(observed)
            seq = _sequence(pairs, first, end)
            progress.append({"instrument_id": block["rule"]["instrument_id"], "as_of_day": str(end),
                             "complete_days": sum(value is not None for _, value in seq),
                             "gaps": sum(value is None for _, value in seq),
                             "lowest_average_f": str(min(value for _, value in seq if value is not None)),
                             "witnesses": [[str(a), str(b)] for a, b in _hits(seq, 20)]})
    return {"version": VERSION, "day_pairs": list(pairs.values()), "raw_receipts": sources, "gaps": gaps, "live": native,
            "progress": progress, "independent_cli_spot": checkpoints}


def _cases_by_year(pairs, threshold, horizon):
    cases = []
    for year in range(FIRST, HELD_END - horizon + 1):
        first, last = date(year, 7, 23), date(year + horizon, 12, 31)
        paired = _sequence(pairs, first, last)
        if any(value is None for _, value in paired):
            continue
        alternatives = _sequence(pairs, first, last, field="average_f_before_rounding")
        cases.append({"year": year, "horizon": horizon, "first": first, "last": last, "series": paired,
                      "hit": bool(_hits(paired, threshold)), "before_rounding": bool(_hits(alternatives, threshold))})
    return cases


def _selection(rows, first):
    return [(day, reading) for day, reading in rows if day < first]


def _climate_case(native, day_pair, originals):
    rule = native["rule"]
    a, b = day_pair
    return {"venue": rule["venue"], "instrument_id": rule["instrument_id"], "station": None,
            "date": a["date"], "title": "A verified past two-airport cold spell", "detail":
                f"The paired station averages were {a['average_f']}°F on {a['date']} and {b['average_f']}°F on {b['date']}. "
                "Both days are below the current listing's 20°F threshold. These predate the native contract.",
            "data": {"primary_rule": rule, "historic_qualifying_days": day_pair,
                     "original_daily_reports": {x["date"]: originals.get(x["date"], {}) for x in (a, b)}}}


def evaluate(data, corpus):
    frozen, digest = research_model.freeze_inputs(data, corpus)
    pairs = {date.fromisoformat(row["date"]): row for row in corpus["day_pairs"]}
    scored, examples, audit = [], [], {}
    for threshold in THRESHOLDS:
        by_day = _daily_climate(pairs, threshold)
        for horizon in range(3):
            cohort = _cases_by_year(pairs, threshold, horizon)
            train = [row for row in cohort if row["year"] + horizon <= FIT_END]
            held = [row for row in cohort if row["year"] > FIT_END]
            audit[f"{threshold}F / +{horizon}y"] = {"fit_years": len(train), "held_initial_years": len(held),
                    "actual_fit_positive": sum(row["hit"] for row in train), "actual_held_positive": sum(row["hit"] for row in held),
                    "fahrenheit_reporting_precision_changes": sum(row["hit"] != row["before_rounding"] for row in cohort)}
            if threshold == 20 and horizon == 1:
                examples = [event for row in cohort for event in _hits(row["series"], threshold) if event[0].month in (1, 2, 12)]
            for stage in ("Start", "November 15"):
                for row in held:
                    first = row["first"] if stage == "Start" else date(row["year"], 11, 15)
                    past = _selection(row["series"], first)
                    if _hits(past, threshold):
                        continue
                    baseline_cases = [earlier for earlier in train if not _hits(_selection(earlier["series"],
                                      earlier["first"] if stage == "Start" else date(earlier["year"], 11, 15)), threshold)]
                    climate = (sum(case["hit"] for case in baseline_cases) + 0.5) / (len(baseline_cases) + 1)
                    known = {day: temp < threshold for day, temp in past[-3:]}
                    scores = {method: _risk(by_day, method, first, row["last"], known)
                              for method in ("independent", "persistent")}
                    scored.append({"year": row["year"], "stage": stage, "horizon": horizon, "threshold": threshold,
                                   "outcome": row["hit"], "probabilities": {"earlier_period": climate, **scores}})
    metrics, grouped = [], defaultdict(list)
    for row in scored:
        grouping = (row["stage"], row["horizon"])
        grouped[grouping, "All tested strict thresholds"].append(row)
        if row["threshold"] == 20:
            grouped[grouping, "Native 20°F threshold"].append(row)
    for ((phase, horizon), scope), sample in sorted(grouped.items()):
        for method, label in METHODS.items():
            metrics.append({"group": f"{phase} • +{horizon} years • {scope}", "model": label, "cases": len(sample),
                            "calendar_start_years": len({row["year"] for row in sample}),
                            "brier": mean((row["probabilities"][method] - row["outcome"]) ** 2 for row in sample)})
    live, cases = corpus["live"], []
    for dates in dict.fromkeys(examples):
        if live and [str(day) for day in dates] in (["2021-02-15", "2021-02-16"], ["2022-12-23", "2022-12-24"]):
            cases.append(_climate_case(live[0], [pairs[day] for day in dates], corpus["independent_cli_spot"]))
    by_market = {row["instrument_id"]: row for row in corpus["progress"]}
    for current in live:
        market = current["rule"]
        progress = by_market.get(market["instrument_id"])
        if progress is None:
            continue
        cases.append({"venue": market["venue"], "instrument_id": market["instrument_id"], "date": market["day"],
                      "at": (current["book"] or {}).get("at"), "title": market["title"],
                      "detail": f"Observed source coverage of {progress['complete_days']} paired days through {progress['as_of_day']}. "
                                "Native outcome is pending.",
                      "data": {"rule": market, "depth": current["book"], "coverage": progress}})
    record = {"version": VERSION, "title": "Texas cold spells need two stations and two cold days",
              "hypothesis": "Serial dependence increases four-day two-airport cold risk and might improve the held forecast.",
              "target": "At least two days below the arithmetic average 20°F in four consecutive reporting days",
              "venues": ["Kalshi"], "date_window": {"earlier complete periods": [FIRST, FIT_END], "later observations": [FIT_END + 1, HELD_END]},
              "coverage": {"paired_climate_days": len(pairs), "live_native_instruments": len(live),
                           "live_books": sum(item["book"] is not None for item in live), "source_label_and_timing": audit},
              "metrics": metrics, "cases": cases, "missing": corpus["gaps"],
              "limits": ["Calendar year starts overlap for multi-year contracts. Reported scores are separated by horizon.",
                         "Retrospective NCEI temperatures are paired by exact station and day. The primary filed source is NWS and the historical data can be revised.",
                         "The analysis tests rounding sensitivity before the filed truncation. Dated complete NWS source reports are linked for selected actual events.",
                         "No native payouts for the new series are available. The archived public book omits its native exchange image clock."],
              "provenance": {"filed terms": TERMS, "frozen_input": str(frozen.relative_to(data)), "frozen_input_sha256": digest,
                             "source_sha256": research_model.run_sources(sys.modules[__name__], venue_ghcn, venue_storm)}}
    return research_model.write_record(data, record, {"historical_threshold_cases": scored, "live": live, "source_examples": corpus["independent_cli_spot"]})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--input", type=Path)
    parser.add_argument("--collect", action="store_true")
    args = parser.parse_args(argv)
    try:
        corpus = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else gather(args.data, collect=args.collect)
        print(evaluate(args.data, corpus))
    except CollectionPaused:
        print("Collection is stopped. Retained receipts are available.")
        return 2


if __name__ == "__main__":
    sys.exit(main())
