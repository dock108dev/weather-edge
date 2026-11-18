"""Trace retained preliminary and complete NWS daily minima to exact native payouts."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import re
import sys

from . import expansion_weather, research_model, research_temperature_sources, venue_archive, venue_contracts
from .nbm import climate_window, iso, utc

VERSION = "daily-low-source-editions-v1"
PREVIEW_THROUGH = re.compile(r"^\s*(VALID TODAY (?:AS OF|THROUGH) [^\n]{2,60})", re.M)
NATIVE_FEEDS = ((venue_archive.KALSHI, "NWS Daily Climate Report"), (venue_archive.KALSHI, "The Weather Company"),
                (venue_archive.POLY, "NWS Daily Climate Report"))
NWS_GUIDE = "https://www.weather.gov/media/directives/010_pdfs_archived/pd01010004e.pdf"


def _edition(data, station, path):
    product = path.stem
    if not expansion_weather._valid_product(product, station):
        return None
    raw = path.read_bytes()
    body = raw.decode(errors="replace").replace("\r\n", "\n")
    report_date = expansion_weather.CLI_DATE.search(body)
    pil = expansion_weather.CLI_OFFICE[station][1]
    if report_date is None or not re.search(rf"^{pil}\s*$", body, re.M):
        return None
    month, number, year = report_date.groups()
    day = date(int(year), expansion_weather.MONTHS[month], int(number))
    at = datetime.strptime(product[:12], "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
    opens, closes = climate_window(day, expansion_weather.STATIONS[station][3])
    if at >= closes:
        parsed = expansion_weather.raw_report(data, station, day, product)
        if parsed and parsed["low_f"] is not None:
            return {**parsed, "edition": "complete", "raw_path": str(path.relative_to(data))}
        return None
    printed = PREVIEW_THROUGH.search(body)
    current = re.search(r"^\s*TODAY[ \t]*$", body, re.M)
    if not opens <= at < closes or printed is None or not current:
        return None
    observations = [(value, clock) for field, value, clock in expansion_weather.TEMPERATURE.findall(body) if field == "MINIMUM"]
    if len(observations) != 1 or (value := expansion_weather.parse_number(observations[0][0])) is None:
        return None
    return {"station": station, "date": str(day), "published_at": iso(at), "available_at": iso(at), "edition": "preview",
            "source": f"{expansion_weather.IEM}/api/1/nwstext/{product}", "product": product, "low_f": int(value),
            "low_at_local_standard": observations[0][1] or None, "printed_through": printed[1].strip(),
            "raw_path": str(path.relative_to(data)), "sha256": hashlib.sha256(raw).hexdigest()}


def _weather(data):
    grouped, skipped = defaultdict(list), Counter()
    root = data / "expansion" / "cli"
    for station in sorted(expansion_weather.CLI_OFFICE):
        for path in sorted((root / station).glob("*.txt")):
            try:
                row = _edition(data, station, path)
            except (OSError, ValueError, KeyError, TypeError):
                row = None
            if row:
                grouped[station, row["date"]].append(row)
            else:
                skipped["raw artifact lacks a qualifying minimum edition"] += 1
    timelines = {}
    for (station, day), versions in sorted(grouped.items()):
        ordered = sorted(versions, key=lambda row: (row["published_at"], row["product"]))
        preview = [row for row in ordered if row["edition"] == "preview"]
        complete = [row for row in ordered if row["edition"] == "complete"]
        if not complete:
            skipped["station-day with only a preview"] += 1
            continue
        opens, closes = climate_window(day, expansion_weather.STATIONS[station][3])
        timelines[station, day] = {"station": station, "date": day, "climate_starts_at": iso(opens), "climate_ends_at": iso(closes),
                                   "previews": preview, "complete": complete,
                                   "first_complete": complete[0], "last_complete": complete[-1]}
    return timelines, skipped


def _native(data, timelines):
    results, missing = [], Counter()
    with venue_archive.connect(data) as db:
        found = [dict(row) for row in db.execute("""SELECT venue,instrument_id,event_id,series_id,station,day,label,units,operator,lower,
            upper,opened_at,closed_at,climate_starts_at,climate_ends_at,resolution_source,rules_primary,rules_secondary,rules_url,
            report_policy,fallback,expiration_value,settlement_at,settlement_received_at,settlement_receipt_id,source,yes_payout
            FROM markets WHERE family='daily-low' AND status='settled' AND station IS NOT NULL
              AND yes_payout IN ('0','1') AND settlement_receipt_id IS NOT NULL ORDER BY venue,station,day,instrument_id""")]
    for market in found:
        pair = market["station"], market["day"]
        timeline = timelines.get(pair)
        if not timeline:
            missing["binary payout with no retained complete station minimum"] += 1
            continue
        feed = market["venue"], market["resolution_source"]
        primary = (market["rules_primary"] or "").lower()
        if feed not in NATIVE_FEEDS or market["units"] != "°F" or not any(word in primary for word in ("lowest", "minimum")):
            missing["no qualifying explicit Fahrenheit minimum rule"] += 1
            continue
        strike = venue_contracts.temperature_window(market)
        if not strike:
            missing["unsupported exact strike"] += 1
            continue
        payout = int(market["yes_payout"])
        at = utc(market["settlement_at"]) if market["settlement_at"] else None
        older = [edition for edition in timeline["complete"] if at and utc(edition["published_at"]) <= at]
        recent = older[-1] if older else None
        matches = market["climate_starts_at"], market["climate_ends_at"]
        known_window = bool(all(matches) and tuple(map(utc, matches)) ==
                            (utc(timeline["climate_starts_at"]), utc(timeline["climate_ends_at"])))
        last_preview = timeline["previews"][-1] if timeline["previews"] else None
        numerical = research_temperature_sources._expiration(market["expiration_value"])
        if numerical is not None and (numerical != int(numerical) or research_temperature_sources._win(strike, numerical) != payout):
            missing["native numeric ruling has incompatible precision or payout"] += 1
            numerical = None

        def hypothetical(which):
            return research_temperature_sources._win(strike, which["low_f"]) if which else None

        results.append({"station": pair[0], "date": pair[1], "venue": market["venue"], "source": feed[1], "market": market,
                        "bounds_f": strike, "native_yes": payout, "native_low_f": int(numerical) if numerical is not None else None,
                        "exact_window": known_window, "last_preview_would_pay": hypothetical(last_preview),
                        "first_complete_would_pay": hypothetical(timeline["first_complete"]),
                        "last_complete_would_pay": hypothetical(timeline["last_complete"]),
                        "last_complete_by_settlement": recent, "last_complete_by_settlement_would_pay": hypothetical(recent),
                        "complete_edition_available_by_explicit_native_clock": bool(recent),
                        "primary_low_secondary_high_wording": "highest temperature" in (market["rules_secondary"] or "").lower()})
    return results, missing


def gather(data):
    timelines, weather_gaps = _weather(data)
    settlements, native_gaps = _native(data, timelines)
    return {"version": VERSION, "weather": list(timelines.values()), "markets": settlements,
            "gaps": {"weather": dict(weather_gaps), "native": dict(native_gaps)}}


def _weather_metrics(timelines):
    previews = [row for row in timelines if row["previews"]]
    updated = [row for row in previews if row["previews"][-1]["low_f"] != row["first_complete"]["low_f"]]
    return {"group": "Retained exact-station NWS minima", "method": "nws-editions",
            **research_model._independent_counts(timelines), "stations": len({row["station"] for row in timelines}),
            "complete_editions": sum(len(row["complete"]) for row in timelines),
            "preliminary_editions": sum(len(row["previews"]) for row in previews), "station_days_with_preview_and_complete": len(previews),
            "station_days_where_low_changed_after_preview": len(updated),
            "station_days_where_final_low_exceeded_preview": sum(row["first_complete"]["low_f"] > row["previews"][-1]["low_f"] for row in updated),
            "station_days_with_two_or_more_complete_editions": sum(len(row["complete"]) >= 2 for row in timelines),
            "station_days_with_changed_complete_low": sum(len({edition["low_f"] for edition in row["complete"]}) > 1 for row in timelines)}


def _market_metrics(rows, timelines):
    numerical = {(row["station"], row["date"], row["native_low_f"]) for row in rows if row["native_low_f"] is not None}
    metrics = {**research_model._independent_counts(rows), "bucket_cases": len(rows),
               "station_days_with_native_numeric_low": len({(station, day) for station, day, _ in numerical}),
               "native_clock_rows": sum(bool(row["market"]["settlement_at"]) for row in rows),
               "exact_window_rows": sum(row["exact_window"] for row in rows),
               "native_numeric_vs_first_complete_changes": sum(low != timelines[station, day]["first_complete"]["low_f"] for station, day, low in numerical),
               "native_numeric_vs_last_complete_changes": sum(low != timelines[station, day]["last_complete"]["low_f"] for station, day, low in numerical),
               "station_days_with_conflicting_secondary_high_wording": len({(row["station"], row["date"]) for row in rows if row["primary_low_secondary_high_wording"]})}
    for field in ("last_preview", "first_complete", "last_complete", "last_complete_by_settlement"):
        supported = [row for row in rows if row[field + "_would_pay"] is not None]
        metrics[field + "_comparable_payouts"] = len(supported)
        metrics[field + "_disagreeing_payouts"] = sum(row[field + "_would_pay"] != row["native_yes"] for row in supported)
    return metrics


def _pairs(rows):
    by_strike = defaultdict(dict)
    for row in rows:
        key = row["station"], row["date"], tuple(row["bounds_f"])
        by_strike[key].setdefault(row["venue"], []).append(row)
    comparable = []
    for choices in by_strike.values():
        primary, secondary = choices.get(venue_archive.KALSHI, []), choices.get(venue_archive.POLY, [])
        if len(primary) == len(secondary) == 1:
            comparable.append({"station": primary[0]["station"], "date": primary[0]["date"], "kalshi": primary[0], "polymarket": secondary[0]})
    return comparable


def _case(timeline, row=None):
    first, last = timeline["first_complete"], timeline["last_complete"]
    preview = timeline["previews"][-1] if timeline["previews"] else None
    description = (f"The first complete report gave {first['low_f']}°F and the last retained edition gave {last['low_f']}°F. "
                   if last != first else f"The complete report gave {first['low_f']}°F. ")
    if preview:
        description = f"The same-day preview reported a low of {preview['low_f']}°F. " + description
    if row:
        value = row["native_low_f"]
        if value is not None:
            description += f"The native numerical low was {value}°F. "
        description += f"Native YES paid ${row['native_yes']}."
    return {"station": timeline["station"], "date": timeline["date"], "at": first["published_at"],
            "title": f"{timeline['station']} · The raw minimum through successive editions",
            "venue": row["venue"] if row else None, "instrument_id": row["market"]["instrument_id"] if row else None,
            "detail": description.strip(), "climate_editions": timeline, "payout_comparison": row}


def _rank(row, weather):
    timeline = weather[row["station"], row["date"]]
    prior = timeline["previews"][-1]["low_f"] if timeline["previews"] else timeline["first_complete"]["low_f"]
    return (row["first_complete_would_pay"] != row["last_complete_would_pay"],
            row["last_complete_would_pay"] != row["native_yes"],
            row["last_preview_would_pay"] is not None and row["last_preview_would_pay"] != row["native_yes"],
            abs(prior - timeline["first_complete"]["low_f"]))


def evaluate(data, inputs):
    if inputs["version"] != VERSION or not inputs["weather"]:
        raise ValueError("Expected retained exact daily-low station edition inputs")
    frozen, digest = research_model.freeze_inputs(data, inputs)
    weather = {(row["station"], row["date"]): row for row in inputs["weather"]}
    native = inputs["markets"]
    metrics = [_weather_metrics(inputs["weather"])]
    for venue, source in NATIVE_FEEDS:
        rows = [row for row in native if (row["venue"], row["source"]) == (venue, source)]
        if rows:
            metrics.append({"group": source, "method": "native verified payout", "venue": venue, **_market_metrics(rows, weather)})
    pairs = _pairs(native)
    disagreement = [row for row in pairs if row["kalshi"]["native_yes"] != row["polymarket"]["native_yes"]]
    metrics.append({"group": "Same integer strike at two venues", "method": "observed binary payouts",
                    **research_model._independent_counts(pairs), "different_actual_payouts": len(disagreement),
                    "both_reporting_windows_explicit_and_matched": sum(row["kalshi"]["exact_window"] and row["polymarket"]["exact_window"] for row in pairs)})
    cases, seen = [], set()
    for difference in disagreement[:3]:
        pair = difference["station"], difference["date"]
        if pair in seen:
            continue
        seen.add(pair)
        cases.extend(_case(weather[pair], difference[venue]) for venue in ("kalshi", "polymarket"))
    ranked = sorted(native, key=lambda row: _rank(row, weather), reverse=True)
    for venue, source in NATIVE_FEEDS:
        eligible = [row for row in ranked if (row["venue"], row["source"]) == (venue, source)
                    and (row["station"], row["date"]) not in seen]
        if eligible:
            choice = eligible[0]
            key = choice["station"], choice["date"]
            cases.append(_case(weather[key], choice))
            seen.add(key)
    for row in ranked:
        key = row["station"], row["date"]
        if len(cases) >= 8:
            break
        if key not in seen and row["station"] not in {case["station"] for case in cases}:
            seen.add(key)
            cases.append(_case(weather[key], row))
    days = [row["date"] for row in inputs["weather"]]
    record = {"version": VERSION, "title": "Preliminary, complete, and revised editions of daily lows",
              "hypothesis": "A same-day partial minimum can still change, and some complete raw report revisions may change the threshold implied by the first available report.",
              "target": "Actual published NWS daily-low editions and independently verified exact native threshold payouts",
              "venues": [venue_archive.POLY, venue_archive.KALSHI], "date_window": {"retained_station_days": [min(days), max(days)]},
              "coverage": {"raw_low_station_days": len(weather), "eligible_native_payouts": len(native), "same_integer_strike_pairs": len(pairs)},
              "missing": inputs["gaps"], "metrics": metrics, "cases": cases,
              "limits": ["Preview editions must explicitly report values through part of the named climate day. The measured interim minimum is separate from a complete raw result after the fixed local standard day ends.",
                         "This uses the locally retained raw WFO products and their original publication times. There may be other historical issues or correction editions outside the archive.",
                         "The all-edition first/last comparison uses physical raw results and actual payouts. Where the venue prints an exact original settlement clock, a separate comparison restricts complete issues to those published by then.",
                         "Matching integer bounds and observed payouts identifies common strikes. The clock check still requires separately verified station and reporting windows under each native source. Local late collection of a settled market does not establish its original payout clock.",
                         "A numerical value matching a WFO report does not verify the edition consulted by The Weather Company."],
              "provenance": {"frozen_input": str(frozen.relative_to(data)), "frozen_input_sha256": digest, "nws_directive": NWS_GUIDE,
                             "source_sha256": research_model.run_sources(sys.modules[__name__], expansion_weather, research_temperature_sources, venue_contracts)}}
    return research_model.write_record(data, record, {"weather_editions": inputs["weather"], "native_payouts": native, "same_integer_strikes": pairs})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--input", type=Path)
    args = parser.parse_args()
    inputs = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else gather(args.data)
    print(evaluate(args.data, inputs))


if __name__ == "__main__":
    main()
