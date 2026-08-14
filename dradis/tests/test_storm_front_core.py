"""
tests/test_storm_front_core.py
───────────────────────────────
Algorithm tests for the storm front monitor.

    cd dradis && python3 -m unittest discover tests

Most of these run whole simulated storms through the real pipeline (see
stormsim.py). The two that matter most are the invariant tests: `test_message_
count_is_bounded` and `test_event_always_terminates` are what replace the
threshold tuning of the six previous generations, and `test_stationary_storm_
does_not_keep_talking` is a direct regression of the v3.3.0 field failure.
"""

import random
import unittest

from dradis.live_monitors import storm_front_core as C
from dradis.live_monitors.storm_front_core import (
    ClearAlert, Frame, RingAlert, SectorReading, StormFrontTracker,
    build_frame, front_of_sector, next_ring, ring_edges, ring_of,
    sector_delta, sector_of,
)
from tests import stormsim as S


def empty_frame(now: float) -> Frame:
    return Frame(now=now, active=(), dominant=None, has_activity=False)


# ── Scenarios ─────────────────────────────────────────────────────────────────

class DirectHitTest(unittest.TestCase):
    def test_every_ring_is_announced_exactly_once(self):
        for seed in range(1, 6):
            res = S.run([S.head_on(48, 315, 55, rate_per_min=15, spread_km=6,
                                   active_until=75)],
                        duration_min=150, seed=seed)
            self.assertEqual(res.rings(), [1, 2, 3, 4], f"seed {seed}")
            self.assertEqual(len(res.clear_alerts()), 1, f"seed {seed}")
            self.assertIs(res.alerts[-1].__class__, ClearAlert)

    def test_a_storm_coming_at_you_is_never_called_grazing(self):
        """The dangerous error: telling the user it will miss when it will not."""
        for bearing, speed, spread in ((315, 55, 6), (45, 50, 5),
                                       (180, 35, 7), (270, 45, 8)):
            for seed in range(1, 11):
                res = S.run([S.head_on(46, bearing, speed, rate_per_min=15,
                                       spread_km=spread, active_until=85)],
                            duration_min=160, seed=seed)
                verdicts = [a.track for a in res.ring_alerts()]
                self.assertNotIn(C.TRACK_GRAZING, verdicts,
                                 f"bearing={bearing} seed={seed} → {verdicts}")

    def test_the_innermost_ring_is_flagged(self):
        res = S.run([S.head_on(48, 315, 55, rate_per_min=15, spread_km=6,
                               active_until=75)], duration_min=150, seed=3)
        self.assertTrue(res.ring_alerts()[-1].is_innermost)
        self.assertTrue(res.ring_alerts()[0].is_first)


class GrazingStormTest(unittest.TestCase):
    def test_a_passing_storm_is_recognised_as_passing(self):
        detected = 0
        for miss, speed, spread in ((22, 55, 6), (16, 60, 5), (25, 50, 6)):
            for seed in range(1, 11):
                res = S.run([S.crossing(miss, -45, speed, rate_per_min=16,
                                        spread_km=spread)],
                            duration_min=150, seed=seed)
                verdicts = [a.track for a in res.ring_alerts()]
                if C.TRACK_GRAZING in verdicts:
                    detected += 1
                # Never claim a collision course once past the outermost ring.
                for alert in res.ring_alerts():
                    if alert.ring >= 2:
                        self.assertNotEqual(alert.track, C.TRACK_CLOSING,
                                            f"miss={miss} seed={seed}")
        self.assertGreaterEqual(detected, 24, "grazing detection rate too low")

    def test_a_grazing_storm_never_reaches_the_innermost_ring(self):
        for seed in range(1, 6):
            res = S.run([S.crossing(25, -50, 50, rate_per_min=20, spread_km=6)],
                        duration_min=150, seed=seed)
            self.assertLessEqual(max(res.rings(), default=0), 3)


