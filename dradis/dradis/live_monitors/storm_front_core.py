"""
live_monitors/storm_front_core.py
──────────────────────────────────
Pure decision core of the storm front monitor. No I/O, no asyncio, no MQTT, no
matplotlib: everything here is deterministic given (strikes, origin, clock),
which makes the whole algorithm unit-testable.

Why this module exists
──────────────────────
Six generations of storm monitor failed the same way, and the last one failed in
the field with two symptoms that were really one bug: the user could never tell
whether a storm was coming, and the alerts would not stop.

  · The old primary observable was d10 — the 10th percentile of the distances of
    ALL strikes in the radius. That is a function of the count RATIO between
    cells, not of geometry. A very active storm 80 km away dominates the
    percentile; when it dies, d10 collapses to 20 km although nothing moved. It
    is the v2.26 "minimum over an unstable set" error in statistical clothing.
  · v_c was the shift between the centroids of two half-windows — a derivative
    of an already noisy estimator — and the displayed ETA was d10/v_c, a ratio of
    two noisy quantities. Hence numbers that jump around.
  · WARNING latched: leaving it required d10 >= warn_exit_km AND v_c <= 3 km/h,
    with no condition on activity at all. A weak but persistent cell parked
    between the enter and exit thresholds stayed in WARNING for hours, re-alerting
    every 10 minutes.
  · No generation ever computed whether the storm would HIT or pass by. Distance
    and bearing are numerically identical in both cases.

The three principles here
─────────────────────────
P1 · Geometric binning, never labelling.
     (lat, lon) → (sector, distance) is a pure function of the strike and the
     origin. There is no assignment step, no min_samples, no neighbour search, so
     nothing can be RE-labelled between two polls. Consecutive frames differ only
     by strikes that arrived and strikes that aged out. This is the property both
     the DBSCAN generations and the global-percentile generation lacked.

P2 · The front, not the centre.
     Per sector, the front is the leading edge of the activity IN THAT SECTOR. A
     very active cell to the north contributes no term whatsoever to the
     south-west measurement — the direct fix for the v3.3.0 root cause.

P3 · CBDR — constant bearing, decreasing range.
     The mariner's collision rule. Ring descending + sector steady → collision
     course. Ring descending + sector rotating → it will pass by. No least
     squares, no extrapolation, no ETA. When the evidence is insufficient the
     monitor says so instead of producing a number.

The two invariants
──────────────────
A · Bounded messages. `notified_ring` increases strictly at every message and is
    reset only when the event closes, so ONE event emits at most `ring_count`
    ring messages plus one all-clear — for any input whatsoever. The v3.3.0 field
    failure is not unlikely under this design, it is arithmetically impossible.

B · Every state has a reachable exit. The only thing holding an event open is
    `has_activity` — "some sector has >= MIN_STRIKES_SECTOR strikes in the last
    WINDOW_MIN minutes inside R" — computed over a hard sliding window with
    unconditional expiry. No exit condition mentions speed, ring, bearing, track
    verdict or ETA. Escalation reads front_km, de-escalation reads activity in
    the same window and the same radius. One variable holds the event open and it
    decays to zero on its own. That is the structural fix for the error that
    killed v2.26 and v3.3.0: escalation and de-escalation evaluated on different
    variables, hence states with no way out.

Nothing in this module is user-tunable. There are no sensitivity presets: preset
tuning was the reflex of the last three generations, and the anti-spam bound here
is structural, so there is nothing left for a preset to trade off.
"""

import math
from dataclasses import dataclass

from .geo import distance_km, azimuth_deg

# ── Grid ──────────────────────────────────────────────────────────────────────
SECTOR_COUNT     = 12
SECTOR_WIDTH_DEG = 360.0 / SECTOR_COUNT      # 30° — about the angular width of a
                                             # cell at 20-30 km: fine enough to
                                             # resolve rotation, coarse enough not
                                             # to shatter one cell across bins.
OBSERVE_FACTOR   = 1.6                       # observation radius = R * this

