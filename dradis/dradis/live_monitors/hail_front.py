"""
live_monitors/hail_front.py
────────────────────────────
LLM-free live monitor: watches the national radar's probability-of-hail product
(POH) and says so when hail becomes likely near the observer.

Why this is not a mode of the rain front
────────────────────────────────────────
The rain front answers "where is the rain going and will we meet", so it needs a
front, a direction, a ring ladder and a drift estimate — and on scattered summer
convection the drift is exactly what it cannot measure. Hail is a different
question: POH is already a probability map, so "how likely is hail within R km of
me, and where is the most likely spot" is read straight off one raster. No motion,
no heading, no rings. Hail also does not need rain to clear a mm/h floor first;
the old hail line on the rain front only existed when that floor had been cleared.

What it decides
───────────────
Two numbers per poll: `peak`, the highest POH inside the radius, and `near`, the
highest inside `NEAR_RADIUS_KM`. Two tiers follow:

  WATCH    peak >= watch_percent
  WARNING  peak >= severe_percent, or near >= watch_percent

An event sends at most one message per tier — so at most two — plus one all-clear,
for any input. A tier only ever escalates within an event; a cell that flickers
around a threshold cannot repeat a message. The all-clear needs
`CLEAR_GRIDS` consecutive NEW rasters below the watch threshold, counted by raster
timestamp rather than by poll, because the radar publishes every five minutes and a
60-second poll would otherwise "confirm" the same picture five times.

Blindness is not calm. A missing, stale or out-of-coverage raster freezes the
tracker: no alert, and above all no all-clear.
"""

import asyncio
import html
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, time as time_t
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .geo import azimuth_deg, direction_label, distance_km
from .position import ORIGIN_FALLBACK, position_manager
from .radar import PRODUCT_HAIL, radar_feed
from .radar_core import coverage_fraction, peak_with_location
from .snapshot import origin_line
from .storm_front_core import POLL_INTERVAL_SEC, clamp_radius

_LOGGER = logging.getLogger(__name__)

STATE_PATH = "/data/hail_front_state.json"
STATE_MAX_AGE_SEC = 3600

DEFAULT_WATCH_PERCENT = 40.0
DEFAULT_SEVERE_PERCENT = 70.0
PERCENT_FLOOR = 5.0
PERCENT_CEILING = 100.0

# A cell this close is not "in the area", it is where the observer is standing.
NEAR_RADIUS_KM = 10.0

# Consecutive NEW rasters below the watch threshold before the all-clear. Three
# five-minute products is a quarter of an hour: hail cells pulse, and an
# all-clear that arrives between two pulses is worse than none.
CLEAR_GRIDS = 3

# Below this share of the disc actually seen by the radar network the monitor is
# not watching what it claims to watch.
MIN_COVERAGE_FRACTION = 0.4

# Position-following monitors hold an old fix longer while an event is open.
EVENT_STALE_FACTOR = 3.0

TIER_NONE = 0
TIER_WATCH = 1
TIER_WARNING = 2


# ── Decision core ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class HailAlert:
    tier: int
    peak_percent: float
    near_percent: float | None
    distance_km: float
    bearing_deg: float
    escalation: bool                # True when this follows a lower-tier message


@dataclass(frozen=True)
class HailClear:
    peak_percent: float             # highest value seen during the event
    quiet_grids: int


