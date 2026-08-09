"""
tests/test_lightning_core.py
─────────────────────────────
Scenario tests for the lightning decision core.

Two of these are direct regressions for the field-observed failures of the
pre-3.3.0 design, which drove the state machine with the distance of the nearest
DBSCAN cluster centroid — a minimum over an unstable set:

  · test_phantom_cluster_never_warns
        A couple of stray strikes forming close to the point used to make that
        scalar jump (e.g. 60 → 20 km) in a single poll, which read as a fast
        approach and fired a WARNING for a storm that did not exist.
  · test_receding_storm_reaches_clear
        De-escalation used to require zero clusters anywhere in the radius, so
        activity 70 km away kept the all-clear from ever being sent.

Run with:  cd dradis && python -m unittest discover tests
"""

import math
import random
import unittest

from dradis.live_monitors.lightning_core import (
    LEVEL_CLEAR, LEVEL_WATCH, LEVEL_WARNING, LEVEL_NAMES,
    ObservableTracker, ThreatStateMachine, Observables, Decision,
    get_preset, distance_km, topics_for_area,
    WINDOW_MIN, POLL_INTERVAL_STATIC,
)

ORIGIN = (40.8500, 14.2700)          # Naples
RADIUS_KM = 100.0
T0 = 1_700_000_000.0


# ── Scenario helpers ──────────────────────────────────────────────────────────

def km_offset(north_km: float, east_km: float) -> tuple[float, float]:
    """Point at a local flat offset from ORIGIN. Accurate enough at storm scale."""
    lat = ORIGIN[0] + north_km / 111.32
    lon = ORIGIN[1] + east_km / (111.32 * math.cos(math.radians(ORIGIN[0])))
    return lat, lon


class Storm:
    """A cell moving in a straight line, emitting strikes at a constant rate.

    Position is expressed in local (north, east) km so scenarios read as geometry.
    """

    def __init__(self, north_km, east_km, v_north=0.0, v_east=0.0,
                 rate_per_min=3.0, spread_km=4.0, active_from=0.0, active_until=1e12):
        self.n0, self.e0 = north_km, east_km
        self.vn, self.ve = v_north, v_east          # km/h
        self.rate = rate_per_min
        self.spread = spread_km
        self.active_from = active_from              # seconds since T0
        self.active_until = active_until

    def position(self, elapsed_sec: float) -> tuple[float, float]:
        h = elapsed_sec / 3600.0
        return self.n0 + self.vn * h, self.e0 + self.ve * h

    def emit(self, rng, t_start: float, t_end: float) -> list:
        """Strikes generated in [t_start, t_end), as (t, lat, lon)."""
        out = []
        span = t_end - t_start
        count = self.rate * span / 60.0
        whole = int(count)
        if rng.random() < count - whole:
            whole += 1
        for _ in range(whole):
            t = rng.uniform(t_start, t_end)
            elapsed = t - T0
            if not (self.active_from <= elapsed < self.active_until):
                continue
            n, e = self.position(elapsed)
            n += rng.gauss(0, self.spread)
            e += rng.gauss(0, self.spread)
            lat, lon = km_offset(n, e)
            out.append((t, lat, lon))
        return out


def simulate(storms, duration_min=180, sensitivity="medium", seed=42):
    """Run the full perception + decision pipeline. Returns (timeline, machine).

    timeline entries are (elapsed_sec, committed_level, Observables).
    """
    rng = random.Random(seed)
    tracker = ObservableTracker()
    machine = ThreatStateMachine(get_preset(sensitivity))
    buffer: list = []
    timeline = []
    steps = int(duration_min * 60 / POLL_INTERVAL_STATIC)

    for step in range(steps):
        now = T0 + step * POLL_INTERVAL_STATIC
        for storm in storms:
            buffer.extend(storm.emit(rng, now - POLL_INTERVAL_STATIC, now))
        buffer = [s for s in buffer if s[0] >= now - WINDOW_MIN * 60]

        obs = tracker.observe(buffer, ORIGIN, now, RADIUS_KM)
        decision = machine.evaluate(obs, now)
        if decision is not None:
            machine.commit(decision, now)          # tests assume delivery succeeds
        timeline.append((now - T0, machine.level, obs))

    return timeline, machine


def levels(timeline) -> list:
    return [lvl for _, lvl, _ in timeline]


def describe(timeline) -> str:
    """Compact timeline for assertion failure messages."""
    rows = []
    prev = None
    for elapsed, lvl, obs in timeline:
        if lvl != prev:
            rows.append(f"t={elapsed/60:5.1f}m {LEVEL_NAMES[lvl]:<7} "
                        f"d10_s={obs.d10_s if obs.d10_s is None else round(obs.d10_s, 1)} "
                        f"r={obs.r_near:.2f} "
                        f"vc={obs.v_c_s if obs.v_c_s is None else round(obs.v_c_s, 1)} "
                        f"eta={obs.eta_min if obs.eta_min is None else round(obs.eta_min)}")
            prev = lvl
    return "\n" + "\n".join(rows)


