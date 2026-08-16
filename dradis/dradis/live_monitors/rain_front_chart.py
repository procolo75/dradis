"""
live_monitors/rain_front_chart.py
──────────────────────────────────
The radar picture attached to every ring message.

Where the storm front draws a polar scatter of discharges — a photograph of the
MODEL, because lightning has no image of its own — this draws the measurement
itself: the national composite, cropped to the observation disc, with the ring
ladder and the encounter geometry laid over it. Same palette as
`storm_front_chart` on purpose, so the two monitors read as one product.

Three things are deliberate.

  · The crop is centred on the ADVECTED origin, the same one the numbers in the
    message were computed against. Drawing the raster around the user's true
    position instead would put the front a few kilometres from where the text
    says it is, and the user would believe the picture.
  · The title carries the RADAR's timestamp, never the send time. The product is
    published about ten minutes late, and a picture that implies "now" is a
    promise the source cannot keep.
  · The projection is cartesian, not polar, because underneath there is an image.
    Rings become circles; nothing else changes.

The same two implementation rules as the storm front's chart apply and are not
negotiable: matplotlib through the OBJECT API only (pyplot keeps thread-unsafe
global state and this renders on a worker thread), and every failure degrades to
a text-only alert — a picture must never be able to stop a warning.
"""

import io
import math
from datetime import datetime

from .geo import direction_label
from .radar_core import NODATA_THRESHOLD, latlon_to_pixel
from .storm_front_chart import (
    _BG, _FRONT, _GRID, _HOME, _RADIUS, _RING_TEXT, _TITLE, _WEDGE,
)

# Intensity ladder in mm/h and its colours. Blue through green to red and violet
# is the convention every weather service uses; departing from it would make the
# picture harder to read for no gain.
RAIN_BOUNDS = [0.1, 0.5, 1, 2, 4, 8, 16, 32, 64, 200]
RAIN_COLORS = ["#2b5a8a", "#3d8ec9", "#4fc3f7", "#54d17a", "#c8d94a",
               "#f2c14b", "#ef7f3c", "#e0453c", "#b02f8a"]

# Where the radar network cannot see. Distinct from "no rain", which is _BG:
# a blind spot must not look like a clear sky.
_NO_COVERAGE = "#1b2230"
_MOTION = "#7fe3c4"
_MISS = "#ffc857"