class HailTracker:
    """Tier ladder with escalate-only semantics. Pure: no clock, no I/O."""

    def __init__(self, watch_percent: float = DEFAULT_WATCH_PERCENT,
                 severe_percent: float = DEFAULT_SEVERE_PERCENT):
        self.watch_percent = watch_percent
        self.severe_percent = max(severe_percent, watch_percent)
        self.notified_tier = TIER_NONE
        self.event_peak = 0.0
        self._quiet_grids = 0
        self._last_grid_t: float | None = None

    @property
    def event_open(self) -> bool:
        return self.notified_tier != TIER_NONE

    def tier_of(self, peak: float | None, near: float | None) -> int:
        if peak is None:
            return TIER_NONE
        if peak >= self.severe_percent or (near is not None
                                           and near >= self.watch_percent):
            return TIER_WARNING
        if peak >= self.watch_percent:
            return TIER_WATCH
        return TIER_NONE

    def evaluate(self, peak: float | None, near: float | None,
                 where: tuple[float, float] | None, grid_t: float,
                 *, feed_ok: bool = True) -> "HailAlert | HailClear | None":
        """The verdict for this poll. Never mutates the notified tier — only
        `commit` does, once the message has actually been sent."""
        if not feed_ok:
            return None
        tier = self.tier_of(peak, near)

        if tier == TIER_NONE:
            # An unmeasured disc is not a calm one: only a real reading below the
            # threshold counts towards the all-clear.
            if peak is None:
                return None
            if self.event_open and grid_t != self._last_grid_t:
                self._quiet_grids += 1
            self._last_grid_t = grid_t
            if self.event_open and self._quiet_grids >= CLEAR_GRIDS:
                return HailClear(self.event_peak, self._quiet_grids)
            return None

        self._quiet_grids = 0
        self._last_grid_t = grid_t
        self.event_peak = max(self.event_peak, peak or 0.0)
        if tier <= self.notified_tier or where is None:
            return None
        return HailAlert(tier=tier, peak_percent=peak, near_percent=near,
                         distance_km=where[0], bearing_deg=where[1],
                         escalation=self.notified_tier != TIER_NONE)

    def commit(self, alert: "HailAlert | HailClear") -> None:
        if isinstance(alert, HailClear):
            self.notified_tier = TIER_NONE
            self.event_peak = 0.0
            self._quiet_grids = 0
            return
        self.notified_tier = alert.tier

    def to_dict(self) -> dict:
        return {"notified_tier": self.notified_tier,
                "event_peak": self.event_peak,
                "quiet_grids": self._quiet_grids,
                "last_grid_t": self._last_grid_t}

    @classmethod
    def from_dict(cls, watch_percent: float, severe_percent: float,
                  data: dict | None) -> "HailTracker":
        tracker = cls(watch_percent, severe_percent)
        if isinstance(data, dict):
            tracker.notified_tier = int(data.get("notified_tier", 0) or 0)
            tracker.event_peak = float(data.get("event_peak", 0.0) or 0.0)
            tracker._quiet_grids = int(data.get("quiet_grids", 0) or 0)
            tracker._last_grid_t = data.get("last_grid_t")
        return tracker


# ── Persistent state ──────────────────────────────────────────────────────────

def _load_state_file() -> dict:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    except OSError as e:
        _LOGGER.warning("[HailFront] cannot read %s: %s", STATE_PATH, e)
        return {}


def _save_state_entry(monitor_id: str, entry: dict) -> None:
    data = _load_state_file()
    data[monitor_id] = entry
    tmp = f"{STATE_PATH}.tmp"
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, STATE_PATH)
    except OSError as e:
        _LOGGER.warning("[HailFront] cannot write %s: %s", STATE_PATH, e)


