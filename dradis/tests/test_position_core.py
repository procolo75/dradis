"""
tests/test_position_core.py
────────────────────────────
Unit tests for the pure position core. No broker, no asyncio: every case here is
a sequence of published values and a clock.

The cases that matter are the ones where a naive reading would produce a
confident wrong answer — a fix assembled from two different moments, a course
invented out of GPS jitter, a mislocated report accepted as a 300 km journey.
"""

import unittest

from dradis.live_monitors.geo import offset_km
from dradis.live_monitors.position_core import (
    MAX_PLAUSIBLE_KMH, MOTION_MIN_DT_SEC, MOVING_MIN_KMH,
    FixHistory,
)

T0 = 1_700_000_000.0
HOME = (40.85, 14.27)


def feed(history: FixHistory, lat: float, lon: float, t: float) -> None:
    """Publish both components at the same instant, as statestream does."""
    history.set_latitude(lat, t)
    history.set_longitude(lon, t)


def at_km(north_km: float, east_km: float) -> tuple[float, float]:
    return offset_km(HOME[0], HOME[1], north_km, east_km)


class PairingTest(unittest.TestCase):

    def test_one_component_alone_is_not_a_fix(self):
        history = FixHistory()
        history.set_latitude(HOME[0], T0)
        self.assertIsNone(history.current(T0))

    def test_both_components_make_a_fix(self):
        history = FixHistory()
        feed(history, *HOME, T0)
        state = history.current(T0)
        self.assertIsNotNone(state)
        self.assertAlmostEqual(state.lat, HOME[0])
        self.assertAlmostEqual(state.lon, HOME[1])

    def test_components_too_far_apart_in_time_are_not_paired(self):
        # A latitude from now and a longitude from ten minutes ago describe a
        # point the user was never at.
        history = FixHistory()
        history.set_latitude(HOME[0], T0)
        history.set_longitude(HOME[1], T0 - 600)
        self.assertIsNone(history.current(T0))

    def test_a_late_second_component_repairs_the_pair(self):
        history = FixHistory()
        history.set_latitude(HOME[0], T0)
        history.set_longitude(HOME[1], T0 - 600)
        self.assertIsNone(history.current(T0))
        history.set_longitude(HOME[1], T0 + 1)
        self.assertIsNotNone(history.current(T0 + 1))

    def test_the_fix_is_dated_by_the_newer_component(self):
        history = FixHistory()
        history.set_latitude(HOME[0], T0)
        history.set_longitude(HOME[1], T0 + 5)
        self.assertEqual(history.current(T0 + 5).t, T0 + 5)


class AgeTest(unittest.TestCase):

    def test_age_grows_with_the_clock(self):
        history = FixHistory()
        feed(history, *HOME, T0)
        self.assertAlmostEqual(history.current(T0 + 120).age_sec, 120.0)

    def test_a_backdated_fix_is_old_the_moment_it_arrives(self):
        # This is the retained-message case: it lands now, but `last_updated` says
        # it is from an hour ago, and treating it as fresh is the whole failure.
        history = FixHistory()
        feed(history, *HOME, T0 - 3600)
        self.assertAlmostEqual(history.current(T0).age_sec, 3600.0)


class MotionTest(unittest.TestCase):

    def test_a_single_fix_yields_no_motion(self):
        history = FixHistory()
        feed(history, *HOME, T0)
        state = history.current(T0)
        self.assertIsNone(state.speed_kmh)
        self.assertIsNone(state.course_deg)
        self.assertFalse(state.moving)

    def test_fixes_closer_together_than_the_minimum_yield_no_motion(self):
        history = FixHistory()
        feed(history, *HOME, T0)
        feed(history, *at_km(0.5, 0.0), T0 + 10)
        self.assertIsNone(history.current(T0 + 10).speed_kmh)

    def test_driving_north_is_measured(self):
        history = FixHistory()
        # 60 km/h due north for four minutes: 1 km per minute.
        for minute in range(5):
            feed(history, *at_km(minute * 1.0, 0.0), T0 + minute * 60)
        state = history.current(T0 + 4 * 60)
        self.assertAlmostEqual(state.speed_kmh, 60.0, delta=1.0)
        self.assertAlmostEqual(state.course_deg, 0.0, delta=2.0)
        self.assertTrue(state.moving)

    def test_driving_east_is_measured(self):
        history = FixHistory()
        for minute in range(5):
            feed(history, *at_km(0.0, minute * 1.5), T0 + minute * 60)
        state = history.current(T0 + 4 * 60)
        self.assertAlmostEqual(state.speed_kmh, 90.0, delta=1.5)
        self.assertAlmostEqual(state.course_deg, 90.0, delta=2.0)

    def test_gps_jitter_is_stationary_and_has_no_course(self):
        # A phone on a table drifts a few metres. That displacement is real in the
        # data and meaningless in the world; a course derived from it would be
        # compared against a storm bearing.
        history = FixHistory()
        jitter = [(0.0, 0.0), (0.02, -0.01), (-0.015, 0.02), (0.01, 0.01),
                  (-0.02, -0.005)]
        for minute, (north, east) in enumerate(jitter):
            feed(history, *at_km(north, east), T0 + minute * 60)
        state = history.current(T0 + 4 * 60)
        self.assertEqual(state.speed_kmh, 0.0)
        self.assertIsNone(state.course_deg)
        self.assertFalse(state.moving)

    def test_a_parked_car_stops_reporting_the_speed_it_arrived_at(self):
        # Drive, then stand still while the position keeps being republished.
        # Dropping the duplicate reports would leave the newest usable pair
        # describing the last real movement — forever.
        history = FixHistory()
        for minute in range(5):
            feed(history, *at_km(minute * 1.0, 0.0), T0 + minute * 60)
        self.assertGreater(history.current(T0 + 4 * 60).speed_kmh, 50.0)
        for minute in range(5, 12):
            feed(history, *at_km(4.0, 0.0), T0 + minute * 60)
        self.assertEqual(history.current(T0 + 11 * 60).speed_kmh, 0.0)

    def test_motion_expires_when_the_newest_fix_goes_stale(self):
        # Statestream only publishes on change, so silence is not evidence of
        # anything. The last known speed must not survive it.
        history = FixHistory()
        for minute in range(5):
            feed(history, *at_km(minute * 1.0, 0.0), T0 + minute * 60)
        self.assertTrue(history.current(T0 + 4 * 60).moving)
        self.assertIsNone(history.current(T0 + 4 * 60 + 3600).speed_kmh)

    def test_walking_pace_is_not_moving(self):
        history = FixHistory()
        for minute in range(5):
            feed(history, *at_km(minute * 0.08, 0.0), T0 + minute * 60)
        state = history.current(T0 + 4 * 60)
        self.assertLess(state.speed_kmh, MOVING_MIN_KMH)
        self.assertFalse(state.moving)


