"""Compare dated NOAA Atlantic outlooks and settled Kalshi seasonal counts."""

import argparse
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
import gzip
from hashlib import sha256
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from statistics import NormalDist, mean
import sys
import time
from urllib.parse import quote, urljoin, urlsplit
from urllib.request import Request, build_opener

from sklearn import __version__ as sklearn_version
from sklearn.linear_model import Ridge

from . import research_model, venue_archive
from .capture import ImmutableOrigin
from .collector import paused
from .history import _write
from .market import iso, parse_time
from .venue_benchmark import _probability_proxy
from .venue_history import _candle_entries, _save_history
from .venue_sources import EXCEPTIONS, PublicGET, _rows, _scan_error

VERSION = "atlantic-seasonal-storm-v2"
CPC = "https://www.cpc.ncep.noaa.gov/products/outlooks/hurricane-archive_body.html"
NHC = "https://www.nhc.noaa.gov/data/"
HURDAT_DOC = "https://www.nhc.noaa.gov/data/hurdat/hurdat2-format-atl-1851-2021.pdf"
HURDAT_SOURCE = "https://www.nhc.noaa.gov/data/hurdat/hurdat2-1851-2025-092326.txt"
CANDLE_KIND = "candle-1d"
FIT = (2008, 2016)
TEST = (2017, 2025)
CLIMATOLOGY_START = 1995
PHASES = ("may", "august")
KINDS = ("storms", "hurricanes", "major hurricanes")
WIND = {"storms": 34, "hurricanes": 64, "major hurricanes": 96}
THRESHOLDS = {"storms": (10, 14, 18, 22, 26, 30), "hurricanes": (3, 5, 7, 9, 11, 13),
              "major hurricanes": (1, 2, 3, 4, 6, 7)}
SERIES = {"KXTROPSTORM": "storms", "KXHURCTOT": "hurricanes", "KXHURCTOTMAJ": "major hurricanes"}
MODELS = {"climatology": "Earlier Atlantic climatology", "noaa": "NOAA outlook range proxy",
          "calibrated": "Earlier-season Ridge on NOAA range center"}
BINARY_WIND = {"TS", "HU", "SS"}
STORM_HEADER = re.compile(r"AL\d{6}")
ISSUED = re.compile(r"Issued\s*:?\s*(\d{1,2}\s+[A-Za-z]+\s+20\d{2}|[A-Za-z]+\s+\d{1,2},?\s+20\d{2})", re.I)
RANGE = re.compile(
    r"(?<!\d)(\d{1,2})\s*(?:[-–—]|to)\s*(\d{1,2})\s*(?:(?:are )?expected to become )?"
    r"(named storms|major hurricanes|hurricanes)", re.I,
)


