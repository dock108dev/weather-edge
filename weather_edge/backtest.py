"""Bounded, auditable evaluations of retained Weather Edge evidence."""

from collections import Counter
from datetime import date, datetime, time, timedelta, timezone

from . import archive, capture, capture_fixture, collector, paper_runs, strategy_native_capture, trade
from .market import iso, parse_time
from .strategy_rules import EXCHANGE_AGE_SECONDS


STRATEGIES = ("two-sided", "two-sided-buffered", "no-trade")
MAX_DAYS = 7
CASE_PAGE_SIZE = 40


def folder(data, fixture):
    return data / "backtests" / ("fixture" if fixture else "live")


def _archive_coverage(data, start, through):
    rows = []
    for offset in range((through.date() - start.date()).days + 1):
        day = start.date() + timedelta(days=offset)
        for event in archive.load_day(data, str(day))["events"]:
            samples = sum(
                start <= parse_time(price.get("available_at") or price["at"]) <= through
                for bucket in event["buckets"] for price in bucket.get("prices", [])
            )
            rows.append({"day": event["date"], "station": event["station"], "display_samples": samples})
    return rows


def _real_tape(data, request, now):
    selected = request.get("capture_at")
    if selected:
        through = parse_time(selected)
        if through > now:
            raise ValueError("Choose a retained capture in the past")
        retained = capture.load_captures(data / "captures", since=through, through=through, versioned_windows=True)
        if retained is None or parse_time(retained["captured_at"]) != through:
            raise ValueError("That exact capture is no longer available")
        start = parse_time(retained["started_at"])
        retained = strategy_native_capture.with_listing_evidence(data, retained, through)
        return collector.enrich_capture(data, retained), start, through, "capture", []

    first, last = date.fromisoformat(request["start"]), date.fromisoformat(request["end"])
    if not 0 <= (last - first).days < MAX_DAYS or first > now.date():
        raise ValueError(f"Choose a past date or a range of up to {MAX_DAYS} UTC dates")
    start = datetime.combine(first, time.min, timezone.utc)
    through = min(datetime.combine(last + timedelta(days=1), time.min, timezone.utc) - timedelta(microseconds=1), now)
    retained = capture.load_captures(data / "captures", since=start, through=through, versioned_windows=True)
    retained = retained or {"events": [], "issues": []}
    retained = {**retained, "kind": "live", "started_at": iso(start), "captured_at": iso(through)}
    for family in ("daily-high", "daily-low"):
        events, issues = strategy_native_capture.polymarket_events(data, through, since=start, family=family)
        _, conflicts = strategy_native_capture.merge_supplemental(retained, events)
        retained["issues"] = [*retained["issues"], *issues, *conflicts]
    retained = strategy_native_capture.with_listing_evidence(data, retained, through)
    coverage = _archive_coverage(data, start, through)
    return collector.enrich_capture(data, retained), start, through, "range", coverage


def _fixture_probability(event, _at):
    return {
        item["id"]: {"probability": probability, "uncertain": False, "model_version": "synthetic",
                     "forecast_issued_at": event["forecasts"][0]["issued_at"],
                     "forecast_available_at": event["forecasts"][0]["available_at"]}
        for item in event["buckets"]
        for suffix, probability in (("gte49lt50f", 0.23), ("gte51lt52f", 0.258))
        if item["id"].endswith(suffix)
    }


