"""
live_monitors/storm_front.py
─────────────────────────────
LLM-free live monitor: watches the Blitzortung feed and reports an approaching
storm as a short ladder of ring messages, each with a polar radar attached.

This module is the I/O half — feed lifecycle, persistence, quiet hours, message
formatting and the Telegram handshake. Every decision lives in
`storm_front_core`, which is pure and unit-tested; the maths lives in `geo`; the
picture lives in `storm_front_chart`.

Pipeline
────────
  1. `BlitzortungFeed` keeps one MQTT connection open and buffers the last
     WINDOW_MIN of strikes as (t, lat, lon) — no geometry at ingest.
  2. Every POLL_INTERVAL_SEC the buffer is binned into rings × sectors, the
     front of each sector is derived, and the tracker decides whether anything
     needs saying.
  3. A ring is announced at most once per event, so one storm produces at most
     `ring_count` messages plus one all-clear. There is no periodic re-alert.
  4. The committed state advances ONLY on confirmed delivery, so a dropped
     message is retried — and because the retry is rebuilt from the current
     frame, it is never stale.

State survives a restart via /data/storm_front_state.json: a storm in progress
does not re-announce rings it already announced, and does not lose its all-clear.

Origin
──────
`position_id` selects where the radar is centred. Empty — the default, and what
every monitor configured before this existed inherits — uses the configured
coordinates and never touches the position manager. Otherwise it names a position
from `position.py`, and the monitor follows THAT and nothing else: there is no
fallback, because watching the configured house while the user is two hundred
kilometres away answers a different question without saying so. With no usable
fix the monitor goes blind and freezes; see `_go_blind`.
"""

import asyncio
import html
import json
import logging
import os
import time
from datetime import datetime, time as time_t
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .blitzortung import BlitzortungFeed
from .geo import direction_label, distance_km
from .position import position_manager
from .position_core import MAX_PLAUSIBLE_KMH
from .snapshot import Snapshot, describe_origin, preview_alert
from .storm_front_core import (
    CLEAR_DWELL_SEC, EVENT_IDLE, OBSERVE_FACTOR, POLL_INTERVAL_SEC, WINDOW_MIN,
    TRACK_CLOSING, TRACK_GRAZING,
    ClearAlert, RingAlert, StormFrontTracker,
    angle_delta, build_frame, clamp_radius, clamp_ring_count, ring_edges,
)

_LOGGER = logging.getLogger(__name__)

STATE_PATH = "/data/storm_front_state.json"
# A restored state older than this is discarded. Shorter than the old monitor's
# hour because what is restored here is "an event is open", which goes stale
# faster than a threat level.
STATE_MAX_AGE_SEC = 1800

# Quiet hours silence the outer rings only. Anything at or inside this fraction
# of the radius is close enough to be worth waking up for.
QUIET_OVERRIDE_FRACTION = 0.40

# A chart must never delay an alert. If it is not ready in time, the message
# goes out as text.
CHART_TIMEOUT_SEC = 20.0

# ── Moving origin ─────────────────────────────────────────────────────────────
#
# With a `position_id` set, the origin follows that phone. Nothing in the
# perception pipeline had to change for that: the strike buffer holds ABSOLUTE
# coordinates and the geometry is rebuilt against the current origin on every poll
# (see `blitzortung.py`), so a different origin simply produces a different — and
# correct — frame.
#
# The CBDR verdict comes along for free, and this is the part worth understanding.
# `track_verdict` compares bearings and ranges MEASURED FROM THE ORIGIN. Let the
# origin move and those become relative bearings and relative ranges, which is
# exactly what the mariner's rule is defined on: a constant relative bearing with
# a decreasing relative range means collision whether or not the observer is under
# way. Driving into a storm therefore reads as TRACK_CLOSING without a single line
# of new decision logic.
#
# What does NOT come for free is the difference between MOVING and BEING MOVED. A
# continuous track is signal; a jump — a mislocated fix, or the position coming
# back after a blackout somewhere else entirely — is a change of reference frame.
# The stored bearings were then measured from somewhere else and comparing them to
# the new ones would fabricate a rotation that never happened.

