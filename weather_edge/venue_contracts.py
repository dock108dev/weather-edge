"""Extract searchable weather metadata from native venue records.

Every instrument keeps the original rules. These fields support lookup and
comparison; a structured comparison never approves two-leg settlement.
"""

from datetime import date, datetime, time, timedelta, timezone
import re
from zoneinfo import ZoneInfo

from .market import OPEN, STATIONS, _bucket_bounds, decimal, iso
from .venue_archive import KALSHI, POLY

KALSHI_API = "https://external-api.kalshi.com/trade-api/v2"
POLY_API = "https://gateway.polymarket.us"
STATION_OFFSETS = {
    **{key: value[3] for key, value in STATIONS.items()},
    "KAUS": -6, "KDEN": -7, "KSEA": -8, "KSJC": -8, "KORD": -6, "KDAL": -6,
    "KDFW": -6, "KMSP": -6, "KBOS": -5, "KDCA": -5, "KLGA": -5, "KPIT": -5,
    "KDTW": -5, "KPVD": -5, "KCLT": -5, "KATL": -5, "KSAV": -5, "KRDU": -5,
    "KJAX": -5, "KTPA": -5, "KMCO": -5, "KMSY": -6, "KIAH": -6, "KHOU": -6,
    "KOKC": -6, "KMCI": -6, "KSTL": -6, "KCVG": -5, "KIND": -5, "KCMH": -5,
    "KMKE": -6, "KPHX": -7, "KLAS": -8, "KPDX": -8, "KSAN": -8, "KSMF": -8,
    "KSLC": -7, "KABQ": -7, "KSAT": -6, "KSDF": -5, "KEWR": -5,
    "KTTN": -5, "KCLL": -6, "KONT": -8, "KDJT": -5,
    "KAPC": -8, "KBNA": -6, "KBWI": -5, "KCLE": -5, "KDSM": -6, "KGNV": -5,
    "KGTF": -7, "KICT": -6, "KLEX": -5, "KMSN": -6, "KPNS": -6, "KPWM": -5,
    "KSGF": -6, "KSPG": -5, "KTUS": -7,
}
# These exact CLI product IDs were verified against NWS product/station
# identity or the Weather Company's Nov. 14 dashboard cliId/icao fields.
CLIMATE_CODES = (
    "ABQ APC ATL AUS BNA BOS BWI CLE CLL CLT CMH CVG DCA DEN DFW DJT DSM DTW EWR "
    "GNV GTF HOU IAH ICT IND JAX LAS LAX LEX LGA MCI MCO MDW MIA MKE MSN MSP "
    "MSY NYC OKC ONT ORD PDX PHL PHX PIT PNS PVD PWM SAN SAT SDF SEA SFO SGF "
    "SJC SLC SMF SPG STL TPA TTN TUS"
).split()
CLIMATE_STATIONS = {f"CLI{code}": f"K{code}" for code in CLIMATE_CODES}
LEGACY_CLIMATE_SITES = {
    "austin bergstrom": "KAUS",
    "central park, new york": "KNYC",
    "chicago midway, il": "KMDW",
    "houston hobby": "KHOU",
    "los angeles airport, ca": "KLAX",
    "midway international airport, il": "KMDW",
    "miami international airport": "KMIA",
    "philadelphia international airport": "KPHL",
}
NWS_CITY_CLIMATE_SITES = {"denver, co": "KDEN", "miami, fl": "KMIA", "philadelphia, pa": "KPHL"}
NWS_DAILY_CLIMATE = re.compile(
    r"(?:National Weather Service|NWS)(?:['’]s)? (?:Climatological Report \(Daily\)|Daily Climate Report)", re.I,
)
ATLANTIC_SERIES = {"KXTROPSTORM", "KXHURCTOT", "KXHURCTOTMAJ"}
SKI_OPENING_SERIES = {
    "KXALTAOPEN", "KXALYOPEN", "KXASPENMTNOPEN", "KXBACHELOROPEN", "KXBALDYOPEN", "KXBEAVEROPEN",
    "KXBIGBEAROPEN", "KXBIGSKYOPEN", "KXBRECKOPEN", "KXCOPPEROPEN", "KXDEERVALLEYOPEN", "KXHEAVENLYOPEN",
    "KXJACKSONOPEN", "KXKEYSTONEOPEN", "KXKILLINGTONOPEN", "KXMAMMOTHOPEN", "KXNORTHSTAROPEN", "KXPARKCOPEN",
    "KXSIERRAOPEN", "KXSNOWBASINOPEN", "KXSNOWBIRDOPEN", "KXSTEAMBOATOPEN", "KXSTOWEOPEN", "KXSTRATTONOPEN",
    "KXSUGARBUSHOPEN", "KXWINTERPARKOPEN",
}
INDEX_CITIES = {
    "KXTEMPNYCHS": ("nyc", "New York City", "America/New_York"),
    "KXTEMPCHIHS": ("chicago", "Chicago Metro", "America/Chicago"),
    "KXTEMPMIAH": ("miami", "Miami", "America/New_York"),
    "KXTEMPLAXHS": ("la-coastal", "Los Angeles Coastal", "America/Los_Angeles"),
}
MONTHS = {name.lower(): index for index, name in enumerate((
    "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
), 1)}
LONG_DAY = re.compile(r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?) (\d{1,2})(?:st|nd|rd|th)?, (\d{4})\b", re.I)
NYC_LOCAL_RANGE = re.compile(
    r"\b([A-Za-z]{3,9}) (\d{1,2}) to (?:([A-Za-z]{3,9}) )?(\d{1,2}), (20\d{2}) "
    r"\(inclusive, America/New_York local calendar dates\)", re.I,
)
ISO_DAY = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")
KALSHI_DAY = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})(?:[-A-Z0-9]|$)")
KALSHI_MONTH = re.compile(r"-(\d{2})([A-Z]{3})$")
MONTH_RULE = re.compile(r"\bin ([A-Za-z]{3,9}) (20\d{2})\b", re.I)
DROUGHT_RULE = re.compile(r"maximum drought monitor category in (.+?) during ([A-Za-z]+) (\d{1,2})[–-]"
                          r"([A-Za-z]+) (\d{1,2}), (20\d{2}) is at least D([0-4])")
