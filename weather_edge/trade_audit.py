"""Measure the live Trade path using retained local and exchange clocks."""

import argparse
from bisect import bisect_right
from collections import Counter
from datetime import timedelta
from hashlib import sha256
from http.client import HTTPConnection
import json
from pathlib import Path
from urllib.parse import urlencode

from . import capture, collector, paper_eligibility, strategy_native_capture, trade, trade_orders, venue_archive
from .market import _latest_book, iso, mark_book_progress, paper_trial, parse_time
from .strategy_rules import StrategyRule


def _samples(path, since=None):
    with Path(path).open() as log:
        for line in log:
            if line.endswith("\n"):
                item = json.loads(line)
                started = item.get("sample_started")
                if since is None or (started and parse_time(started) >= since):
                    yield item


def sample_local_board(port, *, budget=trade.DEFAULT_BUDGET):
    """Read the bounded board sample and its exact tickets from the local listener."""
    connection = HTTPConnection("127.0.0.1", port, timeout=30)

    def read(**params):
        connection.request("GET", "/api/trade?" + urlencode({"budget": budget, **params}))
        response = connection.getresponse()
        payload = json.loads(response.read())
        if response.status != 200:
            raise ValueError(payload.get("error") or f"HTTP {response.status}")
        return payload

    sample = {"sample_started": iso(collector.now()), "tickets": []}
    try:
        board = sample["board"] = read(summary=1, funnel=1)
        for row in board["funnel"]["preview_samples"]:
            entry = {"board": row}
            sample["tickets"].append(entry)
            try:
                entry["ticket"] = read(**{key: row[key] for key in ("venue", "instrument", "side")})
            except (OSError, ValueError) as error:
                entry["error"] = str(error)
    except (OSError, ValueError) as error:
        sample["error"] = str(error)
    finally:
        connection.close()
    sample["sample_finished"] = iso(collector.now())
    return sample


def observed_funnel(path, through_clock=None, *, since=None):
    """Summarize sampled board appearances and read-only exact ticket requests."""
    summary = {}
    start = through = None
    errors, coverage = [], []
    for sample in _samples(path, since=since):
        if through_clock and sample.get("sample_started") and parse_time(sample["sample_started"]) > through_clock:
            continue
        start = start or sample.get("sample_started")
        finished = sample.get("sample_finished")
        if finished:
            through = iso(min(parse_time(finished), through_clock)) if through_clock else finished
        if sample.get("error"):
            errors.append({"at": sample["sample_started"], "error": sample["error"]})
        board = sample.get("board", {}).get("funnel")
        if not board or through_clock and parse_time(sample["board"]["at"]) > through_clock:
            continue
        if board.get("instrument_coverage"):
            coverage.append({"at": sample["board"]["at"], "budget": board["budget"],
                             "by_lane": board["instrument_coverage"]})
        scope = summary.setdefault(board["readiness_basis"], {
            "samples": 0, "candidate_scopes": Counter(), "budgets": Counter(), "venues": {},
        })
        scope["samples"] += 1
        scope["candidate_scopes"][board.get("candidate_scope", "listing-status")] += 1
        scope["budgets"][str(board.get("budget", "unrecorded"))] += 1
        for venue, counts in board["venues"].items():
            totals = scope["venues"].setdefault(venue, {
                "appearances": Counter(), "ready_blockers": Counter(), "ladder_blockers": Counter(),
                "ticket_outcomes": Counter(), "exact_previews": Counter(),
            })
            totals["appearances"].update({key: value for key, value in counts.items() if isinstance(value, int)})
            for field in ("ready_blockers", "ladder_blockers"):
                totals[field].update(counts[field])
        for check in sample["tickets"]:
            row = check["board"]
            ticket = check.get("ticket") or {}
            reason = check.get("error") or ticket.get("blocker")
            previews = scope["venues"][row["venue"]]["exact_previews"]
            previews["sampled"] += 1
            if through_clock and ticket.get("at") and parse_time(ticket["at"]) > through_clock:
                status = "Preview outside observation cutoff"
                previews["outside_cutoff"] += 1
            elif not ticket:
                status = reason or "Exact preview unwitnessed"
                previews["failed" if check.get("error") else "unobserved"] += 1
            elif reason:
                status = reason
                previews["blocked"] += 1
            elif not ticket.get("review_key") or not ticket.get("order"):
                status = "Exact preview eligibility unwitnessed"
                previews["unobserved"] += 1
            elif ticket.get("review_key") != row.get("review_key"):
                status = "Ready with revised preview"
                previews["eligible"] += 1
            else:
                status = "Same Ready preview"
                previews["eligible"] += 1
            scope["venues"][row["venue"]]["ticket_outcomes"][status] += 1
    return {"from": start, "through": through, "by_policy": summary, "instrument_coverage": coverage, "errors": errors,
            "denominators": {"appearances": "each exact side on each observed board; repeats across evaluations count",
                             "exact_previews": "bounded ticket GET samples from the board, including failed requests",
                             "instrument_coverage": "distinct open instruments per observed lane and clock"}}


