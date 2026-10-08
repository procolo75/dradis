"""
tests/test_hail_front.py
─────────────────────────
The hail monitor: the tier ladder (escalate-only, bounded), the all-clear counted
in rasters, blindness that never turns into calm, and the words it sends.

    cd dradis && python3 -m unittest discover tests
"""

import sys
import tempfile
import types
import unittest

import numpy as np

if "aiomqtt" not in sys.modules:
    stub = types.ModuleType("aiomqtt")

    class _Client:
        def __init__(self, *a, **kw):
            pass

    stub.Client = _Client
    sys.modules["aiomqtt"] = stub

from dradis.live_monitors import hail_front as HF                     # noqa: E402
from dradis.live_monitors.geo import distance_km, offset_km           # noqa: E402
from dradis.live_monitors.radar_core import (                         # noqa: E402
    GeoTransform, RadarGrid, peak_with_location, pixel_to_latlon,
)

HF.STATE_PATH = tempfile.mktemp(suffix="-hail-front-state.json")

T0 = 1_700_000_000.0
COLS = ROWS = 400
GT = GeoTransform(cols=COLS, rows=ROWS, pixel_m=1000.0,
                  x0=-200000.0, y0=200000.0, lon0=12.5, lat0=42.0)
ORIGIN = tuple(float(v) for v in pixel_to_latlon(GT, COLS / 2, ROWS / 2))


def monitor(**overrides) -> HF.HailFrontLiveMonitor:
    cfg = {"id": "h1", "name": "Casa", "location": "Casa", "language": "it",
           "latitude": ORIGIN[0], "longitude": ORIGIN[1], "radius_km": 30.0}
    cfg.update(overrides)
    return HF.HailFrontLiveMonitor(cfg, telegram_send_fn=None, tz_name="UTC")


def poh_grid(cells=(), t: float = T0) -> RadarGrid:
    """A POH raster; each cell is (north_km, east_km, percent)."""
    data = np.zeros((ROWS, COLS), dtype=np.float32)
    for north, east, percent in cells:
        row = int(ROWS / 2 - north)
        col = int(COLS / 2 + east)
        data[row, col] = percent
    return RadarGrid(t=t, product="POH", data=data, gt=GT)


class FakeFeed:
    def __init__(self, grid=None, ok=True):
        self.grid, self.ok = grid, ok
        self.acquired = self.released = 0

    def latest(self, product):
        return self.grid

    def feed_ok(self, product, now):
        return self.ok

    def acquire(self, *products):
        self.acquired += 1

    def release(self, *products):
        self.released += 1

    def status(self):
        return "running"


# ── Raster ────────────────────────────────────────────────────────────────────

class PeakLocationTest(unittest.TestCase):

    def test_finds_the_strongest_cell_and_where_it_is(self):
        grid = poh_grid([(10, 0, 55), (0, 20, 80)])
        value, lat, lon = peak_with_location(grid, ORIGIN, 30.0)
        self.assertEqual(value, 80.0)
        self.assertAlmostEqual(distance_km(*ORIGIN, lat, lon), 20.0, delta=1.5)

    def test_ties_resolve_to_the_nearest_cell(self):
        grid = poh_grid([(25, 0, 70), (0, 8, 70)])
        _, lat, lon = peak_with_location(grid, ORIGIN, 30.0)
        self.assertAlmostEqual(distance_km(*ORIGIN, lat, lon), 8.0, delta=1.5)

    def test_cells_beyond_the_radius_are_ignored(self):
        grid = poh_grid([(0, 40, 90)])
        self.assertEqual(peak_with_location(grid, ORIGIN, 30.0)[0], 0.0)

    def test_unmeasured_disc_is_none_not_zero(self):
        grid = poh_grid()
        grid.data[:] = -9999.0
        self.assertIsNone(peak_with_location(grid, ORIGIN, 30.0))


# ── Tier ladder ───────────────────────────────────────────────────────────────

WHERE = (12.0, 270.0)


