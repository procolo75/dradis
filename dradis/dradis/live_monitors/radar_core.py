"""
live_monitors/radar_core.py
────────────────────────────
Pure raster maths for the Italian weather radar composite. No I/O, no asyncio,
no HTTP: everything here is deterministic given (grid, origin, clock), which
makes it unit-testable against a fixture file.

What the source actually is
───────────────────────────
The Dipartimento della Protezione Civile publishes an Open Access composite of
the 24 national radars every 5 minutes as a Cloud-Optimized GeoTIFF. Verified
against a live product:

  · 1200 × 1400 px, 1000 m/px, one file covering the whole country
  · float32 values ALREADY in physical units — SRI is mm/h, no scaling table
  · nodata is -9999.0, and it means "outside radar coverage", not "no rain".
    Zero means no rain. Confusing the two would turn the edge of the network
    into a permanent dry spell, so `NODATA_THRESHOLD` is load-bearing.
  · LZW compression, which Pillow decodes natively — hence numpy + Pillow are
    the only requirements and both already ship with matplotlib.

Why the geotransform is parsed and not hardcoded
────────────────────────────────────────────────
The bounding box is constant across samples, so the transform could in principle
be a module constant. It is read from the file's own GeoTIFF tags instead: if DPC
ever re-grids the product, a parsed transform keeps working and a hardcoded one
would silently place every measurement a few kilometres from where it belongs.
Silent geographic error is the worst failure this module could have — every
distance downstream would still look perfectly reasonable.

The projection is a Transverse Mercator on a SPHERE (GeoTIFF keys: CT_Transverse-
Mercator, natural origin 12.5°E / 42.0°N, metres, WGS84 datum). Sphere rather
than ellipsoid is not an approximation chosen here — it is what the product
declares — and it conveniently shares `geo.EARTH_RADIUS_KM` with the rest of the
package, so distances computed by the two modules agree.

Why the rain field is handed to the storm front's `build_frame`
───────────────────────────────────────────────────────────────
`build_frame` bins an iterable of (t, lat, lon) into sectors and derives the
leading edge of each. Every raster pixel above threshold is exactly such a
triple, so the whole tested geometry of the storm front monitor — sector binning,
front-of-sector quantile, dominant sector, ring ladder, hysteresis, event
lifecycle, bounded-message invariant — applies to rain with no new decision code.
`rain_points` exists to perform that adaptation and nothing else.

The one thing lightning could not give
──────────────────────────────────────
A field has a measurable velocity; a scatter of discharges does not. `field_motion`
recovers it by phase correlation between two consecutive rasters, which turns the
"will it hit me" question from an inference over a bearing time series (CBDR) into
a direct calculation (`cpa`). See the note on `field_motion` for why that matters
at this cadence.
"""

import math
from dataclasses import dataclass

import numpy as np

from .geo import EARTH_RADIUS_KM, azimuth_deg, distance_km

# ── Raster ────────────────────────────────────────────────────────────────────

R_M = EARTH_RADIUS_KM * 1000.0

# Anything at or below this is "no measurement here". The published fill value is
# exactly -9999.0; the margin absorbs any future change of fill without ever
# swallowing a real intensity, which cannot be negative.
NODATA_THRESHOLD = -9000.0

# GeoTIFF tag and GeoKey identifiers, named so the parser reads as the spec.
_TAG_PIXEL_SCALE   = 33550
_TAG_TIEPOINT      = 33922
_TAG_GEO_KEYS      = 34735
_TAG_GEO_DOUBLES   = 34736

_KEY_RASTER_TYPE   = 1025
_KEY_COORD_TRANS   = 3075
_KEY_LINEAR_UNITS  = 3076
_KEY_NAT_ORIGIN_LON = 3080
_KEY_NAT_ORIGIN_LAT = 3081
_KEY_FALSE_EASTING  = 3082
_KEY_FALSE_NORTHING = 3083
_KEY_SCALE_AT_ORIGIN = 3092

_CT_TRANSVERSE_MERCATOR = 1
_LINEAR_UNIT_METRE      = 9001


