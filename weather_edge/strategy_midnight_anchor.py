"""Test a preliminary midnight temperature posterior against native price proxies."""

import argparse
from collections import Counter, defaultdict
from datetime import timedelta
import gzip
from hashlib import sha256
import json
from pathlib import Path
from statistics import mean

from . import collector, research_model, strategy_midnight_metar as weather, strategy_official_display as official
from . import strategy_payout_timing, venue_archive, venue_clock_study, venue_history, venue_sources
from .market import iso, parse_time
from .model import _outcome
from .strategy_market import normalized_prices


VERSION = "midnight-metar-market-anchor-v1"
FIT = ("2026-10-01", "2026-10-11")
SELECT = ("2026-10-13", "2026-10-21")
WEIGHTS = (0, 0.1, 0.25, 0.5, 1)
WAIT = timedelta(minutes=3)


def inputs(data, source):
    prior = json.loads(source.read_bytes())
    archive = prior["provenance"]
    raw = (data / archive["replay_details_path"]).read_bytes()
    if prior["version"] != weather.VERSION or sha256(raw).hexdigest() != archive["replay_details_sha256"]:
        raise ValueError("Midnight METAR study checksum mismatch")
    evidence = json.loads(gzip.decompress(raw))
    first = []
    lock = parse_time(SELECT[0] + "T00:00:00Z")
    for row in evidence["fit"]:
        if FIT[0] <= row["date"] <= FIT[1] and row["archive_receipt"]:
            known = [version for version in row["official_editions"] if parse_time(version["at"]) < lock]
            if known:
                first.append({**row, "actual_f": known[-1]["amount"], "official_editions": known})
    earlier = [r for r in evidence["fit"] if SELECT[0] <= r["date"] <= SELECT[1] and r["archive_receipt"]]
    held = [r for r in evidence["held"]["Official climate high and low"] if r["archive_receipt"]]
    if not first or not earlier or not held:
        raise ValueError("Original NOAA MADIS receipt evidence is required on independent dates")
    return prior, first, earlier, held


def _fit_label(row, native, lock):
    """Use the native payout before lock when known, otherwise the earlier public CLI."""
    if official.known_settlement(native, lock):
        return [b["payout"] for b in native["buckets"]], "native payout known before lock"
    reports = [v for v in row["official_editions"] if parse_time(v["at"]) < lock]
    if not reports:
        return [], None
    return [_outcome(b, reports[-1]["amount"]) for b in native["buckets"]], "public climate report before lock"


def candidates(data, earlier, held):
    native, _ = strategy_payout_timing.assemble(data)
    eligible = defaultdict(list)
    for row in native:
        if row["family"] in weather.FAMILIES and (event := official.normalized_event(row)):
            eligible[row["venue"], row["station"], row["date"], row["family"]].append(event)
    groups, gaps = defaultdict(list), Counter()
    lock = parse_time(str(weather.TEST[0]) + "T00:00:00Z")
    for phase, rows in (("selection", earlier), ("later test", held)):
        for row in rows:
            for venue in venue_archive.VENUES:
                key = phase, venue, row["family"]
                matched = eligible[venue, row["station"], row["date"], row["family"]]
                if len(matched) != 1:
                    gaps[*key, "no unique native exact settlement partition"] += 1
                    continue
                market, = matched
                at = parse_time(row["archive_receipt"]["available_at"]) + WAIT
                if at >= parse_time(row["official_editions"][0]["at"]):
                    gaps[*key, "first official edition already available"] += 1
                    continue
                if market["first_native_settlement_at"] and at >= parse_time(market["first_native_settlement_at"]):
                    gaps[*key, "native payout already available"] += 1
                    continue
                market_open = all(parse_time(b["opened_at"]) <= at < parse_time(b["closed_at"]) for b in market["buckets"])
                if not market_open:
                    gaps[*key, "outside common native open and close clocks"] += 1
                    continue
                truth, basis = _fit_label(row, market, lock) if phase == "selection" else ([b["payout"] for b in market["buckets"]], "verified later native payout")
                if not truth:
                    gaps[*key, "no labels before weight selection"] += 1
                    continue
                groups[key].append({"weather": row, "native": market, "truth": truth, "labels": basis, "venue": venue,
                                    "date": row["date"], "station": row["station"], "family": row["family"], "at": iso(at)})
    return groups, gaps


def acquire(data, groups, limit):
    fetch = venue_sources.PublicGET(data)
    counts = Counter()
    with venue_archive.connect(data) as db:
        complete = {(row["venue"], row["scope"]) for row in db.execute(
            "SELECT venue,scope FROM progress WHERE kind IN (?,?) AND state='complete'",
            (venue_history.DISPLAY_KIND + "-window", venue_clock_study.MINUTE_CANDLE_KIND + "-window"))}
    for key, rows in sorted(groups.items()):
        for row in official.spread_dates(rows, limit):
            if collector.paused(data):
                return counts
            venue = row["venue"]
            at = parse_time(row["at"]).replace(second=0, microsecond=0)
            native = row["native"]
            if venue == venue_archive.KALSHI:
                prefix = "midnight-metar/" + native["event_id"] + "/"
                if (venue, f"{prefix}{iso(at)}#1") not in complete:
                    tickers = [b["instrument_id"] for b in native["buckets"]]
                    counts[*key, "minute candle requests"] += 1
                    counts[*key, "minute records"] += venue_clock_study._batch(data, fetch, at, tickers, 1, before=4, after=2, prefix=prefix)
            else:
                start, end = at - timedelta(minutes=4), at + timedelta(minutes=2)
                for b in native["buckets"]:
                    scope = f"{b['instrument_id']}/{iso(start)}/{iso(end)}"
                    if (venue, scope) not in complete:
                        counts[*key, "display requests"] += 1
                        counts[*key, "minute records"] += venue_history._poly_price(data, fetch, b, window=(start, end))
    return counts


