"""
tests/test_storm_front_position.py
───────────────────────────────────
The monitor's side of the dynamic origin: choosing where the radar is centred,
going blind when that is unknown, noticing a relocation, aiming the feed, and
wording the own-motion line.

    cd dradis && python3 -m unittest discover tests

aiomqtt is stubbed before the import under test, as in test_storm_front_manager.
"""

import asyncio
import sys
import tempfile
import types
import unittest
from unittest import mock

if "aiomqtt" not in sys.modules:
    stub = types.ModuleType("aiomqtt")

    class _Messages:
        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.sleep(3600)
            raise StopAsyncIteration

    class _Client:
        def __init__(self, *a, **kw):
            self.messages = _Messages()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def subscribe(self, *a, **kw):
            return None

    stub.Client = _Client
    sys.modules["aiomqtt"] = stub

from dradis.live_monitors import storm_front as SF                # noqa: E402
from dradis.live_monitors.blitzortung import BlitzortungFeed      # noqa: E402
from dradis.live_monitors.position_core import PositionState      # noqa: E402
from dradis.live_monitors.storm_front_core import (               # noqa: E402
    EVENT_ACTIVE, EVENT_IDLE, TRACK_CLOSING,
)

SF.STATE_PATH = tempfile.mktemp(suffix="-storm-front-position-state.json")

HOME = (40.85, 14.27)
POSITION_ID = "p1"
T0 = 1_700_000_000.0


def config(**overrides) -> dict:
    cfg = {
        "id": "sf1", "name": "Bacoli", "type": "storm_front", "enabled": True,
        "location": "Bacoli", "latitude": HOME[0], "longitude": HOME[1],
        "radius_km": 30, "ring_count": 4, "language": "it",
        "quiet_start": "", "quiet_end": "", "chart": False,
        "telegram_bot_id": "default",
    }
    cfg.update(overrides)
    return cfg


async def send_ok(text, photo=None):
    return True


def make_send(_cfg):
    return send_ok


def state(lat=41.10, lon=14.55, age=30.0, speed=None, course=None,
          accuracy=15.0, discontinuity=0) -> PositionState:
    return PositionState(
        lat=lat, lon=lon, t=T0 - age, age_sec=age, accuracy_m=accuracy,
        speed_kmh=speed, course_deg=course,
        moving=speed is not None and speed >= 15.0,
        discontinuity=discontinuity,
    )


class FakeManager:
    """Stands in for the singleton position manager."""

    def __init__(self, usable=None, name="My phone", max_age=900.0):
        self._usable = usable
        self._name = name
        self._max_age = max_age
        self.calls = []

    def usable(self, position_id, now=None, max_age_sec=None):
        self.calls.append((position_id, max_age_sec))
        return self._usable

    def current(self, position_id, now=None):
        return self._usable

    def max_age_sec(self, position_id):
        return self._max_age

    def name_of(self, position_id):
        return self._name


