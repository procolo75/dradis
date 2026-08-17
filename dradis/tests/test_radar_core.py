"""
tests/test_radar_core.py
─────────────────────────
Unit tests for the pure raster core. No network, no GeoTIFF on disk: the grids
are built in code against the real projection parameters, so every expected
value can be derived by hand.

    cd dradis && python3 -m unittest discover tests

The cases that matter are the ones where a plausible-looking implementation
returns a confident wrong answer: a front reported from the middle of a rain
area instead of its edge, a velocity invented from an ambiguous correlation, a
blind spot read as a clear sky.
"""

import math
import unittest

import numpy as np

from dradis.live_monitors.geo import distance_km, offset_km
from dradis.live_monitors.radar_core import (
    MOTION_MAX_KMH, MOTION_MIN_PIXELS, NODATA_THRESHOLD,
    GeoTransform, RadarGrid, RadarGridError,
    build_rain_frame, coverage_fraction, cpa, field_motion, latlon_to_pixel,
    parse_geotransform, peak_in_disc, pixel_to_latlon, rain_points,
    velocity_components,
)

T0 = 1_700_000_000.0

# The real grid is 1200×1400; a 400×400 window of it is enough for every test
# here and keeps them fast. Projection parameters are the published ones.
COLS = ROWS = 400
GT = GeoTransform(cols=COLS, rows=ROWS, pixel_m=1000.0,
                  x0=-200000.0, y0=200000.0, lon0=12.5, lat0=42.0)

# Centre of the synthetic grid, and therefore the origin of most tests.
ORIGIN = tuple(float(v) for v in pixel_to_latlon(GT, COLS / 2, ROWS / 2))


def blank(fill: float = 0.0) -> np.ndarray:
    return np.full((ROWS, COLS), fill, dtype=np.float32)


def grid(data: np.ndarray, t: float = T0) -> RadarGrid:
    return RadarGrid(t=t, product="SRI", data=data, gt=GT)


def paint(data: np.ndarray, lat: float, lon: float, value: float) -> None:
    col, row = latlon_to_pixel(GT, lat, lon)
    data[int(row), int(col)] = value


def paint_arc(data: np.ndarray, bearing_deg: float, half_width_deg: float,
              near_km: float, far_km: float, value: float) -> None:
    """Fill an annular sector around ORIGIN, in whole kilometres."""
    for km in range(int(near_km), int(far_km) + 1):
        steps = max(3, int(2 * half_width_deg))
        for i in range(steps + 1):
            b = math.radians(bearing_deg - half_width_deg
                             + 2 * half_width_deg * i / steps)
            paint(data, *offset_km(ORIGIN[0], ORIGIN[1],
                                   km * math.cos(b), km * math.sin(b)), value)


# ── GeoTIFF tags ──────────────────────────────────────────────────────────────

# Exactly what a real DPC product carries, transcribed from a downloaded file.
DPC_TAGS = {
    33550: (1000.0, 1000.0, 0.0),
    33922: (0.0, 0.0, 0.0, -600000.0, 650000.0, 0.0),
    34735: (1, 1, 0, 7, 1024, 0, 1, 1, 1025, 0, 1, 1, 2048, 0, 1, 4326,
            3075, 0, 1, 1, 3076, 0, 1, 9001, 3080, 34736, 1, 0,
            3081, 34736, 1, 1),
    34736: (12.5, 42.0),
}


