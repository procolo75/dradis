"""
live_monitors/position.py
──────────────────────────
Ingest half of the dynamic position source: ONE persistent MQTT listener on the
Home Assistant broker, reading the coordinates of any number of named positions
off mqtt_statestream. The logic lives in `position_core`, which is pure and
unit-testable.

Why MQTT and not the HA REST API
────────────────────────────────
DRADIS already speaks to Home Assistant over MQTT and nothing else (see `ha.py`),
and the add-on holds no Supervisor token. Reading the position the same way keeps
the add-on's surface exactly where it is: no new permission, and the user decides
what is exposed by choosing what to include in mqtt_statestream.

What Home Assistant must publish
────────────────────────────────
A device_tracker keeps its coordinates in ATTRIBUTES, and statestream publishes
states. The supported shape is therefore two template sensors whose state IS the
coordinate, included in mqtt_statestream:

    homeassistant/sensor/phone_latitude/state    → "40.82731"
    homeassistant/sensor/phone_longitude/state   → "14.13902"
    homeassistant/sensor/phone_gps_accuracy/state → "12"       (optional)

With `publish_timestamps: true` statestream also publishes `.../last_updated`,
which this feed uses to date the RETAINED message it receives on connect. Without
it a position from yesterday would arrive looking brand new — the one failure
mode that would make a stale fix indistinguishable from a fresh one.

Why positions are named, and why there is one connection
────────────────────────────────────────────────────────
Two phones, or another family member's phone, is the ordinary case rather than
the exception, so a position is a named thing a monitor selects — the same shape
as the Telegram bots. They all live on the same broker and amount to a handful of
topics, so they share a single connection with a topic → (position, component)
routing table, instead of one client each.
"""

import asyncio
import logging
import time
from datetime import datetime

import aiomqtt

from .position_core import FixHistory, PositionState

_LOGGER = logging.getLogger(__name__)

RECONNECT_DELAY = 15
# Connected but nothing received for this long → "degraded" rather than pretending
# the silence is meaningful. Mirrors BlitzortungFeed's own silence budget.
DEGRADED_SILENCE_SEC = 1800

DEFAULT_MAX_AGE_MIN = 15.0
DEFAULT_MAX_ACCURACY_M = 500.0


