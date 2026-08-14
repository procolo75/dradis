"""
tests/test_storm_front_manager.py
──────────────────────────────────
Lifecycle, quiet hours, formatting and migration for the storm front monitor.

    cd dradis && python3 -m unittest discover tests

aiomqtt is stubbed in sys.modules BEFORE importing the monitor, so the feed task
stays alive without touching the network, and STATE_PATH is redirected to a
tempfile so the tests never write to /data.
"""

import asyncio
import sys
import tempfile
import types
import unittest
from unittest import mock

# ── aiomqtt stub, installed before the import under test ──────────────────────

if "aiomqtt" not in sys.modules:
    stub = types.ModuleType("aiomqtt")

    class _Messages:
        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.sleep(3600)       # never yields; keeps the task alive
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

        async def unsubscribe(self, *a, **kw):
            return None

    stub.Client = _Client
    stub.MqttError = Exception
    sys.modules["aiomqtt"] = stub

from dradis.live_monitors import storm_front as SF                # noqa: E402
from dradis.live_monitors.storm_front_core import (                # noqa: E402
    ClearAlert, RingAlert, SectorReading, TRACK_CLOSING, TRACK_GRAZING,
    TRACK_UNKNOWN,
)

SF.STATE_PATH = tempfile.mktemp(suffix="-storm-front-state.json")


def config(**overrides) -> dict:
    cfg = {
        "id": "sf1", "name": "Bacoli", "type": "storm_front", "enabled": True,
        "location": "Bacoli", "latitude": 40.85, "longitude": 14.27,
        "radius_km": 30, "ring_count": 4, "language": "it",
        "quiet_start": "", "quiet_end": "", "chart": False,
        "telegram_bot_id": "default",
    }
    cfg.update(overrides)
    return cfg


async def send_ok(text, photo=None):
    return True


def make_send(_cfg):
    """The manager is handed a FACTORY; the monitor is handed what it returns."""
    return send_ok


def ring_alert(ring=2, ring_edge=19.5, front=18.0, track=TRACK_CLOSING, **kw):
    params = dict(
        ring=ring, ring_count=4, ring_edge_km=ring_edge, front_km=front,
        bearing_deg=315.0, sector=10, strikes=22, strikes_in_radius=40,
        track=track, is_innermost=ring >= 4,
    )
    params.update(kw)
    return RingAlert(**params)


class ManagerLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.manager = SF.StormFrontMonitorManager()

    async def asyncTearDown(self):
        for monitor in list(self.manager._monitors.values()):
            await monitor.aclose()
        self.manager.stop_all()

    async def test_an_unrelated_save_does_not_restart_the_monitor(self):
        self.manager.reload([config()], make_send, "Europe/Rome")
        first = self.manager._monitors["sf1"]
        await asyncio.sleep(0)
        self.manager.reload(
            [config(), {"id": "f1", "type": "football_betting", "enabled": True}],
            make_send, "Europe/Rome")
        self.assertIs(self.manager._monitors["sf1"], first)

    async def test_state_survives_an_unrelated_save(self):
        self.manager.reload([config()], make_send, "Europe/Rome")
        monitor = self.manager._monitors["sf1"]
        monitor._tracker.notified_ring = 3
        self.manager.reload([config()], make_send, "Europe/Rome")
        self.assertEqual(self.manager._monitors["sf1"]._tracker.notified_ring, 3)

    async def test_changed_settings_restart_the_monitor(self):
        for field, value in (("radius_km", 40), ("ring_count", 2),
                             ("chart", True), ("location", "Napoli")):
            with self.subTest(field=field):
                manager = SF.StormFrontMonitorManager()
                manager.reload([config()], make_send, "Europe/Rome")
                first = manager._monitors["sf1"]
                manager.reload([config(**{field: value})], make_send, "Europe/Rome")
                self.assertIsNot(manager._monitors["sf1"], first)
                for monitor in list(manager._monitors.values()):
                    await monitor.aclose()

    async def test_timezone_change_restarts_the_monitor(self):
        self.manager.reload([config()], make_send, "Europe/Rome")
        first = self.manager._monitors["sf1"]
        self.manager.reload([config()], make_send, "UTC")
        self.assertIsNot(self.manager._monitors["sf1"], first)

    async def test_a_disabled_monitor_is_removed(self):
        self.manager.reload([config()], make_send, "Europe/Rome")
        self.manager.reload([config(enabled=False)], make_send, "Europe/Rome")
        self.assertEqual(self.manager._monitors, {})
        self.assertEqual(self.manager.status("sf1"), "stopped")

    async def test_other_types_are_ignored(self):
        self.manager.reload([config(type="seismic"), config(id="x", type="lightning")],
                            make_send, "Europe/Rome")
        self.assertEqual(self.manager._monitors, {})

    async def test_status_reports_stopped_when_not_running(self):
        self.assertEqual(self.manager.status("nope"), "stopped")


