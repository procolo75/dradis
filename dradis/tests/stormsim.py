"""
tests/stormsim.py
──────────────────
Storm simulator for the storm front monitor tests.

Generates plausible strike streams — cells that move in a straight line, emit at
a given rate and scatter their discharges around their centre — and drives them
through the real perception + decision pipeline at the real poll cadence. No
mocking of the algorithm: `run()` calls `build_frame` and `StormFrontTracker`
exactly as the live monitor does.

Everything is expressed in kilometres north/east of the origin, which is far
easier to reason about than latitudes, and converted to real coordinates through
the same `offset_km` the chart uses.
"""

import math
import random
from dataclasses import dataclass, field

from dradis.live_monitors.geo import offset_km
from dradis.live_monitors.storm_front_core import (
    OBSERVE_FACTOR, POLL_INTERVAL_SEC, WINDOW_MIN,
    StormFrontTracker, build_frame,
)

BASE_TS = 1_700_000_000.0      # a fixed epoch keeps failures reproducible
ORIGIN  = (40.85, 14.27)


@dataclass
class Storm:
    """A cell moving in a straight line at constant speed.

    north_km/east_km are its centre at t=0; v_north/v_east are km/h. `spread_km`
    is the standard deviation of the discharge scatter around the centre, so the
    front sits roughly one spread nearer than the centre.
    """
    north_km:     float
    east_km:      float
    v_north:      float = 0.0
    v_east:       float = 0.0
    rate_per_min: float = 12.0
    spread_km:    float = 6.0
    active_from:  float = 0.0            # minutes
    active_until: float = float("inf")   # minutes

    def center_at(self, minutes: float) -> tuple[float, float]:
        hours = minutes / 60.0
        return (self.north_km + self.v_north * hours,
                self.east_km + self.v_east * hours)

    def emit(self, rng: random.Random, origin: tuple[float, float],
             t0_min: float, t1_min: float) -> list[tuple[float, float, float]]:
        """Strikes discharged in the (t0, t1] minute interval."""
        if t1_min <= self.active_from or t0_min >= self.active_until:
            return []
        span = t1_min - t0_min
        count = int(round(self.rate_per_min * span))
        out = []
        for _ in range(count):
            when = t0_min + rng.random() * span
            if not (self.active_from <= when < self.active_until):
                continue
            north, east = self.center_at(when)
            north += rng.gauss(0.0, self.spread_km)
            east  += rng.gauss(0.0, self.spread_km)
            lat, lon = offset_km(origin[0], origin[1], north, east)
            out.append((BASE_TS + when * 60.0, lat, lon))
        return out


@dataclass
class Result:
    alerts:        list = field(default_factory=list)
    alert_minutes: list = field(default_factory=list)   # parallel to `alerts`
    trace:         list = field(default_factory=list)
    tracker: StormFrontTracker | None = None

    def rings(self) -> list[int]:
        from dradis.live_monitors.storm_front_core import RingAlert
        return [a.ring for a in self.alerts if isinstance(a, RingAlert)]

    def ring_alerts(self) -> list:
        from dradis.live_monitors.storm_front_core import RingAlert
        return [a for a in self.alerts if isinstance(a, RingAlert)]

    def clear_alerts(self) -> list:
        from dradis.live_monitors.storm_front_core import ClearAlert
        return [a for a in self.alerts if isinstance(a, ClearAlert)]


