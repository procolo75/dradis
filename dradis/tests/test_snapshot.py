"""
tests/test_snapshot.py
───────────────────────
The on-demand snapshots behind `/rain` and `/storm`.

    cd dradis && python3 -m unittest discover tests

The first class is the one that matters. A snapshot perceives without deciding,
and if that ever stops being true the failure is silent: a test run during a
quiet afternoon would advance `notified_ring` and mute the real storm hours
later. Everything else here is wording.

aiomqtt is stubbed before the import under test, as in test_storm_front_position.
"""

import asyncio
import sys
import tempfile
import types
import unittest
from unittest import mock

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

from dradis.live_monitors import rain_front as RF                    # noqa: E402
from dradis.live_monitors import snapshot as SNAP                    # noqa: E402
from dradis.live_monitors import storm_front as SF                   # noqa: E402
from dradis.live_monitors.geo import offset_km                       # noqa: E402
from dradis.live_monitors.radar_core import (                        # noqa: E402
    GeoTransform, RadarGrid, latlon_to_pixel, pixel_to_latlon,
)
from dradis.live_monitors.snapshot import (                          # noqa: E402
    OriginInfo, Snapshot, describe_origin, format_caption, preview_alert,
)
from dradis.live_monitors.storm_front_core import (                  # noqa: E402
    EVENT_ACTIVE, build_frame, ring_edges,
)

RF.STATE_PATH = tempfile.mktemp(suffix="-snapshot-rain-state.json")
SF.STATE_PATH = tempfile.mktemp(suffix="-snapshot-storm-state.json")

T0 = 1_700_000_000.0
COLS = ROWS = 400
GT = GeoTransform(cols=COLS, rows=ROWS, pixel_m=1000.0,
                  x0=-200000.0, y0=200000.0, lon0=12.5, lat0=42.0)
SITE = tuple(float(v) for v in pixel_to_latlon(GT, COLS / 2, ROWS / 2))


def rain_monitor(**overrides):
    cfg = {"id": "r1", "name": "Casa", "location": "Casa", "language": "it",
           "latitude": SITE[0], "longitude": SITE[1], "radius_km": 30.0,
           "ring_count": 4, "min_mmh": 1.0, "chart": False}
    cfg.update(overrides)
    return RF.RainFrontLiveMonitor(cfg, telegram_send_fn=None, tz_name="UTC")


def storm_monitor(**overrides):
    cfg = {"id": "s1", "name": "Bacoli", "location": "Bacoli", "language": "it",
           "latitude": SITE[0], "longitude": SITE[1], "radius_km": 30.0,
           "ring_count": 4, "chart": False}
    cfg.update(overrides)
    return SF.StormFrontLiveMonitor(cfg, telegram_send_fn=None, tz_name="UTC")


def wet_grid(t: float = T0) -> RadarGrid:
    """A band of rain a few km west of SITE."""
    data = np.zeros((ROWS, COLS), dtype=np.float32)
    col, row = latlon_to_pixel(GT, *SITE)
    col, row = int(col), int(row)
    data[row - 30:row + 30, col - 22:col - 8] = 8.0
    return RadarGrid(t=t, product="SRI", data=data, gt=GT)


def strikes_at(bearing_deg: float, km: float, count: int, now: float):
    lat, lon = offset_km(SITE[0], SITE[1],
                         km * np.cos(np.radians(bearing_deg)),
                         km * np.sin(np.radians(bearing_deg)))
    return [(now - i, float(lat), float(lon)) for i in range(count)]


class FakeState:
    def __init__(self, lat, lon, age_sec=30.0, accuracy_m=10.0,
                 speed_kmh=None, course_deg=None, moving=False, discontinuity=0):
        self.lat, self.lon = lat, lon
        self.t = T0 - age_sec
        self.age_sec = age_sec
        self.accuracy_m = accuracy_m
        self.speed_kmh = speed_kmh
        self.course_deg = course_deg
        self.moving = moving
        self.discontinuity = discontinuity


class FakeSource:
    def __init__(self, max_accuracy_m=500.0):
        self.max_accuracy_m = max_accuracy_m


