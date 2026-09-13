"""
tests/test_rain_front_gauges.py
────────────────────────────────
The MeteoHub ground-truth line in `rain_front`, and the promise that it is only
a line.

    cd dradis && python3 -m unittest discover tests

The whole point of this file is the first test class. `rain_front` alerts on the
DPC radar composite and must go on alerting on it alone: a rain gauge publishes
on a ten-minute cadence with its own lag, a station that has stopped
transmitting returns the same nothing as a station standing in the dry, and zero
millimetres seven kilometres away says nothing about a cell at three. So the
reading is shown and never consulted, and `MessageIsUnchanged` asserts that by
diffing whole messages rather than by trusting the comment that says so.

Also pinned:
  · The line appears on EVERY alert, including when nothing is wet and when the
    network is down. If it only appeared on confirmation, its absence would
    become an assertion — the authority this reading does not have.
  · Three states, not two. Switched off says nothing; asked-and-unanswered says
    so; asked-and-answered reports. A single None cannot carry that.
  · A gauge failure costs a line, never the alert — the guarantee already given
    to the chart.
  · The tracker is not touched. `notified_ring` is what bounds an event to
    `ring_count` messages plus one all-clear; a diagnostic that advanced it
    would silence the real alert half an hour later.
  · The all-clear is never delayed or blocked by a wet gauge.
"""

import asyncio
import sys
import tempfile
import types
import unittest

if "aiomqtt" not in sys.modules:                                      # noqa: E402
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

try:
    from dradis.live_monitors import gauges as GS                     # noqa: E402
    from dradis.live_monitors import rain_front as RF                 # noqa: E402
except ModuleNotFoundError as e:          # httpx absent outside the add-on image
    raise unittest.SkipTest(str(e))

from dradis.live_monitors.gauges_core import (                        # noqa: E402
    GaugeReading, GaugeView, Measurement, VAR_GUST, VAR_PRECIP, VAR_TEMP,
)
from dradis.live_monitors.storm_front_core import (                   # noqa: E402
    ClearAlert, RingAlert,
)

RF.STATE_PATH = tempfile.mktemp(suffix="-rain-front-gauge-state.json")

T0 = 1_700_000_000.0
ORIGIN = (40.79361, 14.16389)          # Nisida, where the fixture was captured


def monitor(**overrides) -> RF.RainFrontLiveMonitor:
    cfg = {"id": "g1", "name": "Casa", "location": "Casa", "language": "it",
           "latitude": ORIGIN[0], "longitude": ORIGIN[1], "radius_km": 30.0,
           "ring_count": 4, "min_mmh": 1.0, "chart": False}
    cfg.update(overrides)
    return RF.RainFrontLiveMonitor(cfg, telegram_send_fn=None, tz_name="UTC")


def reading(name="Nisida METEO", net="dpcn-campania", mmh=4.2,
            km=7.0, bearing=225.0, gust_ms=None, temp_k=None) -> GaugeReading:
    """A station reading built by hand.

    `mmh`, `gust_kmh` and `temp_c` are derived properties now — each quantity
    carries its own timestamp, because the gust is an hourly maximum while the
    rain arrives every ten minutes. The assertions below are unchanged; only
    this fixture knows the difference.
    """
    m = {VAR_PRECIP: Measurement(mmh, T0 - 600, 15)} if mmh is not None else {}
    if gust_ms is not None:
        m[VAR_GUST] = Measurement(gust_ms, T0 - 2400, 60)
    if temp_k is not None:
        m[VAR_TEMP] = Measurement(temp_k, T0 - 600, 0)
    return GaugeReading(name=name, network=net, lat=ORIGIN[0], lon=ORIGIN[1],
                        distance_km=km, bearing_deg=bearing, measurements=m)


def empty_view(radius_km: float = 45.0) -> GaugeView:
    """Asked and answered, with nothing recent in range."""
    return GaugeView(readings=(), wet=(), nearest=None,
                     radius_km=radius_km, fetched_at=T0)


def view(*readings, min_mmh: float = 1.0, radius_km: float = 45.0) -> GaugeView:
    ordered = tuple(sorted(readings, key=lambda r: (-(r.mmh or 0.0),
                                                    r.distance_km)))
    wet = tuple(r for r in ordered if r.mmh is not None and r.mmh >= min_mmh)
    gauges = [r for r in ordered if r.mmh is not None]
    return GaugeView(readings=ordered, wet=wet,
                     nearest=min(gauges, key=lambda r: r.distance_km)
                     if gauges else None,
                     radius_km=radius_km, fetched_at=T0)


def ring_alert(**overrides) -> RingAlert:
    fields = dict(ring=2, ring_count=4, ring_edge_km=19.5, front_km=12.0,
                  bearing_deg=270.0, sector=9, strikes=120, strikes_in_radius=200)
    fields.update(overrides)
    return RingAlert(**fields)


