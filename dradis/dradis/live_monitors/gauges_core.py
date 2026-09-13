"""
live_monitors/gauges_core.py
─────────────────────────────
The MeteoHub reading, without the socket: query construction, the BUFR shapes
that come back, and the arithmetic that turns an accumulation series into a
rainfall rate. `gauges.py` is the half that fetches.

Separated for the reason `radar_core` is separated from `radar`, and `geo` from
`blitzortung`: every wrinkle of this format deserves a test, and none of them
needs an HTTP client installed to run. Nothing here decides anything either —
the caller is told what the stations said and is not allowed to act on it.

Why this reading has no authority
─────────────────────────────────
`rain_front` alerts on the DPC radar composite and MUST keep alerting on it
alone. A gauge reading is shown to the reader and never consulted by the
monitor, for three independent reasons, any one of which is sufficient:

1. It is as late as the radar or later. Stations publish on a ten-minute
   cadence with their own lag, and rainfall has to be integrated over a window
   before it is a rate at all. It always arrives after the decision is due.
2. A broken gauge and a dry gauge produce the same zero. A station that stops
   transmitting does not return an error — it returns fewer rows. Giving that
   zero a vote would make the monitor's silence depend on a sensor nobody is
   watching.
3. Zero millimetres is not an absence of rain. The nearest station can be seven
   kilometres away while the cell is at three.

So the module is careful to keep "no data" and "no rain" apart in the TYPE and
not merely in a value: `observe()` returns None when it could not measure, and
a `GaugeView` with an empty `wet` when it measured nothing falling. A caller
that collapses the two has thrown away the only thing that makes the line
honest.

Properties of the source, each verified against the live service
────────────────────────────────────────────────────────────────
* `/api/observations` is public — no account, no token. ~0.3 s for a small box.
* `license:...` in `q` is mandatory. `CCBY_COMPLIANT` is the only group that
  serves observations; `CCBY-SA_COMPLIANT` answers 500.
* `reftime:` is mandatory and is UTC, whatever the station's own clock says.
* Several products in one query are joined by ` or `. A comma silently keeps
  ONE of them and returns a confident partial answer, which is worse than an
  error.
* `stationDetails=true` and `allStationProducts=true` both demand a `networks`
  parameter that accepts a single network (a comma list 404s), so neither is
  used — the station name arrives in `stat.details` regardless.
* A query with no bounding box scans the country: 23 seconds and 600 KB. The
  box is not an optimisation, it is the difference between usable and not.
* Accumulated precipitation carries its period in the timerange: `1,0,60` is a
  one-minute bucket on some networks and `1,0,3600` an hourly total on others,
  in the same response. Assuming either one is how a 0.2 mm minute becomes
  12 mm/h.

Attribution
───────────
CC BY 4.0. The network id travels with every reading and is printed by the
caller, which is what discharges the licence in the place the data is shown.
"""

import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .geo import azimuth_deg, distance_km, offset_km

_LOGGER = logging.getLogger(__name__)

API_URL = "https://meteohub.agenziaitaliameteo.it/api/observations"
LICENSE_GROUP = "CCBY_COMPLIANT"

# BUFR descriptors. One request carries as many as are asked for, so the only
# cost of a wider set is bytes.
VAR_PRECIP   = "B13011"    # kg/m**2, i.e. mm, accumulated over the timerange
VAR_TEMP     = "B12101"    # K
VAR_GUST     = "B11041"    # m/s, MAXIMUM over the timerange, not instantaneous
VAR_HUMIDITY = "B13003"    # %
VAR_PRESSURE = "B10004"    # Pa
VAR_WIND     = "B11002"    # m/s
VAR_WIND_DIR = "B11001"    # degrees true
VAR_RIVER    = "B13215"    # m, stage above a LOCAL datum — see PLAUSIBLE_RANGE
VAR_SNOW     = "B13013"    # m, total snow depth
VAR_NAME     = "B01019"    # long station name

# What `rain_front` asks for: the measurement it prints plus the two that ride
# along for free in the same response.
MONITOR_PRODUCTS = (VAR_PRECIP, VAR_TEMP, VAR_GUST)

