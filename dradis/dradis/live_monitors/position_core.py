"""
live_monitors/position_core.py
───────────────────────────────
Pure decision core of the dynamic position source. No I/O, no asyncio, no MQTT:
everything here is deterministic given (published values, clock), which makes the
whole thing unit-testable without a broker.

What this module is for
───────────────────────
Home Assistant publishes the phone's coordinates as TWO independent MQTT topics
(one template sensor per component). Turning that stream into "where am I, how
old is that answer, and am I moving" is not a one-liner, and every hard case here
is one that would otherwise corrupt the storm front's geometry:

  · The two components arrive as separate messages. A fix built from a latitude
    published now and a longitude published four minutes ago is a point where
    nobody has ever been.
  · A retained message arrives with no age attached. Treating it as fresh means
    trusting a position from yesterday.
  · GPS jitter republishes a stationary phone forever. Read naively, that is
    motion, and a fabricated course would then be compared against the storm's
    bearing.
  · A single mislocated fix would yank the radar origin hundreds of kilometres,
    which does not merely move the picture — it invalidates the CBDR history,
    because those bearings were measured from somewhere else.

The two rules worth stating
───────────────────────────
R1 · Below the noise floor, say "stationary" — never invent a course. A phone
     sitting on a table drifts a few metres between fixes. That displacement is
     real in the data and meaningless in the world, and at 20 km of lever arm a
     fabricated course is enough to claim the user is driving into a storm.

R2 · A discontinuity must be confirmed before it is believed. This is the same
     rule `storm_front_core` applies to ring descents ("a descent must repeat
     before it is believed") for the same reason: being one publish late costs
     nothing, being wrong moves the observer to a place they have never been.
"""

from dataclasses import dataclass

from .geo import azimuth_deg, distance_km

# ── Pairing ───────────────────────────────────────────────────────────────────
# Latitude and longitude are published by the same HA state_changed batch, so
# they normally land milliseconds apart. Anything beyond this and the two halves
# describe different moments, which would place the observer on a point they
# never occupied.
PAIR_MAX_SKEW_SEC = 30.0

# ── Motion ────────────────────────────────────────────────────────────────────
# The reference fix must be at least this old. Two fixes seconds apart differ
# mostly by GPS noise, so the derived speed would be noise divided by a small
# number — the classic way to manufacture a large wrong answer.
MOTION_MIN_DT_SEC = 60.0
# Below this displacement there is no motion to speak of: 150 m is comfortably
# above the scatter of a stationary consumer GPS and far below anything a moving
# vehicle covers in MOTION_MIN_DT_SEC.
MOTION_MIN_DIST_KM = 0.15
# If the newest fix is older than this, motion is UNKNOWN rather than "whatever
# it was last time". A stationary phone stops publishing (statestream only fires
# on change), so a stale newest fix means "no recent evidence", and the last
# computed speed would otherwise persist forever.
MOTION_MAX_AGE_SEC = 300.0
# History kept for the motion estimate. Longer buys nothing: the estimate only
# ever looks at the newest fix and the newest one older than MOTION_MIN_DT_SEC.
HISTORY_SEC = 900.0
# At or above this the user is travelling rather than walking around the garden.
MOVING_MIN_KMH = 15.0
# Implied speeds above this are not travel, they are a bad fix or a frame change.
MAX_PLAUSIBLE_KMH = 400.0


@dataclass(frozen=True)
class Fix:
    """One paired position report."""
    t:          float
    lat:        float
    lon:        float
    accuracy_m: float | None = None


@dataclass(frozen=True)
class PositionState:
    """What a consumer needs to decide whether to trust and use this position."""
    lat:        float
    lon:        float
    t:          float
    age_sec:    float
    accuracy_m: float | None
    speed_kmh:  float | None      # None = not enough evidence; 0.0 = stationary
    course_deg: float | None      # None whenever speed is None or 0.0
    moving:     bool
    # Incremented every time the history is discarded because the observer
    # jumped. A consumer that stores geometry derived from this position watches
    # this number: when it changes, that geometry describes a different place.
    discontinuity: int


