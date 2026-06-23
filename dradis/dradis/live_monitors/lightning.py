"""
live_monitors/lightning.py
───────────────────────────
LLM-free live monitor: persistent MQTT listener on the Blitzortung public broker.
Sends Telegram alerts based on DBSCAN storm cell tracking.

Algorithm
─────────
- Connects once to blitzortung.ha.sed.pl:1883 and stays connected.
- Subscribes to geohash-based topics covering the configured area.
- Buffers all incoming strikes within radius_km in a 15-minute sliding window.
- Every 2 minutes a polling task runs pure-Python DBSCAN (eps=8 km, min_samples=2)
  to identify storm cells, tracks each cell's centroid over a 20-minute history,
  and classifies each cell as APPROACHING / RETREATING / STATIONARY / UNKNOWN.
- Alerts are zone-based: <15 km · 15-30 km · 30-50 km · >50 km.
  Triggered on: initial detection, zone crossing, 10-min periodic re-alert (if
  APPROACHING), and "all clear" after 15 min with no new strikes.
- Hard cooldown of 5 min between alerts per cluster (except all-clear).

One LightningLiveMonitor instance per enabled live monitor entry.
All instances are owned by LiveMonitorManager (singleton live_monitor_manager).
Called by main.py on startup and on config changes — NOT via the APScheduler cron.
"""

import asyncio
import bisect
import html
import json
import math
import time
import logging
from collections import namedtuple
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiomqtt

_LOGGER = logging.getLogger(__name__)

MQTT_HOST       = "blitzortung.ha.sed.pl"
MQTT_PORT       = 1883
RECONNECT_DELAY = 15

# ── DBSCAN & clustering ────────────────────────────────────────────────────────
DBSCAN_EPS_KM        = 8.0
DBSCAN_MIN_SAMPLES   = 2
STRIKE_BUFFER_MIN    = 15
CENTROID_HISTORY_MIN = 20

# ── State classification ───────────────────────────────────────────────────────
TREND_SAMPLES     = 3
APPROACH_SLOPE_KM = -0.5
RETREAT_SLOPE_KM  =  0.5

# ── Distance zones (km) ───────────────────────────────────────────────────────
ZONES = [15, 30, 50]   # bisect.bisect(ZONES, dist) → 0=<15, 1=15-30, 2=30-50, 3=>50

# ── Alert timing ──────────────────────────────────────────────────────────────
POLL_INTERVAL_SEC    = 120
PERIODIC_ALERT_MIN   = 10
CLUSTER_COOLDOWN_MIN = 5
CLUSTER_GONE_MIN     = 15
MAX_MATCH_KM         = 20


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


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class StormCluster:
    cluster_id: str
    centroid_history: list   # [(ts, lat, lon, dist_km), ...]
    state: str               # APPROACHING | RETREATING | STATIONARY | UNKNOWN
    zone: int                # bisect index into ZONES
    last_alert_ts: float
    last_periodic_ts: float
    initial_alert_sent: bool
    all_clear_sent: bool
    last_seen_ts: float


AlertEvent = namedtuple("AlertEvent", ["kind", "cluster", "old_zone"])
# kind ∈ {"initial", "zone", "periodic", "all_clear"}
# old_zone: int for "zone" events, None otherwise


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


# ── Cluster helpers ───────────────────────────────────────────────────────────

def _match_clusters(
    new_centroids: dict,
    existing: dict,
) -> tuple:
    """Greedy nearest-centroid matching. Returns (matched, unmatched_new, gone)."""
    consumed: set = set()
    matched: dict = {}   # cluster_id → raw_label

    for cid, cluster in existing.items():
        if not cluster.centroid_history:
            continue
        last = cluster.centroid_history[-1]
        best_dist = float("inf")
        best_raw = None
        for raw_label, (clat, clon) in new_centroids.items():
            if raw_label in consumed:
                continue
            d = _distance_km(last[1], last[2], clat, clon)
            if d < best_dist:
                best_dist = d
                best_raw = raw_label
        if best_raw is not None and best_dist <= MAX_MATCH_KM:
            matched[cid] = best_raw
            consumed.add(best_raw)

    unmatched_new = [r for r in new_centroids if r not in consumed]
    gone = [cid for cid in existing if cid not in matched]
    return matched, unmatched_new, gone


def _classify_state(cluster: StormCluster) -> str:
    hist = cluster.centroid_history
    if len(hist) < TREND_SAMPLES:
        return "UNKNOWN"
    dists = [h[3] for h in hist[-TREND_SAMPLES:]]
    diffs = [dists[i + 1] - dists[i] for i in range(len(dists) - 1)]
    if all(d < APPROACH_SLOPE_KM for d in diffs):
        return "APPROACHING"
    if all(d > RETREAT_SLOPE_KM for d in diffs):
        return "RETREATING"
    return "STATIONARY"


