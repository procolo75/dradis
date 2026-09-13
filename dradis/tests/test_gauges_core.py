"""
tests/test_gauges_core.py
──────────────────────────
The MeteoHub reading: query construction and the BUFR shapes that come back.

    cd dradis && python3 -m unittest discover tests

No network. `gauges_core` deliberately imports no HTTP client, so every wrinkle
of the format is pinned here without the add-on image.

What is pinned, and why each one bites:
  · The ` or ` product join. A comma is accepted, returns 200, and silently
    keeps ONE product — a confident partial answer, which is worse than an
    error, and invisible until the day a station stops reporting rain.
  · The licence group and a UTC reftime. Both mandatory; without either the
    service answers 400 with a bare JSON STRING, not an object.
  · BOTH accumulation periods. `1,0,60` (one-minute buckets) and `1,0,3600`
    (hourly total) arrive in the SAME response from different networks — the
    fixture has both. Reading a one-minute 0.2 mm tip as an hourly total is
    12 mm/h out of a damp pavement.
  · Only `pind == 1` is an accumulation. A gust carries `2,0,3600` and an
    instantaneous reading `254,0,0`; either one read as a rainfall window is a
    rate invented out of the wrong quantity.
  · Units. The service speaks BUFR: kelvin, pascals, metres per second.
  · The square box versus the round disc. Stations in the corners are outside
    the radius the caller asked about.
  · `nearest` means the nearest RAIN GAUGE. A thermometer is closer and
    answers a different question, and `nearest` is what gets named in the line
    saying nothing is wet.
  · An amateur network is excluded by default. `mnw` is the largest single
    network in the country and its siting is not guaranteed.
"""

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from dradis.live_monitors.gauges_core import (
    MIN_RATE_WINDOW_SEC, VAR_GUST, VAR_PRECIP, VAR_TEMP,
    build_params, parse,
)

_FIXTURE = (Path(__file__).resolve().parent / "fixtures"
            / "meteohub_observations_napoli.json")

# Nisida METEO, the dpcn-campania station the fixture was captured around.
NISIDA = (40.79361, 14.16389)


def _load() -> tuple[dict, float]:
    """The fixture, and the instant it was captured.

    `now` is derived from the newest sample in the payload rather than pinned to
    a constant, because the parser drops readings older than an hour: a literal
    timestamp would make every one of these tests start failing an hour after
    the capture and pass again never.
    """
    payload = json.loads(_FIXTURE.read_text())
    newest = max(entry["ref"]
                 for station in payload["data"]
                 for product in station["prod"]
                 for entry in product["val"])
    stamp = datetime.fromisoformat(newest).replace(tzinfo=timezone.utc).timestamp()
    return payload, stamp


def _station(net: str, lat: float, lon: float, prods: list[dict],
             name: str = "Test") -> dict:
    return {"stat": {"lat": lat, "lon": lon, "net": net,
                     "details": [{"var": "B01019", "val": name}]},
            "prod": prods}


def _series(var: str, trange: str, values: list[tuple[str, float]],
            lev: str = "1,0,0,0") -> dict:
    return {"var": var, "lev": lev, "trange": trange,
            "val": [{"val": v, "ref": r, "rel": 1} for r, v in values]}


