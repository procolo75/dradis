"""
live_monitors/lightning_core.py
────────────────────────────────
Pure decision core of the lightning live monitor. No I/O, no asyncio, no MQTT:
everything here is deterministic given (strikes, origin, clock), which makes the
whole algorithm unit-testable and replayable offline against recorded storms.

Why this module exists
──────────────────────
The previous design drove the state machine with a single scalar: the distance of
the centroid of the *nearest DBSCAN cluster*. That is not the physical quantity of
any object — it is a minimum over an unstable set, recomputed from scratch every
poll with min_samples=2. Two consequences, both observed in the field:

  · False WARNING — a new 2-strike cluster forming closer than the previous one
    made the scalar jump (e.g. 60 → 20 km) in one step. The trend read
    APPROACHING, the ETA derived from that jump was tiny, and WARNING fired.
    Nothing had moved; only the cluster labelling had changed.
  · No CLEAR — de-escalation required *zero* clusters in the whole radius, so any
    two strikes 90 km away kept refreshing the "last significant activity"
    timestamp and the quiet countdown never started.

The design error underneath both: escalation and de-escalation were evaluated on
*different* variables, so they were not complements and the machine had states it
could not leave. No threshold tuning can fix that.

Guiding principle
─────────────────
Escalation and de-escalation must be decided on the SAME continuous, physically
interpretable quantities, with separated enter/exit thresholds (Schmitt trigger)
and dwell times. Clustering is gone entirely — it was never needed for the
decision, and it was also the O(n²) call that blocked the event loop.

The three observables
─────────────────────
  d10     10th percentile of the individual strike distances. A percentile rather
          than a minimum: one isolated strike cannot move it, it is continuous
          under adding/removing a few points, and there is no min_samples cliff.
  r_near  strikes/min within d10 + NEAR_BAND_KM — the activity of the relevant
          storm body, self-scaling with its distance.
  v_c     closing speed, from the shift of the strike-field centroid between the
          old and new half of the window. It is the motion of a MASS of strikes,
          not the identity of a cluster, so it does not jump on relabelling.

d10 and v_c are EMA-smoothed so a single noisy poll cannot trigger a transition.
"""

import math
from dataclasses import dataclass

# ── Window / observable tuning (algorithm-level, not user-facing) ──────────────
WINDOW_MIN            = 15     # strike buffer / analysis window
MIN_STRIKES_D10       = 5      # below this, d10 is undefined (no meaningful body)
D10_PERCENTILE        = 0.10
NEAR_BAND_KM          = 15.0   # r_near counts strikes within d10 + this
FIELD_BAND_KM         = 25.0   # v_c uses strikes within d10 + this
MIN_STRIKES_PER_HALF  = 4      # per half-window, below this v_c is undefined
EMA_ALPHA_D10         = 0.5
EMA_ALPHA_VC          = 0.35
STALE_POLLS_RESET     = 2      # polls without data before the EMA state is reset
MIN_CLOSING_KMH       = 10.0   # below this the field is not "closing" at all
ETA_CONFIRM_POLLS     = 3      # consecutive closing polls before an ETA is trusted

# ── Alert cadence ─────────────────────────────────────────────────────────────
PERIODIC_ALERT_SEC    = 600    # re-alert cadence while in WARNING
POLL_INTERVAL_STATIC  = 120
POLL_INTERVAL_MOVING  = 45     # a moving origin needs a faster refresh

# ── Dwell times (seconds) ─────────────────────────────────────────────────────
DWELL_WATCH_ENTER     = 1 * POLL_INTERVAL_STATIC   # 1 poll of persistence
DWELL_WARN_ENTER      = 2 * POLL_INTERVAL_STATIC   # 2 polls of persistence
DWELL_WARN_EXIT       = 600                        # 10 min before 🔴 → 🟡

# ── Threat levels ─────────────────────────────────────────────────────────────
LEVEL_CLEAR   = 0
LEVEL_WATCH   = 1
LEVEL_WARNING = 2

LEVEL_NAMES = {LEVEL_CLEAR: "CLEAR", LEVEL_WATCH: "WATCH", LEVEL_WARNING: "WARNING"}