class Page(HTMLParser):
    def __init__(self, data):
        super().__init__()
        self.text_parts = []
        self.links = []
        self.rows = []
        self.row = None
        self.cell = None
        self.feed(data.decode("utf-8", errors="replace"))

    def handle_data(self, data):
        self.text_parts.append(data)
        if self.cell is not None:
            self.cell.append(data)

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            if (path := dict(attrs).get("href")):
                self.links.append(path)
        elif tag == "tr":
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None and self.row is not None:
            self.row.append(" ".join(" ".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None

    def text(self):
        return " ".join(" ".join(self.text_parts).split())


def _root(data, folder="hurricane"):
    return Path(data) / "venues" / folder


def _get(data, path, source, *, folder="hurricane", max_bytes=20 * 2**20, accept=None):
    archive = _root(data, folder) / path
    manifest = archive.with_suffix(".receipt.json")
    if archive.is_file() and manifest.is_file():
        return _existing(data, path, folder=folder)
    if paused(data):
        raise RuntimeError("Live collection has been stopped")
    target = urlsplit(source)
    allowed = {"www.nhc.noaa.gov", "www.cpc.ncep.noaa.gov", "www.ncei.noaa.gov", "data.giss.nasa.gov", "www.nasa.gov",
               "www.spc.noaa.gov", "psl.noaa.gov", "noaadata.apps.nsidc.org", "nsidc.org", "www.usbr.gov", "data.usbr.gov",
               "ftp.nhc.noaa.gov", "www.wpc.ncep.noaa.gov", "noaa-global-hourly-pds.s3.amazonaws.com",
               "droughtmonitor.unl.edu", "usdmdataservices.unl.edu", "apps.glerl.noaa.gov", "assets.kalshi.com",
               "files.airnowtech.org", "www.epa.gov", "www.eia.gov", "www.eea.europa.eu", "www.consilium.europa.eu"}
    if target.scheme != "https" or target.netloc not in allowed:
        raise ValueError("Unexpected public weather source")
    requested_at = iso(datetime.now(timezone.utc))
    headers = {"User-Agent": "WeatherEdge/0.1 local weather research"}
    if accept:
        headers["Accept"] = accept
    req = Request(source, headers=headers, method="GET")
    with build_opener(ImmutableOrigin()).open(req, timeout=15) as response:
        content = response.read(max_bytes + 1)
        if len(content) > max_bytes:
            raise ValueError("External weather evidence exceeded the download bound")
        receipt = {
            "source": source, "requested_at": requested_at, "received_at": iso(datetime.now(timezone.utc)),
            "sha256": sha256(content).hexdigest(), "http_last_modified": response.headers.get("Last-Modified"),
        }
    _write(archive, content)
    _write(manifest, json.dumps(receipt, separators=(",", ":")).encode())
    time.sleep(0.25)
    return content, {**receipt, "path": str(archive.relative_to(Path(data)))}


def _existing(data, path, *, folder="hurricane"):
    archive = _root(data, folder) / path
    receipt = json.loads(archive.with_suffix(".receipt.json").read_bytes())
    content = archive.read_bytes()
    if sha256(content).hexdigest() != receipt["sha256"]:
        raise ValueError(f"Weather data checksum changed for {path}")
    return content, {**receipt, "path": str(archive.relative_to(Path(data)))}


def fetch(data):
    index, _ = _get(data, "cpc-index.html", CPC)
    for path in Page(index).links:
        pick = re.search(r"hurricane(20\d{2})/(May|August)/", path, re.I)
        if pick and FIT[0] <= int(pick[1]) <= TEST[1] + 1:
            _get(data, f"outlooks/{pick[1]}-{pick[2].lower()}.html", urljoin(CPC, path))
    _get(data, HURDAT_SOURCE.rsplit("/", 1)[-1], HURDAT_SOURCE)
    for year in range(CLIMATOLOGY_START, TEST[1] + 1):
        source = f"{NHC}tcr/index.php?basin=atl&season={year}"
        _get(data, f"season-summary/{year}.html", source)


def parse_outlook(body, year, phase):
    text = Page(body).text()
    match = ISSUED.search(text)
    if match is None:
        raise ValueError("Missing explicit issue date")
    issued = None
    for fmt in ("%d %B %Y", "%d %b %Y", "%B %d %Y", "%b %d %Y"):
        try:
            issued = datetime.strptime(match[1].replace(",", ""), fmt).date()
            break
        except ValueError:
            pass
    if issued is None or issued.year != year or issued.month != {"may": 5, "august": 8}[phase]:
        raise ValueError("NOAA outlook issue does not match archive year and phase")
    hits = list(RANGE.finditer(text))
    for first, second, third in zip(hits, hits[1:], hits[2:]):
        if [item[3].lower() for item in (first, second, third)] != ["named storms", "hurricanes", "major hurricanes"]:
            continue
        if third.end() - first.start() > 430:
            continue
        context = text[max(0, first.start() - 470):third.end() + 100]
        confidence = list(re.finditer(r"\b(67|70)\s*(?:%|percent)", context, re.I))
        if not confidence:
            continue
        bounds = {kind: [int(item[1]), int(item[2])] for kind, item in zip(KINDS, (first, second, third), strict=True)}
        if any(not 0 <= pair[0] <= pair[1] for pair in bounds.values()):
            raise ValueError("Invalid NOAA outlook interval")
        release_after = datetime.combine(issued + timedelta(days=1), datetime.min.time(), timezone.utc)
        coverage_probability = int(confidence[-1][1]) / 100
        return {"year": year, "phase": phase, "issue_day": str(issued), "usable_after_day_bounds": iso(release_after),
                "ranges": bounds, "reported_range_probability": coverage_probability}
    raise ValueError("No documented whole-season count ranges found")


def parse_hurdat(body):
    tracks = []
    current = None
    for line in body.decode("ascii", errors="strict").splitlines():
        fields = [value.strip() for value in line.split(",")]
        if STORM_HEADER.fullmatch(fields[0]):
            if current and len(current["observations"]) != current["record_count"]:
                raise ValueError(f"Incomplete HURDAT storm {current['id']}")
            current = {"id": fields[0], "name": fields[1], "record_count": int(fields[2]), "observations": []}
            if int(fields[0][-4:]) >= CLIMATOLOGY_START:
                tracks.append(current)
            continue
        if current is None or len(fields) < 7:
            raise ValueError("Invalid HURDAT record")
        validity = datetime.strptime(fields[0] + fields[1], "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
        current["observations"].append({
            "at": iso(validity), "category": fields[3], "wind_kt": int(fields[6]), "event": fields[2],
            "latitude": float(fields[4][:-1]) * (-1 if fields[4].endswith("S") else 1),
            "longitude": float(fields[5][:-1]) * (-1 if fields[5].endswith("W") else 1),
        })
    if current and len(current["observations"]) != current["record_count"]:
        raise ValueError(f"Incomplete HURDAT storm {current['id']}")
    return tracks


def _summary_date(raw, year):
    for pattern in ("%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(f"{raw} {year}", pattern).date()
        except ValueError:
            continue
    raise ValueError(f"Unexpected NHC basin date {raw}")


def parse_season_summary(body, year):
    captured = {}
    for row in Page(body).rows:
        if len(row) != 10 or not row[0].isdigit() or row[2] not in {"TD", "SD", "TS", "SS", "HU", "MH", "PTC"}:
            continue
        interval = re.fullmatch(r"\s*(\d{1,2} [A-Za-z]{3,9})\s*[–—-]\s*(\d{1,2} [A-Za-z]{3,9})\s*", row[3])
        if not interval or not row[4].isdigit():
            raise ValueError(f"Unexpected NHC summary row {row[:5]}")
        start, end = (_summary_date(piece, year) for piece in interval.groups())
        if end < start:
            end = end.replace(year=year + 1)
        identity = f"AL{int(row[0]):02}{year}"
        if identity in captured:
            raise ValueError(f"Repeated NHC summary storm {identity}")
        captured[identity] = {"id": identity, "name": row[1], "category": row[2], "max_wind_kt": int(row[4]),
                              "first_date": start.isoformat(), "last_date": end.isoformat(), "status": row[-1]}
    if not captured:
        raise ValueError(f"Missing NHC Atlantic season summary for {year}")
    return captured


def _wind_events(tracks, basin, year, kind, *, first=None, through=None):
    first = first or date(year, 1, 1)
    through = through or date(year, 12, 1)
    events = []
    for track in tracks:
        official = basin.get(track["id"])
        if not official or official["max_wind_kt"] < WIND[kind]:
            continue
        begin = max(first.isoformat(), official["first_date"])
        stop = min(through.isoformat(), official["last_date"])
        possible = [row for row in track["observations"] if begin <= row["at"][:10] <= stop
                    and row["wind_kt"] >= WIND[kind]
                    and row["wind_kt"] <= official["max_wind_kt"]
                    and row["category"] in (BINARY_WIND if kind == "storms" else {"HU"})]
        if possible:
            earliest = min(possible, key=lambda row: row["at"])
            peak = max(possible, key=lambda row: row["wind_kt"])
            events.append({"id": track["id"], "name": track["name"], "first_crossing": earliest,
                           "strongest": peak, "basin_summary": official})
    return events


def _forecast_source(data):
    outlooks = []
    gaps = {}
    for year in range(FIT[0], TEST[1] + 2):
        for phase in PHASES:
            try:
                raw, receipt = _existing(data, f"outlooks/{year}-{phase}.html")
                picked = parse_outlook(raw, year, phase)
            except EXCEPTIONS as error:
                gaps[f"{year}-{phase}"] = str(error)
                continue
            outlooks.append({**picked, "receipt": receipt})
    return outlooks, gaps


def _season_rows(data):
    outlooks, gaps = _forecast_source(data)
    raw, source = _existing(data, HURDAT_SOURCE.rsplit("/", 1)[-1])
    tracks = parse_hurdat(raw)
    basin, sources = {}, []
    counts = {}
    for year in range(CLIMATOLOGY_START, TEST[1] + 1):
        try:
            raw_summary, receipt = _existing(data, f"season-summary/{year}.html")
            scope = parse_season_summary(raw_summary, year)
        except EXCEPTIONS as error:
            gaps[f"Atlantic basin {year}"] = str(error)
            continue
        basin.update(scope)
        sources.append(receipt)
        counts[str(year)] = {kind: len(_wind_events(tracks, scope, year, kind)) for kind in KINDS}
    return {"version": VERSION, "outlooks": outlooks, "gaps": gaps, "counts": counts,
            "best_track_source": source, "basin_sources": sources,
            "settings": {"fit": FIT, "test": TEST, "wind_knots": WIND, "thresholds": THRESHOLDS}}, tracks, basin


def _nd(center, sd):
    return NormalDist(mu=center, sigma=max(1.0, sd))


def _interval(bounds, coverage):
    center = mean(bounds)
    width = bounds[1] - bounds[0] + 1
    return center, width / (2 * NormalDist().inv_cdf((1 + coverage) / 2))


def _survival(distribution, threshold):
    floor = distribution.cdf(-0.5)
    return (1 - distribution.cdf(threshold + 0.5)) / (1 - floor)


def _fit(inputs):
    models = {}
    for phase in PHASES:
        fit = [row for row in inputs["outlooks"] if row["phase"] == phase and FIT[0] <= row["year"] <= FIT[1]
               and str(row["year"]) in inputs["counts"]]
        for kind in KINDS:
            if len(fit) < 3:
                continue
            fit_x = [[mean(row["ranges"][kind])] for row in fit]
            fit_y = [inputs["counts"][str(row["year"])][kind] for row in fit]
            reg = Ridge(alpha=5.0).fit(fit_x, fit_y)
            if len(fit) > 3:
                gaps = []
                for index, label in enumerate(fit_y):
                    train_x = [row for cursor, row in enumerate(fit_x) if cursor != index]
                    train_y = [row for cursor, row in enumerate(fit_y) if cursor != index]
                    fitted = Ridge(alpha=5.0).fit(train_x, train_y)
                    gaps.append((label - float(fitted.predict([fit_x[index]])[0])) ** 2)
                calibration = mean(gaps) ** 0.5
            else:
                calibration = 1.0
            models[(phase, kind)] = (reg, calibration, len(fit))
    return models


def _proxies(row, kind, threshold, models, earlier):
    published = row["ranges"][kind]
    center, sd = _interval(published, row["reported_range_probability"])
    prior = (sum(value > threshold for value in earlier) + 0.5) / (1 + len(earlier))
    predictions = {"climatology": prior, "noaa": _survival(_nd(center, sd), threshold)}
    fitted = models.get((row["phase"], kind))
    if fitted:
        reg, scale, _ = fitted
        predictions["calibrated"] = _survival(_nd(float(reg.predict([[center]])[0]), scale), threshold)
    return predictions


def _metrics(inputs, models):
    selected = []
    for row in inputs["outlooks"]:
        year = row["year"]
        if not TEST[0] <= year <= TEST[1] or str(year) not in inputs["counts"]:
            continue
        for kind in KINDS:
            earlier = [value[kind] for item, value in inputs["counts"].items() if int(item) <= FIT[1]]
            actual = inputs["counts"][str(year)][kind]
            for threshold in THRESHOLDS[kind]:
                probs = _proxies(row, kind, threshold, models, earlier)
                selected.append({"season": year, "phase": row["phase"], "kind": kind, "threshold": threshold,
                                 "actual_count": actual, "outcome": int(actual > threshold), "outlook": row,
                                 **probs})
    return selected


def _season_metrics(rows, label):
    selected = []
    for method in MODELS:
        by_year = defaultdict(list)
        for row in rows:
            if method not in row:
                continue
            by_year[row["season"]].append((row["outcome"] - row[method]) ** 2)
        if by_year:
            selected.append({"group": label, "method": method, "label": MODELS[method],
                             "bucket_brier": mean(mean(values) for values in by_year.values()),
                             "bucket_cases": sum(map(len, by_year.values())), "calendar_years": len(by_year),
                             "station_days": 0, "calendar_dates": len({r["outlook"]["issue_day"] for r in rows})})
    return selected


def _contracts(data):
    where = ",".join("?" for _ in SERIES)
    with venue_archive.connect(data) as db:
        return [dict(row) for row in db.execute(
            """SELECT instrument_id,series_id,event_id,opened_at,closed_at,status,rules_primary,rules_secondary,source,
            rules_url,settlement_at,settlement_received_at,settlement_source,yes_payout,expiration_value
            FROM markets WHERE venue=? AND series_id IN (""" + where + ") ORDER BY instrument_id",
            (venue_archive.KALSHI, *SERIES),
        )]


def _contract_terms(contract):
    primary = contract["rules_primary"] or ""
    countdown = re.search(r"records more than (\d+)", primary, re.I)
    dates = re.search(
        r"between (January \d{1,2}, 20\d{2}) and ((?:November|December) \d{1,2}, 20\d{2})",
        primary, re.I,
    )
    if countdown is None or dates is None or "National Hurricane Center" not in primary:
        return None
    first, end = (datetime.strptime(token, "%B %d, %Y").date() for token in dates.groups())
    if first.month != 1 or first.year != end.year:
        return None
    return SERIES[contract["series_id"]], int(countdown[1]), first, end


def _receive_candle(data, get, contract, *, first_at=None):
    ticker = contract["instrument_id"]
    try:
        closed = min(parse_time(contract["closed_at"]), datetime.now(timezone.utc))
        # Only May and August clocks are tested. Some strikes settle earlier.
        opened = max(parse_time(contract["opened_at"]), first_at or closed.replace(month=1, day=1))
        if closed <= opened:
            return 0
        params = {"start_ts": int(opened.timestamp()), "end_ts": int(closed.timestamp()), "period_interval": 1440}
        escaped = quote(ticker, safe="")
        archive = "/historical/markets?" in contract["source"]
        path = f"/historical/markets/{escaped}/candlesticks" if archive else f"/series/{contract['series_id']}/markets/{escaped}/candlesticks"
        reply = get(venue_archive.KALSHI, path, params)
    except EXCEPTIONS as error:
        _scan_error(data, venue_archive.KALSHI, ticker, CANDLE_KIND, "", 0, 0, error)
        return 0

    def parse(payload):
        if payload["ticker"] != ticker:
            raise ValueError("Kalshi candle symbol mismatch")
        return _candle_entries(_rows(payload, "candlesticks"), params["start_ts"], params["end_ts"])

    return _save_history(data, venue_archive.KALSHI, ticker, CANDLE_KIND, reply, parse)


def candles(data):
    get = PublicGET(data)
    retained = 0
    count = 0
    for contract in _contracts(data):
        terms = _contract_terms(contract)
        if terms and TEST[0] <= terms[-1].year <= TEST[1] + 1:
            count += 1
            retained += _receive_candle(data, get, contract)
    return count, retained


def _pre_decision_price(data, contract, decision):
    if not (contract["opened_at"] and contract["closed_at"] and
            parse_time(contract["opened_at"]) <= decision < parse_time(contract["closed_at"])):
        return None
    before, end = iso(decision - timedelta(days=2)), iso(decision)
    with venue_archive.connect(data) as db:
        row = db.execute(
            """SELECT at,receipt_id,body FROM history WHERE venue=? AND instrument_id=? AND kind=? AND at>=? AND at<?
            ORDER BY at DESC LIMIT 1""",
            (venue_archive.KALSHI, contract["instrument_id"], CANDLE_KIND, before, end),
        ).fetchone()
    if row is None:
        return None
    raw = json.loads(row["body"])
    proxy = _probability_proxy(raw, venue_archive.KALSHI)
    return {**proxy, "source_at": row["at"], "receipt_id": row["receipt_id"], "raw": raw} if proxy else None


def _sample_replays(data, inputs, tracks, basin, models):
    records = []
    earlier = {kind: [value[kind] for item, value in inputs["counts"].items() if int(item) <= FIT[1]] for kind in KINDS}
    outlooks = defaultdict(list)
    for row in inputs["outlooks"]:
        outlooks[row["year"]].append(row)
    for contract in _contracts(data):
        if (terms := _contract_terms(contract)) is None:
            continue
        kind, threshold, first, through = terms
        year = first.year
        if year < TEST[0] or year > TEST[1] + 1:
            continue
        measured = (_wind_events(tracks, basin, year, kind, first=first, through=through)
                    if str(year) in inputs["counts"] else None)
        predicted = []
        for outlook in outlooks[year]:
            decision = parse_time(outlook["usable_after_day_bounds"])
            predicted.append({"clock": outlook["usable_after_day_bounds"], "outlook": outlook,
                              "probabilities": _proxies(outlook, kind, threshold, models, earlier[kind]),
                              "display": _pre_decision_price(data, contract, decision)})
        native = contract["yes_payout"]
        outcome = None if measured is None else int(len(measured) > threshold)
        status = {
            True: "Observed threshold and binary payout agree",
            False: "Observed threshold and binary payout differ",
            None: "Final weather or binary payout unavailable",
        }[outcome == int(native) if outcome is not None and native in ("0", "1") else None]
        records.append({"contract": contract, "status": status, "kind": kind, "threshold": threshold,
                        "start_date": str(first), "end_date": str(through), "tracks": measured, "forecast": predicted,
                        "weather_count": len(measured) if measured is not None else None,
                        "weather_result": outcome})
    return records


def _cases(replays):
    cases = []
    symbols = ("HURCTOTMAJ-22NOV30-T2", "KXHURCTOT-25DEC01-T9", "TROPSTORM-23DEC01-T24", "KXHURCTOTMAJ-25DEC01-T7",
               "KXHURCTOT-26DEC01-T9")
    by_id = {row["contract"]["instrument_id"]: row for row in replays}
    for symbol in symbols:
        row = by_id.get(symbol)
        if row is None:
            continue
        payout = row["contract"]["yes_payout"]
        text = row["status"] + "."
        if row["weather_count"] is not None:
            text += f" NHC counted {row['weather_count']} qualifying {row['kind']}."
        if payout is not None:
            text += f" Native YES paid ${payout}."
        cases.append({"title": f"{row['start_date'][:4]} Atlantic {row['kind']} >{row['threshold']}",
                      "venue": venue_archive.KALSHI, "instrument_id": symbol,
                      "date": row["start_date"], "at": row["contract"]["settlement_at"],
                      "detail": text, "data": row})
    return cases


def evaluate(data, inputs, replays):
    data = Path(data)
    frozen, digest = research_model.freeze_inputs(data, inputs)
    models = _fit(inputs)
    rows = _metrics(inputs, models)
    metrics = _season_metrics(rows, "Both NOAA issues")
    for phase in PHASES:
        panel = [row for row in rows if row["phase"] == phase]
        metrics += _season_metrics(panel, phase.title() + " NOAA issue")
        for kind in KINDS:
            metrics += _season_metrics([row for row in panel if row["kind"] == kind], phase.title() + " " + kind)
    counts = Counter(row["status"] for row in replays)
    research = {
        "version": VERSION, "title": "Atlantic storm totals from as-issued NOAA outlooks",
        "hypothesis": "Official as-issued seasonal outlooks improve Atlantic threshold forecasts over earlier observed "
                      "climatology. An earlier-season calibration checks if shrinking NOAA range centers helps.",
        "target": "NHC tropical storms and hurricanes by Atlantic season", "venues": ["Kalshi"],
        "date_window": {"fit seasons": FIT, "test seasons": TEST},
        "supersedes": "atlantic-seasonal-storm-v1-2827737e9b49d270",
        "coverage": {"forecast_issues": len(inputs["outlooks"]), "test_calendar_years": len({r["season"] for r in rows}),
                     "test_bucket_cases": len(rows), "market_instruments": len(replays), "market_results": dict(counts),
                     "display_clock_matches": sum(sum(bool(f["display"]) for f in r["forecast"]) for r in replays)},
        "missing": inputs["gaps"], "metrics": metrics, "cases": _cases(replays),
        "limits": [
            "NOAA's reported ranges carry stated 67% or 70% coverage. The normal-shaped threshold conversion is a model assumption.",
            "Issue stamps are known to the date only. Replay makes an outlook usable beginning the following UTC day.",
            "NHC HURDAT2 and the Atlantic basin season summaries qualify the outcome. These retrospective "
            "records do not provide live input clocks.",
            "Current 2026 weather and native payouts are pending in this complete historical source. "
            "Archived candle probabilities have no executable depth.",
        ],
        "provenance": {"frozen_input": str(frozen.relative_to(data)), "frozen_input_sha256": digest,
                       "cpc_archive": CPC, "best_track_archive": NHC, "best_track_format": HURDAT_DOC,
                       "forecast_centers": {f"{phase}:{kind}": {"slope": model.coef_.tolist(), "intercept": model.intercept_,
                                                              "out_of_year_residual_scale": scale, "seasons": n}
                                            for (phase, kind), (model, scale, n) in models.items()},
                       "scikit_learn": sklearn_version, "source_sha256": research_model.run_sources(sys.modules[__name__])},
    }
    return research_model.write_record(data, research, {"rows": rows, "market_replays": replays})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".data"))
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--candles", action="store_true")
    parser.add_argument("--input", type=Path)
    parser.add_argument("--replay-details", type=Path)
    args = parser.parse_args(argv)
    if args.download:
        fetch(args.data)
    if args.candles:
        instruments, points = candles(args.data)
        print(f"{instruments} cyclone instruments requested, {points} daily candles retained")
    if args.input and args.replay_details:
        prepared = json.loads(gzip.decompress(args.input.read_bytes()))
        replays = json.loads(gzip.decompress(args.replay_details.read_bytes()))["market_replays"]
    else:
        prepared, tracks, basin = _season_rows(args.data)
        models = _fit(prepared)
        replays = _sample_replays(args.data, prepared, tracks, basin, models)
    print(evaluate(args.data, prepared, replays))


if __name__ == "__main__":
    main()
