"""Generate signals and delayed-book candidates using shared paper evidence."""

from bisect import bisect_left, bisect_right
from collections import Counter
from datetime import timedelta

from . import paper_eligibility as evidence
from .market import MAX_QUOTE_AGE, _ladder, _price, decimal, iso, parse_time
from .strategy_rules import EXCHANGE_AGE_SECONDS, validate_freshness


def candidates(capture, predict, *, through, retry, lag, budget, rules, fee_at, selection, clock_basis, freshness):
    """Check the exact side at the decision and at the first eligible delayed image."""
    fixture = capture.get("kind") == "fixture"
    freshness = (rules.freshness if rules else freshness) or ("legacy" if fixture else "origin-confirmed")
    validate_freshness(freshness)
    max_exchange_age = None if fixture else EXCHANGE_AGE_SECONDS
    if selection:
        max_exchange_age = selection.get("max_exchange_age", max_exchange_age)
    elif rules and rules.max_exchange_age is not None:
        max_exchange_age = rules.max_exchange_age
    price_limit = decimal(selection["limit_price"]) if selection else None
    quantity_limit = decimal(selection["quantity"]) if selection else None
    predictions, signals, fills, rejected = {}, [], [], []
    receipts, station_days = [], set()
    usable = modeled = 0
    phase_counts = {"signal": Counter(), "recheck": Counter()}

    def prediction(event, bucket, clock):
        key = event["id"], clock
        if key not in predictions:
            predictions[key] = predict(event, clock) or {}
        return predictions[key].get(bucket["id"])

    def pricing_event(event, qualification):
        listing = qualification.listing
        if not listing:
            return event
        return {**event, "ends_at": qualification.closes_at,
                "settles_at": listing.get("expected_expiration_at") or listing["closed_at"]}

    def rejection(phase, qualification, clock, book, side, *, reason=None, category=None):
        row = {**qualification.detail(phase, clock, book), "event_id": event["id"], "bucket_id": bucket["id"],
               "station": event["station"], "date": event["date"], "side": side}
        if reason:
            row.update(reason=reason, category=category)
        phase_counts[phase][row["reason"]] += 1
        levels = _ladder(book, side) if book else []
        if levels:
            row["posted_price"] = float(levels[0][0])
        return row

    for event in capture.get("events", []):
        for bucket in event.get("buckets", []):
            if not bucket.get("quantity_step") or (selection and bucket["id"] != selection["instrument"]):
                continue
            books = sorted((b for b in bucket.get("books", []) if parse_time(b["at"]) <= through),
                           key=lambda b: parse_time(b["at"]))
            times = [parse_time(book["at"]) for book in books]
            clocks = [evidence.book_clock(book, clock_basis) for book in books]
            receipts.extend(times)
            if books:
                station_days.add((event["station"], event["date"]))
            if selection:
                decision_at = parse_time(selection["at"])
                decisions = [(decision_at, book) for book in books if book["at"] == selection["book_at"]
                             and decision_at <= through]
            else:
                latest = {}
                for received, available, book in zip(times, clocks, books):
                    clock = available or received
                    if clock <= through:
                        latest[clock] = book
                decisions = sorted(latest.items())
            visible = None
            for decision_at, observed in decisions:
                if visible is not None and parse_time(observed["at"]) < parse_time(visible["at"]):
                    continue
                visible = observed
                book = observed
                if not selection and freshness == "origin-confirmed":
                    recent = books[bisect_left(times, decision_at - timedelta(seconds=MAX_QUOTE_AGE)):
                                   bisect_right(times, decision_at)]
                    book = evidence.origin_witness([b for b in recent
                                                   if (clock := evidence.book_clock(b, clock_basis))
                                                   and clock <= decision_at], decision_at, observed)
                due = decision_at + retry
                deadline = parse_time(selection["deadline"]) if selection and selection.get("deadline") else due + lag
                book_usable = book_modeled = False
                for side in (selection["side"],) if selection else ("YES", "NO"):
                    qualification = evidence.qualify(
                        event, bucket, side, book, decision_at, fee_at=fee_at, fee_clock=decision_at,
                        freshness=freshness, clock_basis=clock_basis, fixture=fixture, max_exchange_age=max_exchange_age,
                    )
                    if qualification.reason:
                        rejected.append(rejection("signal", qualification, decision_at, book, side))
                        continue
                    book_usable = True
                    if rules and rules.name == "no-trade":
                        continue
                    info = prediction(event, bucket, decision_at)
                    if not info or info.get("probability") is None or info.get("uncertain"):
                        reason = ("Model probability unavailable" if not info or info.get("probability") is None
                                  else "Uncertain model probability")
                        rejected.append(rejection("signal", qualification, decision_at, book, side, reason=reason, category="model"))
                        continue
                    book_modeled = True
                    quote = _price(pricing_event(event, qualification), bucket, side, book, info, decision_at, budget, 0, quantity_limit,
                                   fee_at=fee_at, rules=rules, limit_price=price_limit, evidence_checked=True)
                    if not quote["ev_dollars"] or quote["ev_dollars"] <= 0:
                        reason = "No positive net EV after fees" if quote.get("quantity") else quote["status"]
                        rejected.append(rejection("signal", qualification, decision_at, book, side, reason=reason, category="edge"))
                        continue
                    if rules and (not rules.considers(info, side) or not rules.qualifies(quote, signal=True)):
                        rejected.append(rejection("signal", qualification, decision_at, book, side,
                                                  reason="Strategy threshold not met", category="strategy"))
                        continue
                    if reason := evidence.reaction_status(qualification, due):
                        rejected.append(rejection("signal", qualification, decision_at, book, side, reason=reason, category="listing"))
                        continue
                    gaps = Counter()
                    rejections = []
                    expires = min(deadline, parse_time(qualification.closes_at)) if qualification.closes_at else deadline
                    attempt = {
                        **quote, "signal_at": iso(decision_at), "reaction_due_at": iso(due),
                        "reaction_deadline_at": iso(deadline), "reaction_at": None, "reaction_quote": None,
                        "reaction_ev_dollars": None,
                        "result": "Missing eligible recheck" if through >= expires else "Awaiting eligible recheck",
                        "signal_evidence": qualification.detail("signal", decision_at, book),
                        "fee_lock_at": iso(decision_at), "recheck_gaps": gaps, "recheck_rejections": rejections,
                    }
                    signals.append(attempt)
                    indexes = range(bisect_left(times, due), bisect_right(times, deadline))
                    for index in sorted(indexes, key=lambda i: clocks[i] or times[i]):
                        recheck = books[index]
                        checked_at = clocks[index] or times[index]
                        check = evidence.qualify(
                            event, bucket, side, recheck, checked_at, fee_at=fee_at, fee_clock=decision_at,
                            freshness=freshness, clock_basis=clock_basis, fixture=fixture, due=due, deadline=deadline,
                            max_exchange_age=max_exchange_age,
                        )
                        if check.reason:
                            row = rejection("recheck", check, checked_at, recheck, side)
                            rejections.append(row)
                            gaps[row["reason"]] += 1
                            continue
                        if checked_at > through:
                            continue
                        current = prediction(event, bucket, checked_at)
                        revised_event = pricing_event(event, check)
                        delayed = _price(revised_event, bucket, side, recheck, current, checked_at, budget, 0,
                                         decimal(quote["quantity"]), fee_at=fee_at, fee_clock=decision_at,
                                         rules=rules, limit_price=price_limit, evidence_checked=True)
                        attempt.update(reaction_at=iso(checked_at), reaction_quote=delayed,
                                       reaction_ev_dollars=delayed["ev_dollars"],
                                       recheck_evidence=check.detail("recheck", checked_at, recheck))
                        if current is None or delayed["probability"] is None or delayed["uncertain"]:
                            attempt["result"] = "Recheck probability unavailable or uncertain"
                        elif not delayed.get("quantity"):
                            moved = selection and _ladder(recheck, side)[0][0] > price_limit
                            attempt["result"] = "Price moved above the reviewed limit" if moved else "Insufficient size at recheck"
                        elif delayed["ev_dollars"] <= 0 or (rules and not rules.qualifies(delayed, signal=False)):
                            attempt["result"] = "Edge did not persist"
                        else:
                            attempt["result"] = "Edge persisted"
                            fills.append((checked_at, -quote["ev_dollars"], decision_at, attempt,
                                          revised_event, bucket, side, recheck, current))
                        break
                    if not attempt["reaction_at"] and through >= expires and rejections:
                        attempt["result"] = rejections[0]["reason"]
                usable += book_usable
                modeled += book_modeled
    coverage = {
        "from": iso(min(receipts)) if receipts else None, "through": iso(max(receipts)) if receipts else None,
        "book_receipts": len(receipts), "eligible_books": usable, "modeled_books": modeled,
        "station_days": len(station_days), "clock_basis": clock_basis,
        "rejected_books": dict(phase_counts["signal"]),
    }
    return signals, fills, coverage, rejected, phase_counts
