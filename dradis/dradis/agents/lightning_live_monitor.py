"""
agents/lightning_live_monitor.py
─────────────────────────────────
LLM-free live monitor: persistent MQTT listener on the Blitzortung public broker.
Sends a Telegram alert on the FIRST lightning strike within radius_km after each
cooldown period expires. No polling — pure push via MQTT.

Behaviour
─────────
- Connects once to blitzortung.ha.sed.pl:1883 and stays connected.
- Subscribes to geohash-based topics covering the configured area.
- On each incoming strike:
    • computes distance and azimuth from the configured lat/lon
    • if distance <= radius_km AND cooldown has expired → fires alert, resets timer
- After cooldown_min minutes the counter resets and the next strike triggers a new alert.
- Reconnects automatically on disconnect.

One LightningLiveMonitor instance per enabled live monitor entry.
All instances are owned by LiveMonitorManager (singleton live_monitor_manager).
Called by main.py on startup and on config changes — NOT via the APScheduler cron.
"""

import asyncio
import html
import json
import math
import time
import logging
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiomqtt

_LOGGER = logging.getLogger(__name__)

MQTT_HOST       = "blitzortung.ha.sed.pl"
MQTT_PORT       = 1883
RECONNECT_DELAY = 15


# ── Geo helpers ───────────────────────────────────────────────────────────────

def _distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dy = (lat2 - lat1) * math.pi / 180
    dx = (lon2 - lon1) * math.pi / 180 * math.cos(lat1 * math.pi / 180)
    return round(math.sqrt(dx * dx + dy * dy) * 6371, 1)