# While an event is open a stale fix is tolerated for longer: losing GPS in a
# tunnel mid-storm should not blind the monitor, because the last known position
# is still the best evidence available. Outside an event there is no history to
# protect and the normal budget applies.
EVENT_STALE_FACTOR = 3.0

# Two consecutive origins further apart than this many km per hour of elapsed time
# were not travelled between — the observer was relocated.
ORIGIN_JUMP_KMH = MAX_PLAUSIBLE_KMH

# A course within this much of the front's bearing counts as "heading for it".
HEADING_TOLERANCE_DEG = 45.0


# ── Persistent state ──────────────────────────────────────────────────────────

def _load_state_file() -> dict:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    except OSError as e:
        _LOGGER.warning("[StormFront] cannot read %s: %s", STATE_PATH, e)
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
        _LOGGER.warning("[StormFront] cannot write %s: %s", STATE_PATH, e)


# ── Monitor ───────────────────────────────────────────────────────────────────

class StormFrontLiveMonitor:
    """One live monitor entry of type 'storm_front'."""

    def __init__(self, cfg: dict, telegram_send_fn, tz_name: str = "UTC"):
        self.monitor_id = cfg["id"]
        self.name       = cfg.get("name", "Storm front")
        self.location   = cfg.get("location", "")
        self.language   = cfg.get("language", "it")
        self.tz_name    = tz_name
        self._send      = telegram_send_fn

        # The shared LiveMonitorPayload defaults radius_km to 100, which this
        # algorithm is not designed for — clamp loudly rather than trust it.
        raw_radius = cfg.get("radius_km", 30)
        self.radius_km  = clamp_radius(raw_radius)
        if abs(float(raw_radius or 0) - self.radius_km) > 0.01:
            _LOGGER.warning("[StormFront] '%s' radius %s km clamped to %.0f km",
                            self.name, raw_radius, self.radius_km)
        self.ring_count = clamp_ring_count(cfg.get("ring_count", 4))
        self.edges      = ring_edges(self.radius_km, self.ring_count)
        self.observe_radius_km = self.radius_km * OBSERVE_FACTOR

        self.latitude  = float(cfg.get("latitude", 0) or 0)
        self.longitude = float(cfg.get("longitude", 0) or 0)
        # Empty (the default) is the historical behaviour to the letter. Otherwise
        # it names a position, and the monitor follows THAT and nothing else.
        self.position_id = (cfg.get("position_id") or "").strip()
        self._chart    = bool(cfg.get("chart", True))
        self._quiet_start = (cfg.get("quiet_start") or "").strip()
        self._quiet_end   = (cfg.get("quiet_end") or "").strip()

        # Origin bookkeeping — only ever used when following a position.
        self._motion = None                       # PositionState | None
        self._last_origin: tuple[float, float] | None = None
        self._last_origin_at = 0.0
        self._last_discontinuity: int | None = None
        self._blind_since = 0.0

        self._tracker = StormFrontTracker(self.radius_km, self.ring_count)
        self._feed = BlitzortungFeed(
            name=self.name, monitor_id=self.monitor_id,
            lat=self.latitude, lon=self.longitude,
            coverage_radius_km=self.observe_radius_km,
            window_sec=WINDOW_MIN * 60.0,
        )
        self._poll_task: asyncio.Task | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._poll_task and not self._poll_task.done():
            return
        self._restore_state()
        # A monitor following a position has no coordinates yet, so it has nothing
        # to derive geohash topics from. The feed starts on the first usable fix
        # instead — see `_ensure_feed`.
        if not self.position_id:
            self._feed.start()
        self._poll_task = asyncio.create_task(
            self._poll_loop(), name=f"storm_front_poll:{self.monitor_id}"
        )
        print(f"[StormFront] '{self.name}' started (radius={self.radius_km:.0f}km, "
              f"rings={self.ring_count}, edges={[round(e) for e in self.edges]}, "
              f"observe={self.observe_radius_km:.0f}km, "
              f"origin={self.position_id or 'fixed'})")

    def stop(self) -> None:
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
        self._feed.stop()
        print(f"[StormFront] '{self.name}' stopped")

    async def aclose(self) -> None:
        """Cancel and wait, so the old MQTT client is fully disconnected before
        its replacement connects."""
        task = self._poll_task
        if task and not task.done():
            task.cancel()
        await self._feed.aclose()
        if task:
            await asyncio.gather(task, return_exceptions=True)

    def is_running(self) -> bool:
        return self._poll_task is not None and not self._poll_task.done()

    def status(self) -> str:
        if not self.is_running():
            return "stopped"
        return self._feed.status()

    # ── State persistence ─────────────────────────────────────────────────────

    def _restore_state(self) -> None:
        entry = _load_state_file().get(self.monitor_id)
        if not entry:
            return
        if time.time() - float(entry.get("updated_at", 0)) > STATE_MAX_AGE_SEC:
            _LOGGER.info("[StormFront] '%s' saved state too old — starting clean",
                         self.name)
            return
        self._tracker = StormFrontTracker.from_dict(
            self.radius_km, self.ring_count, entry.get("tracker"))

    def _save_state(self) -> None:
        _save_state_entry(self.monitor_id, {
            "updated_at": time.time(),
            "tracker": self._tracker.to_dict(),
        })

    # ── Poll loop ─────────────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        # Evaluate immediately so the monitor knows where it stands rather than
        # staying blind for a whole interval. No alerts on this pass: any decision
        # is simply re-offered on the next poll.
        try:
            await self._tick(notify=False)
        except asyncio.CancelledError:
            return
        except Exception as e:
            _LOGGER.error("[StormFront] '%s' first tick failed: %s", self.name, e)

        while True:
            try:
                await asyncio.sleep(POLL_INTERVAL_SEC)
                await self._tick(notify=True)
            except asyncio.CancelledError:
                return
            except Exception as e:
                _LOGGER.error("[StormFront] '%s' poll error: %s", self.name, e)

    # ── Origin ────────────────────────────────────────────────────────────────

    def _resolve_origin(self, now: float) -> tuple[float, float] | None:
        """Where the radar is centred this poll, or None if that is unknown.

        With no position configured this is the fixed point and nothing else
        happens — a monitor that has not opted in never consults the position
        manager and never causes it to connect.

        Following a position, there is NO fallback. Watching the configured house
        while the user is two hundred kilometres away is not a gentle degradation:
        it answers a different question without saying so. None means "I do not
        know where I am", and the caller treats that as blindness.
        """
        if not self.position_id:
            return (self.latitude, self.longitude)

        # A storm in progress buys patience: a fix that went stale in a tunnel is
        # still the best evidence available, and changing frame mid-event would
        # throw away the CBDR history for no gain.
        budget = None
        if self._tracker.event_state != EVENT_IDLE:
            budget = position_manager.max_age_sec(self.position_id) * EVENT_STALE_FACTOR

        state = position_manager.usable(self.position_id, now, max_age_sec=budget)
        if state is None:
            self._motion = None
            return None

        self._motion = state
        return state.lat, state.lon

    def _origin_jumped(self, origin: tuple[float, float], now: float) -> bool:
        """True when the observer was relocated rather than having travelled.

        Two ways that happens, and both invalidate the stored geometry equally:
        the position source reported a discontinuity of its own, or the two
        origins are further apart than anything could have covered in the elapsed
        time.
        """
        if self._last_origin is None:
            return False                      # first poll — nothing to compare

        if self._motion is not None:
            seen = self._motion.discontinuity
            jumped = (self._last_discontinuity is not None
                      and seen != self._last_discontinuity)
            self._last_discontinuity = seen
            if jumped:
                return True

        elapsed = max(1.0, now - self._last_origin_at)
        moved = distance_km(*self._last_origin, *origin)
        return moved / (elapsed / 3600.0) > ORIGIN_JUMP_KMH

    async def _ensure_feed(self, origin: tuple[float, float]) -> None:
        """Aim the Blitzortung subscription at `origin`, starting it if needed.

        A monitor following a position cannot subscribe until it knows where it
        is, so the feed starts here rather than in `start()`. Afterwards this is
        the re-aim: a geohash cell is ~110 km across, so an ordinary journey
        changes nothing and `retune` is a no-op.
        """
        try:
            await self._feed.retune(*origin)
            if not self._feed.is_running():
                self._feed.start()
        except Exception as e:
            _LOGGER.warning("[StormFront] '%s' feed retune failed: %s", self.name, e)

    # ── Poll ──────────────────────────────────────────────────────────────────

    async def _tick(self, notify: bool) -> None:
        now = time.time()

        origin = self._resolve_origin(now)
        if origin is None:
            self._go_blind(now)
            return

        if self._blind_since:
            _LOGGER.info("[StormFront] '%s' position recovered after %.0f min",
                         self.name, (now - self._blind_since) / 60.0)
            self._blind_since = 0.0
            # The bearings stored before the blackout were measured from wherever
            # the user was then, which is not where they are now.
            self._tracker.reset_geometry_history()
            self._last_origin = None

        if self.position_id:
            if self._origin_jumped(origin, now):
                _LOGGER.info("[StormFront] '%s' origin discontinuity — CBDR "
                             "history reset (event left open)", self.name)
                self._tracker.reset_geometry_history()
            await self._ensure_feed(origin)
        self._last_origin, self._last_origin_at = origin, now

        strikes = self._feed.strikes(now)
        frame = build_frame(strikes, origin, now,
                            self.radius_km, self.observe_radius_km,
                            WINDOW_MIN * 60.0)
        alert = self._tracker.evaluate(frame, now, self._feed.feed_ok(),
                                       self._feed.connected_for(now))

        stats = self._feed.stats()
        _LOGGER.info("[StormFront] %s | %s msgs=%d%s", self.name,
                     self._tracker.debug_line(frame), stats["messages"],
                     self._origin_debug(origin))

        if alert is not None and notify:
            await self._dispatch(alert, frame, list(strikes), now, origin=origin)

    def _go_blind(self, now: float) -> None:
        """No usable position: perceive nothing until one comes back.

        This is the same blindness the monitor already handles when the strike
        feed drops, and it is handled the same way rather than with new state.
        Not knowing where you are and not knowing where the lightning is are the
        same epistemic problem, and the dangerous failure is identical: a monitor
        that cannot tell "nothing is happening" from "I cannot see" will
        cheerfully announce that the storm has cleared.

        `evaluate(feed_ok=False)` is that refusal, already written and already
        tested: no alert of any kind, and the all-clear countdown reset so the
        silence can never be mistaken for calm.
        """
        if not self._blind_since:
            self._blind_since = now
            _LOGGER.info("[StormFront] '%s' has no usable position — frozen "
                         "(no alerts, no false all-clear)", self.name)
        self._tracker.evaluate(
            build_frame([], (0.0, 0.0), now, self.radius_km,
                        self.observe_radius_km, WINDOW_MIN * 60.0),
            now, feed_ok=False)

    # ── Snapshot ──────────────────────────────────────────────────────────────

    async def snapshot(self, now: float) -> Snapshot:
        """What this monitor perceives right now — perception without decision.

        Deliberately never calls `self._tracker.evaluate()` or `.commit()`.
        Invariant A rests on `notified_ring`, so a diagnostic that advanced it
        would silence the real storm half an hour later, and one that opened an
        event would let a single cell emit a second full ladder of rings. The
        tracker is read here and nowhere written; a test asserts its state is
        identical before and after.

        Unlike the rain front there is no on-demand fallback: the strike buffer
        only fills while the MQTT subscription is up, so a stopped monitor has
        nothing to show and the caller says so.
        """
        origin_info = describe_origin(self, now)
        common = dict(
            monitor_id=self.monitor_id, name=self.name, kind="storm",
            language=self.language, tz_name=self.tz_name,
            status=self.status(),
            running=self.is_running(), origin=origin_info,
            event_open=self._tracker.event_state != EVENT_IDLE,
            notified_ring=self._tracker.notified_ring,
            ring_count=self.ring_count, radius_km=self.radius_km,
            feed_connected=self._feed.feed_ok(),
        )
        if not origin_info.usable:
            return Snapshot(blind_reason=origin_info.reason or "position unusable",
                            **common)
        if not self.is_running():
            return Snapshot(
                blind_reason=("the monitor is stopped — lightning can only be "
                              "buffered while connected, so there is nothing to "
                              "show until it runs"), **common)

        origin = (origin_info.lat, origin_info.lon)
        strikes = list(self._feed.strikes(now))
        frame = build_frame(strikes, origin, now, self.radius_km,
                            self.observe_radius_km, WINDOW_MIN * 60.0)
        alert = preview_alert(frame, self.edges, self.ring_count)
        picture = await self._render_chart(alert, frame, strikes, now, origin=origin) \
            if alert is not None else None

        return Snapshot(
            front_km=frame.dominant.front_km if frame.dominant else None,
            front_bearing_deg=frame.dominant.bearing_deg if frame.dominant else None,
            activity=frame.strikes_in_radius,
            picture=picture, **common)

    def _origin_debug(self, origin: tuple[float, float]) -> str:
        if not self.position_id or self._motion is None:
            return ""
        speed = self._motion.speed_kmh
        return (f" origin={origin[0]:.4f},{origin[1]:.4f} "
                f"age={self._motion.age_sec:.0f}s "
                f"v={'—' if speed is None else f'{speed:.0f}km/h'}")

    # ── Dispatch ──────────────────────────────────────────────────────────────

    async def _dispatch(self, alert, frame, strikes, now: float, *,
                        origin: tuple[float, float] | None = None) -> None:
        """`origin` defaults to the configured point: the radar is drawn around
        wherever the frame was measured from, and in fixed mode that is the only
        origin there has ever been."""
        origin = origin or (self.latitude, self.longitude)
        if self._is_silenceable(alert) and self._in_quiet_hours():
            _LOGGER.info("[StormFront] '%s' alert suppressed by quiet hours",
                         self.name)
            # Commit anyway: silencing delivery must not desync the state, or the
            # alert would be retried every minute until the window ends.
            self._tracker.commit(alert, now)
            self._save_state()
            return

        text = self._format(alert)
        photo = await self._render_chart(alert, frame, strikes, now, origin=origin)

        try:
            ok = await self._send(text, photo=photo) if photo else await self._send(text)
        except Exception as e:
            _LOGGER.error("[StormFront] '%s' send error: %s", self.name, e)
            return
        if not ok:
            _LOGGER.warning("[StormFront] '%s' alert NOT delivered — state held, "
                            "retry next poll", self.name)
            return

        self._tracker.commit(alert, now)
        self._save_state()
        _LOGGER.info("[StormFront] %s | committed %s", self.name,
                     f"ring {alert.ring}" if isinstance(alert, RingAlert) else "clear")

    async def _render_chart(self, alert, frame, strikes, now: float, *,
                            origin: tuple[float, float] | None = None) -> bytes | None:
        """Render the radar off the event loop. Any failure degrades to text —
        a picture must never be able to stop an alert."""
        if not self._chart or not isinstance(alert, RingAlert):
            return None
        origin = origin or (self.latitude, self.longitude)
        try:
            from .storm_front_chart import render_radar
            return await asyncio.wait_for(
                asyncio.to_thread(
                    render_radar, strikes, origin, now,
                    alert, radius_km=self.radius_km,
                    observe_radius_km=self.observe_radius_km, edges=self.edges,
                    window_sec=WINDOW_MIN * 60.0, location=self._plain_location(),
                    lang=self.language, tz=self._tz(),
                ),
                timeout=CHART_TIMEOUT_SEC,
            )
        except Exception as e:
            _LOGGER.warning("[StormFront] '%s' chart failed (%s) — sending text only",
                            self.name, e)
            return None

    def _is_silenceable(self, alert) -> bool:
        """Quiet hours hide the distant rings and the all-clear. A front already
        well inside the radius always gets through."""
        if isinstance(alert, ClearAlert):
            return True
        return alert.ring_edge_km > self.radius_km * QUIET_OVERRIDE_FRACTION

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
            return now >= start or now < end        # window crossing midnight
        except (ValueError, AttributeError):
            return False

    # ── Formatting ────────────────────────────────────────────────────────────

    def _format(self, alert) -> str:
        if isinstance(alert, ClearAlert):
            return self._fmt_clear(alert)
        return self._fmt_ring(alert)

    _RING_HEADS_IT = ["⛈️ <b>Temporale nel raggio", "⚡ <b>Temporale più vicino",
                      "🟠 <b>Temporale vicino", "🔴 <b>Temporale sopra di te"]
    _RING_HEADS_EN = ["⛈️ <b>Storm within range", "⚡ <b>Storm closing in",
                      "🟠 <b>Storm nearby", "🔴 <b>Storm overhead"]

    def _head(self, alert: RingAlert) -> str:
        heads = self._RING_HEADS_IT if self.language == "it" else self._RING_HEADS_EN
        if alert.is_innermost:
            head = heads[-1]
        else:
            head = heads[min(alert.ring - 1, len(heads) - 2)]
        return f"{head} — {self._loc()}</b>"

    def _track_line(self, alert: RingAlert) -> str:
        it = self.language == "it"
        if alert.is_innermost and alert.track != TRACK_GRAZING:
            return "🧭 Sei sotto il temporale" if it else "🧭 You are under the storm"
        if alert.track == TRACK_CLOSING:
            return ("🧭 Rotta costante: ti arriva addosso" if it
                    else "🧭 Constant bearing: heading straight for you")
        if alert.track == TRACK_GRAZING:
            side = direction_label(alert.pass_bearing_deg if alert.pass_bearing_deg
                                   is not None else alert.bearing_deg, self.language)
            return (f"🧭 Ti sfiora: passa a {side}" if it
                    else f"🧭 Glancing pass: going by to the {side}")
        if alert.new_cell:
            side = direction_label(alert.bearing_deg, self.language)
            return (f"🧭 Nuovo nucleo da {side} — traiettoria non determinabile" if it
                    else f"🧭 New cell from {side} — track not determinable")
        return ("🧭 Traiettoria non ancora determinabile" if it
                else "🧭 Track not yet determinable")

    def _motion_line(self, alert: RingAlert) -> str | None:
        """The user's own movement, or None when they are not moving.

        This line EXPLAINS the verdict above it, it does not compete with it. The
        CBDR reading is already relative — it was computed from a moving origin —
        so the two can never disagree: this only names the reason the range is
        closing. Wording it as a second opinion would eventually put "heading
        straight for you" next to "glancing pass" and destroy the user's trust in
        both.
        """
        motion = self._motion
        if motion is None or not motion.moving or motion.course_deg is None:
            return None

        it = self.language == "it"
        speed = motion.speed_kmh
        heading = direction_label(motion.course_deg, self.language)
        toward = abs(angle_delta(motion.course_deg,
                                 alert.bearing_deg)) <= HEADING_TOLERANCE_DEG
        if toward:
            return (f"🚗 Ti stai dirigendo verso il temporale a {speed:.0f} km/h" if it
                    else f"🚗 You are heading towards the storm at {speed:.0f} km/h")
        return (f"🚗 In movimento a {speed:.0f} km/h verso {heading}" if it
                else f"🚗 Moving at {speed:.0f} km/h towards {heading}")

    def _fmt_ring(self, alert: RingAlert) -> str:
        it = self.language == "it"
        heading = direction_label(alert.bearing_deg, self.language)
        lines = [self._head(alert)]
        lines.append(
            (f"📍 Fronte a <b>{alert.front_km:.0f} km</b> a {heading} "
             f"({alert.bearing_deg:.0f}°)") if it else
            (f"📍 Front at <b>{alert.front_km:.0f} km</b> to {heading} "
             f"({alert.bearing_deg:.0f}°)")
        )
        lines.append(
            (f"🎯 Anello {alert.ring}/{alert.ring_count} · entro "
             f"{alert.ring_edge_km:.0f} km") if it else
            (f"🎯 Ring {alert.ring}/{alert.ring_count} · within "
             f"{alert.ring_edge_km:.0f} km")
        )
        lines.append(self._track_line(alert))
        motion_line = self._motion_line(alert)
        if motion_line:
            lines.append(motion_line)

        if (alert.prev_front_km is not None and alert.elapsed_sec
                and alert.elapsed_sec > 0):
            minutes = alert.elapsed_sec / 60.0
            lines.append(
                (f"⏱️ Da {alert.prev_front_km:.0f} a {alert.front_km:.0f} km "
                 f"in {minutes:.0f} min") if it else
                (f"⏱️ From {alert.prev_front_km:.0f} to {alert.front_km:.0f} km "
                 f"in {minutes:.0f} min")
            )

        lines.append(
            (f"🔢 {alert.strikes} fulmini in {WINDOW_MIN:.0f} min "
             f"(settore {heading})") if it else
            (f"🔢 {alert.strikes} strikes in {WINDOW_MIN:.0f} min "
             f"({heading} sector)")
        )
        if alert.secondary:
            others = ", ".join(
                f"{direction_label(s.bearing_deg, self.language)} "
                f"a {s.front_km:.0f} km" if it else
                f"{direction_label(s.bearing_deg, self.language)} at {s.front_km:.0f} km"
                for s in alert.secondary
            )
            lines.append((f"➕ Altra attività: {others}" if it
                          else f"➕ Other activity: {others}"))
        lines.append(f"🕐 {self._now_str()}")
        return "\n".join(lines)

    def _fmt_clear(self, alert: ClearAlert) -> str:
        it = self.language == "it"
        lines = [(f"✅ <b>Temporale cessato — {self._loc()}</b>" if it
                  else f"✅ <b>Storm cleared — {self._loc()}</b>")]
        quiet_min = alert.quiet_sec / 60.0
        lines.append(
            (f"🔇 Nessuna attività entro {alert.radius_km:.0f} km "
             f"da {quiet_min:.0f} min") if it else
            (f"🔇 No activity within {alert.radius_km:.0f} km "
             f"for {quiet_min:.0f} min")
        )
        if alert.closest_km is not None:
            when = datetime.fromtimestamp(alert.closest_at, self._tz()).strftime("%H:%M")
            lines.append(
                (f"📉 Massimo avvicinamento: {alert.closest_km:.0f} km "
                 f"(anello {alert.closest_ring}/{alert.ring_count}) alle {when}") if it else
                (f"📉 Closest approach: {alert.closest_km:.0f} km "
                 f"(ring {alert.closest_ring}/{alert.ring_count}) at {when}")
            )
        lines.append(f"🕐 {self._now_str()}")
        return "\n".join(lines)

    def _plain_location(self) -> str:
        """What the message and the radar are titled with.

        Following a position, that position's NAME is the honest title: it says
        where these distances were measured from, which is the thing you need to
        know when several phones in the house are monitored. A configured place
        name would be worse than useless here — heading an alert "Bacoli" while
        the user is 200 km away tells them the storm is somewhere it is not.
        """
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

