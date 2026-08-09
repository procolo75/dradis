"""
live_monitors/lightning.py
───────────────────────────
LLM-free live monitor: persistent MQTT listener on the Blitzortung public broker.
Sends Telegram alerts driven by the threat state machine in `lightning_core`.

This module is the I/O half — MQTT ingest, origin, persistence, formatting and
lifecycle. Every decision lives in `lightning_core`, which is pure and testable
(see tests/test_lightning_core.py) and replayable offline (see replay.py).

Pipeline
────────
  1. One persistent MQTT connection to the Blitzortung broker, subscribed to the
     geohash cells covering `radius_km` around the origin.
  2. Every strike is buffered as (t, lat, lon) — WITHOUT its distance. Distances
     are derived at evaluation time against the CURRENT origin, which is what
     makes a moving origin possible later without touching the ingest path.
  3. Every poll, lightning_core turns the buffer into three stable observables
     (d10, r_near, v_c) and the state machine maps them to 🟢 CLEAR · 🟡 WATCH ·
     🔴 WARNING, with separated enter/exit thresholds and dwell times.
  4. Alerts go straight to Telegram (no LLM). The committed level advances ONLY
     on a confirmed send, so a dropped message is retried rather than lost.

State survives a restart via /data/lightning_state.json, so a storm in progress
does not silently lose its all-clear when the add-on restarts.

One LightningLiveMonitor instance per enabled live monitor entry of type
'lightning'. All instances are owned by LiveMonitorManager (singleton
live_monitor_manager). Called by main.py on startup and on config changes —
NOT via the APScheduler cron.
"""

import asyncio
import html
import json
import logging
import os
import time
from datetime import datetime, time as time_t
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiomqtt

from .lightning_core import (
    LEVEL_CLEAR, LEVEL_WATCH, LEVEL_WARNING, LEVEL_NAMES,
    ObservableTracker, ThreatStateMachine, Observables,
    get_preset, direction_label, topics_for_area,
    WINDOW_MIN, POLL_INTERVAL_STATIC, POLL_INTERVAL_MOVING,
)

_LOGGER = logging.getLogger(__name__)

MQTT_HOST       = "blitzortung.ha.sed.pl"
MQTT_PORT       = 1883
RECONNECT_DELAY = 15

STATE_PATH  = "/data/lightning_state.json"
RECORD_DIR  = "/data/lightning_rec"
RECORD_RETENTION_DAYS = 7

# A restored state older than this is discarded — the weather has moved on.
STATE_MAX_AGE_SEC = 3600
# Connected but silent for this long → the monitor reports "degraded" instead of
# pretending everything is fine. A broken monitor and a clear sky used to look
# identical from the outside.
DEGRADED_SILENCE_SEC = 900
# Hard cap so a burst between polls cannot grow the buffer without bound.
MAX_BUFFER_STRIKES = 20000


# ── Persistent state ──────────────────────────────────────────────────────────

def _load_state_file() -> dict:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    except OSError as e:
        _LOGGER.warning("[Lightning] cannot read %s: %s", STATE_PATH, e)
        return {}


def _save_state_entry(monitor_id: str, entry: dict) -> None:
    """Read-modify-write of the shared state file. Safe without a lock: the whole
    operation is synchronous, so it cannot interleave on the event loop."""
    data = _load_state_file()
    data[monitor_id] = entry
    tmp = f"{STATE_PATH}.tmp"
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, STATE_PATH)
    except OSError as e:
        _LOGGER.warning("[Lightning] cannot write %s: %s", STATE_PATH, e)


# ── Origin ────────────────────────────────────────────────────────────────────

class Origin:
    """Where distances are measured from.

    Deliberately an abstraction from day one: the monitor never assumes the point
    is fixed. A future TrackedOrigin backed by a Home Assistant device_tracker
    only has to implement this interface — the ingest path, the observables and
    the state machine all work against `position()` evaluated at poll time.
    """

    def position(self) -> tuple[float, float] | None:
        raise NotImplementedError

    def velocity(self) -> tuple[float, float] | None:
        """(speed_kmh, bearing_deg) of the origin itself, or None if not moving."""
        return None

    def is_moving(self) -> bool:
        return False

    def coverage_center(self) -> tuple[float, float] | None:
        """Point the MQTT topic set is derived from."""
        return self.position()

    def poll_interval(self) -> float:
        return POLL_INTERVAL_MOVING if self.is_moving() else POLL_INTERVAL_STATIC

    def describe(self) -> str:
        pos = self.position()
        return "unknown" if pos is None else f"{pos[0]:.4f},{pos[1]:.4f}"