class StationaryStormTest(unittest.TestCase):
    def test_stationary_storm_does_not_keep_talking(self):
        """Regression of the v3.3.0 field failure.

        A weak but persistent cell parked between the old enter/exit thresholds
        used to re-alert every 10 minutes for hours — some 24 messages over the
        four hours simulated here. Now it announces the rings it genuinely
        occupies, which settles within the first couple of hours as the analysis
        window fills, and then goes quiet for good.
        """
        for seed in range(1, 8):
            res = S.run([S.Storm(25, 0, rate_per_min=6, spread_km=5)],
                        duration_min=240, seed=seed)
            self.assertLessEqual(len(res.ring_alerts()), 2, f"seed {seed}")
            self.assertEqual(len(res.clear_alerts()), 0)
            # Silence for the whole second half — no periodic re-alerting.
            self.assertTrue(all(m < 120 for m in res.alert_minutes),
                            f"seed {seed}: late alerts at {res.alert_minutes}")

    def test_a_front_oscillating_on_a_ring_edge_does_not_flap(self):
        for seed in range(1, 6):
            res = S.run([S.Storm(21, 0, rate_per_min=14, spread_km=4)],
                        duration_min=120, seed=seed)
            self.assertLessEqual(len(res.ring_alerts()), 2, f"seed {seed}")


class MultipleCellsTest(unittest.TestCase):
    def test_two_cells_never_double_notify(self):
        for seed in range(1, 8):
            res = S.run([S.Storm(10, 0, rate_per_min=12, spread_km=4, active_from=20),
                         S.Storm(-18, -18, rate_per_min=15, spread_km=5)],
                        duration_min=200, seed=seed)
            rings = res.rings()
            self.assertLessEqual(len(rings), 4, f"seed {seed}")
            self.assertEqual(len(rings), len(set(rings)), f"repeat ring: {rings}")
            self.assertEqual(rings, sorted(rings), f"non-monotone: {rings}")

    def test_other_cells_ride_along_as_secondary(self):
        res = S.run([S.Storm(10, 0, rate_per_min=12, spread_km=4, active_from=20),
                     S.Storm(-18, -18, rate_per_min=15, spread_km=5)],
                    duration_min=200, seed=2)
        self.assertTrue(any(a.secondary for a in res.ring_alerts()))


class QuietSkyTest(unittest.TestCase):
    def test_scattered_noise_opens_no_event(self):
        for seed in range(1, 6):
            res = S.run([S.Storm(0, 0, rate_per_min=0.2, spread_km=40)],
                        duration_min=180, seed=seed)
            self.assertEqual(res.alerts, [])
            self.assertEqual(res.tracker.event_state, C.EVENT_IDLE)

    def test_a_handful_of_strikes_opens_no_event(self):
        for seed in range(1, 6):
            res = S.run([S.Storm(10, 0, rate_per_min=0.3, spread_km=1)],
                        duration_min=180, seed=seed)
            self.assertEqual(res.alerts, [])


# ── Invariants ────────────────────────────────────────────────────────────────

class InvariantATest(unittest.TestCase):
    """At most ring_count ring messages + 1 all-clear per event, for ANY input."""

    def test_message_count_is_bounded(self):
        rng = random.Random(20260814)
        for case in range(30):
            for ring_count in (2, 3, 4):
                storms = [S.Storm(
                    north_km=rng.uniform(-45, 45), east_km=rng.uniform(-45, 45),
                    v_north=rng.uniform(-60, 60), v_east=rng.uniform(-60, 60),
                    rate_per_min=rng.uniform(1, 40), spread_km=rng.uniform(2, 12),
                    active_from=rng.uniform(0, 40),
                    active_until=rng.uniform(50, 200),
                ) for _ in range(rng.randint(1, 3))]
                res = S.run(storms, duration_min=220, ring_count=ring_count,
                            seed=rng.randrange(10 ** 6))

                # Per event: rings strictly increase, clears close them.
                rings_this_event, clears = [], 0
                for alert in res.alerts:
                    if isinstance(alert, RingAlert):
                        self.assertTrue(not rings_this_event
                                        or alert.ring > rings_this_event[-1],
                                        f"case {case}: non-increasing {rings_this_event}"
                                        f" then {alert.ring}")
                        rings_this_event.append(alert.ring)
                        self.assertLessEqual(len(rings_this_event), ring_count)
                        self.assertLessEqual(alert.ring, ring_count)
                    else:
                        clears += 1
                        rings_this_event = []
                self.assertLessEqual(clears, len(
                    [a for a in res.alerts if isinstance(a, RingAlert)]) + 1)