class OriginSelectionTest(unittest.TestCase):

    def _monitor(self, **kw):
        return SF.StormFrontLiveMonitor(config(**kw), send_ok, "Europe/Rome")

    def test_a_fixed_monitor_never_consults_the_position_manager(self):
        # A monitor that did not opt in must not even be a reason for the manager
        # to exist, let alone to connect.
        manager = FakeManager(usable=state())
        monitor = self._monitor()
        with mock.patch.object(SF, "position_manager", manager):
            self.assertEqual(monitor._resolve_origin(T0), HOME)
        self.assertEqual(manager.calls, [])

    def test_following_a_position_uses_its_coordinates(self):
        manager = FakeManager(usable=state(lat=41.10, lon=14.55))
        monitor = self._monitor(position_id=POSITION_ID)
        with mock.patch.object(SF, "position_manager", manager):
            self.assertEqual(monitor._resolve_origin(T0), (41.10, 14.55))

    def test_no_usable_fix_means_no_origin_at_all(self):
        # THE design point: there is no fallback. Watching the configured house
        # while the user is elsewhere answers a different question without saying
        # so, and None is how the monitor says "I do not know where I am".
        manager = FakeManager(usable=None)
        monitor = self._monitor(position_id=POSITION_ID)
        with mock.patch.object(SF, "position_manager", manager):
            self.assertIsNone(monitor._resolve_origin(T0))
        self.assertIsNone(monitor._motion)

    def test_an_open_event_buys_patience_with_a_stale_fix(self):
        # Losing GPS in a tunnel mid-storm must not blind the monitor while the
        # last known position is still the best evidence available.
        manager = FakeManager(usable=state(), max_age=900.0)
        monitor = self._monitor(position_id=POSITION_ID)
        monitor._tracker.event_state = EVENT_ACTIVE
        with mock.patch.object(SF, "position_manager", manager):
            monitor._resolve_origin(T0)
        self.assertEqual(manager.calls,
                         [(POSITION_ID, 900.0 * SF.EVENT_STALE_FACTOR)])

    def test_no_open_event_uses_the_normal_budget(self):
        manager = FakeManager(usable=state(), max_age=900.0)
        monitor = self._monitor(position_id=POSITION_ID)
        monitor._tracker.event_state = EVENT_IDLE
        with mock.patch.object(SF, "position_manager", manager):
            monitor._resolve_origin(T0)
        self.assertEqual(manager.calls, [(POSITION_ID, None)])

    def test_a_deleted_position_is_simply_unknown(self):
        # Deleting a position must not throw; the monitor goes blind, which is the
        # same honest answer as a phone that stopped reporting.
        manager = FakeManager(usable=None, name=None)
        monitor = self._monitor(position_id="gone")
        with mock.patch.object(SF, "position_manager", manager):
            self.assertIsNone(monitor._resolve_origin(T0))


class BlindnessTest(unittest.IsolatedAsyncioTestCase):
    """No position, no perception. The dangerous failure is announcing calm while
    blind, so that is what gets asserted."""

    def _monitor(self, manager):
        monitor = SF.StormFrontLiveMonitor(config(position_id=POSITION_ID),
                                           send_ok, "Europe/Rome")
        monitor._feed = mock.MagicMock()
        monitor._feed.strikes.return_value = []
        monitor._feed.feed_ok.return_value = True
        monitor._feed.connected_for.return_value = 1e9
        monitor._feed.stats.return_value = {"messages": 0}
        monitor._feed.is_running.return_value = True
        return monitor

    async def test_a_blind_tick_produces_no_alert(self):
        manager = FakeManager(usable=None)
        monitor = self._monitor(manager)
        sent = []
        monitor._send = lambda text, photo=None: sent.append(text)
        with mock.patch.object(SF, "position_manager", manager):
            await monitor._tick(notify=True)
        self.assertEqual(sent, [])

    async def test_a_blind_monitor_cannot_announce_an_all_clear(self):
        # The exact failure the freeze exists to prevent: a monitor that cannot
        # tell "nothing is happening" from "I cannot see" would cheerfully report
        # that the storm has cleared.
        manager = FakeManager(usable=None)
        monitor = self._monitor(manager)
        monitor._tracker.event_state = EVENT_ACTIVE
        monitor._tracker.notified_ring = 2
        monitor._tracker.fading_since = T0 - 100_000     # long past the dwell
        alerts = []
        monitor._dispatch = lambda *a, **kw: alerts.append(a)

        with mock.patch.object(SF, "position_manager", manager):
            for _ in range(5):
                await monitor._tick(notify=True)

        self.assertEqual(alerts, [])
        self.assertEqual(monitor._tracker.fading_since, 0.0)

    async def test_going_blind_is_recorded_once(self):
        manager = FakeManager(usable=None)
        monitor = self._monitor(manager)
        with mock.patch.object(SF, "position_manager", manager):
            await monitor._tick(notify=True)
            first = monitor._blind_since
            await monitor._tick(notify=True)
        self.assertTrue(first)
        self.assertEqual(monitor._blind_since, first)

    async def test_recovery_clears_the_blind_flag_and_the_stale_geometry(self):
        # The bearings from before the blackout were measured wherever the user
        # was then, which is not where they are now.
        manager = FakeManager(usable=None)
        monitor = self._monitor(manager)
        with mock.patch.object(SF, "position_manager", manager):
            await monitor._tick(notify=True)
            self.assertTrue(monitor._blind_since)

            monitor._tracker._history = [(T0, 10.0, 20.0, 1)]
            manager._usable = state()
            await monitor._tick(notify=True)

        self.assertEqual(monitor._blind_since, 0.0)
        self.assertEqual(monitor._tracker._history, [])

    async def test_an_open_event_survives_a_blackout(self):
        manager = FakeManager(usable=None)
        monitor = self._monitor(manager)
        monitor._tracker.event_state = EVENT_ACTIVE
        monitor._tracker.notified_ring = 2
        with mock.patch.object(SF, "position_manager", manager):
            await monitor._tick(notify=True)
        self.assertEqual(monitor._tracker.event_state, EVENT_ACTIVE)
        self.assertEqual(monitor._tracker.notified_ring, 2)