def sampled_rechecks(data, path, through, predict, *, since=None):
    """Follow an actually sampled, eligible HTTP preview over the retained tape."""
    results = []
    for sample in _samples(path, since=since):
        for request in sample.get("tickets", ()):
            ticket = request.get("ticket") or {}
            if not ticket.get("review_key") or not ticket.get("order") or ticket.get("blocker"):
                continue
            if ticket["venue"] != trade.POLY:
                continue
            at = parse_time(ticket["at"])
            if at > through:
                continue
            start = parse_time(ticket["quote_at"]) - timedelta(seconds=collector.BOOK_INTERVAL)
            _, deadline = trade.recheck_window(at, ticket.get("closes_at"), False)
            end = min(through, deadline + timedelta(seconds=collector.BOOK_INTERVAL))
            entry = {"venue": ticket["venue"], "instrument": ticket["instrument"], "side": ticket["side"],
                     "preview_at": ticket["at"], "window_ends_at": iso(deadline),
                     "quote_at": ticket["quote_at"], "board_at": request["board"]["at"]}
            results.append(entry)
            if deadline > through:
                entry["outcome"] = "Recheck window incomplete at observation cutoff"
                entry["window_state"] = "incomplete"
                continue
            tape = capture.load_captures(data / "captures", since=start, through=end, versioned_windows=True)
            events = (tape or {}).get("events", [])
            if not any(bucket["id"] == ticket["instrument"] for event in events for bucket in event["buckets"]):
                events, _ = strategy_native_capture.polymarket_events(
                    data, end, since=start, family=ticket.get("family") or "daily-low")
            matched = next(((event, bucket, book) for event in events for bucket in event["buckets"]
                            if bucket["id"] == ticket["instrument"] for book in bucket["books"]
                            if book["at"] == ticket["quote_at"]), None)
            if not matched:
                entry["outcome"] = "Preview receipt unavailable for reconstruction"
                entry["window_state"] = "unobserved"
                continue
            event, bucket, book = matched
            tape = strategy_native_capture.with_listing_evidence(data, {"kind": "live", "events": [event]}, end)
            event = collector.enrich_capture(data, tape)["events"][0]
            bucket = next(item for item in event["buckets"] if item["id"] == ticket["instrument"])
            info = (predict(event, at) or {}).get(bucket["id"])
            base = trade._contract(event, bucket, {})
            reviewed = trade._side(data, base, event, bucket, {}, book, info, ticket["side"], at,
                                   trade.order_size(ticket.get("budget") or trade.DEFAULT_BUDGET), False, False,
                                   detail=False)
            if (reviewed["blocker"] or any(reviewed.get(key) != ticket.get(key) for key in ("order", "probability", "quote_at"))
                    or ticket.get("model_vintage") and reviewed["model_vintage"] != ticket["model_vintage"]):
                entry["outcome"] = "Exact preview could not be reproduced"
                entry["window_state"] = "unobserved"
                continue
            entry.update(_check(event, bucket, bucket["books"], reviewed, book, info, predict, end))
    fields = ("eligible_previews", "eligible_rechecks", "incomplete", "unobserved", "missing", "failed")
    by_venue = {venue: Counter(dict.fromkeys(fields, 0)) for venue in venue_archive.VENUES}
    for item in results:
        counts = by_venue[item["venue"]]
        counts["eligible_previews"] += 1
        state = item["window_state"]
        counts["eligible_rechecks" if state == "eligible" else state] += 1
    return {"through": iso(through), "denominator": "sampled eligible exact previews",
            "by_venue": by_venue, "outcomes": dict(Counter(item["outcome"] for item in results)), "cases": results}


