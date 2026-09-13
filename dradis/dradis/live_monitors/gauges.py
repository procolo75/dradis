"""
live_monitors/gauges.py
────────────────────────
The fetching half of the MeteoHub reading: one HTTP call, a neighbourhood
cache, and the discipline of returning None rather than a confident blank.
The shapes and the arithmetic are in `gauges_core`.

MeteoHub (Agenzia ItaliaMeteo) republishes the Italian ground-station networks —
5075 stations, ten-minute cadence, `/api/observations` public with no account
and no token. It is the only instrument in DRADIS that MEASURES rain instead of
inferring it from reflectivity.

Why this reading has no authority
─────────────────────────────────
`rain_front` alerts on the DPC radar composite and MUST keep alerting on it
alone. A gauge reading is shown to the reader and never consulted by the
monitor, for three independent reasons, any one of which is sufficient:

1. It is as late as the radar or later. Stations publish on a ten-minute
   cadence with their own lag, and rainfall has to be integrated over a window
   before it is a rate at all. It always arrives after the decision is due.
2. A broken gauge and a dry gauge produce the same zero. A station that stops
   transmitting does not return an error — it returns fewer rows. Giving that
   zero a vote would make the monitor's silence depend on a sensor nobody is
   watching.
3. Zero millimetres is not an absence of rain. The nearest station can be seven
   kilometres away while the cell is at three.

So "could not measure" and "measured nothing falling" are kept apart in the
TYPE and not merely in a value: `observe()` returns None for the first and a
`GaugeView` with an empty `wet` for the second. A caller that collapses the two
has thrown away the only thing that makes the printed line honest.

Politeness to a free public service
───────────────────────────────────
Stations publish every ten minutes, so anything faster than that is asking for
an answer that cannot have changed. Readings are cached per NEIGHBOURHOOD
rather than per coordinate, because a monitor that follows a phone moves its
origin a few hundred metres between polls and would otherwise miss an exact-key
cache every single time. One in-flight request per key, shared by whoever asks
for it meanwhile.

Unlike `RadarFeed` there is no background task and no reference counting: this
is fetched lazily, only when an alert is about to go out or `/rain` asks. Those
are rare — at most `ring_count` + 1 per event — so the load is near zero and
there is no lifecycle to get wrong.
"""

import asyncio
import logging
import time

import httpx

from .gauges_core import (
    GaugeReading, GaugeView, MAX_READING_AGE_SEC, MIN_RATE_WINDOW_SEC,
    OFFICIAL_NETWORKS, OFFICIAL_PREFIXES, WINDOW_MIN, API_URL, LICENSE_GROUP,
    build_params, parse,
)

_LOGGER = logging.getLogger(__name__)

HTTP_TIMEOUT_SEC = 8.0
CACHE_TTL_SEC = 300.0

# Cache key rounding, ~11 km: far coarser than a poll's worth of movement and
# far finer than the disc being read.
_CACHE_GRID_DEG = 0.1

_cache: dict[tuple, tuple[float, GaugeView]] = {}
_locks: dict[tuple, asyncio.Lock] = {}

# Global knobs, set from the add-on settings. They are properties of the SOURCE
# — is it switched on, which networks are trusted, how long an answer stays
# fresh — not of any one monitor, so they live here rather than being copied
# into every monitor's config. Mirrors `position_manager.configure`.
_enabled = False
_official_only = True
_cache_ttl = CACHE_TTL_SEC


def configure(settings: dict) -> None:
    global _enabled, _official_only, _cache_ttl
    before = (_enabled, _official_only, _cache_ttl)
    _enabled = bool(settings.get("meteohub_enabled", False))
    _official_only = bool(settings.get("meteohub_official_only", True))
    try:
        _cache_ttl = max(60.0, float(settings.get("meteohub_cache_ttl")
                                     or CACHE_TTL_SEC))
    except (TypeError, ValueError):
        _cache_ttl = CACHE_TTL_SEC
    if (_enabled, _official_only, _cache_ttl) != before:
        clear_cache()
        print(f"[Gauges] MeteoHub {'on' if _enabled else 'off'} "
              f"(official_only={_official_only}, ttl={_cache_ttl:.0f}s)")


def is_enabled() -> bool:
    return _enabled


def _cache_key(lat: float, lon: float, radius_km: float,
               min_mmh: float, official_only: bool) -> tuple:
    return (round(lat / _CACHE_GRID_DEG), round(lon / _CACHE_GRID_DEG),
            round(radius_km), round(min_mmh, 2), official_only)


def clear_cache() -> None:
    _cache.clear()


async def observe(lat: float, lon: float, radius_km: float, *,
                  min_mmh: float = 0.2, official_only: bool | None = None,
                  cache_ttl: float | None = None,
                  timeout: float = HTTP_TIMEOUT_SEC,
                  now: float | None = None) -> GaugeView | None:
    """Stations within `radius_km`, or None if they could not be read.

    None means the question went UNANSWERED — the source switched off, service
    down, request refused. An empty `GaugeView` means it was answered and there
    was nothing recent within the radius, and a view with a dry `wet` means the
    stations were read and had nothing falling on them. Three facts, three
    returns; the caller words each differently.
    `official_only` and `cache_ttl` fall back to `configure()`.
    """
    if not _enabled:
        return None
    official_only = _official_only if official_only is None else official_only
    cache_ttl = _cache_ttl if cache_ttl is None else cache_ttl
    now = time.time() if now is None else now
    key = _cache_key(lat, lon, radius_km, min_mmh, official_only)

    hit = _cache.get(key)
    if hit is not None and now - hit[0] <= cache_ttl:
        return hit[1]

    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        hit = _cache.get(key)
        if hit is not None and now - hit[0] <= cache_ttl:
            return hit[1]

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(
                    API_URL, params=build_params(lat, lon, radius_km, now))
            response.raise_for_status()
            payload = response.json()
        except Exception as e:
            _LOGGER.warning("[Gauges] MeteoHub unreachable (%s): %s",
                            type(e).__name__, e)
            return None

        # The service answers a rejected query with a bare JSON string rather
        # than an object — "Reftime is missing", "License group parameter is
        # mandatory" — so a body that is not a dict is a refusal, not data.
        if not isinstance(payload, dict):
            _LOGGER.warning("[Gauges] MeteoHub refused the query: %r", payload)
            return None

        # An empty view is an ANSWER — the stations were asked and had nothing
        # recent to say, which is a different fact from the service being down
        # and is worded differently. Only a failed request is None.
        view = parse(payload, lat, lon, radius_km, min_mmh=min_mmh,
                     official_only=official_only, now=now)
        if not view.readings:
            _LOGGER.info("[Gauges] no recent reading within %.0f km of %.4f,%.4f",
                         radius_km, lat, lon)

        _cache[key] = (now, view)
        _LOGGER.info("[Gauges] %d station(s) within %.0f km, %d wet",
                     len(view.readings), radius_km, len(view.wet))
        return view


__all__ = [
    "GaugeReading", "GaugeView", "observe", "parse", "clear_cache",
    "API_URL", "LICENSE_GROUP", "OFFICIAL_NETWORKS", "OFFICIAL_PREFIXES",
    "configure", "is_enabled",
    "WINDOW_MIN", "CACHE_TTL_SEC", "HTTP_TIMEOUT_SEC",
    "MIN_RATE_WINDOW_SEC", "MAX_READING_AGE_SEC",
]
