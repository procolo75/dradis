"""
tests/test_rain_front.py
─────────────────────────
The monitor's side of the radar: choosing a verdict, compensating the publication
lag, refusing to speak when it cannot see, and wording the result.

    cd dradis && python3 -m unittest discover tests

aiomqtt is stubbed before the import under test, as in test_storm_front_position.
"""

import asyncio
import sys
import tempfile
import types
import unittest

import numpy as np

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

from dradis.live_monitors import rain_front as RF                     # noqa: E402
from dradis.live_monitors.geo import distance_km, offset_km           # noqa: E402
from dradis.live_monitors.radar_core import (                         # noqa: E402
    FieldMotion, GeoTransform, RadarGrid, build_rain_frame, pixel_to_latlon,
    rain_points, velocity_components,
)
from dradis.live_monitors.storm_front_core import (                   # noqa: E402
    EVENT_IDLE, TRACK_CLOSING, TRACK_GRAZING, TRACK_UNKNOWN, ClearAlert,
    RingAlert,
)

RF.STATE_PATH = tempfile.mktemp(suffix="-rain-front-state.json")

T0 = 1_700_000_000.0
COLS = ROWS = 400
GT = GeoTransform(cols=COLS, rows=ROWS, pixel_m=1000.0,
                  x0=-200000.0, y0=200000.0, lon0=12.5, lat0=42.0)
ORIGIN = tuple(float(v) for v in pixel_to_latlon(GT, COLS / 2, ROWS / 2))


def monitor(**overrides) -> RF.RainFrontLiveMonitor:
    cfg = {"id": "m1", "name": "Casa", "location": "Casa", "language": "it",
           "latitude": ORIGIN[0], "longitude": ORIGIN[1], "radius_km": 30.0,
           "ring_count": 4, "min_mmh": 1.0, "chart": False}
    cfg.update(overrides)
    return RF.RainFrontLiveMonitor(cfg, telegram_send_fn=None, tz_name="UTC")


def grid(data=None, t: float = T0) -> RadarGrid:
    if data is None:
        data = np.zeros((ROWS, COLS), dtype=np.float32)
    return RadarGrid(t=t, product="SRI", data=data, gt=GT)


class Fix:
    """Stand-in for a PositionState."""

    def __init__(self, moving=True, speed_kmh=100.0, course_deg=0.0,
                 discontinuity=0, lat=None, lon=None):
        self.moving = moving
        self.speed_kmh = speed_kmh
        self.course_deg = course_deg
        self.discontinuity = discontinuity
        self.lat = ORIGIN[0] if lat is None else lat
        self.lon = ORIGIN[1] if lon is None else lon


def ring_alert(**overrides) -> RingAlert:
    fields = dict(ring=2, ring_count=4, ring_edge_km=19.5, front_km=12.0,
                  bearing_deg=270.0, sector=9, strikes=120, strikes_in_radius=200)
    fields.update(overrides)
    return RingAlert(**fields)


# ── Verdict ───────────────────────────────────────────────────────────────────