class LazyFeedTest(unittest.IsolatedAsyncioTestCase):
    """A monitor following a position has nothing to derive geohash topics from
    until the first fix, so the feed must not start before then."""

    async def asyncTearDown(self):
        for monitor in getattr(self, "_started", []):
            await monitor.aclose()

    async def test_a_following_monitor_does_not_start_the_feed_up_front(self):
        monitor = SF.StormFrontLiveMonitor(config(position_id=POSITION_ID),
                                           send_ok, "Europe/Rome")
        self._started = [monitor]
        with mock.patch.object(SF, "position_manager", FakeManager(usable=None)):
            monitor.start()
        self.assertFalse(monitor._feed.is_running())

    async def test_a_fixed_monitor_still_starts_the_feed_up_front(self):
        monitor = SF.StormFrontLiveMonitor(config(), send_ok, "Europe/Rome")
        self._started = [monitor]
        monitor.start()
        self.assertTrue(monitor._feed.is_running())

    async def test_the_first_fix_starts_the_feed(self):
        manager = FakeManager(usable=state(lat=41.10, lon=14.55))
        monitor = SF.StormFrontLiveMonitor(config(position_id=POSITION_ID),
                                           send_ok, "Europe/Rome")
        self._started = [monitor]
        with mock.patch.object(SF, "position_manager", manager):
            await monitor._ensure_feed((41.10, 14.55))
        self.assertTrue(monitor._feed.is_running())
        self.assertAlmostEqual(monitor._feed.lat, 41.10)


class OriginJumpTest(unittest.TestCase):

    def _monitor(self):
        monitor = SF.StormFrontLiveMonitor(config(position_id=POSITION_ID),
                                           send_ok, "Europe/Rome")
        monitor._last_origin = HOME
        monitor._last_origin_at = T0
        return monitor

    def test_the_first_poll_is_never_a_jump(self):
        monitor = self._monitor()
        monitor._last_origin = None
        self.assertFalse(monitor._origin_jumped((0.0, 0.0), T0))

    def test_ordinary_driving_is_not_a_jump(self):
        # 2 km in a minute is 120 km/h — a motorway, not a relocation.
        monitor = self._monitor()
        moved = (HOME[0] + 0.018, HOME[1])
        self.assertFalse(monitor._origin_jumped(moved, T0 + 60))

    def test_an_implausible_leap_is_a_jump(self):
        monitor = self._monitor()
        self.assertTrue(monitor._origin_jumped((42.85, 14.27), T0 + 60))

    def test_a_feed_discontinuity_is_a_jump(self):
        monitor = self._monitor()
        monitor._last_discontinuity = 3
        monitor._motion = state(discontinuity=4)
        self.assertTrue(monitor._origin_jumped(HOME, T0 + 60))

    def test_a_steady_discontinuity_counter_is_not_a_jump(self):
        monitor = self._monitor()
        monitor._last_discontinuity = 3
        monitor._motion = state(discontinuity=3)
        self.assertFalse(monitor._origin_jumped(HOME, T0 + 60))


def ring_alert(bearing_deg=0.0):
    from dradis.live_monitors.storm_front_core import RingAlert
    return RingAlert(ring=2, ring_count=4, ring_edge_km=19.5, front_km=18.0,
                     bearing_deg=bearing_deg, sector=0, strikes=22,
                     strikes_in_radius=40, track=TRACK_CLOSING)


