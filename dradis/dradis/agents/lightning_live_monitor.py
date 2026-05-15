"""
agents/lightning_live_monitor.py
─────────────────────────────────
LLM-free live monitor: persistent MQTT listener on the Blitzortung public broker.
Sends Telegram alerts with adaptive frequency based on storm trajectory analysis.
No polling — pure push via MQTT.

Behaviour
─────────
- Connects once to blitzortung.ha.sed.pl:1883 and stays connected.
- Subscribes to geohash-based topics covering the configured area.
- On each incoming strike within radius_km:
    • adds the event to a 60-minute sliding window buffer
    • runs trajectory analysis (linear regression over 5-min windows)
    • classifies the storm: AVVICINAMENTO / ALLONTANAMENTO / STAZIONARIO / UNKNOWN
    • applies adaptive cooldown based on the classification
    • fires a rich Telegram alert including ETA, velocity, and intensity trend
- Reconnects automatically on disconnect.

Cooldown logic (automatic, not user-configurable):
    AVVICINAMENTO  → alert every 5 min with updated ETA
    ALLONTANAMENTO → alert every 30 min (low frequency)
    STAZIONARIO    → silent (no alert — only directional storms trigger notifications)
    UNKNOWN        → silent (insufficient data — waits for 2 populated 5-min windows)

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

# ── Trajectory analysis constants ─────────────────────────────────────────────

TRAJ_BUFFER_MIN          = 60   # minutes of history kept in the sliding buffer
TRAJ_WINDOW_MIN          = 5    # minutes per analysis window
TRAJ_MIN_WINDOWS         = 2    # minimum populated windows before analysis
TRAJ_APPROACH_SLOPE      = 0.5  # km/window threshold for a significant trend
TRAJ_APPROACH_COOLDOWN   = 5    # minutes between alerts when AVVICINAMENTO
TRAJ_AWAY_COOLDOWN       = 30   # minutes between alerts when ALLONTANAMENTO
TRAJ_STATIONARY_COOLDOWN = 15   # minutes between alerts when STAZIONARIO


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


# ── Trajectory analysis ───────────────────────────────────────────────────────

def _linear_regression_slope(xs: list[float], ys: list[float]) -> float:
    """Return the slope of the least-squares line through the given points."""
    n = len(xs)
    if n < 2:
        return 0.0
    sx  = sum(xs)
    sy  = sum(ys)
    sxy = sum(x * y for x, y in zip(xs, ys))
    sxx = sum(x * x for x in xs)
    denom = n * sxx - sx * sx
    if denom == 0.0:
        return 0.0
    return (n * sxy - sx * sy) / denom


def _classify_storm(n_windows: int, slope_per_window: float) -> str:
    if n_windows < TRAJ_MIN_WINDOWS:
        return "UNKNOWN"
    if n_windows < 3:
        return "STAZIONARIO"
    if slope_per_window <= -TRAJ_APPROACH_SLOPE:
        return "AVVICINAMENTO"
    if slope_per_window >= TRAJ_APPROACH_SLOPE:
        return "ALLONTANAMENTO"
    return "STAZIONARIO"


def _build_traj_message(
    stato: str,
    dist: float,
    az: float,
    velocity: float | None,
    eta: int | None,
    density_trend: str,
    lang: str,
) -> str:
    dir_lbl = _direction(az, lang)
    if lang == "it":
        if stato == "AVVICINAMENTO":
            msg = f"Temporale in avvicinamento da {dir_lbl}"
            if velocity:
                msg += f" a ~{velocity:.0f} km/h"
            if eta:
                msg += f", arrivo stimato tra {eta} minuti"
        elif stato == "ALLONTANAMENTO":
            msg = f"Temporale in allontanamento verso {dir_lbl}"
            if velocity:
                msg += f" a ~{velocity:.0f} km/h"
        elif stato == "STAZIONARIO":
            msg = f"Temporale stazionario a {dist:.1f} km ({dir_lbl})"
        else:
            msg = f"Situazione non determinata — {dist:.1f} km"
        if density_trend == "CRESCENTE":
            msg += " · intensità crescente"
        elif density_trend == "CALANTE":
            msg += " · intensità in calo"
    else:
        if stato == "AVVICINAMENTO":
            msg = f"Storm approaching from {dir_lbl}"
            if velocity:
                msg += f" at ~{velocity:.0f} km/h"
            if eta:
                msg += f", estimated arrival in {eta} min"
        elif stato == "ALLONTANAMENTO":
            msg = f"Storm moving away toward {dir_lbl}"
            if velocity:
                msg += f" at ~{velocity:.0f} km/h"
        elif stato == "STAZIONARIO":
            msg = f"Stationary storm at {dist:.1f} km ({dir_lbl})"
        else:
            msg = f"Undetermined — {dist:.1f} km"
        if density_trend == "CRESCENTE":
            msg += " · increasing intensity"
        elif density_trend == "CALANTE":
            msg += " · decreasing intensity"
    return msg


def _analyze_trajectory(
    buffer: list,
    ref_lat: float,
    ref_lon: float,
    lang: str = "it",
) -> dict:
    """
    Analyse the trajectory of a storm from the sliding window buffer.

    Each buffer element is a tuple (timestamp_s, lat, lon, dist_km).
    Returns a dict with stato, distanza_attuale_km, velocita_kmh,
    direzione_gradi, eta_minuti, densita_trend, messaggio.
    """
    current_dist = buffer[-1][3] if buffer else 0.0

    def _unknown(msg: str) -> dict:
        return {
            "stato": "UNKNOWN",
            "distanza_attuale_km": current_dist,
            "velocita_kmh": None,
            "direzione_gradi": None,
            "eta_minuti": None,
            "densita_trend": "STABILE",
            "messaggio": msg,
        }

    if not buffer:
        return _unknown(
            "Dati insufficienti per analisi traiettoria"
            if lang == "it" else
            "Insufficient data for trajectory analysis"
        )

    t0      = buffer[0][0]
    win_sec = TRAJ_WINDOW_MIN * 60

    # Group events into time windows
    windows_map: dict[int, list] = {}
    for ts, lat, lon, dist in buffer:
        idx = int((ts - t0) / win_sec)
        windows_map.setdefault(idx, []).append((ts, lat, lon, dist))

    # Build per-window summaries
    win_data = []
    for idx in sorted(windows_map):
        evts    = windows_map[idx]
        c_lat   = sum(e[1] for e in evts) / len(evts)
        c_lon   = sum(e[2] for e in evts) / len(evts)
        c_dist  = sum(e[3] for e in evts) / len(evts)
        t_min   = idx * TRAJ_WINDOW_MIN
        win_data.append({"t_min": t_min, "lat": c_lat, "lon": c_lon,
                         "dist": c_dist, "count": len(evts)})

    n_windows = len(win_data)
    if n_windows < TRAJ_MIN_WINDOWS:
        return _unknown(
            "Dati insufficienti per analisi traiettoria"
            if lang == "it" else
            "Insufficient data for trajectory analysis"
        )

    # Linear regression: slope in km/min, convert to km/window
    xs = [w["t_min"] for w in win_data]
    ys = [w["dist"]  for w in win_data]
    slope_km_per_min  = _linear_regression_slope(xs, ys)
    slope_per_window  = slope_km_per_min * TRAJ_WINDOW_MIN

    # Velocity from centroid displacement between first and last window
    fw, lw   = win_data[0], win_data[-1]
    time_h   = (lw["t_min"] - fw["t_min"]) / 60.0
    c_dist_km = _distance_km(fw["lat"], fw["lon"], lw["lat"], lw["lon"])
    velocity  = round(c_dist_km / time_h, 1) if time_h > 0 else None

    # Direction: azimuth from reference to latest centroid (where the storm is)
    az = _azimuth_deg(ref_lat, ref_lon, lw["lat"], lw["lon"])

    # Density trend: compare lightning rate in first vs second half of buffer
    mid         = max(len(buffer) // 2, 1)
    first_half  = buffer[:mid]
    second_half = buffer[mid:]
    t_first  = max((first_half[-1][0]  - first_half[0][0])  / 60.0, 0.1) if len(first_half)  > 1 else 0.1
    t_second = max((second_half[-1][0] - second_half[0][0]) / 60.0, 0.1) if len(second_half) > 1 else 0.1
    rate_first  = len(first_half)  / t_first
    rate_second = len(second_half) / t_second
    if rate_second > rate_first * 1.2:
        density_trend = "CRESCENTE"
    elif rate_second < rate_first * 0.8:
        density_trend = "CALANTE"
    else:
        density_trend = "STABILE"

    stato = _classify_storm(n_windows, slope_per_window)

    # ETA only when approaching and velocity is meaningful
    eta = None
    if stato == "AVVICINAMENTO" and velocity and velocity > 0:
        eta = int(current_dist / (velocity / 60))

    messaggio = _build_traj_message(stato, current_dist, az, velocity, eta, density_trend, lang)

    return {
        "stato":              stato,
        "distanza_attuale_km": round(current_dist, 1),
        "velocita_kmh":       velocity,
        "direzione_gradi":    round(az, 1),
        "eta_minuti":         eta,
        "densita_trend":      density_trend,
        "messaggio":          messaggio,
    }


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
        self._last_alert: float = 0.0
        self._buffer: list      = []   # list of (ts, lat, lon, dist) tuples
        self._task: asyncio.Task | None     = None
        self._avv_task: asyncio.Task | None = None  # periodic AVVICINAMENTO heartbeat

    def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self._run(), name=f"live_lightning:{self.monitor_id}"
            )
            print(f"[LiveMonitor] '{self.name}' started "
                  f"(radius={self.radius_km:.0f}km, adaptive cooldown)")

    def stop(self):
        if self._avv_task and not self._avv_task.done():
            self._avv_task.cancel()
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

    def _start_avvicinamento_loop(self) -> None:
        if self._avv_task and not self._avv_task.done():
            self._avv_task.cancel()
        self._avv_task = asyncio.create_task(
            self._avvicinamento_loop(), name=f"avv_loop:{self.monitor_id}"
        )

    async def _avvicinamento_loop(self) -> None:
        """Sends a periodic AVVICINAMENTO update every TRAJ_APPROACH_COOLDOWN minutes
        even when no new MQTT strikes arrive, as long as the buffer still classifies
        the storm as approaching."""
        while True:
            await asyncio.sleep(TRAJ_APPROACH_COOLDOWN * 60)
            if not self._buffer:
                return
            analysis = _analyze_trajectory(self._buffer, self.lat, self.lon, self.language)
            if analysis["stato"] != "AVVICINAMENTO":
                return

            # Extrapolate current distance: storm keeps moving even without new strikes.
            # Without this, every heartbeat would repeat identical distance/ETA since the
            # buffer is unchanged when no new MQTT messages arrive.
            velocity = analysis.get("velocita_kmh")
            if velocity and velocity > 0:
                elapsed_min = (time.time() - self._buffer[-1][0]) / 60.0
                if elapsed_min > 0:
                    est_dist = analysis["distanza_attuale_km"] - (velocity / 60.0) * elapsed_min
                    est_dist = max(0.1, round(est_dist, 1))
                    analysis["distanza_attuale_km"] = est_dist
                    analysis["eta_minuti"] = int(est_dist / (velocity / 60)) if est_dist > 0.1 else 0

            last      = self._buffer[-1]
            az        = _azimuth_deg(self.lat, self.lon, last[1], last[2])
            direction = _direction(az, self.language)
            self._last_alert = time.time()
            _LOGGER.info(
                "[Trajectory-loop] %s | state=AVVICINAMENTO dist=%.1fkm eta=%s buf=%d events",
                self.name, analysis["distanza_attuale_km"], analysis.get("eta_minuti"),
                len(self._buffer),
            )
            try:
                await self._send(
                    self._format_trajectory_alert(
                        analysis["distanza_attuale_km"], az, direction, analysis
                    )
                )
            except Exception as e:
                print(f"[LiveMonitor] '{self.name}' avv-loop send error: {e}")

    def _add_to_buffer(self, ts: float, lat: float, lon: float, dist: float) -> None:
        self._buffer.append((ts, lat, lon, dist))
        cutoff = ts - TRAJ_BUFFER_MIN * 60
        while self._buffer and self._buffer[0][0] < cutoff:
            self._buffer.pop(0)

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
        self._add_to_buffer(now, float(s_lat), float(s_lon), dist)

        analysis = _analyze_trajectory(self._buffer, self.lat, self.lon, self.language)
        stato    = analysis["stato"]

        _LOGGER.info(
            "[Trajectory] %s | state=%s dist=%.1fkm vel=%s dir=%s° buf=%d events",
            self.name, stato, analysis["distanza_attuale_km"],
            analysis.get("velocita_kmh"), analysis.get("direzione_gradi"),
            len(self._buffer),
        )

        if stato in ("UNKNOWN", "STAZIONARIO"):
            return

        cooldown_map = {
            "AVVICINAMENTO":  TRAJ_APPROACH_COOLDOWN,
            "ALLONTANAMENTO": TRAJ_AWAY_COOLDOWN,
            "STAZIONARIO":    TRAJ_STATIONARY_COOLDOWN,
        }
        effective_cooldown = cooldown_map.get(stato, TRAJ_STATIONARY_COOLDOWN)

        if (now - self._last_alert) / 60.0 < effective_cooldown:
            return

        self._last_alert = now
        az        = _azimuth_deg(self.lat, self.lon, float(s_lat), float(s_lon))
        direction = _direction(az, self.language)
        print(f"[LiveMonitor] '{self.name}' alert: {dist:.1f}km {direction} ({stato})")
        try:
            if stato in ("AVVICINAMENTO", "ALLONTANAMENTO"):
                await self._send(self._format_trajectory_alert(dist, az, direction, analysis))
                if stato == "AVVICINAMENTO":
                    self._start_avvicinamento_loop()
            else:
                await self._send(self._format_alert(dist, az, direction, stato))
        except Exception as e:
            print(f"[LiveMonitor] '{self.name}' send error: {e}")

    def _next_alert_label(self, stato: str) -> str:
        cooldown_map = {
            "AVVICINAMENTO":  TRAJ_APPROACH_COOLDOWN,
            "ALLONTANAMENTO": TRAJ_AWAY_COOLDOWN,
            "STAZIONARIO":    TRAJ_STATIONARY_COOLDOWN,
        }
        minutes = cooldown_map.get(stato, TRAJ_STATIONARY_COOLDOWN)
        if self.language == "it":
            return f"Prossimo alert tra {minutes} min"
        return f"Next alert in {minutes} min"

    def _format_alert(self, dist: float, az: float, direction: str, stato: str = "UNKNOWN") -> str:
        try:
            tz = ZoneInfo(self.tz_name)
        except ZoneInfoNotFoundError:
            tz = ZoneInfo("UTC")
        now_str = datetime.now(tz).strftime("%H:%M")
        loc     = html.escape(self.location or self.name)
        next_lbl = self._next_alert_label(stato)
        if self.language == "it":
            return (
                f"⚡ <b>Fulmine rilevato — {loc}</b>\n"
                f"📍 Distanza: <b>{dist:.1f} km</b> a {direction} ({az:.0f}°)\n"
                f"🔕 {next_lbl}\n"
                f"🕐 {now_str}"
            )
        return (
            f"⚡ <b>Lightning detected — {loc}</b>\n"
            f"📍 Distance: <b>{dist:.1f} km</b> to {direction} ({az:.0f}°)\n"
            f"🔕 {next_lbl}\n"
            f"🕐 {now_str}"
        )

    def _format_trajectory_alert(
        self, dist: float, az: float, direction: str, analysis: dict
    ) -> str:
        try:
            tz = ZoneInfo(self.tz_name)
        except ZoneInfoNotFoundError:
            tz = ZoneInfo("UTC")
        now_str     = datetime.now(tz).strftime("%H:%M")
        loc         = html.escape(self.location or self.name)
        stato       = analysis["stato"]
        vel         = analysis.get("velocita_kmh")
        eta         = analysis.get("eta_minuti")
        density     = analysis.get("densita_trend", "STABILE")
        next_lbl    = self._next_alert_label(stato)

        if self.language == "it":
            icon  = "🔴" if stato == "AVVICINAMENTO" else "🟢"
            lines = [f"⚡ <b>Fulmine rilevato — {loc}</b>"]
            lines.append(f"📍 Distanza: <b>{dist:.1f} km</b> a {direction} ({az:.0f}°)")
            if stato == "AVVICINAMENTO":
                if vel:
                    lines.append(f"{icon} In avvicinamento a ~{vel:.0f} km/h")
                if eta:
                    lines.append(f"⏱ Arrivo stimato: <b>{eta} min</b>")
            elif stato == "ALLONTANAMENTO":
                if vel:
                    lines.append(f"{icon} In allontanamento a ~{vel:.0f} km/h")
            if density == "CRESCENTE":
                lines.append("⚡ Intensità in aumento")
            elif density == "CALANTE":
                lines.append("📉 Intensità in diminuzione")
            lines.append(f"🔕 {next_lbl}")
            lines.append(f"🕐 {now_str}")
        else:
            icon  = "🔴" if stato == "AVVICINAMENTO" else "🟢"
            lines = [f"⚡ <b>Lightning detected — {loc}</b>"]
            lines.append(f"📍 Distance: <b>{dist:.1f} km</b> to {direction} ({az:.0f}°)")
            if stato == "AVVICINAMENTO":
                if vel:
                    lines.append(f"{icon} Approaching at ~{vel:.0f} km/h")
                if eta:
                    lines.append(f"⏱ Estimated arrival: <b>{eta} min</b>")
            elif stato == "ALLONTANAMENTO":
                if vel:
                    lines.append(f"{icon} Moving away at ~{vel:.0f} km/h")
            if density == "CRESCENTE":
                lines.append("⚡ Increasing intensity")
            elif density == "CALANTE":
                lines.append("📉 Decreasing intensity")
            lines.append(f"🔕 {next_lbl}")
            lines.append(f"🕐 {now_str}")

        return "\n".join(lines)


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