class RadarGridError(ValueError):
    """The file is not the grid this module knows how to read."""


@dataclass(frozen=True)
class GeoTransform:
    """Everything needed to turn a pixel into a coordinate, and back."""
    cols: int
    rows: int
    pixel_m: float
    x0: float          # projected easting of the OUTER EDGE of column 0
    y0: float          # projected northing of the OUTER EDGE of row 0
    lon0: float        # natural origin longitude, degrees
    lat0: float        # natural origin latitude, degrees
    k0: float = 1.0
    false_easting: float = 0.0
    false_northing: float = 0.0


def parse_geotransform(tags, cols: int, rows: int) -> GeoTransform:
    """Build a GeoTransform from a Pillow `tag_v2` mapping.

    Raises rather than guessing: a wrong transform produces plausible-looking
    coordinates that are simply in the wrong place, and nothing downstream could
    ever detect it.
    """
    scale = tags.get(_TAG_PIXEL_SCALE)
    tie = tags.get(_TAG_TIEPOINT)
    if not scale or not tie or len(tie) < 6:
        raise RadarGridError("missing ModelPixelScale/ModelTiepoint tags")

    keys = tags.get(_TAG_GEO_KEYS) or ()
    doubles = tags.get(_TAG_GEO_DOUBLES) or ()
    geo_keys: dict[int, float] = {}
    if len(keys) >= 4:
        for i in range(int(keys[3])):
            base = 4 + 4 * i
            if base + 3 >= len(keys):
                break
            key_id, location, _count, offset = keys[base:base + 4]
            if location == 0:
                geo_keys[key_id] = float(offset)
            elif location == _TAG_GEO_DOUBLES and offset < len(doubles):
                geo_keys[key_id] = float(doubles[offset])

    transform = int(geo_keys.get(_KEY_COORD_TRANS, _CT_TRANSVERSE_MERCATOR))
    if transform != _CT_TRANSVERSE_MERCATOR:
        raise RadarGridError(f"unsupported projection (ProjCoordTrans={transform})")
    units = int(geo_keys.get(_KEY_LINEAR_UNITS, _LINEAR_UNIT_METRE))
    if units != _LINEAR_UNIT_METRE:
        raise RadarGridError(f"unsupported linear unit ({units})")
    if _KEY_NAT_ORIGIN_LON not in geo_keys or _KEY_NAT_ORIGIN_LAT not in geo_keys:
        raise RadarGridError("projection origin missing from GeoKeyDirectory")

    # RasterPixelIsArea (1) puts the tiepoint on the pixel CORNER, which is what
    # the rest of this module assumes. RasterPixelIsPoint (2) would offset every
    # coordinate by half a pixel — 500 m of quiet error.
    raster_type = int(geo_keys.get(_KEY_RASTER_TYPE, 1))
    tie_x, tie_y = float(tie[3]), float(tie[4])
    if raster_type == 2:
        tie_x -= float(scale[0]) / 2.0
        tie_y += float(scale[1]) / 2.0

    return GeoTransform(
        cols=cols, rows=rows,
        pixel_m=float(scale[0]),
        x0=tie_x - float(tie[0]) * float(scale[0]),
        y0=tie_y + float(tie[1]) * float(scale[1]),
        lon0=geo_keys[_KEY_NAT_ORIGIN_LON],
        lat0=geo_keys[_KEY_NAT_ORIGIN_LAT],
        k0=geo_keys.get(_KEY_SCALE_AT_ORIGIN, 1.0),
        false_easting=geo_keys.get(_KEY_FALSE_EASTING, 0.0),
        false_northing=geo_keys.get(_KEY_FALSE_NORTHING, 0.0),
    )


@dataclass(frozen=True)
class RadarGrid:
    """One product at one instant. `data` is (rows, cols) float32 in the product's
    own units — mm/h for SRI — with nodata still in place."""
    t: float
    product: str
    data: np.ndarray
    gt: GeoTransform


# ── Projection ────────────────────────────────────────────────────────────────
#
# Spherical Transverse Mercator, Snyder §8. Written with numpy so the same two
# functions serve a single point and the tens of thousands of pixels that
# `rain_points` converts in one shot.