# What a readout asks for. Nine terms joined by " or " were verified against the
# live service; the cost is bytes, not stations.
READOUT_PRODUCTS = (VAR_PRECIP, VAR_TEMP, VAR_GUST, VAR_HUMIDITY, VAR_PRESSURE,
                    VAR_WIND, VAR_WIND_DIR, VAR_RIVER)

# VAR_SNOW is deliberately NOT in that list, and the constant is kept only so
# the descriptor is documented.
#
# Measured on 13 September 2026, nationally: 191 snow-depth series, of which 83
# report more than 5 cm. Barco (Emilia-Romagna lowland) reports 4.30, Torino
# Giardini Reali — the city centre — 1.13, and several series swing between
# -0.02 and +0.47 within three hours. Negative depths mean the sensors are
# unreferenced, and it is not even possible to tell from the response whether a
# network is publishing metres, as the descriptor claims, or centimetres: at
# 4.3 cm and 1.13 cm those same numbers would be ordinary out-of-season noise.
#
# Unlike the river datum, which is wrong on 4% of stations, this is wrong on
# most of them for most of the year, and there is nothing in the data to
# separate a real 40 cm in January from a drifting sensor in September. A line
# that is wrong most of the year is not a reading — the same conclusion v4.8.2
# reached about a badge that was amber most of the year.

# Bounds outside which a value is an instrument fault or a different quantity
# wearing the same descriptor, in the units the service publishes.
#
# This is not defensive tidiness. B13215 carries -1000.0 flagged `rel=1` —
# RELIABLE, by the service's own reliability flag — on 2 of 1091 gauges, and
# about 42 more report a reservoir or lake surface in metres above sea level
# (Piediluco at +368) rather than a river stage above a local datum. The service
# cannot tell them apart and neither can a downstream consumer, so anything
# outside a physically sane band is dropped where it is read.
PLAUSIBLE_RANGE = {
    VAR_PRECIP:   (0.0, 500.0),        # mm accumulated in one bucket
    VAR_TEMP:     (173.15, 333.15),    # K, -100..+60 C
    VAR_GUST:     (0.0, 120.0),        # m/s
    VAR_HUMIDITY: (0.0, 100.0),        # %
    VAR_PRESSURE: (80000.0, 110000.0), # Pa
    VAR_WIND:     (0.0, 120.0),        # m/s
    VAR_WIND_DIR: (0.0, 360.0),        # degrees
    VAR_RIVER:    (-20.0, 20.0),       # m above a local datum
    VAR_SNOW:     (0.0, 30.0),         # m
}

# How far back to ask.
#
# This is set by the PUBLICATION LAG, which is a property of each network and
# varies enormously. Measured against the live service on 2026-09-13 at 07:42
# UTC, newest sample per network:
#
#     sir-toscana      12 min        dpcn-lazio       52-57 min
#     dpcn-campania    27 min        dpcn-puglia      52-132 min
#     dpcn-piemonte    30-42 min     mnw (amateur)    42-282 min
#     dpcn-lombardia   32-42 min
#
# A thirty-minute window returned ZERO stations for Rome while 85 of them were
# publishing normally — the data was simply older than the question. The window
# has to cover the worst lag worth serving PLUS the rate window below, since a
# station whose newest sample is inside the window but has no buckets behind it
# yields no rate at all.
#
# Ninety minutes costs ~115 KB on a dense urban box against ~38 KB at sixty,
# which is worth it for a lazy fetch made a handful of times per event.
WINDOW_MIN = 90

# A reading older than this is not a present-tense fact, whatever the station
# thinks. Stations drop out routinely and a stale row is indistinguishable from
# a fresh one except by its timestamp, so the age is checked here rather than
# trusted. Matched to the window: a network lagging further than this (Puglia
# does, some days) drops out entirely, and the caller says so rather than
# reporting a two-hour-old zero as if it were now.
MAX_READING_AGE_SEC = WINDOW_MIN * 60.0

# Rain rate is derived from accumulation, and accumulation over a short bucket
# is quantised: a single 0.2 mm tip in a one-minute bucket is 12 mm/h if read
# literally. Buckets are therefore summed over at least this long before being
# turned into a rate.
MIN_RATE_WINDOW_SEC = 900.0