# ── Sensitivity presets ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class Preset:
    """A coherent set of thresholds. Enter/exit are always strictly separated."""
    name:                str
    watch_enter_km:      float
    watch_exit_km:       float
    warn_enter_km:       float
    warn_exit_km:        float
    watch_rate:          float   # strikes/min to enter WATCH
    clear_rate:          float   # strikes/min below which activity counts as over
    warn_rate:           float   # strikes/min required for any WARNING
    eta_max_min:         float   # confirmed approach reaching us within this → WARNING
    eta_max_dist_km:     float   # …but never from farther away than this
    warn_exit_closing:   float   # km/h; must be below this to leave WARNING
    clear_dwell_sec:     float   # how long the exit condition must hold for ✅


PRESETS: dict[str, Preset] = {
    "low": Preset(
        name="low",
        watch_enter_km=30.0, watch_exit_km=45.0,
        warn_enter_km=10.0,  warn_exit_km=16.0,
        watch_rate=0.30, clear_rate=0.10, warn_rate=0.80,
        eta_max_min=20.0, eta_max_dist_km=35.0,
        warn_exit_closing=3.0, clear_dwell_sec=1500.0,
    ),
    "medium": Preset(
        name="medium",
        watch_enter_km=40.0, watch_exit_km=55.0,
        warn_enter_km=15.0,  warn_exit_km=22.0,
        watch_rate=0.20, clear_rate=0.07, warn_rate=0.50,
        eta_max_min=25.0, eta_max_dist_km=45.0,
        warn_exit_closing=3.0, clear_dwell_sec=1200.0,
    ),
    "high": Preset(
        name="high",
        watch_enter_km=55.0, watch_exit_km=70.0,
        warn_enter_km=22.0,  warn_exit_km=30.0,
        watch_rate=0.13, clear_rate=0.05, warn_rate=0.30,
        eta_max_min=35.0, eta_max_dist_km=55.0,
        warn_exit_closing=3.0, clear_dwell_sec=900.0,
    ),
}

DEFAULT_SENSITIVITY = "medium"


def get_preset(name: str) -> Preset:
    return PRESETS.get((name or "").strip().lower(), PRESETS[DEFAULT_SENSITIVITY])


# ── Geo helpers ───────────────────────────────────────────────────────────────

EARTH_RADIUS_KM = 6371.0


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance. Haversine — correct at any latitude and separation,
    which matters once the origin can move."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def azimuth_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial great-circle bearing from point 1 to point 2, degrees clockwise from N."""
    dlon = math.radians(lon2 - lon1)
    p1, p2 = math.radians(lat1), math.radians(lat2)
    x = math.sin(dlon) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


_DIR_IT = ["N", "NE", "E", "SE", "S", "SO", "O", "NO"]
_DIR_EN = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def direction_label(azimuth: float, lang: str) -> str:
    labels = _DIR_IT if lang == "it" else _DIR_EN
    return labels[round(azimuth / 45) % 8]


# ── Geohash helpers (Blitzortung topic derivation) ────────────────────────────

_BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"

# Precision-3 geohash cells are 1.40625° × 1.40625° (8 lon bits, 7 lat bits).
GEOHASH_PRECISION = 3
_CELL_LAT_DEG = 180.0 / (2 ** 7)
_CELL_LON_DEG = 360.0 / (2 ** 8)


def geohash_encode(lat: float, lon: float, precision: int = GEOHASH_PRECISION) -> str:
    min_lat, max_lat = -90.0, 90.0
    min_lon, max_lon = -180.0, 180.0
    chars, bits, is_lon, char_bits = [], 0, True, 0
    for _ in range(precision * 5):
        if is_lon:
            mid = (min_lon + max_lon) / 2
            if lon >= mid:
                bits = (bits << 1) | 1; min_lon = mid
            else:
                bits <<= 1; max_lon = mid
        else:
            mid = (min_lat + max_lat) / 2
            if lat >= mid:
                bits = (bits << 1) | 1; min_lat = mid
            else:
                bits <<= 1; max_lat = mid
        is_lon = not is_lon
        char_bits += 1
        if char_bits == 5:
            chars.append(_BASE32[bits & 0x1F]); bits = 0; char_bits = 0
    return "".join(chars)


def _ring_size_for_radius(radius_km: float) -> int:
    """How many cells to extend in each direction around the origin cell.

    A 3×3 block is anchored on the *cell* the origin falls in, not on the origin
    itself, so the guaranteed coverage measured from the origin is only one full
    cell — about 118 km east/west at mid latitudes. Beyond that we widen the ring
    instead of silently under-counting strikes, which is what the old fixed 3×3
    block did for any radius above ~110 km.
    """
    worst_case_km = _CELL_LON_DEG * 111.32 * math.cos(math.radians(45))  # ~110 km
    return max(1, math.ceil(radius_km / worst_case_km))