def latlon_to_pixel(gt: GeoTransform, lat, lon):
    """(col, row) in fractional pixels; integers land on pixel corners."""
    phi = np.radians(lat)
    dlon = np.radians(np.asarray(lon, dtype=float) - gt.lon0)
    b = np.clip(np.cos(phi) * np.sin(dlon), -0.999999999, 0.999999999)
    x = R_M * gt.k0 * np.arctanh(b) + gt.false_easting
    y = R_M * gt.k0 * (np.arctan2(np.tan(phi), np.cos(dlon))
                       - math.radians(gt.lat0)) + gt.false_northing
    return (x - gt.x0) / gt.pixel_m, (gt.y0 - y) / gt.pixel_m


def pixel_to_latlon(gt: GeoTransform, col, row):
    """Inverse of `latlon_to_pixel`."""
    x = gt.x0 + np.asarray(col, dtype=float) * gt.pixel_m - gt.false_easting
    y = gt.y0 - np.asarray(row, dtype=float) * gt.pixel_m - gt.false_northing
    d = y / (R_M * gt.k0) + math.radians(gt.lat0)
    xp = x / (R_M * gt.k0)
    lat = np.arcsin(np.sin(d) / np.cosh(xp))
    lon = math.radians(gt.lon0) + np.arctan2(np.sinh(xp), np.cos(d))
    return np.degrees(lat), np.degrees(lon)


# ── Sampling ──────────────────────────────────────────────────────────────────

def sample(grid: RadarGrid, lat: float, lon: float) -> float | None:
    """Value at a point, or None outside the grid or outside radar coverage.

    None is deliberately not 0.0. "I cannot see there" and "it is not raining
    there" are different answers, and only one of them justifies an all-clear.
    """
    col, row = latlon_to_pixel(grid.gt, lat, lon)
    c, r = int(math.floor(float(col))), int(math.floor(float(row)))
    if not (0 <= c < grid.gt.cols and 0 <= r < grid.gt.rows):
        return None
    value = float(grid.data[r, c])
    return None if value <= NODATA_THRESHOLD else value


def _window_bounds(gt: GeoTransform, lat: float, lon: float,
                   half_km: float) -> tuple[int, int, int, int]:
    """Pixel slice covering a square of side 2*half_km centred on a point,
    clipped to the grid. Returns (c0, c1, r0, r1) as half-open bounds."""
    col, row = latlon_to_pixel(gt, lat, lon)
    half_px = half_km * 1000.0 / gt.pixel_m
    c0 = max(0, int(math.floor(float(col) - half_px)))
    c1 = min(gt.cols, int(math.ceil(float(col) + half_px)) + 1)
    r0 = max(0, int(math.floor(float(row) - half_px)))
    r1 = min(gt.rows, int(math.ceil(float(row) + half_px)) + 1)
    return c0, c1, r0, r1


def rain_points(grid: RadarGrid, origin: tuple[float, float],
                radius_km: float, min_value: float,
                t: float | None = None) -> list[tuple[float, float, float]]:
    """Pixels above threshold within `radius_km`, as (t, lat, lon) triples.

    This is the whole adapter between a raster and the storm front's decision
    core: the output is precisely the shape `build_frame` consumes, so rain gets
    the sector binning and the ring ladder for free.

    Every point carries the same timestamp — the instant the radar measured, not
    the instant we are asking. Staleness is therefore a property of the GRID and
    must be judged by the caller before it ever gets here; a stale raster has to
    read as blindness, never as an empty sky.
    """
    gt = grid.gt
    olat, olon = origin
    c0, c1, r0, r1 = _window_bounds(gt, olat, olon, radius_km)
    if c0 >= c1 or r0 >= r1:
        return []

    window = grid.data[r0:r1, c0:c1]
    hits = np.argwhere((window >= min_value) & (window > NODATA_THRESHOLD))
    if hits.size == 0:
        return []

    # +0.5 moves from the pixel corner the transform speaks in to the pixel
    # centre, which is where the measurement actually applies.
    cols = hits[:, 1] + c0 + 0.5
    rows = hits[:, 0] + r0 + 0.5
    lats, lons = pixel_to_latlon(gt, cols, rows)

    # The window is a square; the model is a disc. Trim the corners here rather
    # than letting build_frame see points beyond the observation radius.
    keep = _haversine_km(olat, olon, lats, lons) <= radius_km
    stamp = grid.t if t is None else t
    return [(stamp, float(la), float(lo))
            for la, lo in zip(lats[keep], lons[keep])]


