"""Audit first complete NWS rain reports, subsequent revisions and market marks."""
from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
import gzip
from hashlib import sha256
import json
from pathlib import Path
import statistics
import sys

from . import expansion_contract, expansion_weather, research_gefs, research_model, venue_archive
from .nbm import iso, utc


VERSION = "rain-first-report-v1"
STATIONS = ("KNYC", "KMDW", "KPHL", "KMIA", "KSFO", "KLAX")
FIRST, FIT_THROUGH, TEST_FROM, LAST = map(date.fromisoformat, ("2026-10-30", "2026-11-06", "2026-11-08", "2026-11-14"))
HORIZONS = (0, 15, 30, 60)
LIVE_DATE = "2026-11-14"
MAX_SPREAD = .25
UTC = timezone.utc


def _quote_at(rows, times, at):
    index = bisect_right(times, at) - 1
    if index < 0 or at - times[index] > timedelta(minutes=2):
        return None
    row = rows[index]
    yes, no = row.get("yes"), row.get("no")
    if yes is None or no is None or not 0 <= yes <= 1 or not 0 <= no <= 1:
        return None
    bid, offer = 1 - no, yes
    if offer - bid < -1e-9 or offer - bid > MAX_SPREAD + 1e-9:
        return None
    return {**row, "yes_bid": bid, "yes_offer": offer, "yes_midpoint": (bid + offer) / 2}


def _edition_rows(reports, cutoff):
    prior = {}
    for row in sorted(reports, key=lambda item: utc(item["published_at"])):
        if utc(row["published_at"]) > cutoff:
            continue
        truth = expansion_contract.rain_outcome(row["precip_as_reported"])
        if truth is None:
            continue
        instant = utc(row["published_at"])
        if instant in prior and prior[instant]["interpreted"] != truth:
            return []
        prior.setdefault(instant, {**row, "interpreted": truth})
    return list(prior.values())


def _case_data(data, candidate, known, through):
    day, station = date.fromisoformat(candidate["day"]), candidate["station"]
    if not expansion_contract.rain_rule_matches(day, station, candidate["rules_primary"]):
        return None, "unverified printed report terms"
    if (candidate["yes_payout"] not in ("0", "1") or not candidate["settlement_received_at"]
            or not candidate["settlement_source"] or utc(candidate["settlement_received_at"]) > through):
        return None, "public payout receipt unavailable by current capture cutoff"
    if not candidate["opened_at"] or not candidate["closed_at"]:
        return None, "missing contract trading window"
    if not known:
        return None, "missing same-station complete CLI product"
    try:
        official, reports, cutoff = expansion_contract.rain_report(data, day, known["product"], station=station,
                                                                  latest_end=utc(candidate["closed_at"]))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return None, expansion_contract.availability_error(exc)
    edits = _edition_rows(reports, cutoff)
    if not edits:
        return None, "no unambiguous complete report before the printed cutoff"
    result = int(candidate["yes_payout"])
    if official is None or expansion_contract.rain_outcome(official["precip_as_reported"]) != result:
        return None, "official winning report could not verify the recorded payout"
    first = edits[0]
    decision, close = utc(first["published_at"]), utc(candidate["closed_at"])
    if not utc(candidate["opened_at"]) <= decision < min(cutoff, close):
        return None, "first complete report was outside the live pre-cutoff market"
    quotes = {}
    issue, source = None, None
    try:
        history, source = expansion_contract.load_price_history(
            data, candidate["event_id"], {"id": candidate["instrument_id"]},
            decision - timedelta(minutes=5), min(cutoff, close, decision + timedelta(minutes=63)),
        )
        times = [utc(row["available_at"]) for row in history]
        for horizon in HORIZONS:
            at = decision + timedelta(minutes=horizon)
            if at < min(cutoff, close):
                quote = _quote_at(history, times, at)
                if quote is not None:
                    quotes[str(horizon)] = quote
    except (OSError, ValueError, TypeError, KeyError) as exc:
        issue = expansion_contract.availability_error(exc)

    close_books = []
    if str(day) == LIVE_DATE:
        close_books = venue_archive.books(data, "polymarket-us", candidate["instrument_id"], start=cutoff, end=close)
    return {"station": station, "date": str(day), "at": iso(decision), "market_close_at": candidate["closed_at"],
            "report_cutoff": iso(cutoff), "first": first, "editions": edits, "official": official, "truth": result,
            "market": candidate, "quote_source": source, "quotes": quotes, "quote_gap": issue,
            "report_cutoff_books": close_books}, None