ENSO_PERIODS = ("DJF", "JFM", "FMA", "MAM", "AMJ", "MJJ", "JJA", "JAS", "ASO", "SON", "OND", "NDJ")
ENSO_PERIOD = re.compile(r"\b(" + "|".join(ENSO_PERIODS) + r") (20\d{2})(?:-\d{2})? season\b")
GISS_YEARS = re.compile(r"\b(?:in|for) (20\d{2})\b")
GISS_RANK = re.compile(r"^trc-yr-httst-rnk-glbl-(20\d{2})-(1st|2nd|3rd|4th|lte5th)$")
GISS_SOURCE = "NASA GISS unsmoothed Land–Ocean Temperature Index"
GISS_FIXED = re.compile(r"\b(\d\.\d{2}) degrees Celsius")
GISS_REFERENCE = re.compile(r"\b(20\d{2}) value\b")
TEMP_RULE = re.compile(
    r"(?:highest|lowest) temperature recorded at (.*?) \(([A-Z]{4})\).*? for (\d{4}-\d{2}-\d{2}) "
    r"as reported by the National Weather Service's Climatological Report \(Daily\) be (.+?)\?",
    re.I,
)


def _unit(rule, family):
    if re.search(r"\bcelsius\b|°\s*C\b", rule, re.I):
        return "°C"
    if re.search(r"\bfahrenheit\b|\d+\s*°?\s*F\b", rule, re.I):
        return "°F"
    if re.search(r"\binch(?:es)?\b|PRECIPITATION \(IN\)", rule, re.I):
        return "in"
    if re.search(r"\bmillimeters?\b|[\d.]+\s*mm\b", rule, re.I):
        return "mm"
    if family in ("annual-temperature-rank", "storm-count", "snow-days"):
        return "count"
    return None


def _family(title, slug="", frequency="", tags=()):
    text = title.lower()
    slug = slug.lower()
    daily = frequency == "daily" or "daily" in text
    if re.search(r"high-(?:\d{4}-|20\d{2})", slug) or (daily and re.search(r"\b(?:high(?:est)?|max(?:imum)?)\b", text) and ("temp" in text or "warm" in text)):
        return "daily-high"
    if re.search(r"low-(?:\d{4}-|20\d{2})", slug) or (daily and re.search(r"\b(?:low(?:est)?|min(?:imum)?)\b", text) and ("temp" in text or "cold" in text)):
        return "daily-low"
    if "rain-daily" in slug or daily and ("rain" in text or "precipitation" in text):
        return "daily-rain"
    if frequency == "hourly" and any(word in text for word in ("temperature", "heat", "wind")):
        return "hourly-weather"
    if "rank" in text or slug.startswith("yr-httst-rnk"):
        return "annual-temperature-rank" if "temp" in text or "hot" in text or slug.startswith("yr-httst-rnk") else "other-weather"
    if "snow-day" in text:
        return "snow-days"
    if "snow" in text or "Snow" in tags:
        return "snow"
    if "rain" in text or "precipitation" in text or "Rain" in tags:
        return "rain"
    if any(word in text for word in ("hurricane", "tropical storm", "cyclone", "typhoon", "named storm")) or "Hurricanes" in tags:
        return "storm-count" if any(word in text for word in ("count", "how many", "number of", "named")) else "storm"
    if "temperature" in text or slug.startswith("temp-") or "heatwave" in text:
        return "period-temperature"
    return "other-weather"


def poly_family(event):
    if event.get("seriesSlug") == "rain-daily":
        return "daily-rain"
    return _family(event.get("title", ""), event.get("slug", ""))


def kalshi_family(series):
    exact_series = {
        "KXLOWTPERIOD": "period-low", "KXCITIESWEATHER": "multi-station-high", "KXHIGHUS": "national-daily-high",
        "KXRAINWKND": "weekend-rain", "KXRAINTHANKSGIVING": "holiday-rain", "KXRAINHOLIDAY": "holiday-rain",
        "KXRAINELECTION": "election-rain", "KXHOLIDAYTMAX": "daily-high", "KXHOLIDAYTMIN": "daily-low",
        "KXRAINNAPAM": "period-rain", "KXRAINGTF": "period-rain", "KXRAINSNYC": "period-rain",
        "KXRAINSBOS": "period-rain", "KXRAINNYCW": "period-rain",
        "KXTXURI": "regional-cold-spell",
        "KXHURCITYTPA": "storm-landfall", "KXHURRLANDFL4": "storm-landfall", "KXHURRLANDNE3": "storm-landfall",
        "KXHURPATHAL": "storm-path", "KXDROUGHTLEVEL": "drought-severity", "KXMICHTEMP": "lake-surface-temperature",
        "KXAQICITY": "air-quality",
        "KXUSCLIMATE": "annual-emissions", "KXEUCLIMATE": "annual-emissions",
    }
    if series.get("ticker") in exact_series:
        return exact_series[series["ticker"]]
    if series.get("ticker") in SKI_OPENING_SERIES:
        return "ski-opening"
    terms = (series.get("contract_terms_url") or "").rsplit("/", 1)[-1]
    if terms in ("RAINM.pdf", "RAINNYCM.pdf"):
        return "monthly-rain"
    if terms == "ELNINOSTRENGTH.pdf":
        return "enso"
    if terms in ("GLBTEMP.pdf", "HOTYEAR.pdf"):
        return "annual-temperature"
    if terms == "HDDAREA.pdf":
        return "heating-degree-days"
    if terms == "TORNADO.pdf":
        return "tornado-count"
    if terms == "HMONTHRANGE.pdf":
        return "monthly-global-temperature"
    if terms == "ARCTICICE.pdf":
        return "sea-ice-extent"
    if terms == "MEAD.pdf":
        return "reservoir-elevation"
    if terms == "HURCAT.pdf":
        return "storm-intensity"
    if terms == "CO2LEVEL.pdf":
        return "atmospheric-co2"
    return _family(series.get("title", ""), series.get("ticker", ""), series.get("frequency", ""),
                   series.get("tags") or ())


def _long_day(matched):
    return date(int(matched[3]), MONTHS[matched[1][:3].lower()], int(matched[2]))


def _rule_day(rule, symbol, family):
    monthly = ("monthly-rain", "tornado-count", "monthly-global-temperature")
    if not (family.startswith("daily-") or family in ("snow", "storm-count", "hourly-weather", "multi-station-high",
                                                      "national-daily-high", *monthly)):
        return None, None
    match = ISO_DAY.search(rule)
    if match:
        return match[1], "contract rule"
    match = LONG_DAY.search(rule)
    if match:
        event_day = _long_day(match).isoformat()
        return event_day, "contract period start" if family == "storm-count" else "contract rule"
    if family in ("snow", *monthly) and (period := MONTH_RULE.search(rule)) and (month := MONTHS.get(period[1][:3].lower())):
        return date(int(period[2]), month, 1).isoformat(), "contract period start"
    if family in monthly and (period := KALSHI_MONTH.search(symbol)) and (month := MONTHS.get(period[2].lower())):
        return date(2000 + int(period[1]), month, 1).isoformat(), "contract period start from event ticker"
    match = ISO_DAY.search(symbol)
    if match:
        return match[1], "event slug"
    match = KALSHI_DAY.search(symbol)
    if match and match[2].lower() in MONTHS:
        return date(2000 + int(match[1]), MONTHS[match[2].lower()], int(match[3])).isoformat(), "event ticker"
    return None, None