class GeoTransformTest(unittest.TestCase):

    def test_reads_the_published_grid(self):
        gt = parse_geotransform(DPC_TAGS, 1200, 1400)
        self.assertEqual(gt.cols, 1200)
        self.assertEqual(gt.rows, 1400)
        self.assertEqual(gt.pixel_m, 1000.0)
        self.assertEqual(gt.x0, -600000.0)
        self.assertEqual(gt.y0, 650000.0)
        self.assertEqual(gt.lon0, 12.5)
        self.assertEqual(gt.lat0, 42.0)
        self.assertEqual(gt.k0, 1.0)

    def test_pixel_is_point_shifts_the_tiepoint_by_half_a_pixel(self):
        """RasterPixelIsPoint puts the tiepoint at the pixel CENTRE. Ignoring the
        distinction is 500 m of error that nothing downstream could detect."""
        tags = dict(DPC_TAGS)
        keys = list(DPC_TAGS[34735])
        keys[keys.index(1025) + 3] = 2               # RasterPixelIsPoint
        tags[34735] = tuple(keys)
        gt = parse_geotransform(tags, 1200, 1400)
        self.assertEqual(gt.x0, -600000.0 - 500.0)
        self.assertEqual(gt.y0, 650000.0 + 500.0)

    def test_unknown_projection_is_refused_not_guessed(self):
        tags = dict(DPC_TAGS)
        keys = list(DPC_TAGS[34735])
        keys[keys.index(3075) + 3] = 11              # some other transform
        tags[34735] = tuple(keys)
        with self.assertRaises(RadarGridError):
            parse_geotransform(tags, 1200, 1400)

    def test_missing_tiepoint_is_refused(self):
        with self.assertRaises(RadarGridError):
            parse_geotransform({33550: (1000.0, 1000.0, 0.0)}, 1200, 1400)

    def test_non_metre_units_are_refused(self):
        tags = dict(DPC_TAGS)
        keys = list(DPC_TAGS[34735])
        keys[keys.index(3076) + 3] = 9002            # feet
        tags[34735] = tuple(keys)
        with self.assertRaises(RadarGridError):
            parse_geotransform(tags, 1200, 1400)


class ProjectionTest(unittest.TestCase):

    def test_round_trip_is_exact(self):
        for lat, lon in [(45.4642, 9.19), (41.9028, 12.4964), (38.1157, 13.3615),
                         (39.2238, 9.1217), (46.5, 12.0), (37.0, 15.3)]:
            col, row = latlon_to_pixel(GT, lat, lon)
            back_lat, back_lon = pixel_to_latlon(GT, col, row)
            self.assertAlmostEqual(float(back_lat), lat, places=9)
            self.assertAlmostEqual(float(back_lon), lon, places=9)

    def test_natural_origin_sits_where_the_tiepoint_says(self):
        """x=0,y=0 in projected metres is the natural origin, which the tiepoint
        places at a specific pixel — the anchor everything else hangs from."""
        col, row = latlon_to_pixel(GT, GT.lat0, GT.lon0)
        self.assertAlmostEqual(float(col), -GT.x0 / GT.pixel_m, places=6)
        self.assertAlmostEqual(float(row), GT.y0 / GT.pixel_m, places=6)

    def test_one_pixel_is_one_kilometre(self):
        a = pixel_to_latlon(GT, 200, 200)
        b = pixel_to_latlon(GT, 201, 200)
        self.assertAlmostEqual(distance_km(float(a[0]), float(a[1]),
                                           float(b[0]), float(b[1])), 1.0, places=2)

    def test_vectorised_and_scalar_agree(self):
        cols = np.array([10.0, 50.0, 120.0])
        rows = np.array([20.0, 60.0, 130.0])
        lats, lons = pixel_to_latlon(GT, cols, rows)
        for i in range(3):
            one = pixel_to_latlon(GT, float(cols[i]), float(rows[i]))
            self.assertAlmostEqual(float(lats[i]), float(one[0]), places=12)
            self.assertAlmostEqual(float(lons[i]), float(one[1]), places=12)