def _minutes(base: str, count: int, step: int = 1) -> list[str]:
    """`count` timestamps `step` minutes apart, in the service's own format."""
    start = datetime.fromisoformat(base).replace(tzinfo=timezone.utc).timestamp()
    return [datetime.fromtimestamp(start + i * step * 60, timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%S") for i in range(count)]


class QueryShape(unittest.TestCase):

    def test_products_joined_with_or_not_comma(self):
        q = build_params(*NISIDA, 25.0, 1_700_000_000.0)["q"]
        self.assertIn(f"product:{VAR_PRECIP} or {VAR_TEMP} or {VAR_GUST}", q)
        self.assertNotIn(f"{VAR_PRECIP},{VAR_TEMP}", q)

    def test_licence_group_and_utc_reftime_present(self):
        # 2023-11-14 22:13:20 UTC — the same instant is 23:13 in Rome, and the
        # service reads reftime as UTC whatever the station's clock says.
        q = build_params(*NISIDA, 25.0, 1_700_000_000.0)["q"]
        self.assertIn("license:CCBY_COMPLIANT", q)
        self.assertIn("reftime:>=2023-11-14 20:43,<=2023-11-14 22:13", q)

    def test_box_contains_the_disc(self):
        params = build_params(*NISIDA, 25.0, 1_700_000_000.0)
        self.assertLess(params["latmin"], NISIDA[0])
        self.assertGreater(params["latmax"], NISIDA[0])
        self.assertLess(params["lonmin"], NISIDA[1])
        self.assertGreater(params["lonmax"], NISIDA[1])
        # ~25 km at this latitude, give or take the flat-earth approximation.
        self.assertAlmostEqual(params["latmax"] - NISIDA[0], 25 / 111.32, places=3)


class FixtureShapes(unittest.TestCase):
    """Against the real capture: both accumulation periods, two networks."""

    def setUp(self):
        self.payload, self.now = _load()

    def test_official_filter_excludes_meteonetwork(self):
        official = parse(self.payload, *NISIDA, 25.0, min_mmh=0.2,
                         official_only=True, now=self.now)
        everyone = parse(self.payload, *NISIDA, 25.0, min_mmh=0.2,
                         official_only=False, now=self.now)
        self.assertTrue(official.readings)
        self.assertLess(len(official.readings), len(everyone.readings))
        self.assertTrue(all(r.network.startswith("dpcn-") for r in official.readings))
        self.assertIn("mnw", {r.network for r in everyone.readings})

    def test_both_accumulation_periods_yield_a_rate(self):
        view = parse(self.payload, *NISIDA, 25.0, min_mmh=0.2,
                     official_only=False, now=self.now)
        windows = {r.network: r.window_min for r in view.readings
                   if r.mmh is not None}
        # One-minute buckets are summed up to the minimum rate window; an
        # hourly total is already a rate and keeps its own hour.
        self.assertEqual(windows["dpcn-campania"], MIN_RATE_WINDOW_SEC / 60)
        self.assertEqual(windows["mnw"], 60)

    def test_units_converted_out_of_bufr(self):
        view = parse(self.payload, *NISIDA, 25.0, min_mmh=0.2,
                     official_only=True, now=self.now)
        nisida = next(r for r in view.readings if r.name == "Nisida METEO")
        # 298.75 K and 7.1 m/s in the capture — a plausible September morning
        # in Naples only after conversion.
        self.assertTrue(15 < nisida.temp_c < 40)
        self.assertTrue(10 < nisida.gust_kmh < 60)

    def test_dry_stations_are_read_not_missing(self):
        """Nothing was falling when this was captured, and that is a reading."""
        view = parse(self.payload, *NISIDA, 25.0, min_mmh=0.2,
                     official_only=True, now=self.now)
        self.assertTrue(view.readings)
        self.assertEqual(view.wet, ())
        self.assertIsNotNone(view.nearest)
        self.assertEqual(view.nearest.mmh, 0.0)

    def test_stale_payload_yields_nothing(self):
        """An hour later the same bytes are no longer a present-tense fact."""
        view = parse(self.payload, *NISIDA, 25.0, min_mmh=0.2,
                     official_only=True, now=self.now + 7200)
        self.assertEqual(view.readings, ())


class RateArithmetic(unittest.TestCase):
    """Built in code so every expected number can be derived by hand."""

    NOW = datetime(2026, 9, 13, 8, 0, tzinfo=timezone.utc).timestamp()

    def _parse(self, prods, **kw):
        payload = {"data": [_station("dpcn-campania", *NISIDA, prods)]}
        kw.setdefault("min_mmh", 0.2)
        return parse(payload, *NISIDA, 25.0, now=self.NOW, **kw)

    def test_one_minute_buckets_summed_over_the_window(self):
        # 15 buckets of 0.2 mm = 3.0 mm in 15 minutes = 12 mm/h.
        stamps = _minutes("2026-09-13T07:45:00", 15)
        view = self._parse([_series(VAR_PRECIP, "1,0,60",
                                    [(s, 0.2) for s in stamps])])
        self.assertAlmostEqual(view.readings[0].mmh, 12.0, places=6)
        self.assertEqual(view.readings[0].window_min, 15)

    def test_single_tip_is_not_a_downpour(self):
        """The bug this window exists to prevent.

        One 0.2 mm tip read as a one-minute rate is 12 mm/h — 'moderate rain'
        out of a single tick of the bucket. Over the real window it is 0.8.
        """
        stamps = _minutes("2026-09-13T07:45:00", 15)
        values = [(s, 0.0) for s in stamps[:-1]] + [(stamps[-1], 0.2)]
        view = self._parse([_series(VAR_PRECIP, "1,0,60", values)])
        self.assertAlmostEqual(view.readings[0].mmh, 0.8, places=6)

    def test_hourly_total_is_already_a_rate(self):
        view = self._parse([_series(VAR_PRECIP, "1,0,3600",
                                    [("2026-09-13T07:00:00", 1.4),
                                     ("2026-09-13T08:00:00", 4.6)])])
        self.assertAlmostEqual(view.readings[0].mmh, 4.6, places=6)
        self.assertEqual(view.readings[0].window_min, 60)

    def test_gust_timerange_is_not_an_accumulation_window(self):
        """`2,0,3600` is a maximum over an hour, not an hour of rainfall."""
        view = self._parse([_series(VAR_PRECIP, "2,0,3600",
                                    [("2026-09-13T08:00:00", 9.0)])])
        self.assertEqual(view.readings, ())

    def test_instantaneous_timerange_is_not_an_accumulation_window(self):
        view = self._parse([_series(VAR_PRECIP, "254,0,0",
                                    [("2026-09-13T08:00:00", 9.0)])])
        self.assertEqual(view.readings, ())

    def test_unreliable_samples_dropped(self):
        stamps = _minutes("2026-09-13T07:45:00", 15)
        series = _series(VAR_PRECIP, "1,0,60", [(s, 0.2) for s in stamps])
        for entry in series["val"]:
            entry["rel"] = 0
        self.assertEqual(self._parse([series]).readings, ())


class Geometry(unittest.TestCase):

    NOW = datetime(2026, 9, 13, 8, 0, tzinfo=timezone.utc).timestamp()

    def _view(self, stations, **kw):
        kw.setdefault("min_mmh", 0.2)
        kw.setdefault("official_only", False)
        return parse({"data": stations}, *NISIDA, 25.0, now=self.NOW, **kw)

    def _rain(self, mmh: float) -> dict:
        # An hourly total states the rate directly, which keeps these cases
        # about geometry rather than about arithmetic.
        return _series(VAR_PRECIP, "1,0,3600", [("2026-09-13T08:00:00", mmh)])

    def test_box_corner_is_outside_the_disc(self):
        """The query is a square; the question was a circle.

        A station 24 km north AND 24 km east is inside the box the service was
        given and 34 km from the observer — beyond the radius the caller asked
        about, and reported as within it by any code that trusts the box.
        """
        corner = (NISIDA[0] + 24 / 111.32,
                  NISIDA[1] + 24 / (111.32 * 0.757))
        view = self._view([_station("dpcn-campania", *corner, [self._rain(5.0)],
                                    name="Corner")])
        self.assertEqual(view.readings, ())

    def test_wettest_first_then_nearest(self):
        far_wet = (NISIDA[0] + 0.15, NISIDA[1])       # ~17 km N
        near_dry = (NISIDA[0] + 0.02, NISIDA[1])      # ~2 km N
        view = self._view([
            _station("dpcn-campania", *near_dry, [self._rain(0.0)], name="Near"),
            _station("dpcn-campania", *far_wet, [self._rain(6.0)], name="Far"),
        ])
        self.assertEqual([r.name for r in view.readings], ["Far", "Near"])
        self.assertEqual([r.name for r in view.wet], ["Far"])

    def test_nearest_is_the_nearest_rain_gauge(self):
        """A thermometer two kilometres away is closer and is not an answer."""
        thermo = (NISIDA[0] + 0.02, NISIDA[1])        # ~2 km
        gauge = (NISIDA[0] + 0.09, NISIDA[1])         # ~10 km
        view = self._view([
            _station("mnw", *thermo,
                     [_series(VAR_TEMP, "254,0,0",
                              [("2026-09-13T08:00:00", 295.0)],
                              lev="103,2000,0,0")], name="Thermo"),
            _station("dpcn-campania", *gauge, [self._rain(0.0)], name="Gauge"),
        ])
        self.assertEqual({r.name for r in view.readings}, {"Thermo", "Gauge"})
        self.assertEqual(view.nearest.name, "Gauge")

    def test_station_with_nothing_usable_is_dropped(self):
        view = self._view([_station("dpcn-campania", *NISIDA, [])])
        self.assertEqual(view.readings, ())


if __name__ == "__main__":
    unittest.main()
