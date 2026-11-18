"""Marine replays use local receipt time and retain corrected measurements."""
from datetime import datetime

from weather_edge.capture import Reply
from weather_edge.marine import SOURCE, collect, snapshot

HEADER = (
    "#STN LAT LON YYYY MM DD hh mm WDIR WSPD GST WVHT DPD APD MWD PRES PTDY ATMP WTMP DEWP VIS TIDE\n"
    "#text deg deg yr mo day hr mn degT m/s m/s m sec sec degT hPa hPa degC degC degC nmi ft\n"
)


def test_native_marine_publication_does_not_backdate_receipt(tmp_path):
    def receipt(at, waves):
        raw = (HEADER + f"44065 40.368 -73.701 2026 11 15 17 40 310 8.0 11.0 {waves} MM MM MM 1019.9 MM 9.0 14.9 1.4 MM MM\n"
               + "44009 0 0 2026 11 16 17 40 310 8.0 11.0 1.5 MM MM MM 1000.0 MM 9.0 14.9 1.4 MM MM\n")
        return Reply(raw.encode(), datetime.fromisoformat(at),
                     {"last-modified": "Sun, 15 Nov 2026 18:00:00 GMT"})

    def request(url):
        assert url == SOURCE
        return receipt("2026-11-15T18:35:00+00:00", 0.7)

    saved = collect(tmp_path, request)
    assert saved["records"] == saved["inserted"] == 1
    assert snapshot(tmp_path, "2026-11-15T18:10:00Z") == []
    before, = snapshot(tmp_path, "2026-11-15T18:35:00Z")
    assert before["station"] == "44065" and before["wave_height_m"] == 0.7
    assert before["measured_at"] == "2026-11-15T17:40:00.000000Z"
    assert before["available_at"] == "2026-11-15T18:35:00.000000Z"

    assert collect(tmp_path, request)["inserted"] == 0
    collect(tmp_path, lambda _: receipt("2026-11-15T18:40:00Z", 0.8))
    later, = snapshot(tmp_path, "2026-11-15T18:45:00Z")
    assert later["wave_height_m"] == 0.8
    assert snapshot(tmp_path, "2026-11-15T18:38:00Z")[0]["wave_height_m"] == 0.7


def test_wave_partitions_have_their_own_local_receipt(tmp_path, monkeypatch):
    from weather_edge import wave_components

    parsed = {"available_at": "2026-11-15T15:33:21Z", "ingested_at": "2026-11-15T22:04:02Z",
              "sha256": "components", "source": "original NOAA object"}
    monkeypatch.setattr(wave_components, "metadata", lambda *_: parsed)
    monkeypatch.setattr(wave_components, "fields", lambda *_: {
        "wind_sea_m": 2.18, "primary_swell_m": 4.20, "secondary_swell_m": 1.27, "tertiary_swell_m": None})
    monkeypatch.setattr(wave_components.waves, "sample_grid", lambda value, *_: (value, (46.25, -124.50)))
    args = (tmp_path, {}, 46.147, -124.485)
    assert wave_components.point(*args, "2026-11-15T22:04:01Z", local_receipt=True) is None
    assert wave_components.point(*args, "2026-11-15T18:00:00Z")["primary_swell_m"] == 4.20
    assert wave_components.point(*args, "2026-11-15T22:04:02Z", local_receipt=True)["tertiary_swell_m"] is None