class MotionLineTest(unittest.TestCase):

    def _monitor(self, motion, language="it"):
        monitor = SF.StormFrontLiveMonitor(
            config(position_id=POSITION_ID, language=language), send_ok,
            "Europe/Rome")
        monitor._motion = motion
        return monitor

    def test_a_stationary_user_gets_no_line(self):
        self.assertIsNone(self._monitor(state(speed=0.0))._motion_line(ring_alert()))

    def test_an_unknown_motion_gets_no_line(self):
        self.assertIsNone(self._monitor(None)._motion_line(ring_alert()))

    def test_a_speed_without_a_course_gets_no_line(self):
        # Below the noise floor there is a speed but no direction, and a line that
        # named one would be inventing it.
        monitor = self._monitor(state(speed=40.0, course=None))
        self.assertIsNone(monitor._motion_line(ring_alert()))

    def test_driving_at_the_storm_says_so(self):
        monitor = self._monitor(state(speed=95.0, course=10.0))
        line = monitor._motion_line(ring_alert(bearing_deg=0.0))
        self.assertIn("verso il temporale", line)
        self.assertIn("95", line)

    def test_driving_across_the_storm_does_not(self):
        monitor = self._monitor(state(speed=95.0, course=270.0))
        line = monitor._motion_line(ring_alert(bearing_deg=0.0))
        self.assertNotIn("verso il temporale", line)
        self.assertIn("In movimento", line)

    def test_the_boundary_is_the_tolerance(self):
        toward = self._monitor(state(speed=60.0, course=44.0))
        across = self._monitor(state(speed=60.0, course=46.0))
        self.assertIn("verso il temporale", toward._motion_line(ring_alert()))
        self.assertNotIn("verso il temporale", across._motion_line(ring_alert()))

    def test_english(self):
        monitor = self._monitor(state(speed=95.0, course=0.0), language="en")
        self.assertIn("heading towards the storm",
                      monitor._motion_line(ring_alert()))

    def test_the_line_appears_in_the_formatted_alert(self):
        monitor = self._monitor(state(speed=95.0, course=0.0))
        with mock.patch.object(SF, "position_manager", FakeManager()):
            self.assertIn("🚗", monitor._format(ring_alert()))

    def test_a_fixed_monitor_never_shows_it(self):
        monitor = SF.StormFrontLiveMonitor(config(), send_ok, "Europe/Rome")
        self.assertNotIn("🚗", monitor._format(ring_alert()))


class LocationLabelTest(unittest.TestCase):

    def test_a_fixed_monitor_is_titled_with_its_location(self):
        monitor = SF.StormFrontLiveMonitor(config(), send_ok, "Europe/Rome")
        self.assertEqual(monitor._plain_location(), "Bacoli")

    def test_following_a_position_is_titled_with_its_name(self):
        # With several phones in the house, the name is the only thing that says
        # where these distances were measured from.
        monitor = SF.StormFrontLiveMonitor(config(position_id=POSITION_ID),
                                           send_ok, "Europe/Rome")
        with mock.patch.object(SF, "position_manager",
                               FakeManager(name="Cellulare di Procolo")):
            self.assertEqual(monitor._plain_location(), "Cellulare di Procolo")
            self.assertIn("Cellulare di Procolo", monitor._format(ring_alert()))

    def test_a_deleted_position_falls_back_to_a_readable_title(self):
        monitor = SF.StormFrontLiveMonitor(config(position_id="gone"),
                                           send_ok, "Europe/Rome")
        with mock.patch.object(SF, "position_manager", FakeManager(name=None)):
            self.assertEqual(monitor._plain_location(), "Bacoli")