# ── Regression: the two field-observed failures ───────────────────────────────

class PhantomClusterTest(unittest.TestCase):
    """A distant storm plus a handful of stray nearby strikes must not warn."""

    def test_phantom_cluster_never_warns(self):
        distant = Storm(north_km=0, east_km=62, rate_per_min=3.0, spread_km=6.0)
        # Two tight strays 20 km away — exactly what the old DBSCAN would have
        # labelled a "storm cell" with min_samples=2, collapsing the scalar.
        phantom = Storm(north_km=0, east_km=20, rate_per_min=0.15, spread_km=1.0,
                        active_from=30 * 60, active_until=50 * 60)

        timeline, machine = simulate([distant, phantom], duration_min=120)

        self.assertNotIn(LEVEL_WARNING, levels(timeline),
                         msg="stray strikes produced a WARNING" + describe(timeline))
        # The percentile must stay anchored on the real storm body.
        mid = [obs for elapsed, _, obs in timeline
               if 35 * 60 <= elapsed <= 50 * 60 and obs.d10_s is not None]
        self.assertTrue(mid, "no observables in the phantom window")
        self.assertTrue(all(o.d10_s > 40 for o in mid),
                        msg="d10 was dragged in by the strays" + describe(timeline))


class RecedingStormTest(unittest.TestCase):
    """A storm that comes close and then leaves must reach CLEAR even though it
    keeps producing strikes inside the radius the whole time."""

    def test_receding_storm_reaches_clear(self):
        approach = Storm(north_km=0, east_km=30, v_east=-20.0, rate_per_min=4.0,
                         active_until=40 * 60)
        # Same cell, now retreating from 20 km to ~90 km — past watch_exit but
        # deliberately still inside the radius, so the test proves the all-clear
        # is reached *while strikes are still arriving*.
        recede = Storm(north_km=0, east_km=20, v_east=+30.0, rate_per_min=4.0,
                       active_from=40 * 60)

        timeline, machine = simulate([approach, recede], duration_min=180)
        seq = levels(timeline)

        self.assertIn(LEVEL_WATCH, seq,
                      msg="storm never raised a WATCH" + describe(timeline))
        self.assertEqual(machine.level, LEVEL_CLEAR,
                         msg="all-clear never reached" + describe(timeline))
        # And it must clear while strikes are still being received.
        last = timeline[-1][2]
        self.assertGreater(last.strikes_total, 0,
                           "scenario degenerate: no strikes left to ignore")


# ── Behaviour on genuine storms ───────────────────────────────────────────────

class ApproachingStormTest(unittest.TestCase):
    def test_approaching_storm_escalates_to_warning(self):
        storm = Storm(north_km=0, east_km=70, v_east=-60.0, rate_per_min=5.0)
        timeline, machine = simulate([storm], duration_min=90)
        seq = levels(timeline)

        self.assertIn(LEVEL_WATCH, seq, msg="no WATCH" + describe(timeline))
        self.assertIn(LEVEL_WARNING, seq, msg="no WARNING" + describe(timeline))
        self.assertLess(seq.index(LEVEL_WATCH), seq.index(LEVEL_WARNING),
                        msg="escalated out of order" + describe(timeline))

    def test_warning_is_preceded_by_a_confirmed_approach(self):
        storm = Storm(north_km=0, east_km=70, v_east=-60.0, rate_per_min=5.0)
        timeline, _ = simulate([storm], duration_min=90)
        first_warn = next(i for i, (_, lvl, _) in enumerate(timeline)
                          if lvl == LEVEL_WARNING)
        obs = timeline[first_warn][2]
        self.assertIsNotNone(obs.v_c_s)
        self.assertGreater(obs.v_c_s, 0, msg="warned without a closing field")


class GrazingStormTest(unittest.TestCase):
    """A storm passing at 35 km should raise a WATCH and never a WARNING."""

    def test_grazing_storm_stays_at_watch(self):
        storm = Storm(north_km=35, east_km=70, v_east=-60.0, rate_per_min=4.0)
        timeline, _ = simulate([storm], duration_min=120)
        seq = levels(timeline)

        self.assertIn(LEVEL_WATCH, seq, msg="no WATCH" + describe(timeline))
        self.assertNotIn(LEVEL_WARNING, seq,
                         msg="grazing storm warned" + describe(timeline))


class QuietSkyTest(unittest.TestCase):
    def test_scattered_noise_stays_clear(self):
        noise = Storm(north_km=0, east_km=80, rate_per_min=0.2, spread_km=40.0)
        timeline, machine = simulate([noise], duration_min=120)
        self.assertEqual(set(levels(timeline)), {LEVEL_CLEAR},
                         msg="noise raised an alert" + describe(timeline))


# ── State machine mechanics ───────────────────────────────────────────────────

