"""Evidence checks and entry rules for the comparative paper study."""

from dataclasses import dataclass
from datetime import timedelta
import math

from .market import OPEN, _depth_usable, decimal, iso, parse_time, source_time_key


ORIGIN_RESPONSES = frozenset({"EXPIRED", "MISS", "DYNAMIC", "BYPASS", "REVALIDATED"})
CACHE_HITS = frozenset({"HIT", "STALE", "UPDATING"})
RULES = {
    "no-trade": ("No trade", "Holding cash establishes the zero-position benchmark."),
    "noaa-threshold": ("NOAA probability threshold", "Require a supported NOAA probability at least two points above all-in cost."),
    "high-no": ("Conservative high-probability NO", "A supported NO can clear all-in cost after lowering its model probability using station-day calibration support."),
    "two-sided": ("Any YES or NO with expected dollars", "Both sides can qualify when after-fee model expected dollars clear the minimum order edge."),
    "two-sided-buffered": ("Buffered YES or NO with expected dollars", "A side clears the dollar floor even after capping its probability using station-day residual support."),
    "mid-price": ("Middle-of-book price range", "Expected-dollar signals survive when the average fill price is between 10 and 90 cents."),
    "mid-price-buffered": ("Buffered middle price range", "Middle-range price signals can clear fees after the residual support buffer."),
    "remaining-high": ("Observed plus remaining high", "A physical high based on measurements and hourly guidance changes after-fee entries."),
    "late-remaining-high": ("Late observed plus remaining high", "Replacing daily TXN only at trained 17:00 and 21:00 local clocks reduces outlier paper signals."),
    "precision-high": ("Precision-guarded observed high", "A wholly in-day METAR maximum and a calibrated disagreement reserve improve live paper signals."),
    "observation": ("Observation-triggered Ridge", "A new exact-station reading can move the learned side probability enough for a freshly rechecked quote to lag."),
    "market-weather": ("Market plus weather", "A weather update can improve on the venue probability proxy and retain an edge after fees."),
    "live-low": ("Daily-low model only", "Earlier-trained minimum-temperature probabilities can clear verified Polymarket US quotes after fees."),
    "joint-high-low": ("Daily highs and lows in one bankroll", "Sharing the station-day cap across maximum and minimum contracts changes what can be filled."),
    "guarded-low": ("Precision-guarded daily low", "An exact measured-minimum guard changes daily-low paper choices."),
    "joint-high-guarded-low": ("Daily highs and precision-guarded lows", "The guarded low model can change allocation in the shared high and low bankroll."),
    "joint-late-guarded-high-low": ("Precision lows with late precision highs", "Guarded low and late high forecasts can change allocation in a shared station-day bankroll."),
    "first-receipt-low": ("Daily-low first-receipt shadow", "Compare the scheduled low against an earlier update when a tighter exact METAR arrives."),
    "joint-late-high-first-receipt-low": ("Late highs with a first-receipt low shadow", "Compare both guarded low schedules on the same high and low bankroll."),
    "joint-transfer-high-guarded-low": ("Additional late-station high shadow", "Compare the independent later high model while preserving the same guarded low."),
    "joint-dew-point-high-guarded-low": ("Dew-point high shadow with guarded lows", "Compare the same cash and weather receipts with the observed humidity high predictor."),
    "joint-hrrr-high-guarded-low": ("HRRR and NBM high shadow with guarded lows", "Compare a fixed equal blend of remaining-hour weather guidance on the same simulated balance."),
    "joint-native-noaa-high-guarded-low": ("Native raw NOAA high reference", "Use the issued NOAA high and spread on the two added station tapes."),
}
FRESHNESS = {
    "legacy": "Zero recorded HTTP Age, treating missing Age as zero, with valid Polymarket source time.",
    "origin-confirmed": "Affirmative gateway origin fetch or synchronous revalidation, with valid Polymarket source time.",
}
OBSERVATION_MIN_DELTA = 0.02
OBSERVATION_WINDOW_SECONDS = 300
EXCHANGE_AGE_SECONDS = 120


def validate_freshness(policy):
    if policy not in FRESHNESS:
        raise ValueError("Unknown paper book freshness policy")


def temperature_window(row):
    """Translate a native operator to an inclusive integer-Fahrenheit interval."""
    if row["units"] != "°F" or row["operator"] not in {
        "below", "above", "at-most", "at-least", "between-inclusive", "exactly",
    }:
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


def book_status(book, side, at, policy):
    """Return a specific reason this receipt cannot price a simulated decision."""
    if not book or book.get("depth_kind") != "displayed_ladder":
        return "No displayed depth"
    if book.get("state") not in OPEN:
        return "Venue state is not open"
    if not book.get("source_at"):
        return "No exchange book timestamp"
    if book.get("source_time_progress") in {"regressed", "conflicting"} or book.get("book_receipt_conflict"):
        return "Regressing or conflicting exchange timestamp"
    age = book.get("cache_age_seconds")
    if age is not None and age != 0:
        return "Cached response with recorded Age"
    if policy == "origin-confirmed":
        cdn = book.get("http_cache", {}).get("cf-cache-status")
        if cdn in CACHE_HITS:
            return "Cached response without positive Age"
        if cdn not in ORIGIN_RESPONSES or age is not None:
            return "No affirmative origin evidence"
    if not _depth_usable(book, at, 0):
        return "Crossed book or inconsistent receipt clock"
    if side and book.get("offers" if side == "YES" else "bids") is None:
        return "Side depth omitted"
    return None


