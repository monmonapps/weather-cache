"""Open-Meteo response -> custom schema converter.

Custom schema is defined in WeatherSupport/SW_Design.md §4.1.
The Android client only knows this schema; provider details are
fully encapsulated here so future provider swaps need no app update.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any


SCHEMA_VERSION = 1
HOURLY_LENGTH = 72   # 3 days x 24 hours
DAILY_LENGTH = 14    # today + 13 days
VALID_FOR = timedelta(hours=3)  # matches GitHub Actions cron interval


def transform(
    cell_id: str,
    lat: float,
    lon: float,
    openmeteo_json: dict[str, Any],
    fetched_at: datetime,
) -> dict[str, Any]:
    """Convert Open-Meteo forecast response to our custom schema."""
    current = openmeteo_json.get("current", {})
    hourly_src = openmeteo_json.get("hourly", {})
    daily_src = openmeteo_json.get("daily", {})

    out: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "cell_id": cell_id,
        "lat": lat,
        "lon": lon,
        "fetched_at": _iso_utc(fetched_at),
        "valid_until": _iso_utc(fetched_at + VALID_FOR),
        "timezone": openmeteo_json.get("timezone", "Asia/Tokyo"),
        "current": _transform_current(current),
        "hourly": _transform_hourly(hourly_src),
        "daily": _transform_daily(daily_src),
    }
    return out


def _transform_current(current: dict[str, Any]) -> dict[str, Any]:
    return {
        "temperature": current.get("temperature_2m"),
        "weather_code": current.get("weather_code"),
        "is_day": current.get("is_day"),
    }


def _transform_hourly(hourly: dict[str, Any]) -> list[dict[str, Any]]:
    times = hourly.get("time", []) or []
    n = min(len(times), HOURLY_LENGTH)
    result = []
    for i in range(n):
        result.append({
            "time": times[i],
            "temperature": _get_at(hourly, "temperature_2m", i),
            "apparent_temperature": _get_at(hourly, "apparent_temperature", i),
            "precipitation_probability": _get_at(hourly, "precipitation_probability", i),
            "precipitation_mm": _get_at(hourly, "precipitation", i),
            "weather_code": _get_at(hourly, "weather_code", i),
            "wind_speed_ms": _get_at(hourly, "wind_speed_10m", i),
            "wind_direction_deg": _get_at(hourly, "wind_direction_10m", i),
            "humidity": _get_at(hourly, "relative_humidity_2m", i),
            "uv_index": _get_at(hourly, "uv_index", i),
        })
    return result


def _transform_daily(daily: dict[str, Any]) -> list[dict[str, Any]]:
    times = daily.get("time", []) or []
    n = min(len(times), DAILY_LENGTH)
    result = []
    for i in range(n):
        result.append({
            "date": times[i],
            "weather_code": _get_at(daily, "weather_code", i),
            "temperature_max": _get_at(daily, "temperature_2m_max", i),
            "temperature_min": _get_at(daily, "temperature_2m_min", i),
            "precipitation_probability_max": _get_at(daily, "precipitation_probability_max", i),
            "sunrise": _get_at(daily, "sunrise", i),
            "sunset": _get_at(daily, "sunset", i),
            "uv_index_max": _get_at(daily, "uv_index_max", i),
        })
    return result


def _get_at(src: dict[str, Any], key: str, idx: int) -> Any:
    arr = src.get(key)
    if arr is None or idx >= len(arr):
        return None
    return arr[idx]


def _iso_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