class DeliveryGatingTest(unittest.TestCase):
    """A failed Telegram send must not advance the level, and the same alert must
    be offered again on the next poll."""

    def _obs(self, d10=10.0, rate=2.0, vc=25.0, eta=15.0):
        return Observables(strikes_total=40, strikes_near=30, d10=d10, d10_s=d10,
                           r_near=rate, v_c=vc, v_c_s=vc, eta_min=eta,
                           bearing=90.0, has_data=True)

    def test_uncommitted_decision_is_retried(self):
        m = ThreatStateMachine(get_preset("medium"))
        obs = self._obs()
        now = T0
        first = None
        for step in range(8):
            now = T0 + step * POLL_INTERVAL_STATIC
            decision = m.evaluate(obs, now)
            if decision is not None:
                first = decision
                break
        self.assertIsNotNone(first, "never produced a decision")
        self.assertEqual(first.level, LEVEL_WARNING)
        self.assertEqual(m.level, LEVEL_CLEAR, "level advanced without a commit")

        again = m.evaluate(obs, now + POLL_INTERVAL_STATIC)
        self.assertEqual(again, first, "decision was not retried after a failed send")

        m.commit(again, now + POLL_INTERVAL_STATIC)
        self.assertEqual(m.level, LEVEL_WARNING)

    def test_periodic_realert_only_in_warning(self):
        m = ThreatStateMachine(get_preset("medium"), level=LEVEL_WARNING,
                               last_periodic_ts=T0)
        obs = self._obs()
        self.assertIsNone(m.evaluate(obs, T0 + 300), "re-alerted too early")
        decision = m.evaluate(obs, T0 + 601)
        self.assertEqual(decision, Decision(LEVEL_WARNING, periodic=True))
        m.commit(decision, T0 + 601)
        self.assertEqual(m.level, LEVEL_WARNING, "periodic re-alert moved the level")


class HysteresisTest(unittest.TestCase):
    def test_enter_and_exit_thresholds_are_separated(self):
        for name in ("low", "medium", "high"):
            p = get_preset(name)
            with self.subTest(preset=name):
                self.assertGreater(p.watch_exit_km, p.watch_enter_km)
                self.assertGreater(p.warn_exit_km, p.warn_enter_km)
                self.assertGreater(p.watch_rate, p.clear_rate)
                self.assertGreater(p.warn_rate, p.watch_rate)

    def test_every_level_has_an_exit(self):
        """No state may be a trap: with no activity at all, each level must
        eventually target a lower one."""
        empty = Observables(has_data=False)
        for level in (LEVEL_WATCH, LEVEL_WARNING):
            with self.subTest(level=LEVEL_NAMES[level]):
                m = ThreatStateMachine(get_preset("medium"), level=level)
                self.assertLess(m.target_level(empty), level)

    def test_distant_persistent_activity_targets_clear(self):
        """The exact configuration the old design could not leave."""
        far = Observables(strikes_total=60, strikes_near=50, d10=70.0, d10_s=70.0,
                          r_near=4.0, v_c=0.0, v_c_s=0.0, has_data=True)
        m = ThreatStateMachine(get_preset("medium"), level=LEVEL_WATCH)
        self.assertEqual(m.target_level(far), LEVEL_CLEAR)


class PersistenceTest(unittest.TestCase):
    def test_state_round_trip(self):
        m = ThreatStateMachine(get_preset("medium"), level=LEVEL_WARNING,
                               level_since=T0, last_periodic_ts=T0 + 5)
        restored = ThreatStateMachine.from_dict(get_preset("medium"), m.to_dict())
        self.assertEqual(restored.level, LEVEL_WARNING)
        self.assertEqual(restored.level_since, T0)
        self.assertEqual(restored.last_periodic_ts, T0 + 5)

    def test_tracker_round_trip(self):
        t = ObservableTracker(d10_s=12.5, v_c_s=-3.5)
        restored = ObservableTracker.from_dict(t.to_dict())
        self.assertAlmostEqual(restored.d10_s, 12.5)
        self.assertAlmostEqual(restored.v_c_s, -3.5)


# ── Geo / coverage ────────────────────────────────────────────────────────────

class GeoTest(unittest.TestCase):
    def test_distance_is_symmetric_and_sane(self):
        a, b = km_offset(0, 0), km_offset(0, 50)
        self.assertAlmostEqual(distance_km(*a, *b), 50.0, delta=0.5)
        self.assertAlmostEqual(distance_km(*a, *b), distance_km(*b, *a))

    def test_topic_ring_grows_with_radius(self):
        small = topics_for_area(*ORIGIN, 50)
        large = topics_for_area(*ORIGIN, 300)
        self.assertEqual(len(small), 9, "expected a 3x3 block for a small radius")
        self.assertGreater(len(large), len(small),
                           "a large radius must widen the subscribed ring")
        self.assertTrue(all(t.startswith("blitzortung/1.1/") for t in large))


if __name__ == "__main__":
    unittest.main()