def _clamp_percent(value, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if parsed <= 0:
        return default
    return max(PERCENT_FLOOR, min(PERCENT_CEILING, parsed))


# ── Monitor ───────────────────────────────────────────────────────────────────

class HailFrontLiveMonitor:
    """One live monitor entry of type 'hail_front'."""

    BLIND_LABELS = {
        "position": ("non sa da dove misurare",
                     "it does not know where to measure from"),
        "radar":    ("nessuna immagine radar recente",
                     "no recent radar image"),
        "coverage": ("il radar non copre l'area sorvegliata",
                     "the radar does not cover the watched area"),
    }

    def __init__(self, cfg: dict, telegram_send_fn, tz_name: str = "UTC"):
        self.monitor_id = cfg["id"]
        self.name       = cfg.get("name", "Hail")
        self.location   = cfg.get("location", "")
        self.language   = cfg.get("language", "it")
        self.tz_name    = tz_name
        self._send      = telegram_send_fn

        self.radius_km  = clamp_radius(cfg.get("radius_km", 30))
        self.latitude   = float(cfg.get("latitude", 0) or 0)
        self.longitude  = float(cfg.get("longitude", 0) or 0)
        self.position_id = (cfg.get("position_id") or "").strip()
        self.watch_percent = _clamp_percent(cfg.get("watch_percent"),
                                            DEFAULT_WATCH_PERCENT)
        self.severe_percent = max(
            self.watch_percent,
            _clamp_percent(cfg.get("severe_percent"), DEFAULT_SEVERE_PERCENT))
        self._quiet_start = (cfg.get("quiet_start") or "").strip()
        self._quiet_end   = (cfg.get("quiet_end") or "").strip()

        self._origin_grade = ORIGIN_FALLBACK
        self._origin_age_sec: float | None = None
        self._origin_point: tuple[float, float] = (self.latitude, self.longitude)
        self._blind_since = 0.0
        self._blind_reason = ""
        self._grid_t = 0.0

        self._tracker = HailTracker(self.watch_percent, self.severe_percent)
        self._poll_task: asyncio.Task | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._poll_task and not self._poll_task.done():
            return
        self._restore_state()
        radar_feed.acquire(PRODUCT_HAIL)
        self._poll_task = asyncio.create_task(
            self._poll_loop(), name=f"hail_front_poll:{self.monitor_id}")
        print(f"[HailFront] '{self.name}' started (radius={self.radius_km:.0f}km, "
              f"watch={self.watch_percent:g}%, severe={self.severe_percent:g}%, "
              f"origin={self.position_id or 'fixed'})")

    def stop(self) -> None:
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
        radar_feed.release(PRODUCT_HAIL)
        print(f"[HailFront] '{self.name}' stopped")

    async def aclose(self) -> None:
        task = self._poll_task
        if task and not task.done():
            task.cancel()
        radar_feed.release(PRODUCT_HAIL)
        if task:
            await asyncio.gather(task, return_exceptions=True)

    def is_running(self) -> bool:
        return self._poll_task is not None and not self._poll_task.done()

    def status(self) -> str:
        """stopped / blind / degraded / running — same vocabulary as rain front."""
        if not self.is_running():
            return "stopped"
        if self._blind_since:
            return "blind"
        return radar_feed.status()

    def blind_reason_label(self, lang: str = "it") -> str:
        if not self._blind_since:
            return ""
        pair = self.BLIND_LABELS.get(self._blind_reason)
        if pair is None:
            return self._blind_reason
        return pair[0] if lang == "it" else pair[1]

    # ── State persistence ─────────────────────────────────────────────────────

    def _restore_state(self) -> None:
        entry = _load_state_file().get(self.monitor_id)
        if not entry:
            return
        if time.time() - float(entry.get("updated_at", 0)) > STATE_MAX_AGE_SEC:
            _LOGGER.info("[HailFront] '%s' saved state too old — starting clean",
                         self.name)
            return
        self._tracker = HailTracker.from_dict(
            self.watch_percent, self.severe_percent, entry.get("tracker"))

    def _save_state(self) -> None:
        _save_state_entry(self.monitor_id, {
            "updated_at": time.time(),
            "tracker": self._tracker.to_dict(),
        })

    # ── Poll ──────────────────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        try:
            await self._tick(notify=False)
        except asyncio.CancelledError:
            return
        except Exception as e:
            _LOGGER.error("[HailFront] '%s' first tick failed: %s", self.name, e)

        while True:
            try:
                await asyncio.sleep(POLL_INTERVAL_SEC)
                await self._tick(notify=True)
            except asyncio.CancelledError:
                return
            except Exception as e:
                _LOGGER.error("[HailFront] '%s' poll error: %s", self.name, e)

    def _resolve_origin(self, now: float) -> tuple[float, float] | None:
        """Same chain as the rain front: fresh fix, else an old one, else the
        configured point, and blindness only when there is none of them."""
        if not self.position_id:
            self._origin_grade, self._origin_age_sec = ORIGIN_FALLBACK, None
            self._origin_point = (self.latitude, self.longitude)
            return self._origin_point

        budget = None
        if self._tracker.event_open:
            budget = position_manager.max_age_sec(self.position_id) * EVENT_STALE_FACTOR
        resolved = position_manager.resolve(self.position_id, now, max_age_sec=budget)
        if resolved is not None:
            state, grade = resolved
            self._origin_grade, self._origin_age_sec = grade, state.age_sec
            self._origin_point = (state.lat, state.lon)
            return self._origin_point

        if self.latitude or self.longitude:
            self._origin_grade, self._origin_age_sec = ORIGIN_FALLBACK, None
            self._origin_point = (self.latitude, self.longitude)
            return self._origin_point
        return None

    async def _tick(self, notify: bool) -> None:
        now = time.time()

        origin = self._resolve_origin(now)
        if origin is None:
            self._go_blind(now, "position")
            return

        grid = radar_feed.latest(PRODUCT_HAIL)
        if grid is None or not radar_feed.feed_ok(PRODUCT_HAIL, now):
            self._go_blind(now, "radar")
            return

        if coverage_fraction(grid, origin, self.radius_km) < MIN_COVERAGE_FRACTION:
            self._go_blind(now, "coverage")
            return

        if self._blind_since:
            _LOGGER.info("[HailFront] '%s' recovered from '%s' after %.0f min",
                         self.name, self._blind_reason,
                         (now - self._blind_since) / 60.0)
            self._blind_since = 0.0
            self._blind_reason = ""
        self._grid_t = grid.t

        top = peak_with_location(grid, origin, self.radius_km)
        near = peak_with_location(grid, origin, NEAR_RADIUS_KM)
        peak = top[0] if top else None
        near_value = near[0] if near else None
        where = None
        if top is not None:
            where = (distance_km(origin[0], origin[1], top[1], top[2]),
                     azimuth_deg(origin[0], origin[1], top[1], top[2]))

        alert = self._tracker.evaluate(peak, near_value, where, grid.t, feed_ok=True)
        _LOGGER.info("[HailFront] %s | peak=%s near=%s tier=%d notified=%d age=%.0fs",
                     self.name,
                     "—" if peak is None else f"{peak:.0f}%",
                     "—" if near_value is None else f"{near_value:.0f}%",
                     self._tracker.tier_of(peak, near_value),
                     self._tracker.notified_tier, now - grid.t)

        if alert is not None and notify:
            await self._dispatch(alert, now)

    def _go_blind(self, now: float, reason: str) -> None:
        """Perceive nothing until the missing input comes back. Not knowing is
        not the same as nothing happening, and only one of them may produce an
        all-clear — `evaluate(feed_ok=False)` is that refusal."""
        if not self._blind_since or self._blind_reason != reason:
            self._blind_since = self._blind_since or now
            self._blind_reason = reason
            _LOGGER.info("[HailFront] '%s' blind (%s) — frozen, no alerts and no "
                         "false all-clear", self.name, reason)
        self._tracker.evaluate(None, None, None, 0.0, feed_ok=False)

    # ── Dispatch ──────────────────────────────────────────────────────────────

    async def _dispatch(self, alert, now: float) -> None:
        if self._is_silenceable(alert) and self._in_quiet_hours():
            _LOGGER.info("[HailFront] '%s' alert suppressed by quiet hours", self.name)
            self._tracker.commit(alert)
            self._save_state()
            return

        text = self._format(alert, now)
        try:
            ok = await self._send(text)
        except Exception as e:
            _LOGGER.error("[HailFront] '%s' send error: %s", self.name, e)
            return
        # Only a REFUSED send is retried; an UNCONFIRMED one is committed, because
        # retrying it is what put the same message on the phone twice.
        if ok is False:
            _LOGGER.warning("[HailFront] '%s' alert NOT delivered — state held, "
                            "retry next poll", self.name)
            return
        if ok is None:
            _LOGGER.warning("[HailFront] '%s' delivery UNCONFIRMED — committed "
                            "anyway, the alert will not be sent again", self.name)

        self._tracker.commit(alert)
        self._save_state()
        _LOGGER.info("[HailFront] %s | committed %s", self.name,
                     f"tier {alert.tier}" if isinstance(alert, HailAlert) else "clear")

    @staticmethod
    def _is_silenceable(alert) -> bool:
        """Quiet hours may hold back a WATCH or the all-clear, never a WARNING."""
        return isinstance(alert, HailClear) or alert.tier < TIER_WARNING

    def _in_quiet_hours(self) -> bool:
        if not self._quiet_start or not self._quiet_end:
            return False
        try:
            sh, sm = map(int, self._quiet_start.split(":"))
            eh, em = map(int, self._quiet_end.split(":"))
            start, end = time_t(sh, sm), time_t(eh, em)
            now = datetime.now(self._tz()).time().replace(second=0, microsecond=0)
            if start <= end:
                return start <= now < end
            return now >= start or now < end
        except (ValueError, AttributeError):
            return False

    # ── Formatting ────────────────────────────────────────────────────────────

    def _format(self, alert, now: float) -> str:
        if isinstance(alert, HailClear):
            return self._fmt_clear(alert, now)
        return self._fmt_alert(alert, now)

    def _fmt_alert(self, alert: HailAlert, now: float) -> str:
        it = self.language == "it"
        warning = alert.tier >= TIER_WARNING
        if warning:
            head = (f"🚨 <b>Grandine probabile — {self._loc()}</b>" if it
                    else f"🚨 <b>Hail likely — {self._loc()}</b>")
        else:
            head = (f"🧊 <b>Rischio grandine — {self._loc()}</b>" if it
                    else f"🧊 <b>Hail risk — {self._loc()}</b>")
        lines = [head, self._origin_line()]

        if alert.distance_km <= 2.0:
            lines.append(
                (f"📍 Massimo <b>{alert.peak_percent:.0f}%</b> proprio sopra di te"
                 if it else
                 f"📍 Peak <b>{alert.peak_percent:.0f}%</b> right over you"))
        else:
            heading = direction_label(alert.bearing_deg, self.language)
            lines.append(
                (f"📍 Massimo <b>{alert.peak_percent:.0f}%</b> a "
                 f"<b>{alert.distance_km:.0f} km</b> a {heading} "
                 f"({alert.bearing_deg:.0f}°)") if it else
                (f"📍 Peak <b>{alert.peak_percent:.0f}%</b> at "
                 f"<b>{alert.distance_km:.0f} km</b> to {heading} "
                 f"({alert.bearing_deg:.0f}°)"))
        if alert.near_percent is not None and alert.near_percent >= self.watch_percent:
            lines.append(
                (f"⚠️ Entro {NEAR_RADIUS_KM:.0f} km: {alert.near_percent:.0f}%" if it
                 else f"⚠️ Within {NEAR_RADIUS_KM:.0f} km: {alert.near_percent:.0f}%"))
        if warning:
            lines.append("🚗 Metti al riparo auto e oggetti esposti" if it
                         else "🚗 Shelter vehicles and exposed items")
        lines.append(
            (f"🎯 Soglie: allerta {self.watch_percent:g}% · "
             f"grave {self.severe_percent:g}% · raggio {self.radius_km:.0f} km") if it
            else (f"🎯 Thresholds: watch {self.watch_percent:g}% · "
                  f"severe {self.severe_percent:g}% · radius {self.radius_km:.0f} km"))
        lines.append(self._radar_line(now))
        lines.append(f"🕐 {self._now_str()}")
        return "\n".join(line for line in lines if line)

    def _fmt_clear(self, alert: HailClear, now: float) -> str:
        it = self.language == "it"
        lines = [(f"✅ <b>Rischio grandine cessato — {self._loc()}</b>" if it
                  else f"✅ <b>Hail risk over — {self._loc()}</b>"),
                 self._origin_line(),
                 (f"🔇 Sotto il {self.watch_percent:g}% entro {self.radius_km:.0f} km "
                  f"da {alert.quiet_grids * 5} min circa" if it else
                  f"🔇 Below {self.watch_percent:g}% within {self.radius_km:.0f} km "
                  f"for about {alert.quiet_grids * 5} min"),
                 (f"📈 Massimo dell'evento: {alert.peak_percent:.0f}%" if it
                  else f"📈 Event peak: {alert.peak_percent:.0f}%"),
                 self._radar_line(now),
                 f"🕐 {self._now_str()}"]
        return "\n".join(line for line in lines if line)

    def _radar_line(self, now: float) -> str:
        """The age of the measurement, always: the product is published about ten
        minutes late and the reader has to reconcile it with the sky."""
        if not self._grid_t:
            return ""
        clock = datetime.fromtimestamp(self._grid_t, self._tz()).strftime("%H:%M")
        age = max(0, round((now - self._grid_t) / 60.0))
        return (f"📡 Radar delle {clock} ({age} min fa)" if self.language == "it"
                else f"📡 Radar at {clock} ({age} min ago)")

    def _origin_line(self) -> str:
        lat, lon = self._origin_point
        return origin_line(
            lat, lon, self._origin_grade, self.language,
            age_sec=self._origin_age_sec,
            position_name=(position_manager.name_of(self.position_id) or ""
                           if self.position_id else ""),
        )

    def _plain_location(self) -> str:
        if self.position_id:
            return (position_manager.name_of(self.position_id)
                    or self.location or self.name)
        return self.location or self.name

    def _loc(self) -> str:
        return html.escape(self._plain_location())

    def _tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.tz_name)
        except (ZoneInfoNotFoundError, ValueError):
            return ZoneInfo("UTC")

    def _now_str(self) -> str:
        return datetime.now(self._tz()).strftime("%H:%M")


