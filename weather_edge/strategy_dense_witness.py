"""Check extreme-report disagreement cases against the delayed one-minute ASOS archive."""

import argparse
from collections import defaultdict, deque
from hashlib import sha256
import json
from pathlib import Path
from statistics import mean
from types import SimpleNamespace

from . import archive, collector, expansion_weather, research_model, strategy_midnight_metar
from . import strategy_official_display, strategy_payout_timing, strategy_twc, venue_archive
from .market import parse_time
from .nbm import climate_window


VERSION = "daily-extrema-dense-witness-v1"
MINUTE_ARCHIVE = "https://mesonet.agron.iastate.edu/request/asos/1min.phtml"


def consecutive_mean(points):
    recent = deque(maxlen=5)
    means = []
    for reading in points:
        current = parse_time(reading["at"])
        if recent and (current - recent[-1][0]).total_seconds() != 60:
            recent.clear()
        recent.append((current, reading["temp_f"]))
        if len(recent) == 5:
            means.append({"at": reading["at"], "temp_f": round(mean(value for _, value in recent), 2)})
    return means


def trace(data, station, day, *, fetch):
    point_path = data / "archive-raw" / "asos-minute" / station / f"{day}.csv"
    if not point_path.is_file() and (not fetch or collector.paused(data)):
        return None
    offset = expansion_weather.STATIONS[station][3]
    start, end = climate_window(day, offset)
    witness = archive._archive_minute_trace(SimpleNamespace(station=station, day=day), start, end, data, False)
    return {**witness, "path": str(point_path.relative_to(data)), "sha256": sha256(point_path.read_bytes()).hexdigest()} if witness else None


def measure(case, observation, markets):
    row = case["data"]
    family = row["family"]
    extreme = max if family == "daily-high" else min
    full = observation["points"]
    arithmetic = consecutive_mean(full)
    point = extreme(full, key=lambda item: item["temp_f"])
    five_minute = extreme(arithmetic, key=lambda item: item["temp_f"], default=None)
    official = row["actual_f"]
    early, _ = climate_window(case["date"], expansion_weather.STATIONS[row["station"]][3])
    timestamps = [item["at"] for item in full if item["temp_f"] == point["temp_f"]]
    markets_fact = []
    for market in markets:
        bucket = next(b for b in market["buckets"] if b["payout"])
        markets_fact.append({"venue": bucket["venue"], "instrument_id": bucket["instrument_id"], "label": bucket["label"],
                             "payout_source": bucket["settlement_source"], "payout": bucket["payout"],
                             "native_expiration_value": bucket["expiration_value"],
                             "settlement_at": bucket["settlement_at"], "payout_received_at": bucket["settlement_received_at"]})
    fields = {"station": row["station"], "date": row["date"], "family": family, "one_minute_rows": len(full),
              "missing_one_minute_rows": observation["missing_minutes"], "first_extreme_at": timestamps[0],
              "first_extreme_minutes_after_climate_start": (parse_time(timestamps[0]) - early).total_seconds() / 60,
              "latest_retained_cli_f": official, "midnight_group_f": row["forecast"][strategy_midnight_metar.MODELS[2]],
              "point_six_hour_f": row["forecast"][strategy_midnight_metar.MODELS[1]],
              "one_minute_extreme_f": point["temp_f"], "one_minute_extreme_minus_cli_f": point["temp_f"] - official,
              "contiguous_five_minute_extreme_f": five_minute["temp_f"] if five_minute else None}
    proof = {"market_case": case, "minute_archive": observation, "observed_extreme_times": timestamps,
             "five_minute_arithmetic_extreme": five_minute, "exact_payouts": markets_fact}
    return fields, proof