class InvariantBTest(unittest.TestCase):
    """Every state has a reachable exit — no latch, ever."""

    def test_event_always_terminates_once_the_sky_goes_quiet(self):
        for seed in range(1, 8):
            res = S.run([S.head_on(46, 315, 50, rate_per_min=18, spread_km=6,
                                   active_until=60)],
                        duration_min=140, seed=seed)
            self.assertEqual(res.tracker.event_state, C.EVENT_IDLE, f"seed {seed}")
            self.assertEqual(len(res.clear_alerts()), 1, f"seed {seed}")
            self.assertEqual(res.tracker.notified_ring, 0)
            self.assertEqual(res.tracker.current_ring, 0)

            # The all-clear must land within the window + dwell + confirmation.
            last_ring_min = max(m for m, a in zip(res.alert_minutes, res.alerts)
                                if isinstance(a, RingAlert))
            clear_min = res.alert_minutes[-1]
            budget = 60 + C.WINDOW_MIN + C.CLEAR_DWELL_SEC / 60 + 2
            self.assertLess(clear_min - last_ring_min, budget)

    def test_no_state_is_a_trap(self):
        """Exhaustive: from every reachable combination of state variables, an
        empty sky must lead back to IDLE with at most one all-clear."""
        now = 1_700_000_000.0
        for event_state in (C.EVENT_IDLE, C.EVENT_ACTIVE, C.EVENT_FADING):
            for current_ring in range(0, 5):
                for notified_ring in range(0, 5):
                    tracker = StormFrontTracker(30.0, 4)
                    tracker.event_state      = event_state
                    tracker.current_ring     = current_ring
                    tracker.notified_ring    = notified_ring
                    tracker.event_started_at = now - 1800
                    tracker.fading_since     = now - 60 if event_state == C.EVENT_FADING else 0.0

                    clears = 0
                    for step in range(60):
                        t = now + step * C.POLL_INTERVAL_SEC
                        alert = tracker.evaluate(empty_frame(t), t, True, 1e9)
                        if alert is not None:
                            self.assertIsInstance(alert, ClearAlert)
                            clears += 1
                            tracker.commit(alert, t)
                    label = f"{event_state}/{current_ring}/{notified_ring}"
                    self.assertEqual(tracker.event_state, C.EVENT_IDLE, label)
                    self.assertLessEqual(clears, 1, label)
                    self.assertEqual(tracker.notified_ring, 0, label)


# ── Robustness ────────────────────────────────────────────────────────────────

class OutlierTest(unittest.TestCase):
    def test_two_mislocated_strikes_cannot_pull_the_front_in(self):
        distances = sorted([3.0, 3.2] + [25.0] * 30)
        self.assertGreaterEqual(front_of_sector(distances), 20.0)

    def test_a_lone_strike_does_not_make_a_sector_active(self):
        self.assertIsNone(front_of_sector([3.0]))
        self.assertIsNone(front_of_sector([3.0, 3.1, 3.2]))
        self.assertIsNotNone(front_of_sector([3.0, 3.1, 3.2, 3.3]))

    def test_an_isolated_strike_does_not_open_an_event(self):
        """One strike 3 km away in an otherwise empty sector must be invisible."""
        origin, now = S.ORIGIN, S.BASE_TS
        far = [S.offset_km(*origin, 24.0, i * 0.4) for i in range(20)]
        strikes = [(now - 60, lat, lon) for lat, lon in far]
        strikes.append((now - 30, *S.offset_km(*origin, 3.0, 0.0)))
        frame = build_frame(strikes, origin, now, 30.0, 48.0, 600.0)
        self.assertIsNotNone(frame.dominant)
        self.assertGreater(frame.dominant.front_km, 15.0)