def _read_tape(data, start, through):
    floor = start - timedelta(seconds=trade.MAX_QUOTE_AGE + collector.BOOK_INTERVAL)
    directory = data / "captures"
    tape = capture.load_captures(directory, since=floor, through=through, versioned_windows=True)
    if not tape:
        raise ValueError("No retained Polymarket US captures in the specified window")
    return collector.enrich_capture(data, strategy_native_capture.with_listing_evidence(data, tape, through))


def _check(event, bucket, books, ticket, book, info, predict, through):
    at = parse_time(ticket["at"])
    due, deadline = trade.recheck_window(at, ticket.get("closes_at"), False)
    limit = min(through, deadline)
    tape = [dict(book), *(dict(row) for row in books if
                           parse_time(book["at"]) < parse_time(row["at"]) <= limit and
                           (not row.get("available_at") or parse_time(row["available_at"]) <= through))]
    mark_book_progress(tape)
    trial = paper_trial(
        {"events": [{**event, "buckets": [{**bucket, "books": tape}]}]},
        trade_orders.reviewed_probability(ticket["instrument"], at, info, predict),
        as_of=through, reaction_seconds=trade.REACTION_SECONDS, order_size=ticket["order"]["max_loss"],
        freshness=trade.FRESHNESS, selection=trade_orders._reviewed_selection(ticket, book, False),
        clock_basis=paper_eligibility.APP_CLOCK,
    )
    attempt = next(iter(trial["attempts"]), None)
    if not attempt:
        return {"outcome": next((r["reason"] for r in trial["signal_rejections"]), "Signal could not be reproduced"),
                "eligible_recheck": False, "window_state": "unobserved", "gaps": {}}
    state = ("eligible" if attempt["reaction_at"] else "incomplete" if deadline > through else
             "failed" if any(parse_time(row["at"]) >= due for row in tape[1:]) else "missing")
    return {"outcome": attempt["result"], "eligible_recheck": bool(attempt["reaction_at"]), "window_state": state,
            "due_at": iso(due), "deadline_at": iso(deadline), "observation_cutoff": iso(through),
            "at": attempt["reaction_at"], "gaps": attempt.get("recheck_gaps", {}),
            "evidence": attempt.get("recheck_evidence"), "rejections": attempt["recheck_rejections"]}


def exact(data, selected, at, through, predict, *, budget=trade.DEFAULT_BUDGET):
    venue, instrument, _ = selected
    floor = at - timedelta(seconds=trade.MAX_QUOTE_AGE + collector.BOOK_INTERVAL)
    if venue == trade.POLY:
        record = capture.load_captures(data / "captures", since=floor, through=through, instrument=instrument,
                                       versioned_windows=True)
        events = (record or {}).get("events", [])
        if not events:
            with venue_archive.connect(data) as db:
                row = db.execute("SELECT family FROM markets WHERE venue=? AND instrument_id=?", (venue, instrument)).fetchone()
            if row and row["family"] in ("daily-low", "daily-rain"):
                events, _ = strategy_native_capture.polymarket_events(data, through, since=floor, family=row["family"])
        record = strategy_native_capture.with_listing_evidence(data, {"kind": "live", "events": events}, through)
        record = collector.enrich_capture(data, record)
    else:
        record = None
    rows, witness = trade.market_rows(data, record, predict, at, selected=selected, budget=budget)
    result = {"at": iso(at), "through": iso(through), "budget": budget, "readiness_basis": trade.READINESS_BASIS,
              "venue": venue, "instrument": instrument, "side": selected[2], "ticket": next(iter(rows), None)}
    if not rows:
        result["recheck"] = {"window_state": "unobserved", "outcome": "Exact listing and ticket unavailable"}
    elif rows[0]["blocker"]:
        result["recheck"] = rows[0]["source_trace"]["recheck"]
    else:
        event, bucket, book, info = witness
        result["recheck"] = _check(event, bucket, bucket["books"], rows[0], book, info, predict, through)
    return result


