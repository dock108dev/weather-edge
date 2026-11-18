# NOAA cloud coverage

The cloud layer uses the NOAA GFS instantaneous total cloud cover field (`TCDC`, entire atmosphere). The archive inventory also lists period-average cloud coverage. The downloader selects the instantaneous record and verifies the GRIB field, forecast timestamps, grid orientation, and 0% to 100% value range before caching. [NOAA GFS field inventory](https://www.nco.ncep.noaa.gov/pmb/products/gfs/gfs.t00z.pgrb2.0p25.f003.shtml)

Run `uv run python -m weather_edge.clouds replay` after preparing the replay temperature grids. This matches the cloud and temperature frame IDs by NOAA run and forecast lead. The verified Nov. 5 replay window uses 29 frames drawn from the Nov. 4–5 runs. Each cloud frame uses its own archive publication timestamp and retains the issue and valid timestamps. This layer shows modeled cloud coverage. [NOAA GFS public archive](https://registry.opendata.aws/noaa-gfs-bdp-pds/)

The optional `uv run python -m weather_edge.clouds live` command downloads matching weather grids from a recent GFS run. It only prepares weather caches. Replay and live cloud frames live under ignored `.data/clouds/`. The format is an unsigned byte percentage for each point, packed row-by-row west to east from 0° longitude and north to south from 90° latitude.
