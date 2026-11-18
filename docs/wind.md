# GFS surface wind

The globe's wind forecast reads NOAA GFS instantaneous eastward and northward winds at 10 m above ground. The cache uses the same global 1° regular latitude–longitude grid and forecast run IDs as temperature. It checks the GRIB component flags for Earth-relative axes, validates each field's issue and valid time, and preserves the archive publication time and local retrieval time. Both components come from the same NOAA object. This layer is a model forecast, separate from station anemometer readings. [NOAA GFS archive](https://registry.opendata.aws/noaa-gfs-bdp-pds/), [GRIB wind fields and units](https://www.nco.ncep.noaa.gov/pmb/docs/grib2/grib2_doc/grib2_table4-2-0-2.shtml), [GRIB vector-orientation flag](https://www.nco.ncep.noaa.gov/pmb/docs/grib2/grib2_doc/grib2_table3-3.shtml)

In **Historical replay**, select November 6 and a time after the 1 p.m. EST valid time. Enable **Wind** and probe the North Atlantic near **42°N, 47°W**. The exact 1° cell in the 18 UTC frame, issued at 12 UTC and published at 15:35:05 UTC, had eastward U of +1.1 and northward V of −7.4 m/s. That is **16.7 mph from about 352° true**. Probe values between grid cells can differ with interpolation. Arrowheads point in the direction air moves. Heading readouts use the weather convention and name the direction it came from. [Source NOAA forecast object](https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.20261106/12/atmos/gfs.t12z.pgrb2.1p00.f006)

Cache all available matching wind frames for a replay date or refresh the most recent cached temperature cycle:

```sh
uv run --locked python -m weather_edge.grids replay --start 2026-11-06
uv run --locked python -m weather_edge.wind replay --start 2026-11-06
uv run --locked python -m weather_edge.wind live
```

The ignored cache is `.data/wind/`. Each framebuffer stores interleaved signed little-endian int16 values in tenths of a meter per second. NOAA inventories identify the byte ranges. The downloader uses one request for adjacent records or binds two separate requests to the same ETag. A missing, mislabeled or malformed component leaves a wind gap and does not alter temperature, cloud or market evidence. The globe's replay clock uses archived publication time for frame eligibility and the live clock also respects local ingestion.
