"""
tests/test_stations.py
───────────────────────
The `/stations` readout — `bot/stations.py`.

    cd dradis && python3 -m unittest discover tests

No network and no Telegram: the module imports neither, which is why the wording
can be pinned here.

What is pinned, and why each one bites:
  · Composed by QUANTITY. Most stations are rain gauges and nothing else — 11 of
    11 within 25 km of Bacoli report rain, 2 report wind — so each line takes
    the nearest station that measures ITS quantity. Sorting by distance and
    printing whatever the top few happen to hold produces a wall of "0.0 mm".
  · A missing quantity produces NO LINE, never a blank one.
  · Each line carries its own age. The gust is an hourly MAXIMUM and arrives
    hourly; the rain arrives every ten minutes. One age per station misdates one
    of them on every readout that prints both.
  · "wet" is at least the printed resolution. Below 0.1 mm/h the rate renders as
    "0.0", and "3 gauges wet, peak 0.0 mm/h" contradicts itself.
  · Rain is summarised over the whole set and never stated as "it is not
    raining" — one dry gauge says nothing about a shower three kilometres away.
  · Three states, three sentences: unreachable, answered-with-nothing, answered.
  · NO coordinates, NO URLs, NO numeric ids. Car Mode prints this readout whole
    and strips all three, leaving stumps. The constraint is met at the source.
"""

import re
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# `bot.stations` uses the add-on's own absolute imports, as every module in the
# image does, so its directory has to be on the path — same shim as
# test_campania_alert.py. It pulls in nothing that opens a socket.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dradis"))

from bot.stations import format_stations                              # noqa: E402
from live_monitors.gauges_core import (                               # noqa: E402
    GaugeReading, GaugeView, Measurement,
    VAR_GUST, VAR_HUMIDITY, VAR_PRECIP, VAR_PRESSURE, VAR_RIVER, VAR_SNOW,
    VAR_TEMP, VAR_WIND, VAR_WIND_DIR,
)  # VAR_SNOW is imported to assert it is NOT printed

NOW = datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc).timestamp()
TZ = ZoneInfo("Europe/Rome")


def station(name="Nisida METEO", net="dpcn-campania", km=7.0, bearing=90.0,
            **quantities) -> GaugeReading:
    """`quantities` are var -> value, or var -> (value, age_min, window_min)."""
    m = {}
    for var, spec in quantities.items():
        value, age, window = spec if isinstance(spec, tuple) else (spec, 15, 0)
        m[var] = Measurement(value, NOW - age * 60, window)
    return GaugeReading(name=name, network=net, lat=40.0, lon=14.0,
                        distance_km=km, bearing_deg=bearing, measurements=m)


def view(*readings, radius_km: float = 30.0) -> GaugeView:
    gauges = [r for r in readings if r.mmh is not None]
    return GaugeView(readings=tuple(readings),
                     wet=tuple(r for r in gauges if (r.mmh or 0) >= 0.2),
                     nearest=min(gauges, key=lambda r: r.distance_km)
                     if gauges else None,
                     radius_km=radius_km, fetched_at=NOW)


def render(v, place="Bacoli", lang="it", radius_km=30.0) -> str:
    return format_stations(v, place, lang=lang, tz=TZ,
                           radius_km=radius_km, now=NOW)


