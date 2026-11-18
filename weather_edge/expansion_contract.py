"""Strict settlement matching for exact-station Polymarket US weather replays."""

from __future__ import annotations

import json
import re
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from zoneinfo import ZoneInfo

from . import expansion_weather
from .history import _check_bucket_partition, _fetch, parse_prices, price_history_url
from .market import GATEWAY, SETTLEMENT_RULE, _bucket_bounds, _identifier
from .nbm import climate_window, iso, utc

LOW_EVENTS = {"KNYC": "temp-nyclow", "KMDW": "temp-mdwlow"}
LOW_RULE = re.compile(SETTLEMENT_RULE.pattern.replace("highest temperature", "lowest temperature"), re.IGNORECASE)
TEMP_FILING = "https://www.cftc.gov/IndustryOversight/IndustryFilings/TradingOrganizationProducts/60333"


def _market_dir(data, slug):
    if not re.fullmatch(r"[a-z0-9-]+", slug):
        raise ValueError(f"Invalid market slug {slug!r}")
    return Path(data) / "expansion" / "markets" / slug


def load_event(data, slug, *, refresh=False):
    source = f"{GATEWAY}/v1/events/slug/{slug}"
    raw = _market_dir(data, slug)
    path = raw / "event.json"
    if refresh:
        now = datetime.now(timezone.utc)
        path = raw / "versions" / f"event-{now:%Y%m%dT%H%M%SZ}.json"
    else:
        path = max((raw / "versions").glob("event-*.json"), default=path)
    event = json.loads(_fetch(path, source))["event"]
    if event["slug"] != slug:
        raise ValueError(f"Public event slug does not match request for {slug}")
    return event, source


def _side_ids(market):
    market_id = _identifier(market["id"])
    sides = {True: [], False: []}
    for side in market["marketSides"]:
        long = side.get("long")
        if (type(long) is not bool or _identifier(side.get("marketId")) != market_id
                or side.get("description") != ("Yes" if long else "No")):
            raise ValueError(f"Unverified side identity for {market['slug']}")
        sides[long].append(_identifier(side["id"]))
    if any(len(values) != 1 for values in sides.values()) or sides[True] == sides[False]:
        raise ValueError(f"Incomplete side identity for {market['slug']}")
    return market_id, sides[True][0], sides[False][0]


def load_payout(data, event_slug, market):
    """Use the explicit exchange payout endpoint and retain its raw response."""
    slug = market["slug"]
    if market.get("status") != "MARKET_STATUS_RESOLVED":
        return None, None
    _market_dir(data, slug)
    source = f"{GATEWAY}/v1/markets/{slug}/settlement"
    raw = _market_dir(data, event_slug)
    value = json.loads(_fetch(raw / f"{slug}-settlement.json", source))
    p = value.get("settlement")
    if value.get("slug") != slug or type(p) not in (int, float) or p not in (0, 1):
        raise ValueError(f"Invalid binary payout for {slug}")
    return int(p), source


def load_price_history(data, event_slug, bucket, start, end):
    slug = bucket["id"]
    _market_dir(data, slug)
    source = price_history_url(slug, start, end)
    raw = _market_dir(data, event_slug)
    path = raw / f"{slug}-{int(start.timestamp())}-{int(end.timestamp())}-prices.json"
    data = json.loads(_fetch(path, source))
    return parse_prices(data, start, end), source


def _low_buckets(event, day, station):
    buckets = []
    for market in event["markets"]:
        rule = market["description"]
        match = LOW_RULE.search(rule)
        if (match is None or match[1].upper() != station or match[2] != str(day)
                or not market["slug"].startswith(f"tc-{event['slug']}-")):
            raise ValueError(f"Low market date, station, or CLI terms unverified for {market['slug']}")
        lower, upper = _bucket_bounds(match[3])
        market_id, yes_id, no_id = _side_ids(market)
        buckets.append({"id": market["slug"], "market_id": market_id, "label": market["title"], "rules": rule,
                        "lower_f": lower, "upper_f": upper, "yes_id": yes_id, "no_id": no_id,
                        "starts_at": market["startDate"], "scheduled_end_at": market["endDate"],
                        "status": market.get("status"), "market": market})
    buckets.sort(key=lambda bucket: float("-inf") if bucket["lower_f"] is None else bucket["lower_f"])
    _check_bucket_partition(buckets)
    if len({bucket["id"] for bucket in buckets}) != len(buckets):
        raise ValueError("Duplicate low market identifiers")
    return buckets


def _between(value, bucket):
    lo, hi = bucket["lower_f"], bucket["upper_f"]
    return (lo is None or value >= lo) and (hi is None or value <= hi)