def render_rain_radar(grid, origin, now, alert, *, radius_km, observe_radius_km,
                      edges, motion=None, encounter=None, location="",
                      lang="it", tz=None) -> bytes:
    """Render the situation as a PNG. Raises on failure — the caller decides."""
    import matplotlib
    matplotlib.use("Agg")
    import numpy as np
    from matplotlib.colors import BoundaryNorm, ListedColormap
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg

    gt = grid.gt
    col_o, row_o = latlon_to_pixel(gt, origin[0], origin[1])
    col_o, row_o = float(col_o), float(row_o)
    half = observe_radius_km * 1000.0 / gt.pixel_m

    c0 = max(0, int(math.floor(col_o - half)))
    c1 = min(gt.cols, int(math.ceil(col_o + half)) + 1)
    r0 = max(0, int(math.floor(row_o - half)))
    r1 = min(gt.rows, int(math.ceil(row_o + half)) + 1)
    window = grid.data[r0:r1, c0:c1]

    # Pixels are 1 km, so a pixel offset IS a kilometre offset. East is +x, north
    # is +y — hence the row axis flips.
    extent = (c0 - col_o, c1 - col_o, row_o - r1, row_o - r0)

    fig = Figure(figsize=(8, 8), dpi=100)
    FigureCanvasAgg(fig)
    fig.patch.set_facecolor(_BG)
    ax = fig.add_subplot()
    ax.set_facecolor(_BG)

    # The raster arrives as a square; the model only ever looked at a disc, so the
    # picture is clipped to one. Spelled out as an explicit Path plus transform
    # rather than handed a Circle patch: the patch form leaves the radius to be
    # resolved through the patch's own transform and silently declines to clip.
    from matplotlib.path import Path
    disc = Path([(observe_radius_km * math.sin(i * math.pi / 90),
                  observe_radius_km * math.cos(i * math.pi / 90))
                 for i in range(181)])

    clipped = []
    # The dominant sector sits UNDER the data: it is context, and a tint laid over
    # the measurement would change the colour the user reads an intensity from.
    if alert is not None:
        clipped.append(ax.add_patch(_wedge(alert.bearing_deg, observe_radius_km)))

    blind = np.ma.masked_where(window > NODATA_THRESHOLD, np.zeros_like(window))
    clipped.append(ax.imshow(blind, cmap=ListedColormap([_NO_COVERAGE]),
                             extent=extent, origin="upper",
                             interpolation="nearest", zorder=1))

    rain = np.ma.masked_where(
        (window <= NODATA_THRESHOLD) | (window < RAIN_BOUNDS[0]), window)
    clipped.append(ax.imshow(rain, cmap=ListedColormap(RAIN_COLORS),
                             norm=BoundaryNorm(RAIN_BOUNDS, len(RAIN_COLORS)),
                             extent=extent, origin="upper",
                             interpolation="nearest", zorder=2))

    for layer in clipped:
        layer.set_clip_path(disc, ax.transData)

    theta = [i * math.pi / 90 for i in range(181)]
    for i, edge in enumerate(edges):
        ax.plot([edge * math.sin(a) for a in theta],
                [edge * math.cos(a) for a in theta],
                color=_RADIUS if i == 0 else _GRID,
                linewidth=2.0 if i == 0 else 1.0,
                linestyle="-" if i == 0 else (0, (4, 4)),
                alpha=0.95, zorder=4)

    for angle in range(0, 360, 30):
        rad = math.radians(angle)
        ax.plot([0, observe_radius_km * math.sin(rad)],
                [0, observe_radius_km * math.cos(rad)],
                color=_GRID, linewidth=0.6, alpha=0.5, zorder=3)

    for label, angle in (("N", 0), ("E", 90), ("S", 180), ("W", 270)):
        text = {"W": "O"}.get(label, label) if lang == "it" else label
        rad = math.radians(angle)
        ax.text(observe_radius_km * 1.06 * math.sin(rad),
                observe_radius_km * 1.06 * math.cos(rad), text,
                color=_RING_TEXT, ha="center", va="center", fontsize=13)

    # Distance labels go opposite the front, where nothing is happening — offset
    # far enough not to collide with whichever cardinal label sits there.
    label_rad = math.radians(((alert.bearing_deg + 155.0) % 360.0)
                             if alert is not None else 135.0)
    for edge in list(edges) + [observe_radius_km]:
        ax.text(edge * math.sin(label_rad), edge * math.cos(label_rad),
                f"{edge:.0f}", color=_RING_TEXT, fontsize=10,
                ha="center", va="bottom", zorder=7,
                bbox=dict(facecolor=_BG, edgecolor="none", pad=1.0, alpha=0.75))

    # Where the rain is going, measured. Absent when it was not measurable, which
    # is the honest state and must not be drawn as a zero-length arrow.
    if motion is not None and motion.speed_kmh >= 1.0:
        rad = math.radians(motion.bearing_deg)
        length = observe_radius_km * 0.28
        tip = (length * math.sin(rad), length * math.cos(rad))
        ax.annotate("", xy=tip, xytext=(0, 0), zorder=8,
                    arrowprops=dict(arrowstyle="-|>", color=_MOTION,
                                    linewidth=2.0, alpha=0.9))
        # Offset the label PERPENDICULAR to the arrow: along it, the text lands on
        # the arrowhead and neither can be read.
        perp = rad + math.pi / 2
        gap = observe_radius_km * 0.08
        ax.text(tip[0] + gap * math.sin(perp), tip[1] + gap * math.cos(perp),
                f"{motion.speed_kmh:.0f} km/h", color=_MOTION, fontsize=9,
                ha="center", va="center", zorder=8)

    # The encounter the message announces, drawn where it will happen.
    if encounter is not None and encounter.approaching:
        rad = math.radians(encounter.miss_bearing_deg)
        spot = (encounter.miss_km * math.sin(rad), encounter.miss_km * math.cos(rad))
        # A miss this small IS a hit, and the marker would simply sit on top of the
        # home dot. Label the time under the centre instead of stacking symbols.
        direct = encounter.miss_km < 1.5
        if not direct:
            ax.plot([spot[0]], [spot[1]], marker="x", ms=13, mew=2.5,
                    color=_MISS, zorder=9)
        ax.text(spot[0] if not direct else 0.0,
                (spot[1] if not direct else 0.0) - observe_radius_km * 0.07,
                f"{encounter.minutes:.0f} min", color=_MISS, fontsize=9,
                ha="center", va="top", zorder=9,
                bbox=dict(facecolor=_BG, edgecolor="none", pad=1.0, alpha=0.7))

    if alert is not None:
        rad = math.radians(alert.bearing_deg)
        ax.plot([alert.front_km * math.sin(rad)], [alert.front_km * math.cos(rad)],
                marker="v", ms=14, color=_FRONT, mec=_BG, mew=1.5, zorder=10)

    ax.plot([0], [0], marker="o", ms=10, color=_HOME, mec=_BG, mew=1.5, zorder=11)

    ax.set_xlim(-observe_radius_km * 1.12, observe_radius_km * 1.12)
    ax.set_ylim(-observe_radius_km * 1.12, observe_radius_km * 1.12)
    ax.set_aspect("equal")
    ax.axis("off")

    ax.set_title(_title(alert, grid, location, lang, tz),
                 color=_TITLE, fontsize=14, pad=18)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=_BG, bbox_inches="tight")
    fig.clf()
    return buf.getvalue()


def _wedge(bearing_deg: float, radius_km: float):
    from matplotlib.patches import Wedge
    # Matplotlib measures anticlockwise from east; the model measures clockwise
    # from north. 90 - bearing converts between the two.
    centre = 90.0 - bearing_deg
    return Wedge((0, 0), radius_km, centre - 15.0, centre + 15.0,
                 facecolor=_WEDGE, alpha=0.16, edgecolor="none", zorder=0)


def _title(alert, grid, location: str, lang: str, tz) -> str:
    clock = datetime.fromtimestamp(grid.t, tz).strftime("%H:%M")
    if alert is None:
        return f"{location} · radar {clock}"
    heading = direction_label(alert.bearing_deg, lang)
    if lang == "it":
        return (f"{location} · radar {clock}\n"
                f"Fronte a {alert.front_km:.0f} km a {heading} · "
                f"anello {alert.ring}/{alert.ring_count}")
    return (f"{location} · radar {clock}\n"
            f"Front {alert.front_km:.0f} km to {heading} · "
            f"ring {alert.ring}/{alert.ring_count}")


__all__ = ["render_rain_radar", "RAIN_BOUNDS", "RAIN_COLORS"]