class TrackerTest(unittest.TestCase):

    def setUp(self):
        self.tracker = HF.HailTracker(40.0, 70.0)
        self.t = T0

    def step(self, peak, near=0.0, where=WHERE):
        self.t += 300.0                       # a new raster every call
        return self.tracker.evaluate(peak, near, where, self.t)

    def test_quiet_sky_says_nothing(self):
        self.assertIsNone(self.step(10.0))

    def test_watch_fires_at_the_watch_threshold(self):
        alert = self.step(40.0)
        self.assertEqual(alert.tier, HF.TIER_WATCH)
        self.assertFalse(alert.escalation)

    def test_below_the_threshold_does_not_fire(self):
        self.assertIsNone(self.step(39.9))

    def test_severe_peak_goes_straight_to_warning(self):
        self.assertEqual(self.step(75.0).tier, HF.TIER_WARNING)

    def test_a_cell_within_ten_km_is_a_warning_at_the_watch_value(self):
        self.assertEqual(self.step(45.0, near=45.0).tier, HF.TIER_WARNING)

    def test_same_tier_is_never_repeated(self):
        alert = self.step(50.0)
        self.tracker.commit(alert)
        self.assertIsNone(self.step(55.0))
        self.assertIsNone(self.step(45.0))

    def test_escalation_fires_once_and_is_marked(self):
        self.tracker.commit(self.step(50.0))
        alert = self.step(80.0)
        self.assertEqual(alert.tier, HF.TIER_WARNING)
        self.assertTrue(alert.escalation)
        self.tracker.commit(alert)
        self.assertIsNone(self.step(85.0))

    def test_a_cell_that_flickers_around_the_threshold_cannot_repeat(self):
        self.tracker.commit(self.step(45.0))
        for peak in (35.0, 46.0, 38.0, 47.0, 39.0, 41.0):
            alert = self.step(peak)
            self.assertIsNone(alert)

    def test_uncommitted_alert_is_offered_again(self):
        self.assertIsNotNone(self.step(50.0))
        self.assertIsNotNone(self.step(50.0))

    def test_one_event_is_at_most_two_alerts_and_one_clear(self):
        sent = []
        for peak in (50, 80, 90, 60, 30, 30, 30, 30, 30, 30):
            out = self.step(float(peak))
            if out is not None:
                sent.append(type(out).__name__)
                self.tracker.commit(out)
        self.assertEqual(sent, ["HailAlert", "HailAlert", "HailClear"])


class ClearTest(unittest.TestCase):

    def setUp(self):
        self.tracker = HF.HailTracker(40.0, 70.0)
        self.tracker.commit(self.tracker.evaluate(60.0, 0.0, WHERE, T0))

    def test_clear_needs_consecutive_new_rasters(self):
        results = [self.tracker.evaluate(10.0, 0.0, WHERE, T0 + 300.0 * i)
                   for i in range(1, HF.CLEAR_GRIDS + 1)]
        self.assertTrue(all(r is None for r in results[:-1]))
        self.assertIsInstance(results[-1], HF.HailClear)

    def test_polling_the_same_raster_does_not_count_twice(self):
        for _ in range(10):
            self.assertIsNone(self.tracker.evaluate(10.0, 0.0, WHERE, T0 + 300.0))

    def test_a_pulse_resets_the_count(self):
        self.tracker.evaluate(10.0, 0.0, WHERE, T0 + 300.0)
        self.tracker.evaluate(10.0, 0.0, WHERE, T0 + 600.0)
        self.tracker.evaluate(55.0, 0.0, WHERE, T0 + 900.0)
        self.assertIsNone(self.tracker.evaluate(10.0, 0.0, WHERE, T0 + 1200.0))

    def test_an_unmeasured_disc_never_clears(self):
        for i in range(1, 10):
            self.assertIsNone(self.tracker.evaluate(None, None, None, T0 + 300.0 * i))

    def test_a_blind_feed_never_clears(self):
        for i in range(1, 10):
            self.assertIsNone(
                self.tracker.evaluate(0.0, 0.0, WHERE, T0 + 300.0 * i, feed_ok=False))

    def test_commit_clear_closes_the_event(self):
        for i in range(1, HF.CLEAR_GRIDS + 1):
            out = self.tracker.evaluate(10.0, 0.0, WHERE, T0 + 300.0 * i)
        self.tracker.commit(out)
        self.assertFalse(self.tracker.event_open)
        self.assertEqual(self.tracker.event_peak, 0.0)

    def test_state_round_trips(self):
        restored = HF.HailTracker.from_dict(40.0, 70.0, self.tracker.to_dict())
        self.assertEqual(restored.notified_tier, self.tracker.notified_tier)
        self.assertEqual(restored.event_peak, self.tracker.event_peak)


# ── Monitor ───────────────────────────────────────────────────────────────────

class ConfigTest(unittest.TestCase):

    def test_defaults(self):
        mon = monitor()
        self.assertEqual((mon.watch_percent, mon.severe_percent), (40.0, 70.0))

    def test_severe_is_never_below_watch(self):
        mon = monitor(watch_percent=60, severe_percent=30)
        self.assertEqual(mon.severe_percent, 60.0)

    def test_nonsense_falls_back_to_defaults(self):
        mon = monitor(watch_percent="x", severe_percent=0)
        self.assertEqual((mon.watch_percent, mon.severe_percent), (40.0, 70.0))

    def test_only_the_hail_product_is_requested(self):
        feed = FakeFeed()
        real = HF.radar_feed
        HF.radar_feed = feed
        try:
            mon = monitor()
            mon._poll_task = None
            mon.stop()
        finally:
            HF.radar_feed = real
        self.assertEqual(feed.released, 1)


