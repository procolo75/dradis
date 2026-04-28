import httpx
import statistics
from datetime import date as _date

from agent_core import create_agent, _now_str
from web.server import SETTINGS_DEFAULTS


def _summarise_hourly(hourly: dict, days: int) -> list:
    """Collapse raw hourly arrays into per-day, per-time-band dicts."""
    times = hourly.get("time", [])
    if not times:
        return []
    scalar_fields = [
        "temperature_2m", "relative_humidity_2m", "dew_point_2m",
        "precipitation_probability", "precipitation", "showers",
        "wind_speed_10m", "wind_gusts_10m", "cloud_cover",
    ]
    BANDS = [
        ("night",     range(0,  6)),
        ("morning",   range(6,  12)),
        ("afternoon", range(12, 18)),
        ("evening",   range(18, 24)),
    ]
    result = []
    for day_idx in range(days):
        base = day_idx * 24
        if base >= len(times):
            break
        bands_out = {}
        for band_name, hour_range in BANDS:
            bucket: dict = {}
            for h in hour_range:
                slot = base + h
                if slot >= len(times):
                    break
                for field in scalar_fields:
                    vals = hourly.get(field, [])
                    if slot < len(vals) and vals[slot] is not None:
                        bucket.setdefault(field, []).append(vals[slot])
                codes = hourly.get("weather_code", [])
                if slot < len(codes) and codes[slot] is not None:
                    bucket.setdefault("weather_code", []).append(codes[slot])
            if not bucket:
                continue
            summary: dict = {}
            for field in scalar_fields:
                v = bucket.get(field, [])
                if v:
                    summary[field] = round(statistics.mean(v), 1)
            wc = bucket.get("weather_code", [])
            if wc:
                summary["weather_code"] = max(wc)
            bands_out[band_name] = summary
        iso = times[base][:10]
        weekday = _date.fromisoformat(iso).strftime("%A")
        result.append({"date": iso, "weekday": weekday, "bands": bands_out})
    return result


async def fetch_weather(location: str, days: int = 7) -> str:
    """Fetch weather forecast from Open-Meteo.

    Args:
        location: city name or geographic location.
        days: number of forecast days (1-16, default 7).
    """
    days = max(1, min(days, 16))
    async with httpx.AsyncClient(timeout=10) as client:
        geo = await client.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": location, "count": 1, "language": "en", "format": "json"},
        )
    results = geo.json().get("results", [])
    if not results:
        return f"Location '{location}' not found. Do not invent weather data."
    r = results[0]
    lat, lon, name = r["latitude"], r["longitude"], r.get("name", location)

    params = {
        "latitude": lat,
        "longitude": lon,
        "current": (
            "temperature_2m,relative_humidity_2m,precipitation,"
            "wind_speed_10m,wind_gusts_10m,weather_code,cloud_cover"
        ),
        "hourly": (
            "temperature_2m,relative_humidity_2m,dew_point_2m,"
            "precipitation_probability,precipitation,showers,weather_code,"
            "wind_speed_10m,wind_gusts_10m,cloud_cover"
        ),
        "daily": (
            "temperature_2m_max,temperature_2m_min,precipitation_sum,"
            "weather_code,wind_speed_10m_max,wind_gusts_10m_max,"
            "precipitation_probability_max"
        ),
        "timezone": "auto",
        "forecast_days": days,
    }

    async with httpx.AsyncClient(timeout=10) as client:
        fc = await client.get("https://api.open-meteo.com/v1/forecast", params=params)

    data    = fc.json()
    current = data.get("current", {})
    hourly  = data.get("hourly", {})
    daily   = data.get("daily", {})

    daily_times = daily.get("time", [])
    daily_weekdays = [_date.fromisoformat(d).strftime("%A") for d in daily_times]
    daily_annotated = {**daily, "weekday": daily_weekdays}

    hourly_summary = _summarise_hourly(hourly, days)
    return (
        f"Location: {name} | Days: {days}\n"
        f"Current: {current}\n"
        f"Hourly summary by time band: {hourly_summary}\n"
        f"Daily: {daily_annotated}"
    )


def create_weather_agent(settings: dict):
    tz_name = settings.get("timezone", "UTC") or "UTC"

    base_prompt = (
        f"It is {_now_str(tz_name)} ({tz_name}). "
        "You are a meteorologist. Summarise the weather data clearly and concisely "
        "in the same language the user used. Never invent data not present in the results. "
        + settings.get("weather_instructions", "")
    )

    async def get_weather(location: str, days: int = 7) -> str:
        """Get current weather and multi-day forecast for a location.

        Call this when the user asks about current weather, forecast, temperature,
        rain, wind, cloud cover, or uses phrases like 'che tempo fa', 'previsioni',
        'meteo', 'weather', 'forecast', 'temperature'.

        Args:
            location: city name or geographic location.
            days: number of forecast days (1-16, default 7).
        """
        return await fetch_weather(location, days=days)

    return create_agent(
        system_prompt=base_prompt,
        model=settings.get("weather_model", SETTINGS_DEFAULTS["weather_model"]),
        provider=settings.get("weather_provider", SETTINGS_DEFAULTS["weather_provider"]),
        tools=[get_weather],
        name="weather",
        tool_call_limit=2,
    )
