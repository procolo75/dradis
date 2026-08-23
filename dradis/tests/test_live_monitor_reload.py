"""
tests/test_live_monitor_reload.py
──────────────────────────────────
Enabling and disabling a live monitor from Telegram, and what the /monitors list
says about it.

    cd dradis && python3 -m unittest discover tests

Two failures are pinned down here, and they share a shape: the flag was written
correctly and nothing acted on it.

  · Every manager is reloaded from one function, in sequence, and an exception in
    any of them abandoned the rest. A monitor after the failing one kept running
    its old configuration, which from the outside is indistinguishable from
    "/manage did nothing".
  · The /monitors list showed a storm front's `location` even when it follows a
    named position — that is, a place the monitor is not watching, usually the
    default it was created with.
"""

import asyncio
import builtins
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

_OPTIONS = Path(tempfile.mktemp(suffix="-options.json"))
_OPTIONS.write_text(json.dumps({
    "telegram_bot_token": "test", "telegram_allowed_chat_id": 1,
}))

if "aiomqtt" not in sys.modules:
    sys.modules["aiomqtt"] = types.ModuleType("aiomqtt")

# `bot.*` uses the same absolute imports the running add-on does.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dradis"))

_real_open = builtins.open


def _patched_open(path, *args, **kwargs):
    if str(path) == "/data/options.json":
        return _real_open(_OPTIONS, *args, **kwargs)
    return _real_open(path, *args, **kwargs)


builtins.open = _patched_open
try:
    import bot.handlers as handlers                               # noqa: E402
    import bot.scheduler as scheduler                             # noqa: E402
    from live_monitors.football import FootballMonitorManager     # noqa: E402
except ImportError as e:
    # Unlike most of the suite, this module pulls in the Telegram and LLM SDKs.
    raise unittest.SkipTest(f"bot dependencies unavailable: {e}")
finally:
    builtins.open = _real_open


POSITIONS = [{"id": "p1", "name": "Cellulare di Procolo",
              "lat_entity": "sensor/a", "lon_entity": "sensor/b"}]


class MonitorLabelTest(unittest.TestCase):
    """What /monitors and /manage print next to a live monitor's name."""

    def detail(self, monitor, positions=POSITIONS):
        with mock.patch.object(handlers, "load_positions", lambda: positions):
            return handlers._live_monitor_detail(monitor)

    def test_a_fixed_monitor_shows_its_location(self):
        self.assertEqual(
            self.detail({"type": "storm_front", "location": "Bacoli"}), "Bacoli")

    def test_a_monitor_following_a_position_shows_its_name(self):
        # THE regression: `location` is unused once a position is selected, so
        # printing it names a place the monitor is not watching — typically the
        # default the monitor was created with.
        detail = self.detail({"type": "storm_front", "location": "Roma",
                              "position_id": "p1"})
        self.assertIn("Cellulare di Procolo", detail)
        self.assertNotIn("Roma", detail)

    def test_a_dangling_position_is_flagged(self):
        detail = self.detail({"type": "storm_front", "location": "Roma",
                              "position_id": "gone"})
        self.assertIn("missing position", detail)
        self.assertNotIn("Roma", detail)

    def test_an_empty_position_id_is_treated_as_fixed(self):
        self.assertEqual(
            self.detail({"type": "storm_front", "location": "Bacoli",
                         "position_id": ""}), "Bacoli")

    def test_seismic_and_football_are_unchanged(self):
        self.assertEqual(
            self.detail({"type": "seismic", "areas": ["flegrei", "vesuvio"]}),
            "flegrei, vesuvio")
        self.assertEqual(self.detail({"type": "football_betting"}), "⚽ live")


class DeliveryOutcomeTest(unittest.TestCase):
    """A timeout is not a refusal.

    The live monitors gate their notification bookkeeping on this classification:
    `REFUSED` is retried at the next poll, `UNCONFIRMED` is committed as delivered.
    Getting a timed-out photo upload into the first class is what sent every ring
    alert of one rain event twice on 17 Aug 2026.
    """

    def test_a_timeout_is_unconfirmed(self):
        from telegram.error import TimedOut
        self.assertIs(scheduler._state.classify_send_failure(TimedOut()),
                      scheduler._state.UNCONFIRMED)

    def test_everything_else_is_refused(self):
        from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter
        for exc in (BadRequest("unclosed tag"), Forbidden("bot was blocked"),
                    NetworkError("connection refused"), RetryAfter(30)):
            with self.subTest(error=type(exc).__name__):
                self.assertIs(scheduler._state.classify_send_failure(exc),
                              scheduler._state.REFUSED)


class Recorder:
    def __init__(self, name, boom=False):
        self.name, self.boom, self.calls = name, boom, []

    def reload(self, *args, **kwargs):
        if self.boom:
            raise RuntimeError(f"{self.name} exploded")
        self.calls.append(args)

    def configure(self, *args, **kwargs):
        if self.boom:
            raise RuntimeError(f"{self.name} exploded")
        self.calls.append(args)