def _bounds(rule, family):
    if family not in ("daily-high", "daily-low"):
        return {}
    match = TEMP_RULE.search(rule)
    if not match:
        return {}
    try:
        low, high = _bucket_bounds(match[4])
    except ValueError:
        return {}
    operator = "at-most" if low is None else "at-least" if high is None else "exactly" if low == high else "between-inclusive"
    return {"lower": low, "upper": high, "operator": operator}


def _window(station, day, offset):
    if offset is None or not day or not station:
        return {}
    start = datetime.combine(date.fromisoformat(day), time.min, tzinfo=timezone(timedelta(hours=offset)))
    return {"climate_starts_at": iso(start), "climate_ends_at": iso(start + timedelta(days=1))}


def _precipitation_dates(primary, secondary):
    """Return the rule's inclusive reporting dates, independently of trading."""
    for rule in (primary, secondary):
        dates = list(LONG_DAY.finditer(rule))
        if len(dates) == 2 and re.fullmatch(r"\s+(?:through|to)\s+", rule[dates[0].end():dates[1].start()], re.I):
            first, last = map(_long_day, dates)
            return (first, last) if first <= last else (None, None)
    if " on " in primary and (one := LONG_DAY.search(primary)):
        first = _long_day(one)
        return first, first
    return None, None


def _monthly_rain_terms(primary, secondary, station, day, terms, has_twc):
    kwargs = {}
    if station and day and station in STATION_OFFSETS:
        first = date.fromisoformat(day)
        last = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
        offset = timezone(timedelta(hours=STATION_OFFSETS[station]))
        kwargs.update(climate_starts_at=iso(datetime.combine(first, time.min, offset)),
                      climate_ends_at=iso(datetime.combine(last, time.min, offset)))
    if has_twc:
        kwargs["report_policy"] = "TWC Daily report; amended material-error terms" if "amended" in secondary.lower() else "TWC Daily report"
    elif "Climate Data Online Daily Summaries" in secondary:
        kwargs.update(resolution_source="NOAA NCEI Daily Summaries",
                      report_policy="Sum of NOAA Climate Data Online daily precipitation",
                      fallback="NWS Monthly Climate Report, then sum of Daily Climate Reports")
    elif terms == "RAINNYCM.pdf":
        kwargs.update(resolution_source="NWS Daily Climate Report",
                      report_policy="Sum of official daily amounts; material-error review; can settle early",
                      fallback="The Weather Company; last fair price if reliable data unavailable")
    elif "latest version" in secondary.lower():
        kwargs.update(resolution_source="NWS Daily Climate Report",
                      report_policy="Instrument directs readers to the latest daily report")
    elif "Daily Climate Report" in secondary or "total precipitation" in primary:
        kwargs.update(resolution_source="NWS Daily Climate Report",
                      report_policy="First complete monthly report; later revisions excluded under series terms",
                      fallback="Reported trace and missing daily values count zero under series terms")
    return kwargs


def _explicit_utc_window(rule, day):
    match = re.search(
        r"\b([0-2][0-9]):([0-5][0-9]) UTC that date to ([0-2][0-9]):([0-5][0-9]) UTC the following date",
        rule, re.I,
    )
    if not match or not day:
        return {}
    current = date.fromisoformat(day)
    start = datetime.combine(current, time(int(match[1]), int(match[2])), timezone.utc)
    end = datetime.combine(current + timedelta(days=1), time(int(match[3]), int(match[4])), timezone.utc)
    return {"climate_starts_at": iso(start), "climate_ends_at": iso(end)}