def _haversine_km(lat1: float, lon1: float, lat2, lon2):
    """Vectorised twin of `geo.distance_km`, same formula and same radius."""
    p1 = math.radians(lat1)
    p2 = np.radians(lat2)
    dp = p2 - p1
    dl = np.radians(np.asarray(lon2, dtype=float) - lon1)
    a = np.sin(dp / 2) ** 2 + math.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * np.arcsin(np.minimum(1.0, np.sqrt(a)))


def peak_in_disc(grid: RadarGrid, origin: tuple[float, float],
                 radius_km: float) -> float | None:
    """Strongest reading within the disc, or None where nothing is measured.

    The front tells you WHEN it arrives; this tells you WHAT arrives. Drizzle and
    a downpour reach you the same way and matter very differently.
    """
    c0, c1, r0, r1 = _window_bounds(grid.gt, *origin, radius_km)
    if c0 >= c1 or r0 >= r1:
        return None
    window = grid.data[r0:r1, c0:c1]
    rows, cols = np.mgrid[r0:r1, c0:c1]
    lats, lons = pixel_to_latlon(grid.gt, cols + 0.5, rows + 0.5)
    usable = (window > NODATA_THRESHOLD) & (
        _haversine_km(origin[0], origin[1], lats, lons) <= radius_km)
    if not usable.any():
        return None
    return float(window[usable].max())


def coverage_fraction(grid: RadarGrid, origin: tuple[float, float],
                      radius_km: float) -> float:
    """Share of the disc the radar network can actually see, 0..1.

    A monitor sitting near the edge of coverage is not watching what it thinks it
    is watching, and the honest response is to refuse to run rather than to report
    permanent calm over a blind spot.
    """
    c0, c1, r0, r1 = _window_bounds(grid.gt, *origin, radius_km)
    if c0 >= c1 or r0 >= r1:
        return 0.0
    window = grid.data[r0:r1, c0:c1]
    rows, cols = np.mgrid[r0:r1, c0:c1]
    lats, lons = pixel_to_latlon(grid.gt, cols + 0.5, rows + 0.5)
    inside = _haversine_km(origin[0], origin[1], lats, lons) <= radius_km
    total = int(inside.sum())
    if total == 0:
        return 0.0
    return float(((window > NODATA_THRESHOLD) & inside).sum()) / total


# ── Frame ─────────────────────────────────────────────────────────────────────
#
# `storm_front_core.build_frame` cannot be reused here, and the reason is worth
# recording because everything else in that module CAN be.
#
# Its front estimator is the 15th percentile of the distances in a sector, which
# is the right answer for lightning: discharges are sparse, individually
# mislocated by kilometres, and a low quantile is the robust way to find their
# leading edge. A radar sector is not sparse — it is a filled area — and in a
# filled sector the distance distribution has density proportional to r, so the
# 15th percentile sits at roughly 0.39 of the outer radius NO MATTER WHERE THE
# EDGE ACTUALLY IS.
#
# Measured on a real product: over Arezzo the nearest rain was 3.7 km away and
# the quantile reported 14.7 km. Eleven kilometres of error, in the direction that
# matters, with the rain effectively overhead — it would have placed the front
# outside the innermost ring while the user was getting wet.
#
# For a field the robust leading edge is simply the k-th nearest wet pixel: immune
# to isolated speckle, and measured at 0.7-1.3 km from the true edge at every site
# tested. This builder therefore replaces the estimator and nothing else. It emits
# the very same `Frame` and `SectorReading` objects, so `StormFrontTracker` — the
# rings, the hysteresis, the confirmation dwell, the event lifecycle and the
# bounded-message invariant, which is the part six generations of field failure
# paid for — consumes it without knowing the difference.
#
# One naming caveat, deliberate: `Frame.strikes_in_radius` and
# `SectorReading.count` carry WET PIXEL COUNTS here. At 1 km resolution that is an
# area in km², which is what the message reports. Forking the dataclasses to
# rename two fields would have cost more than this paragraph.