def _parse_timestamp(raw: str) -> float | None:
    """Parse statestream's `last_updated` (ISO 8601) into an epoch.

    Returns None on anything unexpected: the caller then falls back to arrival
    time, which is right for a live message and merely optimistic for a retained
    one — never wrong in a way that invents freshness out of nothing.
    """
    try:
        return datetime.fromisoformat(raw.strip().replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


# ── One named position ────────────────────────────────────────────────────────

class PositionSource:
    """A named position: which entities carry it, how fresh it must be, and what
    has been heard so far. No I/O — the manager owns the socket and hands
    payloads over."""

    def __init__(self, cfg: dict, default_prefix: str):
        self.id   = str(cfg.get("id") or "")
        self.name = (cfg.get("name") or "").strip() or "Position"
        self.lat_entity = (cfg.get("lat_entity") or "").strip()
        self.lon_entity = (cfg.get("lon_entity") or "").strip()
        self.acc_entity = (cfg.get("accuracy_entity") or "").strip()
        self.prefix = ((cfg.get("mqtt_prefix") or "").strip()
                       or default_prefix).rstrip("/")
        self.max_age_sec = float(
            cfg.get("max_age_min") or DEFAULT_MAX_AGE_MIN) * 60.0
        self.max_accuracy_m = float(
            cfg.get("max_accuracy_m") or DEFAULT_MAX_ACCURACY_M)

        self.history = FixHistory()
        self._stamps: dict[str, float] = {}
        self.messages = 0

    # ── Identity ──────────────────────────────────────────────────────────────

    def subscription_key(self) -> tuple:
        """What must be identical for a reload to keep this source's fix history.

        Deliberately excludes the name and the thresholds: renaming a phone or
        widening its age budget must not blind the monitor for the next few
        minutes while a new history rebuilds.
        """
        return (self.id, self.prefix, self.lat_entity, self.lon_entity,
                self.acc_entity)

    def is_complete(self) -> bool:
        return bool(self.lat_entity and self.lon_entity)

    def adopt_thresholds(self, other: "PositionSource") -> None:
        self.name = other.name
        self.max_age_sec = other.max_age_sec
        self.max_accuracy_m = other.max_accuracy_m

    # ── Topics ────────────────────────────────────────────────────────────────

    def topics(self) -> dict[str, str]:
        """Topic → component name."""
        mapping: dict[str, str] = {}
        for component, entity in (("lat", self.lat_entity),
                                  ("lon", self.lon_entity),
                                  ("acc", self.acc_entity)):
            if not entity:
                continue
            mapping[f"{self.prefix}/{entity}/state"] = component
            mapping[f"{self.prefix}/{entity}/last_updated"] = f"{component}_ts"
        return mapping

    # ── Ingest ────────────────────────────────────────────────────────────────

    def ingest(self, component: str, payload: str, now: float) -> None:
        self.messages += 1

        if component.endswith("_ts"):
            stamp = _parse_timestamp(payload)
            if stamp is not None:
                self._stamps[component[:-3]] = stamp
            return

        if payload.lower() in ("", "unknown", "unavailable", "none"):
            return
        try:
            value = float(payload)
        except ValueError:
            return

        # Prefer the timestamp HA published over arrival time. It is the only
        # thing that makes a retained message datable: without it, a coordinate
        # from hours ago would be indistinguishable from one published now.
        t = self._stamps.pop(component, now)
        if t > now + 60:
            t = now

        if component == "lat":
            self.history.set_latitude(value, t)
        elif component == "lon":
            self.history.set_longitude(value, t)
        elif component == "acc":
            self.history.set_accuracy(value)

    # ── Read ──────────────────────────────────────────────────────────────────

    def current(self, now: float | None = None) -> PositionState | None:
        """The latest fix, whatever its age. Freshness is the caller's call."""
        return self.history.current(time.time() if now is None else now)

    def usable(self, now: float | None = None,
               max_age_sec: float | None = None) -> PositionState | None:
        """The latest fix if it passes this position's own age and accuracy
        budgets, else None. `max_age_sec` lets a caller be more patient — the
        storm front is, while a storm is in progress."""
        state = self.current(now)
        if state is None:
            return None
        budget = self.max_age_sec if max_age_sec is None else max_age_sec
        if state.age_sec > budget:
            return None
        if (state.accuracy_m is not None
                and state.accuracy_m > self.max_accuracy_m):
            return None
        return state


# ── The manager ───────────────────────────────────────────────────────────────

class PositionManager:
    """One MQTT connection serving every configured position."""

    def __init__(self):
        self._sources: dict[str, PositionSource] = {}
        self._routes: dict[str, tuple[str, str]] = {}   # topic → (id, component)
        self._broker: dict = {}
        self._task: asyncio.Task | None = None

        self._connected = False
        self._connect_failures = 0
        self._last_msg_ts = 0.0

    # ── Configuration ─────────────────────────────────────────────────────────

    @staticmethod
    def broker_cfg(settings: dict) -> dict:
        return {
            "host":     settings.get("mqtt_host", "core-mosquitto"),
            "port":     int(settings.get("mqtt_port", 1883) or 1883),
            "username": settings.get("mqtt_username") or None,
            "password": settings.get("mqtt_password") or None,
            "prefix":   settings.get("mqtt_statestream_prefix",
                                     "homeassistant").rstrip("/"),
        }

    def configure(self, settings: dict, positions: list[dict]) -> None:
        """(Re)build from the saved positions.

        Sources whose subscription did not change keep their existing fix history,
        so adding a second phone — or renaming the first — does not blind a
        running monitor while a new history rebuilds.
        """
        broker = self.broker_cfg(settings)
        rebuilt: dict[str, PositionSource] = {}
        for cfg in positions:
            source = PositionSource(cfg, broker["prefix"])
            if not source.id or not source.is_complete():
                continue
            existing = self._sources.get(source.id)
            if existing and existing.subscription_key() == source.subscription_key():
                existing.adopt_thresholds(source)
                rebuilt[source.id] = existing
            else:
                rebuilt[source.id] = source

        routes: dict[str, tuple[str, str]] = {}
        for source in rebuilt.values():
            for topic, component in source.topics().items():
                routes[topic] = (source.id, component)

        changed = (routes != self._routes or broker != self._broker)
        self._sources, self._routes, self._broker = rebuilt, routes, broker

        if not changed:
            return
        self.stop()
        if routes:
            self.start()
        else:
            _LOGGER.info("[Position] no position configured — listener not started")

    # ── Read ──────────────────────────────────────────────────────────────────

    def get(self, position_id: str) -> PositionSource | None:
        return self._sources.get(position_id or "")

    def name_of(self, position_id: str) -> str | None:
        source = self.get(position_id)
        return source.name if source else None

    def current(self, position_id: str, now: float | None = None):
        source = self.get(position_id)
        return source.current(now) if source else None

    def usable(self, position_id: str, now: float | None = None,
               max_age_sec: float | None = None):
        source = self.get(position_id)
        return source.usable(now, max_age_sec) if source else None

    def max_age_sec(self, position_id: str) -> float:
        source = self.get(position_id)
        return source.max_age_sec if source else DEFAULT_MAX_AGE_MIN * 60.0

    def ids(self) -> list[str]:
        return list(self._sources)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Configured before the loop exists. Harmless: this is a singleton
            # reconfigured from several places, and the next call that runs on the
            # loop starts it. Crashing a settings save over it would not be.
            # The check comes before building the coroutine, so nothing is left
            # un-awaited behind us.
            _LOGGER.warning("[Position] no running event loop — listener deferred")
            return
        self._task = loop.create_task(self._run(), name="position_manager")

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        self._connected = False

    async def aclose(self) -> None:
        task = self._task
        self.stop()
        if task:
            await asyncio.gather(task, return_exceptions=True)

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def status(self) -> str:
        if not self._routes:
            return "disabled"
        if not self.is_running():
            return "stopped"
        if self._connect_failures >= 3 or not self._connected:
            return "degraded"
        if self._last_msg_ts and time.time() - self._last_msg_ts > DEGRADED_SILENCE_SEC:
            return "degraded"
        return "running"

    def stats(self) -> dict:
        return {
            "connected": self._connected,
            "positions": len(self._sources),
            "topics": len(self._routes),
            "messages": sum(s.messages for s in self._sources.values()),
        }

    # ── MQTT ──────────────────────────────────────────────────────────────────

    async def _run(self) -> None:
        broker, routes = self._broker, self._routes
        kwargs = {}
        if broker["username"]:
            kwargs["username"] = broker["username"]
        if broker["password"]:
            kwargs["password"] = broker["password"]

        while True:
            try:
                print(f"[Position] connecting to {broker['host']}:{broker['port']} "
                      f"({len(routes)} topics, {len(self._sources)} positions)")
                async with aiomqtt.Client(broker["host"], broker["port"],
                                          **kwargs) as client:
                    for topic in routes:
                        await client.subscribe(topic)
                    self._connected = True
                    self._connect_failures = 0
                    async for message in client.messages:
                        self._on_message(message)
            except asyncio.CancelledError:
                self._connected = False
                return
            except Exception as e:
                self._connected = False
                self._connect_failures += 1
                print(f"[Position] disconnected: {e} — retry in {RECONNECT_DELAY}s "
                      f"(failures={self._connect_failures})")
                await asyncio.sleep(RECONNECT_DELAY)

    def _on_message(self, message) -> None:
        route = self._routes.get(str(message.topic))
        if route is None:
            return
        source = self._sources.get(route[0])
        if source is None:
            return
        self._last_msg_ts = time.time()
        source.ingest(route[1],
                      message.payload.decode("utf-8", errors="replace").strip(),
                      self._last_msg_ts)


def probe(settings: dict, position: dict) -> PositionManager:
    """A throwaway manager holding one position, for the Test button.

    Testing a form you have not saved yet is the normal case, so the test must
    answer "would THESE values work" using what is on screen. A separate instance
    is what makes that safe: the running manager keeps its connection and every
    position's fix history untouched, whatever the user is typing.
    """
    manager = PositionManager()
    broker = PositionManager.broker_cfg(settings)
    source = PositionSource({**position, "id": position.get("id") or "probe"},
                            broker["prefix"])
    manager._broker = broker
    manager._sources = {source.id: source}
    manager._routes = {topic: (source.id, component)
                       for topic, component in source.topics().items()}
    return manager


position_manager = PositionManager()


__all__ = ["PositionSource", "PositionManager", "position_manager", "probe",
           "RECONNECT_DELAY", "DEGRADED_SILENCE_SEC",
           "DEFAULT_MAX_AGE_MIN", "DEFAULT_MAX_ACCURACY_M"]