class FeedHealthTest(unittest.TestCase):
    def test_an_outage_never_produces_a_false_all_clear(self):
        for seed in range(1, 5):
            res = S.run([S.head_on(48, 315, 55, rate_per_min=15, spread_km=6,
                                   active_until=70)],
                        duration_min=160, seed=seed,
                        feed_ok_fn=lambda m: not (60 <= m < 90),
                        connected_for_fn=lambda m: (0.0 if 60 <= m < 90
                                                    else 1e9 if m < 60
                                                    else (m - 90) * 60))
            during = [m for m, a in zip(res.alert_minutes, res.alerts)
                      if 60 <= m < 90 and isinstance(a, ClearAlert)]
            self.assertEqual(during, [], f"seed {seed}: all-clear while deaf")
            self.assertEqual(len(res.clear_alerts()), 1, f"seed {seed}")

    def test_no_all_clear_before_the_connection_has_warmed_up(self):
        now = 1_700_000_000.0
        tracker = StormFrontTracker(30.0, 4)
        tracker.event_state      = C.EVENT_FADING
        tracker.notified_ring    = 2
        tracker.current_ring     = 2
        tracker.event_started_at = now - 3600
        tracker.fading_since     = now - C.CLEAR_DWELL_SEC - 60
        self.assertIsNone(tracker.evaluate(empty_frame(now), now, True,
                                           C.WARMUP_SEC - 1))
        self.assertIsInstance(tracker.evaluate(empty_frame(now), now, True,
                                               C.WARMUP_SEC + 1), ClearAlert)


class DeliveryTest(unittest.TestCase):
    def test_an_uncommitted_alert_is_retried(self):
        res = S.run([S.head_on(48, 315, 55, rate_per_min=15, spread_km=6)],
                    duration_min=60, seed=4, commit=False)
        self.assertGreater(len(res.ring_alerts()), 1)
        # Without commit the same ring keeps being offered.
        self.assertEqual(res.ring_alerts()[0].ring, res.ring_alerts()[1].ring)

    def test_a_retried_alert_describes_the_current_ring(self):
        """A failed send must not freeze a stale message: the next poll rebuilds
        the alert from the frame as it is now."""
        now = 1_700_000_000.0
        tracker = StormFrontTracker(30.0, 4)
        tracker.event_state = C.EVENT_ACTIVE
        tracker.event_started_at = now

        def frame_at(t, front):
            reading = SectorReading(sector=0, count=30, front_km=front, bearing_deg=0.0)
            return Frame(now=t, active=(reading,), dominant=reading,
                         strikes_in_radius=30, has_activity=True,
                         track_bearing=0.0, track_count=30)

        first = None
        for step in range(4):
            t = now + step * 60
            first = tracker.evaluate(frame_at(t, 25.0), t, True, 1e9) or first
        self.assertEqual(first.ring, 1)

        # Never committed; the storm has meanwhile reached ring 3.
        for step in range(4, 8):
            t = now + step * 60
            later = tracker.evaluate(frame_at(t, 10.0), t, True, 1e9)
        self.assertEqual(later.ring, 3)
        self.assertAlmostEqual(later.front_km, 10.0)


class PersistenceTest(unittest.TestCase):
    def test_round_trip_preserves_the_event(self):
        res = S.run([S.head_on(48, 315, 55, rate_per_min=15, spread_km=6)],
                    duration_min=45, seed=5)
        original = res.tracker
        restored = StormFrontTracker.from_dict(30.0, 4, original.to_dict())
        self.assertEqual(restored.event_state, original.event_state)
        self.assertEqual(restored.notified_ring, original.notified_ring)
        self.assertEqual(restored.current_ring, original.current_ring)
        self.assertEqual(len(restored._history), len(original._history))

    def test_a_restart_does_not_re_announce_a_ring(self):
        now = 1_700_000_000.0
        source = StormFrontTracker(30.0, 4)
        source.event_state      = C.EVENT_ACTIVE
        source.current_ring     = 3
        source.notified_ring    = 3
        source.event_started_at = now - 1200
        tracker = StormFrontTracker.from_dict(30.0, 4, source.to_dict())

        def frame_at(t, front):
            reading = SectorReading(sector=0, count=30, front_km=front, bearing_deg=0.0)
            return Frame(now=t, active=(reading,), dominant=reading,
                         strikes_in_radius=30, has_activity=True,
                         track_bearing=0.0, track_count=30)

        for step in range(4):                       # still ring 3 — silence
            t = now + step * 60
            self.assertIsNone(tracker.evaluate(frame_at(t, 10.0), t, True, 1e9))
        alerts = []
        for step in range(4, 9):                    # now ring 4 — one message
            t = now + step * 60
            alert = tracker.evaluate(frame_at(t, 4.0), t, True, 1e9)
            if alert:
                alerts.append(alert)
                tracker.commit(alert, t)
        self.assertEqual([a.ring for a in alerts], [4])

    def test_a_ring_deeper_than_the_ladder_is_clamped(self):
        restored = StormFrontTracker.from_dict(30.0, 2, {
            "event_state": C.EVENT_ACTIVE, "current_ring": 4, "notified_ring": 4,
        })
        self.assertLessEqual(restored.current_ring, 2)
        self.assertLessEqual(restored.notified_ring, 2)