def _compute_velocity_eta(cluster: StormCluster) -> tuple:
    hist = cluster.centroid_history
    if len(hist) < 2:
        return None, None
    first, last = hist[0], hist[-1]
    time_h = (last[0] - first[0]) / 3600.0
    if time_h <= 0:
        return None, None
    displacement_km = _distance_km(first[1], first[2], last[1], last[2])
    velocity = round(displacement_km / time_h, 1) if displacement_km > 0 else None
    # ETA from distance decrease rate (not raw centroid displacement)
    approach_rate_kmh = -(last[3] - first[3]) / time_h   # positive = approaching
    eta_min = None
    if approach_rate_kmh > 0 and last[3] > 0:
        eta_min = int(last[3] / (approach_rate_kmh / 60.0))
    return velocity, eta_min


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
        self._strike_buffer: list = []          # [(ts, lat, lon, dist_km), ...]
        self._clusters: dict[str, StormCluster] = {}
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
                  f"(radius={self.radius_km:.0f}km, DBSCAN clustering)")

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
        self._add_to_buffer(time.time(), float(s_lat), float(s_lon), dist)

    def _add_to_buffer(self, ts: float, lat: float, lon: float, dist: float) -> None:
        self._strike_buffer.append((ts, lat, lon, dist))

    # ── Polling task ──────────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(POLL_INTERVAL_SEC)
                now = time.time()
                alerts = self._update_clusters(now)
                for alert in alerts:
                    await self._dispatch_alert(alert, now)
                # All-clear for clusters gone >= CLUSTER_GONE_MIN minutes
                for cid in list(self._clusters):
                    cluster = self._clusters[cid]
                    if ((now - cluster.last_seen_ts) >= CLUSTER_GONE_MIN * 60
                            and cluster.initial_alert_sent
                            and not cluster.all_clear_sent):
                        await self._dispatch_alert(AlertEvent("all_clear", cluster, None), now)
                # Remove clusters that have been cleaned up
                for cid in list(self._clusters):
                    cluster = self._clusters[cid]
                    if (now - cluster.last_seen_ts) >= CLUSTER_GONE_MIN * 60:
                        if cluster.all_clear_sent or not cluster.initial_alert_sent:
                            del self._clusters[cid]
            except asyncio.CancelledError:
                return
            except Exception as e:
                _LOGGER.error("[LiveMonitor] '%s' poll error: %s", self.name, e)

    # ── Clustering core ───────────────────────────────────────────────────────

    def _update_clusters(self, now: float) -> list:
        cutoff = now - STRIKE_BUFFER_MIN * 60
        self._strike_buffer = [(t, la, lo, d) for t, la, lo, d in self._strike_buffer
                               if t >= cutoff]

        alerts: list = []
        if len(self._strike_buffer) < DBSCAN_MIN_SAMPLES:
            return alerts

        points = [(la, lo) for _, la, lo, _ in self._strike_buffer]
        labels = _dbscan(points, DBSCAN_EPS_KM, DBSCAN_MIN_SAMPLES)

        cluster_entries: dict[int, list] = {}
        for idx, label in enumerate(labels):
            if label != -1:
                cluster_entries.setdefault(label, []).append(self._strike_buffer[idx])

        new_centroids: dict[int, tuple] = {
            label: (
                sum(e[1] for e in entries) / len(entries),
                sum(e[2] for e in entries) / len(entries),
            )
            for label, entries in cluster_entries.items()
        }

        matched, unmatched_new, _ = _match_clusters(new_centroids, self._clusters)
        alerted: set[str] = set()

        for cid, raw_label in matched.items():
            cluster = self._clusters[cid]
            clat, clon = new_centroids[raw_label]
            dist = _distance_km(self.lat, self.lon, clat, clon)

            cluster.centroid_history.append((now, clat, clon, dist))
            h_cutoff = now - CENTROID_HISTORY_MIN * 60
            cluster.centroid_history = [h for h in cluster.centroid_history if h[0] >= h_cutoff]
            cluster.last_seen_ts = now
            cluster.state = _classify_state(cluster)

            new_zone = bisect.bisect(ZONES, dist)
            old_zone = cluster.zone
            can_alert = (now - cluster.last_alert_ts) >= CLUSTER_COOLDOWN_MIN * 60

            if not cluster.initial_alert_sent and can_alert:
                cluster.zone = new_zone
                cluster.last_alert_ts = now
                alerts.append(AlertEvent("initial", cluster, None))
                alerted.add(cid)
            elif can_alert and new_zone != old_zone:
                approaching_zone = new_zone < old_zone
                retreating_zone = new_zone > old_zone and cluster.state == "RETREATING"
                if approaching_zone or retreating_zone:
                    cluster.last_alert_ts = now
                    alerts.append(AlertEvent("zone", cluster, old_zone))
                    alerted.add(cid)
                cluster.zone = new_zone
            else:
                cluster.zone = new_zone

            if (cid not in alerted
                    and cluster.initial_alert_sent
                    and cluster.state == "APPROACHING"
                    and can_alert
                    and (now - cluster.last_periodic_ts) >= PERIODIC_ALERT_MIN * 60):
                cluster.last_alert_ts = now
                cluster.last_periodic_ts = now
                alerts.append(AlertEvent("periodic", cluster, None))
                alerted.add(cid)

        for raw_label in unmatched_new:
            clat, clon = new_centroids[raw_label]
            dist = _distance_km(self.lat, self.lon, clat, clon)
            cid = self._make_cluster_id(clat, clon)
            cluster = StormCluster(
                cluster_id=cid,
                centroid_history=[(now, clat, clon, dist)],
                state="UNKNOWN",
                zone=bisect.bisect(ZONES, dist),
                last_alert_ts=now,
                last_periodic_ts=0.0,
                initial_alert_sent=False,
                all_clear_sent=False,
                last_seen_ts=now,
            )
            self._clusters[cid] = cluster
            alerts.append(AlertEvent("initial", cluster, None))

        return alerts

    def _make_cluster_id(self, lat: float, lon: float) -> str:
        base = f"{round(lat, 1):.1f}:{round(lon, 1):.1f}"
        if base not in self._clusters:
            return base
        n = 1
        while f"{base}#{n}" in self._clusters:
            n += 1
        return f"{base}#{n}"

    # ── Alert dispatch ────────────────────────────────────────────────────────

    async def _dispatch_alert(self, alert: AlertEvent, now: float) -> None:
        cluster = alert.cluster
        kind = alert.kind

        if kind == "initial":
            msg = self._fmt_initial(cluster)
        elif kind == "zone":
            msg = self._fmt_zone_change(cluster, alert.old_zone)
        elif kind == "periodic":
            msg = self._fmt_periodic(cluster)
        elif kind == "all_clear":
            msg = self._fmt_all_clear(cluster)
        else:
            return

        _LOGGER.info(
            "[LiveMonitor] %s | kind=%s cluster=%s dist=%.1fkm state=%s",
            self.name, kind, cluster.cluster_id,
            cluster.centroid_history[-1][3] if cluster.centroid_history else 0,
            cluster.state,
        )
        try:
            # Promote the flags ONLY on confirmed delivery. send_telegram swallows
            # its own exceptions and returns False on failure, so awaiting it never
            # raises — we must check the return value, otherwise a rejected initial
            # alert (e.g. HTML parse error) would still flip initial_alert_sent and
            # let all_clear/periodic fire without the user ever seeing a warning.
            ok = await self._send(msg)
            if ok:
                if kind == "initial":
                    cluster.initial_alert_sent = True
                elif kind == "all_clear":
                    cluster.all_clear_sent = True
            else:
                _LOGGER.warning(
                    "[LiveMonitor] '%s' alert kind=%s NOT delivered — flag not promoted, "
                    "will retry next poll", self.name, kind,
                )
        except Exception as e:
            _LOGGER.error("[LiveMonitor] '%s' send error: %s", self.name, e)

    # ── Message formatters ────────────────────────────────────────────────────

    def _fmt_initial(self, cluster: StormCluster) -> str:
        dist, az, dir_lbl = self._cluster_az_dir(cluster)
        loc = html.escape(self.location or self.name)
        zone_lbl = self._zone_label(cluster.zone)
        state_lbl = self._state_label(cluster.state)
        if self.language == "it":
            lines = [f"⚡ <b>Temporale rilevato — {loc}</b>",
                     f"📍 Distanza: <b>{dist:.1f} km</b> a {dir_lbl} ({az:.0f}°)",
                     f"🏷 Zona: {zone_lbl}",
                     f"🟡 Stato: {state_lbl}"]
        else:
            lines = [f"⚡ <b>Storm detected — {loc}</b>",
                     f"📍 Distance: <b>{dist:.1f} km</b> to {dir_lbl} ({az:.0f}°)",
                     f"🏷 Zone: {zone_lbl}",
                     f"🟡 Status: {state_lbl}"]
        lines.append(f"🕐 {self._now_str()}")
        return "\n".join(lines)

    def _fmt_zone_change(self, cluster: StormCluster, old_zone: int) -> str:
        dist, az, dir_lbl = self._cluster_az_dir(cluster)
        vel, eta = _compute_velocity_eta(cluster)
        loc = html.escape(self.location or self.name)
        zone_lbl = self._zone_label(cluster.zone)
        approaching = cluster.zone < old_zone
        if self.language == "it":
            header = (f"🔴 <b>Temporale in avvicinamento — {loc}</b>"
                      if approaching else
                      f"🟢 <b>Temporale in allontanamento — {loc}</b>")
            lines = [header,
                     f"📍 Distanza: <b>{dist:.1f} km</b> a {dir_lbl} ({az:.0f}°)",
                     f"🏷 Zona: {zone_lbl}"]
            if vel:
                if approaching and eta:
                    lines.append(f"🚀 ~{vel:.0f} km/h — arrivo stimato: {eta} min")
                else:
                    lines.append(f"🚀 ~{vel:.0f} km/h")
        else:
            header = (f"🔴 <b>Storm approaching — {loc}</b>"
                      if approaching else
                      f"🟢 <b>Storm retreating — {loc}</b>")
            lines = [header,
                     f"📍 Distance: <b>{dist:.1f} km</b> to {dir_lbl} ({az:.0f}°)",
                     f"🏷 Zone: {zone_lbl}"]
            if vel:
                if approaching and eta:
                    lines.append(f"🚀 ~{vel:.0f} km/h — estimated arrival: {eta} min")
                else:
                    lines.append(f"🚀 ~{vel:.0f} km/h")
        lines.append(f"🕐 {self._now_str()}")
        return "\n".join(lines)

    def _fmt_periodic(self, cluster: StormCluster) -> str:
        dist, az, dir_lbl = self._cluster_az_dir(cluster)
        vel, eta = _compute_velocity_eta(cluster)
        loc = html.escape(self.location or self.name)
        strikes = len(self._strike_buffer)
        if self.language == "it":
            lines = [f"🔴 <b>Temporale ancora in avvicinamento — {loc}</b>",
                     f"📍 Distanza: <b>{dist:.1f} km</b> a {dir_lbl} ({az:.0f}°)"]
            if vel:
                if eta:
                    lines.append(f"🚀 ~{vel:.0f} km/h — arrivo stimato: {eta} min")
                else:
                    lines.append(f"🚀 ~{vel:.0f} km/h")
            lines.append(f"🔢 Colpi ultimi {STRIKE_BUFFER_MIN} min: {strikes}")
        else:
            lines = [f"🔴 <b>Storm still approaching — {loc}</b>",
                     f"📍 Distance: <b>{dist:.1f} km</b> to {dir_lbl} ({az:.0f}°)"]
            if vel:
                if eta:
                    lines.append(f"🚀 ~{vel:.0f} km/h — estimated arrival: {eta} min")
                else:
                    lines.append(f"🚀 ~{vel:.0f} km/h")
            lines.append(f"🔢 Strikes (last {STRIKE_BUFFER_MIN} min): {strikes}")
        lines.append(f"🕐 {self._now_str()}")
        return "\n".join(lines)

    def _fmt_all_clear(self, cluster: StormCluster) -> str:
        loc = html.escape(self.location or self.name)
        if self.language == "it":
            return (f"✅ <b>Temporale dissolto — {loc}</b>\n"
                    f"🔇 Nessun fulmine negli ultimi {CLUSTER_GONE_MIN} min\n"
                    f"🕐 {self._now_str()}")
        return (f"✅ <b>Storm cleared — {loc}</b>\n"
                f"🔇 No lightning in the last {CLUSTER_GONE_MIN} min\n"
                f"🕐 {self._now_str()}")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _cluster_az_dir(self, cluster: StormCluster) -> tuple:
        if not cluster.centroid_history:
            return 0.0, 0.0, "N"
        last = cluster.centroid_history[-1]
        az = _azimuth_deg(self.lat, self.lon, last[1], last[2])
        return last[3], az, _direction(az, self.language)

    def _zone_label(self, zone_idx: int) -> str:
        idx = min(zone_idx, 3)
        # &lt;/&gt; entities: these labels are inserted into HTML (parse_mode=HTML)
        # Telegram messages; a raw '<' is read as a malformed tag and the whole
        # message is rejected with "can't parse entities".
        if self.language == "it":
            return ["Zona pericolo (&lt;15 km)", "Zona vicina (15–30 km)",
                    "Zona intermedia (30–50 km)", "Zona distante (&gt;50 km)"][idx]
        return ["Danger zone (&lt;15 km)", "Near zone (15–30 km)",
                "Intermediate zone (30–50 km)", "Distant zone (&gt;50 km)"][idx]

    def _state_label(self, state: str) -> str:
        if self.language == "it":
            return {"APPROACHING": "In avvicinamento", "RETREATING": "In allontanamento",
                    "STATIONARY": "Stazionario", "UNKNOWN": "Non determinato"}.get(state, state)
        return {"APPROACHING": "Approaching", "RETREATING": "Retreating",
                "STATIONARY": "Stationary", "UNKNOWN": "Undetermined"}.get(state, state)

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