def _azimuth_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dlon = math.radians(lon2 - lon1)
    x = math.sin(dlon) * math.cos(math.radians(lat2))
    y = (math.cos(math.radians(lat1)) * math.sin(math.radians(lat2))
         - math.sin(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.cos(dlon))
    return (math.degrees(math.atan2(x, y)) + 360) % 360


_DIR_IT = ["N", "NE", "E", "SE", "S", "SO", "O", "NO"]
_DIR_EN = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def _direction(azimuth: float, lang: str) -> str:
    labels = _DIR_IT if lang == "it" else _DIR_EN
    return labels[round(azimuth / 45) % 8]


# ── Geohash helpers ───────────────────────────────────────────────────────────

_BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"


def _geohash_encode(lat: float, lon: float, precision: int = 3) -> str:
    min_lat, max_lat = -90.0, 90.0
    min_lon, max_lon = -180.0, 180.0
    chars, bits, is_lon, char_bits = [], 0, True, 0
    for _ in range(precision * 5):
        if is_lon:
            mid = (min_lon + max_lon) / 2
            if lon >= mid:
                bits = (bits << 1) | 1; min_lon = mid
            else:
                bits <<= 1; max_lon = mid
        else:
            mid = (min_lat + max_lat) / 2
            if lat >= mid:
                bits = (bits << 1) | 1; min_lat = mid
            else:
                bits <<= 1; max_lat = mid
        is_lon = not is_lon
        char_bits += 1
        if char_bits == 5:
            chars.append(_BASE32[bits & 0x1F]); bits = 0; char_bits = 0
    return "".join(chars)


def _geohash_decode(gh: str) -> tuple[float, float]:
    min_lat, max_lat = -90.0, 90.0
    min_lon, max_lon = -180.0, 180.0
    is_lon = True
    for char in gh:
        bits = _BASE32.index(char)
        for i in range(4, -1, -1):
            bit = (bits >> i) & 1
            if is_lon:
                mid = (min_lon + max_lon) / 2
                if bit: min_lon = mid
                else:   max_lon = mid
            else:
                mid = (min_lat + max_lat) / 2
                if bit: min_lat = mid
                else:   max_lat = mid
            is_lon = not is_lon
    return (min_lat + max_lat) / 2, (min_lon + max_lon) / 2


def _geohash_neighbors(gh: str) -> list[str]:
    lat, lon = _geohash_decode(gh)
    step = 180.0 / (2 ** (2.5 * len(gh)))
    neighbors: set[str] = set()
    for dlat in (-1, 0, 1):
        for dlon in (-1, 0, 1):
            if dlat == 0 and dlon == 0:
                continue
            nlat = max(-90.0, min(90.0, lat + dlat * step))
            nlon = ((lon + dlon * step + 180) % 360) - 180
            neighbors.add(_geohash_encode(nlat, nlon, len(gh)))
    return list(neighbors)


def _topics_for_area(lat: float, lon: float) -> list[str]:
    gh = _geohash_encode(lat, lon, precision=3)
    cells = [gh] + _geohash_neighbors(gh)
    return [f"blitzortung/1.1/{c[0]}/{c[1]}/{c[2]}/#" for c in cells]


# ── Monitor class ─────────────────────────────────────────────────────────────

class LightningLiveMonitor:
    """Persistent MQTT listener for one live monitor entry of type 'lightning'."""

    def __init__(self, cfg: dict, telegram_send_fn, tz_name: str = "UTC"):
        self.monitor_id   = cfg["id"]
        self.name         = cfg.get("name", "Lightning")
        self.location     = cfg.get("location", "")
        self.lat          = float(cfg.get("latitude", 0))
        self.lon          = float(cfg.get("longitude", 0))
        self.radius_km    = float(cfg.get("radius_km", 100))
        self.cooldown_min = float(cfg.get("cooldown_min", 30))
        self.language     = cfg.get("language", "it")
        self.tz_name      = tz_name
        self._send        = telegram_send_fn
        self._last_alert: float = 0.0
        self._task: asyncio.Task | None = None

    def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self._run(), name=f"live_lightning:{self.monitor_id}"
            )
            print(f"[LiveMonitor] '{self.name}' started "
                  f"(radius={self.radius_km:.0f}km, cooldown={self.cooldown_min:.0f}min)")

    def stop(self):
        if self._task and not self._task.done():
            self._task.cancel()
            print(f"[LiveMonitor] '{self.name}' stopped")

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _run(self):
        topics = _topics_for_area(self.lat, self.lon)
        while True:
            try:
                print(f"[LiveMonitor] '{self.name}' connecting to {MQTT_HOST}:{MQTT_PORT}")
                async with aiomqtt.Client(MQTT_HOST, MQTT_PORT) as client:
                    for topic in topics:
                        await client.subscribe(topic)
                    print(f"[LiveMonitor] '{self.name}' subscribed ({len(topics)} topics)")
                    async for message in client.messages:
                        await self._on_message(message)
            except asyncio.CancelledError:
                return
            except Exception as e:
                print(f"[LiveMonitor] '{self.name}' disconnected: {e} — retry in {RECONNECT_DELAY}s")
                await asyncio.sleep(RECONNECT_DELAY)

    async def _on_message(self, message):
        try:
            data = json.loads(message.payload)
        except Exception:
            return
        s_lat = data.get("lat")
        s_lon = data.get("lon")
        if s_lat is None or s_lon is None:
            return
        dist = _distance_km(self.lat, self.lon, float(s_lat), float(s_lon))
        if dist > self.radius_km:
            return
        now = time.time()
        if (now - self._last_alert) / 60.0 < self.cooldown_min:
            return
        self._last_alert = now
        az        = _azimuth_deg(self.lat, self.lon, float(s_lat), float(s_lon))
        direction = _direction(az, self.language)
        print(f"[LiveMonitor] '{self.name}' alert: {dist:.1f}km {direction}")
        try:
            await self._send(self._format_alert(dist, az, direction))
        except Exception as e:
            print(f"[LiveMonitor] '{self.name}' send error: {e}")

    def _format_alert(self, dist: float, az: float, direction: str) -> str:
        try:
            tz = ZoneInfo(self.tz_name)
        except ZoneInfoNotFoundError:
            tz = ZoneInfo("UTC")
        now_str = datetime.now(tz).strftime("%H:%M")
        loc = html.escape(self.location or self.name)
        if self.language == "it":
            return (
                f"⚡ <b>Fulmine rilevato — {loc}</b>\n"
                f"📍 Distanza: <b>{dist:.1f} km</b> a {direction} ({az:.0f}°)\n"
                f"🔕 Prossimo alert tra {int(self.cooldown_min)} min\n"
                f"🕐 {now_str}"
            )
        return (
            f"⚡ <b>Lightning detected — {loc}</b>\n"
            f"📍 Distance: <b>{dist:.1f} km</b> to {direction} ({az:.0f}°)\n"
            f"🔕 Next alert in {int(self.cooldown_min)} min\n"
            f"🕐 {now_str}"
        )


# ── Manager ───────────────────────────────────────────────────────────────────

class LiveMonitorManager:
    """Owns all live monitor instances. Called by main.py on startup and config changes."""

    def __init__(self):
        self._monitors: dict[str, LightningLiveMonitor] = {}

    def reload(self, configs: list[dict], telegram_send_fn, tz_name: str):
        wanted: set[str] = set()
        for cfg in configs:
            if cfg.get("type") != "lightning":
                continue
            if not cfg.get("enabled"):
                continue
            mid = cfg["id"]
            wanted.add(mid)
            if mid in self._monitors:
                self._monitors[mid].stop()
            m = LightningLiveMonitor(cfg, telegram_send_fn, tz_name)
            self._monitors[mid] = m
            m.start()
        for mid in list(self._monitors):
            if mid not in wanted:
                self._monitors[mid].stop()
                del self._monitors[mid]

    def stop_all(self):
        for m in self._monitors.values():
            m.stop()
        self._monitors.clear()

    def status(self, monitor_id: str) -> str:
        m = self._monitors.get(monitor_id)
        if m and m.is_running():
            return "running"
        return "stopped"


live_monitor_manager = LiveMonitorManager()
