"""Reprice unchanged paper holdings under the frozen later weather probabilities."""

import argparse
import gzip
import json
from pathlib import Path

from . import research_model, study
from .market import decimal, parse_time
from .strategy_runs import DEFAULT_DATA, Predictor, digest


VERSION = "paper-held-weather-scenarios-v1"
RULE = "forward/two-sided/origin-confirmed"


def _mark(position, estimate):
    if estimate is None or estimate.get("probability") is None:
        return None
    yes = decimal(estimate["probability"])
    side = yes if position["side"] == "YES" else 1 - yes
    expected = side * decimal(position["quantity"])
    return {"side_probability": float(side), "expected_cash_payout": float(expected),
            "expected_net_from_entry": float(expected - decimal(position["max_loss"])),
            "uncertain": bool(estimate.get("uncertain"))}


def report(data, paper):
    original = json.loads(paper.read_bytes())
    saved = original["provenance"]
    frozen = json.loads(gzip.decompress((data / saved["frozen_inputs"]).read_bytes()))
    if digest(frozen) != saved["tape_sha256"]:
        raise ValueError("The frozen paper inputs differ from their recorded hash")
    artifact = study.load_selected(data, vintage=frozen["model_vintage"])
    if artifact is None:
        raise ValueError("The original NOAA baseline is unavailable")
    at = parse_time(original["date_window"]["through"])
    events = {event["id"]: event for event in frozen["capture"]["events"]}
    checks = {row["event_id"]: row for row in frozen.get("scheduled_high_weather_checks", [])
              if row["model"] == "guarded" and parse_time(row["decision_at"]) <= at}
    predict = Predictor(artifact)("two-sided")
    methods = ("at actual entry", "NOAA as of cutoff", "later guarded-weather shadow")
    values = {method: [] for method in methods}
    positions = [p for p in original["records"][RULE]["positions"] if not p["settled"]]
    cases = []
    for entry in positions:
        station, ident, side = entry["station"], entry["bucket_id"], entry["side"]
        event = events[entry["event_id"]]
        check = checks.get(event["id"], {})
        ordinary = predict(event, at).get(ident)
        guarded = check.get("estimates", {}).get(ident)
        entry_yes = entry["probability"] if side == "YES" else 1 - entry["probability"]
        estimates = ({"probability": entry_yes, "uncertain": entry["uncertain"]}, ordinary, guarded)
        marks = dict(zip(methods, (_mark(entry, estimate) for estimate in estimates), strict=True))
        for method, mark in marks.items():
            if mark is not None:
                values[method].append(mark)
        clock = check.get("decision_at", original["date_window"]["through"])
        late = " The later guarded-weather estimate falls in a thin historical tail." if guarded and guarded.get("uncertain") else ""
        probability = marks[methods[2]]
        detail = f"The entry weather estimated {entry['probability']:.2%} for {side}."
        if probability is not None:
            detail += f" The later guard scenario estimated {probability['side_probability']:.8%} for the same held side.{late}"
        cases.append({"station": station, "date": entry["date"], "venue": entry["venue"], "mode": "live",
                      "instrument_id": ident, "event_id": event["id"], "at": clock,
                      "title": f"{station} {entry['label']} {side} · later weather scenario", "detail": detail,
                      "data": {"position": entry, "as_of_cutoff": original["date_window"]["through"],
                               "current_noaa": ordinary, "guarded_weather": guarded, "weather_scenarios": marks}})
    cost = sum(p["max_loss"] for p in positions)
    metrics = [{"forecast": method, "positions_with_estimates": len(rows), "open_positions": len(positions),
                "uncertain_position_estimates": sum(row["uncertain"] for row in rows), "cash_committed": cost,
                "expected_net_from_entry": sum(row["expected_net_from_entry"] for row in rows)
                if len(rows) == len(positions) else None}
               for method, rows in values.items()]
    cases.sort(key=lambda case: -case["data"]["position"]["max_loss"])
    result = {"version": VERSION, "title": "What did later weather change on the same paper holdings?",
              "hypothesis": "Late physical observations can change the forecast valuation of the largest existing paper holding.",
              "target": "Exact held positions, fixed quantities and recorded all-in entry costs.",
              "venues": original["venues"], "date_window": original["date_window"], "metrics": metrics, "cases": cases,
              "limits": ["Expected payout uses the indicated forecast probability for each held side. These are weather scenarios, with no exit book or settlement credit.",
                         "The later precision guard can yield uncertain estimates when evidence in either tail is sparse.",
                         "Open positions and entry costs stay fixed across scenarios."],
              "provenance": {"engine": VERSION, "frozen_paper": original["id"], "frozen_inputs": saved["frozen_inputs"],
                             "frozen_input_sha256": saved["tape_sha256"], "ordinary_model_vintage": artifact["vintage"],
                             "late_guarded_high": saved.get("options", {}).get("late_guarded_high"), "paper_trial": RULE}}
    return research_model.write_record(data, result, cases)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("paper", type=Path)
    args = parser.parse_args()
    path = report(args.data, args.paper)
    print(json.dumps({"path": str(path), "metrics": json.loads(path.read_bytes())["metrics"]}, indent=2))


if __name__ == "__main__":
    main()