class StaticOrigin(Origin):
    """A fixed point, from the monitor's latitude/longitude."""

    def __init__(self, lat: float, lon: float):
        self.lat = lat
        self.lon = lon

    def position(self) -> tuple[float, float]:
        return self.lat, self.lon


# ── Monitor ───────────────────────────────────────────────────────────────────

class LightningLiveMonitor:
    """Persistent MQTT listener for one live monitor entry of type 'lightning'."""

    def __init__(self, cfg: dict, telegram_send_fn, tz_name: str = "UTC"):
        self.monitor_id = cfg["id"]
        self.name       = cfg.get("name", "Lightning")
        self.location   = cfg.get("location", "")
        self.radius_km  = float(cfg.get("radius_km", 100))
        self.language   = cfg.get("language", "it")
        self.tz_name    = tz_name
        self._send      = telegram_send_fn

        self.origin  = StaticOrigin(float(cfg.get("latitude", 0)),
                                    float(cfg.get("longitude", 0)))
        self.preset  = get_preset(cfg.get("sensitivity", ""))
        self._quiet_start = (cfg.get("quiet_start") or "").strip()
        self._quiet_end   = (cfg.get("quiet_end") or "").strip()
        self._recording   = bool(cfg.get("record_strikes", False))

        # Perception + decision
        self._buffer: list = []            # [(t, lat, lon), ...] — no distance
        self._tracker = ObservableTracker()
        self._machine = ThreatStateMachine(self.preset)

        # Diagnostics
        self._messages     = 0
        self._parse_errors = 0
        self._dropped_stale = 0
        self._last_msg_ts  = 0.0
        self._connected    = False
        self._connect_failures = 0

        # Recording buffer, flushed on the poll task so ingest never blocks
        self._pending_record: list = []

        self._subscribed: set[str] = set()
        self._client = None
        self._sub_lock = asyncio.Lock()
        self._task: asyncio.Task | None      = None
        self._poll_task: asyncio.Task | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        running = [t for t in (self._task, self._poll_task) if t and not t.done()]
        if running:
            return
        self._restore_state()
        if self._recording:
            self._prune_recordings()
        self._task = asyncio.create_task(
            self._run(), name=f"live_lightning:{self.monitor_id}"
        )
        self._poll_task = asyncio.create_task(
            self._poll_loop(), name=f"lightning_poll:{self.monitor_id}"
        )
        print(f"[LiveMonitor] '{self.name}' started "
              f"(radius={self.radius_km:.0f}km, sensitivity={self.preset.name}, "
              f"level={LEVEL_NAMES[self._machine.level]})")

    def stop(self) -> None:
        for task in (self._poll_task, self._task):
            if task and not task.done():
                task.cancel()
        print(f"[LiveMonitor] '{self.name}' stopped")

    async def aclose(self) -> None:
        """Cancel and wait. Used on restart so the old MQTT client is fully
        disconnected before its replacement connects."""
        tasks = [t for t in (self._poll_task, self._task) if t and not t.done()]
        self.stop()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

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

    # ── State persistence ─────────────────────────────────────────────────────

    def _restore_state(self) -> None:
        entry = _load_state_file().get(self.monitor_id)
        if not entry:
            return
        updated = float(entry.get("updated_at", 0))
        if time.time() - updated > STATE_MAX_AGE_SEC:
            _LOGGER.info("[Lightning] '%s' saved state too old — starting clean", self.name)
            return
        self._machine = ThreatStateMachine.from_dict(self.preset, entry.get("machine"))
        self._tracker = ObservableTracker.from_dict(entry.get("tracker"))

    def _save_state(self) -> None:
        _save_state_entry(self.monitor_id, {
            "updated_at": time.time(),
            "machine": self._machine.to_dict(),
            "tracker": self._tracker.to_dict(),
        })

    # ── MQTT ingest ───────────────────────────────────────────────────────────

    async def _run(self) -> None:
        while True:
            try:
                print(f"[LiveMonitor] '{self.name}' connecting to {MQTT_HOST}:{MQTT_PORT}")
                async with aiomqtt.Client(MQTT_HOST, MQTT_PORT) as client:
                    self._client = client
                    self._subscribed = set()
                    await self._sync_subscriptions()
                    self._connected = True
                    self._connect_failures = 0
                    async for message in client.messages:
                        self._on_message(message)
            except asyncio.CancelledError:
                return
            except Exception as e:
                self._connected = False
                self._connect_failures += 1
                self._client = None
                print(f"[LiveMonitor] '{self.name}' disconnected: {e} "
                      f"— retry in {RECONNECT_DELAY}s (failures={self._connect_failures})")
                await asyncio.sleep(RECONNECT_DELAY)

    async def _sync_subscriptions(self) -> None:
        """Bring the subscribed topic set in line with the current origin.

        For a static origin this runs once and never changes anything. It exists
        so a moving origin can cross geohash cells without dropping the
        connection.
        """
        client = self._client
        center = self.origin.coverage_center()
        if client is None or center is None:
            return
        wanted = set(topics_for_area(center[0], center[1], self.radius_km))
        if wanted == self._subscribed:
            return
        async with self._sub_lock:
            for topic in sorted(wanted - self._subscribed):
                await client.subscribe(topic)
            for topic in sorted(self._subscribed - wanted):
                await client.unsubscribe(topic)
            added, removed = len(wanted - self._subscribed), len(self._subscribed - wanted)
            self._subscribed = wanted
        _LOGGER.info("[Lightning] '%s' topics synced (+%d/-%d, total %d)",
                     self.name, added, removed, len(wanted))

    def _on_message(self, message) -> None:
        """Ingest is deliberately geometry-free: the subscribed geohash box is the
        only filter here. The radius is applied at evaluation time, because with a
        moving origin a strike outside the radius now may well be inside it in
        five minutes."""
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
            if candidate < now - WINDOW_MIN * 60:
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
        if self._recording:
            self._pending_record.append(strike)

    # ── Recording (for offline replay and threshold tuning) ───────────────────

    def _record_path(self) -> str:
        day = datetime.now(self._tz()).strftime("%Y-%m-%d")
        return os.path.join(RECORD_DIR, f"{self.monitor_id}-{day}.ndjson")

    def _flush_recorder(self) -> None:
        if not self._recording or not self._pending_record:
            return
        pending, self._pending_record = self._pending_record, []
        try:
            os.makedirs(RECORD_DIR, exist_ok=True)
            with open(self._record_path(), "a", encoding="utf-8") as fh:
                for t, la, lo in pending:
                    fh.write(json.dumps({"t": round(t, 3), "lat": la, "lon": lo}) + "\n")
        except OSError as e:
            _LOGGER.warning("[Lightning] '%s' cannot write recording: %s", self.name, e)

    def _prune_recordings(self) -> None:
        cutoff = time.time() - RECORD_RETENTION_DAYS * 86400
        try:
            for fname in os.listdir(RECORD_DIR):
                if not fname.startswith(f"{self.monitor_id}-"):
                    continue
                path = os.path.join(RECORD_DIR, fname)
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
        except OSError:
            pass

    # ── Poll loop ─────────────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        # Evaluate immediately so the monitor reports its state right away instead
        # of staying blind for the first poll interval. No alerts on this pass:
        # any decision it produces is simply re-offered on the next poll.
        try:
            await self._tick(notify=False)
        except asyncio.CancelledError:
            return
        except Exception as e:
            _LOGGER.error("[Lightning] '%s' first tick failed: %s", self.name, e)

        while True:
            try:
                await asyncio.sleep(self.origin.poll_interval())
                await self._tick(notify=True)
            except asyncio.CancelledError:
                return
            except Exception as e:
                _LOGGER.error("[Lightning] '%s' poll error: %s", self.name, e)

    async def _tick(self, notify: bool) -> None:
        now = time.time()
        position = self.origin.position()
        if position is None:
            _LOGGER.warning("[Lightning] '%s' no origin fix — skipping poll", self.name)
            return

        await self._sync_subscriptions()
        self._buffer = [s for s in self._buffer if s[0] >= now - WINDOW_MIN * 60]
        self._flush_recorder()

        obs = self._tracker.observe(self._buffer, position, now, self.radius_km)
        decision = self._machine.evaluate(obs, now)
        self._log_observables(obs)

        if decision is not None and notify:
            await self._dispatch(decision, obs, now)

    def _log_observables(self, obs: Observables) -> None:
        def num(v, fmt="{:.1f}"):
            return "—" if v is None else fmt.format(v)
        _LOGGER.info(
            "[Lightning] %s | d10=%s(s%s) Rnear=%.2f/min vc=%s eta=%s "
            "lvl=%s pend=%s strikes=%d msgs=%d",
            self.name, num(obs.d10), num(obs.d10_s), obs.r_near,
            num(obs.v_c_s, "{:+.1f}"), num(obs.eta_min, "{:.0f}"),
            LEVEL_NAMES[self._machine.level], self._machine.pending_label,
            obs.strikes_total, self._messages,
        )

    # ── Alert dispatch ────────────────────────────────────────────────────────

    async def _dispatch(self, decision, obs: Observables, now: float) -> None:
        # WARNING is never silenced — quiet hours suppress only 🟡 and ✅.
        if decision.level != LEVEL_WARNING and self._in_quiet_hours():
            _LOGGER.info("[Lightning] '%s' %s suppressed by quiet hours",
                         self.name, LEVEL_NAMES[decision.level])
            # Commit anyway: silencing delivery must not desync the level, or the
            # monitor would keep retrying this alert until quiet hours end.
            self._machine.commit(decision, now)
            self._save_state()
            return

        text = self._format(decision, obs)
        # send_telegram swallows its own exceptions and returns False on failure,
        # so the state machine advances ONLY on confirmed delivery — a dropped
        # alert is retried on the next poll instead of leaving the user out of sync.
        try:
            ok = await self._send(text)
        except Exception as e:
            _LOGGER.error("[Lightning] '%s' send error: %s", self.name, e)
            return
        if not ok:
            _LOGGER.warning("[Lightning] '%s' alert NOT delivered — state held, "
                            "retry next poll", self.name)
            return
        self._machine.commit(decision, now)
        self._save_state()
        _LOGGER.info("[Lightning] %s | committed level=%s periodic=%s",
                     self.name, LEVEL_NAMES[decision.level], decision.periodic)

    def _in_quiet_hours(self) -> bool:
        if not self._quiet_start or not self._quiet_end:
            return False
        try:
            sh, sm = map(int, self._quiet_start.split(":"))
            eh, em = map(int, self._quiet_end.split(":"))
            s, e = time_t(sh, sm), time_t(eh, em)
            t = datetime.now(self._tz()).time().replace(second=0, microsecond=0)
            if s <= e:
                return s <= t < e
            return t >= s or t < e
        except (ValueError, AttributeError):
            return False

    # ── Formatters ────────────────────────────────────────────────────────────

    def _format(self, decision, obs: Observables) -> str:
        if decision.level == LEVEL_CLEAR:
            return self._fmt_clear(obs)
        if decision.level == LEVEL_WATCH:
            return self._fmt_watch(obs)
        return self._fmt_warning(obs)

    def _bearing_text(self, obs: Observables) -> str:
        if obs.bearing is None:
            return ""
        return f" a {direction_label(obs.bearing, self.language)} ({obs.bearing:.0f}°)" \
            if self.language == "it" else \
            f" to {direction_label(obs.bearing, self.language)} ({obs.bearing:.0f}°)"

    def _trend_phrase(self, obs: Observables) -> str:
        v = obs.v_c_s
        if v is None:
            return "In osservazione" if self.language == "it" else "Watching"
        if v >= 5:
            return "In avvicinamento" if self.language == "it" else "Approaching"
        if v <= -5:
            return "In allontanamento" if self.language == "it" else "Moving away"
        return "Stazionario" if self.language == "it" else "Stationary"

    def _loc(self) -> str:
        return html.escape(self.location or self.name)

    def _warning_lead(self, obs: Observables) -> str:
        """A WARNING is held through the exit dwell, so it can still be active
        while the storm is already pulling away — say so rather than claiming it
        is overhead."""
        it = self.language == "it"
        v = obs.v_c_s
        if v is not None and v >= 5:
            return "In avvicinamento" if it else "Approaching"
        if v is not None and v <= -5:
            return "In allontanamento" if it else "Moving away"
        return "Nelle immediate vicinanze" if it else "In the immediate area"

    def _fmt_warning(self, obs: Observables) -> str:
        dist = obs.d10_s or 0.0
        approaching = obs.v_c_s is not None and obs.v_c_s >= 5
        it = self.language == "it"
        lines = [f"🔴 <b>ALLERTA temporale — {self._loc()}</b>" if it
                 else f"🔴 <b>Storm WARNING — {self._loc()}</b>"]
        lines.append(f"📍 {self._warning_lead(obs)}: "
                     f"<b>{dist:.1f} km</b>{self._bearing_text(obs)}")
        if obs.speed_kmh and approaching and obs.eta_min:
            lines.append(f"🚀 ~{obs.speed_kmh:.0f} km/h — "
                         + (f"arrivo stimato: {obs.eta_min:.0f} min" if it
                            else f"estimated arrival: {obs.eta_min:.0f} min"))
        elif obs.speed_kmh:
            lines.append(f"🚀 ~{obs.speed_kmh:.0f} km/h")
        lines.append((f"🔢 Fulmini ultimi {WINDOW_MIN} min: {obs.strikes_total}" if it
                      else f"🔢 Strikes (last {WINDOW_MIN} min): {obs.strikes_total}"))
        lines.append(f"🕐 {self._now_str()}")
        return "\n".join(lines)

    def _fmt_watch(self, obs: Observables) -> str:
        dist = obs.d10_s or 0.0
        it = self.language == "it"
        head = (f"🟡 <b>Temporale in zona — {self._loc()}</b>" if it
                else f"🟡 <b>Storm in the area — {self._loc()}</b>")
        activity = (f"📍 Attività a <b>{dist:.1f} km</b>{self._bearing_text(obs)}" if it
                    else f"📍 Activity at <b>{dist:.1f} km</b>{self._bearing_text(obs)}")
        strikes = (f"🔢 Fulmini ultimi {WINDOW_MIN} min: {obs.strikes_total}" if it
                   else f"🔢 Strikes (last {WINDOW_MIN} min): {obs.strikes_total}")
        return "\n".join([head, activity, f"📊 {self._trend_phrase(obs)}",
                          strikes, f"🕐 {self._now_str()}"])

    def _fmt_clear(self, obs: Observables) -> str:
        it = self.language == "it"
        head = (f"✅ <b>Cessato allarme temporale — {self._loc()}</b>" if it
                else f"✅ <b>Storm threat cleared — {self._loc()}</b>")
        # CLEAR is now reachable two ways — the storm left, or it died out — and
        # the message says which, instead of always claiming silence.
        if obs.has_data and obs.d10_s is not None:
            reason = (f"🔇 Attività residua a {obs.d10_s:.0f} km, in allontanamento" if it
                      else f"🔇 Remaining activity {obs.d10_s:.0f} km away, moving off")
        else:
            reason = ("🔇 Nessuna attività significativa" if it
                      else "🔇 No significant activity")
        return "\n".join([head, reason, f"🕐 {self._now_str()}"])

    def _tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.tz_name)
        except (ZoneInfoNotFoundError, ValueError):
            return ZoneInfo("UTC")

    def _now_str(self) -> str:
        return datetime.now(self._tz()).strftime("%H:%M")


