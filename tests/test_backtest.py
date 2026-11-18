from datetime import datetime, timezone

from weather_edge import backtest, paper_runs


NOW = datetime(2026, 11, 16, tzinfo=timezone.utc)


def test_fixture_backtest_freezes_fill_and_skipped_recheck(tmp_path):
    request = {"fixture": True, "strategy": "two-sided", "size": 25, "reaction": 5}
    run = backtest.save(tmp_path, request, None, NOW)
    loaded = paper_runs.load_run(backtest.folder(tmp_path, True), run["id"])
    assert loaded == run
    result = paper_runs.verify_run(loaded)
    assert result["fills"] == 1
    assert not backtest.folder(tmp_path, False).exists()
    summary = backtest.describe(loaded)
    skip = next(case for case in summary["cases"] if case["result"] == "Edge did not persist")
    case = backtest.describe(loaded, case=skip["index"])["case"]
    assert case["position"] is None
    assert case["reading"]["available_at"] <= case["attempt"]["signal_at"]
    assert case["attempt"]["reaction_quote"]["entry_price"] == 0.85
    assert case["attempt"]["max_loss"] == 6.21
    assert [book["at"] for book in case["books"]] == [case["attempt"]["signal_at"], case["attempt"]["reaction_at"]]
    assert summary["skipped"] == 3 and summary["awaiting"] == 1 and summary["unresolved_payouts"] == 1


def test_archived_display_samples_never_become_books(tmp_path, monkeypatch):
    day = "2026-11-06"
    prices = [{"at": day + "T20:00:00Z", "yes": 0.95},
              {"at": day + "T20:00:00Z", "available_at": "2026-11-07T06:00:00Z", "yes": 0.99}]
    event = {"date": day, "station": "KNYC", "buckets": [{"prices": prices}]}
    monkeypatch.setattr(backtest.archive, "load_day", lambda *_: {"events": [event]})
    monkeypatch.setattr(backtest.strategy_native_capture, "polymarket_events", lambda *_, **__: ([], []))

    def unexpected(*_):
        raise AssertionError("Display-only evidence was passed to execution")

    request = {"start": day, "end": day, "strategy": "two-sided", "size": 25, "reaction": 30}
    run = backtest.save(tmp_path, request, unexpected, NOW)
    summary = backtest.describe(run)
    assert summary["display_samples"] == 1
    assert summary["fills"] == summary["coverage"]["book_receipts"] == summary["signals"] == 0
    assert summary["cash"] == 500