def inputs(data, through):
    labels = {station: expansion_weather.daily_labels(data, station, FIRST, LAST) for station in STATIONS}
    markets = [row for row in venue_archive.export(data, venue="polymarket-us", family="daily-rain")
               if row["station"] in labels and row["instrument_id"].startswith("raincli-") and row["day"]
               and str(FIRST) <= row["day"] <= str(LAST)]
    rows, omitted = [], []
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = [(market, pool.submit(_case_data, data, market, labels[market["station"]].get(market["day"]), through))
                for market in markets]
        for market, future in jobs:
            row, issue = future.result()
            if row is not None:
                rows.append(row)
            else:
                omitted.append({"station": market["station"], "date": market["day"], "id": market["instrument_id"], "issue": issue})
    return {"version": VERSION, "through": iso(through), "rows": rows, "omitted": omitted}


def _stats(rows, group):
    return {"group": group, "station_days": len(rows), "calendar_dates": len({row["date"] for row in rows}),
            "different_report_amounts": sum(len({edition["precip_as_reported"] for edition in row["editions"]}) > 1 for row in rows),
            "first_to_winning_binary_change": sum(row["first"]["interpreted"] != row["truth"] for row in rows),
            "yes_cases": sum(row["truth"] for row in rows)}


def _scores(rows, group, horizon=None):
    stats = _stats(rows, group)
    if not rows:
        return []
    methods = {"first-climate-report": lambda row: row["first"]["interpreted"], "earlier-smoothed-report": lambda row: row["smoothed"]}
    if horizon is not None:
        methods["archived-display-midpoint"] = lambda row: row["quotes"][str(horizon)]["yes_midpoint"]
    return [{**stats, "method": method, "brier": statistics.mean((prediction(row) - row["truth"])**2 for row in rows)}
            for method, prediction in methods.items()]


def _case(row):
    report, official = row["first"], row["official"]
    changes = len({edition["precip_as_reported"] for edition in row["editions"]}) > 1
    description = (f"First full CLI reported {report['precip_as_reported']} inches at {report['published_at']}. "
                   f"Selected report listed {official['precip_as_reported']} inches. YES paid ${row['truth']}.")
    if row["quotes"]:
        clock, quote = min(row["quotes"].items(), key=lambda pair: int(pair[0]))
        description += f" Display midpoint {quote['yes_midpoint']:.1%} at the +{clock} minute sample."
    return {"title": f"{row['station']} {row['date']} · First complete rain report", "station": row["station"], "date": row["date"],
            "at": row["at"], "venue": "polymarket-us", "instrument_id": row["market"]["instrument_id"], "detail": description,
            "official_report": official, "market": row["market"], "evidence": {
                "first": report, "revisions": row["editions"], "first_amount_revised": changes,
                "rule_cutoff": row["report_cutoff"], "display": row["quotes"], "display_source": row["quote_source"],
                "locally_captured_books_after_cutoff": len(row["report_cutoff_books"]), "display_retrieval_gap": row["quote_gap"],
            }}


