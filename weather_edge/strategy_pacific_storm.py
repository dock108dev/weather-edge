"""Replay Eastern Pacific storm-count thresholds against basin-local NHC tracks."""

import argparse
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
from statistics import mean
from urllib.error import HTTPError
from zoneinfo import ZoneInfo

from . import research_model, venue_archive, venue_storm
from .market import iso, parse_time
from .venue_sources import PublicGET, parse_kalshi_book


VERSION = "east-pacific-storm-count-v1"
HURDAT = "https://www.nhc.noaa.gov/data/hurdat/hurdat2-nepac-1949-2025-092926.txt"
HURDAT_FORMAT = "https://www.nhc.noaa.gov/data/hurdat/hurdat2-format-nencpac-1949-2021.pdf"
REPORT = "https://www.nhc.noaa.gov/data/tcr/?basin=epac&season=2026"
ANNUAL_REPORT = "https://www.nhc.noaa.gov/data/tcr/?basin=epac&season={}"
OUTLOOK = "https://www.nhc.noaa.gov/gtwo.php?basin=epac"
CONTRACT = "https://assets.kalshi.com/contract_terms/HURRICANE.pdf"
FIT = range(1991, 2008)
HELD = range(2008, 2026)
DECISIONS = ((8, 15), (9, 15), (10, 15), (11, 1), (11, 15))
KINDS = {"named storms": 34, "hurricanes": 64, "major hurricanes": 96}
EVENTS = {
    "KXNAMEDSTORM-26DEC01EPACTOT": "named storms",
    "KXHURRICANE-26DEC01EPACTOT": "hurricanes",
    "KXHURRICANE-26DEC01EPACMAJ": "major hurricanes",
}
STUDY_THRESHOLDS = {"named storms": (12, 16, 20, 24), "hurricanes": (6, 8, 10, 12), "major hurricanes": (2, 4, 6, 8)}
SUMMARY = re.compile(r"20\d{2} East Pacific Summary as of (\d{2})(\d{2})? UTC (\d{1,2} [A-Za-z]+ 20\d{2})")
# The best-track file duplicates the Atlantic track for these through-crossers.
BASIN_ENTRY = {
    "EP222016": ("2016-11-25T03:30:00Z", "https://www.nhc.noaa.gov/data/tcr/AL162016_Otto.pdf"),
    "EP042022": ("2022-07-02T12:00:00Z", "https://www.nhc.noaa.gov/data/tcr/AL022022_EP042022_Bonnie.pdf"),
    "EP182022": ("2022-10-10T00:00:00Z", "https://www.nhc.noaa.gov/data/tcr/AL132022_EP182022_Julia.pdf"),
}


def stamp(year, month, day, hour=0):
    return datetime(year, month, day, hour, tzinfo=timezone.utc)