RING_FRACTIONS = {
    4: (1.00, 0.65, 0.40, 0.20),             # R=30 → 30 / 19.5 / 12 / 6
    3: (1.00, 0.55, 0.25),                   # R=30 → 30 / 16.5 / 7.5
    2: (1.00, 0.40),                         # R=30 → 30 / 12
}
DEFAULT_RING_COUNT = 4
RING_HYSTERESIS    = 0.15                    # the front must retreat 15 % past an
                                             # edge before the ring is released

MIN_RADIUS_KM = 10.0
MAX_RADIUS_KM = 60.0

# ── Front ─────────────────────────────────────────────────────────────────────
WINDOW_MIN         = 10.0    # analysis window, minutes
MIN_STRIKES_SECTOR = 4       # below this a sector is INACTIVE and contributes nothing
FRONT_QUANTILE     = 0.15
FRONT_MIN_RANK     = 3       # 1-based: the front is never nearer than the 3rd-nearest
                             # strike, so one or two mislocated strikes cannot pull it in

# ── Cadence ───────────────────────────────────────────────────────────────────
POLL_INTERVAL_SEC = 60.0
CONFIRM_POLLS     = 2        # consecutive polls a descent must repeat
CLEAR_DWELL_SEC   = 600.0    # quiet time inside R before the all-clear
WARMUP_SEC        = 120.0    # connection must be up this long before any all-clear

# ── CBDR ──────────────────────────────────────────────────────────────────────
#
# The discriminant is SIDEWAYS DISPLACEMENT IN KILOMETRES, not bearing rotation
# in degrees. Rotation alone is not comparable across distances: the same 25°
# swing means 12 km of sideways travel at 28 km out and only 2 km at 5 km out, so
# a degree threshold calls a storm that is about to hit you "a glancing pass"
# precisely when it is closest and the answer matters most. Multiplying by the
# lever arm removes the distance dependence and leaves the physical quantity the
# user is actually asking about: by how much will it miss me.
#
#   lateral_km = min(front_now, front_ref) * sin(rotation)
#
# The shorter of the two arms is deliberate. Calling an approaching storm
# "grazing" is the dangerous error, so the estimate is biased against that.
#
# The dead band between the two thresholds is not indecision, it is honesty:
# far out, geometry makes every storm look head-on, and strike-derived bearings
# jitter by several degrees. The monitor reports the ambiguity and lets the next
# ring settle it, where the geometry is unmistakable.
# The two bearings compared are each averaged over a WINDOW of polls, and the
# windows are placed FURTHER APART THAN THE ANALYSIS WINDOW IS WIDE. Both details
# matter. A front's bearing comes from a handful of discharges scattered several
# km sideways, so one sample carries 10-20° of jitter — enough, on a 20 km lever
# arm, to fake 6 km of sideways travel and label an incoming storm "grazing". But
# averaging consecutive polls barely helps, because polls 60 s apart share ~90 %
# of the strikes in a 10-minute window and are therefore strongly correlated.
# Separating the windows by more than WINDOW_MIN is what makes the two estimates
# independent; only then does averaging actually buy noise reduction.
#
# The cost is latency: no verdict before ~15 minutes of history. That is what the
# observation radius (R * OBSERVE_FACTOR) pays for — the storm is watched for
# some 20 minutes outside the alert radius, so the history is already there by
# the time the front crosses R and the first message goes out.
CBDR_HISTORY_SEC    = 960.0
CBDR_NOW_WINDOW_SEC = 180.0          # samples aged 0..180 s → the current bearing
CBDR_REF_FROM_SEC   = 600.0          # samples aged 600..900 s → the reference
CBDR_REF_TO_SEC     = 900.0
CBDR_MIN_SAMPLES    = 2              # per window, below which no verdict is drawn
# Calibrated on the simulator over 175 seeded scenarios: zero head-on storms
# mislabelled "grazing" (the dangerous error), 73 of 75 grazing storms correctly
# identified, no grazing storm called "closing" at ring 2 or deeper.
CBDR_CLOSING_KM    = 2.5     # sideways displacement below this → constant bearing
CBDR_GRAZING_KM    = 4.0     # above this → it will pass by
CBDR_NEW_CELL_DEG  = 60.0    # jump between two CONSECUTIVE polls → a different cell
                             # took over; storms do not swing 60° in a minute