class RadiusClampTest(unittest.TestCase):
    def test_the_shared_payload_default_is_clamped(self):
        monitor = SF.StormFrontLiveMonitor(config(radius_km=100), send_ok, "UTC")
        self.assertEqual(monitor.radius_km, 60.0)

    def test_a_tiny_radius_is_clamped(self):
        monitor = SF.StormFrontLiveMonitor(config(radius_km=2), send_ok, "UTC")
        self.assertEqual(monitor.radius_km, 10.0)

    def test_an_unknown_ring_count_falls_back(self):
        monitor = SF.StormFrontLiveMonitor(config(ring_count=9), send_ok, "UTC")
        self.assertEqual(monitor.ring_count, 4)

    def test_rings_are_derived_from_the_radius(self):
        monitor = SF.StormFrontLiveMonitor(config(radius_km=30), send_ok, "UTC")
        self.assertEqual([round(e, 1) for e in monitor.edges], [30.0, 19.5, 12.0, 6.0])


class QuietHoursTest(unittest.TestCase):
    def _monitor(self, **kw):
        return SF.StormFrontLiveMonitor(config(**kw), send_ok, "Europe/Rome")

    def test_no_window_means_never_quiet(self):
        self.assertFalse(self._monitor()._in_quiet_hours())

    def test_a_window_crossing_midnight(self):
        monitor = self._monitor(quiet_start="23:00", quiet_end="07:00")
        with mock.patch.object(SF, "datetime") as fake:
            for hour, expected in ((23, True), (2, True), (6, True),
                                   (7, False), (12, False)):
                fake.now.return_value.time.return_value.replace.return_value = \
                    SF.time_t(hour, 0)
                self.assertEqual(monitor._in_quiet_hours(), expected, f"{hour}:00")

    def test_outer_rings_and_the_all_clear_are_silenceable(self):
        monitor = self._monitor()
        self.assertTrue(monitor._is_silenceable(ring_alert(ring=1, ring_edge=30.0)))
        self.assertTrue(monitor._is_silenceable(ring_alert(ring=2, ring_edge=19.5)))
        self.assertTrue(monitor._is_silenceable(
            ClearAlert(ring_count=4, radius_km=30.0, closest_km=5.0, closest_ring=4,
                       closest_at=0.0, quiet_sec=600.0, event_duration_sec=3600.0)))

    def test_close_rings_always_get_through(self):
        monitor = self._monitor()
        self.assertFalse(monitor._is_silenceable(ring_alert(ring=3, ring_edge=12.0)))
        self.assertFalse(monitor._is_silenceable(ring_alert(ring=4, ring_edge=6.0)))


class DispatchTest(unittest.IsolatedAsyncioTestCase):
    def _monitor(self, send, **kw):
        return SF.StormFrontLiveMonitor(config(**kw), send, "Europe/Rome")

    async def test_a_failed_send_does_not_commit(self):
        async def failing(text, photo=None):
            return False
        monitor = self._monitor(failing)
        alert = ring_alert(ring=2)
        await monitor._dispatch(alert, None, [], 1_700_000_000.0)
        self.assertEqual(monitor._tracker.notified_ring, 0)

    async def test_a_confirmed_send_commits(self):
        async def ok(text, photo=None):
            return True
        monitor = self._monitor(ok)
        with mock.patch.object(SF, "_save_state_entry"):
            await monitor._dispatch(ring_alert(ring=2), None, [], 1_700_000_000.0)
        self.assertEqual(monitor._tracker.notified_ring, 2)

    async def test_a_send_that_raises_does_not_commit(self):
        async def boom(text, photo=None):
            raise RuntimeError("telegram down")
        monitor = self._monitor(boom)
        await monitor._dispatch(ring_alert(ring=2), None, [], 1_700_000_000.0)
        self.assertEqual(monitor._tracker.notified_ring, 0)

    async def test_a_suppressed_alert_is_committed_anyway(self):
        """Otherwise the alert would be retried every minute until the window
        ends, and then arrive as a burst."""
        sent = []

        async def record(text, photo=None):
            sent.append(text)
            return True

        monitor = self._monitor(record, quiet_start="00:00", quiet_end="23:59")
        with mock.patch.object(SF, "_save_state_entry"):
            await monitor._dispatch(ring_alert(ring=1, ring_edge=30.0), None, [],
                                    1_700_000_000.0)
        self.assertEqual(sent, [])
        self.assertEqual(monitor._tracker.notified_ring, 1)

    async def test_a_broken_chart_still_sends_the_text(self):
        sent = []

        async def record(text, photo=None):
            sent.append((text, photo))
            return True

        monitor = self._monitor(record, chart=True)
        with mock.patch.object(SF, "_save_state_entry"), \
             mock.patch("dradis.live_monitors.storm_front_chart.render_radar",
                        side_effect=RuntimeError("no matplotlib")):
            await monitor._dispatch(ring_alert(ring=2), None, [], 1_700_000_000.0)
        self.assertEqual(len(sent), 1)
        self.assertIsNone(sent[0][1])
        self.assertIn("Temporale", sent[0][0])
        self.assertEqual(monitor._tracker.notified_ring, 2)