class DiscontinuityTest(unittest.TestCase):

    def test_a_lone_wild_fix_does_not_move_the_observer(self):
        history = FixHistory()
        for minute in range(3):
            feed(history, *at_km(minute * 1.0, 0.0), T0 + minute * 60)
        feed(history, *at_km(300.0, 0.0), T0 + 3 * 60)      # 300 km in one minute
        state = history.current(T0 + 3 * 60)
        self.assertAlmostEqual(state.lat, at_km(2.0, 0.0)[0], places=4)
        self.assertTrue(history.has_pending_jump())

    def test_a_confirmed_jump_is_accepted_and_flagged(self):
        history = FixHistory()
        for minute in range(3):
            feed(history, *at_km(minute * 1.0, 0.0), T0 + minute * 60)
        before = history.current(T0 + 2 * 60).discontinuity

        feed(history, *at_km(300.0, 0.0), T0 + 3 * 60)
        feed(history, *at_km(300.2, 0.0), T0 + 4 * 60)      # the new place agrees

        state = history.current(T0 + 4 * 60)
        self.assertAlmostEqual(state.lat, at_km(300.2, 0.0)[0], places=4)
        self.assertGreater(state.discontinuity, before)
        self.assertFalse(history.has_pending_jump())

    def test_a_jump_wipes_the_motion_history(self):
        # The fixes before the jump describe a different place, so no speed may be
        # derived across it.
        history = FixHistory()
        for minute in range(5):
            feed(history, *at_km(minute * 1.0, 0.0), T0 + minute * 60)
        feed(history, *at_km(300.0, 0.0), T0 + 5 * 60)
        feed(history, *at_km(300.2, 0.0), T0 + 6 * 60)
        self.assertIsNone(history.current(T0 + 6 * 60).speed_kmh)

    def test_travel_just_under_the_ceiling_is_not_a_jump(self):
        history = FixHistory()
        feed(history, *HOME, T0)
        km = (MAX_PLAUSIBLE_KMH * 0.9) * (MOTION_MIN_DT_SEC / 3600.0)
        feed(history, *at_km(km, 0.0), T0 + MOTION_MIN_DT_SEC)
        state = history.current(T0 + MOTION_MIN_DT_SEC)
        self.assertAlmostEqual(state.lat, at_km(km, 0.0)[0], places=4)
        self.assertFalse(history.has_pending_jump())

    def test_reset_counts_as_a_discontinuity(self):
        history = FixHistory()
        feed(history, *HOME, T0)
        before = history.current(T0).discontinuity
        history.reset()
        self.assertIsNone(history.current(T0))
        feed(history, *HOME, T0 + 60)
        self.assertGreater(history.current(T0 + 60).discontinuity, before)


class AccuracyTest(unittest.TestCase):

    def test_accuracy_rides_along_with_the_fix(self):
        history = FixHistory()
        history.set_accuracy(12.0)
        feed(history, *HOME, T0)
        self.assertEqual(history.current(T0).accuracy_m, 12.0)

    def test_accuracy_is_optional(self):
        history = FixHistory()
        feed(history, *HOME, T0)
        self.assertIsNone(history.current(T0).accuracy_m)

    def test_accuracy_arriving_after_the_coordinates_still_counts(self):
        # On connect every retained message lands at once and in no particular
        # order. Without this, the accuracy threshold would never apply to the
        # first fix — the one the user sees in Test connection.
        history = FixHistory()
        feed(history, *HOME, T0)
        history.set_accuracy(12.0)
        self.assertEqual(history.current(T0).accuracy_m, 12.0)

    def test_a_fix_keeps_its_own_accuracy(self):
        history = FixHistory()
        history.set_accuracy(12.0)
        feed(history, *HOME, T0)
        history.set_accuracy(900.0)
        feed(history, *at_km(5.0, 0.0), T0 + 60)
        self.assertEqual(history.current(T0 + 60).accuracy_m, 900.0)


if __name__ == "__main__":
    unittest.main()