# The k-th nearest wet pixel is the front. Above 1 the estimator ignores isolated
# speckle; kept small so it stays the EDGE and not the body.
FRONT_RANK = 5

# A sector holding less rain than this is not weather, it is noise or the ragged
# margin of a cell. 8 km² at 1 km resolution.
MIN_PIXELS_SECTOR = 8


def build_rain_frame(points, origin: tuple[float, float], now: float,
                     radius_km: float, observe_radius_km: float,
                     min_pixels: int = MIN_PIXELS_SECTOR,
                     front_rank: int = FRONT_RANK) -> "Frame":
    """Bin wet pixels into sectors and derive each sector's leading edge.

    `points` is what `rain_points` returns. O(n), one distance and one azimuth
    per pixel, exactly like the storm front's own builder.
    """
    from .storm_front_core import (
        FRONT_BEARING_BAND_KM, TRACK_BAND_KM, Frame, SectorReading,
        mean_bearing, sector_of,
    )

    olat, olon = origin
    buckets: dict[int, list[tuple[float, float]]] = {}
    in_radius = 0
    observed = 0

    for _t, lat, lon in points:
        d = distance_km(olat, olon, lat, lon)
        if d > observe_radius_km:
            continue
        observed += 1
        if d <= radius_km:
            in_radius += 1
        buckets.setdefault(sector_of(azimuth_deg(olat, olon, lat, lon)), []).append(
            (d, azimuth_deg(olat, olon, lat, lon)))

    readings: list[SectorReading] = []
    for sector, entries in buckets.items():
        if len(entries) < min_pixels:
            continue
        entries.sort(key=lambda p: p[0])
        front = entries[min(front_rank, len(entries)) - 1][0]
        leading = [az for d, az in entries if d <= front + FRONT_BEARING_BAND_KM]
        readings.append(SectorReading(
            sector=sector, count=len(entries), front_km=front,
            bearing_deg=mean_bearing(leading or [az for _, az in entries]),
        ))

    readings.sort(key=lambda r: (r.front_km, -r.count, r.sector))
    dominant = readings[0] if readings else None

    track_bearing, track_count = None, 0
    if dominant is not None:
        band = dominant.front_km + TRACK_BAND_KM
        body = [az for entries in buckets.values() for d, az in entries if d <= band]
        if body:
            track_bearing, track_count = mean_bearing(body), len(body)

    return Frame(
        now=now,
        active=tuple(readings),
        dominant=dominant,
        strikes_in_radius=in_radius,
        strikes_observed=observed,
        has_activity=dominant is not None and dominant.front_km <= radius_km,
        track_bearing=track_bearing,
        track_count=track_count,
    )


# ── Field motion ──────────────────────────────────────────────────────────────

# Phase correlation needs the two frames to be genuinely consecutive. Too short a
# baseline and the displacement is buried in the 1 km quantisation; too long and
# the field has evolved rather than moved, which correlation cannot distinguish
# from having stood still. Measured on three consecutive real products: a 10 min
# baseline scored consistently WORSE than 5 min, because summer convection decays
# and regenerates faster than it travels. Consecutive frames are the right pair.
MOTION_MIN_DT_SEC = 120.0
MOTION_MAX_DT_SEC = 900.0

# Side of the square used for the correlation, in km. Larger than the alert radius
# because correlation wants texture, but not enormous: a box spanning the whole
# country averages regions whose flow genuinely differs, and the peak flattens.
# 192 km covers a 48 km observation radius with generous context.
MOTION_BOX_KM = 192.0

# Below this share of wet pixels there is nothing to correlate, and the peak of
# an almost-empty field is noise that would fabricate a velocity.
MOTION_MIN_WET_FRACTION = 0.01