def clear_alert(**overrides) -> ClearAlert:
    fields = dict(radius_km=30.0, quiet_sec=1800.0, closest_km=8.0,
                  closest_ring=1, closest_at=T0 - 3600, ring_count=4,
                  event_duration_sec=5400.0)
    fields.update(overrides)
    return ClearAlert(**fields)


class _Enabled:
    """MeteoHub switched on for the duration of a test."""

    def __enter__(self):
        GS.configure({"meteohub_enabled": True})
        return self

    def __exit__(self, *exc):
        GS.configure({"meteohub_enabled": False})
        return False


# ── The promise ───────────────────────────────────────────────────────────────

class MessageIsUnchanged(unittest.TestCase):
    """Everything except the gauge line is byte-identical, in every state.

    This is the test that makes the constraint real. If a future edit lets the
    reading change a heading, a ring, or a verdict, the diff shows up here as a
    second changed line rather than as a surprise during weather.
    """

    def _lines(self, mon, alert, gauges, **kw):
        text = mon._format(alert, kw.get("peak_mmh", 6.0), None, T0,
                           kw.get("overhead_mmh"), gauges)
        return [ln for ln in text.split("\n") if not ln.startswith("🎚️")]

    def _cases(self, mon, alert, **kw):
        """Every gauge state, against the same alert."""
        with _Enabled():
            return {
                "unreachable": self._lines(mon, alert, None, **kw),
                "dry": self._lines(mon, alert, view(reading(mmh=0.0)), **kw),
                "no recent reading": self._lines(mon, alert, empty_view(), **kw),
                "wet": self._lines(mon, alert, view(reading(mmh=9.9)), **kw),
                "many": self._lines(mon, alert,
                                    view(reading(mmh=9.9),
                                         reading(name="Pozzuoli", mmh=6.1,
                                                 km=4.0, bearing=10.0)), **kw),
            }

    def test_ring_alert_identical_in_every_gauge_state(self):
        mon = monitor()
        off = self._lines(mon, ring_alert(), None)       # feature off entirely
        for label, lines in self._cases(mon, ring_alert()).items():
            with self.subTest(state=label):
                self.assertEqual(lines, off)

    def test_overhead_claim_survives_a_dry_co_located_gauge(self):
        """The radar's ground claim is still the radar's to make.

        A station inside the overhead disc reporting nothing is the most
        tempting case for letting the gauge win, and it is refused: the gauge
        is later than the radar and its zero is ambiguous. The contradiction is
        shown to the reader on its own line, not resolved on their behalf.
        """
        mon = monitor()
        alert = ring_alert(ring=4, ring_edge_km=7.5, front_km=1.0,
                           is_innermost=True)
        dry_next_door = view(reading(mmh=0.0, km=1.2))
        with _Enabled():
            text = mon._format(alert, 6.0, None, T0, 5.0, dry_next_door)
        self.assertIn("Pioggia su di te", text)
        self.assertIn("Sei sotto la pioggia", text)
        self.assertEqual(self._lines(mon, alert, dry_next_door,
                                     peak_mmh=6.0, overhead_mmh=5.0),
                         self._lines(mon, alert, None,
                                     peak_mmh=6.0, overhead_mmh=5.0))

    def test_all_clear_identical_and_never_delayed(self):
        mon = monitor()
        alert = clear_alert()
        off = mon._fmt_clear(alert, T0, None)
        with _Enabled():
            wet = mon._fmt_clear(alert, T0, view(reading(mmh=9.9)))
        self.assertIn("Pioggia cessata", wet)
        self.assertEqual([ln for ln in wet.split("\n")
                          if not ln.startswith("🎚️")],
                         off.split("\n"))


# ── The line itself ───────────────────────────────────────────────────────────