# ── Event states ──────────────────────────────────────────────────────────────
EVENT_IDLE, EVENT_ACTIVE, EVENT_FADING = 0, 1, 2
EVENT_NAMES = {EVENT_IDLE: "IDLE", EVENT_ACTIVE: "ACTIVE", EVENT_FADING: "FADING"}

# ── Track verdicts ────────────────────────────────────────────────────────────
TRACK_CLOSING = "closing"
TRACK_GRAZING = "grazing"
TRACK_UNKNOWN = "unknown"


# ── Radius / rings ────────────────────────────────────────────────────────────

def clamp_radius(radius_km: float) -> float:
    """The shared LiveMonitorPayload defaults radius_km to 100, which is far
    outside what this algorithm is designed for. Clamp rather than trust."""
    try:
        r = float(radius_km)
    except (TypeError, ValueError):
        return 30.0
    return max(MIN_RADIUS_KM, min(MAX_RADIUS_KM, r))


def clamp_ring_count(ring_count) -> int:
    try:
        n = int(ring_count)
    except (TypeError, ValueError):
        return DEFAULT_RING_COUNT
    return n if n in RING_FRACTIONS else DEFAULT_RING_COUNT


def ring_edges(radius_km: float, ring_count: int = DEFAULT_RING_COUNT) -> list[float]:
    """Ring boundaries, descending, with edges[0] == radius_km.

    Ring k (1-based) occupies (edges[k], edges[k-1]], with edges[n] implicitly 0.
    Deriving them proportionally means the shape of the alert ladder does not
    change when the user changes the radius.
    """
    fractions = RING_FRACTIONS[clamp_ring_count(ring_count)]
    return [radius_km * f for f in fractions]


def ring_of(front_km: float, edges: list[float]) -> int:
    """Ring index of a distance. 0 means "outside the radius"; higher is closer."""
    if front_km > edges[0]:
        return 0
    deepest = 0
    for k, edge in enumerate(edges, start=1):
        if front_km <= edge:
            deepest = k
    return deepest


def next_ring(front_km: float, current_ring: int, edges: list[float]) -> int:
    """Ring index with hysteresis.

    Closing in is acted on immediately; retreating is only honoured once the front
    has passed the edge by RING_HYSTERESIS. Expanding the edges makes
    `front <= edge` easier to satisfy, hence yields a deeper index, hence resists
    the retreat.
    """
    ring_now  = ring_of(front_km, edges)
    if ring_now > current_ring:
        return ring_now
    ring_hold = ring_of(front_km, [e * (1 + RING_HYSTERESIS) for e in edges])
    return min(current_ring, ring_hold)


# ── Sectors ───────────────────────────────────────────────────────────────────