class SamplingTest(unittest.TestCase):

    def test_value_is_returned_in_product_units(self):
        data = blank()
        paint(data, ORIGIN[0], ORIGIN[1], 7.5)
        self.assertAlmostEqual(sample_at(grid(data), ORIGIN), 7.5, places=4)

    def test_nodata_is_none_and_not_zero(self):
        """The distinction the whole monitor rests on: -9999 means the radar
        cannot see there, which must never be reported as an absence of rain."""
        data = blank(-9999.0)
        self.assertIsNone(sample_at(grid(data), ORIGIN))

    def test_outside_the_grid_is_none(self):
        self.assertIsNone(sample_at(grid(blank()), (43.0, -20.0)))

    def test_zero_is_a_measurement(self):
        self.assertEqual(sample_at(grid(blank(0.0)), ORIGIN), 0.0)


def sample_at(g: RadarGrid, point):
    from dradis.live_monitors.radar_core import sample
    return sample(g, point[0], point[1])


class RainPointsTest(unittest.TestCase):

    def test_threshold_selects_pixels(self):
        data = blank()
        paint(data, *offset_km(*ORIGIN, 10, 0), 0.4)
        paint(data, *offset_km(*ORIGIN, 11, 0), 3.0)
        self.assertEqual(len(rain_points(grid(data), ORIGIN, 40.0, 1.0)), 1)
        self.assertEqual(len(rain_points(grid(data), ORIGIN, 40.0, 0.2)), 2)

    def test_nodata_never_passes_the_threshold(self):
        """-9999 is below every threshold, but a naive `>= min_value` on a masked
        array or a flipped comparison would let it through as a rain pixel."""
        self.assertEqual(rain_points(grid(blank(-9999.0)), ORIGIN, 40.0, 0.1), [])

    def test_the_square_window_is_trimmed_to_a_disc(self):
        data = blank()
        paint(data, *offset_km(*ORIGIN, 14, 14), 5.0)     # 19.8 km: a corner
        self.assertEqual(rain_points(grid(data), ORIGIN, 15.0, 1.0), [])
        self.assertEqual(len(rain_points(grid(data), ORIGIN, 25.0, 1.0)), 1)

    def test_points_carry_the_measurement_time_not_the_query_time(self):
        data = blank()
        paint(data, *offset_km(*ORIGIN, 5, 0), 5.0)
        points = rain_points(grid(data, t=T0), ORIGIN, 40.0, 1.0)
        self.assertEqual(points[0][0], T0)

    def test_returned_coordinates_land_back_on_the_painted_pixel(self):
        data = blank()
        target = offset_km(ORIGIN[0], ORIGIN[1], 12.0, -7.0)
        paint(data, *target, 5.0)
        points = rain_points(grid(data), ORIGIN, 40.0, 1.0)
        self.assertEqual(len(points), 1)
        # Within half a pixel diagonal of the painted centre.
        self.assertLess(distance_km(points[0][1], points[0][2], *target), 1.0)