class TrackVerdictTest(unittest.TestCase):
    """The one method the subclass overrides, and the reason it exists."""

    def test_a_measured_head_on_track_is_closing(self):
        tracker = RF.RainFrontTracker(30.0, 4)
        # Rain due west, sliding east at 40 km/h, observer stationary.
        tracker.set_motion(FieldMotion(40.0, 90.0, 8.0, 300.0))
        verdict, side, new_cell = tracker.track_verdict(T0, 270.0, 20.0)
        self.assertEqual(verdict, TRACK_CLOSING)
        self.assertIsNone(side)
        self.assertFalse(new_cell)
        self.assertAlmostEqual(tracker.last_encounter.minutes, 30.0, places=3)
        self.assertAlmostEqual(tracker.last_encounter.miss_km, 0.0, places=6)

    def test_a_measured_crossing_track_is_grazing_and_names_the_side(self):
        tracker = RF.RainFrontTracker(30.0, 4)
        # Rain to the north-west, sliding east: it comes closer, then goes by to
        # the north. Due north would already be AT its closest approach.
        tracker.set_motion(FieldMotion(40.0, 90.0, 8.0, 300.0))
        verdict, side, _ = tracker.track_verdict(T0, 315.0, 20.0)
        self.assertEqual(verdict, TRACK_GRAZING)
        self.assertIsNotNone(side)
        self.assertAlmostEqual(tracker.last_encounter.miss_km, 14.14, delta=0.2)
        self.assertAlmostEqual(tracker.last_encounter.minutes, 21.2, delta=0.5)

    def test_rain_already_at_its_closest_is_not_called_approaching(self):
        """Due north and sliding due east is at minimum range right now: there is
        no encounter left to announce, so the inherited verdict answers."""
        tracker = RF.RainFrontTracker(30.0, 4)
        tracker.set_motion(FieldMotion(40.0, 90.0, 8.0, 300.0))
        verdict, _, _ = tracker.track_verdict(T0, 0.0, 20.0)
        self.assertEqual(verdict, TRACK_UNKNOWN)
        self.assertIsNone(tracker.last_encounter)

    def test_the_dead_band_between_the_thresholds_stays_undecided(self):
        tracker = RF.RainFrontTracker(30.0, 4)
        tracker.set_motion(FieldMotion(40.0, 90.0, 8.0, 300.0))
        # Geometry chosen so the miss lands between CLOSING and GRAZING.
        import math
        from dradis.live_monitors.storm_front_core import (
            CBDR_CLOSING_KM, CBDR_GRAZING_KM)
        front = 20.0
        bearing = 270.0 + math.degrees(math.asin(3.2 / front))
        verdict, _, _ = tracker.track_verdict(T0, bearing, front)
        self.assertEqual(verdict, TRACK_UNKNOWN)
        self.assertGreater(tracker.last_encounter.miss_km, CBDR_CLOSING_KM)
        self.assertLess(tracker.last_encounter.miss_km, CBDR_GRAZING_KM)

    def test_without_a_measured_field_it_defers_to_the_inherited_verdict(self):
        """The common case on scattered convection: phase correlation refuses, and
        CBDR — which needs no velocity — has to answer instead."""
        tracker = RF.RainFrontTracker(30.0, 4)
        tracker.set_motion(None)
        verdict, _, _ = tracker.track_verdict(T0, 270.0, 20.0)
        self.assertEqual(verdict, TRACK_UNKNOWN)     # no history yet
        self.assertIsNone(tracker.last_encounter)

    def test_a_closest_approach_already_past_defers_to_the_inherited_verdict(self):
        """Receding rain cannot explain a descending ring, so the measured reading
        is discarded rather than reported as a negative time. The rain that duly
        arrived on 17 Aug 2026 while the drift pointed away is why this stands."""
        tracker = RF.RainFrontTracker(30.0, 4)
        tracker.set_motion(FieldMotion(40.0, 90.0, 8.0, 300.0))
        verdict, _, _ = tracker.track_verdict(T0, 90.0, 20.0)   # rain east, going east
        self.assertEqual(verdict, TRACK_UNKNOWN)
        self.assertIsNone(tracker.last_encounter)

    def test_the_disagreement_is_recorded_even_though_it_is_not_acted_on(self):
        """Discarded for the verdict, kept for the wording: the message must not
        state the drift and the approach as two unrelated facts."""
        tracker = RF.RainFrontTracker(30.0, 4)
        tracker.set_motion(FieldMotion(40.0, 90.0, 8.0, 300.0))
        tracker.track_verdict(T0, 90.0, 20.0)
        self.assertTrue(tracker.last_receding)

    def test_rain_coming_at_you_is_not_recorded_as_receding(self):
        tracker = RF.RainFrontTracker(30.0, 4)
        tracker.set_motion(FieldMotion(40.0, 90.0, 8.0, 300.0))
        tracker.track_verdict(T0, 270.0, 20.0)      # rain west, coming east
        self.assertFalse(tracker.last_receding)

    def test_the_observer_velocity_enters_the_verdict(self):
        """Driving into stationary rain must read as a collision course."""
        tracker = RF.RainFrontTracker(30.0, 4)
        east, north = velocity_components(100.0, 0.0)            # driving north
        tracker.set_motion(FieldMotion(0.0, 0.0, 9.0, 300.0), east, north)
        verdict, _, _ = tracker.track_verdict(T0, 0.0, 20.0)     # rain due north
        self.assertEqual(verdict, TRACK_CLOSING)
        self.assertAlmostEqual(tracker.last_encounter.minutes, 12.0, places=3)

    def test_the_encounter_is_cleared_between_evaluations(self):
        tracker = RF.RainFrontTracker(30.0, 4)
        tracker.set_motion(FieldMotion(40.0, 90.0, 8.0, 300.0))
        tracker.track_verdict(T0, 270.0, 20.0)
        self.assertIsNotNone(tracker.last_encounter)
        tracker.set_motion(None)
        tracker.track_verdict(T0, 270.0, 20.0)
        self.assertIsNone(tracker.last_encounter)

    def test_inherited_state_machine_is_untouched(self):
        """The subclass must not have disturbed what it inherited."""
        tracker = RF.RainFrontTracker(30.0, 4)
        self.assertEqual(tracker.event_state, EVENT_IDLE)
        self.assertEqual(tracker.ring_count, 4)
        self.assertEqual(tracker.notified_ring, 0)
        restored = RF.RainFrontTracker.from_dict(30.0, 4, tracker.to_dict())
        self.assertIsInstance(restored, RF.RainFrontTracker)
        self.assertIsNone(restored.last_encounter)


