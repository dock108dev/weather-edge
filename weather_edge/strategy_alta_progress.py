"""Replay December snowfall probabilities as Alta publishes in-month accumulation."""

import argparse
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
from statistics import mean

from . import research_model, strategy_alta as alta, venue_archive


VERSION = "alta-december-accumulation-v1"
CLOCKS = (7, 14, 21)
METHODS = ("Preseason three nearest winters", "Observed plus earlier same-date remainder", "Observed plus three analogous remainders",
           "Preseason forecast with accumulated snowfall floor")


def received_before(row, cutoff):
    return all(isinstance(row.get(key), str) and datetime.fromisoformat(row[key]) <= cutoff for key in ("created_at", "updated_at"))


def observed(row, clock):
    cutoff = datetime(row["year"], 12, clock, 12)
    if len(row["boundary_reports"]) != 2:
        return None
    start = row["boundary_reports"][0]
    if not (start["reported_at"] < f"{row['year']}-12-01" and received_before(start, cutoff)):
        return None
    prior = [r for r in row["december_rows"] if r["reported_at"] < cutoff.date().isoformat() and received_before(r, cutoff)]
    if not prior:
        return None
    witness = prior[-1]
    accumulated = alta.inches(witness["ytd_snow"]) - alta.inches(start["ytd_snow"])
    if accumulated < 0:
        return None
    return {"year": row["year"], "cutoff": cutoff.isoformat(), "accumulated_in": float(accumulated),
            "to_date": witness["reported_at"], "days_since_report": (cutoff.date() - datetime.fromisoformat(witness["reported_at"]).date()).days,
            "november_anchor": start, "last_known": witness, "actual_december_in": row["december_in"],
            "mid_november_in": row["mid_november"]["mid_november_ytd_in"]}


def predictive_samples(fit, current):
    history = sorted(fit, key=lambda row: (abs(row["mid_november_in"] - current["mid_november_in"]), row["year"]))
    close_weather = sorted(fit, key=lambda row: (abs(row["accumulated_in"] - current["accumulated_in"]), row["year"]))
    baseline = [row["actual_december_in"] for row in history[:3]]
    seen = current["accumulated_in"]

    def remainder(row):
        return seen + max(0, row["actual_december_in"] - row["accumulated_in"])

    return {METHODS[0]: baseline, METHODS[1]: list(map(remainder, fit)), METHODS[2]: list(map(remainder, close_weather[:3])),
            METHODS[3]: [max(seen, sample) for sample in baseline]}


def phase_rows(seasons, boundaries, clock):
    return [point for row in seasons if boundaries[0] <= row["year"] <= boundaries[-1] and row["mid_november"]
            and (point := observed(row, clock))]


def eval_phase(fit, candidates, thresholds):
    results = []
    for row in candidates:
        probabilistic = {}
        for name, sample in predictive_samples(fit, row).items():
            p = [alta.probability(sample, strike) for strike in thresholds]
            scores = [(prediction - (row["actual_december_in"] > strike)) ** 2 for prediction, strike in zip(p, thresholds, strict=True)]
            probabilistic[name] = {"p": p, "brier": mean(scores), "forecast_mean": mean(sample)}
        results.append({**row, "models": probabilistic})
    return results


def research(data):
    seasons, _, gaps, origin, page = alta.dataset(data)
    contracts = alta.listings(data)
    thresholds = [row["threshold_in"] for row in contracts]
    metrics, details, cases = [], {}, []
    for clock in CLOCKS:
        fit = phase_rows(seasons, alta.PHASES["fit"], clock)
        selection = phase_rows(seasons, alta.PHASES["selection"], clock)
        held = phase_rows(seasons, alta.PHASES["later test"], clock)
        if not (fit and selection and held):
            continue
        earlier, later = eval_phase(fit, selection, thresholds), eval_phase(fit, held, thresholds)
        chosen = min(METHODS, key=lambda name: mean(row["models"][name]["brier"] for row in earlier))
        for phase, rows in (("selection", earlier), ("later test", later)):
            for name in METHODS:
                metrics.append({"window": phase, "method": name, "decision_day_in_december": clock,
                                "earlier_selected": name == chosen, "fit_seasons": len(fit), "independent_seasons": len(rows),
                                "threshold_checks": len(rows) * len(thresholds),
                                "brier": mean(row["models"][name]["brier"] for row in rows),
                                "mae_inches": mean(abs(row["models"][name]["forecast_mean"] - row["actual_december_in"]) for row in rows),
                                "known_accumulation_exceeding_later_total": sum(row["accumulated_in"] > row["actual_december_in"] for row in rows)})
        details[str(clock)] = {"fit": fit, "selection": earlier, "test": later, "selected_method": chosen}
        example = max(later, key=lambda row: abs(row["models"][METHODS[0]]["forecast_mean"] - row["models"][chosen]["forecast_mean"]))
        contract = min(contracts, key=lambda r: abs(r["threshold_in"] - example["actual_december_in"]))
        cases.append({"region": "Alta Ski Area, Collins Study Plot", "date": f"{example['year']}-12-{clock:02}",
                      "venue": venue_archive.KALSHI, "instrument_id": contract["instrument_id"], "at": None,
                      "title": f"Alta {example['year']} remaining December snowfall",
                      "detail": f"Reported {example['accumulated_in']:.1f} inches by the cutoff. Observed December total {example['actual_december_in']:.1f} inches. The linked 2026 instrument has the comparable threshold.",
                      "data": {"archived_weather": example, "linked_listing": contract, "tuned_method": chosen}})
    if not metrics:
        raise ValueError("No date split has earlier and later original accumulation evidence")
    result = {"version": VERSION, "title": "Alta December snowfall as official accumulation arrives",
              "hypothesis": "Adding published partial accumulation to a distribution of remaining snowfall improves December threshold forecasts.",
              "target": "Official historical Collins Study Plot totals replayed on current native December strikes.", "venues": [venue_archive.KALSHI],
              "date_window": {"from": "2004-12-01", "through": "2025-12-31", **alta.PHASES}, "metrics": metrics, "cases": cases,
              "limits": ["Every partial input preserves the provider's dated cumulative figure and created and updated timestamps before noon on the specified local day.",
                         "Older archive gaps can leave an earlier retained official reading before the desired calendar cutoff. Its exact age is preserved.",
                         "Scores use historical weather labels. The linked open 2026 thresholds have no historical venue payouts.",
                         "The floor comparison keeps the preseason distribution's remaining uncertainty and excludes modeled totals below the observed amount.",
                         "The observed-plus-remainder comparison uses earlier matched dates to calibrate future snowfall. No executable trade replay is inferred."],
              "provenance": {"engine": VERSION, "horizons_in_december": CLOCKS, "raw_seasons": origin, "website": page,
                             "thresholds": thresholds, "weather_gaps": gaps, "sample_definition": "same cases per decision clock",
                             "implementation_sha256": sha256(Path(__file__).read_bytes()).hexdigest()}}
    return research_model.write_record(data, result, {"clocks": details, "native_thresholds": contracts})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=venue_archive.DEFAULT_DATA)
    args = parser.parse_args()
    path = research(args.data)
    record = json.loads(path.read_bytes())
    print(json.dumps({"path": str(path), "metrics": record["metrics"], "cases": [{k: v for k, v in row.items() if k != "data"}
                                                                                for row in record["cases"]]}, indent=2))


if __name__ == "__main__":
    main()