class FormattingTest(unittest.TestCase):
    def _monitor(self, **kw):
        return SF.StormFrontLiveMonitor(config(**kw), send_ok, "Europe/Rome")

    def test_every_ring_and_language_renders(self):
        for lang in ("it", "en"):
            monitor = self._monitor(language=lang)
            for ring, edge in ((1, 30.0), (2, 19.5), (3, 12.0), (4, 6.0)):
                text = monitor._format(ring_alert(ring=ring, ring_edge=edge))
                self.assertEqual(text.count("<b>"), text.count("</b>"))
                self.assertIn("🕐", text)
                self.assertIn(f"{ring}/4", text)

    def test_every_track_verdict_renders(self):
        monitor = self._monitor()
        cases = [
            (ring_alert(track=TRACK_CLOSING), "Rotta costante"),
            (ring_alert(ring=4, ring_edge=6.0, track=TRACK_CLOSING),
             "Sei sotto il temporale"),
            (ring_alert(track=TRACK_GRAZING, pass_bearing_deg=10.0), "Ti sfiora"),
            (ring_alert(track=TRACK_UNKNOWN), "non ancora determinabile"),
            (ring_alert(track=TRACK_UNKNOWN, new_cell=True), "Nuovo nucleo"),
        ]
        for alert, expected in cases:
            self.assertIn(expected, monitor._format(alert))

    def test_a_grazing_storm_at_the_innermost_ring_still_says_grazing(self):
        monitor = self._monitor()
        text = monitor._format(ring_alert(ring=4, ring_edge=6.0,
                                          track=TRACK_GRAZING, pass_bearing_deg=90.0))
        self.assertIn("Ti sfiora", text)

    def test_the_elapsed_line_only_appears_with_a_previous_ring(self):
        monitor = self._monitor()
        self.assertNotIn("⏱️", monitor._format(ring_alert()))
        with_prev = ring_alert(prev_ring=1, prev_front_km=27.0, elapsed_sec=540.0)
        text = monitor._format(with_prev)
        self.assertIn("Da 27 a 18 km in 9 min", text)

    def test_secondary_activity_is_listed(self):
        monitor = self._monitor()
        alert = ring_alert(secondary=(
            SectorReading(sector=4, count=9, front_km=24.0, bearing_deg=225.0),))
        self.assertIn("Altra attività", monitor._format(alert))
        self.assertIn("24 km", monitor._format(alert))

    def test_the_all_clear_reports_the_closest_approach(self):
        monitor = self._monitor()
        text = monitor._format(ClearAlert(
            ring_count=4, radius_km=30.0, closest_km=5.0, closest_ring=4,
            closest_at=1_700_000_000.0, quiet_sec=600.0, event_duration_sec=4680.0))
        self.assertIn("cessato", text)
        self.assertIn("5 km", text)
        self.assertIn("4/4", text)

    def test_an_all_clear_without_a_closest_approach_still_renders(self):
        monitor = self._monitor()
        text = monitor._format(ClearAlert(
            ring_count=4, radius_km=30.0, closest_km=None, closest_ring=0,
            closest_at=0.0, quiet_sec=600.0, event_duration_sec=600.0))
        self.assertIn("cessato", text)

    def test_the_location_is_html_escaped(self):
        monitor = self._monitor(location="<script>alert(1)</script>")
        text = monitor._format(ring_alert())
        self.assertNotIn("<script>", text)
        self.assertIn("&lt;script&gt;", text)


class MigrationTest(unittest.TestCase):
    def test_lightning_configs_become_storm_front(self):
        configs = [{"id": "a", "type": "lightning", "radius_km": 100,
                    "sensitivity": "medium", "record_strikes": True,
                    "name": "Bacoli", "enabled": True}]
        migrated, count = SF.migrate_lightning_configs(configs)
        self.assertEqual(count, 1)
        entry = migrated[0]
        self.assertEqual(entry["type"], "storm_front")
        self.assertEqual(entry["radius_km"], 30.0)
        self.assertEqual(entry["ring_count"], 4)
        self.assertTrue(entry["chart"])
        self.assertNotIn("sensitivity", entry)
        self.assertNotIn("record_strikes", entry)

    def test_a_deliberate_radius_is_preserved(self):
        configs = [{"id": "a", "type": "lightning", "radius_km": 45}]
        migrated, _ = SF.migrate_lightning_configs(configs)
        self.assertEqual(migrated[0]["radius_km"], 45.0)

    def test_other_types_are_left_alone(self):
        configs = [{"id": "a", "type": "seismic", "radius_km": 100}]
        migrated, count = SF.migrate_lightning_configs(configs)
        self.assertEqual(count, 0)
        self.assertEqual(migrated[0]["type"], "seismic")

    def test_migration_is_idempotent(self):
        configs = [{"id": "a", "type": "lightning", "radius_km": 100}]
        configs, first = SF.migrate_lightning_configs(configs)
        configs, second = SF.migrate_lightning_configs(configs)
        self.assertEqual((first, second), (1, 0))


if __name__ == "__main__":
    unittest.main()