# Peak-to-sidelobe ratio: the peak divided by the best value outside a small
# exclusion box around it. This — not the peak's absolute height — is what
# separates a measurement from an invention, and the difference is not subtle.
#
# Calibrated against three consecutive real products at four sites. Every
# physically absurd reading (195 km/h NE over Rome, 252 km/h E over Bologna, each
# contradicted by the same site's other baselines) scored PSR 1.0-1.9. Every
# reading that agreed across box sizes and baselines scored 3.1-6.5. Nothing at
# all landed between 1.9 and 3.1, so the threshold sits in an empty gap rather
# than on a judgement call.
#
# An earlier version accepted on peak height alone and reported 27 km/h from a
# surface whose winner beat the runner-up by 6 %. That is precisely the class of
# error this module exists to refuse.
MOTION_MIN_PSR = 2.5
MOTION_PSR_EXCLUDE_PX = 3

# A field that appears to move faster than this was not tracked, it was mismatched.
MOTION_MAX_KMH = 180.0

# ...and a field that appears to move slower than half a pixel per frame did not
# move at all. The correlation peak is found on the integer grid; everything below
# one pixel comes from the parabolic fit, which always returns SOMETHING because a
# peak with any asymmetry at all has a sub-pixel maximum. At 1 km per pixel and
# 300 s between products, one pixel is 12 km/h — so a "3 km/h" reading is a
# quarter of a pixel, i.e. a correlation peak that sat squarely at zero shift with
# a direction supplied entirely by the interpolation.
#
# This is not a cosmetic threshold. On 17 Aug 2026 a rain front 19 km to the NW
# was reported as "moving NW at 3 km/h" — away from the observer — in the same
# message that correctly said it was heading straight for them. The rain arrived.
# The verdict was right and the drift line was noise with a compass bearing on it.
MOTION_MIN_PIXELS = 0.5


@dataclass(frozen=True)
class FieldMotion:
    """Velocity of the precipitation field over the ground, measured."""
    speed_kmh: float
    bearing_deg: float      # direction of travel, degrees clockwise from north
    psr: float              # peak-to-sidelobe ratio; 1.0 means indistinguishable
    dt_sec: float

    @property
    def east_kmh(self) -> float:
        return self.speed_kmh * math.sin(math.radians(self.bearing_deg))

    @property
    def north_kmh(self) -> float:
        return self.speed_kmh * math.cos(math.radians(self.bearing_deg))


def _parabolic_offset(lo: float, mid: float, hi: float) -> float:
    """Sub-pixel peak position from three samples. One kilometre of quantisation
    over five minutes is twelve km/h, which is a large share of a typical front's
    speed — worth the six lines it costs to remove."""
    denominator = lo - 2.0 * mid + hi
    if abs(denominator) < 1e-12:
        return 0.0
    return max(-1.0, min(1.0, 0.5 * (lo - hi) / denominator))