def run(data, frozen=None, *, through=None):
    if frozen is None:
        evidence = inputs(data, through or datetime.now(UTC))
        frozen, digest = research_model.freeze_inputs(data, evidence)
    else:
        raw = gzip.decompress(frozen.read_bytes())
        evidence, digest = json.loads(raw), sha256(raw).hexdigest()
    rows = evidence["rows"]
    development = [row for row in rows if row["date"] <= str(FIT_THROUGH)]
    test = [row for row in rows if row["date"] >= str(TEST_FROM)]
    history = {side: [row["truth"] for row in development if row["first"]["interpreted"] == side] for side in (0, 1)}
    for row in test:
        support = history[row["first"]["interpreted"]]
        row["smoothed"] = (sum(support) + .5) / (len(support) + 1)
    metrics = _scores(test, "All held-out reports")
    movement = []
    for horizon in HORIZONS:
        matched = [row for row in test if str(horizon) in row["quotes"]]
        metrics.extend(_scores(matched, f"The same display subset at +{horizon} minutes", horizon))
        pair = [row for row in matched if "0" in row["quotes"]]
        if horizon and pair:
            deltas = defaultdict(list)
            for row in pair:
                direction = 2 * row["first"]["interpreted"] - 1
                change = (row["quotes"][str(horizon)]["yes_midpoint"] - row["quotes"]["0"]["yes_midpoint"]) * direction
                deltas[row["date"]].append(change)
            values = [item for daily in deltas.values() for item in daily]
            movement.append({"minutes": horizon, "station_days": len(pair), "calendar_dates": len(deltas),
                             "mean_midpoint_move_toward_first_report": statistics.mean(values),
                             "date_interval_95": research_gefs._mean_interval(deltas)})

    live = [row for row in test if row["date"] == LIVE_DATE]
    ranked = sorted(test, key=lambda row: (
        row["first"]["interpreted"] != row["truth"],
        max((abs(quote["yes_midpoint"] - row["truth"]) for quote in row["quotes"].values()), default=-1),
        len(row["editions"])), reverse=True)
    cases = [_case(row) for row in ranked[:3]]
    newest = next((row for row in live if row["station"] == "KNYC"), None)
    if newest and newest not in ranked[:3]:
        cases.append(_case(newest))
    return research_model.write_record(data, {
        "version": VERSION, "title": "Does the first complete rain report predict the eventual payout?",
        "hypothesis": "The first complete climate-day report may resolve most short-term uncertainty before the exchange's stated revision cutoff. Later editions and market evidence determine how useful that early report is.",
        "target": "Polymarket US ≥0.01-inch daily rain contract under the printed NWS report rule",
        "venues": ["polymarket-us"],
        "date_window": {"fit": [str(FIRST), str(FIT_THROUGH)], "test": [str(TEST_FROM), str(LAST)]},
        "metrics": metrics, "cases": cases, "uncertainty": {"display_price_movement": movement},
        "coverage": {"development": _stats(development, "Earlier"),
                     "held_out": _stats(test, "Later"),
                     "newest_settled_date": _stats(live, "Follow-up"),
                     "locally_retained_books_after_cutoff": sum(len(row["report_cutoff_books"]) for row in live),
                     "omitted": dict(Counter(row["issue"] for row in evidence["omitted"])),
                     "side_calibration_case_counts": {key: len(group) for key, group in history.items()}},
        "limits": [
            "Independent test calendar dates begin after the final development climate-day report cutoff. Jeffreys smoothing uses only the earlier same report-side outcomes.",
            "Each case requires native station, exact climate day, printed threshold, trace or missing treatment, governing cutoff and a matching binary payout receipt. Same-issue editions with conflicting binary interpretations are ineligible.",
            "The first-report clock uses the original WFO issuance stamp. Original local receipt times are unavailable for those issues. Quotes use archived minute display marks and require an uncrossed spread up to 25 points.",
            "The newest date separately checks raw local order-book history from the printed revision cutoff through each venue close. Archived minute display values have no simulated execution or realized return.",
        ],
        "provenance": {"frozen_input": str(frozen.relative_to(data)), "input_sha256": digest, "through": evidence["through"],
                       "source_sha256": research_model.run_sources(sys.modules[__name__], expansion_contract, expansion_weather),
                       "reproduce": f"python -m weather_edge.research_rain_report --inputs {frozen}"},
    }, {"test": test, "earlier": development, "omitted": evidence["omitted"]})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--through", type=utc)
    parser.add_argument("--inputs", type=Path)
    args = parser.parse_args()
    print(run(args.data, args.inputs, through=args.through))


if __name__ == "__main__":
    main()