class TickTest(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self._real_feed = HF.radar_feed
        self.sent = []

        async def send(text, photo=None):
            self.sent.append(text)
            return True

        self.send = send

    def tearDown(self):
        HF.radar_feed = self._real_feed

    def _monitor(self, grid, ok=True, **overrides):
        HF.radar_feed = FakeFeed(grid, ok)
        mon = monitor(**overrides)
        mon._send = self.send
        return mon

    async def test_a_cell_in_range_sends_an_alert_with_distance_and_direction(self):
        # 20 km due west.
        mon = self._monitor(poh_grid([(0, -20, 55)], t=HF.time.time()))
        await mon._tick(notify=True)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Rischio grandine", self.sent[0])
        self.assertIn("55%", self.sent[0])
        self.assertIn("20 km", self.sent[0])
        self.assertIn("O (", self.sent[0])

    async def test_no_direction_of_travel_is_needed_or_claimed(self):
        mon = self._monitor(poh_grid([(0, -20, 55)], t=HF.time.time()))
        await mon._tick(notify=True)
        self.assertNotIn("verso", self.sent[0])

    async def test_first_tick_does_not_notify(self):
        mon = self._monitor(poh_grid([(0, -20, 90)], t=HF.time.time()))
        await mon._tick(notify=False)
        self.assertEqual(self.sent, [])

    async def test_an_alert_is_sent_once(self):
        mon = self._monitor(poh_grid([(0, -20, 55)], t=HF.time.time()))
        await mon._tick(notify=True)
        await mon._tick(notify=True)
        self.assertEqual(len(self.sent), 1)

    async def test_a_stale_raster_is_blindness_and_stays_silent(self):
        mon = self._monitor(poh_grid([(0, -5, 95)], t=T0), ok=False)
        await mon._tick(notify=True)
        self.assertEqual(self.sent, [])
        self.assertEqual(mon._blind_reason, "radar")

    async def test_no_raster_is_blindness(self):
        mon = self._monitor(None)
        await mon._tick(notify=True)
        self.assertEqual(mon._blind_reason, "radar")

    async def test_a_followed_position_with_no_fix_and_no_fallback_is_blindness(self):
        mon = self._monitor(poh_grid(t=HF.time.time()), latitude=0, longitude=0,
                            position_id="never-seen")
        await mon._tick(notify=True)
        self.assertEqual(mon._blind_reason, "position")

    async def test_a_disc_outside_the_radar_is_blindness(self):
        grid = poh_grid(t=HF.time.time())
        grid.data[:] = -9999.0
        mon = self._monitor(grid)
        await mon._tick(notify=True)
        self.assertEqual(mon._blind_reason, "coverage")

    async def test_blindness_clears_when_the_input_returns(self):
        mon = self._monitor(None)
        await mon._tick(notify=True)
        self.assertEqual(mon.status(), "stopped")      # not running: no task
        HF.radar_feed = FakeFeed(poh_grid(t=HF.time.time()))
        await mon._tick(notify=True)
        self.assertEqual(mon._blind_since, 0.0)

    async def test_overhead_cell_is_worded_as_over_you(self):
        mon = self._monitor(poh_grid([(0, 0, 80)], t=HF.time.time()))
        await mon._tick(notify=True)
        self.assertIn("sopra di te", self.sent[0])
        self.assertIn("🚨", self.sent[0])


class DispatchTest(unittest.IsolatedAsyncioTestCase):

    def _monitor(self, send, **overrides):
        mon = monitor(**overrides)
        mon._send = send
        mon._grid_t = T0
        return mon

    def _alert(self, tier=HF.TIER_WATCH):
        return HF.HailAlert(tier=tier, peak_percent=60.0, near_percent=0.0,
                            distance_km=12.0, bearing_deg=270.0, escalation=False)

    async def test_a_confirmed_send_commits(self):
        async def confirmed(text, photo=None):
            return True
        mon = self._monitor(confirmed)
        await mon._dispatch(self._alert(), T0)
        self.assertEqual(mon._tracker.notified_tier, HF.TIER_WATCH)

    async def test_an_unconfirmed_send_commits_and_is_never_repeated(self):
        sent = []

        async def unconfirmed(text, photo=None):
            sent.append(text)
            return None
        mon = self._monitor(unconfirmed)
        await mon._dispatch(self._alert(), T0)
        self.assertEqual(mon._tracker.notified_tier, HF.TIER_WATCH)
        self.assertEqual(len(sent), 1)

    async def test_a_refused_send_is_held(self):
        async def refused(text, photo=None):
            return False
        mon = self._monitor(refused)
        await mon._dispatch(self._alert(), T0)
        self.assertEqual(mon._tracker.notified_tier, HF.TIER_NONE)

    async def test_a_send_that_raises_is_held(self):
        async def boom(text, photo=None):
            raise RuntimeError("telegram down")
        mon = self._monitor(boom)
        await mon._dispatch(self._alert(), T0)
        self.assertEqual(mon._tracker.notified_tier, HF.TIER_NONE)

    async def test_quiet_hours_hold_back_a_watch_but_never_a_warning(self):
        sent = []

        async def send(text, photo=None):
            sent.append(text)
            return True
        mon = self._monitor(send, quiet_start="00:00", quiet_end="23:59")
        await mon._dispatch(self._alert(HF.TIER_WATCH), T0)
        self.assertEqual(sent, [])
        self.assertEqual(mon._tracker.notified_tier, HF.TIER_WATCH)   # consumed
        await mon._dispatch(self._alert(HF.TIER_WARNING), T0)
        self.assertEqual(len(sent), 1)

    async def test_quiet_hours_hold_back_the_all_clear(self):
        sent = []

        async def send(text, photo=None):
            sent.append(text)
            return True
        mon = self._monitor(send, quiet_start="00:00", quiet_end="23:59")
        await mon._dispatch(HF.HailClear(60.0, 3), T0)
        self.assertEqual(sent, [])


class FormatTest(unittest.TestCase):

    def alert(self, **kw):
        fields = dict(tier=HF.TIER_WATCH, peak_percent=55.0, near_percent=0.0,
                      distance_km=18.0, bearing_deg=315.0, escalation=False)
        fields.update(kw)
        return HF.HailAlert(**fields)

    def test_watch_and_warning_have_different_headlines(self):
        mon = monitor()
        mon._grid_t = T0
        watch = mon._format(self.alert(), T0 + 600)
        warning = mon._format(self.alert(tier=HF.TIER_WARNING), T0 + 600)
        self.assertIn("Rischio grandine", watch)
        self.assertNotIn("Metti al riparo", watch)
        self.assertIn("Grandine probabile", warning)
        self.assertIn("Metti al riparo", warning)

    def test_the_message_states_the_age_of_the_picture(self):
        mon = monitor()
        mon._grid_t = T0
        self.assertIn("10 min fa", mon._format(self.alert(), T0 + 600))

    def test_the_message_names_the_cell_position(self):
        mon = monitor()
        text = mon._format(self.alert(), T0)
        self.assertIn("18 km", text)
        self.assertIn("NO", text)

    def test_english(self):
        mon = monitor(language="en")
        mon._grid_t = T0
        text = mon._format(self.alert(tier=HF.TIER_WARNING), T0)
        self.assertIn("Hail likely", text)
        self.assertIn("Shelter", text)

    def test_the_all_clear(self):
        mon = monitor()
        mon._grid_t = T0
        text = mon._format(HF.HailClear(72.0, 3), T0)
        self.assertIn("cessato", text)
        self.assertIn("72%", text)


# ── Manager ───────────────────────────────────────────────────────────────────

class ManagerTest(unittest.TestCase):

    def setUp(self):
        self.manager = HF.HailFrontMonitorManager()
        self.started = []

        def fake_start(monitor_self):
            self.started.append(monitor_self.monitor_id)
            monitor_self._poll_task = None

        self._real_start = HF.HailFrontLiveMonitor.start
        HF.HailFrontLiveMonitor.start = fake_start

    def tearDown(self):
        HF.HailFrontLiveMonitor.start = self._real_start

    def cfg(self, **kw):
        base = {"id": "h1", "type": "hail_front", "enabled": True, "name": "Casa",
                "latitude": ORIGIN[0], "longitude": ORIGIN[1]}
        base.update(kw)
        return base

    def test_only_enabled_hail_monitors_are_created(self):
        self.manager.reload(
            [self.cfg(), self.cfg(id="x", type="rain_front"),
             self.cfg(id="y", enabled=False)], lambda c: None, "UTC")
        self.assertEqual(self.started, ["h1"])

    def test_a_removed_monitor_is_dropped(self):
        self.manager.reload([self.cfg()], lambda c: None, "UTC")
        self.manager.reload([], lambda c: None, "UTC")
        self.assertIsNone(self.manager.get("h1"))
        self.assertEqual(self.manager.status("h1"), "stopped")


if __name__ == "__main__":
    unittest.main()