def field_motion(older: RadarGrid, newer: RadarGrid,
                 centre: tuple[float, float],
                 box_km: float = MOTION_BOX_KM) -> FieldMotion | None:
    """Ground velocity of the rain field between two rasters, or None.

    Phase correlation: the cross-power spectrum keeps only the phase difference
    between the two windows, so the peak of its inverse transform is the rigid
    displacement that best aligns them regardless of how the intensities changed.
    Both windows are taken at the SAME pixel indices, which — the two grids
    sharing one geotransform — means the same patch of ground even when the
    observer has moved between frames.

    None is returned whenever the measurement would be invented rather than made:
    wrong spacing, too little rain, a flat correlation surface, or an implausible
    speed. The caller must degrade to a distance-and-direction message instead of
    quoting an encounter time it does not have.
    """
    dt = newer.t - older.t
    if not (MOTION_MIN_DT_SEC <= dt <= MOTION_MAX_DT_SEC):
        return None
    if older.gt != newer.gt:
        return None

    c0, c1, r0, r1 = _window_bounds(older.gt, centre[0], centre[1], box_km / 2.0)
    if (c1 - c0) < 32 or (r1 - r0) < 32:
        return None

    a = np.asarray(older.data[r0:r1, c0:c1], dtype=np.float64)
    b = np.asarray(newer.data[r0:r1, c0:c1], dtype=np.float64)
    # Nodata is not a dry pixel, but for correlation purposes both contribute no
    # texture, and zero is the only neutral value the transform accepts.
    a = np.where(a > NODATA_THRESHOLD, a, 0.0)
    b = np.where(b > NODATA_THRESHOLD, b, 0.0)

    wet = min(float((a > 0).mean()), float((b > 0).mean()))
    if wet < MOTION_MIN_WET_FRACTION:
        return None

    # Remove the mean and taper the edges: without a window the frame boundary is
    # a step discontinuity that correlates with itself and pins the peak at zero.
    a -= a.mean()
    b -= b.mean()
    taper = np.outer(np.hanning(a.shape[0]), np.hanning(a.shape[1]))
    fa = np.fft.fft2(a * taper)
    fb = np.fft.fft2(b * taper)
    cross = fb * np.conj(fa)
    magnitude = np.abs(cross)
    magnitude[magnitude < 1e-12] = 1e-12
    surface = np.real(np.fft.ifft2(cross / magnitude))

    prow, pcol = np.unravel_index(int(np.argmax(surface)), surface.shape)
    height = float(surface[prow, pcol])
    if height <= 0:
        return None

    # How isolated is the winner? Blank a small box around it and take the best
    # of what remains: a broad, ambiguous ridge leaves a rival almost as tall,
    # while a genuine displacement leaves nothing close.
    rivals = surface.copy()
    rows = [(prow + d) % surface.shape[0]
            for d in range(-MOTION_PSR_EXCLUDE_PX, MOTION_PSR_EXCLUDE_PX + 1)]
    cols = [(pcol + d) % surface.shape[1]
            for d in range(-MOTION_PSR_EXCLUDE_PX, MOTION_PSR_EXCLUDE_PX + 1)]
    rivals[np.ix_(rows, cols)] = -np.inf
    sidelobe = float(rivals.max())
    psr = height / sidelobe if sidelobe > 0 else float("inf")
    if psr < MOTION_MIN_PSR:
        return None

    drow = prow + _parabolic_offset(float(surface[prow - 1, pcol]), height,
                                    float(surface[(prow + 1) % surface.shape[0], pcol]))
    dcol = pcol + _parabolic_offset(float(surface[prow, pcol - 1]), height,
                                    float(surface[prow, (pcol + 1) % surface.shape[1]]))
    # The transform wraps: the upper half of each axis is a negative shift.
    if drow > surface.shape[0] / 2:
        drow -= surface.shape[0]
    if dcol > surface.shape[1] / 2:
        dcol -= surface.shape[1]

    # Convert the pixel displacement into a true bearing by projecting both ends
    # back to coordinates. Doing the trigonometry in grid space instead would
    # ignore meridian convergence, which reaches a few degrees at the edges of
    # this grid — small, but free to avoid.
    col_mid = (c0 + c1) / 2.0
    row_mid = (r0 + r1) / 2.0
    lat_a, lon_a = pixel_to_latlon(older.gt, col_mid, row_mid)
    lat_b, lon_b = pixel_to_latlon(older.gt, col_mid + dcol, row_mid + drow)
    km = distance_km(float(lat_a), float(lon_a), float(lat_b), float(lon_b))
    speed = km / (dt / 3600.0)
    if speed > MOTION_MAX_KMH:
        return None
    if km < MOTION_MIN_PIXELS * older.gt.pixel_m / 1000.0:
        # A confidently measured standstill. Not the same as "no measurement":
        # the caller may legitimately report that the rain is not moving. The
        # floor is derived from the raster rather than fixed in km/h, so it stays
        # correct if the product's resolution or its cadence ever changes.
        return FieldMotion(0.0, 0.0, psr, dt)

    return FieldMotion(
        speed_kmh=speed,
        bearing_deg=azimuth_deg(float(lat_a), float(lon_a), float(lat_b), float(lon_b)),
        psr=psr,
        dt_sec=dt,
    )


