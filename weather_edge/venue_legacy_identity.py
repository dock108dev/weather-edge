"""Verify restored native market aliases with original conditions and two early high-temperature examples."""

import argparse
from datetime import date, datetime, time, timedelta, timezone
import gzip
from hashlib import sha256
import json
from pathlib import Path
import sys

from . import expansion_replay, expansion_weather, research_model, venue_archive, venue_benchmark, venue_exports, venue_weekend_rain
from .market import iso, parse_time
from .model import _limits, _probability
from .nbm import climate_window
from .venue_contracts import STATION_OFFSETS, temperature_window

VERSION = "legacy-native-identities-weather-recovery-v1"
DAY = date(2023, 5, 11)
EVENTS = {"KAUS": "HIGHAUS-23MAY11", "KMIA": "HIGHMIA-23MAY11"}


def _receipt_payload(path):
    raw, receipt = path.read_bytes(), json.loads(path.with_suffix(path.suffix + ".receipt.json").read_bytes())
    if sha256(raw).hexdigest() != receipt["sha256"]:
        raise ValueError(f"Retained weather source checksum changed at {path}")
    return raw, receipt


def gather(data):
    labels, forecasts = {}, {}
    root = data / "venues" / "snow-climate"
    cards, origin = venue_weekend_rain._cards(data, datetime.combine(DAY, time(12), timezone.utc), tuple(EVENTS), False)
    for station, card in cards.items():
        raw, annual = _receipt_payload(root / station / f"{DAY.year}.json")
        matched = [row["product"] for row in json.loads(raw)["results"] if row["station"] == station and row["valid"] == str(DAY)]
        if len(matched) != 1:
            raise ValueError(f"No unique original report for {station} {DAY}")
        body, receipt = _receipt_payload(root / station / f"{matched[0]}.txt")
        label = expansion_weather.decode_raw_report(body, station, DAY, matched[0])
        if not label:
            raise ValueError(f"Incomplete original report for {station} {DAY}")
        labels[station] = {**label, "original_receipt": receipt, "annual_receipt": annual}
        start, end = climate_window(DAY, STATION_OFFSETS[station])
        decision = start + timedelta(hours=13, minutes=5)
        forecast = expansion_replay._high_forecast(card, start, end) if card and parse_time(card["available_at"]) <= decision else None
        forecasts[station] = {"station": station, "card": card, "forecast": forecast, "decision_at": iso(decision)}
    cases = []
    with venue_archive.connect(data) as db:
        db.execute("BEGIN")
        audit = venue_exports._catalog_rejections(db)
        for station, event in EVENTS.items():
            nominal = parse_time(forecasts[station]["decision_at"]) - venue_benchmark.WAIT
            for row in db.execute("SELECT instrument_id FROM markets WHERE venue=? AND event_id=? ORDER BY instrument_id", (venue_archive.KALSHI, event)):
                detail = venue_archive.detail(data, venue_archive.KALSHI, row["instrument_id"])
                mark, gap = venue_benchmark._history_point(db, detail, nominal)
                tape = [dict(point) | {"body": json.loads(point["body"])} for point in db.execute(
                    "SELECT kind,at,native_key,receipt_id,body FROM history WHERE venue=? AND instrument_id=? AND kind IN ('trade','candle-60m') ORDER BY at,native_key",
                    (venue_archive.KALSHI, row["instrument_id"]),
                )]
                cases.append({"market": detail, "mark": mark, "mark_gap": gap, "raw_price_history": tape})
    return {"version": VERSION, "catalog_alias_audit": audit, "native": cases, "labels": labels, "forecast": forecasts,
            "forecast_origin": origin}


def evaluate(data, corpus):
    frozen, digest = research_model.freeze_inputs(data, corpus)
    rows, examples = [], []
    for item in corpus["native"]:
        market = item["market"]
        station = market["station"]
        official = corpus["labels"][station]
        actual = official["high_f"]
        bounds = temperature_window(market)
        expected = None if bounds is None else int((bounds[0] is None or bounds[0] <= actual)
                                                    and (bounds[1] is None or actual <= bounds[1]))
        payout = venue_benchmark._binary_result(market)
        forecast = corpus["forecast"][station]["forecast"]
        probability = None
        if bounds and forecast and forecast.get("high_sd_f"):
            probability = _probability(_limits({"lower_f": bounds[0], "upper_f": bounds[1]}), [forecast["high_f"]], forecast["high_sd_f"])
        rows.append({**item, "source_expected_yes": expected, "native_paid_yes": payout, "compatible_noaa_probability": probability})
        if payout != 1:
            continue
        examples.append({"venue": market["venue"], "instrument_id": market["instrument_id"], "station": station, "date": str(DAY),
                         "at": market["settlement_at"], "title": f"{station} · {actual}°F · original native instrument pointer",
                         "detail": f"Independent exchange event metadata verifies {market['instrument_id']} under {market['event_id']}. "
                                   f"The original NWS report measured {actual}°F and the exchange paid YES ${payout}.",
                         "data": {**rows[-1], "original_nws": official, "forecast_input": corpus["forecast"][station]}})
    matched = [row for row in rows if row["source_expected_yes"] is not None and row["native_paid_yes"] is not None]
    audit = corpus["catalog_alias_audit"]
    record = {"version": VERSION, "title": "Recovered exchange symbols and their original weather payouts",
              "hypothesis": "A legacy instrument may have a different literal prefix from its official event and survive an independent membership check.",
              "target": "Original native identities, settlement and same-station first complete high-temperature reports", "venues": ["Kalshi"],
              "date_window": {"paired weather date": str(DAY)}, "coverage": {**audit, "audited_temperature_instruments": len(rows),
                  "matches_with_exact_weather_and_cash": sum(row["source_expected_yes"] == row["native_paid_yes"] for row in matched),
                  "coherent_native_price_at_fixed_clock": sum(item["mark"] is not None for item in rows),
                  "native_trade_prints": sum(point["kind"] == "trade" for row in rows for point in row["raw_price_history"]),
                  "same_native_noaa_and_quote_support": sum(row["mark"] is not None and row["compatible_noaa_probability"] is not None for row in rows)},
              "metrics": [], "cases": examples, "missing": audit["still_unindexed"],
              "limits": ["The literal instrument and event IDs are retained with their original archive provenance.",
                         "Evidence covers two station-days on one calendar date. It supports an identity and payout audit.",
                         "The original pricing transport records candles and prints. It supplies no historical executable book depth."],
              "provenance": {"frozen_input": str(frozen.relative_to(data)), "frozen_input_sha256": digest,
                             "source_sha256": research_model.run_sources(sys.modules[__name__], venue_exports, expansion_weather)}}
    return research_model.write_record(data, record, {"paired_cash_weather": rows, "original_nws": corpus["labels"]})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--input", type=Path)
    args = parser.parse_args(argv)
    corpus = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else gather(args.data)
    print(evaluate(args.data, corpus))


if __name__ == "__main__":
    main()
