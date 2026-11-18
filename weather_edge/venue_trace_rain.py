"""Audit the observed change from trace-inclusive to measurable-only Central Park rain."""

import argparse
from collections import Counter, defaultdict
from datetime import date
import gzip
from hashlib import sha256
import json
from pathlib import Path
import sys

from . import expansion_weather, research_model, strategy_twc, venue_archive, venue_snow_data
from .collector import paused
from .market import decimal
from .venue_sources import CollectionPaused

VERSION = "kalshi-central-park-trace-rain-v1"
OLD_TERMS = "https://assets.kalshi.com/regulatory/product-certifications/RAINNYC.pdf"
CURRENT_TERMS = "https://assets.kalshi.com/contract_terms/RAINGLOBALPERIOD.pdf"
TWC = "https://weather.com/kalshi"
POINT_DAYS = ("2026-06-18", "2026-07-15", "2026-11-07")


def _kind(raw):
    if raw == "T":
        return "trace"
    try:
        number = decimal(raw)
    except ValueError:
        return "unknown"
    if number == 0:
        return "dry"
    return "measurable" if number > 0 else "unknown"


def _nws_annual(data, *, collect):
    root = data / "venues" / "snow-climate" / "KNYC"
    reports, provenance = {}, []
    for year in range(2021, 2027):
        path = root / f"{year}.json"
        sidecar = path.with_suffix(".json.receipt.json")
        if path.exists() and sidecar.exists():
            raw, receipt = path.read_bytes(), json.loads(sidecar.read_bytes())
            if sha256(raw).hexdigest() != receipt["sha256"]:
                raise ValueError(f"Annual climate source changed in {path}")
        elif collect:
            if paused(data):
                raise CollectionPaused
            raw, receipt = venue_snow_data.obtain_year(data, "KNYC", year)
        else:
            continue
        for row in json.loads(raw)["results"]:
            exact = venue_snow_data._day_row("KNYC", row, offset=-5)
            if exact is None:
                continue
            day = exact["date"]
            if day in reports:
                raise ValueError(f"Duplicate annual Central Park climate date {day}")
            reports[day] = {key: exact[key] for key in ("date", "product", "published_at", "source")}
            reports[day]["precip"] = row.get("precip")
        provenance.append(receipt)
    return reports, provenance


def gather(data, *, collect=False):
    with venue_archive.connect(data) as db:
        sql = f"""SELECT {venue_archive.PUBLIC_SELECT} FROM markets WHERE venue=? AND family='daily-rain'
         AND (series_id='KXRAINNYC' AND lower='0' AND operator='above-or-trace'
           OR series_id='KXRAINSEA' AND lower='0' AND operator='above-or-trace'
           OR series_id='KXRAIN' AND station='KNYC' AND lower='0' AND operator='above')
         ORDER BY day,instrument_id"""
        markets = [dict(row) for row in db.execute(sql, (venue_archive.KALSHI,))]
    in_nyc = [row for row in markets if row["station"] == "KNYC"]
    annual, sources = _nws_annual(data, collect=collect)
    modern = {row["label_date"]: row for row in strategy_twc.reports(data, station="KNYC", start="2026-07-15")}
    spot = {}
    for day in POINT_DAYS:
        if (label := annual.get(day)) is None:
            continue
        if collect and paused(data):
            raise CollectionPaused
        try:
            if collect:
                reports = expansion_weather.reports_for_day(data, "KNYC", date.fromisoformat(day), label["product"])
            elif (data / "expansion" / "cli" / "KNYC" / (label["product"] + ".txt")).exists():
                item = expansion_weather.raw_report(data, "KNYC", date.fromisoformat(day), label["product"])
                reports = [item] if item else []
            else:
                reports = []
        except (OSError, ValueError):
            reports = []
        spot[day] = reports
    histories = {row["day"]: row["instrument_id"] for row in in_nyc if row["series_id"] == "KXRAINNYC"}
    rows = []
    for market in markets:
        day, series = market["day"], market["series_id"]
        nws = annual.get(day) if market["station"] == "KNYC" else None
        twc = modern.get(day) if series == "KXRAIN" else None
        reported = (twc or {}).get("precipitation_in") if series == "KXRAIN" else (nws or {}).get("precip")
        authority = _kind(reported) if twc is None or twc["provider_status"] in ("official", "revised") else "unknown"
        raw = _kind(market["expiration_value"])
        payout = market["yes_payout"]
        rows.append({"market": market, "native_recorded": raw, "weather_reported": authority, "nws": nws, "twc": twc,
                     "payout": int(payout) if payout in ("1", "0") else None, "spot_products": spot.get(day),
                     "counterpart_same_day": histories.get(day) if series == "KXRAIN" else None})
    return {"version": VERSION, "rows": rows, "annual_source_receipts": sources}