class FixHistory:
    """Accumulates position components and answers "where am I, and am I moving".

    Components are fed in as they arrive (`set_latitude`, `set_longitude`,
    `set_accuracy`); a Fix is formed only once both coordinates are known and
    close enough in time. Everything else is derived on read, so the object holds
    no cached answer that could go stale.
    """

    def __init__(self):
        self._lat: tuple[float, float] | None = None    # (value, t)
        self._lon: tuple[float, float] | None = None
        self._accuracy: float | None = None

        self._fixes: list[Fix] = []
        self._pending_jump: Fix | None = None
        self._discontinuity = 0

    # ── Ingest ────────────────────────────────────────────────────────────────

    def set_latitude(self, value: float, t: float) -> None:
        self._lat = (float(value), float(t))
        self._try_pair()

    def set_longitude(self, value: float, t: float) -> None:
        self._lon = (float(value), float(t))
        self._try_pair()

    def set_accuracy(self, value: float | None) -> None:
        self._accuracy = None if value is None else float(value)

    def _try_pair(self) -> None:
        if self._lat is None or self._lon is None:
            return
        lat, t_lat = self._lat
        lon, t_lon = self._lon
        if abs(t_lat - t_lon) > PAIR_MAX_SKEW_SEC:
            return
        self._offer(Fix(t=max(t_lat, t_lon), lat=lat, lon=lon,
                        accuracy_m=self._accuracy))

    def _offer(self, fix: Fix) -> None:
        """Admit a candidate fix, unless it is an unconfirmed discontinuity.

        Duplicates are deliberately KEPT. A republished identical position is
        positive evidence of standing still, and dropping it would leave the
        newest pair of fixes describing the last time the user actually moved —
        a phone parked in a driveway would report the speed it arrived at,
        forever.
        """
        previous = self._fixes[-1] if self._fixes else None

        if previous is not None and self._is_jump(previous, fix):
            # R2: one wild fix is not a teleport. Hold it and see whether the
            # next report agrees; a genuine move keeps producing fixes near the
            # new place, a glitch does not.
            if (self._pending_jump is not None
                    and not self._is_jump(self._pending_jump, fix)):
                self._fixes = []
                self._pending_jump = None
                self._discontinuity += 1
            else:
                self._pending_jump = fix
                return
        else:
            self._pending_jump = None

        self._fixes.append(fix)
        self._prune(fix.t)

    @staticmethod
    def _is_jump(previous: Fix, fix: Fix) -> bool:
        dt = fix.t - previous.t
        if dt <= 0:
            # Same instant, different place: nothing can travel in zero time.
            return distance_km(previous.lat, previous.lon, fix.lat, fix.lon) > 0.0
        km = distance_km(previous.lat, previous.lon, fix.lat, fix.lon)
        return km / (dt / 3600.0) > MAX_PLAUSIBLE_KMH

    def _prune(self, now: float) -> None:
        cutoff = now - HISTORY_SEC
        kept = [f for f in self._fixes if f.t >= cutoff]
        # Always keep the newest, even if the whole history aged out at once.
        self._fixes = kept or self._fixes[-1:]

    def reset(self) -> None:
        """Drop everything and count it as a discontinuity — used when the source
        is reconfigured, so geometry derived from the old stream is not compared
        against the new one."""
        self._lat = self._lon = None
        self._accuracy = None
        self._fixes = []
        self._pending_jump = None
        self._discontinuity += 1

    # ── Read ──────────────────────────────────────────────────────────────────

    def current(self, now: float) -> PositionState | None:
        if not self._fixes:
            return None
        newest = self._fixes[-1]
        speed, course = self._motion(now)
        return PositionState(
            lat=newest.lat,
            lon=newest.lon,
            t=newest.t,
            age_sec=max(0.0, now - newest.t),
            # A fix keeps the accuracy it was born with; if it has none, the
            # latest reported value fills in. On connect every retained message
            # lands at once and in no particular order, so a coordinate pair can
            # easily beat its own accuracy by milliseconds — without this, the
            # accuracy threshold would silently never apply to the first fix.
            accuracy_m=(newest.accuracy_m if newest.accuracy_m is not None
                        else self._accuracy),
            speed_kmh=speed,
            course_deg=course,
            moving=speed is not None and speed >= MOVING_MIN_KMH,
            discontinuity=self._discontinuity,
        )

    def _motion(self, now: float) -> tuple[float | None, float | None]:
        """(speed_kmh, course_deg).

        `(None, None)` means "no evidence" — too few fixes, or none recent enough
        to say anything about the present. `(0.0, None)` means "stationary", which
        is a real answer: there is evidence, and it says the observer is not
        moving. The distinction matters to the caller, which must not print a
        course in either case but may print "stationary" only in the second.
        """
        if len(self._fixes) < 2:
            return None, None
        newest = self._fixes[-1]
        if now - newest.t > MOTION_MAX_AGE_SEC:
            return None, None

        reference = None
        for fix in reversed(self._fixes[:-1]):
            if newest.t - fix.t >= MOTION_MIN_DT_SEC:
                reference = fix
                break
        if reference is None:
            return None, None

        km = distance_km(reference.lat, reference.lon, newest.lat, newest.lon)
        if km < MOTION_MIN_DIST_KM:
            return 0.0, None                      # R1 — stationary, no course
        hours = (newest.t - reference.t) / 3600.0
        return (km / hours,
                azimuth_deg(reference.lat, reference.lon, newest.lat, newest.lon))

    # ── Diagnostics ───────────────────────────────────────────────────────────

    def fix_count(self) -> int:
        return len(self._fixes)

    def has_pending_jump(self) -> bool:
        return self._pending_jump is not None


__all__ = [
    "PAIR_MAX_SKEW_SEC", "MOTION_MIN_DT_SEC", "MOTION_MIN_DIST_KM",
    "MOTION_MAX_AGE_SEC", "HISTORY_SEC", "MOVING_MIN_KMH", "MAX_PLAUSIBLE_KMH",
    "Fix", "PositionState", "FixHistory",
]