def priced(row, residuals, price):
    at = parse_time(row["at"])
    market = row["native"]
    symbols = [b["instrument_id"] for b in market["buckets"]]
    marks = [price(row["venue"], ident, at) for ident in symbols]
    if any(not q or not timedelta(0) <= at - parse_time(q["at"]) <= timedelta(minutes=2) for q in marks):
        return None
    coherent = normalized_prices([q.get("midpoint", q.get("mid")) for q in marks])
    if not coherent:
        return None
    key = row["station"], row["date"], row["family"]
    forecast, = weather.run([row["weather"]], residuals, {key: market})
    probabilities = forecast["estimates"][weather.MODELS[2]]["probabilities"]
    return {**row, "market": coherent, "weather_probabilities": probabilities, "marks": marks}


def score(rows, weight):
    daily = defaultdict(list)
    for row in rows:
        errors = [((1 - weight) * market + weight * weather - outcome) ** 2
                  for market, weather, outcome in zip(row["market"], row["weather_probabilities"], row["truth"], strict=True)]
        daily[row["date"]].append(mean(errors))
    return mean(mean(values) for values in daily.values()) if daily else None


def compare(groups, residuals, price):
    chosen, all_rows, metrics, cases = {}, {}, [], []
    for key, rows in sorted(groups.items()):
        all_rows[key] = [found for row in rows if (found := priced(row, residuals, price))]
    for (phase, venue, family), rows in sorted(all_rows.items()):
        choose_key = venue, family
        if phase == "selection" and rows:
            chosen[choose_key] = min(WEIGHTS, key=lambda w: score(rows, w))
    for (phase, venue, family), rows in sorted(all_rows.items()):
        locked = chosen.get((venue, family))
        metrics.append({"window": phase, "venue": venue, "family": family, "candidate_events": len(groups[phase, venue, family]),
                        "priced_events": len(rows), "station_days": len({(row["station"], row["date"]) for row in rows}),
                        "calendar_dates": len({row["date"] for row in rows}), "native_buckets": sum(len(row["truth"]) for row in rows),
                        "locked_weather_weight": locked, "venue_only_brier": score(rows, 0), "weather_only_brier": score(rows, 1),
                        "locked_blend_brier": score(rows, locked) if locked is not None else None})
        if rows and phase == "later test":
            largest = max(rows, key=lambda row: max(abs(a - b) for a, b in zip(row["market"], row["weather_probabilities"], strict=True)))
            index = max(range(len(largest["truth"])), key=lambda i: abs(largest["market"][i] - largest["weather_probabilities"][i]))
            b = largest["native"]["buckets"][index]
            cases.append({"station": largest["station"], "date": largest["date"], "venue": venue, "at": largest["at"],
                          "instrument_id": b["instrument_id"], "title": f"{largest['station']} preliminary {family.removeprefix('daily-')} near midnight",
                          "detail": f"Weather {largest['weather_probabilities'][index]:.1%}, native displayed midpoint {largest['market'][index]:.1%}, verified YES ${b['payout']}.",
                          "data": largest})
    return metrics, cases, {" / ".join(key): rows for key, rows in all_rows.items()}


def research(data, source, *, fetch):
    prior, fit, earlier, held = inputs(data, source)
    groups, gaps = candidates(data, earlier, held)
    acquired = acquire(data, groups, fetch) if fetch else Counter()
    with venue_archive.connect(data) as db:
        db.execute("BEGIN")
        metrics, cases, replay = compare(groups, weather.train(fit), official.price_reader(data, db, kalshi_minutes=True))
    record = {"version": VERSION, "title": "Do midnight preliminary temperatures improve venue probabilities?",
              "hypothesis": "An earlier-selected blend of the midnight METAR forecast and native midpoint improves paid weather buckets on separate dates.",
              "target": "Settled native highs and lows, before publication of the first retained complete daily climate bulletin.",
              "venues": list(venue_archive.VENUES), "date_window": {"from": FIT[0], "through": str(weather.TEST[1]), "fit": FIT,
                                                                 "select": SELECT, "holdout": list(map(str, weather.TEST))},
              "metrics": metrics, "cases": cases, "limits": ["The report must have an original archived NOAA MADIS receipt. The three-minute wait is fixed.",
                "Every constituent needs a coherent display vector and a mark within two minutes. These marks contribute to prediction scores.",
                "Earlier prospectively available official weather labels stand in when the native payout clock is unverified. The later target always uses actual paid dollars.",
                "A near-midnight METAR value is preliminary and can disagree with the final exact station report."],
              "provenance": {"engine": VERSION, "midnight_study": prior["id"], "weather_weight_grid": WEIGHTS, "calibration_rows": len(fit),
                             "targeted_public_history": {" / ".join(k): v for k, v in acquired.items()},
                             "gaps": {" / ".join(k): v for k, v in gaps.items()},
                             "implementation_sha256": sha256(Path(__file__).read_bytes()).hexdigest()}}
    return research_model.write_record(data, record, {"panels": replay, "earlier_weather_fit": fit})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=venue_archive.DEFAULT_DATA)
    parser.add_argument("--fetch-events", type=int, default=0, help="Acquire this many eligible events per venue, family and period.")
    parser.add_argument("midnight_study", type=Path)
    args = parser.parse_args()
    if args.fetch_events < 0:
        parser.error("The event bound must be nonnegative")
    path = research(args.data, args.midnight_study, fetch=args.fetch_events)
    result = json.loads(path.read_bytes())
    print(json.dumps({"path": str(path), "metrics": result["metrics"], "provenance": result["provenance"]}, indent=2))


if __name__ == "__main__":
    main()