def _case(row, detail, *, related=None):
    native = row["market"]
    return {"venue": native["venue"], "instrument_id": native["instrument_id"], "station": native["station"],
            "date": native["day"], "at": native["settlement_at"], "title": native["title"], "detail": detail,
            "data": row, **({"related_venue": native["venue"], "related_instrument_id": related} if related else {})}


def evaluate(data, corpus):
    frozen, digest = research_model.freeze_inputs(data, corpus)
    groups = defaultdict(list)
    for row in corpus["rows"]:
        groups[row["market"]["series_id"]].append(row)
    audits, products = {}, {}
    for series, rows in sorted(groups.items()):
        payouts = [row for row in rows if row["payout"] is not None]
        native_traces = [row for row in payouts if row["native_recorded"] == "trace"]
        reports = [row for row in payouts if row["weather_reported"] != "unknown"]
        source_traces = [row for row in reports if row["weather_reported"] == "trace"]
        counts = Counter(row["native_recorded"] for row in payouts)
        forecasts = {policy: sum((row["weather_reported"] in eligible) != row["payout"] for row in reports)
                     for policy, eligible in {"trace_inclusive": ("trace", "measurable"), "measurable": ("measurable",)}.items()}
        audits[series] = {"cataloged": len(rows), "native_binary_payouts": len(payouts), "native_expiration_kind": dict(counts),
                          "native_trace_payouts": dict(Counter(f"${row['payout']}" for row in native_traces)),
                          "source_report_weather": len(reports), "source_trace_days": len(source_traces),
                          "source_trace_payouts": dict(Counter(f"${row['payout']}" for row in source_traces)),
                          "source_to_cash_disagreements_by_trace_definition": forecasts}
        products[series] = [row for row in reports if row["weather_reported"] == "trace"]
    by_instrument = {row["market"]["instrument_id"]: row for row in corpus["rows"]}
    cases = []
    if (row := by_instrument.get("KXRAINNYC-26JUN18-T0")) and row["payout"] == 1:
        cases.append(_case(row, "The completed NWS climate report and native expiration field read T. The original explicit trace clause paid YES $1."))
    if (row := by_instrument.get("KXRAIN-26NOV07-NYC")) and row["payout"] == 0:
        cases.append(_case(row, "The retained official TWC Central Park record reads T. The later rule values trace at zero. The native expiration shows 0.00 inch and YES paid $0."))
    if (row := by_instrument.get("KXRAIN-26JUL15-NYC")) and (leg := by_instrument.get(row["counterpart_same_day"])):
        value, older = row["market"]["expiration_value"], leg["market"]["expiration_value"]
        cases.append(_case(row, f"Both versions were listed for the same Central Park reporting day. The measurable amount cleared both rules. "
                                f"The native expiration values were {value} and {older} inches and both YES contracts paid ${row['payout']}.",
                           related=leg["market"]["instrument_id"]))
    metrics = [{"group": series, "model": definition, "published_weather_compared": value["source_report_weather"],
                "disagreement_count": count}
               for series, value in audits.items() if value["source_report_weather"]
               for definition, count in value["source_to_cash_disagreements_by_trace_definition"].items()]
    result = {"version": VERSION, "title": "Central Park trace precipitation changed its native payout",
              "hypothesis": "One rain target cannot be used across the Central Park series change without changing observed payouts.",
              "target": "Actual binary payouts and documented dry, trace and measurable precipitation categories",
              "date_window": {"archive": ["2021-08-20", "2026-11-15"]}, "venues": ["Kalshi"],
              "coverage": audits, "metrics": metrics, "cases": cases,
              "limits": ["Annual NWS summary data can include downstream revisions. The linked dates carry complete original NWS reports when archived.",
                         "The modern provider's actual local receipt is preserved. Its original issue clock is often absent.",
                         "The old WFO Seattle listing names its forecast office. No airport station was inferred for its trace examples.",
                         "These are settlement-definition comparisons. They do not measure forecast quality or executable trading."],
              "provenance": {"legacy terms": OLD_TERMS, "current terms": CURRENT_TERMS, "current source": TWC,
                             "frozen_input": str(frozen.relative_to(data)), "frozen_input_sha256": digest,
                             "source_sha256": research_model.run_sources(sys.modules[__name__], expansion_weather, venue_snow_data)}}
    return research_model.write_record(data, result, {"audit_rows": corpus["rows"], "weather_trace_rows": products})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--collect", action="store_true")
    parser.add_argument("--input", type=Path)
    args = parser.parse_args(argv)
    try:
        corpus = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else gather(args.data, collect=args.collect)
        print(evaluate(args.data, corpus))
    except CollectionPaused:
        print("Collection is stopped. Retained receipts are available.")
        return 2


if __name__ == "__main__":
    sys.exit(main())
