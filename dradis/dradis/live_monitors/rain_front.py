"""
live_monitors/rain_front.py
────────────────────────────
LLM-free live monitor: watches the national radar composite and reports
approaching rain as a short ladder of ring messages, each with the radar picture
attached.

The twin of `storm_front.py`, and deliberately so. That module answers "is there
lightning coming"; this one answers "is there rain coming, and will it and I
actually meet". The I/O half lives here — feed lifecycle, persistence, quiet
hours, message formatting, Telegram handshake. The raster maths lives in
`radar_core`, the source in `radar`, the picture in `rain_front_chart`, and every
ring/event decision in `storm_front_core`, reused unchanged.

What is inherited, and what could not be
────────────────────────────────────────
`StormFrontTracker` is taken whole: the ring ladder with hysteresis, the two-poll
confirmation of a descent, the IDLE/ACTIVE/FADING lifecycle, the all-clear dwell,
the state file, and above all invariant A — one event emits at most `ring_count`
messages plus one all-clear, for any input. Six generations of storm monitor paid
for that behaviour and none of it is weather-specific.

Its FRONT ESTIMATOR could not be inherited; see the note above
`radar_core.build_rain_frame` for the measurement that settles it.

Two properties of the source that shape everything
──────────────────────────────────────────────────
1. The radar is TEN MINUTES BEHIND. Products carry a nominal time and appear
   about ten minutes later — verified against the live service. Every message
   therefore states the time of the measurement rather than the time of sending,
   and when the field's motion is measurable the geometry is advected forward to
   compensate. Reporting a ten-minute-old picture as if it were live is the
   easiest way to lose the user's trust the first time they look out of a window.

2. Rain fields do not always have a measurable velocity. Phase correlation is
   gated on peak-to-sidelobe ratio precisely so it refuses to invent one, and on
   scattered summer convection it refuses often. That is not a degraded mode to
   apologise for, it is the honest answer — and it is why the inherited CBDR
   verdict stays as the fallback rather than being deleted. CBDR needs no velocity
   at all: it reads the rotation of a relative bearing, which a moving observer
   supplies on its own.

Origin
──────
`position_id` selects where the radar is centred, exactly as in the storm front:
empty means the configured coordinates, otherwise the monitor follows that
position and nothing else. With no usable fix it goes blind and freezes rather
than watching a place the user has left.
"""

import asyncio
import html
import json
import logging
import math
import os
import time
from datetime import datetime, time as time_t
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .geo import direction_label, distance_km, offset_km
from .position import position_manager
from .position_core import MAX_PLAUSIBLE_KMH
from .radar import PRODUCT_HAIL, PRODUCT_RAIN, radar_feed
from .radar_core import (
    Encounter, build_rain_frame, coverage_fraction, cpa, field_motion,
    intensity_label, peak_in_disc, rain_points, sample, velocity_components,
)
from .snapshot import Snapshot, describe_origin, preview_alert
from .storm_front_core import (
    CBDR_CLOSING_KM, CBDR_GRAZING_KM, EVENT_IDLE, OBSERVE_FACTOR,
    POLL_INTERVAL_SEC, TRACK_CLOSING, TRACK_GRAZING, TRACK_UNKNOWN,
    ClearAlert, RingAlert, StormFrontTracker,
    angle_delta, clamp_radius, clamp_ring_count, ring_edges,
)

_LOGGER = logging.getLogger(__name__)

STATE_PATH = "/data/rain_front_state.json"
STATE_MAX_AGE_SEC = 1800

QUIET_OVERRIDE_FRACTION = 0.40
CHART_TIMEOUT_SEC = 20.0

EVENT_STALE_FACTOR = 3.0
ORIGIN_JUMP_KMH = MAX_PLAUSIBLE_KMH
HEADING_TOLERANCE_DEG = 45.0

# What counts as rain worth a message, in mm/h. The storm front has nothing
# tunable because a discharge is a discharge; rain is a continuum, and whether
# drizzle deserves a notification is a genuine preference rather than a threshold
# to be calibrated away. Below 0.1 the product is mostly measurement noise.
DEFAULT_MIN_MMH = 1.0
MIN_MMH_FLOOR = 0.1
MIN_MMH_CEILING = 50.0

# Below this share of the observation disc actually seen by the radar network the
# monitor is not watching what it claims to watch. Announcing calm over a blind
# spot is the failure this refuses.
MIN_COVERAGE_FRACTION = 0.4

# Hail probability, in percent, worth adding a line for.
HAIL_ALERT_PERCENT = 30.0