# Only these fields change how the monitor behaves. Anything else changing in
# live_monitors.json must NOT restart a running monitor — all managers are handed
# the whole config list on every save, and a restart would wipe the strike buffer
# and the in-flight storm.
_FINGERPRINT_FIELDS = (
    "name", "location", "latitude", "longitude", "radius_km", "ring_count",
    "language", "quiet_start", "quiet_end", "chart", "telegram_bot_id",
    # Changing which position a monitor follows changes what it measures, so it
    # must restart. The coordinates themselves never appear here — they live in
    # the position manager and never touch live_monitors.json, which is exactly
    # why a moving user does not restart the monitor once a minute and lose the
    # strike buffer along with the storm in progress.
    "position_id",
)


def _fingerprint(cfg: dict, tz_name: str) -> str:
    return json.dumps([cfg.get(k) for k in _FINGERPRINT_FIELDS] + [tz_name],
                      sort_keys=True, default=str)


class StormFrontMonitorManager:
    """Owns every storm_front monitor instance. Called by main.py on startup and
    on config changes."""

    def __init__(self):
        self._monitors: dict[str, StormFrontLiveMonitor] = {}
        self._fingerprints: dict[str, str] = {}

    def reload(self, configs: list[dict], make_send_fn, tz_name: str):
        wanted: set[str] = set()
        for cfg in configs:
            if cfg.get("type") != "storm_front" or not cfg.get("enabled"):
                continue
            mid = cfg["id"]
            wanted.add(mid)
            fingerprint = _fingerprint(cfg, tz_name)
            existing = self._monitors.get(mid)
            if (existing and self._fingerprints.get(mid) == fingerprint
                    and existing.is_running()):
                continue                      # unchanged — leave the storm alone
            self._fingerprints[mid] = fingerprint
            replacement = StormFrontLiveMonitor(cfg, make_send_fn(cfg), tz_name)
            self._monitors[mid] = replacement
            self._swap(existing, replacement)

        for mid in list(self._monitors):
            if mid not in wanted:
                self._swap(self._monitors.pop(mid), None)
                self._fingerprints.pop(mid, None)

    @staticmethod
    def _swap(old: StormFrontLiveMonitor | None, new: StormFrontLiveMonitor | None):
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
        for monitor in self._monitors.values():
            monitor.stop()
        self._monitors.clear()
        self._fingerprints.clear()

    def status(self, monitor_id: str) -> str:
        monitor = self._monitors.get(monitor_id)
        return monitor.status() if monitor else "stopped"

    def get(self, monitor_id: str) -> StormFrontLiveMonitor | None:
        """The running instance, or None. Symmetric with `PositionManager.get`.

        Without it a caller wanting to ask a monitor what it perceives would have
        to reach into `_monitors`, and an accessor is cheaper than a convention
        everyone breaks.
        """
        return self._monitors.get(monitor_id)