# ── Publication lag ───────────────────────────────────────────────────────────

class AdvectionTest(unittest.TestCase):
    """The radar is ten minutes behind; the geometry has to be told."""

    def test_the_origin_is_displaced_against_the_measured_motion(self):
        mon = monitor()
        mon._field = FieldMotion(60.0, 90.0, 8.0, 300.0)      # 60 km/h eastwards
        shifted = mon._advect(ORIGIN, grid(t=T0), T0 + 600.0)
        # Ten minutes at 60 km/h is 10 km; the observer moves west so that the
        # rain, carried east, lands where the geometry expects it.
        self.assertAlmostEqual(distance_km(*ORIGIN, *shifted), 10.0, delta=0.2)
        self.assertLess(shifted[1], ORIGIN[1])

    def test_no_measured_motion_means_no_advection(self):
        mon = monitor()
        mon._field = None
        self.assertEqual(mon._advect(ORIGIN, grid(t=T0), T0 + 600.0), ORIGIN)

    def test_a_pathologically_old_raster_is_not_advected(self):
        mon = monitor()
        mon._field = FieldMotion(60.0, 90.0, 8.0, 300.0)
        far_future = T0 + RF.MAX_ADVECTION_SEC + 1.0
        self.assertEqual(mon._advect(ORIGIN, grid(t=T0), far_future), ORIGIN)

    def test_a_fresh_raster_barely_moves_the_origin(self):
        mon = monitor()
        mon._field = FieldMotion(60.0, 90.0, 8.0, 300.0)
        shifted = mon._advect(ORIGIN, grid(t=T0), T0 + 1.0)
        self.assertLess(distance_km(*ORIGIN, *shifted), 0.1)


# ── Blindness ─────────────────────────────────────────────────────────────────

class BlindnessTest(unittest.TestCase):
    """Not knowing is not the same as nothing happening."""

    def test_going_blind_never_produces_an_all_clear(self):
        mon = monitor()
        # Open an event first, so there is something an all-clear could close.
        data = np.zeros((ROWS, COLS), dtype=np.float32)
        for km in range(5, 20):
            col, row = _pixel(offset_km(ORIGIN[0], ORIGIN[1], 0.0, float(km)))
            data[row - 3:row + 3, col] = 6.0
        frame = build_rain_frame(rain_points(grid(data), ORIGIN, 48.0, 1.0),
                                 ORIGIN, T0, 30.0, 48.0)
        for i in range(3):
            mon._tracker.evaluate(frame, T0 + i * 60, feed_ok=True,
                                  connected_for=1e9)
        self.assertNotEqual(mon._tracker.event_state, EVENT_IDLE)

        for i in range(60):                       # an hour of blindness
            mon._go_blind(T0 + 600 + i * 60, "radar")
        self.assertNotEqual(mon._tracker.event_state, EVENT_IDLE)
        self.assertEqual(mon._tracker.fading_since, 0.0)

    def test_blindness_records_its_reason_and_start(self):
        mon = monitor()
        mon._go_blind(T0, "coverage")
        self.assertEqual(mon._blind_reason, "coverage")
        self.assertEqual(mon._blind_since, T0)

    def test_blindness_drops_the_measured_motion(self):
        """A velocity from before the blackout describes a field we can no longer
        see; keeping it would let the monitor quote an encounter it cannot check."""
        mon = monitor()
        mon._field = FieldMotion(60.0, 90.0, 8.0, 300.0)
        mon._go_blind(T0, "radar")
        self.assertIsNone(mon._field)
        self.assertIsNone(mon._tracker._field)

    def test_status_is_degraded_while_blind_and_stopped_when_not_running(self):
        mon = monitor()
        mon._poll_task = _FakeTask(done=True)
        self.assertEqual(mon.status(), "stopped")

        mon._poll_task = _FakeTask(done=False)
        mon._go_blind(T0, "position")
        self.assertEqual(mon.status(), "degraded")


