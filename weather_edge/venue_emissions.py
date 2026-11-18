"""Check the governing inventories and public scenarios for active emissions goals."""

import argparse
import gzip
from html import unescape
import json
from pathlib import Path
import re
import subprocess
import sys
from unicodedata import category

from . import research_model, venue_archive, venue_sources, venue_storm
from .collector import paused

VERSION = "climate-emissions-source-and-goal-v1"
FOLDER = "emissions-goals"
BOOK_SCOPE = "Annual net emissions goals"
IDS = ("USCLIMATE-2025", "USCLIMATE-2030", "EUCLIMATE-2030")
SOURCES = {
    "epa-inventory-index.html": "https://www.epa.gov/ghgemissions/inventory-us-greenhouse-gas-emissions-and-sinks",
    "epa-1990-2023.pdf": "https://www.epa.gov/system/files/documents/2025-04/us-ghg-inventory-2025-main-text.pdf",
    "eia-2025.html": "https://www.eia.gov/environment/emissions/carbon/",
    "eea-2030.html": "https://www.eea.europa.eu/en/analysis/indicators/total-greenhouse-gas-emission-trends/",
    "usclimate-terms.pdf": "https://assets.kalshi.com/regulatory/product-certifications/USCLIMATE.pdf",
    "euclimate-terms.pdf": "https://assets.kalshi.com/regulatory/product-certifications/EUCLIMATE.pdf",
}


def _texts(data, collect):
    texts, receipts = {}, {}
    for name, source in SOURCES.items():
        if collect and paused(data):
            raise venue_sources.CollectionPaused
        raw, receipt = (venue_storm._get(data, name, source, folder=FOLDER) if collect
                        else venue_storm._existing(data, name, folder=FOLDER))
        if receipt["source"] != source:
            raise ValueError(f"Original URL changed for {name}")
        if name.endswith(".pdf"):
            extracted = subprocess.run(["pdftotext", "-layout", "-", "-"], input=raw, stdout=subprocess.PIPE, check=True).stdout
            texts[name] = "".join(char for char in extracted.decode("utf-8") if category(char) != "Cf")
        else:
            texts[name] = unescape(re.sub(r"<[^>]+>", " ", raw.decode("utf-8", errors="replace")))
        receipts[name] = receipt
    return texts, receipts


def _facts(texts):
    us_rule = "".join(texts["usclimate-terms.pdf"].split())
    eu_rule = "".join(texts["euclimate-terms.pdf"].split())
    if not all(item.replace(" ", "") in us_rule for item in ("net U.S. emissions", "either 2024 or 2025", "3317.5 million metric tonnes of CO2 equivalent")):
        raise ValueError("Required official US rule text did not validate")
    if not all(item.replace(" ", "") in eu_rule for item in ("releases from the European Union", "55%", "1990 level by 2030")):
        raise ValueError("Required official EU rule text did not validate")
    inventory = re.search(r"^\s*Net Emissions \(Sources and Sinks\)\s+((?:[0-9,]+\.\d\s+){7})",
                          texts["epa-1990-2023.pdf"], re.M)
    if not inventory or not re.search(r"1990\s*[-–]\s*2023\s+DRAFT Inventory", texts["epa-inventory-index.html"], re.I):
        raise ValueError("EPA net row or draft label unavailable")
    net = [float(value.replace(",", "")) for value in inventory[1].split()]
    eia = " ".join(texts["eia-2025.html"].split())
    if not re.search(r"\bTotal\s+4,906\s+4,945\s+4,799\s+4,789\s+4,904\b", eia):
        raise ValueError("Expected official 2025 EIA annual emission table missing")
    european = " ".join(texts["eea-2030.html"].split())
    if not all(re.search(token, european, re.I) for token in ("fell by 39% between 1990 and 2025", "48% reduction", "reach 54%")):
        raise ValueError("EEA inventory and scenario markers missing")
    return {"epa_inventory": {"status": "EPA labels 1990–2023 as draft in the retained inventory index",
                              "net_million_metric_tonnes_CO2e": dict(zip((1990, 2005, 2019, 2020, 2021, 2022, 2023), net, strict=True))},
            "eia_energy_only_2025_million_metric_tonnes_CO2": 4904,
            "eea_2025_approximate_reduction_percent": 39, "eea_2030_existing_policies_percent": 48,
            "eea_2030_existing_and_planned_percent": 54}