def sector_of(azimuth: float) -> int:
    """Sector index 0..11, sector 0 centred on true north."""
    return int(((azimuth + SECTOR_WIDTH_DEG / 2) % 360.0) // SECTOR_WIDTH_DEG)


def sector_center_deg(sector: int) -> float:
    return (sector * SECTOR_WIDTH_DEG) % 360.0


def sector_delta(a: int, b: int) -> int:
    """Signed shortest circular difference, i.e. how many sectors from b to a."""
    half = SECTOR_COUNT // 2
    return ((a - b + half) % SECTOR_COUNT) - half


def angle_delta(a: float, b: float) -> float:
    """Signed shortest angular difference from b to a, in (-180, 180]."""
    return ((a - b + 180.0) % 360.0) - 180.0


def mean_bearing(bearings: list[float]) -> float:
    """Circular mean — a plain average would be wrong across the 0°/360° seam."""
    if not bearings:
        return 0.0
    x = sum(math.cos(math.radians(b)) for b in bearings)
    y = sum(math.sin(math.radians(b)) for b in bearings)
    if x == 0.0 and y == 0.0:
        return bearings[0]
    # Round before the modulo: a mean that lands exactly on north comes out of
    # atan2 as a tiny negative, and -1e-16 % 360 is 360.0, not 0.0.
    return round(math.degrees(math.atan2(y, x)), 9) % 360.0


# ── Front of a sector ─────────────────────────────────────────────────────────

def _quantile(sorted_values: list[float], q: float) -> float:
    """Linear-interpolated quantile of an already sorted list."""
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = q * (len(sorted_values) - 1)
    lo, hi = math.floor(pos), math.ceil(pos)
    if lo == hi:
        return sorted_values[lo]
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def front_of_sector(sorted_distances: list[float],
                    min_strikes: int = MIN_STRIKES_SECTOR) -> float | None:
    """Leading edge of the activity in one sector, or None if the sector is inactive.

    The rank floor is the load-bearing half: Blitzortung mislocates strikes by a
    few km routinely and occasionally by much more, so the front is never allowed
    to be nearer than the FRONT_MIN_RANK-th nearest strike of that sector. One or
    two phantom strikes cannot pull it inward no matter how busy the sector is.
    That is a structural guarantee, not a threshold to tune.
    """
    if len(sorted_distances) < min_strikes:
        return None
    q = _quantile(sorted_distances, FRONT_QUANTILE)
    return max(q, sorted_distances[FRONT_MIN_RANK - 1])


# ── One poll's picture ────────────────────────────────────────────────────────

# Strikes within this much of the front define the sector's bearing: the leading
# edge is what is coming at you, so the tail of the cell must not drag it around.
FRONT_BEARING_BAND_KM = 5.0

# The TRACK bearing is a different measurement with a different job, and it is
# taken over a much wider band — every strike within this distance of the front,
# in any sector.
#
# Using the front for direction as well as for distance was a mistake worth
# recording: the leading edge is only a handful of discharges, so its bearing
# carries 20-30° of jitter, and on a 20 km lever arm that fabricates 8 km of
# sideways travel — enough to report an incoming storm as "a glancing pass". The
# front answers WHEN it arrives; the body of the cell answers WHERE IT IS GOING,
# and averaging a hundred strikes instead of five cuts the angular noise by an
# order of magnitude. This is not the old centroid mistake: the centroid is used
# only for its ANGLE, never for its distance or its rate of change.
TRACK_BAND_KM = 20.0


@dataclass(frozen=True)
class SectorReading:
    sector:      int
    count:       int
    front_km:    float
    bearing_deg: float = 0.0   # circular mean of the strikes nearest the front


@dataclass(frozen=True)
class Frame:
    now:               float
    active:            tuple[SectorReading, ...] = ()   # sorted by front, nearest first
    dominant:          SectorReading | None      = None
    strikes_in_radius: int                       = 0
    strikes_observed:  int                       = 0
    has_activity:      bool                      = False
    track_bearing:     float | None              = None  # body of the relevant cell
    track_count:       int                       = 0


def build_frame(strikes, origin: tuple[float, float], now: float,
                radius_km: float, observe_radius_km: float, window_sec: float,
                min_strikes_sector: int = MIN_STRIKES_SECTOR) -> Frame:
    """Bin the strike buffer into sectors and derive each sector's front.

    O(n): one distance, one azimuth and one integer division per strike. There is
    no pairwise loop anywhere — the O(n²) DBSCAN call of the previous generations
    used to block the event loop for seconds during a severe storm.

    The dominant sector is the active one with the nearest front. Only the
    dominant sector drives the ring, so only one sector can ever produce a
    message: that is what makes several simultaneous cells safe.
    """
    olat, olon = origin
    cutoff = now - window_sec
    buckets: dict[int, list[tuple[float, float]]] = {}   # sector → [(dist, bearing)]
    in_radius = 0
    observed = 0

    for t, lat, lon in strikes:
        if t < cutoff:
            continue
        d = distance_km(olat, olon, lat, lon)
        if d > observe_radius_km:
            continue
        observed += 1
        if d <= radius_km:
            in_radius += 1
        az = azimuth_deg(olat, olon, lat, lon)
        buckets.setdefault(sector_of(az), []).append((d, az))

    readings: list[SectorReading] = []
    for s, points in buckets.items():
        points.sort(key=lambda p: p[0])
        distances = [p[0] for p in points]
        front = front_of_sector(distances, min_strikes_sector)
        if front is None:
            continue
        leading = [az for d, az in points if d <= front + FRONT_BEARING_BAND_KM]
        readings.append(SectorReading(
            sector=s, count=len(points), front_km=front,
            bearing_deg=mean_bearing(leading or [az for _, az in points]),
        ))

    # Deterministic ordering: nearest front, then busiest, then lowest index.
    readings.sort(key=lambda r: (r.front_km, -r.count, r.sector))
    dominant = readings[0] if readings else None

    # Direction of the cell that owns the front, over every sector it spills into.
    track_bearing, track_count = None, 0
    if dominant is not None:
        band = dominant.front_km + TRACK_BAND_KM
        body = [az for points in buckets.values()
                for d, az in points if d <= band]
        if body:
            track_bearing, track_count = mean_bearing(body), len(body)

    return Frame(
        now=now,
        active=tuple(readings),
        dominant=dominant,
        strikes_in_radius=in_radius,
        strikes_observed=observed,
        has_activity=dominant is not None and dominant.front_km <= radius_km,
        track_bearing=track_bearing,
        track_count=track_count,
    )


# ── Alerts ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RingAlert:
    """The front crossed into a ring that has not been announced yet."""
    ring:             int
    ring_count:       int
    ring_edge_km:     float
    front_km:         float
    bearing_deg:      float
    sector:           int
    strikes:          int
    strikes_in_radius: int
    secondary:        tuple[SectorReading, ...] = ()
    track:            str   = TRACK_UNKNOWN
    pass_bearing_deg: float | None = None
    new_cell:         bool  = False
    prev_ring:        int | None   = None
    prev_front_km:    float | None = None
    elapsed_sec:      float | None = None
    is_first:         bool  = False
    is_innermost:     bool  = False


@dataclass(frozen=True)
class ClearAlert:
    """No activity inside the radius for CLEAR_DWELL_SEC — the event is over."""
    ring_count:         int
    radius_km:          float
    closest_km:         float | None
    closest_ring:       int
    closest_at:         float
    quiet_sec:          float
    event_duration_sec: float


# ── Tracker ───────────────────────────────────────────────────────────────────

class StormFrontTracker:
    """Ring tracking, event lifecycle and CBDR verdicts.

    evaluate() advances the GEOMETRY every poll and returns an alert to deliver,
    if any. commit() advances the NOTIFICATION bookkeeping and must only be called
    once delivery is confirmed — so a dropped Telegram message is retried, and the
    retry always describes the current situation rather than a stale one.
    """

    def __init__(self, radius_km: float, ring_count: int = DEFAULT_RING_COUNT):
        self.radius_km  = clamp_radius(radius_km)
        self.ring_count = clamp_ring_count(ring_count)
        self.edges      = ring_edges(self.radius_km, self.ring_count)
        self.observe_radius_km = self.radius_km * OBSERVE_FACTOR

        self.event_state      = EVENT_IDLE
        self.current_ring     = 0
        self.notified_ring    = 0
        self.fading_since     = 0.0
        self.event_started_at = 0.0

        self.closest_km   : float | None = None
        self.closest_ring = 0
        self.closest_at   = 0.0

        self._descent_target = 0
        self._descent_streak = 0
        self._history: list[tuple[float, int, float]] = []   # (t, sector, front_km)

        self._last_ring:     int | None   = None
        self._last_front_km: float | None = None
        self._last_alert_at: float        = 0.0

    # ── Evaluation ────────────────────────────────────────────────────────────

    def evaluate(self, frame: Frame, now: float,
                 feed_ok: bool = True,
                 connected_for: float = 1e9) -> RingAlert | ClearAlert | None:
        if not feed_ok:
            # A dead socket and a clear sky are indistinguishable if you only count
            # strikes. Freeze rather than guess: no alert, and the clear countdown
            # restarts from scratch once the feed is back.
            self.fading_since = 0.0
            return None

        self._prune_history(now)
        dominant = frame.dominant
        if dominant is not None and frame.track_bearing is not None:
            self._history.append((now, frame.track_bearing,
                                  dominant.front_km, dominant.sector))

        self._advance_event(frame, now)
        self._advance_ring(dominant)

        if self.event_state != EVENT_IDLE and dominant is not None:
            if self.closest_km is None or dominant.front_km < self.closest_km:
                self.closest_km   = dominant.front_km
                self.closest_ring = ring_of(dominant.front_km, self.edges)
                self.closest_at   = now

        if self.event_state != EVENT_IDLE and self.current_ring > self.notified_ring:
            return self._ring_alert(frame, now)

        if (self.event_state == EVENT_FADING
                and self.fading_since
                and now - self.fading_since >= CLEAR_DWELL_SEC
                and connected_for >= WARMUP_SEC):
            if self.notified_ring > 0:
                return self._clear_alert(now)
            # Nothing was ever announced — close the event silently.
            self._reset_event()
        return None

    def commit(self, alert, now: float) -> None:
        """Called only after the alert was confirmed delivered (or deliberately
        suppressed by quiet hours — silencing must not desync the state, or the
        alert would be retried every poll until the window ends)."""
        if isinstance(alert, RingAlert):
            self.notified_ring  = alert.ring
            self._last_ring     = alert.ring
            self._last_front_km = alert.front_km
            self._last_alert_at = now
        elif isinstance(alert, ClearAlert):
            self._reset_event()

    # ── Event lifecycle ───────────────────────────────────────────────────────

    def _advance_event(self, frame: Frame, now: float) -> None:
        if frame.has_activity:
            if self.event_state == EVENT_IDLE:
                # Opening an event resets the notification bookkeeping, so a stale
                # notified_ring can never suppress the messages of a new storm.
                self.event_state      = EVENT_ACTIVE
                self.event_started_at = now
                self.notified_ring    = 0
                self.closest_km       = None
                self.closest_ring     = 0
                self.closest_at       = 0.0
                self._last_ring       = None
                self._last_front_km   = None
                self._last_alert_at   = 0.0
            elif self.event_state == EVENT_FADING:
                self.event_state = EVENT_ACTIVE
            self.fading_since = 0.0
        elif self.event_state == EVENT_ACTIVE:
            self.event_state  = EVENT_FADING
            self.fading_since = now
        elif self.event_state == EVENT_IDLE:
            # No event, nothing to announce: keep the bookkeeping consistent so a
            # leftover notified_ring (restored from an odd state file, say) can
            # never silence the first rings of the next storm.
            self.notified_ring = 0

    def _advance_ring(self, dominant: SectorReading | None) -> None:
        target = (next_ring(dominant.front_km, self.current_ring, self.edges)
                  if dominant is not None else 0)

        if target > self.current_ring:
            # A descent must repeat before it is believed. Being late to say
            # "it is here" costs a minute; being early costs the user's trust.
            if self._descent_target == target:
                self._descent_streak += 1
            else:
                self._descent_target = target
                self._descent_streak = 1
            if self._descent_streak >= CONFIRM_POLLS:
                self.current_ring    = target
                self._descent_target = 0
                self._descent_streak = 0
            return

        self._descent_target = 0
        self._descent_streak = 0
        if target < self.current_ring:
            # Already past the hysteresis margin — no dwell needed.
            self.current_ring = target

    def _reset_event(self) -> None:
        self.event_state      = EVENT_IDLE
        self.current_ring     = 0
        self.notified_ring    = 0
        self.fading_since     = 0.0
        self.event_started_at = 0.0
        self.closest_km       = None
        self.closest_ring     = 0
        self.closest_at       = 0.0
        self._descent_target  = 0
        self._descent_streak  = 0
        self._history         = []
        self._last_ring       = None
        self._last_front_km   = None
        self._last_alert_at   = 0.0

    def reset_geometry_history(self) -> None:
        """Forget everything derived from the PREVIOUS origin, without touching
        the event.

        Needed only by a monitor whose origin can move. Continuous motion needs
        nothing: bearings and ranges measured from a moving observer are exactly
        what CBDR is defined on, so the history stays comparable and the verdict
        stays correct. A DISCONTINUITY is different — the observer did not travel
        between the two samples, they were relocated — and the stored bearings
        then describe a geometry that no longer exists.

        What is deliberately NOT reset: `event_state` and `notified_ring`.
        Reopening the event would reset the notification ladder and let one storm
        emit a second full set of ring messages, which is precisely the bound
        invariant A exists to guarantee. Losing the CBDR verdict for a few polls
        is a cost; losing the message bound is the v3.3.0 field failure.
        """
        self._history        = []
        self._descent_target = 0
        self._descent_streak = 0

    # ── CBDR ──────────────────────────────────────────────────────────────────

    def _prune_history(self, now: float) -> None:
        cutoff = now - CBDR_HISTORY_SEC
        if self._history and self._history[0][0] < cutoff:
            self._history = [h for h in self._history if h[0] >= cutoff]

    def track_verdict(self, now: float, bearing: float,
                      front_km: float) -> tuple[str, float | None, bool]:
        """(verdict, pass_bearing_deg, new_cell).

        Only ever consulted together with a ring descent, which is exactly the
        mariner's rule: decreasing range is the precondition, constant or rotating
        bearing is the discriminant. The rotation is converted to kilometres of
        sideways travel so the verdict means the same thing at every distance.
        """
        # A large swing between two CONSECUTIVE polls is not a storm turning —
        # storms do not rotate 60° around you in a minute. It means a different
        # cell became dominant, and the previous samples describe a different
        # object, so no verdict can be drawn from them.
        if len(self._history) >= 2:
            previous = self._history[-2]
            if (now - previous[0] <= 2 * POLL_INTERVAL_SEC
                    and abs(angle_delta(bearing, previous[1])) > CBDR_NEW_CELL_DEG):
                return TRACK_UNKNOWN, None, True

        recent = [h for h in self._history if now - h[0] <= CBDR_NOW_WINDOW_SEC]
        older  = [h for h in self._history
                  if CBDR_REF_FROM_SEC <= now - h[0] <= CBDR_REF_TO_SEC]
        if len(recent) < CBDR_MIN_SAMPLES or len(older) < CBDR_MIN_SAMPLES:
            return TRACK_UNKNOWN, None, False

        bearing_now = mean_bearing([h[1] for h in recent])
        bearing_ref = mean_bearing([h[1] for h in older])
        front_now   = sum(h[2] for h in recent) / len(recent)
        front_ref   = sum(h[2] for h in older) / len(older)

        rotation = angle_delta(bearing_now, bearing_ref)
        lever_km = min(front_now, front_ref)
        lateral_km = abs(lever_km * math.sin(math.radians(rotation)))

        if lateral_km <= CBDR_CLOSING_KM:
            return TRACK_CLOSING, None, False
        if lateral_km >= CBDR_GRAZING_KM:
            # Extrapolate the observed rotation by one more interval of the same
            # length to name the side it will pass on.
            return TRACK_GRAZING, (bearing_now + rotation) % 360.0, False
        return TRACK_UNKNOWN, None, False

    # ── Alert construction ────────────────────────────────────────────────────

    def _ring_alert(self, frame: Frame, now: float) -> RingAlert:
        dominant = frame.dominant
        track, pass_bearing, new_cell = self.track_verdict(
            now, frame.track_bearing if frame.track_bearing is not None
            else dominant.bearing_deg, dominant.front_km)
        elapsed = (now - self._last_alert_at) if self._last_alert_at else None
        return RingAlert(
            ring=self.current_ring,
            ring_count=self.ring_count,
            ring_edge_km=self.edges[self.current_ring - 1],
            front_km=dominant.front_km,
            bearing_deg=dominant.bearing_deg,
            sector=dominant.sector,
            strikes=dominant.count,
            strikes_in_radius=frame.strikes_in_radius,
            # Only sectors at least 60° away count as "other activity". A cell
            # wide enough to straddle a sector boundary would otherwise be
            # reported as a second storm sitting next to itself.
            secondary=tuple(r for r in frame.active
                            if abs(sector_delta(r.sector, dominant.sector)) >= 2)[:2],
            track=track,
            pass_bearing_deg=pass_bearing,
            new_cell=new_cell,
            prev_ring=self._last_ring,
            prev_front_km=self._last_front_km,
            elapsed_sec=elapsed,
            is_first=self.notified_ring == 0,
            is_innermost=self.current_ring >= self.ring_count,
        )

    def _clear_alert(self, now: float) -> ClearAlert:
        return ClearAlert(
            ring_count=self.ring_count,
            radius_km=self.radius_km,
            closest_km=self.closest_km,
            closest_ring=self.closest_ring,
            closest_at=self.closest_at,
            quiet_sec=now - self.fading_since if self.fading_since else 0.0,
            event_duration_sec=(now - self.event_started_at
                                if self.event_started_at else 0.0),
        )

    # ── Diagnostics ───────────────────────────────────────────────────────────

    def debug_line(self, frame: Frame) -> str:
        dominant = frame.dominant
        front = f"{dominant.front_km:.1f}km" if dominant else "—"
        sect  = f"{dominant.sector:02d}" if dominant else "--"
        return (f"ring={self.current_ring}/{self.ring_count} "
                f"notified={self.notified_ring} front={front} sec={sect} "
                f"act={len(frame.active)} n={frame.strikes_in_radius} "
                f"evt={EVENT_NAMES[self.event_state]} "
                f"pend={self._descent_target or '-'}/{self._descent_streak}")

    # ── Persistence ───────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "event_state": self.event_state,
            "current_ring": self.current_ring,
            "notified_ring": self.notified_ring,
            "fading_since": self.fading_since,
            "event_started_at": self.event_started_at,
            "closest_km": self.closest_km,
            "closest_ring": self.closest_ring,
            "closest_at": self.closest_at,
            "descent_target": self._descent_target,
            "descent_streak": self._descent_streak,
            "history": [list(h) for h in self._history],
            "last_ring": self._last_ring,
            "last_front_km": self._last_front_km,
            "last_alert_at": self._last_alert_at,
        }

    @classmethod
    def from_dict(cls, radius_km: float, ring_count: int, data: dict | None):
        tracker = cls(radius_km, ring_count)
        if not data:
            return tracker
        tracker.event_state      = int(data.get("event_state", EVENT_IDLE))
        tracker.current_ring     = int(data.get("current_ring", 0))
        tracker.notified_ring    = int(data.get("notified_ring", 0))
        tracker.fading_since     = float(data.get("fading_since", 0.0))
        tracker.event_started_at = float(data.get("event_started_at", 0.0))
        closest = data.get("closest_km")
        tracker.closest_km       = None if closest is None else float(closest)
        tracker.closest_ring     = int(data.get("closest_ring", 0))
        tracker.closest_at       = float(data.get("closest_at", 0.0))
        tracker._descent_target  = int(data.get("descent_target", 0))
        tracker._descent_streak  = int(data.get("descent_streak", 0))
        tracker._history         = [(float(t), float(b), float(f), int(s))
                                    for t, b, f, s in data.get("history", [])]
        last_ring = data.get("last_ring")
        tracker._last_ring       = None if last_ring is None else int(last_ring)
        last_front = data.get("last_front_km")
        tracker._last_front_km   = None if last_front is None else float(last_front)
        tracker._last_alert_at   = float(data.get("last_alert_at", 0.0))
        # A ring deeper than the ladder (radius or ring_count changed under us)
        # would be unreachable and could never be announced again.
        tracker.current_ring  = min(tracker.current_ring, tracker.ring_count)
        tracker.notified_ring = min(tracker.notified_ring, tracker.ring_count)
        return tracker