def provider_context(data, station, day, family):
    report = next(iter(strategy_twc.reports(data, station=station, day=day)), None)
    name = "temperature_high_f" if family == "daily-high" else "temperature_low_f"
    provider = {key: report[key] for key in ("provider_status", "provider_issued_at", "available_at", "source_url", "body_sha256", name)} if report else None
    with venue_archive.connect(data) as db:
        rows = db.execute("""SELECT d.value,r.id,r.received_at,r.source FROM markets m JOIN metadata d
                             ON m.venue=d.venue AND d.key=m.instrument_id || '-dense-witness'
                             JOIN receipts r ON d.receipt_id=r.id
                             WHERE m.venue=? AND m.station=? AND m.day=? AND m.family=? AND r.status=200
                             ORDER BY m.instrument_id""", (venue_archive.KALSHI, station, day, family)).fetchall()
    fresh_checks = []
    for row in rows:
        market = json.loads(row["value"])["market"]
        keys = ("ticker", "status", "close_time", "expected_expiration_time", "expiration_value", "settlement_ts", "settlement_value_dollars")
        fresh_checks.append({**{key: market.get(key) for key in keys}, "receipt_id": row["id"],
                             "local_received_at": row["received_at"], "source": row["source"]})
    return {"weather_company_current_dashboard": provider, "direct_kalshi_market_receipts": fresh_checks}


def research(data, source, *, fetch):
    evidence = json.loads(source.read_bytes())
    if evidence["version"] != strategy_midnight_metar.VERSION:
        raise ValueError("Expected the midnight METAR research record")
    native, _ = strategy_payout_timing.assemble(data)
    by_target = defaultdict(list)
    for row in native:
        if row["family"] in strategy_midnight_metar.FAMILIES and (exact := strategy_official_display.normalized_event(row)):
            by_target[row["station"], row["date"], row["family"]].append(exact)
    metrics, cases, details, missed = [], [], [], []
    for case in evidence["cases"]:
        station, day, family = case["station"], case["date"], case["data"]["family"]
        try:
            observed = trace(data, station, day, fetch=fetch)
            missing = "No complete requested trace"
        except (OSError, ValueError) as error:
            observed = None
            missing = str(error)
        if not observed:
            missed.append({"station": station, "date": day, "family": family, "reason": missing})
            continue
        fields, proof = measure(case, observed, by_target[station, day, family])
        proof.update(provider_context(data, station, day, family))
        metrics.append(fields)
        title = f"{station} one-minute {family.removeprefix('daily-')} and official payout"
        published = case["data"]["official_editions"][-1]["at"]
        text = (f"Delayed minute archive {fields['one_minute_extreme_f']:.0f}°F, ASOS midnight group "
                f"{fields['midnight_group_f']:.0f}°F, official CLI {fields['latest_retained_cli_f']:.0f}°F issued {published}.")
        minute_context = {**{k: v for k, v in observed.items() if k != "points"},
                          "first_observations": observed["points"][:12],
                          "observations_at_extreme": [p for p in observed["points"]
                                                      if p["temp_f"] == fields["one_minute_extreme_f"]][:12]}
        cases.append({**{k: case[k] for k in ("station", "date", "venue", "instrument_id")}, "at": fields["first_extreme_at"],
                      "title": title, "detail": text, "data": {"metrics": fields, **proof, "minute_archive": minute_context}})
        details.append(proof)
    cases.sort(key=lambda row: abs(row["data"]["metrics"]["one_minute_extreme_minus_cli_f"]), reverse=True)
    record = {"version": VERSION, "title": "Where do dense station observations and official extremes differ?",
              "hypothesis": "Original delayed one-minute readings can corroborate a preliminary extreme even when the official daily report and payout differ.",
              "target": "Exact station and standard climate day, native payouts, original METAR remark and delayed one-minute observations.",
              "venues": list(venue_archive.VENUES), "date_window": evidence["date_window"], "metrics": metrics, "cases": cases,
              "limits": ["One-minute files are delayed observational evidence. Their local ingestion clock does not imply original real-time availability.",
                         "The five-minute number is an arithmetic check on the retained rounded readouts. Native sensor processing and official quality control may differ.",
                         "These are the case examples already published by the earlier midnight study. Population accuracy belongs to that separately frozen study."],
              "provenance": {"engine": VERSION, "prior_study": evidence["id"], "minute_source": MINUTE_ARCHIVE, "missing_cases": missed,
                             "implementation_sha256": sha256(Path(__file__).read_bytes()).hexdigest()}}
    return research_model.write_record(data, record, {"cases": details, "missing": missed})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=venue_archive.DEFAULT_DATA)
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("midnight_study", type=Path)
    args = parser.parse_args()
    path = research(args.data, args.midnight_study, fetch=args.fetch)
    result = json.loads(path.read_bytes())
    print(json.dumps({"path": str(path), "metrics": result["metrics"], "provenance": result["provenance"]}, indent=2))


if __name__ == "__main__":
    main()
