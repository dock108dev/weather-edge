import json
from datetime import datetime, timezone
from io import BytesIO

import pytest

from weather_edge import clouds


INDEX = """1:0:d=2026110512:HCDC:high cloud layer:6 hour fcst:
2:5:d=2026110512:TCDC:entire atmosphere:0-6 hour ave fcst:
3:10:d=2026110512:TCDC:entire atmosphere:6 hour fcst:
4:16:d=2026110512:TMP:2 m above ground:6 hour fcst:
"""
ISSUE = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)


class FakeResponse(BytesIO):
    def __init__(self, content, status=200, headers=None):
        super().__init__(content)
        self.status = status
        self.headers = headers or {}


def test_cloud_selector_prefers_instantaneous_entire_atmosphere_record():
    assert clouds.locate_cloud(INDEX) == (10, 15)
    with pytest.raises(ValueError, match="instantaneous"):
        clouds.locate_cloud(INDEX.replace("entire atmosphere:6 hour fcst", "entire atmosphere:6 hour ave fcst"))


def test_cloud_frame_uses_its_own_archive_publication(tmp_path, monkeypatch):
    monkeypatch.setattr(clouds, "cloud_data", lambda payload, issue, lead: "A" * 86880)

    def get(request, timeout):
        if isinstance(request, str):
            assert request.endswith(".idx")
            return FakeResponse(INDEX.encode())
        assert request.get_header("Range") == "bytes=10-15"
        return FakeResponse(b"values", status=206, headers={
            "Content-Range": "bytes 10-15/100", "Last-Modified": "Thu, 05 Nov 2026 15:34:30 GMT"})

    cached = clouds.cache_frame(ISSUE, 6, tmp_path, get=get)
    record = json.loads(cached.read_text())
    assert record["id"] == "2026110512-f006"
    assert record["available_at"] == "2026-11-05T15:34:30+00:00"
    assert record["valid_at"] == "2026-11-05T18:00:00+00:00"
    assert record["format"] == "u8-percent"
    assert "ingested_at" in record
    assert "pixels" not in clouds.manifest(tmp_path)[0]
