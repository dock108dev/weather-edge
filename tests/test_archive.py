"""Regression checks for archive replay and timing boundaries."""
import base64
from datetime import date, timedelta
import json

from weather_edge import archive, grids
from weather_edge.history import REPLAY_DAY, parse_observations
from weather_edge.nbm import climate_window


def native(series, day):
    return archive._native_station_day(archive._spec(series, day))


def put_event(root, event):
    target = root / "archive" / event["date"] / f"{event['station']}.json"
    archive._write_json(target, event)
    return target


def test_selected_days_preserve_demo_and_skip_corrupt_station(tmp_path):
    new = date(2026, 11, 6)
    demo = native("nyc", REPLAY_DAY)
    demo["title"] = "Original replay"
    (tmp_path / "replay.json").write_text(json.dumps({"events": [demo]}))
    put_event(tmp_path, native("nyc", REPLAY_DAY))
    put_event(tmp_path, native("nyc", new))
    corrupt = put_event(tmp_path, native("miami", new))
    corrupt.write_text('{"observations":')
    sandbox = archive.load_day(tmp_path, str(new))
    assert [event["station"] for event in sandbox["events"]] == ["KNYC"]
    assert sandbox["events"][0]["weather_only"]
    assert sandbox["issues"][0]["source"] == "KMIA.json"
    assert archive.load_day(tmp_path)["events"][0]["title"] == "Original replay"
    index = archive.catalog(tmp_path)
    assert [entry["date"] for entry in index["days"]] == [str(new), str(REPLAY_DAY)]
    assert index["issues"][0]["date"] == str(new)


def test_unmatched_readings_only_get_opt_in_disclosed_proxy():
    starts, ends = climate_window(REPLAY_DAY, -5)
    data = ("station,valid,tmpf,metar,dwpf,sknt,drct,p01i\n"
            "NYC,2026-11-05 05:51,65,KNYC 050551Z,48,4,260,\n")
    assert parse_observations(data, "KNYC", starts, ends, {}) == ([], 1)
    result, n = parse_observations(data, "KNYC", starts, ends, {}, delay_proxy=timedelta(minutes=10))
    assert n == 1
    assert result[0]["available_at"] == "2026-11-05T06:01:00Z"
    assert result[0]["timing_proxy"]
    assert result[0]["wind_knots"] == 4
    assert result[0]["dewpoint_f"] == 48


def test_delayed_minute_observations_lack_original_receipt():
    starts, ends = climate_window(REPLAY_DAY, -5)
    sample = ("station,valid(UTC),tmpf,dwpf\n"
              "NYC,2026-11-05 04:59,56,50\n"
              "NYC,2026-11-05 05:00,57,50\n"
              "NYC,2026-11-05 05:01,M,M\n"
              "NYC,2026-11-05 05:02,59,M\n")
    points = archive._parse_minute_trace(sample, "KNYC", starts, ends)
    assert points == [
        {"at": "2026-11-05T05:00:00Z", "temp_f": 57, "dewpoint_f": 50},
        {"at": "2026-11-05T05:02:00Z", "temp_f": 59},
    ]


def test_delayed_exchange_report_fallback_is_per_station(tmp_path, monkeypatch):
    rows = [
        {"id": slug, "settlement": {"winning_bucket": slug}}
        for slug in ("first", "second")
    ]
    calls = []
    def reports(rows, cache, refresh, *, business_day_offset, on_missing):
        calls.append([row["id"] for row in rows])
        for row in rows:
            if row["id"] == "first" or business_day_offset == 2:
                row["settlement"]["payout_available_at"] = f"2026-11-0{8 + business_day_offset - 1}T00:00:00Z"
            else:
                on_missing(row, "Missing winning payout")
    monkeypatch.setattr(archive.history, "_report_evidence", reports)
    archive._attach_payout_publication(rows, tmp_path, False)
    assert calls == [["first", "second"], ["second"]]
    assert rows[0]["settlement"]["payout_available_at"] < rows[1]["settlement"]["payout_available_at"]


def test_grid_manifest_skips_corrupt_file_and_selects_by_date(tmp_path):
    frame = {
        "format": "u8-percent", "width": 360, "height": 181,
        "id": "2026110812-f006",
        "issued_at": "2026-11-08T12:00:00Z",
        "available_at": "2026-11-08T16:00:00Z",
        "valid_at": "2026-11-08T18:00:00Z",
        "pixels": base64.b64encode(bytes(360 * 181)).decode(),
    }
    (tmp_path / "2026110812-f003.json").write_text("{}")
    (tmp_path / "2026110812-f006.json").write_text(json.dumps(frame))
    manifest = grids.manifest(tmp_path)
    assert len(manifest) == 1 and "pixels" not in manifest[0]
    assert grids.replay_frames(manifest, "2026-11-08") == manifest
    assert grids.replay_frames(manifest, "2026-11-06") == []