def replay(data, start, through, predict, *, preview_seconds):
    """Compare both board policies at the same completed capture clocks."""
    tape = _read_tape(data, start, through)
    stats = {name: {"stages": Counter(), "board_blockers": Counter(), "preview_blockers": Counter(),
                    "recheck_outcomes": Counter(), "recheck_gaps": Counter()}
             for name in ("latest-receipt", trade.READINESS_BASIS)}
    disagreements = []
    for event in tape["events"]:
        estimates = {}

        def probabilities(event, at):
            if at not in estimates:
                estimates[at] = predict(event, at) or {}
            return estimates[at]

        for bucket in event["buckets"]:
            base = trade._contract(event, bucket, {})
            if not base["paper_supported"]:
                continue
            receipts = sorted((parse_time(row["available_at"]), row["at"], index, row)
                              for index, row in enumerate(bucket["books"]) if row.get("available_at"))
            arrivals = [at for at, *_ in receipts]
            all_books = [row for *_, row in receipts]
            times = sorted({at for at in arrivals if start <= at < through})

            def on_side(side, at, keep_origin):
                first = bisect_right(arrivals, at - timedelta(seconds=trade.MAX_QUOTE_AGE + collector.BOOK_INTERVAL))
                available = all_books[first:bisect_right(arrivals, at)]
                latest = _latest_book(available, at)
                origin = trade.origin_witness(available, at, latest) if keep_origin else latest
                info = probabilities(event, at).get(bucket["id"])
                result = trade._side(data, base, event, bucket, {}, origin, info, side, at,
                                     trade.DEFAULT_BUDGET, False, False, detail=False,
                                     newer_book=latest if origin is not latest else None)
                return result, origin, info

            for at in times:
                preview_at = at + timedelta(seconds=preview_seconds)
                for side in trade.SIDES:
                    results = []
                    for name, counts in stats.items():
                        keep_origin = name == trade.READINESS_BASIS
                        board, _, _ = on_side(side, at, keep_origin)
                        stages = counts["stages"]
                        stages["candidate"] += 1
                        stages["open_sides"] += int(board["market_open"])
                        if board["market_open"] and board["posted"]:
                            stages["posted"] += 1
                            stages["displayed_ladders"] += int(board["posted"]["kind"] == "Book offer")
                        if reason := board["blocker"]:
                            counts["board_blockers"][reason] += 1
                            results.append({"board": reason})
                            continue
                        stages["ready"] += 1
                        if preview_at > through:
                            stages["preview_outside_window"] += 1
                            results.append({"board": "Ready", "preview": "Outside observation window"})
                            continue
                        ticket, book, info = on_side(side, preview_at, keep_origin)
                        result = {"board": "Ready", "preview": ticket["blocker"] or "Ready",
                                  "quote_at": ticket["quote_at"], "observed_receipt_at": ticket["observed_receipt_at"]}
                        results.append(result)
                        if ticket["blocker"]:
                            counts["preview_blockers"][ticket["blocker"]] += 1
                            continue
                        stages["eligible_preview"] += 1
                        stages["same_preview" if board["review_key"] == ticket["review_key"] else "revised_preview"] += 1
                        enough_time = preview_at + timedelta(seconds=trade.REACTION_SECONDS + trade.RECHECK_LAG
                                                            + collector.BOOK_INTERVAL)
                        if enough_time > through:
                            stages["recheck_outside_window"] += 1
                            continue
                        stages["complete_recheck_window"] += 1
                        check = _check(event, bucket, all_books, ticket, book, info, probabilities, through)
                        counts["recheck_outcomes"][check["outcome"]] += 1
                        counts["recheck_gaps"].update(check["gaps"])
                        stages["eligible_recheck"] += int(check["eligible_recheck"])
                        stages["paper_fill"] += int(check["outcome"] == "Paper filled")
                        result["recheck"] = check
                    if results[0] != results[1]:
                        disagreements.append({"instrument": bucket["id"], "side": side, "board_at": iso(at),
                                              "preview_at": iso(preview_at), "latest": results[0], "retained": results[1]})
    return {"from": iso(start), "through": iso(through), "preview_seconds": preview_seconds,
            "venue": trade.POLY, "budget": trade.DEFAULT_BUDGET,
            "denominators": {"candidate": "two sides per exact instrument and completed publication in the tape",
                             "posted": "open side observations", "displayed_ladders": "posted side observations",
                             "ready": "open side observations", "eligible_preview": "Ready retest at the preview clock",
                             "eligible_recheck": "eligible previews with a complete observation window"},
            "release_clock": "first completed local capture per exact book",
            "capture_sessions": tape["session_count"], "by_policy": stats, "paired_differences": disagreements}