# ── Manager ───────────────────────────────────────────────────────────────────

_FINGERPRINT_FIELDS = (
    "name", "location", "latitude", "longitude", "radius_km", "language",
    "quiet_start", "quiet_end", "telegram_bot_id", "position_id",
    "watch_percent", "severe_percent",
)


def _fingerprint(cfg: dict, tz_name: str) -> str:
    return json.dumps([cfg.get(k) for k in _FINGERPRINT_FIELDS] + [tz_name],
                      sort_keys=True, default=str)


class HailFrontMonitorManager:
    """Owns every hail_front monitor instance."""

    def __init__(self):
        self._monitors: dict[str, HailFrontLiveMonitor] = {}
        self._fingerprints: dict[str, str] = {}

    def reload(self, configs: list[dict], make_send_fn, tz_name: str):
        wanted: set[str] = set()
        for cfg in configs:
            if cfg.get("type") != "hail_front" or not cfg.get("enabled"):
                continue
            mid = cfg["id"]
            wanted.add(mid)
            fingerprint = _fingerprint(cfg, tz_name)
            existing = self._monitors.get(mid)
            if (existing and self._fingerprints.get(mid) == fingerprint
                    and existing.is_running()):
                continue
            self._fingerprints[mid] = fingerprint
            replacement = HailFrontLiveMonitor(cfg, make_send_fn(cfg), tz_name)
            self._monitors[mid] = replacement
            self._swap(existing, replacement)

        for mid in list(self._monitors):
            if mid not in wanted:
                self._swap(self._monitors.pop(mid), None)
                self._fingerprints.pop(mid, None)

    @staticmethod
    def _swap(old: "HailFrontLiveMonitor | None",
              new: "HailFrontLiveMonitor | None"):
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
        for monitor in self._monitors.values():
            monitor.stop()
        self._monitors.clear()
        self._fingerprints.clear()

    def status(self, monitor_id: str) -> str:
        monitor = self._monitors.get(monitor_id)
        return monitor.status() if monitor else "stopped"

    def get(self, monitor_id: str) -> HailFrontLiveMonitor | None:
        return self._monitors.get(monitor_id)


hail_front_monitor_manager = HailFrontMonitorManager()


__all__ = [
    "STATE_PATH", "TIER_NONE", "TIER_WATCH", "TIER_WARNING", "CLEAR_GRIDS",
    "NEAR_RADIUS_KM", "HailAlert", "HailClear", "HailTracker",
    "HailFrontLiveMonitor", "HailFrontMonitorManager", "hail_front_monitor_manager",
]