__all__ = [
    "SECTOR_COUNT", "SECTOR_WIDTH_DEG", "OBSERVE_FACTOR",
    "RING_FRACTIONS", "DEFAULT_RING_COUNT", "RING_HYSTERESIS",
    "MIN_RADIUS_KM", "MAX_RADIUS_KM",
    "WINDOW_MIN", "MIN_STRIKES_SECTOR", "FRONT_QUANTILE", "FRONT_MIN_RANK",
    "POLL_INTERVAL_SEC", "CONFIRM_POLLS", "CLEAR_DWELL_SEC", "WARMUP_SEC",
    "CBDR_HISTORY_SEC", "CBDR_CLOSING_KM",
    "CBDR_GRAZING_KM", "CBDR_NEW_CELL_DEG",
    "EVENT_IDLE", "EVENT_ACTIVE", "EVENT_FADING", "EVENT_NAMES",
    "TRACK_CLOSING", "TRACK_GRAZING", "TRACK_UNKNOWN",
    "clamp_radius", "clamp_ring_count", "ring_edges", "ring_of", "next_ring",
    "sector_of", "sector_center_deg", "sector_delta", "angle_delta",
    "mean_bearing", "front_of_sector",
    "SectorReading", "Frame", "build_frame",
    "RingAlert", "ClearAlert", "StormFrontTracker",
]
