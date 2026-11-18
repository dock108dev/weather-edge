"""Polymarket US standard Tier 1 taker assumptions used by Weather Edge paper orders."""

from bisect import bisect_right
from datetime import datetime, timezone
from decimal import Decimal

SOURCE = "https://docs.polymarket.us/fees"
TIER_CHANGE_FILING = "https://www.cftc.gov/filings/orgrules/rules11112650879.pdf"
FILING_DAY = datetime(2026, 11, 6, 5, tzinfo=timezone.utc)
FEE_CHANGE = datetime(2026, 11, 14, 5, tzinfo=timezone.utc)  # midnight Eastern
_VERSIONS = (
    (datetime.min.replace(tzinfo=timezone.utc), Decimal("0.0695")),
    (FEE_CHANGE, Decimal("0.095")),
)
_STARTS = tuple(start for start, _ in _VERSIONS)


def version(at):
    return _VERSIONS[bisect_right(_STARTS, at) - 1]


def frozen_schedule():
    # Keep the serialized shape and timestamps used by book-recheck-v2.
    return [{"from": start.isoformat().replace("+00:00", "Z"), "coefficient": str(rate)} for start, rate in _VERSIONS]
