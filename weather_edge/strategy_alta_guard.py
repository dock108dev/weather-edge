"""Compare verified Alta Collins December totals with the separate Alta Guard gauge."""

import argparse
from hashlib import sha256
import json
from pathlib import Path
import re
from statistics import mean

from . import research_model, strategy_alta, venue_archive


VERSION = "alta-december-gauge-comparison-v1"
GUARD_PAGE = "https://utahavalanchecenter.org/alta-monthly-snowfall"
GUARD_WORKBOOK = "https://utahavalanchecenter.org/sites/default/files/attached_files/2026.05.27_Atwater_Snow_and_Water__0.xlsx"


def guard_decembers(export):
    rows = export["values"]
    if (export["address"].split(":")[0] != "A1" or rows[5][0] != "WINTER YEAR"
            or rows[5][2:5] != ["October", "November", "December"] or rows[6][2:8] != ["Snow", "H20"] * 3):
        raise ValueError("The published Alta Guard season and December snow columns changed")
    totals = {}
    for line, row in enumerate(rows[7:], 8):
        season = row[0]
        if not isinstance(season, str) or not (identity := re.fullmatch(r"(\d{4}) to (\d{2})", season)):
            continue
        year, end = map(int, identity.groups())
        if end != (year + 1) % 100 or year in totals:
            raise ValueError(f"Unexpected winter season identity in cell A{line}")
        snow = row[6]
        if snow is not None:
            if type(snow) not in (int, float) or snow < 0:
                raise ValueError(f"Non-numeric Alta Guard snow in cell G{line}")
            totals[year] = {"guard_december_in": float(snow), "guard_workbook_row": line, "guard_winter_label": season}
    return totals


def guard_snow(row):
    return row["guard_december_in"]


def _window(rows, name, cuts, estimate):
    return {"window": name, "paired_winters": len(rows), "thresholds": len(cuts), "paired_thresholds": len(rows) * len(cuts),
            "mean_collins_minus_guard_inches": mean(row["collins_december_in"] - estimate(row) for row in rows),
            "mean_absolute_snow_difference_inches": mean(abs(row["collins_december_in"] - estimate(row)) for row in rows),
            "winters_with_different_strike_outcome": sum(any((estimate(row) > cut) != (row["collins_december_in"] > cut) for cut in cuts) for row in rows),
            "different_strike_outcomes": sum((estimate(row) > cut) != (row["collins_december_in"] > cut) for row in rows for cut in cuts)}


def research(data, workbook=None, export=None):
    root = data / strategy_alta.ROOT
    workbook = workbook or root / "atwater-snow-water.xlsx"
    export = export or root / "atwater-archive-cells.json"
    raw_guard = guard_decembers(json.loads(export.read_bytes()))
    collins, _, gaps, collins_proof, page = strategy_alta.dataset(data)
    market = {x["threshold_in"]: x for x in strategy_alta.listings(data)}
    cuts = sorted(market)
    paired = []
    for row in sorted(collins, key=lambda candidate: candidate["year"]):
        year = row["year"]
        if year not in raw_guard:
            continue
        actual = row["december_in"]
        guard_value = raw_guard[year]["guard_december_in"]
        disagreement = [{"threshold_in": cut, "collins_above": actual > cut, "guard_above": guard_value > cut,
                         "instrument": market[cut]} for cut in cuts if (actual > cut) != (guard_value > cut)]
        paired.append({"year": year, "collins_december_in": actual, **raw_guard[year], "source": collins_proof[year],
                       "different_thresholds": disagreement})
    early = [row for row in paired if row["year"] < 2019]
    later = [row for row in paired if row["year"] >= 2019]
    if not early or not later:
        raise ValueError("Verified Alta gauge pairs are missing from one of the two weather cohorts")
    correction = mean(row["collins_december_in"] - guard_snow(row) for row in early)
    metrics = [_window(paired, "all verified physical comparisons", cuts, guard_snow),
               _window(early, "earlier winters 2004–2018", cuts, guard_snow),
               _window(later, "later winters 2019–2025, raw Guard", cuts, guard_snow),
               _window(later, "later winters 2019–2025, earlier fitted offset", cuts, lambda row: guard_snow(row) + correction)]
    cases = []
    for row in sorted(paired, key=lambda item: (-len(item["different_thresholds"]), -item["year"])):
        for difference in row["different_thresholds"]:
            cut = difference["threshold_in"]
            instrument = difference["instrument"]
            cases.append({"region": "Alta, Utah", "date": f'{row["year"]}-12-01', "venue": venue_archive.KALSHI,
                          "instrument_id": instrument["instrument_id"], "title": f'{row["year"]} gauge comparison at {cut:g} inches',
                          "detail": f'Collins recorded {row["collins_december_in"]:g} inches. Alta Guard recorded {guard_snow(row):g} inches. These historical totals land on different sides of the current strict threshold.',
                          "data": {"historical_pair": row, "strike_comparison": difference, "contract_date": instrument["day"],
                                   "guard_cell": f'Sheet1!G{row["guard_workbook_row"]}'}})
    guard_proof = strategy_alta._archived_proof(workbook, data, GUARD_WORKBOOK)
    record = {"version": VERSION, "title": "Alta’s two snowfall gauges on December thresholds",
              "hypothesis": "Alta Guard monthly snowfall can approximate the contract’s official Collins December amount after a bias adjustment fitted on earlier winters.",
              "target": "Exact verified December snowfall at Collins Study Plot, with a same-winter comparison to separate Alta Guard observations.",
              "venues": [venue_archive.KALSHI], "date_window": {"from": str(paired[0]["year"]), "through": str(paired[-1]["year"]),
                                                                "fit_through": "2018", "holdout_from": "2019"},
              "metrics": metrics, "cases": cases[:12],
              "limits": ["The governing rules use Alta Ski Area’s reporting hierarchy. Alta Guard is a separate published gauge.",
                         "Both observations are retrospective December totals. The current Kalshi strike grid supplies comparison thresholds.",
                         "The seven-winter holdout and lack of original data receipts limit forecast or execution conclusions."],
              "provenance": {"engine": VERSION, "guard_archive": GUARD_PAGE, "guard_workbook": guard_proof,
                             "guard_extracted_values_sha256": sha256(export.read_bytes()).hexdigest(),
                             "guard_worksheet_columns": "Sheet1 calendar-year label A and December snowfall in inches G",
                             "guard_available_decembers": len(raw_guard), "collins_source": page, "collins_gaps": gaps,
                             "rule_url": strategy_alta.RULES_URL, "earlier_mean_offset_inches": correction}}
    return research_model.write_record(data, record, {"pairs": paired, "guard_december_cells": raw_guard, "native_thresholds": market})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=venue_archive.DEFAULT_DATA)
    args = parser.parse_args()
    saved = research(args.data)
    record = json.loads(saved.read_bytes())
    print(json.dumps({"path": str(saved), "metrics": record["metrics"], "earlier_mean_offset_inches": record["provenance"]["earlier_mean_offset_inches"]}, indent=2))


if __name__ == "__main__":
    main()
