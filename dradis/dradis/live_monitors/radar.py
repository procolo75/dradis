"""
live_monitors/radar.py
───────────────────────
Ingest half of the rain front monitor: a polling client for the Dipartimento
della Protezione Civile radar composite. The maths lives in `radar_core`.

Nothing here makes a decision. The feed only answers "which rasters have I got,
and is my source healthy" — mirroring `BlitzortungFeed` deliberately, down to
`feed_ok()` and `connected_for()`, so the storm front's decision core accepts it
without knowing which kind of sky it is looking at.

Why this is a shared singleton and the strike feed is not
────────────────────────────────────────────────────────
A Blitzortung subscription is aimed at an area, so each monitor needs its own.
One radar product is a single file covering the entire country, so a second
monitor watching a different town costs exactly nothing extra. Downloading it
twice would be pure waste, and downloading it per-monitor would make the request
rate grow with the number of monitors against a public service that is doing us
a favour. Hence one instance, reference-counted by its consumers: it runs while
somebody needs it and stops when the last monitor goes away.

Why polling is scheduled rather than periodic
─────────────────────────────────────────────
The API states its own cadence (`"period": "PT5M"`), so the next product's
arrival time is known rather than guessed. Sleeping until then costs one small
metadata request per cycle instead of hammering a public endpoint on a fixed
timer. The GeoTIFF — the expensive part — is fetched only when the timestamp has
actually advanced.

Health
──────
An empty sky and a dead source are indistinguishable if you only look at the
pixels, so age is tracked explicitly. A raster older than `MAX_GRID_AGE_SEC` is
not "no rain", it is "no measurement", and `feed_ok()` says so — which is what
makes the core refuse to issue an all-clear it has not earned.
"""

import asyncio
import io
import logging
import time
from collections import deque

import httpx
import numpy as np

from .radar_core import RadarGrid, parse_geotransform

_LOGGER = logging.getLogger(__name__)

API_BASE = "https://radar-api.protezionecivile.it"
# The service requires this header. Without it the endpoints refuse the request.
API_HEADERS = {
    "origin": "https://radar.protezionecivile.it",
    "referer": "https://radar.protezionecivile.it/",
}

PRODUCT_RAIN = "SRI"      # surface rainfall intensity, mm/h
PRODUCT_HAIL = "POH"      # probability of hail, %

HTTP_TIMEOUT_SEC = 30.0
RETRY_DELAY_SEC = 30.0
MAX_RETRY_DELAY_SEC = 300.0

# ── Publication lag ───────────────────────────────────────────────────────────
#
# Measured against the live service: every 5-minute product becomes available
# about TEN MINUTES after the timestamp it carries (SRI, VMI and POH all reported
# 15:05:00Z at 15:15:01Z; the hourly TEMP was 75 minutes behind). This is a
# property of the source, not a bug to engineer around, and it has two
# consequences that run through the whole monitor:
#
#   · The freshest possible measurement is already ten minutes old, so a front
#     moving at 30 km/h has travelled ~5 km since it was seen. Advecting the
#     field by the measured motion is therefore a correction, not a refinement —
#     and when motion is not measurable, the message must say how old the picture
#     is rather than implying it is live.
#   · Polling on a fixed 5-minute timer would find nothing new for two cycles and
#     then hammer the endpoint. The lag is learned per product instead, so each
#     cycle costs roughly one small metadata request.
PUBLICATION_LAG_INITIAL_SEC = 600.0
PUBLICATION_LAG_ALPHA = 0.3
PUBLICATION_LAG_MAX_SEC = 2400.0

# Asked too early: wait this long before asking again rather than spinning.
RECHECK_SEC = 60.0
PRODUCT_MARGIN_SEC = 30.0
MIN_SLEEP_SEC = 20.0
MAX_SLEEP_SEC = 300.0

# Two frames are all the motion estimate needs, and each one is 1200×1400 float32
# — 6.7 MB. Keeping more would cost real memory on a Raspberry Pi for no gain.
FRAME_HISTORY = 2

# Beyond this a raster is not evidence any more. It has to clear the ten minutes
# of inherent lag plus a couple of missed publications, or the monitor would go
# blind on a source that is behaving perfectly normally.
MAX_GRID_AGE_SEC = 1500.0

DEFAULT_PERIOD_SEC = 300.0


def _parse_iso_period(text: str | None) -> float:
    """Minimal ISO-8601 duration reader for the `PT5M` the API returns."""
    if not text or not text.startswith("PT"):
        return DEFAULT_PERIOD_SEC
    seconds = 0.0
    number = ""
    for char in text[2:]:
        if char.isdigit() or char == ".":
            number += char
            continue
        try:
            value = float(number)
        except ValueError:
            return DEFAULT_PERIOD_SEC
        seconds += value * {"H": 3600.0, "M": 60.0, "S": 1.0}.get(char, 0.0)
        number = ""
    return seconds or DEFAULT_PERIOD_SEC


