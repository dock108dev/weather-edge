"""Decode one named element in a NOAA GHCN Daily station file."""

from calendar import monthrange
from datetime import date


def daily_entries(body, station, element):
    for line in body.decode("ascii").splitlines():
        if line[:11] != station or line[17:21] != element:
            continue
        year, month = int(line[11:15]), int(line[15:17])
        if len(line) < 269:
            raise ValueError(f"Incomplete {element} GHCN record for {station} in {year}-{month:02}")
        for index in range(monthrange(year, month)[1]):
            start = 21 + index * 8
            value = line[start:start + 5]
            measure, quality, source = line[start + 5:start + 8]
            yield date(year, month, index + 1), int(value), measure, quality, source
