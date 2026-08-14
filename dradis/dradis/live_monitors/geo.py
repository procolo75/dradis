"""
live_monitors/geo.py
─────────────────────
Pure geographic helpers: distances, bearings, compass labels and the geohash
encoding Blitzortung uses for its MQTT topic tree.

Kept separate from `blitzortung.py` on purpose. The decision core needs the
maths, but must not drag in a module that opens sockets — that is what keeps
`storm_front_core` importable and unit-testable without aiomqtt installed.
"""

import math

EARTH_RADIUS_KM = 6371.0


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance. Haversine — correct at any latitude and separation."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def azimuth_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial great-circle bearing from point 1 to point 2, degrees clockwise from N."""
    dlon = math.radians(lon2 - lon1)
    p1, p2 = math.radians(lat1), math.radians(lat2)
    x = math.sin(dlon) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


_DIR_IT = ["N", "NE", "E", "SE", "S", "SO", "O", "NO"]
_DIR_EN = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def direction_label(azimuth: float, lang: str) -> str:
    """8-point compass label. The analysis grid is finer than this (30° sectors),
    but a user reads a bearing, not a sector index."""
    labels = _DIR_IT if lang == "it" else _DIR_EN
    return labels[round(azimuth / 45) % 8]


def offset_km(lat: float, lon: float, north_km: float, east_km: float) -> tuple[float, float]:
    """Point `north_km`/`east_km` away from (lat, lon). Flat-earth approximation,
    accurate well past the ~60 km this monitor ever looks at."""
    dlat = north_km / 111.32
    dlon = east_km / (111.32 * max(0.01, math.cos(math.radians(lat))))
    return lat + dlat, lon + dlon


# ── Geohash (Blitzortung topic derivation) ────────────────────────────────────

_BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"

# Precision-3 geohash cells are 1.40625° × 1.40625° (8 lon bits, 7 lat bits).
GEOHASH_PRECISION = 3
_CELL_LAT_DEG = 180.0 / (2 ** 7)
_CELL_LON_DEG = 360.0 / (2 ** 8)


def geohash_encode(lat: float, lon: float, precision: int = GEOHASH_PRECISION) -> str:
    min_lat, max_lat = -90.0, 90.0
    min_lon, max_lon = -180.0, 180.0
    chars, bits, is_lon, char_bits = [], 0, True, 0
    for _ in range(precision * 5):
        if is_lon:
            mid = (min_lon + max_lon) / 2
            if lon >= mid:
                bits = (bits << 1) | 1; min_lon = mid
            else:
                bits <<= 1; max_lon = mid
        else:
            mid = (min_lat + max_lat) / 2
            if lat >= mid:
                bits = (bits << 1) | 1; min_lat = mid
            else:
                bits <<= 1; max_lat = mid
        is_lon = not is_lon
        char_bits += 1
        if char_bits == 5:
            chars.append(_BASE32[bits & 0x1F]); bits = 0; char_bits = 0
    return "".join(chars)


def _ring_size_for_radius(radius_km: float) -> int:
    """How many cells to extend in each direction around the origin cell.

    A 3×3 block is anchored on the *cell* the origin falls in, not on the origin
    itself, so the guaranteed coverage measured from the origin is only one full
    cell — about 110 km east/west at mid latitudes. Beyond that the ring widens
    instead of silently under-counting strikes.
    """
    worst_case_km = _CELL_LON_DEG * 111.32 * math.cos(math.radians(45))  # ~110 km
    return max(1, math.ceil(radius_km / worst_case_km))


def topics_for_area(lat: float, lon: float, radius_km: float) -> list[str]:
    """Blitzortung MQTT topic filters covering `radius_km` around (lat, lon)."""
    ring = _ring_size_for_radius(radius_km)
    cells: set[str] = set()
    for dlat in range(-ring, ring + 1):
        for dlon in range(-ring, ring + 1):
            nlat = max(-90.0, min(90.0, lat + dlat * _CELL_LAT_DEG))
            nlon = ((lon + dlon * _CELL_LON_DEG + 180) % 360) - 180
            cells.add(geohash_encode(nlat, nlon, GEOHASH_PRECISION))
    return sorted(f"blitzortung/1.1/{c[0]}/{c[1]}/{c[2]}/#" for c in cells)


__all__ = [
    "distance_km", "azimuth_deg", "direction_label", "offset_km",
    "geohash_encode", "topics_for_area", "GEOHASH_PRECISION", "EARTH_RADIUS_KM",
]