# The field is only advected when the motion estimate passed its own gate, so this
# is a bound against a pathological age, not against a bad velocity.
MAX_ADVECTION_SEC = 1800.0


# ── Persistent state ──────────────────────────────────────────────────────────

def _load_state_file() -> dict:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    except OSError as e:
        _LOGGER.warning("[RainFront] cannot read %s: %s", STATE_PATH, e)
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
        _LOGGER.warning("[RainFront] cannot write %s: %s", STATE_PATH, e)


# ── Tracker ───────────────────────────────────────────────────────────────────

class RainFrontTracker(StormFrontTracker):
    """The storm front tracker with a measured verdict in place of an inferred one.

    Everything about rings, events and message bounds is inherited untouched. The
    single override is `track_verdict`, and only because a radar field offers
    something a scatter of discharges cannot: its own velocity.

    CBDR asks "is the relative bearing holding steady while the range closes",
    which takes a quarter of an hour of history and yields a category. With two
    velocity vectors the same question is closed-form — closest point of approach,
    in minutes and kilometres. When the field's motion is not measurable the
    override steps aside and the inherited CBDR answers, which is the common case
    on scattered convection rather than a rare one.
    """

    def __init__(self, radius_km: float, ring_count: int):
        super().__init__(radius_km, ring_count)
        self._field = None                  # FieldMotion | None
        self._own = (0.0, 0.0)              # observer velocity, (east, north) km/h
        self.last_encounter: Encounter | None = None

    def set_motion(self, field, own_east_kmh: float = 0.0,
                   own_north_kmh: float = 0.0) -> None:
        self._field = field
        self._own = (own_east_kmh, own_north_kmh)

    def track_verdict(self, now: float, bearing: float,
                      front_km: float) -> tuple[str, float | None, bool]:
        self.last_encounter = None
        if self._field is not None:
            encounter = cpa(front_km, bearing,
                            self._field.east_kmh, self._field.north_kmh,
                            self._own[0], self._own[1])
            # A closest approach already in the past cannot explain a ring that is
            # descending; the inherited reading is the safer answer there.
            if encounter is not None and encounter.approaching:
                self.last_encounter = encounter
                if encounter.miss_km <= CBDR_CLOSING_KM:
                    return TRACK_CLOSING, None, False
                if encounter.miss_km >= CBDR_GRAZING_KM:
                    return TRACK_GRAZING, encounter.miss_bearing_deg, False
                # The same deliberate dead band the storm front uses: between the
                # two thresholds the honest answer is that it is too close to call.
                return TRACK_UNKNOWN, None, False
        return super().track_verdict(now, bearing, front_km)


# ── Monitor ───────────────────────────────────────────────────────────────────

