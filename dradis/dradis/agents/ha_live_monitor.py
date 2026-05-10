"""
agents/ha_live_monitor.py
─────────────────────────────────
MQTT listener for Home Assistant mqtt_statestream.
Monitors selected HA entities and sends an LLM-generated Telegram alert
when their state changes, with per-entity cooldown to avoid spam.

Behaviour
─────────
- Connects to the local Mosquitto broker (configurable host/port/credentials).
- Subscribes to statestream topics for the configured entities.
- On each incoming state message:
    • checks per-entity cooldown
    • if cooldown expired → calls llm_fn with context → sends Telegram alert
- Reconnects automatically on disconnect (15 s delay).

One HaLiveMonitor instance per enabled HA monitor entry.
All instances are owned by HaMonitorManager (singleton ha_monitor_manager).
Called by main.py on startup and on config changes.
"""

import asyncio
import html
import time
import logging
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiomqtt

_LOGGER = logging.getLogger(__name__)

RECONNECT_DELAY = 15


class HaLiveMonitor:
    """Persistent MQTT listener for one HA monitor entry."""

    def __init__(self, cfg: dict, telegram_send_fn, llm_fn, mqtt_cfg: dict, tz_name: str = "UTC"):
        self.monitor_id   = cfg["id"]
        self.name         = cfg.get("name", "HA Monitor")
        self.entities     = cfg.get("entities", [])     # list of "domain/object_id"
        self.instructions = cfg.get("instructions", "")
        self.cooldown_min = float(cfg.get("cooldown_min", 60))
        self.language     = cfg.get("language", "it")
        self.tz_name      = tz_name

        self._send    = telegram_send_fn
        self._llm     = llm_fn
        self._mqtt    = mqtt_cfg
        self._cooldowns: dict[str, float] = {}   # entity_id → last alert timestamp
        self._task: asyncio.Task | None = None

    def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self._run(), name=f"ha_monitor:{self.monitor_id}"
            )
            print(f"[HAMonitor] '{self.name}' started ({len(self.entities)} entities, cooldown={self.cooldown_min:.0f}min)")

    def stop(self):
        if self._task and not self._task.done():
            self._task.cancel()
            print(f"[HAMonitor] '{self.name}' stopped")

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _run(self):
        host     = self._mqtt.get("mqtt_host", "core-mosquitto")
        port     = int(self._mqtt.get("mqtt_port", 1883))
        username = self._mqtt.get("mqtt_username") or None
        password = self._mqtt.get("mqtt_password") or None
        prefix   = self._mqtt.get("mqtt_statestream_prefix", "homeassistant").rstrip("/")

        topics = [f"{prefix}/{e}/state" for e in self.entities]

        while True:
            try:
                print(f"[HAMonitor] '{self.name}' connecting to {host}:{port}")
                kwargs = {}
                if username:
                    kwargs["username"] = username
                if password:
                    kwargs["password"] = password
                async with aiomqtt.Client(host, port, **kwargs) as client:
                    for topic in topics:
                        await client.subscribe(topic)
                    print(f"[HAMonitor] '{self.name}' subscribed ({len(topics)} topics)")
                    async for message in client.messages:
                        await self._on_message(message, prefix)
            except asyncio.CancelledError:
                return
            except Exception as e:
                print(f"[HAMonitor] '{self.name}' disconnected: {e} — retry in {RECONNECT_DELAY}s")
                await asyncio.sleep(RECONNECT_DELAY)

    async def _on_message(self, message, prefix: str):
        topic = str(message.topic)
        # topic format: {prefix}/{domain}/{object_id}/state
        suffix = topic[len(prefix):].lstrip("/")            # "domain/object_id/state"
        if not suffix.endswith("/state"):
            return
        entity_id = suffix[: -len("/state")]                # "domain/object_id"

        if entity_id not in self.entities:
            return

        state = message.payload.decode("utf-8", errors="replace").strip()

        now = time.time()
        last = self._cooldowns.get(entity_id, 0.0)
        if (now - last) / 60.0 < self.cooldown_min:
            return

        print(f"[HAMonitor] '{self.name}' entity={entity_id} state={state!r}")
        try:
            prompt = self._build_prompt(entity_id, state)
            alert_text = await self._llm(prompt)
            if alert_text and alert_text.strip():
                self._cooldowns[entity_id] = now   # cooldown only on actual alert
                await self._send(alert_text.strip())
        except Exception as e:
            print(f"[HAMonitor] '{self.name}' LLM/send error: {e}")

    def _build_prompt(self, entity_id: str, state: str) -> str:
        try:
            tz = ZoneInfo(self.tz_name)
        except ZoneInfoNotFoundError:
            tz = ZoneInfo("UTC")
        now_str   = datetime.now(tz).strftime("%Y-%m-%d %H:%M")
        lang_hint = "Respond in Italian." if self.language == "it" else "Respond in English."
        custom    = f"\n\nCustom instructions:\n{self.instructions}" if self.instructions.strip() else ""
        return (
            f"Home Assistant entity state change — {now_str}\n"
            f"Entity: {html.escape(entity_id)}\n"
            f"New state: {html.escape(state)}\n"
            f"{lang_hint}"
            f"{custom}\n\n"
            "Decide whether this state change is worth alerting the user about. "
            "If yes, write a concise Telegram message (plain text, max 3 lines, no markdown). "
            "If no, respond with exactly: SKIP"
        )


class HaMonitorManager:
    """Owns all HA monitor instances. Called by main.py on startup and config changes."""

    def __init__(self):
        self._monitors: dict[str, HaLiveMonitor] = {}

    def reload(self, configs: list[dict], telegram_send_fn, llm_fn, mqtt_cfg: dict, tz_name: str):
        wanted: set[str] = set()
        for cfg in configs:
            if not cfg.get("enabled"):
                continue
            mid = cfg["id"]
            wanted.add(mid)
            if mid in self._monitors:
                self._monitors[mid].stop()
            m = HaLiveMonitor(cfg, telegram_send_fn, llm_fn, mqtt_cfg, tz_name)
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


ha_monitor_manager = HaMonitorManager()
