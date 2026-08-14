"""
tests/test_storm_front_chart.py
────────────────────────────────
The radar must produce a real PNG and, above all, must never be able to take an
alert down with it. Skipped when matplotlib is absent (it ships in the add-on
image but not necessarily in a bare dev checkout).
"""

import unittest

from dradis.live_monitors.geo import offset_km
from dradis.live_monitors.storm_front_core import (
    Frame, RingAlert, SectorReading, ring_edges,
)

try:
    import matplotlib  # noqa: F401
    HAVE_MPL = True
except ImportError:
    HAVE_MPL = False

PNG_MAGIC = bytes([0x89, 0x50, 0x4E, 0x47])
ORIGIN = (40.85, 14.27)
NOW = 1_700_000_000.0


def sample_alert(ring=2, front=18.0, bearing=315.0):
    return RingAlert(
        ring=ring, ring_count=4, ring_edge_km=19.5, front_km=front,
        bearing_deg=bearing, sector=10, strikes=22, strikes_in_radius=40,
        secondary=(SectorReading(sector=4, count=9, front_km=26.0, bearing_deg=120.0),),
        track="closing",
    )


def sample_strikes(n=120):
    out = []
    for i in range(n):
        north = 14.0 + (i % 11) * 0.9
        east = -14.0 - (i % 7) * 0.8
        out.append((NOW - (i % 10) * 60.0, *offset_km(*ORIGIN, north, east)))
    return out


@unittest.skipUnless(HAVE_MPL, "matplotlib not installed")
class RadarTest(unittest.TestCase):
    def setUp(self):
        from dradis.live_monitors.storm_front_chart import render_radar
        self.render = render_radar
        self.edges = ring_edges(30.0, 4)

    def _render(self, strikes, alert, lang="it"):
        return self.render(strikes, ORIGIN, NOW, alert, radius_km=30.0,
                           observe_radius_km=48.0, edges=self.edges,
                           window_sec=600.0, location="Bacoli", lang=lang)

    def test_renders_a_png(self):
        png = self._render(sample_strikes(), sample_alert())
        self.assertEqual(png[:4], PNG_MAGIC)
        self.assertGreater(len(png), 5000)

    def test_every_ring_and_language_renders(self):
        for ring in (1, 2, 3, 4):
            for lang in ("it", "en"):
                png = self._render(sample_strikes(), sample_alert(ring=ring), lang)
                self.assertEqual(png[:4], PNG_MAGIC)

    def test_degenerate_inputs_do_not_raise(self):
        self.assertEqual(self._render([], None)[:4], PNG_MAGIC)
        one = [(NOW, *offset_km(*ORIGIN, 5.0, 5.0))]
        self.assertEqual(self._render(one, None)[:4], PNG_MAGIC)
        self.assertEqual(self._render(one, sample_alert(ring=4, front=4.0))[:4],
                         PNG_MAGIC)

    def test_strikes_outside_the_window_are_dropped(self):
        from dradis.live_monitors.storm_front_chart import _polar
        strikes = [(NOW - 30, *offset_km(*ORIGIN, 10, 0)),
                   (NOW - 5000, *offset_km(*ORIGIN, 10, 0)),
                   (NOW + 500, *offset_km(*ORIGIN, 10, 0))]
        self.assertEqual(len(_polar(strikes, ORIGIN, NOW, 600.0)), 1)

    def test_all_ring_counts_render(self):
        for count in (2, 3, 4):
            png = self.render(sample_strikes(), ORIGIN, NOW, sample_alert(ring=1),
                              radius_km=30.0, observe_radius_km=48.0,
                              edges=ring_edges(30.0, count), window_sec=600.0,
                              location="Bacoli", lang="it")
            self.assertEqual(png[:4], PNG_MAGIC)


if __name__ == "__main__":
    unittest.main()
