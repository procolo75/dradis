"""
tests/test_lightning_manager.py
────────────────────────────────
Lifecycle tests for LiveMonitorManager.

Regression for the pre-3.3.0 behaviour where reload() unconditionally destroyed
and recreated every enabled lightning monitor. Because scheduler.reload_live_monitors
hands the full config list to all three managers, saving an unrelated football or
seismic monitor wiped the strike buffer, reset the threat level to CLEAR and
dropped the all-clear of a storm that was in progress.

aiomqtt is an add-on dependency and is not needed to exercise this, so it is
stubbed with a client that connects and then simply produces no messages.

Run with:  cd dradis && python -m unittest discover tests
"""

import asyncio
import sys
import tempfile
import types
import unittest


def _install_aiomqtt_stub():
    if "aiomqtt" in sys.modules:
        return

    class _Messages:
        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.sleep(3600)          # never yields; keeps the task alive
            raise StopAsyncIteration

    class _Client:
        def __init__(self, *args, **kwargs):
            self.messages = _Messages()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def subscribe(self, topic):
            return None

        async def unsubscribe(self, topic):
            return None

    sys.modules["aiomqtt"] = types.SimpleNamespace(Client=_Client)


_install_aiomqtt_stub()

from dradis.live_monitors import lightning as L  # noqa: E402  (after the stub)

L.STATE_PATH = tempfile.mktemp(suffix="-lightning_state.json")


def make_send(cfg):
    async def _send(text):
        return True
    return _send


def lightning_cfg(**overrides):
    cfg = {
        "id": "storm1", "type": "lightning", "enabled": True,
        "name": "Casa", "location": "Napoli",
        "latitude": 40.85, "longitude": 14.27,
        "radius_km": 100, "language": "it", "sensitivity": "medium",
        "quiet_start": "", "quiet_end": "", "record_strikes": False,
        "telegram_bot_id": "default",
    }
    cfg.update(overrides)
    return cfg


FOOTBALL_CFG = {"id": "foot1", "type": "football_betting", "enabled": True,
                "name": "Serie A", "windows": ["55-65"]}


class ReloadTest(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.manager = L.LiveMonitorManager()

    async def asyncTearDown(self):
        self.manager.stop_all()
        await asyncio.sleep(0)

    async def _settle(self):
        """Let the scheduled handover tasks run."""
        for _ in range(5):
            await asyncio.sleep(0)

    async def test_unrelated_save_does_not_restart_the_monitor(self):
        cfg = lightning_cfg()
        self.manager.reload([cfg], make_send, "Europe/Rome")
        await self._settle()
        original = self.manager._monitors["storm1"]

        # Simulate the user saving a football monitor: the same lightning config
        # comes back through reload alongside a new, unrelated entry.
        self.manager.reload([cfg, FOOTBALL_CFG], make_send, "Europe/Rome")
        await self._settle()

        self.assertIs(self.manager._monitors["storm1"], original,
                      "an unrelated save restarted the lightning monitor")
        self.assertTrue(original.is_running())

    async def test_state_survives_an_unrelated_save(self):
        cfg = lightning_cfg()
        self.manager.reload([cfg], make_send, "Europe/Rome")
        await self._settle()
        monitor = self.manager._monitors["storm1"]
        monitor._machine.level = L.LEVEL_WARNING
        monitor._buffer = [(1.0, 40.9, 14.3)] * 50

        self.manager.reload([cfg, FOOTBALL_CFG], make_send, "Europe/Rome")
        await self._settle()

        survivor = self.manager._monitors["storm1"]
        self.assertEqual(survivor._machine.level, L.LEVEL_WARNING,
                         "in-flight threat level was reset")
        self.assertEqual(len(survivor._buffer), 50, "strike buffer was wiped")

    async def test_changed_config_does_restart_the_monitor(self):
        self.manager.reload([lightning_cfg()], make_send, "Europe/Rome")
        await self._settle()
        original = self.manager._monitors["storm1"]

        self.manager.reload([lightning_cfg(radius_km=60)], make_send, "Europe/Rome")
        await self._settle()

        replacement = self.manager._monitors["storm1"]
        self.assertIsNot(replacement, original, "config change did not restart")
        self.assertFalse(original.is_running(), "old instance still running")
        self.assertTrue(replacement.is_running())
        self.assertEqual(replacement.radius_km, 60)

    async def test_timezone_change_restarts_the_monitor(self):
        self.manager.reload([lightning_cfg()], make_send, "Europe/Rome")
        await self._settle()
        original = self.manager._monitors["storm1"]

        self.manager.reload([lightning_cfg()], make_send, "UTC")
        await self._settle()

        self.assertIsNot(self.manager._monitors["storm1"], original)

    async def test_disabled_monitor_is_removed(self):
        self.manager.reload([lightning_cfg()], make_send, "Europe/Rome")
        await self._settle()
        monitor = self.manager._monitors["storm1"]

        self.manager.reload([lightning_cfg(enabled=False)], make_send, "Europe/Rome")
        await self._settle()

        self.assertNotIn("storm1", self.manager._monitors)
        self.assertFalse(monitor.is_running())
        self.assertEqual(self.manager.status("storm1"), "stopped")

    async def test_non_lightning_types_are_ignored(self):
        self.manager.reload([FOOTBALL_CFG], make_send, "Europe/Rome")
        await self._settle()
        self.assertEqual(self.manager._monitors, {})


class StatusTest(unittest.IsolatedAsyncioTestCase):

    async def test_silent_feed_reports_degraded(self):
        manager = L.LiveMonitorManager()
        manager.reload([lightning_cfg()], make_send, "Europe/Rome")
        for _ in range(5):
            await asyncio.sleep(0)
        monitor = manager._monitors["storm1"]

        monitor._connected = True
        monitor._last_msg_ts = L.time.time()
        self.assertEqual(monitor.status(), "running")

        # A feed that has gone quiet for longer than the threshold must be visible
        # as degraded rather than reported as healthy.
        monitor._last_msg_ts = L.time.time() - L.DEGRADED_SILENCE_SEC - 1
        self.assertEqual(monitor.status(), "degraded")

        monitor._last_msg_ts = L.time.time()
        monitor._connect_failures = 5
        self.assertEqual(monitor.status(), "degraded")

        manager.stop_all()
        await asyncio.sleep(0)


class QuietHoursTest(unittest.IsolatedAsyncioTestCase):
    """Quiet hours used to be saved and then ignored entirely for lightning."""

    def _monitor(self, start, end):
        return L.LightningLiveMonitor(
            lightning_cfg(quiet_start=start, quiet_end=end), make_send, "UTC")

    async def test_disabled_when_unset(self):
        self.assertFalse(self._monitor("", "")._in_quiet_hours())

    async def test_window_is_evaluated(self):
        self.assertTrue(self._monitor("00:00", "23:59")._in_quiet_hours())
        # An empty window (start == end) silences nothing.
        self.assertFalse(self._monitor("00:00", "00:00")._in_quiet_hours())

    async def test_malformed_window_does_not_raise(self):
        self.assertFalse(self._monitor("nonsense", "07:00")._in_quiet_hours())


if __name__ == "__main__":
    unittest.main()
