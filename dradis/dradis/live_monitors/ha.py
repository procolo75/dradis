"""
live_monitors/ha.py
────────────────────
MQTT listener for Home Assistant mqtt_statestream.
Monitors selected HA entities and sends a Telegram alert on state changes,
with per-entity cooldown.

Pipeline per state change:
  1. First message after (re)connect: record silently (MQTT retained) — no alert
  2. State unchanged → skip
  3. State filter — skip if filter_states non-empty and state not in set
  4. Cooldown check — skip if within cooldown window
  5. Alert: LLM mode (call model) or Direct mode (template message)
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
        self.monitor_id      = cfg["id"]
        self.name            = cfg.get("name", "HA Monitor")
        self.entities        = cfg.get("entities", [])
        self.instructions    = cfg.get("instructions", "")
        self.cooldown_min    = float(cfg.get("cooldown_min", 60))
        self.language        = cfg.get("language", "it")
        self.tz_name         = tz_name
        self.alert_mode      = cfg.get("alert_mode", "llm")
        self.filter_states   = {s.strip().lower() for s in cfg.get("filter_states", []) if s.strip()}
        self.direct_template = cfg.get("direct_template", "").strip()

        self._send    = telegram_send_fn
        self._llm     = llm_fn
        self._mqtt    = mqtt_cfg
        self._cooldowns: dict[str, float] = {}
        self._last_states: dict[str, str] = {}
        self._task: asyncio.Task | None = None

    def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self._run(), name=f"ha_monitor:{self.monitor_id}"
            )

    def stop(self):
        if self._task and not self._task.done():
            self._task.cancel()

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
                kwargs = {}
                if username:
                    kwargs["username"] = username
                if password:
                    kwargs["password"] = password
                async with aiomqtt.Client(host, port, **kwargs) as client:
                    for topic in topics:
                        await client.subscribe(topic)
                    async for message in client.messages:
                        await self._on_message(message, prefix)
            except asyncio.CancelledError:
                return
            except Exception as e:
                _LOGGER.warning("[HAMonitor] '%s' disconnected: %s — retry in %ds", self.name, e, RECONNECT_DELAY)
                await asyncio.sleep(RECONNECT_DELAY)

    async def _on_message(self, message, prefix: str):
        topic = str(message.topic)
        suffix = topic[len(prefix):].lstrip("/")
        if not suffix.endswith("/state"):
            return
        entity_id = suffix[: -len("/state")]

        if entity_id not in self.entities:
            return

        state = message.payload.decode("utf-8", errors="replace").strip()

        if entity_id not in self._last_states:
            self._last_states[entity_id] = state
            return

        if state == self._last_states[entity_id]:
            return
        self._last_states[entity_id] = state

        if self.filter_states and state.lower() not in self.filter_states:
            return

        now = time.time()
        last = self._cooldowns.get(entity_id, 0.0)
        elapsed_min = (now - last) / 60.0
        if elapsed_min < self.cooldown_min:
            return

        try:
            if self.alert_mode == "direct":
                msg = self._build_direct_message(entity_id, state)
                self._cooldowns[entity_id] = now
                await self._send(msg)
            else:
                prompt = self._build_prompt(entity_id, state)
                alert_text = await self._llm(prompt)
                if alert_text and alert_text.strip():
                    self._cooldowns[entity_id] = now
                    await self._send(alert_text.strip())
        except Exception as e:
            _LOGGER.warning("[HAMonitor] '%s' error: %s", self.name, e)

    def _build_direct_message(self, entity_id: str, state: str) -> str:
        try:
            tz = ZoneInfo(self.tz_name)
        except ZoneInfoNotFoundError:
            tz = ZoneInfo("UTC")
        now_str  = datetime.now(tz).strftime("%Y-%m-%d %H:%M")
        template = self.direct_template or "⚡ {entity}: {state} — {time}"
        return template.format(
            entity=html.escape(entity_id),
            state=html.escape(state),
            time=now_str,
        )

    def _build_prompt(self, entity_id: str, state: str) -> str:
        try:
            tz = ZoneInfo(self.tz_name)
        except ZoneInfoNotFoundError:
            tz = ZoneInfo("UTC")
        now_str = datetime.now(tz).strftime("%Y-%m-%d %H:%M")
        parts = [
            f"Home Assistant entity state change — {now_str}",
            f"Entity: {html.escape(entity_id)}",
            f"New state: {html.escape(state)}",
        ]
        if self.instructions.strip():
            parts.append(f"\n{self.instructions}")
        return "\n".join(parts)


class HaMonitorManager:
    """Owns all HA monitor instances. Reloaded by the bot on startup and config changes."""

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