def _decode(raw: bytes, t: float, product: str) -> RadarGrid:
    """Turn the downloaded GeoTIFF into a grid. Runs on a worker thread: Pillow
    decodes 1.68 M LZW-compressed float32 pixels, which is not event-loop work."""
    from PIL import Image
    with Image.open(io.BytesIO(raw)) as image:
        data = np.array(image)
        transform = parse_geotransform(image.tag_v2, image.size[0], image.size[1])
    return RadarGrid(t=t, product=product, data=data, gt=transform)


async def fetch_latest(product: str = PRODUCT_RAIN) -> tuple[RadarGrid, float]:
    """One-shot fetch of the newest product, outside the shared feed's schedule.

    Exists for the settings-panel test button, which has to answer "can this
    add-on reach the radar, and does it cover where you live" on demand — a
    question the feed cannot be asked without disturbing its polling cadence.
    Returns the grid and how late the product was published.
    """
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SEC, headers=API_HEADERS,
                                 follow_redirects=True) as client:
        response = await client.get(f"{API_BASE}/findLastProductByType",
                                    params={"type": product})
        response.raise_for_status()
        products = (response.json() or {}).get("lastProducts") or []
        if not products:
            raise RuntimeError(f"no {product} product advertised")
        stamp_ms = int(products[0]["time"])

        download = await client.post(f"{API_BASE}/downloadProduct",
                                     json={"productType": product,
                                           "productDate": stamp_ms})
        download.raise_for_status()
        url = (download.json() or {}).get("url")
        if not url:
            raise RuntimeError(f"no download url for {product}")

        raw = await client.get(url, headers={})
        raw.raise_for_status()

    stamp = stamp_ms / 1000.0
    grid = await asyncio.to_thread(_decode, raw.content, stamp, product)
    return grid, max(0.0, time.time() - stamp)


