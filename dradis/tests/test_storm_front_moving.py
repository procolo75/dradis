"""
tests/test_storm_front_moving.py
─────────────────────────────────
Scenarios where the OBSERVER moves. Everything here runs the real perception and
decision pipeline through `stormsim`; nothing about the algorithm is mocked.

    cd dradis && python3 -m unittest discover tests

The claim under test is that a moving origin needs no new decision logic. CBDR
compares bearings and ranges measured FROM THE ORIGIN, so letting the origin move
turns them into relative bearings and relative ranges — which is what the
mariner's rule was always defined on. These scenarios are the evidence: the
storms below are mostly PARKED, so any closing at all comes from the user's own
motion, and there is nowhere else for a correct verdict to come from.

The second half covers the part that is NOT free: telling a journey apart from a
change of reference frame.
"""

import sys
import types
import unittest

from tests import stormsim
from tests.stormsim import ORIGIN, driving, parked, run, teleporting

from dradis.live_monitors.storm_front_core import (
    EVENT_IDLE, TRACK_CLOSING, TRACK_GRAZING,
)

# aiomqtt is imported transitively by storm_front (feed + position). The stub only
# needs to exist; nothing here opens a connection.
if "aiomqtt" not in sys.modules:
    sys.modules["aiomqtt"] = types.ModuleType("aiomqtt")


class MovingOriginPlumbingTest(unittest.TestCase):
    """Before trusting any verdict, prove the moving-origin path is inert when the
    observer does not actually move."""

    def test_a_stationary_observer_matches_the_default(self):
        storms = [stormsim.head_on(distance_km=46.0, bearing_deg=0.0,
                                   speed_kmh=45.0)]
        baseline = run(storms, duration_min=90.0, seed=7)
        explicit = run([stormsim.head_on(distance_km=46.0, bearing_deg=0.0,
                                         speed_kmh=45.0)],
                       observer=ORIGIN, duration_min=90.0, seed=7)
        self.assertEqual(baseline.rings(), explicit.rings())
        self.assertEqual(len(baseline.clear_alerts()),
                         len(explicit.clear_alerts()))


class DrivingIntoAStormTest(unittest.TestCase):
    """A storm that does not move, and a user who drives straight at it."""

    def setUp(self):
        # Parked 42 km north; the observer leaves the origin heading due north.
        self.result = run([parked(north_km=42.0, east_km=0.0)],
                          observer=driving(bearing_deg=0.0, speed_kmh=60.0),
                          duration_min=60.0, seed=11)

    def test_the_rings_descend(self):
        rings = self.result.rings()
        self.assertTrue(rings, "no ring alert was produced")
        self.assertEqual(rings, sorted(rings))
        self.assertGreaterEqual(max(rings), 2)

    def test_the_verdict_is_closing(self):
        # The storm is stationary. The only thing closing the range is the car,
        # and the verdict has to say so.
        verdicts = [a.track for a in self.result.ring_alerts() if a.ring >= 2]
        self.assertIn(TRACK_CLOSING, verdicts)
        self.assertNotIn(TRACK_GRAZING, verdicts)

    def test_the_message_bound_holds(self):
        self.assertLessEqual(len(self.result.ring_alerts()), 4)
        self.assertLessEqual(len(self.result.clear_alerts()), 1)


class DrivingAwayFromAStormTest(unittest.TestCase):
    """The same storm, the opposite direction. Escalation must stop and the event
    must be able to close — the invariant that no state is a trap."""

    def setUp(self):
        self.result = run([parked(north_km=30.0, east_km=0.0)],
                          observer=driving(bearing_deg=180.0, speed_kmh=60.0),
                          duration_min=45.0, seed=11)

    def test_it_never_escalates_past_the_outer_ring(self):
        self.assertLessEqual(max(self.result.rings(), default=0), 1)

    def test_the_event_clears(self):
        self.assertEqual(len(self.result.clear_alerts()), 1)
        self.assertEqual(self.result.tracker.event_state, EVENT_IDLE)


class DrivingPastAStormTest(unittest.TestCase):
    """A parked storm off to the north-east and a user driving due north. The
    range closes, so the ring descends — but the bearing swings, so it is a pass,
    not a hit. Distance and bearing alone cannot tell these apart; that is the
    whole reason CBDR exists."""

    def setUp(self):
        self.result = run([parked(north_km=34.0, east_km=24.0)],
                          observer=driving(bearing_deg=0.0, speed_kmh=60.0),
                          duration_min=60.0, seed=11)

    def test_it_is_never_called_a_head_on(self):
        # Calling a glancing pass "closing" is the tolerable error; the reverse is
        # the dangerous one, so this is the assertion that must never soften.
        verdicts = [a.track for a in self.result.ring_alerts() if a.ring >= 2]
        self.assertNotIn(TRACK_CLOSING, verdicts)

    def test_the_pass_is_recognised(self):
        verdicts = [a.track for a in self.result.ring_alerts()]
        self.assertIn(TRACK_GRAZING, verdicts)


class OriginDiscontinuityTest(unittest.TestCase):
    """Being relocated is not travelling. The stored bearings were measured from
    somewhere else, so they must be dropped — but the event must survive, or one
    storm could emit a second full ladder of messages."""

    def _tracker_mid_storm(self):
        result = run([stormsim.head_on(distance_km=46.0, bearing_deg=0.0,
                                       speed_kmh=45.0)],
                     duration_min=45.0, seed=5)
        tracker = result.tracker
        self.assertGreater(tracker.notified_ring, 0)
        self.assertNotEqual(tracker.event_state, EVENT_IDLE)
        return tracker

    def test_the_geometry_history_is_dropped(self):
        tracker = self._tracker_mid_storm()
        self.assertTrue(tracker._history)
        tracker.reset_geometry_history()
        self.assertEqual(tracker._history, [])

    def test_the_event_and_the_message_ladder_survive(self):
        tracker = self._tracker_mid_storm()
        event_state = tracker.event_state
        notified = tracker.notified_ring
        current = tracker.current_ring

        tracker.reset_geometry_history()

        self.assertEqual(tracker.event_state, event_state)
        self.assertEqual(tracker.notified_ring, notified)
        self.assertEqual(tracker.current_ring, current)

    def test_no_verdict_is_drawn_across_a_reset(self):
        tracker = self._tracker_mid_storm()
        tracker.reset_geometry_history()
        verdict, _pass_bearing, _new_cell = tracker.track_verdict(
            stormsim.BASE_TS + 45 * 60.0, bearing=0.0, front_km=15.0)
        self.assertNotIn(verdict, (TRACK_CLOSING, TRACK_GRAZING))

    def test_a_teleported_observer_does_not_fabricate_a_storm(self):
        # Relocating 200 km away from a storm mid-event: whatever happens, the
        # message bound must hold and the monitor must not escalate into a place
        # that has no weather.
        result = run([parked(north_km=28.0, east_km=0.0)],
                     observer=teleporting(at_min=20.0, north_km=200.0,
                                          east_km=0.0),
                     duration_min=60.0, seed=11)
        self.assertLessEqual(len(result.ring_alerts()), 4)
        self.assertEqual(result.rings(), sorted(result.rings()))


if __name__ == "__main__":
    unittest.main()