def topics_for_area(lat: float, lon: float, radius_km: float) -> list[str]:
    """Blitzortung MQTT topic filters covering `radius_km` around (lat, lon)."""
    ring = _ring_size_for_radius(radius_km)
    cells: set[str] = set()
    for dlat in range(-ring, ring + 1):
        for dlon in range(-ring, ring + 1):
            nlat = max(-90.0, min(90.0, lat + dlat * _CELL_LAT_DEG))
            nlon = ((lon + dlon * _CELL_LON_DEG + 180) % 360) - 180
            cells.add(geohash_encode(nlat, nlon, GEOHASH_PRECISION))
    return sorted(f"blitzortung/1.1/{c[0]}/{c[1]}/{c[2]}/#" for c in cells)


# ── Observables ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Observables:
    """One evaluation's view of the world. All distances km, speeds km/h."""
    strikes_total: int   = 0     # strikes in the window within the interest radius
    strikes_near:  int   = 0     # …of those, within d10 + NEAR_BAND_KM
    d10:           float | None = None   # raw 10th-percentile distance
    d10_s:         float | None = None   # EMA-smoothed — what the state machine uses
    r_near:        float = 0.0           # strikes/min near the storm body
    v_c:           float | None = None   # raw closing speed (signed, + = approaching)
    v_c_s:         float | None = None   # EMA-smoothed closing speed
    speed_kmh:     float | None = None   # absolute speed of the strike field
    eta_min:       float | None = None   # only set when the approach is confirmed
    bearing:       float | None = None   # origin → storm body, degrees
    has_data:      bool  = False


