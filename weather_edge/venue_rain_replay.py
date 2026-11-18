"""Audit METAR rain occurrence and measurable rainfall payouts on the same airport day."""

import argparse
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
import gzip
from hashlib import sha256
import json
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlencode

from . import research_model, strategy_twc, venue_archive
from .capture import AWC, ReadOnlyHTTP
from .collector import paused
from .history import _write
from .market import decimal, iso, parse_time
from .venue_contracts import STATION_OFFSETS
from .venue_sources import CollectionPaused

VERSION = "cross-venue-rain-rules-v1"
WEATHER = re.compile(r"^[+-]?(?:SH|FZ|TS)?(?P<condition>(?:DZ|RA|SN|SG|IC|PL|GR|GS|UP)+)$")
CLOUD = re.compile(r"^(?:FEW|SCT|BKN|OVC|VV)(?:\d{3}|///)")
AWC_DOC = "https://aviationweather.gov/data/api/"
RAINGLOBALPERIOD = "https://assets.kalshi.com/contract_terms/RAINGLOBALPERIOD.pdf"


def _primary_weather(raw):
    tokens = []
    for part in raw.split():
        if part in {"RMK", "TEMPO", "BECMG", "NOSIG", "CAVOK"} or part.startswith("FM") or CLOUD.match(part):
            break
        match = WEATHER.fullmatch(part)
        if match:
            categories = [match["condition"][i:i + 2] for i in range(0, len(match["condition"]), 2)]
            if "RA" in categories:
                tokens.append(part)
    return tokens


def _pair_rows(data):
    with venue_archive.connect(data) as db:
        rows = db.execute(
            """SELECT p.instrument_id, k.instrument_id FROM markets p JOIN markets k USING(station,day,family)
            WHERE p.venue=? AND k.venue=? AND p.family='daily-rain' AND k.series_id='KXRAIN'
              AND p.operator IN ('observed-rain-code','at-least') AND
              p.yes_payout IS NOT NULL AND k.yes_payout IS NOT NULL
            ORDER BY p.day,p.station""", (venue_archive.POLY, venue_archive.KALSHI),
        ).fetchall()
        columns = venue_archive.PUBLIC_SELECT
        pairs = []
        for left, right in rows:
            poly = db.execute(f"SELECT {columns} FROM markets WHERE venue=? AND instrument_id=?",
                              (venue_archive.POLY, left)).fetchone()
            kalshi = db.execute(f"SELECT {columns} FROM markets WHERE venue=? AND instrument_id=?",
                                (venue_archive.KALSHI, right)).fetchone()
            pairs.append((dict(poly), dict(kalshi)))
    return pairs


def _archive(data, day, stations):
    stations = tuple(sorted(stations))
    # One 30h UTC query covers the represented local standard days and
    # retains the first routine observation on the next local day.
    end = datetime.combine(date.fromisoformat(day) + timedelta(days=1), datetime.min.time(), timezone.utc)
    end += timedelta(hours=10)
    url = AWC + "?" + urlencode({"ids": ",".join(stations), "format": "json", "date": iso(end), "hours": 30})
    path = data / "venues" / "rain-metars" / f"{day}-{'-'.join(stations)}.json"
    provenance = path.with_suffix(".receipt.json")
    if path.is_file() and provenance.is_file():
        raw = path.read_bytes()
        receipt = json.loads(provenance.read_bytes())
        if sha256(raw).hexdigest() != receipt["sha256"]:
            raise ValueError("Archived AWC response checksum mismatch")
        return receipt, json.loads(raw) if raw else []
    if paused(data):
        raise CollectionPaused
    result = ReadOnlyHTTP(time.monotonic() + 15)(url)
    raw = result.body
    payload = json.loads(raw) if raw else []
    metadata = {
        "source": url, "requested_at": iso(result.requested_at), "received_at": iso(result.at),
        "path": str(path.relative_to(data)), "sha256": sha256(raw).hexdigest(),
        "rows": len(payload),
    }
    _write(path, raw)
    _write(provenance, json.dumps(metadata, separators=(",", ":")).encode())
    # Leave headroom under the AWC 100 requests/minute documentation.
    time.sleep(0.7)
    return metadata, payload


def _metar_resolution(raw, station, starts, ends):
    by_time = defaultdict(list)
    for row in raw:
        if row.get("icaoId") != station or (row.get("metarType") or "").upper() not in ("METAR", "SPECI"):
            continue
        try:
            observation = datetime.fromtimestamp(row["obsTime"], timezone.utc)
            seen = parse_time(row["receiptTime"])
        except (KeyError, ValueError, TypeError):
            continue
        if seen < observation - timedelta(minutes=1):
            continue
        by_time[observation].append((seen, row))
    next_release = min((seen for stamp, items in by_time.items() if stamp >= ends for seen, _ in items), default=None)
    supported = []
    eligible = 0
    late = 0
    by_observation = []
    for stamp, candidates in sorted(by_time.items()):
        if not starts <= stamp < ends:
            continue
        by_observation.append(stamp)
        choices = [(seen, row) for seen, row in candidates if next_release is None or seen <= next_release]
        if not choices:
            late += 1
            continue
        seen, row = max(choices, key=lambda item: item[0])
        eligible += 1
        codes = _primary_weather(row.get("rawOb") or "")
        if codes:
            supported.append({"at": iso(stamp), "source_received_at": iso(seen), "codes": codes,
                              "raw_observation": row["rawOb"], "report_type": row["metarType"]})
    hours = len({int((stamp - starts).total_seconds() / 3600) for stamp in by_observation})
    good = bool(next_release and hours == 24 and not late)
    return {"station": station, "climate_starts_at": iso(starts), "climate_ends_at": iso(ends),
            "next_observation_source_receipt": iso(next_release) if next_release else None,
            "eligible_reports": eligible, "covered_observation_hours": hours, "late_or_replaced": late,
            "rain_reports": supported, "expected_yes": True if supported and next_release else False if good else None,
            "quality": "observed qualifying rain code" if supported and next_release else "no qualifying report in complete routine windows" if good
            else "incomplete direct observation archive"}