class FakeManager:
    def __init__(self, name="Telefono", state=None, max_age=900.0,
                 max_accuracy=500.0):
        self._name, self._state = name, state
        self._max_age = max_age
        self._source = FakeSource(max_accuracy)

    def name_of(self, pid):
        return self._name

    def current(self, pid, now=None):
        return self._state

    def usable(self, pid, now=None, max_age_sec=None):
        return self._state

    def max_age_sec(self, pid):
        return self._max_age

    def get(self, pid):
        return self._source


# ── The invariant ─────────────────────────────────────────────────────────────

class PerceiveWithoutDecidingTest(unittest.TestCase):
    """A snapshot must leave the decision state byte-identical.

    `notified_ring` is what bounds the messages of one event. A snapshot that
    advanced it would silence the real alert; one that opened an event would let
    a single cell emit a second full ladder. Neither would be noticed until
    weather arrived.
    """

    def test_rain_snapshot_does_not_touch_the_tracker(self):
        monitor = rain_monitor()
        before = monitor._tracker.to_dict()
        asyncio.run(monitor.snapshot(T0 + 600, grid=wet_grid()))
        self.assertEqual(monitor._tracker.to_dict(), before)

    def test_rain_snapshot_does_not_touch_an_OPEN_event(self):
        """The dangerous case: state already advanced, and a snapshot on top."""
        monitor = rain_monitor()
        monitor._tracker.event_state = EVENT_ACTIVE
        monitor._tracker.current_ring = 2
        monitor._tracker.notified_ring = 2
        monitor._tracker.event_started_at = T0
        before = monitor._tracker.to_dict()
        asyncio.run(monitor.snapshot(T0 + 600, grid=wet_grid()))
        after = monitor._tracker.to_dict()
        self.assertEqual(after, before)
        self.assertEqual(after["notified_ring"], 2)
        self.assertEqual(after["event_state"], EVENT_ACTIVE)

    def test_repeated_snapshots_never_drift(self):
        monitor = rain_monitor()
        before = monitor._tracker.to_dict()
        for _ in range(5):
            asyncio.run(monitor.snapshot(T0 + 600, grid=wet_grid()))
        self.assertEqual(monitor._tracker.to_dict(), before)

    def test_storm_snapshot_does_not_touch_the_tracker(self):
        monitor = storm_monitor()
        monitor.is_running = lambda: True
        monitor._feed.strikes = lambda now: strikes_at(270.0, 12.0, 30, now)
        monitor._feed.feed_ok = lambda: True
        before = monitor._tracker.to_dict()
        asyncio.run(monitor.snapshot(T0))
        self.assertEqual(monitor._tracker.to_dict(), before)

    def test_a_snapshot_reports_the_event_it_refuses_to_advance(self):
        monitor = rain_monitor()
        monitor._tracker.event_state = EVENT_ACTIVE
        monitor._tracker.notified_ring = 3
        snap = asyncio.run(monitor.snapshot(T0 + 600, grid=wet_grid()))
        self.assertTrue(snap.event_open)
        self.assertEqual(snap.notified_ring, 3)


# ── Perception ────────────────────────────────────────────────────────────────

class RainSnapshotTest(unittest.TestCase):

    def test_it_sees_the_band(self):
        snap = asyncio.run(rain_monitor().snapshot(T0 + 600, grid=wet_grid()))
        self.assertEqual(snap.blind_reason, "")
        self.assertIsNotNone(snap.front_km)
        self.assertAlmostEqual(snap.front_bearing_deg, 270.0, delta=25.0)
        self.assertGreater(snap.peak_mmh, 7.0)

    def test_an_injected_grid_marks_the_snapshot_as_one_shot(self):
        """The caller fetched the image itself, so there is no second frame and
        therefore no drift to report — stated rather than silently omitted."""
        snap = asyncio.run(rain_monitor().snapshot(T0 + 600, grid=wet_grid()))
        self.assertTrue(snap.one_shot)
        self.assertIsNone(snap.field_speed_kmh)

    def test_a_dry_sky_is_not_blindness(self):
        dry = RadarGrid(T0, "SRI", np.zeros((ROWS, COLS), dtype=np.float32), GT)
        snap = asyncio.run(rain_monitor().snapshot(T0 + 600, grid=dry))
        self.assertEqual(snap.blind_reason, "")
        self.assertIsNone(snap.front_km)

    def test_a_blind_spot_is_reported_as_one(self):
        blind = RadarGrid(T0, "SRI",
                          np.full((ROWS, COLS), -9999.0, dtype=np.float32), GT)
        snap = asyncio.run(rain_monitor().snapshot(T0 + 600, grid=blind))
        self.assertIn("visible", snap.blind_reason)
        self.assertIsNone(snap.front_km)

    def test_no_radar_at_all_is_reported(self):
        snap = asyncio.run(rain_monitor().snapshot(T0))
        self.assertIn("radar", snap.blind_reason)

    def test_the_chart_is_skipped_when_the_monitor_has_it_off(self):
        snap = asyncio.run(rain_monitor(chart=False).snapshot(T0 + 600,
                                                              grid=wet_grid()))
        self.assertIsNone(snap.picture)