def run(storms, *, origin=ORIGIN, observer=None, radius_km=30.0, ring_count=4,
        duration_min=180.0, seed=42, feed_ok_fn=None, connected_for_fn=None,
        commit=True, tracker=None) -> Result:
    """Drive `storms` through the real pipeline for `duration_min`.

    feed_ok_fn(minutes) -> bool simulates an MQTT outage: while it returns False
    no strikes reach the buffer, exactly as a dead socket would behave.

    `origin` and `observer` are deliberately two different things, and conflating
    them would silently break every scenario. `origin` is the FIELD's reference
    frame: storms are defined in km north/east of it, so it must stay fixed or the
    weather would follow the user around. `observer` is where the user is —
    `None` (the default) means "standing at the origin", which is what every
    scenario written before the origin could move means. Pass a
    `callable(minutes) -> (lat, lon)` to drive them somewhere.
    """
    observer_at = (observer if callable(observer)
                   else (lambda _m, _fixed=(observer or origin): _fixed))
    rng = random.Random(seed)
    tracker = tracker or StormFrontTracker(radius_km, ring_count)
    observe = tracker.observe_radius_km
    window_sec = WINDOW_MIN * 60.0
    buffer: list[tuple[float, float, float]] = []
    result = Result(tracker=tracker)

    steps = int(duration_min * 60.0 / POLL_INTERVAL_SEC)
    poll_min = POLL_INTERVAL_SEC / 60.0

    for i in range(1, steps + 1):
        minutes = i * poll_min
        now = BASE_TS + minutes * 60.0
        feed_ok = feed_ok_fn(minutes) if feed_ok_fn else True

        if feed_ok:
            for storm in storms:
                buffer.extend(storm.emit(rng, origin, minutes - poll_min, minutes))
        buffer = [s for s in buffer if s[0] >= now - window_sec]

        # The buffer holds absolute coordinates, exactly as the live feed does, so
        # moving the observer needs nothing more than measuring from a new point.
        here = observer_at(minutes)
        frame = build_frame(buffer, here, now, tracker.radius_km, observe, window_sec)
        if connected_for_fn is not None:
            connected_for = connected_for_fn(minutes)
        else:
            connected_for = 1e9 if feed_ok else 0.0
        alert = tracker.evaluate(frame, now, feed_ok, connected_for)

        result.trace.append({
            "minutes": minutes, "now": now, "frame": frame,
            "ring": tracker.current_ring, "notified": tracker.notified_ring,
            "event": tracker.event_state, "feed_ok": feed_ok, "observer": here,
        })
        if alert is not None:
            result.alerts.append(alert)
            result.alert_minutes.append(minutes)
            if commit:
                tracker.commit(alert, now)

    return result


def head_on(distance_km: float = 48.0, bearing_deg: float = 315.0,
            speed_kmh: float = 55.0, **kw) -> Storm:
    """A storm starting `distance_km` away on `bearing_deg`, moving straight at
    the origin."""
    rad = math.radians(bearing_deg)
    north, east = distance_km * math.cos(rad), distance_km * math.sin(rad)
    return Storm(north_km=north, east_km=east,
                 v_north=-speed_kmh * math.cos(rad),
                 v_east=-speed_kmh * math.sin(rad), **kw)


def crossing(miss_km: float, start_east_km: float = -45.0,
             speed_kmh: float = 55.0, **kw) -> Storm:
    """A storm travelling due east that passes `miss_km` to the north."""
    return Storm(north_km=miss_km, east_km=start_east_km,
                 v_north=0.0, v_east=speed_kmh, **kw)


def parked(north_km: float, east_km: float, **kw) -> Storm:
    """A storm that does not move. Any closing then comes from the OBSERVER, which
    is the only way to isolate own-motion in a scenario."""
    return Storm(north_km=north_km, east_km=east_km, v_north=0.0, v_east=0.0, **kw)


# ── Observers ─────────────────────────────────────────────────────────────────

def driving(bearing_deg: float, speed_kmh: float, origin=ORIGIN, start_min=0.0):
    """An observer leaving `origin` on a constant heading at a constant speed."""
    rad = math.radians(bearing_deg)

    def at(minutes: float) -> tuple[float, float]:
        km = speed_kmh * max(0.0, minutes - start_min) / 60.0
        return offset_km(origin[0], origin[1], km * math.cos(rad), km * math.sin(rad))
    return at


def teleporting(at_min: float, north_km: float, east_km: float, origin=ORIGIN):
    """An observer standing at `origin` who is instantaneously relocated at
    `at_min`. Not a journey — a change of reference frame, which is what a GPS
    glitch or a fallback to the configured point looks like from the inside."""
    def at(minutes: float) -> tuple[float, float]:
        if minutes < at_min:
            return origin
        return offset_km(origin[0], origin[1], north_km, east_km)
    return at