class ComposedByQuantity(unittest.TestCase):

    def test_each_line_names_the_station_that_measures_it(self):
        """The far weather station wins the wind; the near gauge wins the rain."""
        out = render(view(
            station("Pozzuoli", km=2.0, **{VAR_PRECIP: 0.0}),
            station("Nisida METEO", km=7.0,
                    **{VAR_TEMP: 300.65, VAR_WIND: 3.6, VAR_PRECIP: 0.0}),
        ))
        temp = next(l for l in out.split("\n") if "°C" in l)
        self.assertIn("Nisida METEO", temp)
        self.assertIn("7 km", temp)
        self.assertIn("2 pluviometri", out)          # the summary counts both

    def test_a_missing_quantity_produces_no_line(self):
        out = render(view(station(**{VAR_TEMP: 295.15})))
        self.assertIn("°C", out)
        for absent in ("hPa", "umidità", "vento", "raffica", "livello"):
            self.assertNotIn(absent, out)

    def test_nearest_wins_when_several_measure_the_same_thing(self):
        out = render(view(
            station("Far", km=20.0, **{VAR_TEMP: 310.15}),
            station("Near", km=3.0, **{VAR_TEMP: 280.15}),
        ))
        line = next(l for l in out.split("\n") if "°C" in l)
        self.assertIn("Near", line)
        self.assertIn("7.0 °C", line)

    def test_units_are_converted_out_of_bufr(self):
        out = render(view(station(**{
            VAR_TEMP: 300.65, VAR_PRESSURE: 100870.0, VAR_WIND: 10.0,
            VAR_GUST: (20.0, 30, 60), VAR_HUMIDITY: 39.0,
            VAR_RIVER: 1.28})))
        self.assertIn("27.5 °C", out)         # K   -> C
        self.assertIn("1009 hPa", out)        # Pa  -> hPa
        self.assertIn("36 km/h", out)         # m/s -> km/h
        self.assertIn("72 km/h", out)         # gust likewise
        self.assertIn("umidità 39%", out)
        self.assertIn("+1.28 m", out)

    def test_wind_carries_its_direction_when_the_station_has_one(self):
        out = render(view(station(**{VAR_WIND: 3.6, VAR_WIND_DIR: 225.0})))
        self.assertIn("da SO", out)
        out = render(view(station(**{VAR_WIND: 3.6})))
        self.assertIn("vento", out)
        self.assertNotIn(" da ", out)


class AgePerQuantity(unittest.TestCase):

    def test_gust_is_named_as_an_hourly_maximum_not_an_age(self):
        """`2,0,3600` is a peak over the past hour, not a reading taken then."""
        out = render(view(station(**{VAR_TEMP: (295.15, 15, 0),
                                     VAR_GUST: (20.0, 30, 60)})))
        temp = next(l for l in out.split("\n") if "°C" in l)
        gust = next(l for l in out.split("\n") if "raffica" in l)
        self.assertIn("15 min fa", temp)
        self.assertIn("nell'ora fino alle", gust)
        self.assertNotIn("min fa", gust)

    def test_two_quantities_from_one_station_can_disagree_on_age(self):
        out = render(view(station(**{VAR_TEMP: (295.15, 10, 0),
                                     VAR_HUMIDITY: (50.0, 55, 0)})))
        self.assertIn("10 min fa", out)
        self.assertIn("55 min fa", out)

    def test_a_long_age_is_hours_not_a_large_number_of_minutes(self):
        out = render(view(station(**{VAR_TEMP: (295.15, 150, 0)})))
        self.assertIn("2.5 h fa", out)


class Rain(unittest.TestCase):

    def test_dry_states_the_count_never_that_it_is_not_raining(self):
        out = render(view(station("A", **{VAR_PRECIP: 0.0}),
                          station("B", km=9.0, **{VAR_PRECIP: 0.0})))
        self.assertIn("nessuna pioggia su 2 pluviometri", out)
        self.assertNotIn("non piove", out.lower())

    def test_wet_counts_and_names_the_strongest(self):
        out = render(view(station("A", **{VAR_PRECIP: 0.4}),
                          station("B", km=4.0, bearing=0.0, **{VAR_PRECIP: 6.1}),
                          station("C", km=9.0, **{VAR_PRECIP: 0.0})))
        self.assertIn("2 pluviometri bagnati su 3", out)
        self.assertIn("6.1 mm/h", out)
        self.assertIn("B, 4 km a N", out)

    def test_a_trace_below_the_printed_resolution_is_not_wet(self):
        """Otherwise: "1 gauge wet, peak 0.0 mm/h" — self-contradicting."""
        out = render(view(station("A", **{VAR_PRECIP: 0.04}),
                          station("B", km=9.0, **{VAR_PRECIP: 0.0})))
        self.assertIn("nessuna pioggia su 2", out)
        self.assertNotIn("0.0 mm/h", out)

    def test_no_gauge_at_all_produces_no_rain_line(self):
        out = render(view(station(**{VAR_TEMP: 295.15})))
        self.assertNotIn("pluviometri", out)