# ── Manager ───────────────────────────────────────────────────────────────────

# Only these fields affect how the monitor behaves. Anything else changing in
# live_monitors.json must NOT restart a running monitor — that used to wipe the
# buffer, the level and the in-flight storm every time an unrelated monitor
# (football, seismic) was saved.
_FINGERPRINT_FIELDS = (
    "name", "location", "latitude", "longitude", "radius_km", "language",
    "sensitivity", "quiet_start", "quiet_end", "record_strikes", "telegram_bot_id",
)


def _fingerprint(cfg: dict, tz_name: str) -> str:
    return json.dumps([cfg.get(k) for k in _FINGERPRINT_FIELDS] + [tz_name],
                      sort_keys=True, default=str)


class LiveMonitorManager:
    """Owns all lightning monitor instances. Called by main.py on startup and on
    config changes."""

    def __init__(self):
        self._monitors: dict[str, LightningLiveMonitor] = {}
        self._fingerprints: dict[str, str] = {}

    def reload(self, configs: list[dict], make_send_fn, tz_name: str):
        wanted: set[str] = set()
        for cfg in configs:
            if cfg.get("type") != "lightning" or not cfg.get("enabled"):
                continue
            mid = cfg["id"]
            wanted.add(mid)
            fingerprint = _fingerprint(cfg, tz_name)
            existing = self._monitors.get(mid)
            if existing and self._fingerprints.get(mid) == fingerprint and existing.is_running():
                continue                      # unchanged — leave the storm alone
            self._fingerprints[mid] = fingerprint
            replacement = LightningLiveMonitor(cfg, make_send_fn(cfg), tz_name)
            self._monitors[mid] = replacement
            self._swap(existing, replacement)

        for mid in list(self._monitors):
            if mid not in wanted:
                self._swap(self._monitors.pop(mid), None)
                self._fingerprints.pop(mid, None)

    @staticmethod
    def _swap(old: LightningLiveMonitor | None, new: LightningLiveMonitor | None):
        """Retire `old` and start `new`, waiting for the old MQTT client to close
        first when an event loop is available."""
        if old is None:
            if new is not None:
                new.start()
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            old.stop()
            if new is not None:
                new.start()
            return

        async def _handover():
            await old.aclose()
            if new is not None:
                new.start()

        loop.create_task(_handover())

    def stop_all(self):
        for m in self._monitors.values():
            m.stop()
        self._monitors.clear()
        self._fingerprints.clear()

    def status(self, monitor_id: str) -> str:
        m = self._monitors.get(monitor_id)
        return m.status() if m else "stopped"


live_monitor_manager = LiveMonitorManager()