# ── Intensity ─────────────────────────────────────────────────────────────────
#
# Lives here rather than with the monitor so the snapshot layer can name an
# intensity without importing the monitor that would import it back.

_INTENSITY_IT = ((0.5, "pioviggine"), (2.0, "debole"), (10.0, "moderata"),
                 (30.0, "forte"), (float("inf"), "nubifragio"))
_INTENSITY_EN = ((0.5, "drizzle"), (2.0, "light"), (10.0, "moderate"),
                 (30.0, "heavy"), (float("inf"), "torrential"))


def intensity_label(mmh: float, lang: str) -> str:
    for edge, label in (_INTENSITY_IT if lang == "it" else _INTENSITY_EN):
        if mmh < edge:
            return label
    return ""


# ── Encounter ─────────────────────────────────────────────────────────────────

# Below this the two are drifting together and the closest-approach time runs off
# to hours; there is no encounter to announce.
MIN_RELATIVE_SPEED_KMH = 3.0


def velocity_components(speed_kmh: float, course_deg: float) -> tuple[float, float]:
    """(east, north) components of a speed-and-course pair."""
    rad = math.radians(course_deg)
    return speed_kmh * math.sin(rad), speed_kmh * math.cos(rad)


@dataclass(frozen=True)
class Encounter:
    """Closest point of approach between the observer and the rain field."""
    minutes: float          # negative once the closest approach is behind us
    miss_km: float
    relative_speed_kmh: float
    miss_bearing_deg: float  # where the rain will sit when it is nearest

    @property
    def approaching(self) -> bool:
        return self.minutes > 0.0


def cpa(front_km: float, bearing_deg: float,
        rain_east_kmh: float, rain_north_kmh: float,
        own_east_kmh: float = 0.0, own_north_kmh: float = 0.0) -> Encounter | None:
    """When and how closely the rain and the observer will pass each other.

    This is what a measured field buys over a scatter of discharges. CBDR has to
    infer the answer from how a bearing drifts over a quarter of an hour; with two
    velocity vectors in hand the geometry is closed-form:

        t = −(r · v) / |v|²        v = v_rain − v_observer
        miss = |r + v t|

    Returns None only when the relative motion is too slow for the question to
    mean anything — never a guess.
    """
    rad = math.radians(bearing_deg)
    r_east = front_km * math.sin(rad)
    r_north = front_km * math.cos(rad)
    v_east = rain_east_kmh - own_east_kmh
    v_north = rain_north_kmh - own_north_kmh

    speed_sq = v_east * v_east + v_north * v_north
    if speed_sq < MIN_RELATIVE_SPEED_KMH ** 2:
        return None

    t_hours = -(r_east * v_east + r_north * v_north) / speed_sq
    miss_east = r_east + v_east * t_hours
    miss_north = r_north + v_north * t_hours
    miss_km = math.hypot(miss_east, miss_north)
    return Encounter(
        minutes=t_hours * 60.0,
        miss_km=miss_km,
        relative_speed_kmh=math.sqrt(speed_sq),
        # Which side it goes by. Degenerate on a dead-centre hit, where the
        # bearing is meaningless anyway and the caller reports a hit, not a side.
        miss_bearing_deg=(math.degrees(math.atan2(miss_east, miss_north)) % 360.0
                          if miss_km > 1e-9 else bearing_deg),
    )


__all__ = [
    "NODATA_THRESHOLD", "R_M",
    "RadarGridError", "GeoTransform", "RadarGrid", "parse_geotransform",
    "latlon_to_pixel", "pixel_to_latlon",
    "sample", "rain_points", "peak_in_disc", "coverage_fraction",
    "FRONT_RANK", "MIN_PIXELS_SECTOR", "build_rain_frame",
    "MOTION_BOX_KM", "MOTION_MIN_DT_SEC", "MOTION_MAX_DT_SEC", "MOTION_MAX_KMH",
    "MOTION_MIN_PIXELS",
    "FieldMotion", "field_motion",
    "MIN_RELATIVE_SPEED_KMH", "Encounter", "cpa", "velocity_components",
    "intensity_label",
]