class ThreeStates(unittest.TestCase):

    def test_unreachable(self):
        out = render(None)
        self.assertIn("non raggiungibile", out)
        self.assertIn("Bacoli", out)

    def test_answered_with_nothing_in_range(self):
        out = render(view())
        self.assertIn("Nessuna lettura recente", out)
        self.assertNotIn("non raggiungibile", out)

    def test_answered(self):
        out = render(view(station(**{VAR_TEMP: 295.15})))
        self.assertIn("°C", out)
        self.assertIn("1 stazioni entro 30 km", out)


class CarModeConstraint(unittest.TestCase):
    """Car Mode prints this whole, and strips coordinates, URLs and ids.

    Whatever it strips leaves a stump, so none of the three may be here at all.
    The patterns are `car_mode._COORD_RE`, `_URL_RE` and `_IDENT_RE`.
    """

    def _full(self) -> str:
        return render(view(
            station("Nisida METEO", km=7.0, **{
                VAR_TEMP: 300.65, VAR_HUMIDITY: 36.0, VAR_PRESSURE: 100870.0,
                VAR_WIND: 3.6, VAR_WIND_DIR: 147.0, VAR_GUST: (6.0, 30, 60),
                VAR_PRECIP: 1.4}),
            station("Regi Lagni", net="dpcn-campania", km=27.0,
                    **{VAR_RIVER: 0.51})))

    def test_no_urls(self):
        self.assertIsNone(re.search(r"https?://", self._full()))

    def test_no_coordinate_pairs(self):
        self.assertIsNone(re.search(r"-?\d+\.\d{4,}\s*,\s*-?\d+\.\d{4,}",
                                    self._full()))

    def test_no_long_numeric_ids(self):
        self.assertIsNone(re.search(r"#\d{3,}", self._full()))

    def test_fits_a_single_telegram_message(self):
        self.assertLess(len(self._full()), 4096)


class SnowIsNotPrinted(unittest.TestCase):
    """Measured 13 Sep 2026: 83 of 191 national series above 5 cm, 113 cm in
    central Turin, and no way to tell metres from centimetres. A station that
    happens to report it must not put it on screen."""

    def test_a_station_reporting_snow_produces_no_snow_line(self):
        out = render(view(station(**{VAR_TEMP: 295.15, VAR_SNOW: 1.13})))
        self.assertIn("°C", out)
        self.assertNotIn("neve", out)
        self.assertNotIn("cm", out)


class English(unittest.TestCase):

    def test_every_line_translates(self):
        out = render(view(station(**{
            VAR_TEMP: 295.15, VAR_HUMIDITY: 50.0, VAR_WIND: 3.6,
            VAR_GUST: (6.0, 30, 60), VAR_RIVER: 1.2,
            VAR_PRECIP: 0.0})), lang="en")
        for expected in ("Ground stations", "humidity", "wind", "peak gust",
                         "river level", "no rain on any of",
                         "stations within", "in the hour to"):
            self.assertIn(expected, out)

    def test_the_three_states_translate(self):
        self.assertIn("unreachable", render(None, lang="en"))
        self.assertIn("No recent reading", render(view(), lang="en"))


class Escaping(unittest.TestCase):

    def test_station_and_place_names_are_escaped(self):
        out = render(view(station("A & <b>B</b>", **{VAR_TEMP: 295.15})),
                     place="X <script>")
        self.assertIn("A &amp; &lt;b&gt;B&lt;/b&gt;", out)
        self.assertIn("X &lt;script&gt;", out)


if __name__ == "__main__":
    unittest.main()