def sensitivity(data, start, through, predict):
    """Compare small sizing and timing changes on the same Polymarket US tape."""
    tape = _read_tape(data, start, through)
    events = [{**event, "buckets": [{**bucket, "books": [row for row in bucket["books"]
               if start <= parse_time(row["at"]) <= through]} for bucket in event["buckets"]]}
              for event in tape["events"]]
    tape = {**tape, "events": events}
    variants = [("two-sided", size, delay) for size in (10, 25) for delay in (15, 30, 60)]
    variants.append(("two-sided-buffered", 25, trade.REACTION_SECONDS))
    forecasts = {}

    def cached(event, at):
        key = event["id"], at
        if key not in forecasts:
            forecasts[key] = predict(event, at) or {}
        return forecasts[key]

    results = []
    for strategy, budget, delay in variants:
        rule = StrategyRule(strategy, trade.FRESHNESS, 0, trade.EXCHANGE_AGE_SECONDS)
        outcome = paper_trial(tape, cached, rules=rule, as_of=through, order_size=budget,
                              reaction_seconds=delay, max_recheck_lag=trade.RECHECK_LAG,
                              clock_basis=paper_eligibility.APP_CLOCK)
        results.append({"strategy": strategy, "budget": budget, "delay": delay,
                        **{key: outcome[key] for key in ("signals", "rechecked", "persisted", "fills", "settled", "coverage")},
                        "outcomes": dict(Counter(row["result"] for row in outcome["attempts"])),
                        "eligibility_rejections": outcome["eligibility_rejections"],
                        "positions": outcome["positions"]})
    return {"from": iso(start), "through": iso(through), "clock_basis": paper_eligibility.APP_CLOCK, "variants": results}


def main():
    from . import server

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("sample", "observed", "exact", "replay", "sensitivity"))
    parser.add_argument("--observations", type=Path)
    parser.add_argument("--data", type=Path, default=server.DATA)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--venue", choices=venue_archive.VENUES)
    parser.add_argument("--instrument")
    parser.add_argument("--side", choices=trade.SIDES)
    parser.add_argument("--budget", default=trade.DEFAULT_BUDGET)
    parser.add_argument("--start")
    parser.add_argument("--through")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    data = args.data
    server.DATA = data
    if args.kind == "sample":
        sample = sample_local_board(args.port, budget=trade.order_size(args.budget))
        with args.output.open("a") as log:
            log.write(json.dumps(sample, allow_nan=False) + "\n")
        print(f"{args.output} · {sample['sample_started']} · {len(sample['tickets'])} sampled exact tickets")
        return
    if args.kind == "observed":
        if not args.observations:
            parser.error("Observed measurement requires --observations")
        since = parse_time(args.start) if args.start else None
        report = observed_funnel(args.observations, parse_time(args.through) if args.through else None, since=since)
        if args.through:
            report["sampled_rechecks"] = sampled_rechecks(data, args.observations, parse_time(args.through),
                                                         server.backtest_probability_lookup(), since=since)
    else:
        if not args.start or not args.through:
            parser.error("Provide --start and --through")
        start, through = parse_time(args.start), parse_time(args.through)
        if start > through or start == through and args.kind != "exact":
            parser.error("Start must precede through, or equal it for an exact ticket")
        lookup = server.backtest_probability_lookup()
        if args.kind == "replay":
            report = replay(data, start, through, lookup, preview_seconds=5)
        elif args.kind == "exact":
            if not args.venue or not args.instrument or not args.side:
                parser.error("Exact measurement requires --venue, --instrument and --side")
            selected = trade.identity(args.venue, args.instrument, args.side)
            report = exact(data, selected, start, through, lookup, budget=trade.order_size(args.budget))
        else:
            report = sensitivity(data, start, through, lookup)
    body = json.dumps(report, indent=2, allow_nan=False) + "\n"
    args.output.write_text(body)
    print(f"{args.output} · SHA-256 {sha256(body.encode()).hexdigest()}")


if __name__ == "__main__":
    main()