def low_market(data, station, day, summary_label):
    """Join a resolved case to source CLI and explicit payouts for all buckets."""
    slug = f"{LOW_EVENTS[station]}-{day}"
    event, source = load_event(data, slug)
    buckets = _low_buckets(event, day, station)
    starts = [utc(bucket["starts_at"]) for bucket in buckets]
    end_times = {utc(bucket["scheduled_end_at"]) for bucket in buckets}
    if len(end_times) != 1:
        raise ValueError(f"Buckets disagree on the scheduled settlement for {slug}")
    started, scheduled = max(starts), end_times.pop()
    if any(bucket["status"] != "MARKET_STATUS_RESOLVED" for bucket in buckets):
        raise ValueError(f"Exchange low family is not completely resolved for {slug}")
    reports = expansion_weather.reports_for_day(data, station, day, summary_label["product"])
    verified = [report for report in reports
                if report["low_f"] is not None and utc(report["published_at"]) <= scheduled]
    if not verified:
        raise ValueError(f"No complete station CLI minimum before scheduled expiry for {slug}")
    official = max(verified, key=lambda row: row["published_at"])
    for bucket in buckets:
        market = bucket.pop("market")
        payout, payout_url = load_payout(data, slug, market)
        if payout is None or payout != int(_between(official["low_f"], bucket)):
            raise ValueError(f"NWS CLI minimum and exchange payoff disagree for {bucket['id']}")
        bucket.update({"payout_yes": payout, "payout_no": 1 - payout, "payout_source": payout_url})
    if sum(bucket["payout_yes"] for bucket in buckets) != 1:
        raise ValueError(f"No unique verified winner for {slug}")
    lo, hi = climate_window(day, expansion_weather.STATIONS[station][3])
    return {"id": slug, "native_id": event["id"], "venue": "polymarket-us", "date": str(day), "station": station,
            "title": event["title"], "buckets": buckets, "starts_at": iso(started),
            "scheduled_end_at": iso(scheduled), "event_end_at": event["endDate"],
            "climate_start": iso(lo), "climate_end": iso(hi),
            "official_report": official, "reports": reports, "source": source,
            "terms_filing": TEMP_FILING,
            "payout_time_note": "The exchange endpoint supplies the payout and no actual publication timestamp."}


def rain_outcome(value):
    """Apply the KNYC daily listing's printed liquid-equivalent threshold."""
    if value in ("T", "M", "MM"):
        return 0
    numeric = expansion_weather.parse_number(value)
    if numeric is None:
        return None
    return int(numeric >= 0.01)


def rain_rule_matches(day, station, rules):
    if station not in expansion_weather.STATIONS or not isinstance(rules, str):
        return False
    lo, hi = climate_window(day, expansion_weather.STATIONS[station][3])
    end_minute = hi - timedelta(minutes=1)
    climate_date = f"{day:%B} {day.day}, {day.year}"
    expected = (f"({station})", climate_date, "National Weather Service Daily Climate Report", "0.01 inches or more",
                'A trace ("T") does not count', 'missing ("M" or "MM")', "total liquid-equivalent precipitation",
                "2:00 PM ET", lo.strftime("%H:%M UTC"), end_minute.strftime("%H:%M UTC"), "first one published afterwards")
    return all(fragment in rules for fragment in expected)


def rain_report(data, day, known_product, *, station="KNYC", latest_end=None, refresh_listing=False):
    local_day = day + timedelta(days=1)
    cutoff = utc(datetime.combine(local_day, time(14), tzinfo=ZoneInfo("America/New_York")))
    reports = expansion_weather.reports_for_day(data, station, day, known_product, refresh_listing=refresh_listing)
    timely = [report for report in reports if utc(report["published_at"]) <= cutoff]
    if timely:
        return max(timely, key=lambda row: row["published_at"]), reports, cutoff
    _, climate_end = climate_window(day, expansion_weather.STATIONS[station][3])
    latest = latest_end or climate_end + timedelta(days=14)
    extended = expansion_weather.reports_for_day(data, station, day, known_product, following_days=15)
    valid = [report for report in extended if cutoff < utc(report["published_at"]) <= latest]
    official = min(valid, key=lambda row: row["published_at"], default=None)
    return official, extended, cutoff


def rain_market(data, day, summary_label, *, station="KNYC", refresh=False):
    slug = f"rain-daily-{day}"
    event, source = load_event(data, slug, refresh=refresh)
    ident = f"raincli-{slug}-{station.lower()}"
    market = next((row for row in event["markets"] if row.get("slug") == ident), None)
    if not market:
        raise ValueError(f"No {station} instrument for {slug}")
    rules = market["description"]
    lo, hi = climate_window(day, expansion_weather.STATIONS[station][3])
    if not rain_rule_matches(day, station, rules):
        raise ValueError(f"Unexpected {station} rain rule for {slug}")
    payout, payout_source = load_payout(data, slug, market)
    if payout is None:
        raise ValueError(f"Exchange outcome pending for {ident}")
    official, reports, ruling_cutoff = rain_report(data, day, summary_label["product"], station=station,
                                                  latest_end=utc(market["endDate"]), refresh_listing=refresh)
    if not official or rain_outcome(official["precip_as_reported"]) != payout:
        raise ValueError(f"Governing NWS report and exchange payoff disagree for {ident}")
    market_id, yes_id, no_id = _side_ids(market)
    bucket = {"id": ident, "market_id": market_id, "label": "At least 0.01 inches precipitation",
              "rules": rules, "yes_id": yes_id, "no_id": no_id, "payout_yes": payout, "payout_no": 1 - payout,
              "payout_source": payout_source, "starts_at": market["startDate"], "scheduled_end_at": market["endDate"]}
    return {"id": slug, "native_id": event["id"], "venue": "polymarket-us", "date": str(day), "station": station,
            "title": f"Measurable precipitation at {station}, {day}", "buckets": [bucket],
            "starts_at": market["startDate"], "scheduled_end_at": market["endDate"], "ruling_cutoff": iso(ruling_cutoff),
            "climate_start": iso(lo), "climate_end": iso(hi), "reports": reports,
            "official_report": official, "source": source,
            "payout_time_note": "The exchange endpoint supplies the payout and no actual publication timestamp."}


def availability_error(error):
    if isinstance(error, HTTPError):
        return f"HTTP {error.code}"
    return str(error)
