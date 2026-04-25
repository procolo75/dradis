import httpx

from agent_core import create_agent, _now_str
from web.server import SETTINGS_DEFAULTS


async def fetch_weather(location: str) -> str:
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

    async with httpx.AsyncClient(timeout=10) as client:
        fc = await client.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat, "longitude": lon,
                "current": "temperature_2m,relative_humidity_2m,precipitation,wind_speed_10m,weather_code",
                "daily":   "temperature_2m_max,temperature_2m_min,precipitation_sum,weather_code",
                "timezone": "auto",
                "forecast_days": 3,
            },
        )
    data    = fc.json()
    current = data.get("current", {})
    daily   = data.get("daily", {})
    return f"Location: {name}\nCurrent: {current}\nDaily forecast (3 days): {daily}"


def create_weather_agent(settings: dict, prefetched_data: str | None = None):
    tz_name = settings.get("timezone", "UTC") or "UTC"

    base_prompt = (
        f"It is {_now_str(tz_name)} ({tz_name}). "
        "You are a meteorologist. Summarise the weather data clearly and concisely "
        "in the same language the user used. Never invent data not present in the results. "
        + settings.get("weather_instructions", "")
    )

    if prefetched_data:
        return create_agent(
            system_prompt=base_prompt + f"\n\nPre-fetched weather data:\n{prefetched_data}",
            model=settings.get("weather_model", SETTINGS_DEFAULTS["weather_model"]),
            provider=settings.get("weather_provider", SETTINGS_DEFAULTS["weather_provider"]),
            tools=[],
            name="weather",
            tool_call_limit=2,
        )

    async def get_weather(location: str) -> str:
        """Get current weather and 3-day forecast for a location.
        Call this when the user asks about current weather, forecast, temperature, rain, wind, or UV index,
        or uses phrases like 'che tempo fa', 'previsioni', 'meteo', 'weather', 'forecast', 'temperature'.
        Pass a city name or geographic location."""
        return await fetch_weather(location)

    return create_agent(
        system_prompt=base_prompt,
        model=settings.get("weather_model", SETTINGS_DEFAULTS["weather_model"]),
        provider=settings.get("weather_provider", SETTINGS_DEFAULTS["weather_provider"]),
        tools=[get_weather],
        name="weather",
        tool_call_limit=2,
    )