def _enso_window(rule):
    period = ENSO_PERIOD.search(rule)
    if not period:
        return None, {}
    month_of_center = ENSO_PERIODS.index(period[1]) + 1
    year_of_center = int(period[2]) + int(period[1] == "DJF" and "-" in period[0])
    first_month = month_of_center - 1 or 12
    start = datetime(year_of_center - int(month_of_center == 1), first_month, 1, tzinfo=timezone.utc)
    end = datetime(start.year + (start.month + 2) // 12, (start.month + 2) % 12 + 1, 1, tzinfo=timezone.utc)
    return start.date().isoformat(), {"climate_starts_at": iso(start), "climate_ends_at": iso(end)}


def _annual_window(year):
    start = datetime(year, 1, 1, tzinfo=timezone.utc)
    end = start.replace(year=year + 1)
    return start.date().isoformat(), "calendar year start", {"climate_starts_at": iso(start), "climate_ends_at": iso(end)}


def _field(value):
    return str(value) if value is not None and not isinstance(value, bool) else None


def native_id(value):
    if not _field(value) or not str(value).strip() or not isinstance(value, (str, int)):
        raise ValueError("Missing native identity")
    return str(value)


def kalshi_ticker_matches_event(ticker, event):
    return isinstance(ticker, str) and isinstance(event, str) and (ticker == event or ticker.startswith(event + "-"))


def _time(value):
    if not value:
        return None
    try:
        return iso(value)
    except (ValueError, TypeError, AttributeError):
        return None


def _status_poly(market):
    native = market.get("status") or market.get("ep3Status")
    if native in OPEN:
        return "open"
    if native in ("MARKET_STATUS_RESOLVED", "MARKET_STATUS_SETTLED", "RESOLVED", "SETTLED"):
        return "settled"
    if market.get("closed") or native in ("MARKET_STATUS_CLOSED", "MARKET_STATUS_EXPIRED", "CLOSED", "EXPIRED"):
        return "closed"
    return "upcoming" if native in ("MARKET_STATUS_PENDING", "MARKET_STATUS_INACTIVE", "INACTIVE") else "unknown"


def poly_event(event):
    event_id = native_id(event.get("slug"))
    series_id = event.get("seriesSlug") or event.get("category") or "weather"
    title = event.get("title") or event_id
    return event_id, series_id, title, poly_family(event)


def poly_market(event, market, source):
    event_id, series_id, title, family = poly_event(event)
    market_id = native_id(market.get("id"))
    slug = native_id(market.get("slug"))
    rule = market.get("description") or ""
    sides = {}
    for side in market.get("marketSides") or []:
        long = side.get("long")
        if type(long) is not bool or str(side.get("description", "")).lower() != ("yes" if long else "no"):
            continue
        if _field(side.get("marketId")) == market_id:
            sides[long] = _field(side.get("id"))
    day, basis = _rule_day(rule, event_id, family)
    temp = TEMP_RULE.search(rule)
    station_match = re.search(r"\(([A-Z]{4})\)", rule)
    station = temp[2].upper() if temp else station_match[1] if station_match else None
    if station in {"NASA", "NOAA", "NHCX"}:
        station = None
    kwargs = {}
    units = _unit(rule, family)
    if temp:
        region = temp[1]
        kwargs.update(_bounds(rule, family))
        kwargs.update(_window(station, day, STATION_OFFSETS.get(station)))
        kwargs.update(resolution_source="NWS Daily Climate Report", report_policy="Contract-specific NWS climate report")
    elif family == "daily-rain" and "present-weather group" in rule and "aviationweather.gov" in rule:
        region = market.get("title")
        units = "METAR present-weather code"
        kwargs.update(operator="observed-rain-code", resolution_source="Aviation Weather Center METAR/SPECI",
                      report_policy="Latest correction through first observation on the next climate day",
                      fallback="NWS Daily Climate Report weather conditions; last fair price if both sources unavailable after delay")
        kwargs.update(_window(station, day, STATION_OFFSETS.get(station)) if "local standard time" in rule.lower() else {})
    elif family == "daily-rain" and re.search(r"(?:0\.01 inches or more|(?:at least|of) 0\.01 inches)", rule, re.I):
        region = market.get("title")
        kwargs.update(lower="0.01", operator="at-least",
                      resolution_source="NWS Daily Climate Report", report_policy="Latest complete by 2 p.m. ET; first later report if none",
                      fallback="Last fair market price after reporting deadline; missing or trace precipitation resolves NO")
        kwargs.update(_window(station, day, STATION_OFFSETS.get(station)) if "local standard time" in rule.lower() else {})
    elif family == "annual-temperature-rank" and (ordered := GISS_RANK.fullmatch(slug)) and "No_Smoothing" in rule:
        day, basis, kwargs = _annual_window(int(ordered[1]))
        rank = ordered[2]
        region, units = "Global", "rank"
        kwargs.update(operator="at-least" if rank == "lte5th" else "exactly",
                      lower="5" if rank == "lte5th" else rank[0], upper=None if rank == "lte5th" else rank[0],
                      resolution_source=GISS_SOURCE, report_policy="First published table; tied rank is counted")
    elif slug == "tc-temp-sjc-dec-lt40" and all(term in rule for term in (
            "San Jose (SJC Airport)", "strictly less than 40 degrees Fahrenheit", "any day in December 2026", "Climatological Report (Daily)")):
        station, region, units = "KSJC", "San Jose, California", "°F"
        day, basis = "2026-12-01", "December 2026 station climate days in primary rule"
        kwargs.update(operator="below", upper="40", resolution_source="NWS Daily Climate Report",
                      report_policy="Any complete station day with a reported minimum below 40°F",
                      climate_starts_at="2026-12-01T08:00:00Z", climate_ends_at="2027-01-01T08:00:00Z")
    else:
        region = market.get("title") if family in ("daily-rain", "storm", "storm-count", "snow") else None
    fee = market.get("feeCoefficient")
    return {
        "venue": POLY, "instrument_id": slug, "event_id": event_id, "series_id": series_id,
        "native_id": market_id, "native_event_id": native_id(event.get("id")),
        "title": market.get("question") or title, "event_title": title, "label": market.get("title") or slug,
        "family": family, "station": station, "region": region, "day": day, "day_basis": basis,
        "native_status": market.get("status") or market.get("ep3Status"), "status": _status_poly(market),
        "rules_primary": rule or None, "rules_secondary": market.get("resolutionCriteria"),
        "rules_url": POLY_API + "/v1/events/slug/" + event_id,
        "source": source, "units": units, "yes_side_id": sides.get(True),
        "no_side_id": sides.get(False), "quantity_step": _field(market.get("minimumTradeQty")),
        "opened_at": _time(market.get("startDate")), "closed_at": _time(market.get("endDate")),
        "updated_at": _time(market.get("updatedAt")), "fee_type": "quadratic" if fee is not None else None,
        "fee_multiplier": _field(fee), "yes_bid": (market.get("bestBidQuote") or {}).get("value"),
        "yes_ask": (market.get("bestAskQuote") or {}).get("value"), **kwargs,
    }


def _status_kalshi(native):
    return {
        "initialized": "upcoming", "inactive": "suspended", "active": "open", "open": "open",
        "closed": "closed", "determined": "closed", "disputed": "closed", "amended": "closed",
        "finalized": "settled", "settled": "settled",
    }.get(native, "unknown")


def _payout(value):
    yes = decimal(value)
    if 0 <= yes <= 1:
        return {"yes_payout": str(yes.normalize()), "no_payout": str((1 - yes).normalize())}
    raise ValueError("Settlement outside [0, 1]")


def temperature_window(row):
    """Translate a verified operator to an inclusive whole-degree weather interval."""
    if row["units"] != "°F" or row["operator"] not in {
            "below", "above", "at-most", "at-least", "between-inclusive", "exactly"}:
        return None
    bounds = [decimal(value) if value is not None else None for value in (row["lower"], row["upper"])]
    if any(value is not None and value != value.to_integral_value() for value in bounds):
        return None
    low, high = (int(value) if value is not None else None for value in bounds)
    if row["operator"] == "below" and high is not None:
        high -= 1
    if row["operator"] == "above" and low is not None:
        low += 1
    return (low, high) if (low is not None or high is not None) and (low is None or high is None or low <= high) else None


def kalshi_market(series, market, source, received_at, event=None):
    ticker = native_id(market.get("ticker"))
    event_id = native_id(market.get("event_ticker"))
    series_id = native_id(series.get("ticker"))
    # Some genuine historical instrument tickers differ from their event
    # prefixes. Accept those only with the separate native event record.
    if not event and not kalshi_ticker_matches_event(ticker, event_id):
        raise ValueError("Unexpected event and market identity relationship")
    if event and (event.get("series_ticker") != series_id or event.get("event_ticker") != event_id):
        raise ValueError("Unexpected nested series or event identity")
    primary = market.get("rules_primary") or ""
    secondary = market.get("rules_secondary") or ""
    label = market.get("yes_sub_title")
    family = kalshi_family(series)
    day, basis = _rule_day(primary, event_id, family)
    strike = market.get("custom_strike") or {}
    region = strike.get("City") or strike.get("Location") or strike.get("Region")
    station = None
    location = re.search(r"(?:recorded\s+(?:at|in)|precipitation at|snowfall at) (.*?) \(([A-Z]{4,6})\)", primary)
    if location:
        region = region or location[1]
        station = CLIMATE_STATIONS.get(location[2])
        if station is None and len(location[2]) == 4:
            station = location[2]
    if not station and family in ("daily-high", "daily-low"):
        airport = re.search(r"temperature recorded at ([A-Z]{4}) in (.*?) for ", primary)
        if airport:
            station, place = airport.groups()
            region = region or place
    nws_cli = NWS_DAILY_CLIMATE.search(primary)
    if "The Weather Company" not in primary and not nws_cli:
        nws_cli = NWS_DAILY_CLIMATE.search(secondary)
    if not station and family in ("daily-high", "daily-low", "daily-rain"):
        description = re.search(r"(?:temperature|precipitation) recorded (?:at|in) (.*?) (?:for|on) ", primary, re.I)
        if description:
            name = description[1].rstrip(",").lower()
            station = LEGACY_CLIMATE_SITES.get(name) or (NWS_CITY_CLIMATE_SITES.get(name) if nws_cli else None)
            region = region or description[1].rstrip(",")
    if not station and (family.startswith("daily-") or family in ("snow", "monthly-rain")):
        code = re.search(r"\bCLI[A-Z]{3}\b", primary + "\n" + secondary)
        station = CLIMATE_STATIONS.get(code[0]) if code else None
    if family == "snow" and not region:
        named = re.search(r"snowfall in (.+?) in [A-Za-z]{3,9} 20\d{2}", primary, re.I)
        region = named[1] if named else None
    if family == "snow" and re.search(r"\bChicago \(O[’']Hare\)", primary, re.I):
        station = "KORD"
    if series_id in ATLANTIC_SERIES and "National Hurricane Center" in primary:
        region = "North Atlantic"
    if family == "national-daily-high":
        region = "Contiguous United States"
    kwargs = _explicit_utc_window(primary, day)
    units = _unit(primary, family)
    if nws_cli and station in STATION_OFFSETS and family in ("daily-high", "daily-low", "daily-rain"):
        units = units or ("°F" if family in ("daily-high", "daily-low") else "in")
        kwargs.update(_window(station, day, STATION_OFFSETS[station]))
    if re.search(r"local standard time", primary + secondary, re.I):
        kwargs.update(_window(station, day, STATION_OFFSETS.get(station)))
    if re.search(r"The Weather Company", primary, re.I) or re.search(r"(?:corresponding )?TWC Report|weather\.com/kalshi", secondary, re.I):
        kwargs.update(
            resolution_source="The Weather Company",
            report_policy=("First official non-preliminary report; material-error revisions"
                           if "material error" in secondary else "Latest applicable report" if "latest version" in secondary
                           else "Initial non-preliminary official values; contract contingencies"
                           if "initial non-preliminary" in secondary else None),
            fallback="Last fair price determined by Kalshi" if "last fair price" in secondary.lower() else None,
        )
    elif re.search(r"(?:National Weather Service|weather\.gov|\bNWS\b)", primary + secondary, re.I):
        kwargs["resolution_source"] = ("NWS Daily Climate Report"
                                       if nws_cli or "Climate Report" in primary + secondary else "National Weather Service")
        if "latest version" in secondary:
            kwargs["report_policy"] = "Latest applicable report"
    elif "National Hurricane Center" in primary:
        kwargs["resolution_source"] = "NOAA National Hurricane Center"
    if family in ("weekend-rain", "holiday-rain", "period-rain", "election-rain"):
        named = re.search(r"precipitation at (.+?) in (.+?) (?:on|in) ", primary, re.I)
        if named:
            code, place = named.groups()
            station = station or CLIMATE_STATIONS.get(code) or ("KCLL" if code == "CLL" and place == "College Station"
                       else code if code in STATION_OFFSETS else None)
            if parsed_airport := re.search(r"\(([A-Z]{4}); [A-Z]{3} on the TWC daily dashboard\)", code):
                station = station or parsed_airport[1]
            if code == "NYC" and place == "New York City":
                station = station or "KNYC"
            region = region or place
        first, last = _precipitation_dates(primary, secondary)
        if first is None and series_id in ("KXRAINHOLIDAY", "KXRAINELECTION") and (on_day := LONG_DAY.search(primary)):
            first = last = _long_day(on_day)
        if first:
            day = str(first)
            basis = (f"Source reporting date {first}" if first == last else f"Source reporting dates {first} through {last} inclusive")
            if station == "KGTF" and "Mountain Standard Time (MST, UTC−7)" in secondary:
                kwargs["climate_starts_at"] = _window(station, str(first), -7)["climate_starts_at"]
                kwargs["climate_ends_at"] = _window(station, str(last), -7)["climate_ends_at"]
        has_period_total = "publishes a total for that exact period" in secondary
        policy = ("Direct period total where published, otherwise sum of complete daily amounts" if has_period_total
                  else "Any source reporting day with measurable precipitation" if family == "weekend-rain"
                  else "Measurable precipitation on the specified source reporting day")
        kwargs.update(resolution_source="The Weather Company", report_policy=policy + "; initial non-preliminary values and material-error review",
                      fallback="Trace and missing daily values count zero; last fair price if reliable data unavailable")
    if series_id == "KXRAINNYC" and station == "KNYC" and "RAINNYC.pdf" in (series.get("contract_terms_url") or ""):
        kwargs.update(_window(station, day, STATION_OFFSETS[station]))
        kwargs.update(resolution_source="NWS Daily Climate Report", report_policy="Applicable Central Park report before instrument expiration",
                      fallback="Instrument-specific NWS hourly fallback when documented")
    if family == "national-daily-high":
        units = "°F"
        kwargs.update(resolution_source="NWS Weather Prediction Center national high and low temperature",
                      report_policy="Daily high across vetted locations in the contiguous United States. "
                                    "Each location uses its own local calendar day. Revisions after expiration excluded.")
    if family == "period-low" and (window := NYC_LOCAL_RANGE.search(primary)):
        first_month, first_day, last_month, last_day, year = window.groups()
        first_month = MONTHS.get(first_month[:3].lower())
        last_month = MONTHS.get(last_month[:3].lower()) if last_month else first_month
        if first_month and last_month:
            first = date(int(year) - (first_month > last_month), first_month, int(first_day))
            last = date(int(year), last_month, int(last_day))
            zone = ZoneInfo("America/New_York")
            day, basis = first.isoformat(), "inclusive Eastern local calendar period start"
            kwargs.update(climate_starts_at=iso(datetime.combine(first, time.min, zone)),
                          climate_ends_at=iso(datetime.combine(last + timedelta(days=1), time.min, zone)))
            if "official daily minimum temperatures" in secondary and kwargs.get("resolution_source") == "The Weather Company":
                units = "°F"
                kwargs["report_policy"] = ("Minimum of the official daily minima for every local date. Initial non-preliminary release "
                                           "with material-error review and no substitute from hourly or incomplete daily data.")
    if family == "monthly-rain":
        if re.search(r"\bat Central Park, New York City\b", primary, re.I):
            station, region = "KNYC", region or "New York City"
        elif "Chicago Midway, IL" in primary:
            station, region = "KMDW", region or "Chicago Midway"
        if not region:
            named = re.search(r"\bCLI[A-Z]{3} in (.*?) in [A-Za-z]{3,9} 20\d{2}", primary)
            region = named[1] if named else None
        terms = (series.get("contract_terms_url") or "").rsplit("/", 1)[-1]
        kwargs.update(_monthly_rain_terms(primary, secondary, station, day, terms,
                                           kwargs.get("resolution_source") == "The Weather Company"))
    if family == "snow" and station == "KORD" and "local calendar day" in secondary and MONTH_RULE.search(primary) and day:
        first = date.fromisoformat(day)
        last = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
        zone = ZoneInfo("America/Chicago")
        kwargs.update(climate_starts_at=iso(datetime.combine(first, time.min, zone)),
                      climate_ends_at=iso(datetime.combine(last, time.min, zone)),
                      report_policy="Sum all initial non-preliminary daily snowfall values for the named station and month. "
                                    "Material corrections, trace and missing-report provisions follow the filed terms.")
    if family == "enso":
        day, window = _enso_window(primary)
        basis = "three-month RONI period start" if day else None
        region = "Equatorial Pacific · Niño 3.4"
        kwargs.update(window, resolution_source="NOAA Climate Prediction Center RONI",
                      report_policy="Final three-month RONI at published precision; post-expiration revisions excluded",
                      fallback="If unavailable after 72 hours: NOAA NCEI, NOAA PSL, JMA, WMO")
    if family == "annual-temperature" and (target := GISS_YEARS.search(primary)):
        day, basis, window = _annual_window(int(target[1]))
        region, units = "Global", "°C"
        kwargs.update(window, resolution_source=GISS_SOURCE,
                      report_policy="Applicable table at expiration; post-expiration revisions excluded")
    if family == "heating-degree-days":
        window = list(LONG_DAY.finditer(primary))
        location = re.search(r"at (.*?) \(station identifier ([A-Z]{4}), WMO identifier \d+\)", primary)
        if location:
            region, station = location.groups()
        if len(window) == 2 and "America/New_York" in secondary:
            first, last = (_long_day(match) for match in window)
            tz = ZoneInfo("America/New_York")
            day, basis = first.isoformat(), "local calendar accumulation period start"
            kwargs.update(climate_starts_at=iso(datetime.combine(first, time.min, tz)),
                          climate_ends_at=iso(datetime.combine(last + timedelta(days=1), time.min, tz)))
        units = "HDD (65°F)"
        if "HDD (65)" in secondary and "The Weather Company" in secondary:
            kwargs.update(resolution_source="The Weather Company daily HDD (65)",
                          report_policy="Sum each final local calendar day; no warmer-day offset",
                          fallback="The Weather Company final hourly mean, then NWS by contract terms")
    if family == "tornado-count":
        region, units = "United States", "count"
        kwargs.update(resolution_source="NOAA Storm Prediction Center preliminary report map legend",
                      report_policy="Preliminary total at expiration; later revisions excluded",
                      fallback="Market Outcome Review can designate another source after material disruption")
    if family == "monthly-global-temperature":
        region, units = "Global", "°C"
        if day and (period := date.fromisoformat(day)):
            end = (period.replace(day=28) + timedelta(days=4)).replace(day=1)
            kwargs.update(climate_starts_at=iso(datetime.combine(period, time.min, timezone.utc)),
                          climate_ends_at=iso(datetime.combine(end, time.min, timezone.utc)))
        kwargs.update(resolution_source="NOAA Climate at a Glance global land and ocean",
                      report_policy="Calendar-month anomaly relative to 1901–2000 reference. Revisions after expiration excluded.")
    ice_terms = None
    if family == "sea-ice-extent":
        region, units = "Arctic", "million km²"
        periods = list(LONG_DAY.finditer(primary))
        if len(periods) == 2:
            first, last = (_long_day(match) for match in periods)
            day, basis = first.isoformat(), "inclusive NSIDC observation-date range"
            kwargs["report_policy"] = f"Daily observations labeled {first.isoformat()} through {last.isoformat()}, inclusive."
        kwargs["resolution_source"] = ("NOAA/NSIDC Sea Ice Index v4 daily Northern Hemisphere extent"
                                       if "Sea Ice Index Version 4" in secondary else "NSIDC daily Northern Hemisphere sea-ice extent")
        if "JAXA" in secondary:
            kwargs["fallback"] = "NSIDC replacement dataset if available by expiration, otherwise JAXA under Rule 7.2."
        ice_terms = re.search(r"\bis (above|below) (\d+(?:\.\d+)?) million square kilometers\b", primary, re.I)
    if family == "reservoir-elevation" and "Lake Mead" in primary:
        region, units = "Lake Mead at Hoover Dam", "ft"
        month = re.search(r"\b([A-Z][a-z]{2,8}) (20\d{2})\b", primary)
        if month and (number := MONTHS.get(month[1][:3].lower())):
            period = date(int(month[2]), number, 1)
            day, basis = period.isoformat(), "target month from primary rule"
        kwargs.update(resolution_source="Bureau of Reclamation month-end elevation at Hoover Dam",
                      report_policy="Month-end table value. Revisions after expiration excluded.",
                      fallback="Bureau of Reclamation last-day elevation table. If no data is available the contract resolves No.")
    if family == "storm-intensity":
        units = "mph"
        if ending := LONG_DAY.search(primary) or LONG_DAY.search(secondary):
            day, basis = _long_day(ending).isoformat(), "stated storm observation deadline"
        if "National Hurricane Center" not in primary:
            kwargs.update(resolution_source="The Weather Company per HURCAT filing",
                          fallback="NOAA National Hurricane Center under filed hierarchy. Last fair price if reliable data unavailable.")
        kwargs["report_policy"] = "Maximum sustained wind during the contract period. Initial non-preliminary agency values and material-error review."
    if family == "storm-landfall" and (category := re.search(r"\bCategory (\d)( or higher)? hurricane makes landfall in", primary)):
        dates = list(LONG_DAY.finditer(primary))
        if len(dates) == 2:
            first, last = map(_long_day, dates)
            day, basis = str(last), f"Landfall window references {first} through {last}"
        region = {"KXHURCITYTPA": "Municipal coastline of Tampa, Florida",
                  "KXHURRLANDFL4": "Florida excluding the Florida Keys",
                  "KXHURRLANDNE3": "Northeast U.S. qualifying state coastlines"}[series_id]
        units = "Saffir–Simpson category"
        kwargs.update(resolution_source="NOAA National Hurricane Center initial operational landfall report",
                      report_policy="Storm center crosses the named coastline at the stated category. Material-error corrections may apply.")
    if family == "storm-path" and "Isaias" in primary and "defined Alabama boundary" in primary:
        if deadline := LONG_DAY.search(primary):
            day, basis = str(_long_day(deadline)), "Exclusive Eastern Standard Time cutoff in primary rule"
        region, units = "Alabama 2025 Census cartographic boundary", "mph"
        kwargs.update(resolution_source="NOAA National Hurricane Center operational public or forecast advisory",
                      report_policy="Consecutive advisory centers at most 12 hours apart, each at least 74 mph, intersect the specified boundary.")
    drought = DROUGHT_RULE.search(primary) if family == "drought-severity" else None
    if drought:
        region, first_month, first_day, last_month, last_day, year, _ = drought.groups()
        first_number, last_number = MONTHS[first_month[:3].lower()], MONTHS[last_month[:3].lower()]
        first = date(int(year) - (first_number > last_number), first_number, int(first_day))
        last = date(int(year), last_number, int(last_day))
        units, day = "USDM category", str(first)
        basis = f"USDM reporting dates {first} through {last} inclusive"
        kwargs.update(resolution_source="United States Drought Monitor (USDM)",
                      report_policy="Highest category over any part of the named geography during the reporting period. Revisions after expiration excluded.",
                      fallback="National Drought Mitigation Center, then USDA, then NOAA")
    if family == "lake-surface-temperature" and "average surface water temperature on Lake Michigan" in primary:
        if target := LONG_DAY.search(primary):
            day, basis = str(_long_day(target)), "GLSEA source date in primary rule"
        region, units = "Lake Michigan", "°C"
        kwargs.update(resolution_source="NOAA Great Lakes Surface Environmental Analysis (GLSEA)",
                      report_policy="Daily lake-wide Michigan average at expiration. Revisions after expiration excluded.")
    air_quality = re.search(r"AQI Index above (\d+) in New York City occurs in New York City ([A-Za-z]+) (\d+), (20\d{2}) at (\d+pm EDT)", primary) if family == "air-quality" else None
    if air_quality:
        region, units = "New York City Region", "AQI"
        day = str(date(int(air_quality[4]), MONTHS[air_quality[2][:3].lower()], int(air_quality[3])))
        basis = f"AirNow hourly observation dated {day} at {air_quality[5]}"
        kwargs.update(resolution_source="AirNow New York City Region",
                      report_policy="First official report confirming the exact hourly threshold. Post-expiration reports excluded.",
                      fallback="NO if qualifying official data remain unavailable at latest expiration")
    emissions = re.search(r"has (\d+(?:\.\d+)?) million metric tonnes of CO2 emissions or fewer in a year by (20\d{2})", primary) if series_id == "KXUSCLIMATE" else None
    reduction = re.search(r"reduced greenhouse gas emissions by (\d+)% compared to the (\d{4}) level by (20\d{2})", primary) if series_id == "KXEUCLIMATE" else None
    if emissions:
        region, units = "United States", "million metric tonnes CO₂e"
        day, basis = f"{emissions[2]}-12-31", f"Eligible EPA inventory calendar years 2024 through {emissions[2]}"
        kwargs.update(resolution_source="EPA Inventory of U.S. Greenhouse Gas Emissions and Sinks",
                      report_policy="Net multi-gas CO₂ equivalent after sinks. Revisions after expiration excluded.")
    if reduction:
        region, units = "European Union", "% greenhouse gas reduction"
        day, basis = f"{reduction[3]}-12-31", "Target year in filed reduction goal"
        kwargs.update(resolution_source="European Union qualifying release",
                      report_policy=f"Official determination relative to {reduction[2]} baseline. Projections are distinct evidence.")
    if family == "ski-opening" and (opening := re.search(r"(?:on which|^If) (.+?) conducts Seasonal Opening Operations", primary)):
        region = opening[1]
        if cutoff := LONG_DAY.search(primary):
            day, basis = str(_long_day(cutoff)), "Exclusive Eastern Time date in primary rule"
            if "Eastern Time" in secondary:
                kwargs["climate_ends_at"] = iso(datetime.combine(_long_day(cutoff), time.min, ZoneInfo("America/New_York")))
        kwargs.update(resolution_source="Named ski resort under the filed source hierarchy",
                      report_policy="First eligible passenger lift operation for downhill access. Announced or projected dates do not count.")
    if family == "atmospheric-co2":
        region, units = "Mauna Loa Observatory", "ppm"
        deadline = re.search(r"\bbefore ([A-Z][a-z]{2,8}) (20\d{2})\b", primary)
        if deadline and (month := MONTHS.get(deadline[1][:3].lower())):
            day, basis = date(int(deadline[2]), month, 1).isoformat(), "exclusive month in primary rule"
        kwargs.update(resolution_source="NASA Vital Signs Mauna Loa monthly CO₂, measured by NOAA GML",
                      report_policy="Monthly average as displayed by NASA. Preliminary completed months qualify. "
                                    "Daily, annual and seasonally adjusted values excluded. Revisions after expiration excluded.")
    if family == "daily-rain" and (series.get("contract_terms_url") or "").endswith("/RAINGLOBALPERIOD.pdf"):
        if not kwargs.get("report_policy"):
            kwargs["report_policy"] = "Initial non-preliminary source report; material-error revisions"
        if "trace" in secondary.lower() and not kwargs.get("fallback"):
            kwargs["fallback"] = "Trace and missing daily values count zero; last fair price if reliable data unavailable"
    if family == "hourly-weather" and re.search(r"Synoptic Data", primary, re.I) and series_id in INDEX_CITIES:
        region = INDEX_CITIES[series_id][1]
        units = "°F"
        kwargs.update(resolution_source="Kalshi Weather Index / Synoptic Data",
                      report_policy="Latest quorum-qualified event minute at or before the labeled hour",
                      fallback="Prior canonical event minute if quorum fails at the labeled hour")
    elif family == "hourly-weather" and re.search(r"AccuWeather", primary, re.I):
        location = re.search(r"temperature recorded (?:at|in) (.*?) for ", primary)
        region = location[1] if location else region
        kwargs.update(resolution_source="AccuWeather", report_policy="Value published for the labeled hour")
    if series_id == "KXSJCLOWT" and "hourly temperature value reported for KSJC" in primary:
        first, last = _precipitation_dates(primary, secondary)
        if first:
            station, region, day = "KSJC", "San Jose, California", str(first)
            basis = f"Source reporting dates {first} through {last} inclusive"
            kwargs.update(resolution_source="The Weather Company", report_policy="Any source-reported hourly observation strictly below strike; as published at expiration",
                          fallback="Missing hourly observations do not count; NO if no qualifying hour is reported by expiration")
    if family == "regional-cold-spell" and "Dallas Fort Worth International Airport (DFW)" in primary and "Houston (IAH)" in primary:
        region = "Dallas–Fort Worth and Houston, Texas"
        if exclusive := LONG_DAY.search(primary):
            day, basis = str(_long_day(exclusive)), "Exclusive station climate date in primary rule"
        kwargs.update(resolution_source="National Weather Service, then NCEI, then NOAA",
                      report_policy="Average both daily station minima, truncated to 0.1°F. At least two qualifying days in a four-day span.",
                      fallback="Next source in filed hierarchy or official subdaily measurements under exchange review")
    strike_type = market.get("strike_type")
    lower, upper = market.get("floor_strike"), market.get("cap_strike")
    operator = {
        "greater": "above", "greater_or_equal": "at-least", "less": "below",
        "less_or_equal": "at-most", "between": "between-inclusive", "functional": "functional",
        "custom": "custom", "structured": "structured",
    }.get(strike_type)
    if strike_type == "between" and " is between " not in primary.lower():
        operator = "between"
    if family == "daily-rain" and (rain_threshold := re.search(r"strictly greater than (0(?:\.\d+)?)\b", primary, re.I)):
        trace_pays = (re.search(r"T (?:\(when the target is 0\) )?for Trace", secondary, re.I) and "resolves to Yes" in secondary)
        trace_positive = re.search(r'\btrace["”)]*\s+corresponds to a value greater than 0', secondary, re.I)
        if decimal(rain_threshold[1]) == 0 and (trace_pays or trace_positive):
            operator, lower, upper = "above-or-trace", "0", None
            kwargs["report_policy"] = "A measurable amount or an explicitly reported trace satisfies the zero threshold."
    if family == "regional-cold-spell" and "at least 2 days" in primary and (threshold := re.search(r"\bis below (\d+(?:\.\d+)?)°F", primary)):
        operator, lower, upper = "two-days-in-four-below", None, threshold[1]
    if ice_terms:
        rule_operator, threshold = ice_terms[1].lower(), ice_terms[2]
        if operator == rule_operator:
            lower, upper = (threshold, None) if operator == "above" else (None, threshold)
        else:
            operator, lower, upper = "conflicting", None, None
            kwargs["report_policy"] = (f'{kwargs.get("report_policy", "")} '
                                       "Primary rule direction conflicts with the native listing direction.").strip()
    if family == "reservoir-elevation" and (level := re.search(
            r"\b(greater|less)\s+(?:than\s+)?(\d+(?:\.\d+)?)\b", primary, re.I)):
        from_rule = "above" if level[1].lower() == "greater" else "below"
        if operator in (None, from_rule):
            operator = from_rule
            lower, upper = (level[2], None) if operator == "above" else (None, level[2])
        else:
            operator, lower, upper = "conflicting", None, None
            kwargs["report_policy"] += " Primary rule and native listing disagree on equality at the strike."
    if family == "storm-intensity" and (wind := re.search(r"greater than or equal to (\d+) mph", primary, re.I)):
        operator, lower, upper = "at-least", wind[1], None
    if family == "storm-landfall" and category:
        operator, lower, upper = ("at-least", category[1], None) if category[2] else ("exactly", category[1], category[1])
    if family == "storm-path" and "at least 74 mph" in primary:
        operator, lower, upper = "qualifying-segment-intersects", "74", None
    if family == "drought-severity" and drought:
        operator, lower, upper = "at-least", drought[7], None
    if family == "lake-surface-temperature" and (lake := re.search(r" is above (\d+(?:\.\d+)?)° Celsius", primary)):
        operator, lower, upper = "above", lake[1], None
    if family == "period-rain" and (depth := re.search(r"strictly greater than (\d+(?:\.\d+)?) inches", primary)):
        operator, lower, upper = "above", depth[1], None
    if air_quality:
        operator, lower, upper = "above", air_quality[1], None
    if emissions:
        operator, lower, upper = "at-most", None, emissions[1]
    if reduction:
        operator, lower, upper = "at-least", reduction[1], None
    if family == "ski-opening" and " before " in primary and day:
        operator = "date-before"
    if family == "annual-temperature" and (ref := GISS_REFERENCE.search(primary)) and (fixed := GISS_FIXED.search(primary)):
        operator, lower, upper = "above-threshold-and-reference", fixed[1], None
        policy = kwargs.get("report_policy") or "Applicable table at expiration"
        kwargs["report_policy"] = f"{policy}; compares with the applicable {ref[1]} value"
    native = market.get("status")
    status = _status_kalshi(native)
    payout = {}
    notional = market.get("notional_value_dollars")
    if (status == "settled" and market.get("settlement_value_dollars") not in (None, "")
            and market.get("market_type") == "binary" and notional is not None and decimal(notional) == 1):
        settlement_at = _time(market.get("settlement_ts"))
        payout = {
            **_payout(market["settlement_value_dollars"]), "settlement_at": settlement_at,
            "settlement_received_at": received_at, "settlement_source": source,
        }
    return {
        "venue": KALSHI, "instrument_id": ticker, "event_id": event_id, "series_id": series_id,
        "native_id": ticker, "native_event_id": event_id, "title": market.get("title"),
        "event_title": event.get("title") if event else None, "label": label or ticker,
        "family": family, "station": station, "region": region, "day": day, "day_basis": basis,
        "status": status, "native_status": native, "rules_primary": primary or None,
        "rules_secondary": secondary or None, "rules_url": series.get("contract_terms_url"),
        "source": source, "units": units, "lower": _field(lower), "upper": _field(upper),
        "operator": operator, "yes_side_id": "yes", "no_side_id": "no",
        "quantity_step": "0.01" if market.get("fractional_trading_enabled") is True else "1" if market.get("fractional_trading_enabled") is False else None,
        "opened_at": _time(market.get("open_time")), "closed_at": _time(market.get("close_time")),
        "updated_at": _time(market.get("updated_time")), "settlement_result": market.get("result") or None,
        "expiration_value": market.get("expiration_value") or None,
        "fee_type": (event or {}).get("fee_type_override") or series.get("fee_type"),
        "fee_multiplier": _field((event or {}).get("fee_multiplier_override", series.get("fee_multiplier"))),
        "fee_waiver_expiration_at": _time(market.get("fee_waiver_expiration_time")),
        "last_price": market.get("last_price_dollars"), "yes_bid": market.get("yes_bid_dollars"),
        "yes_ask": market.get("yes_ask_dollars"), **payout, **kwargs,
    }


def poly_settlement(payload, instrument):
    if native_id(payload.get("slug")) != instrument:
        raise ValueError("Settlement slug identity mismatch")
    return _payout(payload["settlement"])
