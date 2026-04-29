"""
agents/rain_monitor.py
───────────────────────────────
LLM-free monitor: fetches 15-minute precipitation data from Open-Meteo for a
given location and sends a Telegram alert only when rain is forecast in the
next N hours. If no precipitation is expected, returns an empty string (no
Telegram message sent).
"""

import html
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

_GEOCODE_URL  = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"


async def _geocode(location: str) -> tuple[float, float, str]:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            _GEOCODE_URL,
            params={"name": location, "count": 1, "language": "it", "format": "json"},
        )
    results = resp.json().get("results", [])
    if not results:
        raise ValueError(f"Location not found: {location!r}")
    r = results[0]
    return r["latitude"], r["longitude"], r.get("name", location)


async def _fetch_rain(lat: float, lon: float, tz_name: str) -> dict:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            _FORECAST_URL,
            params={
                "latitude":      lat,
                "longitude":     lon,
                "minutely_15":   "precipitation",
                "timezone":      tz_name,
                "past_days":     0,
                "forecast_days": 2,
            },
        )
    resp.raise_for_status()
    return resp.json()


async def run_rain_monitor(monitor: dict, tz_name: str = "UTC") -> str:
    location = monitor.get("location", "").strip()
    if not location:
        raise ValueError("Monitor: 'location' field is required")

    hours_ahead = max(1, min(int(monitor.get("hours_ahead", 2)), 24))
    lang        = monitor.get("language", "it")

    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")

    lat, lon, resolved = await _geocode(location)
    data = await _fetch_rain(lat, lon, tz_name)

    m15    = data.get("minutely_15", {})
    times  = m15.get("time", [])
    precip = m15.get("precipitation", [])

    now    = datetime.now(tz)
    cutoff = now + timedelta(hours=hours_ahead)

    window = []
    for t_str, mm in zip(times, precip):
        try:
            t = datetime.fromisoformat(t_str).replace(tzinfo=tz)
        except ValueError:
            continue
        if now <= t < cutoff:
            window.append((t, float(mm or 0.0)))

    rainy = [(t, mm) for t, mm in window if mm > 0.0]
    if not rainy:
        return ""

    total = sum(mm for _, mm in rainy)
    esc = html.escape(resolved)
    if lang == "en":
        header  = f"🌧️ <b>Rain alert — {esc}</b>"
        subhead = f"Next {hours_ahead}h | {now.strftime('%d %B %Y, %H:%M')}"
        footer  = f"💧 Total: {total:.1f} mm in {hours_ahead}h"
    else:
        header  = f"🌧️ <b>Allerta pioggia — {esc}</b>"
        subhead = f"Prossime {hours_ahead}h | {now.strftime('%d %B %Y, %H:%M')}"
        footer  = f"💧 Totale: {total:.1f} mm in {hours_ahead}h"

    lines = [header, subhead, ""]
    for t, mm in window:
        marker = "🔵" if mm > 0.0 else "⚪"
        lines.append(f"{marker} {t.strftime('%H:%M')} → {mm:.1f} mm")
    lines += ["", footer]

    return "\n".join(lines)