def exchange_clock_status(book, at, max_age):
    clock = source_time_key(book["source_at"])
    if clock > source_time_key(iso(at)):
        return "Exchange book clock is ahead of the decision"
    if clock < source_time_key(iso(at - timedelta(seconds=max_age))):
        return "Exchange image is stale"
    return None


@dataclass(frozen=True)
class StrategyRule:
    name: str
    freshness: str
    minimum_dollars: float
    max_exchange_age: int | None = None

    def depth_status(self, book, side, at):
        gap = book_status(book, side, at, self.freshness)
        if gap is None and self.max_exchange_age is not None:
            return exchange_clock_status(book, at, self.max_exchange_age)
        return gap

    def considers(self, info, side):
        if not info or info.get("uncertain", False) or info.get("probability") is None:
            return False
        if self.name == "no-trade":
            return False
        return self.name != "high-no" or (side == "NO" and 1 - info["probability"] >= 0.8)

    def qualifies(self, quote, *, signal):
        if quote["ev_dollars"] < self.minimum_dollars:
            return False
        if self.name in {"mid-price", "mid-price-buffered"}:
            return 0.10 <= quote["entry_price"] <= 0.90
        if self.name == "noaa-threshold":
            return quote["probability"] - quote["break_even_probability"] >= 0.02
        if signal and self.name == "observation":
            change = quote.get("strategy_context", {}).get("yes_probability_change", 0)
            return (change if quote["side"] == "YES" else -change) >= OBSERVATION_MIN_DELTA
        if self.name == "high-no":
            return quote["probability"] >= 0.8
        return True

    def side_probability(self, info, side, point):
        if self.name in {"two-sided-buffered", "mid-price-buffered"}:
            return min(point, decimal(residual_probability_bound(info, side) or 0))
        return point


def residual_probability_bound(estimate, side):
    """Use a one-sided Wilson buffer on residual support with station-days as n."""
    days, residuals = estimate.get("validation_days"), estimate.get("validation_residuals")
    support = estimate.get(f"{side.lower()}_tail_support")
    if not days or not residuals or support is None:
        return None
    n = min(days, residuals)
    frequency = support / residuals
    z = 1.2815515655446004
    variance = frequency * (1 - frequency) / n + z**2 / (4 * n**2)
    bound = (frequency + z**2 / (2 * n) - z * math.sqrt(variance)) / (1 + z**2 / n)
    return max(0.0, bound)


def conservative_no(estimate):
    bound = residual_probability_bound(estimate, "NO")
    if bound is None:
        return {**estimate, "uncertain": True}
    point = 1 - estimate["probability"]
    probability = min(point, bound)
    return {**estimate, "probability": 1 - probability,
            "strategy_context": {"raw_no_probability": point, "buffered_no_probability": probability,
                                 "residual_no_frequency": estimate["no_tail_support"] / estimate["validation_residuals"],
                                 "validation_station_days": min(estimate["validation_days"], estimate["validation_residuals"])}}


def legacy_recent_observation(event, at):
    """Reproduce the original research rule on earlier frozen runs."""
    start = parse_time(event["climate_starts_at"])
    eligible = [
        (received, weather_at, row)
        for row in event.get("observations", [])
        if row.get("source") in {"NWS", "Aviation Weather Center"}
        and start <= (weather_at := parse_time(row["at"])) <= (received := parse_time(row["available_at"])) <= at
        and (at - received).total_seconds() <= OBSERVATION_WINDOW_SECONDS
    ]
    if not eligible:
        return None
    return max(eligible, key=lambda item: (item[1], item[0], item[2]["source"] == "NWS"))[2]


def normalized_report(row):
    words = (row.get("report") or "").strip().upper().split()
    if words and words[0] in {"METAR", "SPECI"}:
        words = words[1:]
    return " ".join(words).removesuffix("=").rstrip() or None


def distinct_observations(event, at):
    """Keep the first delivery of each reading and separately retain real revisions."""
    start = parse_time(event["climate_starts_at"])
    chronological = []
    for row in event.get("observations", []):
        if row.get("source") not in {"NWS", "Aviation Weather Center"}:
            continue
        observed, received = parse_time(row["at"]), parse_time(row["available_at"])
        if start <= observed <= received <= at:
            chronological.append((received, observed, row))
    known = {}
    rows = []
    for _, observed, row in sorted(chronological, key=lambda item: (item[0], item[1], item[2]["source"] != "NWS")):
        original = normalized_report(row)
        reading = decimal(row["temp_f"])
        prior = known.setdefault((observed, reading), set())
        # A blank report has no original identity. When that same temperature
        # already arrived, a blank mirror or first text attachment only enriches it.
        if original in prior or (prior and (original is None or prior == {None})):
            if original:
                prior.add(original)
            continue
        prior.add(original)
        rows.append(row)
    return rows


def recent_observation(event, at):
    """Select only a genuinely new exact-station receipt in the signal window."""
    eligible = [row for row in distinct_observations(event, at)
                if (at - parse_time(row["available_at"])).total_seconds() <= OBSERVATION_WINDOW_SECONDS]
    return max(eligible, key=lambda row: (parse_time(row["at"]), parse_time(row["available_at"])), default=None)