# The official networks: regional civil protection (dpcn-*), the regional
# agencies, and the Emilia-Romagna set MeteoHub publishes under its own names.
# `mnw` — MeteoNetwork, 1033 stations and the largest single network — is
# amateur: siting and shielding are not guaranteed, and a badly exposed gauge
# is wrong in a way that looks exactly like a right one.
OFFICIAL_NETWORKS = frozenset({
    "arpafvg", "sir-toscana", "open-trentino",
    "agrmet", "boa", "claster", "locali", "simnbo", "simnpr", "spdsra",
    "urbane", "marefe",
})
OFFICIAL_PREFIXES = ("dpcn-",)


@dataclass(frozen=True)
class Measurement:
    """One quantity from one station, in the unit the service published it.

    Carries its OWN timestamp. The gust is a maximum over the past hour and
    arrives hourly; the rain arrives every ten minutes. A single age per station
    would misdate one of them on every readout that prints both.
    """

    value: float
    observed_at: float           # epoch of the newest sample behind it
    window_min: int = 0          # 0 for an instantaneous reading


@dataclass(frozen=True)
class GaugeReading:
    """One station's latest word, already in the units a message prints."""

    name: str
    network: str
    lat: float
    lon: float
    distance_km: float
    bearing_deg: float
    measurements: dict            # BUFR descriptor -> Measurement

    # ── Compatibility surface ────────────────────────────────────────────────
    #
    # `rain_front` reads these four and nothing else, so they stay exactly what
    # they were before the station learned to report more than rain.

    @property
    def mmh(self) -> float | None:
        """Rainfall rate. None: the station reports no precipitation at all."""
        m = self.measurements.get(VAR_PRECIP)
        return None if m is None else m.value

    @property
    def window_min(self) -> int:
        m = self.measurements.get(VAR_PRECIP)
        return 0 if m is None else m.window_min

    @property
    def observed_at(self) -> float:
        """When the RAINFALL behind `mmh` was measured.

        Deliberately the precipitation timestamp rather than the newest of all
        of them. Each quantity now carries its own — `measurements[var].observed_at`
        — because the gust is an hourly maximum while the rain arrives every ten
        minutes, and one age per station was a false simplification the moment a
        second quantity got printed.
        """
        m = self.measurements.get(VAR_PRECIP)
        if m is not None:
            return m.observed_at
        return max((x.observed_at for x in self.measurements.values()), default=0.0)

    @property
    def gust_kmh(self) -> float | None:
        m = self.measurements.get(VAR_GUST)
        return None if m is None else m.value * 3.6

    @property
    def temp_c(self) -> float | None:
        m = self.measurements.get(VAR_TEMP)
        return None if m is None else m.value - 273.15

    @property
    def age_sec(self) -> float:
        return max(0.0, time.time() - self.observed_at)

    def age_of(self, var: str, now: float | None = None) -> float | None:
        """How old THIS quantity's reading is, or None if the station lacks it."""
        m = self.measurements.get(var)
        if m is None:
            return None
        return max(0.0, (time.time() if now is None else now) - m.observed_at)


@dataclass(frozen=True)
class GaugeView:
    """What the station network had to say about one disc at one moment."""

    readings: tuple[GaugeReading, ...]     # wettest first, then nearest
    wet: tuple[GaugeReading, ...]          # those at or above the caller's bar
    nearest: GaugeReading | None           # nearest RAIN GAUGE — see `parse`
    radius_km: float
    fetched_at: float

    def __bool__(self) -> bool:
        return bool(self.readings)


# ── Query construction ────────────────────────────────────────────────────────

def _reftime(now: float, window_min: int) -> str:
    end = datetime.fromtimestamp(now, timezone.utc)
    start = end - timedelta(minutes=window_min)
    fmt = "%Y-%m-%d %H:%M"
    return f"reftime:>={start.strftime(fmt)},<={end.strftime(fmt)}"


def _query(now: float, window_min: int, products) -> str:
    return (f"license:{LICENSE_GROUP};product:{' or '.join(products)};"
            f"{_reftime(now, window_min)}")


def _bbox(lat: float, lon: float, radius_km: float) -> dict:
    """The smallest box containing the disc, in the flat-earth approximation the
    rest of the monitor already uses at these distances."""
    south, west = offset_km(lat, lon, -radius_km, -radius_km)
    north, east = offset_km(lat, lon, radius_km, radius_km)
    return {"latmin": round(south, 4), "latmax": round(north, 4),
            "lonmin": round(west, 4), "lonmax": round(east, 4)}