def gather(data, *, collect=False):
    texts, sources = _texts(data, collect)
    if collect:
        venue_sources.sample_kalshi_books(data, IDS, scope=BOOK_SCOPE)
    results = []
    for ticker in IDS:
        market = venue_archive.detail(data, venue_archive.KALSHI, ticker)
        if not market or market["family"] != "annual-emissions":
            raise ValueError(f"Native scope {ticker} is unavailable")
        results.append({"market": market, "book": next((book for book in market["books"] if book.get("depth_kind") == "displayed_ladder"), None)})
    return {"version": VERSION, "facts": _facts(texts), "markets": results, "retained_sources": sources}


def evaluate(data, corpus):
    frozen, digest = research_model.freeze_inputs(data, corpus)
    facts = corpus["facts"]
    descriptions = {
        "USCLIMATE-2025": "The exchange requires either 2024 or 2025 EPA net emissions of 4,909.9 million tonnes CO₂ equivalent or fewer. "
                          "EIA reports 4,904 million tonnes of 2025 energy-related CO₂. That narrower physical measure does not meet the filed source test.",
        "USCLIMATE-2030": "At least one 2024–2030 calendar year must meet the 3,317.5 million tonne EPA net threshold. "
                          "The retained inventory page supplies no qualifying year.",
        "EUCLIMATE-2030": "EEA reports an approximate 39% decrease through 2025 and 2030 scenarios of 48% under existing measures or 54% with planned measures. "
                         "The payout requires an actual European Union determination for the 55% goal.",
    }
    cases = []
    for row in corpus["markets"]:
        market, book = row["market"], row["book"]
        ticker = market["instrument_id"]
        cases.append({"venue": market["venue"], "instrument_id": ticker, "date": market["day"], "at": book["at"] if book else None,
                      "title": f"{market['region']} · {market['day'][:4]} climate commitment", "detail": descriptions[ticker],
                      "data": {**row, "agency_evidence": facts}})
    result = {"version": VERSION, "title": "Climate commitments, an emissions source mismatch and pending payouts",
              "hypothesis": "A reported total below the US threshold can come from a different emissions inventory and imply the wrong result.",
              "target": "Actual agency determinations for the two US net inventory strikes and the European Union climate goal", "venues": ["Kalshi"],
              "date_window": {"eligible US calendar years": [2024, 2030], "EU goal calendar year": 2030},
              "coverage": {"native_instruments": len(cases), "native_verified_payouts": sum(row["market"]["yes_payout"] is not None for row in corpus["markets"]),
                           "native_ladders": sum(row["book"] is not None for row in corpus["markets"]), "original_public_sources": len(corpus["retained_sources"])},
              "metrics": [], "cases": cases, "missing": {"US EPA net 2024 and 2025 governing inventory": "Neither calendar year is available in the retained inventory page.",
              "EU 2030 official accomplishment": "Future target. EEA scenarios are separate from an outcome release.",
              "European Council public Paris Agreement page": "Direct source receipt returned HTTP 403. The original certified text and EEA sources are retained."},
              "limits": ["The federal series measures net CO₂-equivalent emissions from all sectors including land sinks. The EIA series measures energy CO₂.",
                         "EPA draft results and EEA scenarios use stated source scope and remain separate from final qualifying releases.",
                         "The book sample supplies displayed depth and a local receipt with no native image time."],
              "provenance": {"official_sources": corpus["retained_sources"], "frozen_input": str(frozen.relative_to(data)),
                             "frozen_input_sha256": digest, "source_sha256": research_model.run_sources(sys.modules[__name__], venue_storm)}}
    return research_model.write_record(data, result, corpus["facts"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--collect", action="store_true")
    parser.add_argument("--input", type=Path)
    args = parser.parse_args(argv)
    try:
        corpus = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else gather(args.data, collect=args.collect)
        print(evaluate(args.data, corpus))
    except venue_sources.CollectionPaused:
        print("Collection is stopped. Retained receipts are available.")
        return 2


if __name__ == "__main__":
    sys.exit(main())
