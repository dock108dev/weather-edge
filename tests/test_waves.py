import base64

import numpy as np

from weather_edge import waves


def test_native_ocean_sample_can_fill_a_dry_display_cell():
    grid = np.full(waves.NATIVE_SHAPE, np.nan)
    grid[203, 1145] = .89
    height, position = waves.sample_grid(grid, 39.25, -73.75)
    assert height == .89
    assert position == (39.25, -73.75)
    assert waves.sample_grid(grid, 39.25, -73.75, samples_per_degree=1)[0] is None

    grid[204, 1144] = 0
    pixels = np.frombuffer(base64.b64decode(waves._encode(grid)), dtype="<u2").reshape(waves.SHAPE)
    assert pixels[51, 286] == 0
    assert pixels[50, 286] == waves.NO_VALUE


def test_interpolation_wraps_longitude_and_retains_dry_neighbor():
    grid = np.full(waves.NATIVE_SHAPE, np.nan)
    grid[360:362, 0] = [2., 4.]
    grid[360, 1439] = 1.
    neighbors = waves.interpolation_corners(grid, -.125, -.125)
    assert [row["lon"] for row in neighbors] == [-.25, 0, -.25, 0]
    assert [row["height_m"] for row in neighbors] == [1., 2., None, 4.]
    assert [row["weight"] for row in neighbors] == [.25, .25, .25, .25]