def build_params(lat: float, lon: float, radius_km: float,
                 now: float, window_min: int = WINDOW_MIN,
                 products=MONITOR_PRODUCTS) -> dict:
    """The complete query string set for one disc.

    Assembled here rather than at the call site so the two rules that bite are
    in one testable place: the mandatory licence group and UTC reftime, and the
    ` or ` product join that a comma silently turns into a partial answer.
    """
    return {"q": _query(now, window_min, products),
            **_bbox(lat, lon, radius_km)}


def _is_official(network: str) -> bool:
    return network in OFFICIAL_NETWORKS or network.startswith(OFFICIAL_PREFIXES)


# ── Parsing ───────────────────────────────────────────────────────────────────

def _period_sec(trange: str) -> int | None:
    """The accumulation period out of a dballe timerange `pind,p1,p2`.

    Only `pind == 1` — accumulation — is a period this function will report.
    An instantaneous value (254) or a maximum (2) has a timerange too, and
    reading one of those as an accumulation window is how a gust becomes a
    rainfall rate.
    """
    parts = trange.split(",")
    if len(parts) != 3:
        return None
    try:
        pind, _p1, p2 = (int(p) for p in parts)
    except ValueError:
        return None
    return p2 if pind == 1 and p2 > 0 else None


def _samples(product: dict, now: float) -> list[tuple[float, float]]:
    """(epoch, value) pairs, oldest first, dropping anything unusable.

    `rel` is MeteoHub's own reliability flag; a value it has already doubted is
    not made better by being recent.
    """
    out = []
    for entry in product.get("val", ()):
        value = entry.get("val")
        ref = entry.get("ref")
        if value is None or ref is None or entry.get("rel") == 0:
            continue
        try:
            stamp = datetime.fromisoformat(ref).replace(tzinfo=timezone.utc).timestamp()
        except (TypeError, ValueError):
            continue
        if now - stamp > MAX_READING_AGE_SEC:
            continue
        try:
            out.append((stamp, float(value)))
        except (TypeError, ValueError):
            continue
    out.sort()
    return out


def _rate_mmh(product: dict, now: float) -> tuple[float | None, int, float] | None:
    """Rainfall rate in mm/h from an accumulation series, with its window.

    Two shapes arrive in the same response and both are handled by the same
    arithmetic rather than by branching on the network:

    * short buckets (`1,0,60`) are summed until they span at least
      `MIN_RATE_WINDOW_SEC`, so a single tip cannot become a downpour;
    * an hourly total (`1,0,3600`) is already the rate, and its newest sample
      is taken as it stands.

    Returns (mm/h, window minutes, epoch of the newest sample), or None when the
    series carries nothing usable.
    """
    period = _period_sec(product.get("trange", ""))
    if period is None:
        return None
    samples = _samples(product, now)
    if not samples:
        return None

    newest = samples[-1][0]

    if period >= MIN_RATE_WINDOW_SEC:
        # Already integrated over a long enough window by the station itself.
        return samples[-1][1] * 3600.0 / period, round(period / 60), newest

    # Sum buckets back from the newest until the window is wide enough. The
    # buckets are contiguous by construction, so their count times the period is
    # the span — using the timestamps instead would lose the final bucket's own
    # duration and inflate the rate.
    wanted = max(1, math.ceil(MIN_RATE_WINDOW_SEC / period))
    chunk = samples[-wanted:]
    span = len(chunk) * period
    total = sum(value for _stamp, value in chunk)
    return total * 3600.0 / span, round(span / 60), newest


def _newest(product: dict, now: float) -> tuple[float, float] | None:
    """The latest usable (value, epoch) of an instantaneous series."""
    samples = _samples(product, now)
    return (samples[-1][1], samples[-1][0]) if samples else None