class RainFrontLiveMonitor:
    """One live monitor entry of type 'rain_front'."""

    def __init__(self, cfg: dict, telegram_send_fn, tz_name: str = "UTC"):
        self.monitor_id = cfg["id"]
        self.name       = cfg.get("name", "Rain front")
        self.location   = cfg.get("location", "")
        self.language   = cfg.get("language", "it")
        self.tz_name    = tz_name
        self._send      = telegram_send_fn

        raw_radius = cfg.get("radius_km", 30)
        self.radius_km  = clamp_radius(raw_radius)
        if abs(float(raw_radius or 0) - self.radius_km) > 0.01:
            _LOGGER.warning("[RainFront] '%s' radius %s km clamped to %.0f km",
                            self.name, raw_radius, self.radius_km)
        self.ring_count = clamp_ring_count(cfg.get("ring_count", 4))
        self.edges      = ring_edges(self.radius_km, self.ring_count)
        self.observe_radius_km = self.radius_km * OBSERVE_FACTOR

        self.latitude   = float(cfg.get("latitude", 0) or 0)
        self.longitude  = float(cfg.get("longitude", 0) or 0)
        self.position_id = (cfg.get("position_id") or "").strip()
        self.min_mmh    = _clamp_min_mmh(cfg.get("min_mmh"))
        self.hail       = bool(cfg.get("hail", False))
        self._chart     = bool(cfg.get("chart", True))
        self._quiet_start = (cfg.get("quiet_start") or "").strip()
        self._quiet_end   = (cfg.get("quiet_end") or "").strip()

        self._fix = None                       # PositionState | None
        self._field = None                     # FieldMotion | None
        self._last_origin: tuple[float, float] | None = None
        self._last_origin_at = 0.0
        self._last_discontinuity: int | None = None
        self._blind_since = 0.0
        self._blind_reason = ""
        self._grid_t = 0.0

        self._tracker = RainFrontTracker(self.radius_km, self.ring_count)
        self._poll_task: asyncio.Task | None = None
        self._products = (PRODUCT_RAIN, PRODUCT_HAIL) if self.hail else (PRODUCT_RAIN,)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._poll_task and not self._poll_task.done():
            return
        self._restore_state()
        radar_feed.acquire(*self._products)
        self._poll_task = asyncio.create_task(
            self._poll_loop(), name=f"rain_front_poll:{self.monitor_id}"
        )
        print(f"[RainFront] '{self.name}' started (radius={self.radius_km:.0f}km, "
              f"rings={self.ring_count}, edges={[round(e) for e in self.edges]}, "
              f"min={self.min_mmh:g}mm/h, hail={self.hail}, "
              f"origin={self.position_id or 'fixed'})")

    def stop(self) -> None:
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
        radar_feed.release(*self._products)
        print(f"[RainFront] '{self.name}' stopped")

    async def aclose(self) -> None:
        task = self._poll_task
        if task and not task.done():
            task.cancel()
        radar_feed.release(*self._products)
        if task:
            await asyncio.gather(task, return_exceptions=True)

    def is_running(self) -> bool:
        return self._poll_task is not None and not self._poll_task.done()

    def status(self) -> str:
        if not self.is_running():
            return "stopped"
        if self._blind_since:
            return "degraded"
        return radar_feed.status()

    # ── State persistence ─────────────────────────────────────────────────────

    def _restore_state(self) -> None:
        entry = _load_state_file().get(self.monitor_id)
        if not entry:
            return
        if time.time() - float(entry.get("updated_at", 0)) > STATE_MAX_AGE_SEC:
            _LOGGER.info("[RainFront] '%s' saved state too old — starting clean",
                         self.name)
            return
        self._tracker = RainFrontTracker.from_dict(
            self.radius_km, self.ring_count, entry.get("tracker"))

    def _save_state(self) -> None:
        _save_state_entry(self.monitor_id, {
            "updated_at": time.time(),
            "tracker": self._tracker.to_dict(),
        })

    # ── Poll loop ─────────────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        try:
            await self._tick(notify=False)
        except asyncio.CancelledError:
            return
        except Exception as e:
            _LOGGER.error("[RainFront] '%s' first tick failed: %s", self.name, e)

        while True:
            try:
                await asyncio.sleep(POLL_INTERVAL_SEC)
                await self._tick(notify=True)
            except asyncio.CancelledError:
                return
            except Exception as e:
                _LOGGER.error("[RainFront] '%s' poll error: %s", self.name, e)

    # ── Origin ────────────────────────────────────────────────────────────────

    def _resolve_origin(self, now: float) -> tuple[float, float] | None:
        """Where the disc is centred this poll, or None if that is unknown.

        Identical in intent to the storm front's: following a position there is no
        fallback to the configured point, because watching the house while the
        user is elsewhere answers a different question without saying so.
        """
        if not self.position_id:
            return (self.latitude, self.longitude)

        budget = None
        if self._tracker.event_state != EVENT_IDLE:
            budget = position_manager.max_age_sec(self.position_id) * EVENT_STALE_FACTOR

        state = position_manager.usable(self.position_id, now, max_age_sec=budget)
        if state is None:
            self._fix = None
            return None

        self._fix = state
        return state.lat, state.lon

    def _origin_jumped(self, origin: tuple[float, float], now: float) -> bool:
        if self._last_origin is None:
            return False
        if self._fix is not None:
            seen = self._fix.discontinuity
            jumped = (self._last_discontinuity is not None
                      and seen != self._last_discontinuity)
            self._last_discontinuity = seen
            if jumped:
                return True
        elapsed = max(1.0, now - self._last_origin_at)
        moved = distance_km(*self._last_origin, *origin)
        return moved / (elapsed / 3600.0) > ORIGIN_JUMP_KMH

    # ── Poll ──────────────────────────────────────────────────────────────────

    async def _tick(self, notify: bool) -> None:
        now = time.time()

        origin = self._resolve_origin(now)
        if origin is None:
            self._go_blind(now, "position")
            return

        grid = radar_feed.latest(PRODUCT_RAIN)
        if grid is None or not radar_feed.feed_ok(PRODUCT_RAIN, now):
            self._go_blind(now, "radar")
            return

        coverage = coverage_fraction(grid, origin, self.observe_radius_km)
        if coverage < MIN_COVERAGE_FRACTION:
            self._go_blind(now, "coverage")
            return

        if self._blind_since:
            _LOGGER.info("[RainFront] '%s' recovered from '%s' after %.0f min",
                         self.name, self._blind_reason,
                         (now - self._blind_since) / 60.0)
            self._blind_since = 0.0
            self._blind_reason = ""
            self._tracker.reset_geometry_history()
            self._last_origin = None

        if self.position_id and self._origin_jumped(origin, now):
            _LOGGER.info("[RainFront] '%s' origin discontinuity — geometry history "
                         "reset (event left open)", self.name)
            self._tracker.reset_geometry_history()
        self._last_origin, self._last_origin_at = origin, now
        self._grid_t = grid.t

        # Motion first: it is what lets the ten-minute-old picture be corrected.
        frames = radar_feed.frames(PRODUCT_RAIN)
        self._field = (field_motion(frames[0], frames[1], origin)
                       if len(frames) >= 2 else None)

        effective = self._advect(origin, grid, now)
        points = rain_points(grid, effective, self.observe_radius_km, self.min_mmh)
        frame = build_rain_frame(points, effective, now,
                                 self.radius_km, self.observe_radius_km)

        own_east, own_north = self._own_velocity()
        self._tracker.set_motion(self._field, own_east, own_north)
        alert = self._tracker.evaluate(frame, now, feed_ok=True,
                                       connected_for=radar_feed.connected_for(now))

        _LOGGER.info("[RainFront] %s | %s cov=%.2f age=%.0fs %s", self.name,
                     self._tracker.debug_line(frame), coverage, now - grid.t,
                     self._motion_debug())

        if alert is not None and notify:
            peak = peak_in_disc(grid, effective, self.radius_km)
            hail = self._hail_at(effective, frame)
            await self._dispatch(alert, frame, points, now,
                                 origin=origin, effective=effective,
                                 peak_mmh=peak, hail_percent=hail)

    # ── Snapshot ──────────────────────────────────────────────────────────────

    async def snapshot(self, now: float, *, grid=None) -> Snapshot:
        """What this monitor perceives right now — perception without decision.

        Deliberately never calls `self._tracker.evaluate()` or `.commit()`. The
        bounded-message invariant rests on `notified_ring`, so a diagnostic that
        advanced it would silence the real alert half an hour later, and one that
        opened an event would let a single band emit a second full ladder. Both
        are silent failures that would surface only during weather. The tracker
        is read here and nowhere written; a test asserts its state is identical
        before and after.

        `grid` lets the caller inject a raster fetched on demand, which is what
        makes the command work on a monitor that is not running.
        """
        origin_info = describe_origin(self, now)
        common = dict(
            monitor_id=self.monitor_id, name=self.name, kind="rain",
            language=self.language, tz_name=self.tz_name,
            status=self.status(),
            running=self.is_running(), origin=origin_info,
            event_open=self._tracker.event_state != EVENT_IDLE,
            notified_ring=self._tracker.notified_ring,
            ring_count=self.ring_count, radius_km=self.radius_km,
            one_shot=grid is not None,
        )
        if not origin_info.usable:
            return Snapshot(blind_reason=origin_info.reason or "position unusable",
                            **common)

        origin = (origin_info.lat, origin_info.lon)
        one_shot = grid is not None
        grid = grid or radar_feed.latest(PRODUCT_RAIN)
        if grid is None:
            return Snapshot(blind_reason="no radar image available yet", **common)

        age = now - grid.t
        coverage = coverage_fraction(grid, origin, self.observe_radius_km)
        common.update(radar_t=grid.t, radar_age_sec=age, coverage=coverage)
        if coverage < MIN_COVERAGE_FRACTION:
            return Snapshot(
                blind_reason=(f"only {coverage:.0%} of the watched area is visible "
                              f"to the radar network"), **common)

        # A raster fetched on demand comes alone, so there is no pair to correlate
        # and no drift to report — said plainly rather than silently skipped.
        frames = () if one_shot else radar_feed.frames(PRODUCT_RAIN)
        field = (field_motion(frames[0], frames[1], origin)
                 if len(frames) >= 2 else None)

        effective = origin
        if field is not None and 0 < age <= MAX_ADVECTION_SEC:
            hours = age / 3600.0
            effective = offset_km(origin[0], origin[1],
                                  -field.north_kmh * hours, -field.east_kmh * hours)

        points = rain_points(grid, effective, self.observe_radius_km, self.min_mmh)
        frame = build_rain_frame(points, effective, now,
                                 self.radius_km, self.observe_radius_km)
        alert = preview_alert(frame, self.edges, self.ring_count)

        encounter = None
        if field is not None and frame.dominant is not None:
            own_east, own_north = _velocity_of(origin_info)
            encounter = cpa(frame.dominant.front_km,
                            frame.track_bearing if frame.track_bearing is not None
                            else frame.dominant.bearing_deg,
                            field.east_kmh, field.north_kmh, own_east, own_north)
            if encounter is not None and not encounter.approaching:
                encounter = None

        picture = await self._render_chart_bytes(grid, effective, now, alert, field,
                                                 encounter)
        return Snapshot(
            front_km=frame.dominant.front_km if frame.dominant else None,
            front_bearing_deg=frame.dominant.bearing_deg if frame.dominant else None,
            activity=frame.strikes_in_radius,
            peak_mmh=peak_in_disc(grid, effective, self.radius_km),
            field_speed_kmh=field.speed_kmh if field else None,
            field_bearing_deg=field.bearing_deg if field else None,
            encounter_minutes=encounter.minutes if encounter else None,
            encounter_miss_km=encounter.miss_km if encounter else None,
            picture=picture, **common)

    async def _render_chart_bytes(self, grid, origin, now, alert, field, encounter):
        """Chart rendering for a snapshot, with the alert path's own guarantee:
        a picture that fails must never take the message down with it."""
        if not self._chart:
            return None
        try:
            from .rain_front_chart import render_rain_radar
            return await asyncio.wait_for(
                asyncio.to_thread(
                    render_rain_radar, grid, origin, now, alert,
                    radius_km=self.radius_km,
                    observe_radius_km=self.observe_radius_km,
                    edges=self.edges, motion=field, encounter=encounter,
                    location=self._plain_location(), lang=self.language,
                    tz=self._tz()),
                timeout=CHART_TIMEOUT_SEC)
        except Exception as e:
            _LOGGER.warning("[RainFront] '%s' snapshot chart failed: %s", self.name, e)
            return None

    def _own_velocity(self) -> tuple[float, float]:
        fix = self._fix
        if fix is None or not fix.moving or fix.course_deg is None:
            return 0.0, 0.0
        return velocity_components(fix.speed_kmh, fix.course_deg)

    def _advect(self, origin: tuple[float, float], grid, now: float
                ) -> tuple[float, float]:
        """Compensate for the age of the raster by displacing the ORIGIN.

        Every rain feature has travelled `v · age` since it was measured, so the
        geometry we want is against `p + d`. Shifting the observer by `-d` gives
        exactly the same relative vectors for one arithmetic operation instead of
        one per pixel.

        Only ever applied when the motion estimate cleared its own peak-to-sidelobe
        gate. Without a measured velocity the picture is simply reported as being
        as old as it is.
        """
        age = now - grid.t
        if self._field is None or age <= 0 or age > MAX_ADVECTION_SEC:
            return origin
        hours = age / 3600.0
        return offset_km(origin[0], origin[1],
                         -self._field.north_kmh * hours,
                         -self._field.east_kmh * hours)

    def _hail_at(self, origin: tuple[float, float], frame) -> float | None:
        if not self.hail or frame.dominant is None:
            return None
        grid = radar_feed.latest(PRODUCT_HAIL)
        if grid is None:
            return None
        rad = math.radians(frame.dominant.bearing_deg)
        spot = offset_km(origin[0], origin[1],
                         frame.dominant.front_km * math.cos(rad),
                         frame.dominant.front_km * math.sin(rad))
        return sample(grid, spot[0], spot[1])

    def _go_blind(self, now: float, reason: str) -> None:
        """Perceive nothing until the missing input comes back.

        Three different things can blind this monitor — an unusable position, a
        stale raster, or a disc the radar network cannot see into — and all three
        are the same epistemic problem the storm front already solves: not knowing
        is not the same as nothing happening, and only one of them may produce an
        all-clear. `evaluate(feed_ok=False)` is that refusal, already tested.
        """
        if not self._blind_since or self._blind_reason != reason:
            self._blind_since = self._blind_since or now
            self._blind_reason = reason
            _LOGGER.info("[RainFront] '%s' blind (%s) — frozen, no alerts and no "
                         "false all-clear", self.name, reason)
        self._field = None
        self._tracker.set_motion(None)
        self._tracker.evaluate(
            build_rain_frame([], (0.0, 0.0), now,
                             self.radius_km, self.observe_radius_km),
            now, feed_ok=False)

    def _motion_debug(self) -> str:
        bits = []
        if self._field is not None:
            bits.append(f"field={self._field.speed_kmh:.0f}km/h@"
                        f"{self._field.bearing_deg:.0f}° psr={self._field.psr:.1f}")
        else:
            bits.append("field=—")
        if self.position_id and self._fix is not None:
            speed = self._fix.speed_kmh
            bits.append(f"own={'—' if speed is None else f'{speed:.0f}km/h'}")
        return " ".join(bits)

    # ── Dispatch ──────────────────────────────────────────────────────────────

    async def _dispatch(self, alert, frame, points, now: float, *,
                        origin: tuple[float, float],
                        effective: tuple[float, float],
                        peak_mmh: float | None,
                        hail_percent: float | None) -> None:
        if self._is_silenceable(alert) and self._in_quiet_hours():
            _LOGGER.info("[RainFront] '%s' alert suppressed by quiet hours", self.name)
            self._tracker.commit(alert, now)
            self._save_state()
            return

        text = self._format(alert, peak_mmh, hail_percent, now)
        photo = await self._render_chart(alert, frame, points, now, effective)

        try:
            ok = await self._send(text, photo=photo) if photo else await self._send(text)
        except Exception as e:
            _LOGGER.error("[RainFront] '%s' send error: %s", self.name, e)
            return
        if not ok:
            _LOGGER.warning("[RainFront] '%s' alert NOT delivered — state held, "
                            "retry next poll", self.name)
            return

        self._tracker.commit(alert, now)
        self._save_state()
        _LOGGER.info("[RainFront] %s | committed %s", self.name,
                     f"ring {alert.ring}" if isinstance(alert, RingAlert) else "clear")

    async def _render_chart(self, alert, frame, points, now: float,
                            effective: tuple[float, float]) -> bytes | None:
        """Render off the event loop. Any failure degrades to text — a picture
        must never be able to stop an alert."""
        if not self._chart or not isinstance(alert, RingAlert):
            return None
        grid = radar_feed.latest(PRODUCT_RAIN)
        if grid is None:
            return None
        try:
            from .rain_front_chart import render_rain_radar
            return await asyncio.wait_for(
                asyncio.to_thread(
                    render_rain_radar, grid, effective, now, alert,
                    radius_km=self.radius_km,
                    observe_radius_km=self.observe_radius_km,
                    edges=self.edges, motion=self._field,
                    encounter=self._tracker.last_encounter,
                    location=self._plain_location(),
                    lang=self.language, tz=self._tz(),
                ),
                timeout=CHART_TIMEOUT_SEC,
            )
        except Exception as e:
            _LOGGER.warning("[RainFront] '%s' chart failed (%s) — sending text only",
                            self.name, e)
            return None

    def _is_silenceable(self, alert) -> bool:
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
            return now >= start or now < end
        except (ValueError, AttributeError):
            return False

    # ── Formatting ────────────────────────────────────────────────────────────

    _RING_HEADS_IT = ["🌧️ <b>Pioggia nel raggio", "🌧️ <b>Pioggia più vicina",
                      "🟠 <b>Pioggia vicina", "🔵 <b>Pioggia su di te"]
    _RING_HEADS_EN = ["🌧️ <b>Rain within range", "🌧️ <b>Rain closing in",
                      "🟠 <b>Rain nearby", "🔵 <b>Rain overhead"]

    def _format(self, alert, peak_mmh: float | None,
                hail_percent: float | None, now: float) -> str:
        if isinstance(alert, ClearAlert):
            return self._fmt_clear(alert, now)
        return self._fmt_ring(alert, peak_mmh, hail_percent, now)

    def _head(self, alert: RingAlert) -> str:
        heads = self._RING_HEADS_IT if self.language == "it" else self._RING_HEADS_EN
        if alert.is_innermost:
            head = heads[-1]
        else:
            head = heads[min(alert.ring - 1, len(heads) - 2)]
        return f"{head} — {self._loc()}</b>"

    def _track_line(self, alert: RingAlert) -> str:
        """The verdict, worded to say WHICH instrument produced it.

        A measured closest approach carries a time and a miss distance; an
        inherited CBDR reading carries neither and must not be dressed up as if
        it did.
        """
        it = self.language == "it"
        encounter = self._tracker.last_encounter

        if encounter is not None:
            minutes = max(0, round(encounter.minutes))
            if alert.track == TRACK_CLOSING:
                return (f"🧭 Rotta d'incontro: ti raggiunge fra {minutes} min" if it
                        else f"🧭 On a collision course: reaches you in {minutes} min")
            if alert.track == TRACK_GRAZING:
                side = direction_label(alert.pass_bearing_deg
                                       if alert.pass_bearing_deg is not None
                                       else encounter.miss_bearing_deg, self.language)
                return (f"🧭 Ti sfiora: passa a {side} a "
                        f"{encounter.miss_km:.0f} km, fra {minutes} min" if it else
                        f"🧭 Glancing pass: {encounter.miss_km:.0f} km to the "
                        f"{side}, in {minutes} min")
            return (f"🧭 Incontro fra {minutes} min, ma di quanto ti manchi "
                    f"non è ancora chiaro" if it else
                    f"🧭 Closest approach in {minutes} min, by an amount still "
                    f"too close to call")

        if alert.is_innermost and alert.track != TRACK_GRAZING:
            return "🧭 Sei sotto la pioggia" if it else "🧭 You are under the rain"
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
        """The user's own movement. Explains the verdict, never competes with it:
        the reading above was already computed from this same velocity."""
        fix = self._fix
        if fix is None or not fix.moving or fix.course_deg is None:
            return None
        it = self.language == "it"
        heading = direction_label(fix.course_deg, self.language)
        toward = abs(angle_delta(fix.course_deg,
                                 alert.bearing_deg)) <= HEADING_TOLERANCE_DEG
        if toward:
            return (f"🚗 Ti stai dirigendo verso la pioggia a {fix.speed_kmh:.0f} km/h"
                    if it else
                    f"🚗 You are heading towards the rain at {fix.speed_kmh:.0f} km/h")
        return (f"🚗 In movimento a {fix.speed_kmh:.0f} km/h verso {heading}" if it
                else f"🚗 Moving at {fix.speed_kmh:.0f} km/h towards {heading}")

    def _field_line(self) -> str:
        it = self.language == "it"
        if self._field is None:
            return ("🌬️ Movimento della pioggia non misurabile" if it
                    else "🌬️ Rain movement not measurable")
        if self._field.speed_kmh < 1.0:
            return "🌬️ Pioggia stazionaria" if it else "🌬️ Rain is stationary"
        heading = direction_label(self._field.bearing_deg, self.language)
        return (f"🌬️ La pioggia si muove verso {heading} a "
                f"{self._field.speed_kmh:.0f} km/h" if it else
                f"🌬️ Rain moving {heading} at {self._field.speed_kmh:.0f} km/h")

    def _radar_line(self, now: float) -> str:
        """The age of the measurement, always. The product is published about ten
        minutes late, and a user who looks out of the window has to be able to
        reconcile what they see with what they were told."""
        it = self.language == "it"
        if not self._grid_t:
            return ""
        clock = datetime.fromtimestamp(self._grid_t, self._tz()).strftime("%H:%M")
        age = max(0, round((now - self._grid_t) / 60.0))
        return (f"📡 Radar delle {clock} ({age} min fa)" if it
                else f"📡 Radar at {clock} ({age} min ago)")

    def _fmt_ring(self, alert: RingAlert, peak_mmh: float | None,
                  hail_percent: float | None, now: float) -> str:
        it = self.language == "it"
        heading = direction_label(alert.bearing_deg, self.language)
        lines = [self._head(alert)]
        lines.append(
            (f"📍 Fronte a <b>{alert.front_km:.0f} km</b> a {heading} "
             f"({alert.bearing_deg:.0f}°)") if it else
            (f"📍 Front at <b>{alert.front_km:.0f} km</b> to {heading} "
             f"({alert.bearing_deg:.0f}°)"))
        if peak_mmh is not None:
            lines.append(
                (f"🌧️ Intensità massima {peak_mmh:.1f} mm/h "
                 f"({intensity_label(peak_mmh, self.language)})") if it else
                (f"🌧️ Peak intensity {peak_mmh:.1f} mm/h "
                 f"({intensity_label(peak_mmh, self.language)})"))
        if hail_percent is not None and hail_percent >= HAIL_ALERT_PERCENT:
            lines.append((f"🧊 Probabilità di grandine {hail_percent:.0f}%" if it
                          else f"🧊 Hail probability {hail_percent:.0f}%"))
        lines.append(
            (f"🎯 Anello {alert.ring}/{alert.ring_count} · entro "
             f"{alert.ring_edge_km:.0f} km") if it else
            (f"🎯 Ring {alert.ring}/{alert.ring_count} · within "
             f"{alert.ring_edge_km:.0f} km"))
        lines.append(self._track_line(alert))
        lines.append(self._field_line())
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
                 f"in {minutes:.0f} min"))

        lines.append(
            (f"🔢 {alert.strikes} km² di pioggia nel settore {heading}" if it
             else f"🔢 {alert.strikes} km² of rain in the {heading} sector"))
        if alert.secondary:
            others = ", ".join(
                f"{direction_label(s.bearing_deg, self.language)} "
                f"a {s.front_km:.0f} km" if it else
                f"{direction_label(s.bearing_deg, self.language)} at {s.front_km:.0f} km"
                for s in alert.secondary)
            lines.append((f"➕ Altra pioggia: {others}" if it
                          else f"➕ Other rain: {others}"))
        lines.append(self._radar_line(now))
        lines.append(f"🕐 {self._now_str()}")
        return "\n".join(line for line in lines if line)

    def _fmt_clear(self, alert: ClearAlert, now: float) -> str:
        it = self.language == "it"
        lines = [(f"✅ <b>Pioggia cessata — {self._loc()}</b>" if it
                  else f"✅ <b>Rain cleared — {self._loc()}</b>")]
        quiet_min = alert.quiet_sec / 60.0
        lines.append(
            (f"🔇 Niente pioggia entro {alert.radius_km:.0f} km "
             f"da {quiet_min:.0f} min") if it else
            (f"🔇 No rain within {alert.radius_km:.0f} km "
             f"for {quiet_min:.0f} min"))
        if alert.closest_km is not None:
            when = datetime.fromtimestamp(alert.closest_at, self._tz()).strftime("%H:%M")
            lines.append(
                (f"📉 Massimo avvicinamento: {alert.closest_km:.0f} km "
                 f"(anello {alert.closest_ring}/{alert.ring_count}) alle {when}") if it
                else
                (f"📉 Closest approach: {alert.closest_km:.0f} km "
                 f"(ring {alert.closest_ring}/{alert.ring_count}) at {when}"))
        lines.append(self._radar_line(now))
        lines.append(f"🕐 {self._now_str()}")
        return "\n".join(line for line in lines if line)

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