class _FakeTask:
    def __init__(self, done: bool):
        self._done = done

    def done(self) -> bool:
        return self._done


def _pixel(point):
    from dradis.live_monitors.radar_core import latlon_to_pixel
    col, row = latlon_to_pixel(GT, point[0], point[1])
    return int(col), int(row)


# ── Configuration ─────────────────────────────────────────────────────────────

class ConfigTest(unittest.TestCase):

    def test_min_mmh_defaults_and_clamps(self):
        self.assertEqual(RF._clamp_min_mmh(None), RF.DEFAULT_MIN_MMH)
        self.assertEqual(RF._clamp_min_mmh("nonsense"), RF.DEFAULT_MIN_MMH)
        self.assertEqual(RF._clamp_min_mmh(0), RF.DEFAULT_MIN_MMH)
        self.assertEqual(RF._clamp_min_mmh(-3), RF.DEFAULT_MIN_MMH)
        self.assertEqual(RF._clamp_min_mmh(0.001), RF.MIN_MMH_FLOOR)
        self.assertEqual(RF._clamp_min_mmh(999), RF.MIN_MMH_CEILING)
        self.assertEqual(RF._clamp_min_mmh(2.5), 2.5)

    def test_the_shared_default_radius_is_clamped_into_range(self):
        """LiveMonitorPayload defaults radius_km to 100, far outside the ladder."""
        self.assertLessEqual(monitor(radius_km=100).radius_km, 60.0)

    def test_hail_adds_a_product_to_the_feed_request(self):
        self.assertEqual(monitor().  _products, ("SRI",))
        self.assertEqual(monitor(hail=True)._products, ("SRI", "POH"))

    def test_intensity_labels_cover_the_scale(self):
        self.assertEqual(RF.intensity_label(0.2, "it"), "pioviggine")
        self.assertEqual(RF.intensity_label(1.0, "it"), "debole")
        self.assertEqual(RF.intensity_label(5.0, "it"), "moderata")
        self.assertEqual(RF.intensity_label(20.0, "it"), "forte")
        self.assertEqual(RF.intensity_label(80.0, "it"), "nubifragio")
        self.assertEqual(RF.intensity_label(5.0, "en"), "moderate")


# ── Wording ───────────────────────────────────────────────────────────────────

