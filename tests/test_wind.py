"""Offline GRIB and inventory checks for the GFS surface-wind layer."""
import base64
from datetime import datetime, timezone
from io import BytesIO
import json
import struct

from eccodes import codes_get_message, codes_grib_new_from_samples, codes_release, codes_set, codes_set_values
import numpy as np
import pytest

from weather_edge import wind

ISSUE = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)


def component(code, value, grid_relative=0):
    handle = codes_grib_new_from_samples("regular_ll_sfc_grib2")
    try:
        for key, argument in {
            "Ni": 360, "Nj": 181, "latitudeOfFirstGridPointInDegrees": 90,
            "latitudeOfLastGridPointInDegrees": -90, "longitudeOfFirstGridPointInDegrees": 0,
            "longitudeOfLastGridPointInDegrees": 359, "iDirectionIncrementInDegrees": 1,
            "jDirectionIncrementInDegrees": 1, "discipline": 0, "parameterCategory": 2,
            "parameterNumber": code, "typeOfLevel": "heightAboveGround", "level": 10,
            "uvRelativeToGrid": grid_relative, "iScansNegatively": 0, "jScansPositively": 0,
            "alternativeRowScanning": 0, "jPointsAreConsecutive": 0, "dataDate": 20261106,
            "dataTime": 1200, "stepType": "instant", "forecastTime": 6,
        }.items():
            codes_set(handle, key, argument)
        codes_set_values(handle, np.full(360 * 181, value))
        return codes_get_message(handle)
    finally:
        codes_release(handle)


def test_requires_exact_instantaneous_pair():
    index = "\n".join([
        "1:0:d=2026110612:GUST:surface:6 hour fcst:",
        "2:10:d=2026110612:UGRD:10 m above ground:0-6 hour ave fcst:",
        "3:20:d=2026110612:UGRD:10 m above ground:6 hour fcst:",
        "4:27:d=2026110612:VGRD:10 m above ground:6 hour fcst:",
        "5:38:d=2026110612:VGRD:850 mb:6 hour fcst:",
    ])
    assert wind.locate_wind(index) == {"UGRD": (20, 26), "VGRD": (27, 37)}
    with pytest.raises(ValueError, match="both"):
        wind.locate_wind(index.replace(":VGRD:10 m", ":VGRD:12 m"))


def test_grib_pair_rejects_grid_relative_and_mixed_forecast_lead():
    parts = {"UGRD": component(2, -1.24), "VGRD": component(3, 7.43)}
    raw = base64.b64decode(wind.wind_data(parts, ISSUE, 6))
    assert len(raw) == 360 * 181 * 4
    assert struct.unpack_from("<hh", raw) == (-12, 74)
    with pytest.raises(ValueError, match="validityTime"):
        wind.wind_data(parts, ISSUE, 3)
    parts["VGRD"] = component(3, 7.43, grid_relative=1)
    with pytest.raises(ValueError, match="uvRelativeToGrid"):
        wind.wind_data(parts, ISSUE, 6)


def test_cache_uses_one_aligned_range_and_preserves_source_clock(tmp_path):
    u = component(2, -1.3)
    v = component(3, 7.0)
    start = 10
    midpoint = start + len(u)
    stop = midpoint + len(v)
    seen = []

    def get(request, timeout):
        if isinstance(request, str):
            assert request.endswith(".idx")
            index = f"1:{start}:UGRD:10 m above ground:6 hour fcst:\n2:{midpoint}:VGRD:10 m above ground:6 hour fcst:\n3:{stop}:PRES:surface:6 hour fcst:"
            return BytesIO(index.encode())
        seen.append(request.get_header("Range"))
        response = BytesIO(u + v)
        response.status = 206
        response.headers = {
            "Content-Range": f"bytes {start}-{stop - 1}/400", "Last-Modified": "Fri, 06 Nov 2026 15:35:05 GMT",
            "ETag": '"exact-object"',
        }
        return response

    path = wind.cache_frame(ISSUE, 6, tmp_path, get=get)
    frame = json.loads(path.read_text())
    assert frame["available_at"] == "2026-11-06T15:35:05+00:00"
    assert frame["valid_at"] == "2026-11-06T18:00:00+00:00"
    assert frame["units"] == "m/s"
    assert frame["source_etag"] == '"exact-object"'
    (tmp_path / "2026110612-f009.json").write_text("{}")
    assert [entry["id"] for entry in wind.manifest(tmp_path)] == [frame["id"]]
    assert seen == [f"bytes={start}-{stop - 1}"]
    assert wind.cache_frame(ISSUE, 6, tmp_path, get=get) == path
    assert len(seen) == 1
