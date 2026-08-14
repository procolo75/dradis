"""
live_monitors/blitzortung.py
─────────────────────────────
Ingest half of the storm front monitor: a persistent MQTT listener on the public
Blitzortung broker. The maths lives in `geo.py`.

Nothing here makes a decision. The feed only answers "which strikes have I seen
recently, and is my connection healthy" — everything else lives in
`storm_front_core`, which is pure and unit-testable.

Why the feed is a separate object
─────────────────────────────────
Health and decisions must not be entangled. A dead socket and a clear sky look
identical if you only count strikes, and a monitor that cannot tell them apart
will happily announce "storm cleared" while it is simply deaf. The feed
therefore exposes `feed_ok()` and `connected_for()` as first-class inputs, and
the decision core refuses to close an event unless the connection has actually
been up long enough to have seen the sky.
"""

import asyncio
import json
import logging
import time

import aiomqtt

from .geo import topics_for_area

_LOGGER = logging.getLogger(__name__)

MQTT_HOST       = "blitzortung.ha.sed.pl"
MQTT_PORT       = 1883
RECONNECT_DELAY = 15

# Connected but silent for this long → report "degraded" rather than pretending
# everything is fine.
DEGRADED_SILENCE_SEC = 900
# Hard cap so a burst between polls cannot grow the buffer without bound.
MAX_BUFFER_STRIKES = 20000


# ── Feed ──────────────────────────────────────────────────────────────────────

class BlitzortungFeed:
    """One persistent MQTT connection, buffering recent strikes as (t, lat, lon).

    Distances are NOT computed here. The buffer is pure observation; the geometry
    is derived at evaluation time against the current origin.
    """

    def __init__(self, name: str, monitor_id: str, lat: float, lon: float,
                 coverage_radius_km: float, window_sec: float):
        self.name       = name
        self.monitor_id = monitor_id
        self.lat        = lat
        self.lon        = lon
        self.coverage_radius_km = coverage_radius_km
        self.window_sec = window_sec

        self._buffer: list[tuple[float, float, float]] = []

        self._messages       = 0
        self._parse_errors   = 0
        self._dropped_stale  = 0
        self._last_msg_ts    = 0.0
        self._connected      = False
        self._connected_since = 0.0
        self._connect_failures = 0

        self._task: asyncio.Task | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(
            self._run(), name=f"blitzortung:{self.monitor_id}"
        )

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()

    async def aclose(self) -> None:
        """Cancel and wait, so the old client is fully disconnected before a
        replacement connects."""
        task = self._task
        self.stop()
        if task:
            await asyncio.gather(task, return_exceptions=True)

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def status(self) -> str:
        if not self.is_running():
            return "stopped"
        if self._connect_failures >= 3:
            return "degraded"
        if self._last_msg_ts and time.time() - self._last_msg_ts > DEGRADED_SILENCE_SEC:
            return "degraded"
        if not self._connected:
            return "degraded"
        return "running"

    # ── Health, as inputs to the decision core ────────────────────────────────

    def feed_ok(self) -> bool:
        return self._connected

    def connected_for(self, now: float) -> float:
        """Seconds since the current connection came up, 0.0 while disconnected.

        The core uses this to refuse an all-clear it has not actually earned:
        a monitor that just reconnected has not been watching the sky yet.
        """
        if not self._connected or not self._connected_since:
            return 0.0
        return max(0.0, now - self._connected_since)

    def strikes(self, now: float) -> list[tuple[float, float, float]]:
        """Window-trimmed view of the buffer. Trimming here rather than on ingest
        keeps `_on_message` as short as possible."""
        cutoff = now - self.window_sec
        self._buffer = [s for s in self._buffer if s[0] >= cutoff]
        return self._buffer

    def stats(self) -> dict:
        return {
            "messages": self._messages,
            "parse_errors": self._parse_errors,
            "dropped_stale": self._dropped_stale,
            "buffered": len(self._buffer),
        }

    # ── MQTT ──────────────────────────────────────────────────────────────────

    async def _run(self) -> None:
        topics = topics_for_area(self.lat, self.lon, self.coverage_radius_km)
        while True:
            try:
                print(f"[StormFront] '{self.name}' connecting to {MQTT_HOST}:{MQTT_PORT} "
                      f"({len(topics)} topics)")
                async with aiomqtt.Client(MQTT_HOST, MQTT_PORT) as client:
                    for topic in topics:
                        await client.subscribe(topic)
                    self._connected = True
                    self._connected_since = time.time()
                    self._connect_failures = 0
                    async for message in client.messages:
                        self._on_message(message)
            except asyncio.CancelledError:
                self._connected = False
                return
            except Exception as e:
                self._connected = False
                self._connected_since = 0.0
                self._connect_failures += 1
                print(f"[StormFront] '{self.name}' disconnected: {e} "
                      f"— retry in {RECONNECT_DELAY}s (failures={self._connect_failures})")
                await asyncio.sleep(RECONNECT_DELAY)

    def _on_message(self, message) -> None:
        self._messages += 1
        now = time.time()
        self._last_msg_ts = now
        try:
            data = json.loads(message.payload)
        except (ValueError, TypeError):
            self._parse_errors += 1
            return
        lat = data.get("lat")
        lon = data.get("lon")
        if lat is None or lon is None:
            self._parse_errors += 1
            return

        # Prefer the broker's own strike time (nanoseconds) over arrival time, so
        # a reconnect backlog is aged out correctly instead of being stamped
        # "now" and inflating the window.
        t = now
        raw_ts = data.get("time")
        if isinstance(raw_ts, (int, float)) and raw_ts > 0:
            candidate = raw_ts / 1e9
            if candidate > now + 60:
                candidate = now
            if candidate < now - self.window_sec:
                self._dropped_stale += 1
                return
            t = candidate

        try:
            strike = (t, float(lat), float(lon))
        except (TypeError, ValueError):
            self._parse_errors += 1
            return

        self._buffer.append(strike)
        if len(self._buffer) > MAX_BUFFER_STRIKES:
            del self._buffer[:len(self._buffer) - MAX_BUFFER_STRIKES]


__all__ = [
    "BlitzortungFeed",
    "MQTT_HOST", "MQTT_PORT", "RECONNECT_DELAY",
    "DEGRADED_SILENCE_SEC", "MAX_BUFFER_STRIKES",
]