def _percentile(sorted_values: list[float], q: float) -> float:
    """Linear-interpolated percentile of an already sorted list."""
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = q * (len(sorted_values) - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return sorted_values[lo]
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def _centroid(points: list[tuple[float, float, float]]) -> tuple[float, float, float]:
    """Return (mean_t, mean_lat, mean_lon). Plain arithmetic mean is fine at the
    scale of a single storm field; the antimeridian is not a concern here."""
    n = len(points)
    return (sum(p[0] for p in points) / n,
            sum(p[1] for p in points) / n,
            sum(p[2] for p in points) / n)


class ObservableTracker:
    """Turns the raw strike buffer into the smoothed observables.

    Holds only the EMA state and the closing streak, so it is cheap to construct
    and trivially serialisable.
    """

    def __init__(self, d10_s: float | None = None, v_c_s: float | None = None):
        self.d10_s = d10_s
        self.v_c_s = v_c_s
        self._stale_polls = 0
        self._closing_streak = 0

    def observe(self, strikes: list, origin: tuple[float, float],
                now: float, radius_km: float) -> Observables:
        """`strikes` is a list of (t, lat, lon), any order, already time-windowed
        by the caller. Distances are computed here, against the CURRENT origin —
        never frozen at ingest, so a moving origin works unchanged."""
        olat, olon = origin
        window_sec = WINDOW_MIN * 60

        # Distance is derived now, not at ingest. Keep (t, lat, lon, dist).
        scoped = []
        for t, la, lo in strikes:
            if t < now - window_sec:
                continue
            d = distance_km(olat, olon, la, lo)
            if d <= radius_km:
                scoped.append((t, la, lo, d))

        if len(scoped) < MIN_STRIKES_D10:
            # Not enough to define a storm body. Let the EMA go stale rather than
            # decay it toward a value nothing supports.
            self._stale_polls += 1
            if self._stale_polls >= STALE_POLLS_RESET:
                self.d10_s = None
                self.v_c_s = None
            self._closing_streak = 0
            return Observables(strikes_total=len(scoped), d10_s=self.d10_s,
                               v_c_s=self.v_c_s, has_data=False)

        self._stale_polls = 0

        d10 = _percentile(sorted(s[3] for s in scoped), D10_PERCENTILE)
        self.d10_s = d10 if self.d10_s is None else (
            EMA_ALPHA_D10 * d10 + (1 - EMA_ALPHA_D10) * self.d10_s
        )

        near = [s for s in scoped if s[3] <= d10 + NEAR_BAND_KM]
        r_near = len(near) / WINDOW_MIN

        v_c, speed, bearing = self._field_motion(scoped, olat, olon, d10, now)
        if v_c is not None:
            self.v_c_s = v_c if self.v_c_s is None else (
                EMA_ALPHA_VC * v_c + (1 - EMA_ALPHA_VC) * self.v_c_s
            )

        # ETA is only trusted when the field has been closing for several polls.
        # This is the gate that made the old design produce phantom warnings: it
        # used to derive an ETA from a single jump of the nearest-cluster scalar.
        if self.v_c_s is not None and self.v_c_s >= MIN_CLOSING_KMH:
            self._closing_streak += 1
        else:
            self._closing_streak = 0

        eta = None
        if (self._closing_streak >= ETA_CONFIRM_POLLS
                and self.v_c_s and self.v_c_s > 0 and self.d10_s is not None):
            eta = self.d10_s / self.v_c_s * 60.0

        if bearing is None and near:
            _, clat, clon = _centroid([(s[0], s[1], s[2]) for s in near])
            bearing = azimuth_deg(olat, olon, clat, clon)

        return Observables(
            strikes_total=len(scoped),
            strikes_near=len(near),
            d10=d10,
            d10_s=self.d10_s,
            r_near=r_near,
            v_c=v_c,
            v_c_s=self.v_c_s,
            speed_kmh=speed,
            eta_min=eta,
            bearing=bearing,
            has_data=True,
        )

    def _field_motion(self, scoped: list, olat: float, olon: float,
                      d10: float, now: float):
        """Closing speed, absolute speed and bearing of the strike field.

        Compares the centroid of the older half of the window with the centroid of
        the newer half, both restricted to the storm body. This measures the motion
        of a mass of strikes, so it is immune to the cluster-relabelling that broke
        the previous implementation.
        """
        band = d10 + FIELD_BAND_KM
        body = [(s[0], s[1], s[2]) for s in scoped if s[3] <= band]
        split = now - (WINDOW_MIN * 60) / 2
        old = [p for p in body if p[0] < split]
        new = [p for p in body if p[0] >= split]
        if len(old) < MIN_STRIKES_PER_HALF or len(new) < MIN_STRIKES_PER_HALF:
            return None, None, None

        t_old, lat_old, lon_old = _centroid(old)
        t_new, lat_new, lon_new = _centroid(new)
        dt_h = (t_new - t_old) / 3600.0
        if dt_h <= 0:
            return None, None, None

        d_old = distance_km(olat, olon, lat_old, lon_old)
        d_new = distance_km(olat, olon, lat_new, lon_new)
        v_c = (d_old - d_new) / dt_h
        speed = distance_km(lat_old, lon_old, lat_new, lon_new) / dt_h
        bearing = azimuth_deg(olat, olon, lat_new, lon_new)
        return v_c, speed, bearing

    def to_dict(self) -> dict:
        return {"d10_s": self.d10_s, "v_c_s": self.v_c_s}

    @classmethod
    def from_dict(cls, data: dict | None):
        data = data or {}
        return cls(d10_s=data.get("d10_s"), v_c_s=data.get("v_c_s"))


# ── State machine ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Decision:
    """An alert the caller should deliver. Nothing is committed until the caller
    confirms delivery — a dropped Telegram message must not desync the level."""
    level:    int
    periodic: bool = False


class ThreatStateMachine:
    """🟢 CLEAR · 🟡 WATCH · 🔴 WARNING driven by the smoothed observables.

    Enter and exit conditions are expressed on the SAME variables with strictly
    separated thresholds, and every transition carries a dwell time. That is what
    makes CLEAR always reachable (the storm moving away is enough — total absence
    of activity in the whole radius is no longer required) and WARNING immune to
    the phantom clusters that used to trigger it.
    """

    def __init__(self, preset: Preset, level: int = LEVEL_CLEAR,
                 level_since: float = 0.0, last_periodic_ts: float = 0.0):
        self.preset = preset
        self.level = level
        self.level_since = level_since
        self.last_periodic_ts = last_periodic_ts
        self._pending_level: int | None = None
        self._pending_since: float = 0.0

    # ── target level, on the same variables in both directions ────────────────

    def _warning_condition(self, obs: Observables) -> bool:
        p = self.preset
        if obs.r_near < p.warn_rate:
            return False
        if obs.d10_s is None:
            return False
        if obs.d10_s <= p.warn_enter_km:
            return True
        return (obs.eta_min is not None
                and obs.eta_min <= p.eta_max_min
                and obs.d10_s <= p.eta_max_dist_km)

    def _watch_condition(self, obs: Observables) -> bool:
        p = self.preset
        return (obs.d10_s is not None
                and obs.d10_s <= p.watch_enter_km
                and obs.r_near >= p.watch_rate)

    def target_level(self, obs: Observables) -> int:
        p = self.preset
        if self.level == LEVEL_WARNING:
            if self._warning_condition(obs):
                return LEVEL_WARNING
            far_enough = obs.d10_s is None or obs.d10_s >= p.warn_exit_km
            not_closing = obs.v_c_s is None or obs.v_c_s <= p.warn_exit_closing
            return LEVEL_WATCH if (far_enough and not_closing) else LEVEL_WARNING

        if self.level == LEVEL_WATCH:
            if self._warning_condition(obs):
                return LEVEL_WARNING
            gone = obs.d10_s is None or obs.d10_s >= p.watch_exit_km
            quiet = obs.r_near < p.clear_rate
            return LEVEL_CLEAR if (gone or quiet) else LEVEL_WATCH

        # CLEAR — a violent storm may justify jumping straight to WARNING.
        if self._warning_condition(obs):
            return LEVEL_WARNING
        return LEVEL_WATCH if self._watch_condition(obs) else LEVEL_CLEAR

    def _dwell_sec(self, target: int) -> float:
        if target == LEVEL_WARNING:
            return DWELL_WARN_ENTER
        if self.level == LEVEL_WARNING:          # 🔴 → 🟡
            return DWELL_WARN_EXIT
        if target == LEVEL_CLEAR:                # 🟡 → ✅
            return self.preset.clear_dwell_sec
        return DWELL_WATCH_ENTER                 # ✅ → 🟡

    # ── evaluation ────────────────────────────────────────────────────────────

    def evaluate(self, obs: Observables, now: float) -> Decision | None:
        """Advance the dwell tracking and return an alert to deliver, if any.

        The committed level is NOT changed here — call commit() once delivery is
        confirmed. If delivery fails the same Decision is returned on the next
        poll, so the alert is retried instead of being silently lost.
        """
        target = self.target_level(obs)

        if target == self.level:
            self._pending_level = None
            if (self.level == LEVEL_WARNING
                    and now - self.last_periodic_ts >= PERIODIC_ALERT_SEC):
                return Decision(self.level, periodic=True)
            return None

        if self._pending_level != target:
            self._pending_level = target
            self._pending_since = now
            return None

        if now - self._pending_since >= self._dwell_sec(target):
            return Decision(target)
        return None

    def commit(self, decision: Decision, now: float) -> None:
        """Called only after the alert was confirmed delivered."""
        if decision.periodic:
            self.last_periodic_ts = now
            return
        self.level = decision.level
        self.level_since = now
        self._pending_level = None
        if decision.level == LEVEL_WARNING:
            self.last_periodic_ts = now
        else:
            self.last_periodic_ts = 0.0

    @property
    def pending_label(self) -> str:
        if self._pending_level is None:
            return "-"
        return LEVEL_NAMES[self._pending_level]

    def to_dict(self) -> dict:
        return {
            "level": self.level,
            "level_since": self.level_since,
            "last_periodic_ts": self.last_periodic_ts,
        }

    @classmethod
    def from_dict(cls, preset: Preset, data: dict | None):
        data = data or {}
        return cls(
            preset,
            level=int(data.get("level", LEVEL_CLEAR)),
            level_since=float(data.get("level_since", 0.0)),
            last_periodic_ts=float(data.get("last_periodic_ts", 0.0)),
        )


__all__ = [
    "LEVEL_CLEAR", "LEVEL_WATCH", "LEVEL_WARNING", "LEVEL_NAMES",
    "Preset", "PRESETS", "DEFAULT_SENSITIVITY", "get_preset",
    "Observables", "ObservableTracker", "Decision", "ThreatStateMachine",
    "distance_km", "azimuth_deg", "direction_label",
    "topics_for_area", "geohash_encode",
    "WINDOW_MIN", "POLL_INTERVAL_STATIC", "POLL_INTERVAL_MOVING",
    "PERIODIC_ALERT_SEC",
]
