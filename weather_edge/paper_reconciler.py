"""Run the local paper ledger worker with one listener owner per data root."""

from contextlib import contextmanager
from fcntl import LOCK_EX, LOCK_NB, flock
import logging
import os
from threading import Event, Thread

from . import trade_orders

LOG = logging.getLogger(__name__)
POLL_SECONDS = 2  # Local fallback, including submissions through a standby listener.


@contextmanager
def running(data, clock, fixture_tape, predict_for):
    stop, wake = Event(), Event()
    root = data / "trade-paper"

    def work():
        owner = None
        issues = {}

        def report(key, message):
            if message is None:
                if key in issues:
                    LOG.info("Paper reconciliation recovered: %s", key)
                    issues.pop(key)
            elif issues.get(key) != message:
                issues[key] = message
                LOG.warning("Paper reconciliation will retry %s: %s", key, message)

        try:
            while not stop.is_set():
                try:
                    if owner is None and (root / "orders.sqlite3").is_file():
                        # Keep the lock file in place. Replacing or unlinking
                        # it would allow two different inodes to be locked.
                        with (root / "reconcile.lock").open("ab") as candidate:
                            try:
                                flock(candidate, LOCK_EX | LOCK_NB)
                            except BlockingIOError:
                                pass
                            else:
                                owner = os.dup(candidate.fileno())
                                LOG.info("Paper reconciler owner PID %s: %s", os.getpid(), root)
                    if owner is not None:
                        trade_orders.reconcile(data, clock(), fixture_tape, predict_for, report=report)
                    report("ledger", None)
                except Exception as error:
                    report("ledger", f"{type(error).__name__}: {error}")
                wake.wait(POLL_SECONDS)
                wake.clear()
        finally:
            if owner is not None:
                os.close(owner)

    thread = Thread(target=work, name="paper-reconciler")
    thread.start()
    try:
        yield wake.set
    finally:
        stop.set()
        wake.set()
        thread.join()