# ── Grid mechanics ────────────────────────────────────────────────────────────

class RingGeometryTest(unittest.TestCase):
    def test_edges_are_proportional_and_ordered(self):
        for radius in (10.0, 30.0, 60.0):
            for count in (2, 3, 4):
                edges = ring_edges(radius, count)
                self.assertEqual(len(edges), count)
                self.assertAlmostEqual(edges[0], radius)
                self.assertEqual(edges, sorted(edges, reverse=True))
                self.assertTrue(all(e > 0 for e in edges))

    def test_ring_of_round_trips_at_every_edge(self):
        edges = ring_edges(30.0, 4)
        self.assertEqual(ring_of(30.01, edges), 0)
        for k, edge in enumerate(edges, start=1):
            self.assertEqual(ring_of(edge, edges), k)
            self.assertEqual(ring_of(edge - 0.01, edges), k)

    def test_hysteresis_holds_the_ring_until_the_margin_is_passed(self):
        edges = ring_edges(30.0, 4)
        self.assertEqual(next_ring(20.0, 2, edges), 2)   # inside the margin — held
        self.assertEqual(next_ring(23.0, 2, edges), 1)   # past it — released
        self.assertEqual(next_ring(11.0, 2, edges), 3)   # closing — immediate

    def test_radius_and_ring_count_are_clamped(self):
        self.assertEqual(C.clamp_radius(100.0), C.MAX_RADIUS_KM)
        self.assertEqual(C.clamp_radius(2.0), C.MIN_RADIUS_KM)
        self.assertEqual(C.clamp_radius("bad"), 30.0)
        self.assertEqual(C.clamp_ring_count(9), C.DEFAULT_RING_COUNT)
        self.assertEqual(C.clamp_ring_count(3), 3)


class SectorGridTest(unittest.TestCase):
    def test_sector_boundaries(self):
        self.assertEqual(sector_of(0.0), 0)
        self.assertEqual(sector_of(359.9), 0)
        self.assertEqual(sector_of(14.9), 0)
        self.assertEqual(sector_of(15.1), 1)
        self.assertEqual(sector_of(345.1), 0)
        self.assertTrue(all(0 <= sector_of(a) < C.SECTOR_COUNT
                            for a in range(0, 360)))

    def test_sector_delta_is_signed_and_shortest(self):
        self.assertEqual(sector_delta(0, 11), 1)
        self.assertEqual(sector_delta(11, 0), -1)
        self.assertEqual(sector_delta(3, 3), 0)
        for a in range(12):
            for b in range(12):
                self.assertLessEqual(abs(sector_delta(a, b)), 6)

    def test_angle_delta_wraps(self):
        self.assertAlmostEqual(C.angle_delta(10, 350), 20)
        self.assertAlmostEqual(C.angle_delta(350, 10), -20)

    def test_mean_bearing_crosses_the_seam(self):
        self.assertAlmostEqual(C.mean_bearing([350.0, 10.0]), 0.0, places=6)


class BinningTest(unittest.TestCase):
    def test_binning_is_linear_in_the_strike_count(self):
        """Guards against reintroducing the O(n²) that used to block the event
        loop for seconds during severe storms."""
        origin, now = S.ORIGIN, S.BASE_TS
        rng = random.Random(1)
        strikes = [(now - rng.random() * 500,
                    *S.offset_km(*origin, rng.uniform(-40, 40), rng.uniform(-40, 40)))
                   for _ in range(20000)]

        calls = 0
        real = C.distance_km

        def counting(*args):
            nonlocal calls
            calls += 1
            return real(*args)

        C.distance_km = counting
        try:
            frame = build_frame(strikes, origin, now, 30.0, 48.0, 600.0)
        finally:
            C.distance_km = real
        self.assertEqual(calls, len(strikes))
        self.assertGreater(frame.strikes_observed, 0)


if __name__ == "__main__":
    unittest.main()