def _window_min(product: dict) -> int:
    """The span a non-accumulated value covers, in minutes.

    A gust is `2,0,3600` — a MAXIMUM over the past hour, not a reading taken at
    the timestamp it carries — and a readout that prints it as an instantaneous
    value is wrong by up to an hour. An instantaneous series (`254,0,0`) has no
    span and reports 0.
    """
    parts = (product.get("trange") or "").split(",")
    if len(parts) != 3:
        return 0
    try:
        pind, _p1, p2 = (int(x) for x in parts)
    except ValueError:
        return 0
    return round(p2 / 60) if pind in (1, 2, 3) and p2 > 0 else 0


def _plausible(var: str, value: float) -> bool:
    """Whether a value is the quantity its descriptor claims.

    See `PLAUSIBLE_RANGE`: the service flags a -1000 river stage as reliable,
    and publishes reservoir surfaces in metres above sea level under the same
    descriptor as a river stage above a local datum.
    """
    lo, hi = PLAUSIBLE_RANGE.get(var, (float("-inf"), float("inf")))
    return lo <= value <= hi


def parse(payload: dict, lat: float, lon: float, radius_km: float, *,
          min_mmh: float, official_only: bool = True,
          now: float | None = None) -> GaugeView:
    """Turn one `/api/observations` response into readings around a point.

    Pure: no clock of its own beyond `now`, no network. Every wrinkle of the
    format that this has to survive is in `tests/fixtures/`.
    """
    now = time.time() if now is None else now
    readings: list[GaugeReading] = []

    for station in payload.get("data", ()):
        stat = station.get("stat") or {}
        network = stat.get("net") or ""
        if official_only and not _is_official(network):
            continue
        try:
            slat, slon = float(stat["lat"]), float(stat["lon"])
        except (KeyError, TypeError, ValueError):
            continue

        # The box is square and the disc is round: the corners are outside the
        # radius the caller asked about and must not be reported as inside it.
        dist = distance_km(lat, lon, slat, slon)
        if dist > radius_km:
            continue

        name = ""
        for detail in stat.get("details", ()):
            if detail.get("var") == VAR_NAME and detail.get("val"):
                name = str(detail["val"])
                break

        # Whatever the station published, in the unit it published it. The
        # readout picks what it can use; nothing is special-cased here except
        # precipitation, which is an accumulation and has to become a rate.
        measurements: dict = {}
        for product in station.get("prod", ()):
            var = product.get("var")
            if var == VAR_PRECIP:
                rate = _rate_mmh(product, now)
                if rate is not None and _plausible(var, rate[0]):
                    measurements[var] = Measurement(rate[0], rate[2], rate[1])
            elif var in PLAUSIBLE_RANGE:
                latest = _newest(product, now)
                if latest is not None and _plausible(var, latest[0]):
                    measurements[var] = Measurement(latest[0], latest[1],
                                                    _window_min(product))

        if not measurements:
            continue

        readings.append(GaugeReading(
            name=name or network or "?", network=network,
            lat=slat, lon=slon, distance_km=dist,
            bearing_deg=azimuth_deg(lat, lon, slat, slon),
            measurements=measurements,
        ))

    # Wettest first so the caller can name the strongest without re-sorting, and
    # nearest as the tie-break so a row of dry stations still leads with the one
    # the reader cares about.
    readings.sort(key=lambda r: (-(r.mmh or 0.0), r.distance_km))
    wet = tuple(r for r in readings if r.mmh is not None and r.mmh >= min_mmh)

    # `nearest` is the nearest station that MEASURES PRECIPITATION, not the
    # nearest station. A thermometer three kilometres away is closer and
    # answers a different question, and it is `nearest` that gets named in the
    # line reporting that nothing is wet — where naming an instrument with no
    # rain gauge in it would be a straightforwardly misleading answer.
    gauges = [r for r in readings if r.mmh is not None]
    nearest = min(gauges, key=lambda r: r.distance_km) if gauges else None
    return GaugeView(readings=tuple(readings), wet=wet, nearest=nearest,
                     radius_km=radius_km, fetched_at=now)


__all__ = [
    "GaugeReading", "GaugeView", "parse", "build_params",
    "API_URL", "LICENSE_GROUP", "OFFICIAL_NETWORKS", "OFFICIAL_PREFIXES",
    "WINDOW_MIN", "MIN_RATE_WINDOW_SEC", "MAX_READING_AGE_SEC",
    "VAR_PRECIP", "VAR_TEMP", "VAR_GUST", "VAR_NAME",
]
