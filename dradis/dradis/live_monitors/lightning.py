"""
live_monitors/lightning.py
───────────────────────────
LLM-free live monitor: persistent MQTT listener on the Blitzortung public broker.
Sends Telegram alerts driven by a single location-level threat state machine.

Algorithm
─────────
- Connects once to blitzortung.ha.sed.pl:1883 and stays connected.
- Subscribes to geohash-based topics covering the configured area.
- Buffers all incoming strikes within radius_km in a 15-minute sliding window.
- Every 2 minutes a polling task runs pure-Python DBSCAN (eps=8 km, min_samples=2)
  to identify storm cells. Instead of alerting per cell, it reduces all activity to
  ONE scalar: the distance of the nearest significant cell to the configured point.
  That scalar is tracked as a 30-minute series — the single source of truth for the
  approach trend, velocity and ETA (it does not reset when DBSCAN re-labels cells).
- A single threat state machine per monitor decides one of three levels:
  🟢 CLEAR · 🟡 WATCH · 🔴 WARNING. Alerts fire only on level changes (plus a
  periodic re-alert while in WARNING). Going up to WARNING needs a confirmed approach
  trend; coming down to CLEAR needs a quiet period — both with hysteresis so the user
  sees one coherent thread per storm episode instead of contradictory micro-updates.

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
from collections import namedtuple
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiomqtt

_LOGGER = logging.getLogger(__name__)

MQTT_HOST       = "blitzortung.ha.sed.pl"
MQTT_PORT       = 1883
RECONNECT_DELAY = 15

# ── DBSCAN & strike buffer ─────────────────────────────────────────────────────
DBSCAN_EPS_KM       = 8.0
DBSCAN_MIN_SAMPLES  = 2
STRIKE_BUFFER_MIN   = 15
MIN_CLUSTER_STRIKES = 2     # strikes for a DBSCAN cell to count as significant activity

# ── Threat-distance series (location level) ────────────────────────────────────
THREAT_SERIES_MIN = 30      # rolling window for the nearest-threat distance track
TREND_SAMPLES     = 3       # samples used to classify the approach trend
APPROACH_SLOPE_KM = -0.5    # per-step distance delta (km) considered "approaching"
RETREAT_SLOPE_KM  =  0.5

# ── Threat levels ──────────────────────────────────────────────────────────────
LEVEL_CLEAR   = 0
LEVEL_WATCH   = 1
LEVEL_WARNING = 2

# ── Level thresholds & hysteresis ──────────────────────────────────────────────
WATCH_DIST_KM         = 50    # significant activity within this range → at least WATCH
WARNING_DIST_KM       = 15    # active activity this close → WARNING
WARNING_ETA_MIN       = 30    # confirmed approach reaching us within this ETA → WARNING
WARNING_MIN_STRIKES   = 3     # min strikes in the buffer for a WARNING
WARNING_CONFIRM_POLLS = 2     # consecutive approaching polls required before WARNING
WARNING_HOLD_KM       = 5     # must move beyond WARNING_DIST + this to drop WARNING→WATCH

# ── Alert timing ──────────────────────────────────────────────────────────────
POLL_INTERVAL_SEC  = 120
PERIODIC_ALERT_MIN = 10     # re-alert cadence while in WARNING
DEESCALATE_MIN     = 12     # quiet gap before WARNING de-escalates to WATCH
CLEAR_QUIET_MIN    = 25     # no significant activity this long → all-clear


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


# ── DBSCAN (pure Python, O(n²)) ───────────────────────────────────────────────

def _dbscan(points: list, eps_km: float, min_samples: int) -> list:
    """Return cluster labels for each point; -1 = noise."""
    n = len(points)
    labels = [-1] * n
    visited = [False] * n

    def neighbours(i: int) -> list:
        return [j for j in range(n)
                if j != i and _distance_km(points[i][0], points[i][1],
                                           points[j][0], points[j][1]) <= eps_km]

    cluster_id = 0
    for i in range(n):
        if visited[i]:
            continue
        visited[i] = True
        nbrs = neighbours(i)
        if len(nbrs) < min_samples - 1:
            continue
        labels[i] = cluster_id
        seed_set = list(nbrs)
        k = 0
        while k < len(seed_set):
            j = seed_set[k]
            if not visited[j]:
                visited[j] = True
                nbrs_j = neighbours(j)
                if len(nbrs_j) >= min_samples - 1:
                    for nb in nbrs_j:
                        if nb not in seed_set:
                            seed_set.append(nb)
            if labels[j] == -1:
                labels[j] = cluster_id
            k += 1
        cluster_id += 1
    return labels


# ── Trend / velocity over the threat-distance series ──────────────────────────
# A "series" is a list of (ts, lat, lon, dist_km) samples of the nearest significant
# activity, oldest first. Trend, velocity and ETA are all derived from it.

def _classify_trend(series: list) -> str:
    if len(series) < TREND_SAMPLES:
        return "UNKNOWN"
    dists = [s[3] for s in series[-TREND_SAMPLES:]]
    diffs = [dists[i + 1] - dists[i] for i in range(len(dists) - 1)]
    if all(d < APPROACH_SLOPE_KM for d in diffs):
        return "APPROACHING"
    if all(d > RETREAT_SLOPE_KM for d in diffs):
        return "RETREATING"
    return "STATIONARY"


def _velocity_eta(series: list) -> tuple:
    """Return (velocity_kmh, eta_min) — either may be None."""
    if len(series) < 2:
        return None, None
    first, last = series[0], series[-1]
    time_h = (last[0] - first[0]) / 3600.0
    if time_h <= 0:
        return None, None
    displacement_km = _distance_km(first[1], first[2], last[1], last[2])
    velocity = round(displacement_km / time_h, 1) if displacement_km > 0 else None
    approach_rate_kmh = -(last[3] - first[3]) / time_h   # positive = approaching
    eta_min = None
    if approach_rate_kmh > 0 and last[3] > 0:
        eta_min = int(last[3] / (approach_rate_kmh / 60.0))
    return velocity, eta_min


# ── Pending alert ─────────────────────────────────────────────────────────────

Alert = namedtuple("Alert", ["level", "text", "periodic"])


# ── Monitor class ─────────────────────────────────────────────────────────────

class LightningLiveMonitor:
    """Persistent MQTT listener for one live monitor entry of type 'lightning'."""

    def __init__(self, cfg: dict, telegram_send_fn, tz_name: str = "UTC"):
        self.monitor_id = cfg["id"]
        self.name       = cfg.get("name", "Lightning")
        self.location   = cfg.get("location", "")
        self.lat        = float(cfg.get("latitude", 0))
        self.lon        = float(cfg.get("longitude", 0))
        self.radius_km  = float(cfg.get("radius_km", 100))
        self.language   = cfg.get("language", "it")
        self.tz_name    = tz_name
        self._send      = telegram_send_fn
        # Perception layer
        self._strike_buffer: list = []   # [(ts, lat, lon, dist_km), ...]
        self._series: list = []          # [(ts, lat, lon, dist_km), ...] nearest activity
        # Threat state machine
        self._level             = LEVEL_CLEAR
        self._approach_streak   = 0
        self._last_significant_ts = 0.0
        self._last_periodic_ts    = 0.0
        # Tasks
        self._task: asyncio.Task | None      = None
        self._poll_task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self._run(), name=f"live_lightning:{self.monitor_id}"
            )
            self._poll_task = asyncio.create_task(
                self._poll_loop(), name=f"lightning_poll:{self.monitor_id}"
            )
            print(f"[LiveMonitor] '{self.name}' started "
                  f"(radius={self.radius_km:.0f}km, threat state machine)")

    def stop(self) -> None:
        for task in (self._poll_task, self._task):
            if task and not task.done():
                task.cancel()
        print(f"[LiveMonitor] '{self.name}' stopped")

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    # ── MQTT loop ─────────────────────────────────────────────────────────────

    async def _run(self) -> None:
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

    async def _on_message(self, message) -> None:
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
        self._strike_buffer.append((time.time(), float(s_lat), float(s_lon), dist))

    # ── Polling task ──────────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(POLL_INTERVAL_SEC)
                now = time.time()
                for alert in self._evaluate(now):
                    await self._dispatch(alert, now)
            except asyncio.CancelledError:
                return
            except Exception as e:
                _LOGGER.error("[LiveMonitor] '%s' poll error: %s", self.name, e)

    # ── Perception: nearest significant activity ──────────────────────────────

    def _nearest_activity(self) -> tuple | None:
        """Return (lat, lon, dist_km) of the nearest significant DBSCAN cell, or None."""
        if len(self._strike_buffer) < DBSCAN_MIN_SAMPLES:
            return None
        points = [(la, lo) for _, la, lo, _ in self._strike_buffer]
        labels = _dbscan(points, DBSCAN_EPS_KM, DBSCAN_MIN_SAMPLES)
        groups: dict[int, list] = {}
        for idx, label in enumerate(labels):
            if label != -1:
                groups.setdefault(label, []).append(self._strike_buffer[idx])
        best = None
        for entries in groups.values():
            if len(entries) < MIN_CLUSTER_STRIKES:
                continue
            clat = sum(e[1] for e in entries) / len(entries)
            clon = sum(e[2] for e in entries) / len(entries)
            dist = _distance_km(self.lat, self.lon, clat, clon)
            if best is None or dist < best[2]:
                best = (clat, clon, dist)
        return best

    # ── Decision: threat state machine ────────────────────────────────────────

    def _evaluate(self, now: float) -> list:
        # 1. Age out the strike buffer and refresh the nearest-activity sample.
        self._strike_buffer = [s for s in self._strike_buffer
                               if s[0] >= now - STRIKE_BUFFER_MIN * 60]
        nearest = self._nearest_activity()
        if nearest:
            self._series.append((now, nearest[0], nearest[1], nearest[2]))
            self._last_significant_ts = now
        self._series = [s for s in self._series if s[0] >= now - THREAT_SERIES_MIN * 60]

        # 2. Trend + approach confirmation streak.
        trend = _classify_trend(self._series)
        if nearest and trend == "APPROACHING":
            self._approach_streak += 1
        else:
            self._approach_streak = 0

        _, eta = _velocity_eta(self._series)
        strikes = len(self._strike_buffer)
        target = self._target_level(now, nearest, trend, eta, strikes)

        # 3. Emit on level change, or periodic re-alert while in WARNING.
        if target != self._level:
            return [self._make_alert(target, trend)]
        if (target == LEVEL_WARNING
                and (now - self._last_periodic_ts) >= PERIODIC_ALERT_MIN * 60):
            return [self._make_alert(target, trend, periodic=True)]
        return []

    def _target_level(self, now, nearest, trend, eta, strikes) -> int:
        cur = self._level
        if nearest is None:
            # No current activity — de-escalate only with hysteresis on the quiet gap.
            gap = now - self._last_significant_ts
            if gap >= CLEAR_QUIET_MIN * 60:
                return LEVEL_CLEAR
            if cur == LEVEL_WARNING and gap >= DEESCALATE_MIN * 60:
                return LEVEL_WATCH
            return cur

        dist = nearest[2]
        confirmed_warning = (
            self._approach_streak >= WARNING_CONFIRM_POLLS
            and strikes >= WARNING_MIN_STRIKES
            and (dist <= WARNING_DIST_KM or (eta is not None and 0 < eta <= WARNING_ETA_MIN))
        )
        if confirmed_warning:
            return LEVEL_WARNING
        if cur == LEVEL_WARNING:
            # Hold WARNING until the cell clearly pulls away (distance + hysteresis).
            if dist > WARNING_DIST_KM + WARNING_HOLD_KM and trend != "APPROACHING":
                return LEVEL_WATCH
            return LEVEL_WARNING
        if dist <= WATCH_DIST_KM:
            return LEVEL_WATCH
        return cur

    def _make_alert(self, level: int, trend: str, periodic: bool = False) -> Alert:
        if level == LEVEL_CLEAR:
            text = self._fmt_clear()
        elif level == LEVEL_WATCH:
            text = self._fmt_watch(trend)
        else:
            text = self._fmt_warning(trend)
        return Alert(level, text, periodic)

    # ── Alert dispatch ────────────────────────────────────────────────────────

    async def _dispatch(self, alert: Alert, now: float) -> None:
        _LOGGER.info(
            "[LiveMonitor] %s | level→%d periodic=%s streak=%d strikes=%d",
            self.name, alert.level, alert.periodic,
            self._approach_streak, len(self._strike_buffer),
        )
        # send_telegram swallows its own exceptions and returns False on failure, so
        # the state machine is advanced ONLY on confirmed delivery — a dropped alert
        # is retried on the next poll instead of leaving the user out of sync.
        try:
            ok = await self._send(alert.text)
        except Exception as e:
            _LOGGER.error("[LiveMonitor] '%s' send error: %s", self.name, e)
            return
        if not ok:
            _LOGGER.warning(
                "[LiveMonitor] '%s' alert NOT delivered — state held, retry next poll",
                self.name,
            )
            return
        if alert.periodic:
            self._last_periodic_ts = now
            return
        self._level = alert.level
        if alert.level == LEVEL_WARNING:
            self._last_periodic_ts = now
        elif alert.level == LEVEL_CLEAR:
            self._series.clear()
            self._approach_streak = 0

    # ── Message formatters ────────────────────────────────────────────────────

    def _threat_dir(self) -> tuple:
        """Return (dist_km, azimuth, direction_label) of the latest threat sample."""
        if not self._series:
            return 0.0, 0.0, _direction(0.0, self.language)
        last = self._series[-1]
        az = _azimuth_deg(self.lat, self.lon, last[1], last[2])
        return last[3], az, _direction(az, self.language)

    def _trend_phrase(self, trend: str) -> str:
        if self.language == "it":
            return {"APPROACHING": "In avvicinamento", "RETREATING": "In allontanamento",
                    "STATIONARY": "Stazionario", "UNKNOWN": "In osservazione"}.get(trend, "")
        return {"APPROACHING": "Approaching", "RETREATING": "Moving away",
                "STATIONARY": "Stationary", "UNKNOWN": "Watching"}.get(trend, "")

    def _fmt_warning(self, trend: str) -> str:
        dist, az, dir_lbl = self._threat_dir()
        vel, eta = _velocity_eta(self._series)
        loc = html.escape(self.location or self.name)
        strikes = len(self._strike_buffer)
        approaching = trend == "APPROACHING"
        if self.language == "it":
            lines = [f"🔴 <b>ALLERTA temporale — {loc}</b>"]
            lead = "In avvicinamento" if approaching else "Nelle immediate vicinanze"
            lines.append(f"📍 {lead}: <b>{dist:.1f} km</b> a {dir_lbl} ({az:.0f}°)")
            if vel and approaching and eta:
                lines.append(f"🚀 ~{vel:.0f} km/h — arrivo stimato: {eta} min")
            elif vel:
                lines.append(f"🚀 ~{vel:.0f} km/h")
            lines.append(f"🔢 Fulmini ultimi {STRIKE_BUFFER_MIN} min: {strikes}")
        else:
            lines = [f"🔴 <b>Storm WARNING — {loc}</b>"]
            lead = "Approaching" if approaching else "In the immediate area"
            lines.append(f"📍 {lead}: <b>{dist:.1f} km</b> to {dir_lbl} ({az:.0f}°)")
            if vel and approaching and eta:
                lines.append(f"🚀 ~{vel:.0f} km/h — estimated arrival: {eta} min")
            elif vel:
                lines.append(f"🚀 ~{vel:.0f} km/h")
            lines.append(f"🔢 Strikes (last {STRIKE_BUFFER_MIN} min): {strikes}")
        lines.append(f"🕐 {self._now_str()}")
        return "\n".join(lines)

    def _fmt_watch(self, trend: str) -> str:
        dist, az, dir_lbl = self._threat_dir()
        loc = html.escape(self.location or self.name)
        strikes = len(self._strike_buffer)
        phrase = self._trend_phrase(trend)
        if self.language == "it":
            lines = [f"🟡 <b>Temporale in zona — {loc}</b>",
                     f"📍 Attività a <b>{dist:.1f} km</b> a {dir_lbl} ({az:.0f}°)",
                     f"📊 {phrase}",
                     f"🔢 Fulmini ultimi {STRIKE_BUFFER_MIN} min: {strikes}"]
        else:
            lines = [f"🟡 <b>Storm in the area — {loc}</b>",
                     f"📍 Activity at <b>{dist:.1f} km</b> to {dir_lbl} ({az:.0f}°)",
                     f"📊 {phrase}",
                     f"🔢 Strikes (last {STRIKE_BUFFER_MIN} min): {strikes}"]
        lines.append(f"🕐 {self._now_str()}")
        return "\n".join(lines)

    def _fmt_clear(self) -> str:
        loc = html.escape(self.location or self.name)
        if self.language == "it":
            return (f"✅ <b>Cessato allarme temporale — {loc}</b>\n"
                    f"🔇 Nessun fulmine da {CLEAR_QUIET_MIN} min\n"
                    f"🕐 {self._now_str()}")
        return (f"✅ <b>Storm threat cleared — {loc}</b>\n"
                f"🔇 No lightning for {CLEAR_QUIET_MIN} min\n"
                f"🕐 {self._now_str()}")

    def _now_str(self) -> str:
        try:
            tz = ZoneInfo(self.tz_name)
        except ZoneInfoNotFoundError:
            tz = ZoneInfo("UTC")
        return datetime.now(tz).strftime("%H:%M")


# ── Manager ───────────────────────────────────────────────────────────────────

class LiveMonitorManager:
    """Owns all live monitor instances. Called by main.py on startup and config changes."""

    def __init__(self):
        self._monitors: dict[str, LightningLiveMonitor] = {}

    def reload(self, configs: list[dict], make_send_fn, tz_name: str):
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
            m = LightningLiveMonitor(cfg, make_send_fn(cfg), tz_name)
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