class DiscStatsTest(unittest.TestCase):

    def test_peak_is_the_strongest_reading_inside_the_radius(self):
        data = blank()
        paint(data, *offset_km(*ORIGIN, 5, 0), 4.0)
        paint(data, *offset_km(*ORIGIN, 25, 0), 40.0)
        self.assertAlmostEqual(peak_in_disc(grid(data), ORIGIN, 10.0), 4.0, places=3)
        self.assertAlmostEqual(peak_in_disc(grid(data), ORIGIN, 30.0), 40.0, places=3)

    def test_peak_is_none_where_nothing_is_measured(self):
        self.assertIsNone(peak_in_disc(grid(blank(-9999.0)), ORIGIN, 20.0))

    def test_coverage_is_one_when_the_whole_disc_is_seen(self):
        self.assertAlmostEqual(coverage_fraction(grid(blank()), ORIGIN, 20.0),
                               1.0, places=3)

    def test_coverage_is_zero_over_a_blind_spot(self):
        self.assertEqual(coverage_fraction(grid(blank(-9999.0)), ORIGIN, 20.0), 0.0)

    def test_partial_coverage_is_reported_as_a_fraction(self):
        data = blank()
        data[:ROWS // 2, :] = -9999.0            # everything north is invisible
        fraction = coverage_fraction(grid(data), ORIGIN, 20.0)
        self.assertGreater(fraction, 0.4)
        self.assertLess(fraction, 0.6)


class RainFrameTest(unittest.TestCase):
    """The regression that justifies this module existing at all."""

    def test_front_is_the_edge_of_a_filled_sector_not_its_middle(self):
        """A sector filled from 5 km to 45 km has its leading edge at 5 km.

        The storm front's quantile estimator answers ~0.39 of the outer radius on
        a filled sector no matter where the edge is — it was designed for sparse,
        individually mislocated discharges. Measured on a real product it put rain
        that was 3.7 km away at 14.7 km.
        """
        data = blank()
        paint_arc(data, 90.0, 12.0, 5, 45, 6.0)
        frame = build_rain_frame(rain_points(grid(data), ORIGIN, 70.0, 1.0),
                                 ORIGIN, T0, 50.0, 70.0)
        self.assertIsNotNone(frame.dominant)
        self.assertLess(frame.dominant.front_km, 7.0)
        self.assertGreater(frame.dominant.front_km, 4.0)

    def test_isolated_speckle_cannot_pull_the_front_in(self):
        """One stray pixel is not a front. The rank floor is what guarantees it."""
        data = blank()
        paint_arc(data, 0.0, 12.0, 30, 45, 6.0)
        paint(data, *offset_km(*ORIGIN, 3, 0), 9.0)       # a single hot pixel
        frame = build_rain_frame(rain_points(grid(data), ORIGIN, 70.0, 1.0),
                                 ORIGIN, T0, 50.0, 70.0)
        self.assertGreater(frame.dominant.front_km, 20.0)

    def test_a_sector_below_the_area_floor_is_inactive(self):
        data = blank()
        for km in range(20, 23):                          # 3 pixels only
            paint(data, *offset_km(*ORIGIN, km, 0), 5.0)
        frame = build_rain_frame(rain_points(grid(data), ORIGIN, 70.0, 1.0),
                                 ORIGIN, T0, 50.0, 70.0)
        self.assertIsNone(frame.dominant)
        self.assertFalse(frame.has_activity)

    def test_the_nearest_sector_dominates(self):
        data = blank()
        paint_arc(data, 0.0, 12.0, 30, 40, 6.0)
        paint_arc(data, 180.0, 12.0, 10, 20, 6.0)
        frame = build_rain_frame(rain_points(grid(data), ORIGIN, 70.0, 1.0),
                                 ORIGIN, T0, 50.0, 70.0)
        self.assertLess(frame.dominant.front_km, 15.0)
        self.assertAlmostEqual(frame.dominant.bearing_deg, 180.0, delta=20.0)

    def test_activity_needs_the_front_inside_the_alert_radius(self):
        data = blank()
        paint_arc(data, 0.0, 12.0, 40, 48, 6.0)
        frame = build_rain_frame(rain_points(grid(data), ORIGIN, 70.0, 1.0),
                                 ORIGIN, T0, 30.0, 70.0)
        self.assertIsNotNone(frame.dominant)
        self.assertFalse(frame.has_activity)      # seen, but outside the radius

    def test_empty_input_is_an_empty_frame(self):
        frame = build_rain_frame([], ORIGIN, T0, 30.0, 48.0)
        self.assertIsNone(frame.dominant)
        self.assertFalse(frame.has_activity)
        self.assertEqual(frame.strikes_in_radius, 0)


class FieldMotionTest(unittest.TestCase):

    def setUp(self):
        rng = np.random.default_rng(7)
        # A textured field: correlation needs structure, and uniform rain has none.
        field = rng.random((ROWS, COLS)).astype(np.float32) * 8.0
        field[field < 5.0] = 0.0
        self.field = field

    def shifted(self, drow: int, dcol: int) -> np.ndarray:
        return np.roll(np.roll(self.field, drow, axis=0), dcol, axis=1)

    def test_recovers_a_known_displacement(self):
        """1 px = 1 km, and 1 km over 300 s is 12 km/h."""
        for drow, dcol, speed, bearing in [(0, 2, 24.0, 90.0),
                                           (-3, 0, 36.0, 0.0),
                                           (0, -5, 60.0, 270.0),
                                           (4, 0, 48.0, 180.0)]:
            motion = field_motion(grid(self.field, T0),
                                  grid(self.shifted(drow, dcol), T0 + 300.0),
                                  ORIGIN)
            self.assertIsNotNone(motion, f"shift {(drow, dcol)} was rejected")
            self.assertAlmostEqual(motion.speed_kmh, speed, delta=1.0)
            # Meridian convergence puts true north a degree or so off grid north.
            self.assertAlmostEqual(
                ((motion.bearing_deg - bearing + 180) % 360) - 180, 0.0, delta=3.0)

    def test_a_standstill_is_measured_not_refused(self):
        motion = field_motion(grid(self.field, T0),
                              grid(self.field.copy(), T0 + 300.0), ORIGIN)
        self.assertIsNotNone(motion)
        self.assertAlmostEqual(motion.speed_kmh, 0.0, places=3)

    def test_a_sub_pixel_drift_is_a_standstill_not_a_direction(self):
        """The peak is found on the integer grid; anything under half a pixel is
        the parabolic fit's opinion, and it always has one. A quarter-pixel blend
        used to come out as a few km/h WITH A COMPASS BEARING — which is how a
        rain front 19 km to the NW was reported as drifting NW, away from an
        observer it then rained on."""
        blended = 0.75 * self.field + 0.25 * self.shifted(0, 1)
        motion = field_motion(grid(self.field, T0),
                              grid(blended.astype(np.float32), T0 + 300.0), ORIGIN)
        self.assertIsNotNone(motion)
        self.assertEqual(motion.speed_kmh, 0.0)
        self.assertEqual(motion.bearing_deg, 0.0)

    def test_the_floor_is_half_a_pixel_per_frame(self):
        """1 km per pixel over 300 s is 12 km/h, so the floor sits at 6 km/h —
        derived from the raster, not hard-coded, and comfortably below the 24 km/h
        of the smallest displacement the tests above recover."""
        self.assertEqual(MOTION_MIN_PIXELS, 0.5)

    def test_unrelated_fields_yield_no_measurement(self):
        """Two independent fields correlate at noise level. Reporting a velocity
        from that is the failure this gate exists to prevent."""
        rng = np.random.default_rng(99)
        other = rng.random((ROWS, COLS)).astype(np.float32) * 8.0
        other[other < 5.0] = 0.0
        self.assertIsNone(
            field_motion(grid(self.field, T0), grid(other, T0 + 300.0), ORIGIN))

    def test_an_implausible_speed_is_refused(self):
        motion = field_motion(grid(self.field, T0),
                              grid(self.shifted(0, 40), T0 + 300.0), ORIGIN)
        self.assertIsNone(motion)          # 40 km in 5 min is 480 km/h

    def test_speed_ceiling_matches_the_constant(self):
        self.assertGreater(MOTION_MAX_KMH, 100.0)

    def test_wrong_spacing_is_refused(self):
        same = grid(self.shifted(0, 2), T0 + 30.0)
        self.assertIsNone(field_motion(grid(self.field, T0), same, ORIGIN))
        far = grid(self.shifted(0, 2), T0 + 5000.0)
        self.assertIsNone(field_motion(grid(self.field, T0), far, ORIGIN))

    def test_reversed_order_is_refused(self):
        self.assertIsNone(field_motion(grid(self.field, T0 + 300.0),
                                       grid(self.shifted(0, 2), T0), ORIGIN))

    def test_an_empty_sky_yields_no_measurement(self):
        self.assertIsNone(field_motion(grid(blank(), T0),
                                       grid(blank(), T0 + 300.0), ORIGIN))

    def test_mismatched_grids_are_refused(self):
        other = GeoTransform(cols=COLS, rows=ROWS, pixel_m=2000.0, x0=0.0,
                             y0=0.0, lon0=12.5, lat0=42.0)
        b = RadarGrid(T0 + 300.0, "SRI", self.shifted(0, 2), other)
        self.assertIsNone(field_motion(grid(self.field, T0), b, ORIGIN))


class EncounterTest(unittest.TestCase):

    def test_head_on_hits_and_the_time_is_distance_over_speed(self):
        encounter = cpa(20.0, 0.0, 0.0, -40.0)     # rain due north, moving south
        self.assertAlmostEqual(encounter.minutes, 30.0, places=6)
        self.assertAlmostEqual(encounter.miss_km, 0.0, places=9)
        self.assertTrue(encounter.approaching)

    def test_a_crossing_track_misses_by_the_offset(self):
        encounter = cpa(20.0, 0.0, 40.0, 0.0)      # rain due north, moving east
        self.assertAlmostEqual(encounter.miss_km, 20.0, places=6)
        self.assertLessEqual(encounter.minutes, 0.0)

    def test_receding_rain_has_its_closest_approach_in_the_past(self):
        encounter = cpa(20.0, 90.0, 40.0, 0.0)     # rain due east, moving east
        self.assertLess(encounter.minutes, 0.0)
        self.assertFalse(encounter.approaching)

    def test_the_observer_velocity_is_subtracted(self):
        """Driving into stationary rain must read exactly like stationary rain
        driving into you — that is the whole point of a relative frame."""
        own_east, own_north = velocity_components(60.0, 0.0)      # north at 60
        driving = cpa(20.0, 0.0, 0.0, 0.0, own_east, own_north)
        parked = cpa(20.0, 0.0, 0.0, -60.0)
        self.assertAlmostEqual(driving.minutes, parked.minutes, places=6)
        self.assertAlmostEqual(driving.miss_km, parked.miss_km, places=6)

    def test_drifting_together_is_not_an_encounter(self):
        self.assertIsNone(cpa(20.0, 0.0, 0.5, 0.5))

    def test_miss_bearing_names_the_side_it_passes(self):
        encounter = cpa(20.0, 0.0, 40.0, 0.0)      # crosses to the east
        self.assertAlmostEqual(encounter.miss_bearing_deg, 0.0, delta=1.0)

    def test_relative_speed_is_the_closing_rate(self):
        """Driving north at 50 into rain sliding south at 30 closes at 80 —
        and the same pair both heading south closes at only their difference."""
        own_east, own_north = velocity_components(50.0, 0.0)
        head_on = cpa(30.0, 0.0, 0.0, -30.0, own_east, own_north)
        self.assertAlmostEqual(head_on.relative_speed_kmh, 80.0, places=6)

        chasing = cpa(30.0, 180.0, 0.0, -30.0, *velocity_components(50.0, 180.0))
        self.assertAlmostEqual(chasing.relative_speed_kmh, 20.0, places=6)


class VelocityComponentsTest(unittest.TestCase):

    def test_course_is_clockwise_from_north(self):
        for course, east, north in [(0.0, 0.0, 10.0), (90.0, 10.0, 0.0),
                                    (180.0, 0.0, -10.0), (270.0, -10.0, 0.0)]:
            e, n = velocity_components(10.0, course)
            self.assertAlmostEqual(e, east, places=9)
            self.assertAlmostEqual(n, north, places=9)


class NodataConstantTest(unittest.TestCase):

    def test_threshold_sits_between_the_fill_value_and_any_real_reading(self):
        self.assertLess(-9999.0, NODATA_THRESHOLD)
        self.assertLess(NODATA_THRESHOLD, 0.0)


if __name__ == "__main__":
    unittest.main()
