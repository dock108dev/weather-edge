"""Synthetic exchange and weather replies for the offline capture exercise."""

from datetime import datetime, timedelta, timezone
import json
from threading import Lock
from urllib.error import HTTPError
from urllib.parse import urlencode

from .capture import AWC, NWS, Reply, capture
from .market import DISCOVERY, GATEWAY, iso
from .nbm import nbm_url

START = datetime(2026, 11, 15, 17, 10, tzinfo=timezone.utc)
EVENT = "temp-nychigh-2026-11-15"
BUCKETS = (
    ("lt47f", "46 or below", "less than or equal to 46F"),
    ("gte47lt48f", "47 to 48", "between 47F and 48F"),
    ("gte49lt50f", "49 to 50", "between 49F and 50F"),
    ("gte51lt52f", "51 to 52", "between 51F and 52F"),
    ("gte53lt54f", "53 to 54", "between 53F and 54F"),
    ("gte55f", "55 or above", "greater than or equal to 55F"),
)
END = "2026-11-16T04:59:00Z"


def _event():
    markets = []
    for i, (suffix, label, condition) in enumerate(BUCKETS, 1):
        slug = f"tc-{EVENT}-{suffix}"
        markets.append({
            "slug": slug, "id": str(i), "title": label, "status": "MARKET_STATUS_OPEN",
            "description": ("Will the highest temperature recorded at Central Park (KNYC) in New York City "
                            "for 2026-11-15 as reported by the National Weather Service's Climatological "
                            f"Report (Daily) be {condition}? Outcome verified from NWS Climatological Report."),
            "endDate": "2026-11-16T10:00:00Z", "minimumTradeQty": 0.01, "feeCoefficient": 0.095,
            "marketSides": [
                {"id": str(100+i), "marketId": i, "long": True, "description": "Yes"},
                {"id": str(200+i), "marketId": i, "long": False, "description": "No"},
            ],
        })
    return {"id": "1", "slug": EVENT, "title": "Highest temperature in NYC on November 15?",
            "startDate": "2026-11-15T05:00:00Z", "endDate": END, "markets": markets}


def _row(label, entries):
    return label.ljust(5) + "".join(f"{entry:>3}" for entry in entries) + "\n"


def _nbm(product):
    lines = f" KNYC   NBM  {product.upper()} GUIDANCE    11/15/2026  1600 UTC\n"
    if product == "nbh":
        lines += _row("UTC", ("17", "18", "19", "20", "21", "22", "23", "00", "01", "02", "03", "04"))
        lines += _row("TMP", (50, 51, 51, 50, 49, 48, 47, 46, 45, 45, 45, 44))
        lines += _row("TSD", (2,) * 12)
    else:
        lines += _row("FHR", ("02", "05", "08", "11", "14", "17", "20", "23", "26", "29", "32"))
        lines += _row("TXN", ("", "", 51, "", 44, "", "", "", "", "", 54))
        lines += _row("XND", ("", "", 3, "", 2, "", "", "", "", "", 3))
    return lines.encode()


def _level(price, quantity):
    return {"px": {"currency": "USD", "value": str(price)}, "qty": str(quantity)}


def _book(slug, receipt, request_idx):
    # A wide, shallow book can offer a short-lived edge. Two independent images
    # retain the cheaper bucket; a second attractive bucket reprices after the first.
    suffix = slug.split("-")[-1]
    best_bid, best_offer, top_quantity = {
        "lt47f": (0.01, 0.04, 90),
        "gte47lt48f": (0.02, 0.11, 5),
        "gte49lt50f": (0.05, 0.12, 45),
        "gte51lt52f": (0.10, 0.18 if request_idx == 1 else 0.85, 32),
        "gte53lt54f": (0.13, 0.37, 6),
        "gte55f": (0.01, 0.14, 150),
    }[suffix]
    return {"marketData": {
        "marketSlug": slug, "state": "MARKET_STATE_OPEN", "transactTime": iso(receipt),
        "bids": [_level(best_bid, 40)],
        "offers": [_level(best_offer, top_quantity), _level(round(best_offer + 0.08, 2), 90)],
    }}


class FixtureClock:
    def __init__(self, start=START):
        self.start = start
        self.seconds = 0.0

    def monotonic(self):
        return self.seconds

    def now(self):
        return self.start + timedelta(seconds=self.seconds)

    def sleep(self, seconds):
        self.seconds += seconds


class FixtureTransport:
    def __init__(self, clock):
        self.clock = clock
        self.calls = {}
        self._lock = Lock()

    def __call__(self, url, limit=None, headers=None):
        with self._lock:
            self.calls[url] = self.calls.get(url, 0) + 1
            idx = self.calls[url]
        open_ = self.clock.now() < datetime.fromisoformat(END.replace("Z", "+00:00"))
        at = self.clock.now()
        if url == DISCOVERY:
            data = {"events": [_event()] if open_ else []}
        elif url.startswith(GATEWAY + "/v1/markets/tc-" + EVENT + "-"):
            slug, endpoint = url.split("/")[-2:]
            if endpoint == "book" and open_:
                data = _book(slug, self.clock.now(), idx)
            elif endpoint == "settlement" and not open_:
                data = {"slug": slug, "settlement": float(slug.endswith("gte51lt52f"))}
            else:
                raise HTTPError(url, 404, "No fixture for this time", None, None)
        elif url == f"{NWS}/stations/KNYC/observations?limit=72":
            at = self.clock.start
            data = {"features": [
                {"properties": {"station": f"{NWS}/stations/KNYC", "timestamp": f"2026-11-15T{hour}:51:00Z",
                                "temperature": {"value": temperature, "unitCode": "wmoUnit:degC"}}}
                for hour, temperature in ((12, 6.1), (13, 7.2), (14, 8.2), (15, 9.4), (16, 10.1))
            ]}
        elif url == AWC + "?" + urlencode({"ids": "KNYC", "format": "json", "hours": 24}):
            at = self.clock.start
            reading = datetime(2026, 11, 15, 16, 51, tzinfo=timezone.utc)
            data = [{"icaoId": "KNYC", "obsTime": int(reading.timestamp()), "temp": 10.1,
                     "rawOb": "SYNTHETIC KNYC 151651Z"}]
        elif url in {nbm_url(START.date(), 16, "nbh"), nbm_url(START.date(), 16, "nbs")}:
            return Reply(_nbm("nbh" if "nbhtx" in url else "nbs"), self.clock.start, {})
        else:
            raise HTTPError(url, 404, "Unexpected fixture path", None, None)
        return Reply(json.dumps(data).encode(), at, {})


def fixture_capture(*, start=START, previous=None, **options):
    clock = FixtureClock(start)
    transport = FixtureTransport(clock)
    return capture(transport, kind="fixture", previous=previous,
                   clock=clock.now, monotonic=clock.monotonic, sleep=clock.sleep, **options)