class ReloadIsolationTest(unittest.TestCase):
    """One broken manager must not take the others with it. Toggling a monitor
    writes a flag and relies entirely on this function to act on it."""

    def setUp(self):
        self._patches = [
            mock.patch.object(scheduler, "load_live_monitors", lambda: []),
            mock.patch.object(scheduler, "load_positions", lambda: []),
            mock.patch.object(scheduler._state, "read_settings",
                              lambda: {"timezone": "UTC"}),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def run_reload(self, **managers):
        names = {"position_manager": "position_manager",
                 "storm_front_monitor_manager": "storm_front_monitor_manager",
                 "seismic_monitor_manager": "seismic_monitor_manager",
                 "football_monitor_manager": "football_monitor_manager"}
        recorders = {attr: managers.get(attr) or Recorder(attr)
                     for attr in names}
        with mock.patch.multiple(scheduler, **recorders):
            scheduler.reload_live_monitors()
        return recorders

    def test_all_managers_reload_on_a_healthy_pass(self):
        r = self.run_reload()
        for attr, rec in r.items():
            self.assertTrue(rec.calls, f"{attr} was not reloaded")

    def test_a_failing_manager_does_not_stop_the_ones_after_it(self):
        boom = Recorder("storm_front_monitor_manager", boom=True)
        r = self.run_reload(storm_front_monitor_manager=boom)
        self.assertTrue(r["seismic_monitor_manager"].calls)
        self.assertTrue(r["football_monitor_manager"].calls)

    def test_a_failing_position_manager_does_not_stop_the_monitors(self):
        # This one sits first in the chain, so before the fix a bad broker
        # setting froze every live monitor in its previous state.
        boom = Recorder("position_manager", boom=True)
        r = self.run_reload(position_manager=boom)
        self.assertTrue(r["storm_front_monitor_manager"].calls)
        self.assertTrue(r["football_monitor_manager"].calls)

    def test_the_failure_never_reaches_the_caller(self):
        # `toggle_live_monitor` calls this through the change callback; an
        # exception here would leave the Telegram button with no feedback at all.
        boom = Recorder("storm_front_monitor_manager", boom=True)
        try:
            self.run_reload(storm_front_monitor_manager=boom)
        except Exception as e:                          # pragma: no cover
            self.fail(f"reload_live_monitors raised: {e}")


def football(**overrides) -> dict:
    cfg = {"id": "f1", "name": "Serie A", "type": "football_betting",
           "enabled": True, "windows": ["early", "late"],
           "window_early_start": 55, "window_early_end": 65,
           "window_early_max_odds": 2.0, "window_late_start": 75,
           "window_late_end": 81, "window_late_max_odds": 0.0,
           "quiet_start": "", "quiet_end": "", "language": "it",
           "telegram_bot_id": "default"}
    cfg.update(overrides)
    return cfg


async def _inert(self):
    await asyncio.sleep(3600)


class FootballReloadTest(unittest.IsolatedAsyncioTestCase):
    """Disabling from /manage must actually stop it — and, just as important, an
    unrelated save must not restart it."""

    async def asyncSetUp(self):
        self.manager = FootballMonitorManager()
        self._patch = mock.patch(
            "live_monitors.football.FootballLiveMonitor._run", _inert)
        self._patch.start()

    async def asyncTearDown(self):
        self.manager.stop_all()
        self._patch.stop()

    def reload(self, *configs):
        self.manager.reload(list(configs), lambda _cfg: (lambda *a, **k: None),
                            "Europe/Rome")

    async def test_an_enabled_monitor_starts(self):
        self.reload(football())
        self.assertEqual(self.manager.status("f1"), "running")

    async def test_disabling_stops_it(self):
        self.reload(football())
        self.reload(football(enabled=False))
        self.assertEqual(self.manager.status("f1"), "stopped")

    async def test_re_enabling_starts_it_again(self):
        self.reload(football())
        self.reload(football(enabled=False))
        self.reload(football())
        self.assertEqual(self.manager.status("f1"), "running")

    async def test_removing_it_entirely_stops_it(self):
        self.reload(football())
        self.reload()
        self.assertEqual(self.manager.status("f1"), "stopped")

    async def test_an_unrelated_save_does_not_restart_it(self):
        # Every manager is handed the whole live-monitor list on every save, so
        # without a fingerprint toggling a task would tear this monitor down and
        # back up, losing its dedup state for something unrelated to it.
        self.reload(football())
        first = self.manager._monitors["f1"]
        self.reload(football(),
                    {"id": "sf1", "type": "storm_front", "enabled": True})
        self.assertIs(self.manager._monitors["f1"], first)

    async def test_its_own_change_does_restart_it(self):
        self.reload(football())
        first = self.manager._monitors["f1"]
        self.reload(football(window_late_max_odds=3.5))
        self.assertIsNot(self.manager._monitors["f1"], first)

    async def test_any_field_counts_as_a_change(self):
        for field, value in (("windows", ["early"]), ("window_early_start", 50),
                             ("window_late_end", 90),
                             ("quiet_start", "23:00"),
                             ("name", "Renamed"), ("telegram_bot_id", "other")):
            with self.subTest(field=field):
                manager = FootballMonitorManager()
                manager.reload([football()], lambda _c: (lambda *a, **k: None), "UTC")
                first = manager._monitors["f1"]
                manager.reload([football(**{field: value})],
                               lambda _c: (lambda *a, **k: None), "UTC")
                self.assertIsNot(manager._monitors["f1"], first)
                manager.stop_all()

    async def test_a_timezone_change_restarts_it(self):
        self.reload(football())
        first = self.manager._monitors["f1"]
        self.manager.reload([football()], lambda _c: (lambda *a, **k: None), "UTC")
        self.assertIsNot(self.manager._monitors["f1"], first)


if __name__ == "__main__":
    unittest.main()