class RadarFeed:
    """Shared poller for one or more DPC radar products."""

    def __init__(self):
        self._frames: dict[str, deque] = {}
        self._consumers: dict[str, int] = {}
        self._period: dict[str, float] = {}
        self._next_due: dict[str, float] = {}
        self._lag: dict[str, float] = {}

        self._task: asyncio.Task | None = None
        self._downloads = 0
        self._checks = 0
        self._errors = 0
        self._consecutive_errors = 0
        self._healthy_since = 0.0
        self._last_error = ""

    # ── Consumers ─────────────────────────────────────────────────────────────

    def acquire(self, *products: str) -> None:
        """Register interest in products, starting the loop if it is not running."""
        for product in products:
            if not product:
                continue
            self._consumers[product] = self._consumers.get(product, 0) + 1
            self._frames.setdefault(product, deque(maxlen=FRAME_HISTORY))
            self._next_due.setdefault(product, 0.0)
        self.start()

    def release(self, *products: str) -> None:
        """Drop interest. The loop stops — and the rasters are freed — once no
        monitor is watching anything."""
        for product in products:
            if product not in self._consumers:
                continue
            self._consumers[product] -= 1
            if self._consumers[product] <= 0:
                del self._consumers[product]
                self._frames.pop(product, None)
                self._next_due.pop(product, None)
        if not self._consumers:
            self.stop()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        if not self._consumers:
            return
        self._task = asyncio.create_task(self._run(), name="radar_feed")

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        self._healthy_since = 0.0

    async def aclose(self) -> None:
        task = self._task
        self.stop()
        if task:
            await asyncio.gather(task, return_exceptions=True)

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    # ── Data ──────────────────────────────────────────────────────────────────

    def frames(self, product: str) -> tuple[RadarGrid, ...]:
        """Buffered rasters, oldest first."""
        return tuple(self._frames.get(product, ()))

    def latest(self, product: str) -> RadarGrid | None:
        buffered = self._frames.get(product)
        return buffered[-1] if buffered else None

    def age_sec(self, product: str, now: float) -> float | None:
        grid = self.latest(product)
        return None if grid is None else max(0.0, now - grid.t)

    # ── Health, as inputs to the decision core ────────────────────────────────

    def feed_ok(self, product: str, now: float) -> bool:
        """True only when there is a measurement recent enough to reason about.

        A stale raster must never read as a quiet sky: that is the failure that
        would let the monitor announce the rain had cleared while it was simply
        no longer looking.
        """
        age = self.age_sec(product, now)
        return age is not None and age <= MAX_GRID_AGE_SEC

    def connected_for(self, now: float) -> float:
        """Seconds of uninterrupted healthy polling, 0.0 while broken.

        The core uses this to refuse an all-clear it has not earned — a feed that
        just recovered has not been watching long enough to know.
        """
        if not self._healthy_since:
            return 0.0
        return max(0.0, now - self._healthy_since)

    def status(self) -> str:
        if not self.is_running():
            return "stopped"
        if self._consecutive_errors >= 3:
            return "degraded"
        now = time.time()
        if not self._consumers:
            return "running"
        if any(not self.feed_ok(p, now) for p in self._consumers):
            return "degraded"
        return "running"

    def stats(self) -> dict:
        now = time.time()
        return {
            "products": sorted(self._consumers),
            "downloads": self._downloads,
            "checks": self._checks,
            "errors": self._errors,
            "last_error": self._last_error,
            "ages": {p: (None if (a := self.age_sec(p, now)) is None else round(a))
                     for p in sorted(self._consumers)},
            "lag": {p: round(self._lag[p]) for p in sorted(self._lag)},
        }

    # ── Polling ───────────────────────────────────────────────────────────────

    async def _run(self) -> None:
        backoff = RETRY_DELAY_SEC
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SEC,
                                         headers=API_HEADERS,
                                         follow_redirects=True) as client:
                while True:
                    try:
                        await self._refresh_due(client)
                        self._consecutive_errors = 0
                        self._last_error = ""
                        if not self._healthy_since:
                            self._healthy_since = time.time()
                        backoff = RETRY_DELAY_SEC
                        await asyncio.sleep(self._sleep_for())
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        self._errors += 1
                        self._consecutive_errors += 1
                        self._last_error = str(e)
                        self._healthy_since = 0.0
                        _LOGGER.warning("[Radar] poll failed: %s — retry in %.0fs",
                                        e, backoff)
                        await asyncio.sleep(backoff)
                        backoff = min(MAX_RETRY_DELAY_SEC, backoff * 2)
        except asyncio.CancelledError:
            return

    def _sleep_for(self) -> float:
        now = time.time()
        if not self._next_due:
            return MAX_SLEEP_SEC
        wait = min(self._next_due.values()) - now
        return max(MIN_SLEEP_SEC, min(MAX_SLEEP_SEC, wait))

    async def _refresh_due(self, client: httpx.AsyncClient) -> None:
        now = time.time()
        for product in list(self._consumers):
            if self._next_due.get(product, 0.0) > now:
                continue
            await self._refresh(client, product)

    async def _refresh(self, client: httpx.AsyncClient, product: str) -> None:
        self._checks += 1
        response = await client.get(f"{API_BASE}/findLastProductByType",
                                    params={"type": product})
        response.raise_for_status()
        products = (response.json() or {}).get("lastProducts") or []
        if not products:
            raise RuntimeError(f"no {product} product advertised")

        entry = products[0]
        stamp = int(entry["time"]) / 1000.0
        period = _parse_iso_period(entry.get("period"))
        self._period[product] = period

        buffered = self._frames.setdefault(product, deque(maxlen=FRAME_HISTORY))
        if buffered and buffered[-1].t == stamp:
            # Asked too early. Do not recompute the schedule from a stamp that has
            # not moved — that would put the next check in the past and turn this
            # into a spin against a public endpoint.
            self._next_due[product] = time.time() + RECHECK_SEC
            return

        lag = max(0.0, time.time() - stamp)
        previous = self._lag.get(product, PUBLICATION_LAG_INITIAL_SEC)
        blended = previous + PUBLICATION_LAG_ALPHA * (lag - previous)
        self._lag[product] = min(PUBLICATION_LAG_MAX_SEC, max(0.0, blended))
        self._next_due[product] = (stamp + period + self._lag[product]
                                   + PRODUCT_MARGIN_SEC)

        download = await client.post(f"{API_BASE}/downloadProduct",
                                     json={"productType": product,
                                           "productDate": int(entry["time"])})
        download.raise_for_status()
        url = (download.json() or {}).get("url")
        if not url:
            raise RuntimeError(f"no download url for {product}")

        # The presigned URL is short-lived and already authenticated; sending our
        # own headers to S3 would only risk breaking the signature.
        raw = await client.get(url, headers={})
        raw.raise_for_status()

        grid = await asyncio.to_thread(_decode, raw.content, stamp, product)
        buffered.append(grid)
        self._downloads += 1
        _LOGGER.info("[Radar] %s %s fetched (%d×%d, %.1f KB, published %.0f min late)",
                     product, time.strftime("%H:%M", time.localtime(stamp)),
                     grid.gt.cols, grid.gt.rows, len(raw.content) / 1024.0,
                     lag / 60.0)


radar_feed = RadarFeed()


__all__ = [
    "API_BASE", "API_HEADERS", "PRODUCT_RAIN", "PRODUCT_HAIL",
    "FRAME_HISTORY", "MAX_GRID_AGE_SEC", "DEFAULT_PERIOD_SEC",
    "RadarFeed", "radar_feed", "fetch_latest",
]