def save(data, request, predict, now):
    fixture = request.get("fixture") is True
    strategy = request["strategy"]
    if strategy not in STRATEGIES:
        raise ValueError("Choose an available strategy")
    budget = trade.order_size(request["size"])
    reaction = request["reaction"]
    if type(reaction) is not int or not 1 <= reaction <= 120:
        raise ValueError("Choose a reaction delay from 1 to 120 seconds")
    if fixture:
        tape = capture_fixture.fixture_capture(duration_seconds=30, poll_seconds=8, max_rounds=8, max_events=1)
        start, through = map(parse_time, (tape["started_at"], tape["captured_at"]))
        source, archive_rows = "fixture", []
        predict = _fixture_probability
    else:
        tape, start, through, source, archive_rows = _real_tape(data, request, now)

    # The date range is a UTC decision/recheck window. Preserve prior forecasts
    # and observations separately so the first decision can use already-known data.
    tape["events"] = [{
        **event,
        "official_reports": [row for row in event.get("official_reports", [])
                             if parse_time(row["available_at"]) <= through],
        "buckets": [{**bucket, "books": [book for book in bucket.get("books", [])
                                        if start <= parse_time(book["at"]) <= through]} for bucket in event["buckets"]],
    } for event in tape["events"]]
    evaluation = {
        "strategy": strategy, "source": source, "from": iso(start), "through": iso(through),
        "freshness": "legacy" if fixture else trade.FRESHNESS,
        "max_exchange_age": None if fixture else EXCHANGE_AGE_SECONDS,
        "audit_rejections": True,
        "archive_station_days": archive_rows,
        "receipt_issues": dict(Counter(issue["reason"] for issue in tape.get("issues", []) if issue.get("reason"))),
    }
    run = paper_runs.create_run(
        tape, predict if strategy != "no-trade" else lambda *_: {},
        as_of=through, order_size=budget, reaction_seconds=reaction,
        bankroll=trade.BANKROLL, max_station_day=trade.STATION_LIMIT, evaluation=evaluation,
    )
    return paper_runs.store_run(folder(data, fixture), run)


def _case(run, attempt):
    event = next(event for event in run["inputs"]["evidence"]["events"] if event["id"] == attempt["event_id"])
    bucket = next(bucket for bucket in event["buckets"] if bucket["id"] == attempt["bucket_id"])
    signal = parse_time(attempt["signal_at"])
    reading = max((row for row in event.get("observations", [])
                   if parse_time(row["at"]) <= signal and parse_time(row["available_at"]) <= signal),
                  key=lambda row: parse_time(row["at"]), default=None)
    position = next((pos for pos in run["result"]["positions"]
                     if all(pos[key] == attempt[key] for key in ("event_id", "bucket_id", "side", "signal_at"))), None)
    reaction_book_at = (attempt.get("reaction_quote") or {}).get("quote_at") or attempt["reaction_at"]
    clocks = {parse_time(at) for at in (attempt["quote_at"], reaction_book_at) if at}
    books = [book for book in bucket["books"] if parse_time(book["at"]) in clocks]
    return {"attempt": attempt, "position": position, "reading": reading, "books": books,
            "rule": bucket.get("rules") or bucket.get("description"),
            "venue": event.get("venue", trade.POLY), "fixture": run["inputs"]["evidence"]["kind"] == "fixture"}


def describe(run, offset=0, case=None):
    result = run["result"]
    attempts = sorted(result["attempts"], key=lambda row: (row["signal_at"], row["bucket_id"], row["side"]))
    reasons = Counter(row["result"] for row in attempts)
    evaluation = run["inputs"]["evaluation"]
    summary = paper_runs.describe_run(run, include_result=False)
    awaiting = reasons.get("Awaiting eligible recheck", 0)
    return {
        **summary, "evaluation": evaluation,
        "display_samples": sum(row["display_samples"] for row in evaluation["archive_station_days"]),
        "signals": len(attempts), "skipped": len(attempts) - awaiting - result["fills"], "awaiting": awaiting,
        "settled": result["settled"], "unresolved_payouts": result["fills"] - result["settled"],
        "recheck_outcomes": dict(reasons), "cash": result["cash"], "cash_committed": result["cash_committed"],
        "cash_baseline": result["start_bankroll"],
        **({"eligibility_rejections": result["eligibility_rejections"],
            "signal_rejections": result["signal_rejections"][offset:offset + CASE_PAGE_SIZE]}
           if "eligibility_rejections" in result else {}),
        "cases": [
            {"index": i, **{key: attempts[i].get(key) for key in
                           ("station", "date", "signal_at", "label", "side", "result", "reaction_at")}}
            for i in range(offset, min(offset + CASE_PAGE_SIZE, len(attempts)))
        ],
        "next_offset": offset + CASE_PAGE_SIZE if offset + CASE_PAGE_SIZE < len(attempts) else None,
        "case": _case(run, attempts[case]) if case is not None and 0 <= case < len(attempts) else None,
    }