class StormSnapshotTest(unittest.TestCase):

    def test_a_stopped_monitor_explains_why_it_cannot_show_lightning(self):
        """Unlike rain, there is no on-demand fetch: the buffer only fills while
        the subscription is up."""
        snap = asyncio.run(storm_monitor().snapshot(T0))
        self.assertIn("stopped", snap.blind_reason)
        self.assertEqual(snap.kind, "storm")

    def test_a_running_monitor_reports_the_front(self):
        monitor = storm_monitor()
        monitor.is_running = lambda: True
        monitor._feed.strikes = lambda now: strikes_at(0.0, 15.0, 40, now)
        monitor._feed.feed_ok = lambda: True
        snap = asyncio.run(monitor.snapshot(T0))
        self.assertEqual(snap.blind_reason, "")
        self.assertIsNotNone(snap.front_km)
        self.assertGreater(snap.activity, 0)

    def test_a_disconnected_feed_is_reported(self):
        monitor = storm_monitor()
        monitor.is_running = lambda: True
        monitor._feed.strikes = lambda now: []
        monitor._feed.feed_ok = lambda: False
        snap = asyncio.run(monitor.snapshot(T0))
        self.assertFalse(snap.feed_connected)


# ── Origin ────────────────────────────────────────────────────────────────────

class DescribeOriginTest(unittest.TestCase):

    def test_a_fixed_monitor_reports_its_configured_point(self):
        origin = describe_origin(rain_monitor(), T0)
        self.assertFalse(origin.following)
        self.assertTrue(origin.usable)
        self.assertAlmostEqual(origin.lat, SITE[0], places=6)

    def test_a_followed_position_reports_the_fix_and_its_motion(self):
        state = FakeState(45.0, 9.0, age_sec=30.0, accuracy_m=12.0,
                          speed_kmh=96.0, course_deg=45.0, moving=True)
        with mock.patch.object(SNAP, "position_manager", FakeManager(state=state)):
            origin = describe_origin(rain_monitor(position_id="p1"), T0)
        self.assertTrue(origin.following)
        self.assertTrue(origin.usable)
        self.assertEqual(origin.heading_label("it"), "NE")
        self.assertAlmostEqual(origin.lat, 45.0, places=6)

    def test_a_stale_fix_is_shown_WITH_the_reason_it_is_unusable(self):
        """`usable()` would return None here, which is exactly the moment the
        user needs an answer. `current()` plus the threshold gives them one."""
        state = FakeState(45.0, 9.0, age_sec=2460.0)
        with mock.patch.object(SNAP, "position_manager",
                               FakeManager(state=state, max_age=900.0)):
            origin = describe_origin(rain_monitor(position_id="p1"), T0)
        self.assertFalse(origin.usable)
        self.assertIn("41 min old", origin.reason)
        self.assertTrue(origin.has_fix)          # still shown, not swallowed
        self.assertIn("45.00000", origin.map_url)

    def test_an_imprecise_fix_is_shown_with_its_reason(self):
        state = FakeState(45.0, 9.0, age_sec=10.0, accuracy_m=1200.0)
        with mock.patch.object(SNAP, "position_manager",
                               FakeManager(state=state, max_accuracy=500.0)):
            origin = describe_origin(rain_monitor(position_id="p1"), T0)
        self.assertFalse(origin.usable)
        self.assertIn("1200 m", origin.reason)

    def test_a_deleted_position_is_named_as_such(self):
        manager = FakeManager()
        manager.name_of = lambda pid: None
        with mock.patch.object(SNAP, "position_manager", manager):
            origin = describe_origin(rain_monitor(position_id="gone"), T0)
        self.assertTrue(origin.missing)
        self.assertFalse(origin.usable)
        self.assertFalse(origin.has_fix)

    def test_no_fix_yet_is_distinct_from_a_stale_one(self):
        with mock.patch.object(SNAP, "position_manager", FakeManager(state=None)):
            origin = describe_origin(rain_monitor(position_id="p1"), T0)
        self.assertFalse(origin.usable)
        self.assertIn("no fix", origin.reason)
        self.assertFalse(origin.missing)

    def test_the_map_url_carries_the_coordinates(self):
        origin = describe_origin(rain_monitor(), T0)
        self.assertIn(f"mlat={SITE[0]:.5f}", origin.map_url)
        self.assertIn("openstreetmap.org", origin.map_url)

    def test_no_fix_means_no_map_url(self):
        self.assertEqual(OriginInfo(None, None, True, "x", False).map_url, "")


