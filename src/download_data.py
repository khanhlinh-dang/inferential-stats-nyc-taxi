"""Download all raw data used in this project into data/raw/.

Sources
-------
1. NYC TLC Yellow Taxi trip records, Jan-Dec 2024 (12 monthly Parquet files, ~50 MB each)
   https://www.nyc.gov/site/tlc/about/tlc-trip-record-data.page
2. Open-Meteo Historical Weather API (ERA5 reanalysis), hourly, Central Park grid point
   https://open-meteo.com/en/docs/historical-weather-api
3. NOAA GHCN-Daily, Central Park station USW00094728 (ground-truth daily precipitation,
   used as a robustness check on the reanalysis data)
   https://www.ncei.noaa.gov/access/services/data/v1

Usage:  python src/download_data.py [--months 1 2 3 ...]
"""
import argparse
from pathlib import Path

import pandas as pd
import requests

RAW = Path(__file__).resolve().parents[1] / "data" / "raw"
TLC_URL = "https://d37ci6vzurychx.cloudfront.net/trip-data/yellow_tripdata_2024-{m:02d}.parquet"
LAT, LON = 40.78, -73.97  # Central Park
START, END = "2024-01-01", "2024-12-31"


def download_file(url: str, dest: Path) -> None:
    if dest.exists() and dest.stat().st_size > 0:
        print(f"[skip] {dest.name} already exists")
        return
    print(f"[get ] {url}")
    tmp = dest.with_suffix(dest.suffix + ".part")
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)
    tmp.rename(dest)


def download_taxi(months) -> None:
    for m in months:
        download_file(TLC_URL.format(m=m), RAW / f"yellow_tripdata_2024-{m:02d}.parquet")


def download_weather_hourly() -> None:
    dest = RAW / "weather_hourly_2024.csv"
    if dest.exists():
        print(f"[skip] {dest.name} already exists")
        return
    params = {
        "latitude": LAT, "longitude": LON, "start_date": START, "end_date": END,
        "hourly": "precipitation,rain,snowfall,temperature_2m",
        "timezone": "America/New_York",
    }
    r = requests.get("https://archive-api.open-meteo.com/v1/archive", params=params, timeout=60)
    r.raise_for_status()
    pd.DataFrame(r.json()["hourly"]).to_csv(dest, index=False)
    print(f"[ok  ] {dest.name}")


def download_weather_noaa_daily() -> None:
    dest = RAW / "noaa_central_park_daily_2024.csv"
    if dest.exists():
        print(f"[skip] {dest.name} already exists")
        return
    params = {
        "dataset": "daily-summaries", "stations": "USW00094728",
        "startDate": START, "endDate": END,
        "dataTypes": "PRCP,SNOW,TMAX,TMIN", "units": "metric", "format": "csv",
    }
    r = requests.get("https://www.ncei.noaa.gov/access/services/data/v1", params=params, timeout=60)
    r.raise_for_status()
    dest.write_text(r.text)
    print(f"[ok  ] {dest.name}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=int, nargs="+", default=list(range(1, 13)))
    args = ap.parse_args()
    RAW.mkdir(parents=True, exist_ok=True)
    download_weather_hourly()
    download_weather_noaa_daily()
    download_taxi(args.months)
