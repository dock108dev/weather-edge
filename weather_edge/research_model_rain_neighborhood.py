"""Test local and wind-aligned radar precipitation estimates before a positive gauge report."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone
import gzip
import json
import math
from pathlib import Path
import re
from statistics import mean
import sys

import numpy as np
from sklearn import __version__ as sklearn_version
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from . import expansion_contract, expansion_metar, expansion_mrms, expansion_nbm, expansion_weather, research_model
from . import research_model_rain_update, research_model_rain_warm, venue_archive, venue_benchmark
from .collector import paused
from .nbm import climate_window, iso, utc

VERSION = "daily-rain-wind-aligned-radar-v1"
PRODUCT = "RadarOnly_QPE_01H"
HOUR = 15
RADII = (12, 30)
HEADINGS = tuple(range(0, 360, 45))
WINDOWS = {"fit": ("2026-08-27", "2026-09-08"), "late summer verification": ("2026-09-09", "2026-09-21"),
           "autumn verification": ("2026-10-29", "2026-11-14")}
LABELS = {"p06": "Prior-day NOAA P06 aggregation", "baseline": "Earlier-fitted NOAA prior and remaining guidance",
          "station_radar": "NOAA plus local radar cell", "neighborhood": "NOAA plus local cell and full radar neighborhood",
          "wind_aligned": "The same radar model with upwind/downwind contrast", "market": "Exact same-hour native display midpoint"}
ARMS = {"baseline": (), "station_radar": ("center",), "neighborhood": ("center", "mean"),
        "wind_aligned": ("center", "mean", "upwind_minus_downwind")}
HOURLY_GAUGE = re.compile(r"(?<!\S)P(\d{4})(?=\s|$)")


def _destination(lat, lon, distance_km, heading):
    lat, lon, bearing = map(math.radians, (lat, lon, heading))
    arc = distance_km / 6371.0
    result_lat = math.asin(math.sin(lat) * math.cos(arc) + math.cos(lat) * math.sin(arc) * math.cos(bearing))
    result_lon = lon + math.atan2(math.sin(bearing) * math.sin(arc) * math.cos(lat),
                                  math.cos(arc) - math.sin(lat) * math.sin(result_lat))
    return math.degrees(result_lat), math.degrees(result_lon)


def _locations():
    points = {}
    for station in research_model_rain_warm.STATIONS:
        lat, lon = expansion_weather.STATIONS[station][:2]
        points[station] = lat, lon, station
        for radius in RADII:
            for direction in HEADINGS:
                points[f"{station}/{radius}/{direction}"] = (*_destination(lat, lon, radius, direction),
                                                             f"{station} {radius} km bearing {direction}°")
    return points


def _bearing_distance(left, right):
    return abs((left - right + 180) % 360 - 180)


def _footprint(frame, station, direction):
    """Keep symmetric sampled sectors and report their original valid grid locations."""
    grid = frame["points"]
    center = grid[station]
    cells = [{**grid[f"{station}/{radius}/{heading}"], "heading_deg": heading, "radius_km": radius}
             for radius in RADII for heading in HEADINGS]
    valid = [cell for cell in cells if cell["amount_mm"] is not None]
    up = [cell for cell in valid if _bearing_distance(cell["heading_deg"], direction) <= 45]
    down = [cell for cell in valid if _bearing_distance(cell["heading_deg"], (direction + 180) % 360) <= 45]
    if center["amount_mm"] is None or len(valid) < 12 or min(len(up), len(down)) < 3:
        return None
    upstream, downstream = (mean(cell["amount_mm"] for cell in segment) for segment in (up, down))
    return {"center": center["amount_mm"], "mean": mean(cell["amount_mm"] for cell in valid),
            "upwind_mean_mm": upstream, "downwind_mean_mm": downstream,
            "upwind_minus_downwind": math.log1p(upstream / 0.254) - math.log1p(downstream / 0.254),
            "points": cells, "station_cell": center,
            "frame": {key: value for key, value in frame.items() if key != "points"}}


def _cards(data, station, day):
    return [card for previous in (day - timedelta(days=1), day) for cycle in research_model.NBS_CYCLES
            if (card := expansion_nbm.load_card(data, station, datetime.combine(previous, time(cycle), timezone.utc)))]


def _wind(cards, when):
    eligible = sorted((card for card in cards if utc(card["issued_at"]) <= utc(card["available_at"]) <= when
                       and when - utc(card["issued_at"]) <= timedelta(hours=36)),
                      key=lambda card: card["issued_at"], reverse=True)
    for card in eligible:
        points = [point for point in card["points"] if "WDR" in point and "WSP" in point
                  and abs(utc(point["valid_at"]) - when) <= timedelta(hours=2)]
        if points:
            point = min(points, key=lambda item: (abs(utc(item["valid_at"]) - when), item["valid_at"]))
            if 1 <= point["WDR"] <= 36 and point["WSP"] >= 4:
                return {"direction_from_deg": point["WDR"] * 10 % 360, "speed_knots": point["WSP"],
                        "valid_at": point["valid_at"], **{key: card[key] for key in ("issued_at", "available_at", "card_sha256", "source")}}
            return None
    return None


def _radar_frames(data, times):
    paths = _locations()
    frames, gaps = {}, []

    def stop_aware(request, *, timeout):
        if paused(data):
            raise OSError("Live collection stopped")
        return expansion_mrms.urlopen(request, timeout=timeout)

    def sample(stamp):
        try:
            return stamp, expansion_mrms.get_frame(data, PRODUCT, stamp, points=paths, get=stop_aware), None
        except (OSError, ValueError, TypeError, KeyError) as error:
            return stamp, None, expansion_contract.availability_error(error)

    with ThreadPoolExecutor(max_workers=3) as pool:
        for count, (stamp, frame, reason) in enumerate(pool.map(sample, times), 1):
            if frame:
                frames[stamp] = frame
            else:
                gaps.append({"valid_at": iso(stamp), "reason": reason})
            if count % 25 == 0:
                print(f"Matched radar footprint frames checked {count}/{len(times)}", flush=True)
    return frames, gaps


def _hourly_gauge(observed, station, opens, when):
    previous = None
    for row in observed:
        stamp, available = (utc(row[key]) for key in ("at", "available_at"))
        raw = (row.get("raw") or "").strip().rstrip("=")
        ident = expansion_metar.REPORT_START.match(raw)
        nominal = (stamp + timedelta(minutes=30)).replace(minute=0, second=0, microsecond=0)
        if (not ident or ident[1] != station or ident[2] != stamp.strftime("%d%H%M")
                or not opens <= stamp <= available <= when or abs(nominal - stamp) > timedelta(minutes=15)):
            continue
        remarks = raw.partition(" RMK ")[2]
        mark = HOURLY_GAUGE.search(remarks)
        if (previous is not None and timedelta(minutes=45) <= stamp - previous <= timedelta(minutes=75)
                and mark and mark[1] != "0000" and "PNO" not in remarks.split()):
            return True
        previous = stamp
    return False


def _weather_case(data, row):
    station, day = row["station"], date.fromisoformat(row["date"])
    opens, close = climate_window(day, expansion_weather.STATIONS[station][3])
    when = opens + timedelta(hours=HOUR)
    path = data / "expansion" / "replay" / str(day) / f"{station}.json"
    if not path.is_file():
        return None, "no exact original station archive"
    archive = json.loads(path.read_bytes())
    rows = [obs for obs in archive["observations"] if obs.get("receipt_source")]
    observed = expansion_weather.observations_as_of(rows, opens, when, delay_minutes=None)
    if len(observed) < 6:
        return None, "fewer than six originally timed station observations"
    if _hourly_gauge(observed, station, opens, when):
        return None, "measurable one-hour station gauge already reported"
    if research_model_rain_update.gauge_witness(observed, station, opens, when):
        return None, "measurable three- or six-hour station gauge already reported"
    cards = _cards(data, station, day)
    remaining = research_model_rain_update._remaining(cards, when, close)
    wind = _wind(cards, when)
    if not remaining or not wind:
        return None, "no original remaining guidance or defined incoming wind"
    return {**row, "at": iso(when), "remaining": remaining, "wind": wind, "station_reports": len(observed),
            "station_source": research_model._source_entry(data, path)}, None


def _price(db, market, at):
    if market["venue"] != venue_archive.KALSHI or not market["opened_at"] or not market["closed_at"]:
        return None
    if at < utc(market["opened_at"]) or at + timedelta(minutes=5) >= utc(market["closed_at"]):
        return None
    mark, _ = venue_benchmark._history_point(db, market, at)
    return mark


def gather(data, reference):
    root = json.loads(gzip.decompress(reference.read_bytes()))
    if root["version"] != research_model_rain_warm.VERSION:
        raise ValueError("Expected the two-season daily rain cases")
    native = {(row["station"], row["date"]): row for rows in root["native"].values() for row in rows
              if row["venue"] == venue_archive.KALSHI}
    prior = {(row["station"], row["date"]): row for rows in root["weather"].values() for row in rows}
    groups, missing = {name: [] for name in WINDOWS}, {name: Counter() for name in WINDOWS}
    sample_times = set()
    fit_cutoff = min(climate_window(WINDOWS["late summer verification"][0], expansion_weather.STATIONS[station][3])[0] + timedelta(hours=HOUR)
                     for station in research_model_rain_warm.STATIONS)
    for group, (first, last) in WINDOWS.items():
        for (_, day), row in sorted(prior.items()):
            if not first <= day <= last:
                continue
            if group == "fit" and utc(row["official_report"]["published_at"]) >= fit_cutoff:
                missing[group]["complete climate label unavailable before holdout"] += 1
                continue
            snapshot, reason = _weather_case(data, row)
            if snapshot is None:
                missing[group][reason] += 1
                continue
            when = utc(snapshot["at"])
            sample_times.add(when - timedelta(hours=1))
            groups[group].append(snapshot)
    frames, gaps = _radar_frames(data, sorted(sample_times))
    with venue_archive.connect(data) as db:
        for group, candidates in groups.items():
            paired = []
            for row in candidates:
                when = utc(row["at"])
                frame = frames.get(when - timedelta(hours=1))
                if not frame or not utc(frame["valid_at"]) <= utc(frame["available_at"]) <= when:
                    missing[group]["no original public radar frame by decision"] += 1
                    continue
                footprint = _footprint(frame, row["station"], row["wind"]["direction_from_deg"])
                if footprint is None:
                    missing[group]["insufficient valid local and directional radar cells"] += 1
                    continue
                item = native.get((row["station"], row["date"]))
                market, native_yes, mark = None, None, None
                if item and item["truth"] == row["truth"]:
                    candidate = item["market"]
                    if candidate["opened_at"] and candidate["closed_at"] and utc(candidate["opened_at"]) <= when < utc(candidate["closed_at"]):
                        market, native_yes, mark = candidate, item["truth"], _price(db, candidate, when)
                paired.append({**row, "radar": footprint, "market": market, "native_yes": native_yes, "mark": mark})
            groups[group] = paired
    return {"version": VERSION, "ref": research_model._source_entry(data, reference), "groups": groups,
            "frames": {"requested": len(sample_times), "retained": len(frames), "missing": gaps}, "gaps": missing}


def _features(row, keys):
    def logit(p):
        p = max(.005, min(.995, p))
        return math.log(p / (1 - p))

    remaining = row["remaining"]
    features = [logit(row["direct"]["p06"]), math.log1p(row["q06_amount_inches"] * 100),
                logit(remaining["p06_overlap_probability"]), math.log1p(remaining["weighted_q06_inches"] * 100)]
    features.extend(row["radar"][key] if key == "upwind_minus_downwind" else math.log1p(row["radar"][key] / 0.254) for key in keys)
    return features


def _score(rows, group, *, prices=False):
    methods = ("p06", *ARMS, "market") if prices else ("p06", *ARMS)
    return [{"method": method, "label": LABELS[method], "group": group, **research_model._independent_counts(rows),
             "measurable_cases": sum(row["truth"] for row in rows),
             "bucket_brier": mean((row["probabilities"][method] - row["truth"]) ** 2 for row in rows)} for method in methods] if rows else []


def _contrast(rows, baseline, candidate):
    by_date = defaultdict(list)
    for row in rows:
        truth = row["truth"]
        a, b = (row["probabilities"][k] for k in (baseline, candidate))
        by_date[row["date"]].append((b - truth) ** 2 - (a - truth) ** 2)
    values = [mean(group) for group in by_date.values()]
    if not values:
        return None
    sampled = np.random.default_rng(20261115).choice(values, size=(3000, len(values))).mean(axis=1)
    return {"baseline": baseline, "method": candidate, "delta": mean(values), "calendar_dates": len(values),
            "date_bootstrap_interval_95": np.quantile(sampled, (.025, .975)).tolist()}


def _case(row):
    radar, odds = row["radar"], row["probabilities"]
    reported = row["official_report"]["precip_as_reported"]
    amount = "a trace" if reported == "T" else f"{reported} inches"
    return {"station": row["station"], "date": row["date"], "at": row["at"], "venue": venue_archive.KALSHI,
            "instrument_id": row["market"]["instrument_id"],
            "title": f"{row['station']} · Radar upstream of the settlement station",
            "detail": (f"The preceding one-hour radar field showed {radar['center']:.2f} mm at the station and "
                       f"{radar['upwind_mean_mm']:.2f} mm upwind versus {radar['downwind_mean_mm']:.2f} mm downwind. The ordinary neighborhood model estimated "
                       f"{odds['neighborhood']:.1%}, the direction-aware model {odds['wind_aligned']:.1%}. "
                       f"Complete CLI reported {amount} and YES paid ${row['truth']}."),
            "rain_evidence": row}


def evaluate(data, inputs):
    if inputs["version"] != VERSION:
        raise ValueError("Unexpected wind-aligned radar vintage")
    frozen, digest = research_model.freeze_inputs(data, inputs)
    training = inputs["groups"]["fit"]
    if len({row["truth"] for row in training}) != 2:
        raise ValueError("Earlier matched radar data need both dry and measurable outcomes")
    fits = {}
    for name, variables in ARMS.items():
        pipeline = make_pipeline(StandardScaler(), LogisticRegression(C=1, max_iter=500))
        pipeline.fit([_features(row, variables) for row in training], [row["truth"] for row in training])
        fits[name] = pipeline
    scores, metrics, comparisons, examples = {}, [], [], []
    for group in tuple(WINDOWS)[1:]:
        original = inputs["groups"][group]
        if not original:
            continue
        predictions = {name: fits[name].predict_proba([_features(row, values) for row in original])[:, 1] for name, values in ARMS.items()}
        rows = [{**row, "probabilities": {"p06": row["direct"]["p06"],
                                         **{name: float(values[index]) for name, values in predictions.items()},
                                         **({"market": row["mark"]["probability"]} if row["mark"] else {})}}
                for index, row in enumerate(original)]
        native = [row for row in rows if row["market"]]
        priced = [row for row in native if row["mark"]]
        metrics.extend(_score(rows, group + " · raw complete climate report"))
        metrics.extend(_score(native, group + " · verified payout"))
        metrics.extend(_score(priced, group + " · exact-hour coherent displayed quote", prices=True))
        for method, baseline in (("station_radar", "baseline"), ("neighborhood", "station_radar"), ("wind_aligned", "neighborhood")):
            effect = _contrast(rows, baseline, method)
            comparisons.append({"group": group, **effect})
        if native:
            best = max(native, key=lambda row: abs(row["probabilities"]["wind_aligned"] - row["probabilities"]["neighborhood"]))
            examples.append(_case(best))
        scores[group] = rows
    record = {"version": VERSION, "title": "Does radar upwind of the station help predict measurable rain?",
              "hypothesis": "Before a positive gauge report, local radar estimates can update the station rain outcome. The incoming-wind sector may add information beyond the same surrounding field without direction.",
              "target": "Measurable precipitation in the exact station climate day at 15:00 local standard time",
              "venues": [venue_archive.KALSHI], "date_window": WINDOWS,
              "limits": ["The one-hour radar estimate is complete one hour before decision and must have a native archive publication time by decision. Climate totals come from raw complete reports.",
                         "Cases require six originally timed station reports and no eligible positive one-, three-, or six-hour gauge group. The one-hour group also needs an earlier identified station report within the climate day. Missing a positive group leaves rainfall still possible.",
                         "Near-surface NOAA NBM winds describe the forecast incoming direction. They are a proxy for storm motion. Samples use eight bearings and two radii, with symmetric sectors and minimum data coverage.",
                         "Exact-hour quotes use a five-minute allowance and a coherent two-sided spread at most 30 cents. Original quote receipt time and fillable depth are unavailable."],
              "coverage": {"fit": research_model._independent_counts(training), "earlier_measurable_days": sum(row["truth"] for row in training),
                           "radar_frames": inputs["frames"], **{key: research_model._independent_counts(rows) for key, rows in scores.items()}},
              "missing": inputs["gaps"], "metrics": metrics, "uncertainty": comparisons, "cases": examples,
              "provenance": {"earlier_rain_inputs": inputs["ref"], "frozen_input": str(frozen.relative_to(data)), "frozen_input_sha256": digest,
                             "source_documents": [expansion_mrms.GUIDE], "sklearn": sklearn_version,
                             "source_sha256": research_model.run_sources(sys.modules[__name__], expansion_mrms, expansion_nbm, research_model_rain_update)}}
    return research_model.write_record(data, record, scores)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--rain-input", type=Path)
    source.add_argument("--input", type=Path)
    args = parser.parse_args()
    corpus = json.loads(gzip.decompress(args.input.read_bytes())) if args.input else gather(args.data, args.rain_input)
    print(evaluate(args.data, corpus))


if __name__ == "__main__":
    main()
