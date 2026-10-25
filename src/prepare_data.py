"""Clean the 2024 Yellow Taxi trips and attach weather at the pickup hour.

Reads   data/raw/yellow_tripdata_2024-MM.parquet, weather_hourly_2024.csv, noaa_central_park_daily_2024.csv
Writes  data/processed/trips_2024.parquet   one row per cleaned card-paid trip
        data/processed/daily_2024.parquet   one row per day (aggregated outcomes + weather)
        data/processed/cleaning_log.csv     rows remaining after each cleaning step

Usage:  python src/prepare_data.py
"""
from pathlib import Path

import pandas as pd
from pandas.tseries.holiday import USFederalHolidayCalendar

ROOT = Path(__file__).resolve().parents[1]
RAW, OUT = ROOT / "data" / "raw", ROOT / "data" / "processed"

COLS = ["tpep_pickup_datetime", "tpep_dropoff_datetime", "passenger_count", "trip_distance",
        "RatecodeID", "PULocationID", "DOLocationID", "payment_type", "fare_amount", "tip_amount",
        "total_amount"]
AIRPORT_ZONES = {1, 132, 138}  # Newark, JFK, LaGuardia (TLC taxi zone IDs)
HOLIDAYS = USFederalHolidayCalendar().holidays("2024-01-01", "2024-12-31")


def clean_month(m: int, log: list) -> pd.DataFrame:
    df = pd.read_parquet(RAW / f"yellow_tripdata_2024-{m:02d}.parquet", columns=COLS)
    start = pd.Timestamp(2024, m, 1)
    steps = [
        ("raw", None),
        ("pickup within month", lambda d: (d.tpep_pickup_datetime >= start)
                                          & (d.tpep_pickup_datetime < start + pd.offsets.MonthBegin(1))),
        ("credit card payment", lambda d: d.payment_type == 1),
        ("standard or JFK rate", lambda d: d.RatecodeID.isin([1, 2])),
        ("fare >= $3", lambda d: d.fare_amount >= 3),
        ("0 < distance <= 100 mi", lambda d: (d.trip_distance > 0) & (d.trip_distance <= 100)),
        ("1 <= duration <= 180 min", None),
        ("0 <= tip <= 100% of pre-tip total", lambda d: (d.tip_amount >= 0)
         & (d.tip_amount <= d.total_amount - d.tip_amount)),
    ]
    for name, cond in steps:
        if name == "1 <= duration <= 180 min":
            dur = (df.tpep_dropoff_datetime - df.tpep_pickup_datetime).dt.total_seconds() / 60
            df = df[(dur >= 1) & (dur <= 180)]
        elif cond is not None:
            df = df[cond(df)]
        log.append({"month": m, "step": name, "rows": len(df)})

    out = pd.DataFrame({
        "pickup": df.tpep_pickup_datetime.values,
        "fare": df.fare_amount.astype("float32").values,
        "tip": df.tip_amount.astype("float32").values,
        "distance_mi": df.trip_distance.astype("float32").values,
        "duration_min": ((df.tpep_dropoff_datetime - df.tpep_pickup_datetime)
                         .dt.total_seconds() / 60).astype("float32").values,
        "passengers": df.passenger_count.fillna(1).clip(0, 6).astype("int8").values,
        "jfk_flat_fare": (df.RatecodeID == 2).values,
        "airport": (df.PULocationID.isin(AIRPORT_ZONES) | df.DOLocationID.isin(AIRPORT_ZONES)).values,
    })
    # The payment screen computes suggested tips on the pre-tip total (fare + surcharges + tolls),
    # so the tip rate is measured on that base, which is what the passenger actually sees.
    pre_tip = (df.total_amount - df.tip_amount).astype("float32").values
    out["pre_tip_total"] = pre_tip
    out["tip_pct"] = (100 * out.tip / pre_tip).astype("float32")
    out["tip_pct_fare"] = (100 * out.tip / out.fare).astype("float32")
    out["tipped"] = out.tip > 0
    # tip equals exactly 20% of the pre-tip total, to the cent (the most common preset button)
    out["tip_20"] = (out.tip - (0.2 * out.pre_tip_total).round(2)).abs() <= 0.011
    return out


def add_time_features(df: pd.DataFrame) -> pd.DataFrame:
    df["date"] = df.pickup.dt.normalize()
    df["hour"] = df.pickup.dt.hour.astype("int8")
    df["dow"] = df.pickup.dt.dayofweek.astype("int8")  # 0 = Monday
    df["month"] = df.pickup.dt.month.astype("int8")
    df["holiday"] = df.date.isin(HOLIDAYS)
    return df


def load_weather() -> tuple[pd.DataFrame, pd.DataFrame]:
    hourly = pd.read_csv(RAW / "weather_hourly_2024.csv", parse_dates=["time"])
    hourly = hourly.rename(columns={"time": "hour_ts", "precipitation": "precip_mm",
                                    "temperature_2m": "temp_c"})[["hour_ts", "precip_mm", "temp_c"]]
    noaa = pd.read_csv(RAW / "noaa_central_park_daily_2024.csv", parse_dates=["DATE"])
    daily = noaa.rename(columns={"DATE": "date", "PRCP": "noaa_prcp_mm", "SNOW": "noaa_snow_mm",
                                 "TMAX": "noaa_tmax_c", "TMIN": "noaa_tmin_c"}).drop(columns="STATION")
    era5_daily = (hourly.assign(date=hourly.hour_ts.dt.normalize())
                  .groupby("date").agg(era5_prcp_mm=("precip_mm", "sum"), era5_temp_c=("temp_c", "mean"),
                                       era5_rain_hours=("precip_mm", lambda s: (s >= 0.25).sum()))
                  .reset_index())
    return hourly, daily.merge(era5_daily, on="date", how="left")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    hourly, daily_weather = load_weather()
    log: list = []
    trips, unmatched = [], 0
    for m in range(1, 13):  # month by month to keep memory use low
        print(f"cleaning month {m:02d} ...")
        t = add_time_features(clean_month(m, log))
        t["hour_ts"] = t.pickup.dt.floor("h")
        t = t.merge(hourly, on="hour_ts", how="left").drop(columns="hour_ts")
        t = t.merge(daily_weather[["date", "noaa_prcp_mm"]], on="date", how="left")
        unmatched += t.precip_mm.isna().sum()
        trips.append(t)
    trips = pd.concat(trips, ignore_index=True)
    print(f"trips without an hourly weather match: {unmatched:,}")
    trips.to_parquet(OUT / "trips_2024.parquet", index=False)

    daily = (trips.groupby("date")
             .agg(trips=("tip", "size"), mean_tip=("tip", "mean"), mean_tip_pct=("tip_pct", "mean"),
                  share_tipped=("tipped", "mean"), share_tip_20=("tip_20", "mean"),
                  mean_fare=("fare", "mean"), mean_duration=("duration_min", "mean"),
                  mean_distance=("distance_mi", "mean"))
             .reset_index()
             .merge(daily_weather, on="date", how="left"))
    daily["dow"] = daily.date.dt.dayofweek
    daily["month"] = daily.date.dt.month
    daily["holiday"] = daily.date.isin(HOLIDAYS)
    daily.to_parquet(OUT / "daily_2024.parquet", index=False)

    log = pd.DataFrame(log).groupby("step", sort=False).rows.sum().reset_index()
    log["share_of_raw"] = (log.rows / log.rows.iloc[0]).round(4)
    log.to_csv(OUT / "cleaning_log.csv", index=False)
    print(log.to_string(index=False))
    print(f"saved {len(trips):,} trips and {len(daily)} days")


if __name__ == "__main__":
    main()
