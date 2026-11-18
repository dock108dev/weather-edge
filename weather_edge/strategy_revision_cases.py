"""Reconstruct actual weather and payout disagreements from complete raw climate products."""

import argparse
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

from . import archive, history, strategy_twc, venue_archive
from .market import iso, parse_time
from .nbm import climate_window
from .strategy_runs import DEFAULT_DATA, digest, json_bytes, write_new


ENGINE = "climate-revision-cases-v2"
EXAMPLES = (
    ("KLAX", "2026-10-22", "high_f", -8, "202610230848-KLOX-CDUS46-CLILAX", "KXHIGHLAX-26OCT22-B70.5",
     "tc-temp-laxhigh-2026-10-22-gte70lt71f"),
    ("KSAN", "2026-10-10", "low_f", -8, "202610111000-KSGX-CDUS46-CLISAN", "KXLOWTSAN-26OCT10-B67.5", None),
    ("KMIA", "2026-08-29", "high_f", -5, "202608300910-KMFL-CDUS42-CLIMIA", "KXHIGHMIA-26AUG29-B90.5",
     "tc-temp-miahigh-2026-08-29-gte90lt91f"),
)


def case(data, station, day, variable, offset, product, instrument, related_instrument):
    twc, = strategy_twc.reports(data, station=station, day=day)
    actual = venue_archive.detail(data, venue_archive.KALSHI, instrument)
    related = venue_archive.detail(data, venue_archive.POLY, related_instrument) if related_instrument else None
    cache = data / "research" / "diagnostics" / "cli-mismatches" / station
    summary_url = history.IEM + "/json/cli.py?" + urlencode({"station": station, "year": day[:4]})
    yearly = archive._fetch_json(cache / "annual.json", summary_url)
    summary = next(row for row in yearly["results"] if row["station"] == station and row["valid"] == day)
    weather_day = date.fromisoformat(day)
    _, ends_at = climate_window(weather_day, offset)
    editions, discovery = publications(data, station, weather_day, ends_at, product)
    bulletin = next((record for record in editions if record["product"] == product), None)
    if not bulletin or actual is None or actual["yes_payout"] is None:
        raise ValueError("Missing exact climate product or exchange payout")
    public_value = twc["temperature_high_f"] if variable == "high_f" else twc["temperature_low_f"]
    modeled = bulletin[variable]
    annual = summary["high" if variable == "high_f" else "low"]
    sequence = [edition for edition in editions if edition[variable] is not None and
                parse_time(edition["available_at"]) <= parse_time(actual["settlement_at"])]
    publication_times = [(proof["available_at"], proof[variable]) for proof in sequence]
    text = f"NWS text {modeled}°F, annual summary {annual}°F, The Weather Company display {public_value:.0f}°F."
    if len({value for _, value in publication_times}) > 1:
        first, last = publication_times[0], publication_times[-1]
        minutes = (parse_time(last[0]) - parse_time(first[0])).total_seconds() / 60
        text = (f"Original NWS bulletins reported {first[1]}°F at {first[0][11:16]} UTC and {last[1]}°F at {last[0][11:16]} UTC, "
                f"{minutes:g} minutes apart. The Weather Company display reports {public_value:.0f}°F.")
    return {
        "venue": venue_archive.KALSHI, "station": station, "date": day, "instrument_id": instrument,
        "at": actual["settlement_at"], "title": f"{station} {day} · {variable[:-2]} temperature report conflict",
        "detail": f"{text} Kalshi {actual['label']} YES paid {float(actual['yes_payout']):.2f}." +
                  (f" Polymarket US {related['label']} YES paid {float(related['yes_payout']):.2f}."
                   if related and related["yes_payout"] is not None else ""),
        "raw_nws_publication": bulletin,
        "raw_nws_editions": editions,
        "raw_nws_editions_before_exchange_settlement": sequence,
        "raw_nws_edition_discovery": discovery,
        "date_matched_summary": {"source": summary_url, "row": summary},
        "current_twc_receipt": twc,
        "venue_evidence": {key: actual[key] for key in
                           ("rules_primary", "rules_secondary", "settlement_at", "settlement_received_at",
                            "yes_payout", "settlement_source", "trade_closed_at") if key in actual},
        "paired_polymarket_evidence": {key: related[key] for key in
                                       ("instrument_id", "rules_primary", "yes_payout", "settlement_source", "settlement_received_at")}
        if related else None,
    }


def publications(data, station, weather_day, end, chosen):
    cache = data / "research" / "diagnostics" / "cli-mismatches" / station
    _, office, _, pil, *_ = chosen.split("-")
    known = {chosen}
    if station == "KMIA":
        known.add("202608300824-KMFL-CDUS42-CLIMIA")
    discovery, gaps = [], []
    for i in range(3):
        issue_day = end.date() + timedelta(days=i)
        query = urlencode({"cccc": office, "pil": pil, "date": issue_day})
        url = f"{history.IEM}/api/1/nws/afos/list.json?{query}"
        path = cache / f"afos-{issue_day}.json"
        try:
            body = archive._fetch_json(path, url)
        except (OSError, ValueError) as error:
            gaps.append({"source": url, "reason": str(error)})
            continue
        known.update(item["product_id"] for item in body["data"] if item["cccc"] == office and item["pil"] == pil)
        discovery.append({"source": url, "path": str(path.relative_to(data)), "sha256": sha256(path.read_bytes()).hexdigest()})
    editions = []
    for product in sorted(known):
        source = history.IEM + "/api/1/nwstext/" + product
        paths = [data / root / station / (product + ".txt") for root in
                 ("expansion/cli", "archive-raw/cli", "research/evidence/high-physical/raw-cli")]
        path = next((item for item in paths if item.is_file()), cache / (product + ".txt"))
        try:
            body = history._fetch(path, source)
        except (OSError, ValueError) as error:
            gaps.append({"source": source, "reason": str(error)})
            continue
        bulletin = archive._parse_cli(body.decode(), product, SimpleNamespace(day=weather_day), end)
        if bulletin:
            editions.append({**bulletin, "sha256": sha256(body).hexdigest(), "path": str(path.relative_to(data))})
    return editions, {"listing_receipts": discovery, "gaps": gaps}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", type=Path, default=DEFAULT_DATA)
    args = ap.parse_args()
    data = args.data
    cases = [case(data, *example) for example in EXAMPLES]
    ident = "climate-revisions-" + digest({"method": ENGINE, "cases": cases})[:20]
    result = {
        "id": ident, "title": "The same station-day can have conflicting climate values",
        "hypothesis": "Raw NWS bulletins and current provider snapshots can disagree with derived annual summaries and actual payouts.",
        "target": "Daily highs and lows",
        "venues": list(venue_archive.VENUES), "created_at": iso(datetime.now(timezone.utc)),
        "date_window": {"from": min(c["date"] for c in cases), "through": max(c["date"] for c in cases)},
        "metrics": [{"measure": "Independently checked real station-days", "count": len(cases)}],
        "cases": cases,
        "limits": [
            "The currently served Weather Company page does not provide original issue times for these dated records.",
            "The cause and report that Kalshi selected at settlement are unknown. The actual exchange payouts are verified.",
            "Visible date, station and threshold agreement is insufficient to guarantee equal payouts.",
        ],
        "provenance": {"engine": ENGINE, "climate_sources": [strategy_twc.SITE, history.IEM]},
    }
    path = data / "research" / "models" / f"{ident}.json"
    write_new(path, json_bytes(result) + b"\n")
    print(json.dumps({"path": str(path), "cases": [c["detail"] for c in cases]}, indent=2))


if __name__ == "__main__":
    main()