# ── Preview alert ─────────────────────────────────────────────────────────────

class PreviewAlertTest(unittest.TestCase):

    def setUp(self):
        self.edges = ring_edges(30.0, 4)

    def test_no_activity_means_no_preview(self):
        frame = build_frame([], SITE, T0, 30.0, 48.0, 600.0)
        self.assertIsNone(preview_alert(frame, self.edges, 4))

    def test_a_front_outside_the_radius_still_draws(self):
        """Ring 0 means "outside"; the picture is still worth showing, so the
        display ring is clamped rather than the alert dropped."""
        frame = build_frame(strikes_at(0.0, 40.0, 20, T0), SITE, T0, 30.0, 48.0, 600.0)
        alert = preview_alert(frame, self.edges, 4)
        self.assertIsNotNone(alert)
        self.assertEqual(alert.ring, 1)
        self.assertGreater(alert.front_km, 30.0)

    def test_an_inner_front_is_marked_innermost(self):
        frame = build_frame(strikes_at(0.0, 3.0, 20, T0), SITE, T0, 30.0, 48.0, 600.0)
        alert = preview_alert(frame, self.edges, 4)
        self.assertEqual(alert.ring, 4)
        self.assertTrue(alert.is_innermost)


# ── Caption ───────────────────────────────────────────────────────────────────

def snap(**overrides) -> Snapshot:
    fields = dict(monitor_id="m", name="Casa", kind="rain", language="it",
                  tz_name="Europe/Rome", status="running", running=True,
                  origin=OriginInfo(SITE[0], SITE[1], False, "Casa", True))
    fields.update(overrides)
    return Snapshot(**fields)