class FeedRetuneTest(unittest.IsolatedAsyncioTestCase):

    def _feed(self):
        return BlitzortungFeed(name="t", monitor_id="sf1", lat=HOME[0],
                               lon=HOME[1], coverage_radius_km=48.0,
                               window_sec=600.0)

    async def test_a_short_hop_changes_nothing(self):
        feed = self._feed()
        before = list(feed._topics)
        self.assertFalse(await feed.retune(HOME[0] + 0.05, HOME[1] + 0.05))
        self.assertEqual(feed._topics, before)

    async def test_the_origin_is_still_updated_on_a_short_hop(self):
        feed = self._feed()
        await feed.retune(HOME[0] + 0.05, HOME[1])
        self.assertAlmostEqual(feed.lat, HOME[0] + 0.05)

    async def test_a_long_journey_re_aims_the_subscription(self):
        feed = self._feed()
        before = list(feed._topics)
        self.assertTrue(await feed.retune(HOME[0] + 4.0, HOME[1] + 4.0))
        self.assertNotEqual(feed._topics, before)

    async def test_re_aiming_before_start_does_not_connect(self):
        feed = self._feed()
        await feed.retune(HOME[0] + 4.0, HOME[1] + 4.0)
        self.assertFalse(feed.is_running())

    async def test_the_strike_buffer_survives_a_re_aim(self):
        # The buffer holds absolute coordinates; they stay true wherever the
        # observer goes, and throwing them away would blind the monitor exactly
        # when it just changed area.
        feed = self._feed()
        feed._buffer = [(T0, 41.0, 14.5)]
        await feed.retune(HOME[0] + 4.0, HOME[1] + 4.0)
        self.assertEqual(len(feed._buffer), 1)


class FingerprintTest(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.manager = SF.StormFrontMonitorManager()

    async def asyncTearDown(self):
        for monitor in list(self.manager._monitors.values()):
            await monitor.aclose()
        self.manager.stop_all()

    async def test_changing_the_position_restarts_the_monitor(self):
        self.manager.reload([config()], make_send, "Europe/Rome")
        first = self.manager._monitors["sf1"]
        self.manager.reload([config(position_id=POSITION_ID)], make_send,
                            "Europe/Rome")
        self.assertIsNot(self.manager._monitors["sf1"], first)

    async def test_switching_between_two_positions_restarts_the_monitor(self):
        self.manager.reload([config(position_id="p1")], make_send, "Europe/Rome")
        first = self.manager._monitors["sf1"]
        self.manager.reload([config(position_id="p2")], make_send, "Europe/Rome")
        self.assertIsNot(self.manager._monitors["sf1"], first)

    async def test_the_default_is_the_historical_behaviour(self):
        self.manager.reload([config()], make_send, "Europe/Rome")
        self.assertEqual(self.manager._monitors["sf1"].position_id, "")


class MigrationTest(unittest.TestCase):
    """The unreleased v4.1.0 shape must not leave a monitor pointing at nothing."""

    def test_a_live_monitor_gets_the_new_position_id(self):
        configs, migrated = SF.migrate_position_source_configs(
            [{"id": "a", "position_source": "live"}], "p1")
        self.assertEqual(migrated, 1)
        self.assertEqual(configs[0]["position_id"], "p1")
        self.assertNotIn("position_source", configs[0])

    def test_a_fixed_monitor_stays_fixed(self):
        configs, migrated = SF.migrate_position_source_configs(
            [{"id": "a", "position_source": "fixed"}], "p1")
        self.assertEqual(migrated, 1)
        self.assertEqual(configs[0]["position_id"], "")

    def test_without_a_migrated_position_a_live_monitor_becomes_fixed(self):
        # Pointing at a source that does not exist would freeze it forever with no
        # visible cause; going back to its configured place is the honest outcome.
        configs, _ = SF.migrate_position_source_configs(
            [{"id": "a", "position_source": "live"}], None)
        self.assertEqual(configs[0]["position_id"], "")

    def test_it_is_idempotent(self):
        configs, _ = SF.migrate_position_source_configs(
            [{"id": "a", "position_source": "live"}], "p1")
        configs, migrated = SF.migrate_position_source_configs(configs, "p1")
        self.assertEqual(migrated, 0)
        self.assertEqual(configs[0]["position_id"], "p1")

    def test_an_untouched_monitor_is_not_counted(self):
        configs, migrated = SF.migrate_position_source_configs(
            [{"id": "a", "position_id": "p9"}], "p1")
        self.assertEqual(migrated, 0)
        self.assertEqual(configs[0]["position_id"], "p9")


if __name__ == "__main__":
    unittest.main()