def track_evidence(raw, years):
    """Pick each system's first basin-local attainment of each qualifying wind."""
    by_year = {year: [] for year in years}
    current = None
    for line in raw.decode("ascii").splitlines():
        row = [part.strip() for part in line.split(",")]
        if re.fullmatch(r"(?:EP|CP)\d{6}", row[0]):
            year = int(row[0][-4:])
            current = None
            if year in by_year:
                current = {"id": row[0], "name": row[1], "first_by_kind": {}, "peak_inside_kt": 0,
                           "peak_outside_kt": 0}
                by_year[year].append(current)
            continue
        if current is None or len(row) < 7 or row[3] not in {"TS", "HU", "SS"}:
            continue
        at = datetime.strptime("".join(row[:2]), "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
        if not stamp(year, 5, 15) <= at < stamp(year, 12, 2):
            continue
        longitude, latitude = row[5], row[4]
        wind = int(row[6])
        crossing = BASIN_ENTRY.get(current["id"])
        inside = (longitude.endswith("W") and float(longitude[:-1]) < 140 and latitude.endswith("N")
                  and (crossing is None or at >= parse_time(crossing[0])))
        peak = "peak_inside_kt" if inside else "peak_outside_kt"
        current[peak] = max(current[peak], wind)
        if not inside:
            continue
        for kind, threshold in KINDS.items():
            if wind >= threshold and (kind == "named storms" or row[3] == "HU"):
                current["first_by_kind"].setdefault(kind, {
                    "at": iso(at), "wind_kt": wind, "latitude": latitude, "longitude": longitude,
                })
    return by_year


def _before_or_at(storms, kind, through):
    cutoff = iso(through)
    return sum(kind in row["first_by_kind"] and row["first_by_kind"][kind]["at"] <= cutoff for row in storms)


def snapshots(tracks):
    return {year: {f"{month:02d}-{day:02d}": {
        kind: {"as_of": _before_or_at(rows, kind, stamp(year, month, day, 15)),
               "final": _before_or_at(rows, kind, stamp(year, 12, 2))} for kind in KINDS
    } for month, day in DECISIONS} for year, rows in tracks.items()}


def _binary_probability(hits, total):
    return (hits + 0.5) / (total + 1)


def _remaining(prior, required):
    hits = sum(row["final"] - row["as_of"] >= required for row in prior)
    if required <= 0:
        return 1.0, hits
    return _binary_probability(hits, len(prior)), hits


def backtest(counts):
    decisions = []
    for year in HELD:
        for clock, groups in counts[year].items():
            for kind, row in groups.items():
                prior = [counts[earlier][clock][kind] for earlier in FIT]
                for threshold in STUDY_THRESHOLDS[kind]:
                    hits = sum(earlier["final"] > threshold for earlier in prior)
                    probability, _ = _remaining(prior, threshold + 1 - row["as_of"])
                    decisions.append({"season": year, "clock": clock, "kind": kind, "threshold": threshold,
                                      **row, "outcome_yes": row["final"] > threshold,
                                      "seasonal_climatology": _binary_probability(hits, len(prior)),
                                      "count_plus_late_tail": probability})
    metrics = []
    for clock in (*counts[FIT.start], "all"):
        chosen = [row for row in decisions if clock == "all" or row["clock"] == clock]
        for model in ("seasonal_climatology", "count_plus_late_tail"):
            metrics.append({"window": clock, "model": model, "holdout_seasons": len(HELD), "cases": len(chosen),
                            "brier": mean((row[model] - row["outcome_yes"]) ** 2 for row in chosen)})
    return metrics, decisions


def parse_summary(raw, year=2026):
    rows = venue_storm.Page(raw).rows
    for index, row in enumerate(rows):
        if len(row) != 1 or not row[0].startswith(f"{year} East Pacific Summary"):
            continue
        headings, values = rows[index + 1:index + 3]
        if [name.split("(vs", 1)[0].strip().lower() for name in headings[:3]] != list(KINDS):
            raise ValueError("Unexpected NHC basin summary headings")
        at = None
        if clock := SUMMARY.fullmatch(row[0]):
            time = datetime.strptime(f"{clock[1]}{clock[2] or '00'} {clock[3]}", "%H%M %d %B %Y")
            at = iso(time.replace(tzinfo=timezone.utc))
        return {"source_at": at,
                "counts": dict(zip(KINDS, (int(raw.split()[0]) for raw in values[:3]))),
                "cross_basin_rows": [r[:5] for r in rows[:index] if len(r) >= 5 and r[2].endswith("^")]}
    raise ValueError("NHC East Pacific summary with an issue date unavailable")


def parse_outlook(raw):
    block = re.search(r"MIATWOEP ALL(?P<body>.*?)NNNN", raw.decode("utf-8"), re.S)
    if block is None:
        raise ValueError("NHC English East Pacific outlook unavailable")
    paragraph = " ".join(re.sub(r"<[^>]*>", " ", block["body"]).split())
    clock = re.search(r"\b(\d{3,4} [AP]M) (PST|PDT) [A-Z][a-z]{2} ([A-Z][a-z]{2} \d{1,2} \d{4})\b", paragraph)
    if clock is None:
        raise ValueError("NHC East Pacific outlook issue clock unavailable")
    time = datetime.strptime(f"{clock[1]} {clock[3]}", "%I%M %p %b %d %Y")
    return {"source_at": iso(time.replace(tzinfo=ZoneInfo("America/Los_Angeles"))), "bulletin": paragraph,
            "no_formation_expected_next_seven_days": (
                "Tropical cyclone formation is not expected during the next 7 days." in paragraph)}


def market_catalog(data):
    with venue_archive.connect(data) as db:
        keys = db.execute(
            "SELECT instrument_id FROM markets WHERE venue=? AND event_id IN (?,?,?) ORDER BY instrument_id",
            (venue_archive.KALSHI, *EVENTS),
        ).fetchall()
    fields = ("instrument_id", "event_id", "lower", "status", "title", "rules_primary", "rules_url", "opened_at",
              "closed_at", "first_received_at", "yes_bid", "yes_ask", "last_price", "yes_payout", "no_payout",
              "settlement_at", "settlement_received_at", "receipt", "settlement_receipt")
    return [{key: row.get(key) for key in fields} for (instrument,) in keys
            if (row := venue_archive.detail(data, venue_archive.KALSHI, instrument))]


def read_books(data, catalog):
    """Request a single bounded batch and store native receipts, even for an incomplete reply."""
    venue = venue_archive.KALSHI
    tickers = [row["instrument_id"] for row in catalog if row["status"] == "open"]
    if not tickers:
        return []
    reply = PublicGET(data, timeout=8)(venue, "/markets/orderbooks", {"tickers": tickers})
    books = []
    with venue_archive.connect(data) as db, db:
        receipt_id = venue_archive.save_receipt(db, venue, "book", reply)
        if reply.status == 200:
            for raw in reply.json().get("orderbooks", []):
                try:
                    book = parse_kalshi_book(raw, reply)
                except (KeyError, TypeError, ValueError):
                    continue
                if book["instrument_id"] in tickers:
                    venue_archive.save_book(db, venue, book["instrument_id"], receipt_id, book)
                    books.append({**book, "venue_receipt_id": receipt_id})
    return books


def annual_check(data, counts):
    audited, gaps = [], []
    for year, snapshots in counts.items():
        source = ANNUAL_REPORT.format(year)
        try:
            page, receipt = venue_storm._get(data, f"pacific/annual-{year}.html", source)
            official = parse_summary(page, year)["counts"]
        except (HTTPError, ValueError) as failure:
            gaps.append({"season": year, "source": source, "reason": str(failure)})
            continue
        computed = {kind: row["final"] for kind, row in snapshots["11-15"].items()}
        audited.append({"season": year, "annual_report": official, "calculated_contract_dates": computed,
                        "agree": official == computed, "receipt": receipt})
    return audited, gaps


def study(data, *, request_books=False, check_annual=False):
    run_at = datetime.now(timezone.utc)
    marker = run_at.strftime("%Y%m%dT%H%M%SZ")
    historical, historical_receipt = venue_storm._get(data, "pacific/hurdat2-1949-2025.txt", HURDAT)
    report_raw, report_receipt = venue_storm._get(data, f"pacific/summary-{marker}.html", REPORT)
    outlook_raw, outlook_receipt = venue_storm._get(data, f"pacific/outlook-{marker}.html", OUTLOOK)
    report, outlook = parse_summary(report_raw), parse_outlook(outlook_raw)
    for product, receipt in ((report, report_receipt), (outlook, outlook_receipt)):
        if product["source_at"] is None or parse_time(product["source_at"]) > parse_time(receipt["received_at"]):
            raise ValueError("NOAA's issued-at label is missing or ahead of its local receipt")
    tracks = track_evidence(historical, range(FIT.start, HELD.stop))
    counts = snapshots(tracks)
    metrics, historical_cases = backtest(counts)
    rows = market_catalog(data)
    books = read_books(data, rows) if request_books else []
    annual, annual_gaps = annual_check(data, counts) if check_annual else ([], [])
    live, cases = [], []
    matched_settled = 0
    for row in rows:
        kind = EVENTS[row["event_id"]]
        threshold = int(row["lower"])
        current = report["counts"][kind]
        remaining = threshold + 1 - current
        prior = [counts[year]["11-15"][kind] for year in sorted(counts)]
        proxy, hits = _remaining(prior, remaining)
        matched_settled += bool(row["yes_payout"] == "1" and current > threshold and row["settlement_receipt"])
        book = next((book for book in books if book["instrument_id"] == row["instrument_id"]), None)
        dry_first_week = [prior[n] for n, year in enumerate(sorted(counts))
                          if _before_or_at(tracks[year], kind, stamp(year, 11, 15, 15) + timedelta(days=7))
                          == prior[n]["as_of"]]
        dry_proxy, dry_hits = _remaining(dry_first_week, remaining)
        live.append({"instrument_id": row["instrument_id"], "kind": kind, "threshold": threshold,
                     "confirmed_count": current, "additional_required": max(0, remaining), "analog_seasons": len(prior),
                     "analog_hits": hits, "historical_tail_proxy": proxy, "current_yes_payout": row["yes_payout"],
                     "no_first_week_qualification_seasons": len(dry_first_week),
                     "no_first_week_qualification_hits": dry_hits, "no_first_week_qualification_proxy": dry_proxy,
                     "native_yes_display_bid": row["yes_bid"], "native_yes_display_ask": row["yes_ask"],
                     "book_source_at": book.get("source_at") if book else None})
        if row["status"] == "open" and remaining == 1:
            cases.append({"venue": "kalshi", "instrument_id": row["instrument_id"], "at": report["source_at"],
                          "title": f"One more qualifying storm needed for more than {threshold} {kind}",
                          "detail": f"NHC confirmed {current}. {hits} of {len(prior)} earlier seasons added one from the same calendar cut. Native display bid/ask {row['yes_bid']}/{row['yes_ask']}."})
    for kind in KINDS:
        tail = [storm for year in tracks for storm in tracks[year] if (first := storm["first_by_kind"].get(kind))
                and parse_time(first["at"]) > stamp(year, 11, 15, 15)]
        if tail:
            last = tail[-1]
            first = last["first_by_kind"][kind]
            cases.append({"venue": "kalshi", "at": first["at"], "title": f"Historical late-season witness for {kind}",
                          "detail": f"{last['name']} ({last['id']}) first reached {first['wind_kt']} kt inside the basin at {first['latitude']}, {first['longitude']}."})
    for row in report["cross_basin_rows"]:
        cases.append({"at": report["source_at"], "title": f"NHC flags {row[1]} as a cross-basin peak",
                      "detail": f"The storm row displays {row[2]} and {row[4]} kt. The assessment uses the separately issued East Pacific total."})
    for instrument, (at, source) in BASIN_ENTRY.items():
        storm = next(row for row in tracks[int(instrument[-4:])] if row["id"] == instrument)
        cases.append({"at": at, "title": f"Published basin entry of {storm['name'].title()}",
                      "detail": f"Track peaks {storm['peak_outside_kt']} kt outside and {storm['peak_inside_kt']} kt inside.",
                      "source": source})
    metrics.append({"window": "Native 2026 market and NOAA comparison", "native_instruments": len(rows),
                    "open_markets": sum(row["status"] == "open" for row in rows), "matched_yes_payouts": matched_settled,
                    "captured_native_ladders": len(books), "ladders_with_source_clock": sum(bool(row["source_at"]) for row in books),
                    "paper_fills": 0, **report["counts"]})
    if check_annual:
        metrics.append({"window": "Published annual NHC cross-check", "audited_seasons": len(annual),
                        "exact_all_category_seasons": sum(row["agree"] for row in annual),
                        "unavailable_seasons": len(annual_gaps)})
    receipts = [historical_receipt, report_receipt, outlook_receipt]
    details = {"tracks": tracks, "snapshots": counts, "historical_decisions": historical_cases, "report": report,
               "outlook": outlook, "native_catalog": rows, "books": books, "receipts": receipts, "current": live,
               "annual_audit": annual, "annual_audit_gaps": annual_gaps, "basin_entries": BASIN_ENTRY}
    record = {"version": VERSION, "title": "Eastern Pacific late-season storm thresholds",
              "hypothesis": "Official basin-specific counts plus a historical residual distribution improve on static climatology as the season advances.",
              "target": "Kalshi May 15 through December 1 named storms, all hurricanes, and major hurricanes in the Eastern Pacific.",
              "venues": ["kalshi"], "date_window": {"from": "1991-05-15", "through": report["source_at"],
              "fit_seasons": [FIT.start, FIT.stop - 1], "test_seasons": [HELD.start, HELD.stop - 1]},
              "metrics": metrics, "cases": cases, "series": live,
              "limits": ["HURDAT2 retrospectively revises best tracks. Earlier-year decision snapshots have no historical receipt claims.",
                         "Local estimates use a fixed 15 UTC cut and whole UTC endpoint dates. The contract has venue-specific timing, revision rules and eligible source hierarchy.",
                         "The late-season distribution is a historical prior. Its no first-week qualification sensitivity conditions on past outcomes and does not quantify the forecast's current chance of no formation.",
                         "Venue catalog bid/ask is display evidence. Public Kalshi REST ladders lack exchange snapshot times. No live or paper fills are inferred.",
                         "The current NOAA storm summary is provisional. Open listings require an actual venue payout receipt."],
              "provenance": {"engine": VERSION, "storm_summary": report, "outlook": outlook, "sources": receipts,
                             "format_reference": HURDAT_FORMAT, "native_terms": CONTRACT,
                             "implementation_sha256": sha256(Path(__file__).read_bytes()).hexdigest()}}
    return research_model.write_record(data, record, details)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=venue_archive.DEFAULT_DATA)
    parser.add_argument("--books", action="store_true", help="capture a single bounded public order-book batch")
    parser.add_argument("--check-annual", action="store_true", help="archive and compare NHC annual reports")
    args = parser.parse_args()
    path = study(args.data, request_books=args.books, check_annual=args.check_annual)
    result = json.loads(path.read_bytes())
    print(json.dumps({"path": str(path), "metrics": result["metrics"], "cases": result["cases"]}, indent=2))


if __name__ == "__main__":
    main()