class MessageTest(unittest.TestCase):

    def test_the_radar_timestamp_is_the_measurement_not_the_send(self):
        """The product is published about ten minutes late. A message that implies
        'now' is a promise the source cannot keep."""
        mon = monitor()
        mon._grid_t = T0
        line = mon._radar_line(T0 + 600.0)
        self.assertIn("10 min fa", line)

    def test_no_radar_line_before_the_first_raster(self):
        self.assertEqual(monitor()._radar_line(T0), "")

    def test_a_measured_encounter_is_quoted_with_its_time(self):
        mon = monitor()
        mon._tracker.set_motion(FieldMotion(40.0, 90.0, 8.0, 300.0))
        alert = ring_alert(track=TRACK_CLOSING)
        mon._tracker.track_verdict(T0, 270.0, 20.0)
        line = mon._track_line(alert)
        self.assertIn("30 min", line)
        self.assertIn("Rotta d'incontro", line)

    def test_an_unmeasured_track_never_quotes_a_time(self):
        mon = monitor()
        mon._tracker.set_motion(None)
        mon._tracker.track_verdict(T0, 270.0, 20.0)
        line = mon._track_line(ring_alert(track=TRACK_CLOSING))
        self.assertNotIn("min", line)
        self.assertIn("Rotta costante", line)

    def test_the_field_line_states_when_motion_is_unmeasurable(self):
        mon = monitor()
        mon._field = None
        self.assertIn("non misurabile", mon._field_line())

    def test_a_confident_standstill_is_worded_as_such(self):
        mon = monitor()
        mon._field = FieldMotion(0.2, 0.0, 9.0, 300.0)
        self.assertIn("stazionaria", mon._field_line())

    def test_a_drift_that_points_away_is_tied_to_the_approach(self):
        """THE message that failed in the field: 'front 19 km to the NW' over
        'heading straight for you' over 'moving NW' — the last two cannot both be
        read as standalone facts. The drift now hangs off the approach."""
        mon = monitor()
        mon._field = FieldMotion(20.0, 315.0, 8.0, 300.0)       # drifting NW
        mon._tracker.set_motion(mon._field)
        mon._tracker.track_verdict(T0, 315.0, 19.0)             # rain NW, going NW
        line = mon._field_line(ring_alert(track=TRACK_CLOSING, bearing_deg=315.0))
        self.assertIn("ma il fronte continua ad avvicinarsi", line)

    def test_a_drift_that_agrees_with_the_approach_is_stated_plainly(self):
        mon = monitor()
        mon._field = FieldMotion(20.0, 90.0, 8.0, 300.0)        # drifting east
        mon._tracker.set_motion(mon._field)
        mon._tracker.track_verdict(T0, 270.0, 19.0)             # rain west, coming east
        line = mon._field_line(ring_alert(track=TRACK_CLOSING))
        self.assertIn("si muove verso", line)
        self.assertNotIn("fronte continua", line)

    def test_the_own_motion_line_is_absent_when_parked(self):
        mon = monitor()
        mon._fix = Fix(moving=False)
        self.assertIsNone(mon._motion_line(ring_alert()))

    def test_heading_into_the_rain_is_called_out(self):
        mon = monitor()
        mon._fix = Fix(moving=True, speed_kmh=95.0, course_deg=270.0)
        self.assertIn("dirigendo verso la pioggia",
                      mon._motion_line(ring_alert(bearing_deg=270.0)))

    def test_a_full_ring_message_carries_the_essentials(self):
        mon = monitor()
        mon._grid_t = T0
        mon._field = FieldMotion(40.0, 90.0, 8.0, 300.0)
        mon._fix = Fix(moving=True, speed_kmh=95.0, course_deg=45.0)
        text = mon._format(ring_alert(), 8.4, None, T0 + 600.0)
        self.assertIn("Casa", text)
        self.assertIn("12 km", text)
        self.assertIn("8.4 mm/h", text)
        self.assertIn("moderata", text)
        self.assertIn("Anello 2/4", text)
        self.assertIn("Radar delle", text)

    def test_hail_appears_only_above_the_threshold(self):
        mon = monitor()
        mon._grid_t = T0
        below = mon._format(ring_alert(), 8.0, RF.HAIL_ALERT_PERCENT - 1, T0)
        above = mon._format(ring_alert(), 8.0, RF.HAIL_ALERT_PERCENT + 1, T0)
        self.assertNotIn("grandine", below)
        self.assertIn("grandine", above)

    def test_the_all_clear_reports_the_closest_approach(self):
        mon = monitor()
        mon._grid_t = T0
        text = mon._format(ClearAlert(ring_count=4, radius_km=30.0, closest_km=7.0,
                                      closest_ring=3, closest_at=T0,
                                      quiet_sec=600.0, event_duration_sec=3600.0),
                           None, None, T0)
        self.assertIn("Pioggia cessata", text)
        self.assertIn("7 km", text)

    def test_english_is_a_complete_alternative(self):
        mon = monitor(language="en")
        mon._grid_t = T0
        text = mon._format(ring_alert(), 8.4, None, T0)
        self.assertIn("Rain", text)
        self.assertIn("Ring 2/4", text)
        self.assertNotIn("Anello", text)

    def test_the_location_name_is_escaped(self):
        mon = monitor(location="Casa & <Orto>")
        self.assertIn("&amp;", mon._loc())
        self.assertNotIn("<Orto>", mon._loc())


# ── Delivery ──────────────────────────────────────────────────────────────────