def gather(data):
    data = Path(data)
    markets = _pair_rows(data)
    sets = defaultdict(set)
    for poly, _ in markets:
        if poly["operator"] == "observed-rain-code":
            sets[poly["day"]].add(poly["station"])
    archived = {}
    issues = {}
    for day, stations in sorted(sets.items()):
        try:
            archived[day] = _archive(data, day, stations)
        except (OSError, ValueError, TypeError) as error:
            issues[day] = str(error)
    twc = {(row["station"], row["label_date"]): row for row in strategy_twc.reports(data)}
    retained = []
    for poly, kalshi in markets:
        station, day = poly["station"], poly["day"]
        company = twc.get((station, day))
        validation = None
        if poly["operator"] == "observed-rain-code" and day in archived:
            source, raw = archived[day]
            if station in STATION_OFFSETS and poly["climate_starts_at"] and poly["climate_ends_at"]:
                starts, ends = (parse_time(poly[key]) for key in ("climate_starts_at", "climate_ends_at"))
                validation = {**_metar_resolution(raw, station, starts, ends), "source": source}
        retained.append({"station": station, "date": day, "polymarket": poly, "kalshi": kalshi,
                         "same_yes_payout": decimal(poly["yes_payout"]) == decimal(kalshi["yes_payout"]),
                         "twc": company, "aviation_weather": validation})
    return {"version": VERSION, "rows": retained, "issues": issues}


def _metric(rows):
    mismatches = [row for row in rows if not row["same_yes_payout"]]
    exact = [row for row in rows if row["aviation_weather"] and row["aviation_weather"]["expected_yes"] is not None]
    verified = [row for row in exact if str(int(row["aviation_weather"]["expected_yes"])) == row["polymarket"]["yes_payout"]]
    return {"cases": len(rows), "calendar_dates": len({row["date"] for row in rows}),
            "station_days": len({(row["station"], row["date"]) for row in rows}),
            "native_payout_differences": len(mismatches),
            "direct_observation_audits": len(exact), "payout_matches_direct_audit": len(verified),
            "TWC trace among payout differences": sum(bool(row["twc"] and row["twc"]["precipitation_in"] == "T") for row in mismatches)}


def evaluate(data, corpus):
    data = Path(data)
    frozen, digest = research_model.freeze_inputs(data, corpus)
    rows = corpus["rows"]
    cohort = {
        "All settled pairs": _metric(rows),
        "Legacy METAR rain-code condition": _metric([row for row in rows if row["polymarket"]["operator"] == "observed-rain-code"]),
        "Later CLI measurable condition": _metric([row for row in rows if row["polymarket"]["operator"] == "at-least"]),
    }
    highlight = {"rainc-rain-daily-2026-10-19-kmia", "rainc-rain-daily-2026-10-22-klax",
                 "rainc-rain-daily-2026-10-22-kmia"}
    cases = []
    for row in rows:
        left, right = row["polymarket"], row["kalshi"]
        if left["instrument_id"] not in highlight and (row["same_yes_payout"] or len(cases) >= 10):
            continue
        text = f"Polymarket US YES paid ${left['yes_payout']}. Kalshi YES paid ${right['yes_payout']}."
        if row["aviation_weather"] and row["aviation_weather"]["rain_reports"]:
            proof = row["aviation_weather"]["rain_reports"][0]
            text += " AWC recorded " + "/".join(proof["codes"]) + "."
        if row["twc"] and row["twc"]["precipitation_in"] == "T":
            text += " The Weather Company reported a trace."
        cases.append({"title": f"{row['station']} · {row['date']} · rain payout comparison",
                      "venue": venue_archive.POLY, "instrument_id": left["instrument_id"],
                      "related_instrument_id": right["instrument_id"], "station": row["station"], "date": row["date"],
                      "at": right["settlement_at"], "detail": text, "data": row})
    result = {
        "version": VERSION, "title": "Rain codes and measurable precipitation paid differently",
        "hypothesis": "Legacy observed-rain conditions can pay when no measurable precipitation was recorded. "
                      "Later Polymarket US thresholds can leave a different report-selection question.",
        "target": "Observed station-day rules and verified YES/NO payouts", "venues": ["Polymarket US", "Kalshi"],
        "date_window": {"observed settled pairs": [min((row["date"] for row in rows), default=None),
                                                  max((row["date"] for row in rows), default=None)]},
        "coverage": cohort, "cases": cases, "missing": corpus["issues"],
        "limits": [
            "The native payouts establish settlement. The AWC historical reconstruction can miss an overwritten report revision.",
            "Daily precipitation marks use original source precision. An observed present-weather RA code measures occurrence.",
            "The later legacy-to-CLI rule change and exchange-specific fallback terms determine comparability.",
        ],
        "provenance": {"frozen_input": str(frozen.relative_to(data)), "frozen_input_sha256": digest,
                       "awc_documentation": AWC_DOC, "kalshi_rules": RAINGLOBALPERIOD,
                       "source_sha256": research_model.run_sources(sys.modules[__name__], strategy_twc)},
    }
    return research_model.write_record(data, result, {"markets": rows})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--input", type=Path)
    args = parser.parse_args(argv)
    try:
        corpus = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else gather(args.data)
        print(evaluate(args.data, corpus))
    except CollectionPaused:
        print("Collection paused. Saved settlement and report evidence remains available.")


if __name__ == "__main__":
    main()
