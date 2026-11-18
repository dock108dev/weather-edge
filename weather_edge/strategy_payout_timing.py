"""Audit independent weather, exchange settlement, and archived cash-release clocks."""

import argparse
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
from statistics import median

from . import expansion_weather, research_model, venue_archive
from .market import decimal, iso, parse_time
from .nbm import climate_window


VERSION = "daily-market-payout-clocks-v1"
START, END = "2026-10-01", "2026-11-14"
RAW_ROOTS = ("archive-raw/cli", "expansion/cli", "research/evidence/high-physical/raw-cli")
FAMILIES = ("daily-high", "daily-low", "daily-rain")
MARK_FIELDS = ("venue", "instrument_id", "event_id", "family", "station", "day", "climate_starts_at", "climate_ends_at",
               "resolution_source", "day_basis", "rules_primary", "opened_at", "closed_at", "settlement_at", "settlement_received_at",
               "settlement_source", "yes_payout", "no_payout", "settlement_receipt_id", "operator", "units", "lower", "upper", "label",
               "expiration_value")


def raw_climate_reports(data, *, day_from=START, day_through=END):
    supports = defaultdict(dict)
    for root in RAW_ROOTS:
        for path in (data / root).glob("K???/*.txt"):
            station, product = path.parent.name, path.stem
            if station not in expansion_weather.CLI_OFFICE or not expansion_weather._valid_product(product, station):
                continue
            raw = path.read_bytes()
            body = raw.decode("utf-8", errors="replace").replace("\r\n", "\n")
            label = expansion_weather.CLI_DATE.search(body)
            if label is None or re.search(rf"^{expansion_weather.CLI_OFFICE[station][1]}\s*$", body, re.M) is None:
                continue
            month, day_number, year = label.groups()
            day = date(int(year), expansion_weather.MONTHS[month], int(day_number))
            if not str(day_from) <= str(day) <= str(day_through):
                continue
            published = datetime.strptime(product[:12], "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
            first, last = climate_window(day, expansion_weather.STATIONS[station][3])
            period = "YESTERDAY" if re.search(r"^\s*YESTERDAY\b", body, re.M) else "TODAY"
            if published < last or period == "TODAY" and not expansion_weather.TODAY_THROUGH_CLOSE.search(body):
                continue
            temperatures = {tag: expansion_weather.parse_number(value) for tag, value, _ in expansion_weather.TEMPERATURE.findall(body)}
            counts = (temperatures.get("MAXIMUM"), temperatures.get("MINIMUM"),
                      expansion_weather._reported_amount(body, "PRECIPITATION", period))
            key = station, str(day), iso(first), iso(last)
            for family, amount in zip(FAMILIES, counts, strict=True):
                if amount in (None, "M", "MM"):
                    continue
                proof = {"at": iso(published), "source": f"{expansion_weather.IEM}/api/1/nwstext/{product}",
                         "path": str(path.relative_to(data)), "sha256": sha256(raw).hexdigest(), "amount": amount,
                         "product": product}
                earlier = supports[key, family].get(product)
                if earlier and earlier["sha256"] != proof["sha256"]:
                    supports[key, family][product] = {**earlier, "same_product_body_changed": True}
                elif not earlier:
                    supports[key, family][product] = proof
    return {key: sorted((row for row in earlier.values() if not row.get("same_product_body_changed")),
                        key=lambda row: row["at"]) for key, earlier in supports.items()}


def clocks(data):
    with venue_archive.connect(data) as db:
        db.execute("BEGIN")
        markets = db.execute(f"""SELECT {','.join(MARK_FIELDS)} FROM markets WHERE family IN (?,?,?) AND day BETWEEN ? AND ?
                          AND station IS NOT NULL AND settlement_receipt_id IS NOT NULL AND status='settled'
                          ORDER BY venue, family, station, day, event_id, instrument_id""", (*FAMILIES, START, END)).fetchall()
        daily = db.execute("""SELECT instrument_id, at, receipt_id, body FROM history WHERE venue=? AND kind='clearing-daily-mark'
                            ORDER BY at, instrument_id""", (venue_archive.POLY,)).fetchall()
        reports = defaultdict(list)
        keys = {row["instrument_id"] for row in markets if row["venue"] == venue_archive.POLY}
        for row in daily:
            if row["instrument_id"] not in keys:
                continue
            raw = json.loads(row["body"])
            if raw.get("report_publication_bound") and raw.get("retrospective_payout_match"):
                reports[row["instrument_id"]].append({**dict(row), "body": raw})
    return markets, reports


def _clearing_publication(buckets, reports, climate_end, report_start):
    """Require a complete clearing file with zero OI and all verified payout amounts."""
    source_files = defaultdict(dict)
    for market in buckets:
        ident = market["instrument_id"]
        for point in reports.get(ident, ()):
            data = point["body"]
            stamp = parse_time(data["report_publication_bound"])
            if stamp < max(climate_end, report_start) or stamp != parse_time(point["at"]):
                continue
            body = data["raw_market_report_row"]
            if decimal(body["Open Interest"]) != 0 or decimal(body["Settlement Price"]) != decimal(market["yes_payout"]):
                continue
            source_files[stamp, data["report_source"]][ident] = point
    candidates = [(at, url, rows) for (at, url), rows in source_files.items() if len(rows) == len(buckets)]
    if not candidates:
        return None
    stamp, source, rows = min(candidates, key=lambda selected: (selected[0], selected[1]))
    return {"at": iso(stamp), "source": source, "rows": rows,
            "clock_meaning": "Clearing report publication upper bound. Venue settlement time is absent."}


def _hours(after, before):
    return (parse_time(after) - parse_time(before)).total_seconds() / 3600


def assemble(data):
    weather = raw_climate_reports(data)
    markets, reports = clocks(data)
    grouped = defaultdict(list)
    for row in map(dict, markets):
        if not row["settlement_received_at"]:
            continue
        if row["venue"] == venue_archive.POLY and row["resolution_source"] != "NWS Daily Climate Report":
            continue
        if row["climate_starts_at"] and row["climate_ends_at"]:
            start, end = parse_time(row["climate_starts_at"]), parse_time(row["climate_ends_at"])
            basis = "matching native climate window"
        elif (row["venue"] == venue_archive.KALSHI and row["day_basis"] == "contract rule"
              and row["station"] in expansion_weather.CLI_OFFICE
              and any(f"({key})" in (row["rules_primary"] or "") for key in
                      (row["station"], expansion_weather.CLI_OFFICE[row["station"]][1]))):
            start, end = climate_window(date.fromisoformat(row["day"]), expansion_weather.STATIONS[row["station"]][3])
            basis = "same-station and date NWS comparison clock"
        else:
            continue
        key = row["venue"], row["family"], row["station"], row["day"], row["event_id"], iso(start), iso(end), basis
        grouped[key].append(row)
    rows, gaps = [], Counter()
    for (venue, family, station, day, event, start, end, basis), buckets in grouped.items():
        if any(row["yes_payout"] not in ("0", "1") for row in buckets) or (family != "daily-rain" and
                                                                                 sum(row["yes_payout"] == "1" for row in buckets) != 1):
            gaps[venue, "Settled payouts lack a complete binary partition"] += 1
            continue
        cli = weather.get(((station, day, start, end), family))
        if not cli:
            gaps[venue, "Complete raw climate report for exact clock window absent"] += 1
            continue
        raw, latest = cli[0], cli[-1]
        timestamps = [parse_time(row["settlement_at"]) for row in buckets if row["settlement_at"]]
        first_native = iso(min(timestamps)) if timestamps and len(timestamps) == len(buckets) else None
        last_native = iso(max(timestamps)) if first_native else None
        receipt = iso(max(parse_time(row["settlement_received_at"]) for row in buckets))
        published = (_clearing_publication(buckets, reports, parse_time(end), parse_time(raw["at"]))
                     if venue == venue_archive.POLY else None)
        rows.append({"venue": venue, "station": station, "date": day, "event_id": event, "family": family,
                     "climate_start": start, "climate_end": end, "climate_clock_basis": basis,
                     "first_complete_cli": raw, "latest_local_cli": latest,
                     "raw_cli_versions": cli, "first_native_settlement_at": first_native,
                     "native_final_settlement_at": last_native, "all_public_payouts_received_at": receipt,
                     "clearing_publication": published, "buckets": buckets})
    return rows, {"/".join(key): value for key, value in gaps.items()}


def metrics(rows):
    outcomes = []
    for venue in venue_archive.VENUES:
        for family in FAMILIES:
            group = [row for row in rows if row["venue"] == venue and row["family"] == family]
            if not group:
                continue
            for name, selector in (
                ("First complete raw CLI", lambda row: row["first_complete_cli"]["at"]),
                ("Exchange last settlement timestamp", lambda row: row["native_final_settlement_at"]),
                ("Clearing-file publication upper bound", lambda row: (row["clearing_publication"] or {}).get("at")),
                ("Local receipt of all bucket payouts", lambda row: row["all_public_payouts_received_at"]),
            ):
                paired = [(row, at) for row in group if (at := selector(row))]
                if not paired:
                    continue
                lag = [_hours(at, row["first_complete_cli"]["at"]) for row, at in paired]
                outcomes.append({"venue": venue, "family": family, "clock": name,
                                 "events": len(paired), "dates": len({row["date"] for row, _ in paired}),
                                 "station_days": len({(row["station"], row["date"]) for row, _ in paired}),
                                 "median_hours_after_climate_end": median(_hours(at, row["climate_end"]) for row, at in paired),
                                 "median_hours_after_first_local_cli_version": median(lag),
                                 "before_first_local_cli_version": sum(value < 0 for value in lag),
                                 "distinct_stations": len({row["station"] for row, _ in paired})})
    return outcomes


def _case(row, stamp, message):
    bucket = next((row for row in row["buckets"] if row["yes_payout"] == "1"), row["buckets"][0])
    return {"venue": row["venue"], "station": row["station"], "date": row["date"], "event_id": row["event_id"],
            "instrument_id": bucket["instrument_id"], "at": stamp, "title": message,
            "detail": f"NWS climate window ended {row['climate_end']}. Original report {row['first_complete_cli']['at']}. Evidence clock {stamp}."}


def report(data):
    rows, gaps = assemble(data)
    selection = []
    for row in rows:
        if row["clearing_publication"]:
            selection.append(_case(row, row["clearing_publication"]["at"], "Clearing CSV verifies all constituent payouts"))
    exchange = [row for row in rows if row["native_final_settlement_at"]]
    if exchange:
        earliest = min(exchange, key=lambda row: _hours(row["native_final_settlement_at"], row["first_complete_cli"]["at"]))
        latest = max(exchange, key=lambda row: _hours(row["native_final_settlement_at"], row["first_complete_cli"]["at"]))
        for row in (earliest, latest):
            selection.append(_case(row, row["native_final_settlement_at"], "Exchange clock relative to archived complete weather report"))
    record = {"version": VERSION, "title": "When did the archived weather positions settle?",
              "hypothesis": "Native settlement, original weather publication, and first local payout receipts have materially different times.",
              "target": "Matched daily native weather windows, exact binary outcomes, and independently proven cash-release timestamps.",
              "venues": list(venue_archive.VENUES), "date_window": {"from": START, "through": END},
              "metrics": metrics(rows), "cases": selection[:20], "limits": [
                  "The earliest raw CLI version here is the earliest one retained locally. Earlier reports may exist.",
                  "Historical backfill receipt clocks appear much later than venue events. Paper collateral uses first local verified payout receipt.",
                  "A complete clearing file after the climate report caps publication time and does not measure initial exchange settlement availability.",
                  "Kalshi station and contract date provide an NWS comparison clock when the listing has no explicit climate-window field. Native close clocks stay separate.",
                  "Kalshi can select a different report hierarchy. An earlier native timestamp alone does not identify what source was used.",
              ], "provenance": {"engine": VERSION, "local_cli_roots": list(RAW_ROOTS), "gap_counts": gaps,
                                 "archive_file": "venues.sqlite3", "implementation_sha256": sha256(Path(__file__).read_bytes()).hexdigest()}}
    return research_model.write_record(data, record, {"rows": rows, "gaps": gaps})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=venue_archive.DEFAULT_DATA)
    args = parser.parse_args()
    path = report(args.data)
    output = json.loads(path.read_bytes())
    print(json.dumps({"path": str(path), "metrics": output["metrics"], "cases": output["cases"]}, indent=2))


if __name__ == "__main__":
    main()