class GaugeLine(unittest.TestCase):

    def line(self, mon, gauges) -> str:
        with _Enabled():
            return mon._gauge_line(gauges)

    def test_names_the_station_that_measured_the_rain(self):
        got = self.line(monitor(), view(reading()))
        self.assertIn("Nisida METEO", got)
        self.assertIn("4.2 mm/h", got)
        self.assertIn("7 km", got)
        self.assertIn("dpcn-campania", got)      # the CC BY attribution

    def test_several_wet_stations_lead_with_the_strongest(self):
        got = self.line(monitor(), view(reading(mmh=4.2),
                                        reading(name="Pozzuoli", mmh=6.1,
                                                km=4.0, bearing=10.0)))
        self.assertIn("2 stazioni bagnate", got)
        self.assertIn("Pozzuoli", got)
        self.assertIn("6.1 mm/h", got)

    def test_dry_reports_where_the_instruments_are(self):
        """An absence stated as an absence, never as a denial."""
        got = self.line(monitor(), view(reading(mmh=0.0)))
        self.assertIn("Nessuna stazione bagnata", got)
        self.assertIn("Nisida METEO", got)
        self.assertNotIn("non piove", got.lower())

    def test_unreachable_says_so(self):
        self.assertIn("non raggiungibile", self.line(monitor(), None))

    def test_no_recent_reading_is_not_the_same_as_unreachable(self):
        """Three states, three sentences.

        A network lagging further than the query window answers normally with
        nothing inside it — measured at 52-132 min for dpcn-puglia against
        12 min for sir-toscana. Calling that "unreachable" blames the service
        for a property of the data.
        """
        got = self.line(monitor(), empty_view())
        self.assertIn("Nessuna lettura recente", got)
        self.assertNotIn("non raggiungibile", got)

    def test_reading_age_is_printed(self):
        """The lag runs from twelve minutes to over an hour by network, so a
        clock alone leaves the reader to work out how old the fact is."""
        self.assertIn("min fa", self.line(monitor(), view(reading())))

    def test_silent_when_the_feature_is_off(self):
        """Three states, not two: off is not the same as unreachable."""
        GS.configure({"meteohub_enabled": False})
        self.assertEqual(monitor()._gauge_line(None), "")
        with _Enabled():
            self.assertEqual(monitor(ground_truth=False)._gauge_line(None), "")

    def test_english(self):
        mon = monitor(language="en")
        with _Enabled():
            self.assertIn("Measured", mon._gauge_line(view(reading())))
            self.assertIn("No station reporting rain",
                          mon._gauge_line(view(reading(mmh=0.0))))
            self.assertIn("unreachable", mon._gauge_line(None))
            self.assertIn("No recent reading", mon._gauge_line(empty_view()))
            self.assertIn("min ago", mon._gauge_line(view(reading())))


# ── Failure and state ─────────────────────────────────────────────────────────

class FailureIsolation(unittest.TestCase):

    def test_a_raising_source_costs_a_line_not_the_alert(self):
        mon = monitor()

        async def boom(*a, **kw):
            raise RuntimeError("MeteoHub down")

        with _Enabled():
            original, GS.observe = GS.observe, boom
            try:
                got = asyncio.run(mon._observe_gauges(ORIGIN, T0))
            finally:
                GS.observe = original
        self.assertIsNone(got)
        with _Enabled():
            text = mon._format(ring_alert(), 6.0, None, T0, None, got)
        self.assertIn("Pioggia", text)
        self.assertIn("🎚️", text)

    def test_disabled_source_is_never_asked(self):
        mon = monitor()
        calls = []

        async def spy(*a, **kw):
            calls.append(a)
            return None

        GS.configure({"meteohub_enabled": False})
        original, GS.observe = GS.observe, spy
        try:
            asyncio.run(mon._observe_gauges(ORIGIN, T0))
            self.assertEqual(calls, [])
            with _Enabled():
                asyncio.run(mon._observe_gauges(ORIGIN, T0))
            self.assertEqual(len(calls), 1)
        finally:
            GS.observe = original
            GS.configure({"meteohub_enabled": False})

    def test_tracker_untouched_by_the_gauge_path(self):
        """`notified_ring` bounds the message ladder; nothing here may move it."""
        mon = monitor()
        before = mon._tracker.to_dict()
        with _Enabled():
            mon._format(ring_alert(), 6.0, None, T0, None, view(reading()))
            mon._gauge_line(view(reading()))
            mon._gauge_fields(view(reading()))
        self.assertEqual(mon._tracker.to_dict(), before)


class SnapshotFields(unittest.TestCase):

    def test_flattened_for_the_caption(self):
        mon = monitor()
        with _Enabled():
            fields = mon._gauge_fields(view(reading()))
        self.assertTrue(fields["gauge_asked"])
        self.assertEqual(fields["gauge_name"], "Nisida METEO")
        self.assertEqual(fields["gauge_network"], "dpcn-campania")
        self.assertEqual(fields["gauge_wet_count"], 1)

    def test_dry_leads_with_the_nearest_gauge(self):
        mon = monitor()
        with _Enabled():
            fields = mon._gauge_fields(view(reading(mmh=0.0)))
        self.assertEqual(fields["gauge_wet_count"], 0)
        self.assertEqual(fields["gauge_name"], "Nisida METEO")

    def test_off_and_unreachable_are_different_fields(self):
        mon = monitor()
        GS.configure({"meteohub_enabled": False})
        self.assertFalse(mon._gauge_fields(None)["gauge_asked"])
        with _Enabled():
            unreachable = mon._gauge_fields(None)
        self.assertTrue(unreachable["gauge_asked"])
        self.assertIsNone(unreachable.get("gauge_mmh"))


if __name__ == "__main__":
    unittest.main()