class CaptionTest(unittest.TestCase):

    def test_it_always_says_it_changed_nothing(self):
        """The promise the whole design rests on, stated in every message."""
        self.assertIn("nessun avviso inviato", format_caption(snap()))
        self.assertIn("niente cambiato", format_caption(snap()))
        self.assertIn("no alert sent", format_caption(snap(language="en")))

    def test_the_coordinates_and_the_map_link_are_both_present(self):
        text = format_caption(snap())
        self.assertIn(f"{SITE[0]:.5f}", text)
        self.assertIn("openstreetmap.org", text)

    def test_a_followed_position_reports_age_accuracy_and_motion(self):
        origin = OriginInfo(45.0, 9.0, True, "Telefono", True, age_sec=47.0,
                            accuracy_m=12.0, speed_kmh=96.0, course_deg=45.0,
                            moving=True)
        text = format_caption(snap(origin=origin))
        self.assertIn("Telefono", text)
        self.assertIn("47 s", text)
        self.assertIn("±12 m", text)
        self.assertIn("96 km/h verso NE", text)

    def test_no_distance_from_the_configured_location_is_claimed(self):
        """For a monitor that follows a phone the configured coordinates are dead
        config, so "176 km from the configured location" anchored to nothing.
        v4.1.1 removed the same false anchor from /monitors."""
        origin = OriginInfo(45.0, 9.0, True, "Telefono", True, age_sec=47.0)
        text = format_caption(snap(origin=origin))
        self.assertNotIn("configurata", text)
        self.assertNotIn("configured location", text)

    def test_a_fresh_fix_does_not_read_as_zero_seconds_ago(self):
        origin = OriginInfo(45.0, 9.0, True, "Telefono", True, age_sec=0.0)
        self.assertIn("appena aggiornato", format_caption(snap(origin=origin)))

    def test_a_blind_monitor_explains_itself(self):
        origin = OriginInfo(45.0, 9.0, True, "Telefono", False,
                            reason="the last fix is 41 min old, past the 15 min limit")
        text = format_caption(snap(origin=origin, blind_reason=origin.reason))
        self.assertIn("Cieco", text)
        self.assertIn("41 min", text)
        self.assertIn("non manda avvisi", text)

    def test_an_open_event_is_reported_read_only(self):
        text = format_caption(snap(event_open=True, notified_ring=2, ring_count=4))
        self.assertIn("Evento aperto", text)
        self.assertIn("2/4", text)

    def test_voice_mode_drops_every_line_about_the_instrument(self):
        """Car Mode is not a text filter applied afterwards.

        Coordinates, the map link and "fix appena aggiornato · ±12 m · non si sta
        muovendo" are whole LINES that only mean something on a screen — you can
        neither tap a link nor read a coordinate at the wheel, and being told your
        phone is not moving while you drive is worse than useless. Stripping icons
        would not have saved any of them, so they are omitted at the source.
        """
        origin = OriginInfo(45.0, 9.0, True, "Telefono", True, age_sec=0.0,
                            accuracy_m=12.0, speed_kmh=0.0, moving=False)
        text = format_caption(snap(origin=origin, coverage=0.98), voice=True)
        for banned in [f"{45.0:.5f}", "openstreetmap.org", "apri la mappa",
                       "appena aggiornato", "±12 m", "non si sta muovendo",
                       "copertura", "Nessun evento aperto", "nessun avviso inviato"]:
            self.assertNotIn(banned, text, f"voice mode still says {banned!r}")

    def test_voice_mode_keeps_what_you_asked_the_question_for(self):
        text = format_caption(snap(front_km=8.0, front_bearing_deg=270.0),
                              voice=True)
        self.assertIn("Casa", text)
        self.assertIn("8 km", text)

    def test_voice_mode_never_hides_a_failure(self):
        """Silence and calm must not sound the same. Every line that explains why
        nothing is arriving survives, or the command answers the wrong question."""
        origin = OriginInfo(45.0, 9.0, True, "Telefono", False,
                            reason="the last fix is 41 min old, past the 15 min limit")
        blind = format_caption(snap(origin=origin, blind_reason=origin.reason),
                               voice=True)
        self.assertIn("Cieco", blind)
        self.assertIn("41 min", blind)

        off = format_caption(snap(running=False, status="stopped"), voice=True)
        self.assertIn("anteprima", off)
        self.assertIn("Spento", off)

        gone = OriginInfo(0.0, 0.0, True, "Telefono", False, missing=True)
        self.assertIn("non esiste più", format_caption(snap(origin=gone), voice=True))

    def test_voice_mode_stays_quiet_about_a_healthy_monitor(self):
        """"Active" every time is a word you learn to talk over — and then you
        talk over the one time it says "Off"."""
        self.assertNotIn("Attivo", format_caption(snap(), voice=True))
        self.assertIn("Attivo", format_caption(snap()))

    def test_the_default_is_unchanged(self):
        """Everything above is opt-in; the on-screen caption keeps every word."""
        text = format_caption(snap())
        self.assertIn(f"{SITE[0]:.5f}", text)
        self.assertIn("nessun avviso inviato", text)

    def test_a_stopped_monitor_is_called_a_preview(self):
        """"Stopped" beside a live picture reads as a contradiction otherwise."""
        text = format_caption(snap(running=False, status="stopped"))
        self.assertIn("anteprima", text)

    def test_rain_wording_covers_measured_and_unmeasured_drift(self):
        measured = format_caption(snap(front_km=12.0, front_bearing_deg=225.0,
                                       peak_mmh=8.4, field_speed_kmh=60.0,
                                       field_bearing_deg=90.0,
                                       encounter_minutes=14.0,
                                       encounter_miss_km=1.0))
        self.assertIn("moderata", measured)
        self.assertIn("60 km/h", measured)
        self.assertIn("14 min", measured)

        unmeasured = format_caption(snap(front_km=12.0, front_bearing_deg=225.0))
        self.assertIn("non misurabile", unmeasured)
        self.assertNotIn("min,", unmeasured)

    def test_a_standstill_is_not_dressed_as_a_heading(self):
        """A confidently measured standstill carries speed 0 and bearing 0, which
        the `is None` test let through as 'verso N a 0 km/h'."""
        text = format_caption(snap(front_km=12.0, front_bearing_deg=225.0,
                                   field_speed_kmh=0.0, field_bearing_deg=0.0))
        self.assertIn("stazionaria", text)
        self.assertNotIn("0 km/h", text)

    def test_the_caption_answers_whether_it_is_raining_on_you(self):
        text = format_caption(snap(front_km=3.0, front_bearing_deg=225.0,
                                   peak_mmh=4.0, overhead_mmh=2.4))
        self.assertIn("Su di te: 2.4 mm/h", text)

    def test_nothing_overhead_is_not_reported_as_zero(self):
        text = format_caption(snap(front_km=3.0, front_bearing_deg=225.0,
                                   peak_mmh=4.0, overhead_mmh=0.0))
        self.assertNotIn("Su di te", text)

    def test_storm_wording_counts_strikes(self):
        text = format_caption(snap(kind="storm", front_km=18.0,
                                   front_bearing_deg=315.0, activity=22))
        self.assertIn("22 fulmini", text)
        self.assertIn("18 km", text)

    def test_the_caption_fits_a_telegram_photo(self):
        """Telegram caps photo captions at 1024 characters; overflowing fails the
        send outright, taking the picture with it."""
        origin = OriginInfo(45.123456, 9.123456, True, "Un nome piuttosto lungo",
                            True, age_sec=47.0, accuracy_m=12.0, speed_kmh=96.0,
                            course_deg=45.0, moving=True)
        text = format_caption(snap(origin=origin, front_km=12.0,
                                   front_bearing_deg=225.0, peak_mmh=8.4,
                                   field_speed_kmh=60.0, field_bearing_deg=90.0,
                                   encounter_minutes=14.0, encounter_miss_km=1.0,
                                   event_open=True, notified_ring=2))
        self.assertLess(len(text), 1024)

    def test_the_name_is_escaped(self):
        self.assertIn("&lt;", format_caption(snap(name="Casa <b>")))

    def test_the_badge_describes_the_MONITOR_not_its_feed(self):
        """`status()` returns the FEED's state whenever the poll task is alive, so
        an active storm monitor whose Blitzortung subscription has not connected
        reported "stopped". Active-and-blind and switched-off are opposite
        instructions to the user and must not share a badge."""
        text = format_caption(snap(kind="storm", running=True, status="stopped",
                                   feed_connected=False, front_km=None))
        self.assertIn("Attivo", text)
        self.assertNotIn("Spento", text)
        self.assertIn("non connesso", text)      # the feed, reported separately

    def test_a_healthy_feed_earns_no_line(self):
        text = format_caption(snap(kind="storm", running=True, status="running",
                                   feed_connected=True, activity=5))
        self.assertNotIn("non connesso", text)

    def test_a_switched_off_monitor_is_not_called_active(self):
        text = format_caption(snap(running=False, status="stopped"))
        self.assertIn("Spento", text)
        self.assertNotIn("🟢", text)

    def test_the_radar_clock_uses_the_configured_timezone(self):
        """It used the container's clock, which is UTC, so the radar time
        disagreed with every other message DRADIS sends."""
        noon_utc = 1_700_000_000.0 - (1_700_000_000.0 % 86400) + 12 * 3600
        rome = format_caption(snap(tz_name="Europe/Rome", radar_t=noon_utc,
                                   radar_age_sec=600.0, front_km=5.0,
                                   front_bearing_deg=0.0))
        utc = format_caption(snap(tz_name="UTC", radar_t=noon_utc,
                                  radar_age_sec=600.0, front_km=5.0,
                                  front_bearing_deg=0.0))
        self.assertIn("12:00", utc)
        self.assertIn("13:00", rome)             # CET/CEST is ahead of UTC

    def test_an_unknown_timezone_falls_back_instead_of_raising(self):
        text = format_caption(snap(tz_name="Mars/Olympus", radar_t=T0,
                                   radar_age_sec=60.0))
        self.assertIn("📡", text)


# ── Managers ──────────────────────────────────────────────────────────────────

class ManagerAccessorTest(unittest.TestCase):

    def test_get_returns_none_for_an_unknown_monitor(self):
        self.assertIsNone(RF.rain_front_monitor_manager.get("nope"))
        self.assertIsNone(SF.storm_front_monitor_manager.get("nope"))


if __name__ == "__main__":
    unittest.main()