class DeliveryTest(unittest.IsolatedAsyncioTestCase):
    """Which delivery outcomes advance the notification bookkeeping.

    The field failure this pins down: on 17 Aug 2026 every ring alert of one event
    arrived TWICE, the two copies two minutes apart and sharing a single radar
    frame. The photo upload had timed out after Telegram had already delivered it;
    the monitor read that as "not delivered", held its state and sent the whole
    alert again at the next poll. An unconfirmed send is now committed: at worst
    one message is lost, which is what the deeper rings are for.
    """

    def _monitor(self, send):
        mon = monitor()
        mon._send = send
        mon._grid_t = T0
        return mon

    async def _dispatch(self, mon, ring: int = 2):
        await mon._dispatch(ring_alert(ring=ring), None, [], T0,
                            origin=ORIGIN, effective=ORIGIN,
                            peak_mmh=2.0, hail_percent=None)

    async def test_a_confirmed_send_commits(self):
        async def confirmed(text, photo=None):
            return True

        mon = self._monitor(confirmed)
        await self._dispatch(mon)
        self.assertEqual(mon._tracker.notified_ring, 2)

    async def test_an_unconfirmed_send_commits_and_is_never_repeated(self):
        sent = []

        async def unconfirmed(text, photo=None):
            sent.append(text)
            return None

        mon = self._monitor(unconfirmed)
        await self._dispatch(mon)
        self.assertEqual(mon._tracker.notified_ring, 2)
        # The tracker only re-offers an alert while current_ring > notified_ring.
        self.assertEqual(len(sent), 1)

    async def test_a_refused_send_is_held_and_retried(self):
        sent = []

        async def refused(text, photo=None):
            sent.append(text)
            return False

        mon = self._monitor(refused)
        await self._dispatch(mon)
        await self._dispatch(mon)
        self.assertEqual(mon._tracker.notified_ring, 0)
        self.assertEqual(len(sent), 2)

    async def test_a_send_that_raises_is_held(self):
        async def boom(text, photo=None):
            raise RuntimeError("telegram down")

        mon = self._monitor(boom)
        await self._dispatch(mon)
        self.assertEqual(mon._tracker.notified_ring, 0)


# ── Manager ───────────────────────────────────────────────────────────────────

class ManagerTest(unittest.TestCase):

    def setUp(self):
        self.manager = RF.RainFrontMonitorManager()
        self.started = []

        def fake_start(monitor_self):
            self.started.append(monitor_self.monitor_id)
            monitor_self._poll_task = None

        self._real_start = RF.RainFrontLiveMonitor.start
        RF.RainFrontLiveMonitor.start = fake_start

    def tearDown(self):
        RF.RainFrontLiveMonitor.start = self._real_start

    @staticmethod
    def _cfg(**overrides):
        cfg = {"id": "a", "type": "rain_front", "enabled": True, "name": "Casa",
               "latitude": ORIGIN[0], "longitude": ORIGIN[1], "radius_km": 30.0,
               "ring_count": 4, "min_mmh": 1.0}
        cfg.update(overrides)
        return cfg

    def test_other_monitor_types_are_ignored(self):
        self.manager.reload([self._cfg(type="storm_front")], lambda c: None, "UTC")
        self.assertEqual(self.started, [])

    def test_disabled_monitors_are_not_started(self):
        self.manager.reload([self._cfg(enabled=False)], lambda c: None, "UTC")
        self.assertEqual(self.started, [])

    def test_an_unrelated_change_does_not_restart_a_running_monitor(self):
        """Every manager is handed the whole config list on every save. Restarting
        on an unrelated edit would throw away the event in progress."""
        self.manager.reload([self._cfg()], lambda c: None, "UTC")
        self.assertEqual(self.started, ["a"])
        running = self.manager._monitors["a"]
        running.is_running = lambda: True
        self.manager.reload([self._cfg(created_at=12345, note="irrelevant")],
                            lambda c: None, "UTC")
        self.assertEqual(self.started, ["a"])

    def test_a_meaningful_change_does_restart(self):
        self.manager.reload([self._cfg()], lambda c: None, "UTC")
        self.manager._monitors["a"].is_running = lambda: True
        self.manager.reload([self._cfg(min_mmh=5.0)], lambda c: None, "UTC")
        self.assertEqual(self.started, ["a", "a"])

    def test_the_position_it_follows_is_part_of_the_fingerprint(self):
        self.manager.reload([self._cfg()], lambda c: None, "UTC")
        self.manager._monitors["a"].is_running = lambda: True
        self.manager.reload([self._cfg(position_id="p1")], lambda c: None, "UTC")
        self.assertEqual(self.started, ["a", "a"])

    def test_status_of_an_unknown_monitor_is_stopped(self):
        self.assertEqual(self.manager.status("nope"), "stopped")


if __name__ == "__main__":
    unittest.main()