storm_front_monitor_manager = StormFrontMonitorManager()


# ── Migration from the retired 'lightning' type ───────────────────────────────

# Above this the old radius carried no useful intent (100 km was simply the
# shared default), so the migration picks the new default instead of clamping.
MAX_MIGRATED_RADIUS_KM = 60.0


def migrate_lightning_configs(configs: list[dict]) -> tuple[list[dict], int]:
    """Convert saved 'lightning' monitors to 'storm_front'.

    Without this a monitor configured before v4.0.0 would keep a type no manager
    claims: it would simply never start, silently and with no error anywhere the
    user can see. Returns (configs, migrated_count); the caller persists them.
    """
    migrated = 0
    for cfg in configs:
        if cfg.get("type") != "lightning":
            continue
        cfg["type"] = "storm_front"
        # The old default radius was 100 km — far outside this algorithm's range.
        old_radius = float(cfg.get("radius_km", 100) or 100)
        cfg["radius_km"] = 30.0 if old_radius > MAX_MIGRATED_RADIUS_KM \
            else clamp_radius(old_radius)
        cfg.setdefault("ring_count", 4)
        cfg.setdefault("chart", True)
        cfg.pop("sensitivity", None)
        cfg.pop("record_strikes", None)
        migrated += 1
    return configs, migrated


def migrate_position_source_configs(configs: list[dict],
                                    position_id: str | None) -> tuple[list[dict], int]:
    """Convert the unreleased `position_source` flag to a `position_id`.

    v4.1.0 marked a monitor as following "the" position with `position_source:
    "live"`, back when there was only one. Positions are now named entities
    selected by id, so a leftover flag would point at nothing: the monitor would
    fall through to its fixed coordinates and silently watch the wrong place.

    `position_id` is the id created by the settings-side migration, or None if
    there was no position to migrate — in which case the monitor goes back to
    being a fixed one, which is the only honest outcome when the source it was
    following does not exist.
    """
    migrated = 0
    for cfg in configs:
        if "position_source" not in cfg:
            continue
        was_live = cfg.pop("position_source") == "live"
        if was_live and position_id:
            cfg["position_id"] = position_id
        else:
            cfg.setdefault("position_id", "")
        migrated += 1
    return configs, migrated