# ── Helpers ───────────────────────────────────────────────────────────────────

def _velocity_of(origin) -> tuple[float, float]:
    """Observer velocity from an OriginInfo, as (east, north) km/h."""
    if not origin.moving or origin.course_deg is None or origin.speed_kmh is None:
        return 0.0, 0.0
    return velocity_components(origin.speed_kmh, origin.course_deg)


def _clamp_min_mmh(value) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return DEFAULT_MIN_MMH
    if parsed <= 0:
        return DEFAULT_MIN_MMH
    return max(MIN_MMH_FLOOR, min(MIN_MMH_CEILING, parsed))


# ── Manager ───────────────────────────────────────────────────────────────────

_FINGERPRINT_FIELDS = (
    "name", "location", "latitude", "longitude", "radius_km", "ring_count",
    "language", "quiet_start", "quiet_end", "chart", "telegram_bot_id",
    "position_id", "min_mmh", "hail",
)


def _fingerprint(cfg: dict, tz_name: str) -> str:
    return json.dumps([cfg.get(k) for k in _FINGERPRINT_FIELDS] + [tz_name],
                      sort_keys=True, default=str)


class RainFrontMonitorManager:
    """Owns every rain_front monitor instance."""

    def __init__(self):
        self._monitors: dict[str, RainFrontLiveMonitor] = {}
        self._fingerprints: dict[str, str] = {}

    def reload(self, configs: list[dict], make_send_fn, tz_name: str):
        wanted: set[str] = set()
        for cfg in configs:
            if cfg.get("type") != "rain_front" or not cfg.get("enabled"):
                continue
            mid = cfg["id"]
            wanted.add(mid)
            fingerprint = _fingerprint(cfg, tz_name)
            existing = self._monitors.get(mid)
            if (existing and self._fingerprints.get(mid) == fingerprint
                    and existing.is_running()):
                continue
            self._fingerprints[mid] = fingerprint
            replacement = RainFrontLiveMonitor(cfg, make_send_fn(cfg), tz_name)
            self._monitors[mid] = replacement
            self._swap(existing, replacement)

        for mid in list(self._monitors):
            if mid not in wanted:
                self._swap(self._monitors.pop(mid), None)
                self._fingerprints.pop(mid, None)

    @staticmethod
    def _swap(old: "RainFrontLiveMonitor | None",
              new: "RainFrontLiveMonitor | None"):
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

    def get(self, monitor_id: str) -> RainFrontLiveMonitor | None:
        """The running instance, or None. Symmetric with `PositionManager.get`.

        Without it a caller wanting to ask a monitor what it perceives would have
        to reach into `_monitors`, and an accessor is cheaper than a convention
        everyone breaks.
        """
        return self._monitors.get(monitor_id)


rain_front_monitor_manager = RainFrontMonitorManager()


__all__ = [
    "STATE_PATH", "DEFAULT_MIN_MMH", "MIN_COVERAGE_FRACTION", "HAIL_ALERT_PERCENT",
    "RainFrontTracker", "RainFrontLiveMonitor", "RainFrontMonitorManager",
    "rain_front_monitor_manager", "intensity_label",
]
