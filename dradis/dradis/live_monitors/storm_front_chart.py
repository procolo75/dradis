"""
live_monitors/storm_front_chart.py
───────────────────────────────────
Polar radar attached to every ring message.

The picture is not decoration: rings and sectors ARE the model, so a polar plot
is a literal photograph of what the monitor is thinking. The user sees where the
front is, which way the cell is drifting between messages, and whether anything
else is out there — the things a line of text cannot convey.

Two implementation notes that matter:

  · Matplotlib is used through the OBJECT API (Figure + FigureCanvasAgg), never
    through pyplot. Pyplot keeps global figure state that is not thread-safe, and
    this renders on a worker thread.
  · Rendering takes a few hundred milliseconds, which is why the monitor calls it
    via asyncio.to_thread. Doing it inline would block the event loop exactly as
    the old DBSCAN call used to.

Matplotlib is already a dependency of the add-on (see monitors/weather_chart.py),
so this costs nothing extra. It is still imported lazily and guarded: a chart
must never be able to stop an alert.
"""

import io
import math
from datetime import datetime

from .geo import azimuth_deg, direction_label, distance_km

# Dark palette — Telegram renders these on both light and dark chat backgrounds.
_BG        = "#11151c"
_GRID      = "#2c3542"
_RING_TEXT = "#7d8b9e"
_HOME      = "#4fc3f7"
_FRONT     = "#ff5252"
_WEDGE     = "#ff5252"
_TITLE     = "#e6edf5"
_RADIUS    = "#8fa3bd"

# Strike colours, oldest → newest. Even the oldest stays clearly visible against
# the background: a strike that faded to nothing would hide the storm's tail,
# which is exactly what shows the direction of travel.
_AGE_COLORS = ["#4a5480", "#6f5da5", "#9a63ad", "#c96c96", "#ef7f6b", "#ffc857"]


def _polar(strikes, origin, now, window_sec):
    """(theta_rad, radius_km, age_fraction) for each strike, newest last."""
    olat, olon = origin
    out = []
    for t, lat, lon in strikes:
        age = now - t
        if age < 0 or age > window_sec:
            continue
        d = distance_km(olat, olon, lat, lon)
        az = azimuth_deg(olat, olon, lat, lon)
        out.append((math.radians(az), d, 1.0 - age / window_sec))
    out.sort(key=lambda p: p[2])          # oldest first, so newest draw on top
    return out


def render_radar(strikes, origin, now, alert, *, radius_km, observe_radius_km,
                 edges, window_sec, location="", lang="it", tz=None) -> bytes:
    """Render the situation as a PNG. Raises on failure — the caller decides."""
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg

    fig = Figure(figsize=(8, 8), dpi=100)
    FigureCanvasAgg(fig)
    fig.patch.set_facecolor(_BG)
    ax = fig.add_subplot(projection="polar")
    ax.set_facecolor(_BG)

    # North up, clockwise — a compass, not a maths plot.
    ax.set_theta_zero_location("N")
    ax.set_theta_direction(-1)
    ax.set_ylim(0, observe_radius_km)

    # Sector spokes only. Matplotlib's own radial grid is switched off — its
    # automatic circles sit at arbitrary distances and read as extra rings.
    ax.set_thetagrids(range(0, 360, 30), labels=[""] * 12, color=_GRID)
    ax.set_rgrids(edges, labels=[""] * len(edges))
    ax.xaxis.grid(True, color=_GRID, linewidth=0.6, alpha=0.6)
    ax.yaxis.grid(False)
    ax.spines["polar"].set_color(_GRID)

    for label, angle in (("N", 0), ("E", 90), ("S", 180), ("W", 270)):
        text = {"W": "O"}.get(label, label) if lang == "it" else label
        ax.text(math.radians(angle), observe_radius_km * 1.07, text,
                color=_RING_TEXT, ha="center", va="center", fontsize=13)

    # Ring edges: the alert radius stands out, the inner ladder is dashed.
    theta = [i * math.pi / 90 for i in range(181)]
    for i, edge in enumerate(edges):
        ax.plot(theta, [edge] * len(theta),
                color=_RADIUS if i == 0 else _GRID,
                linewidth=2.0 if i == 0 else 1.0,
                linestyle="-" if i == 0 else (0, (4, 4)),
                alpha=0.95, zorder=2)

    # The dominant sector, highlighted.
    if alert is not None:
        ax.bar(math.radians(alert.bearing_deg), observe_radius_km,
               width=math.radians(30), bottom=0.0, color=_WEDGE,
               alpha=0.10, edgecolor="none", zorder=1)

    # Distance labels go opposite the front, where nothing is happening.
    label_angle = math.radians(((alert.bearing_deg + 180.0) % 360.0)
                               if alert is not None else 135.0)
    for edge in edges:
        ax.text(label_angle, edge, f"{edge:.0f}", color=_RING_TEXT, fontsize=10,
                ha="center", va="bottom", zorder=6,
                bbox=dict(facecolor=_BG, edgecolor="none", pad=1.0, alpha=0.75))
    ax.text(label_angle, observe_radius_km, f"{observe_radius_km:.0f} km",
            color=_RING_TEXT, fontsize=10, ha="center", va="bottom", zorder=6,
            bbox=dict(facecolor=_BG, edgecolor="none", pad=1.0, alpha=0.75))

    # Strikes, coloured and sized by age.
    points = _polar(strikes, origin, now, window_sec)
    if points:
        n = len(_AGE_COLORS)
        ax.scatter([p[0] for p in points], [p[1] for p in points],
                   c=[_AGE_COLORS[min(n - 1, int(p[2] * n))] for p in points],
                   s=[10 + 34 * p[2] ** 2 for p in points],
                   alpha=0.85, linewidths=0, zorder=4)

    # The front itself. Secondary sectors are deliberately NOT marked: the
    # strikes already show them, and extra triangles next to the dominant one
    # read as competing fronts when they are usually the same cell spilling
    # across a sector boundary.
    if alert is not None:
        ax.scatter([math.radians(alert.bearing_deg)], [alert.front_km],
                   marker="v", s=220, color=_FRONT, edgecolors=_BG,
                   linewidths=1.5, zorder=7)

    ax.scatter([0], [0], marker="o", s=110, color=_HOME,
               edgecolors=_BG, linewidths=1.5, zorder=8)

    clock = datetime.fromtimestamp(now, tz).strftime("%H:%M")
    if alert is not None:
        heading = direction_label(alert.bearing_deg, lang)
        if lang == "it":
            title = (f"{location} · {clock}\n"
                     f"Fronte a {alert.front_km:.0f} km a {heading} · "
                     f"anello {alert.ring}/{alert.ring_count}")
        else:
            title = (f"{location} · {clock}\n"
                     f"Front {alert.front_km:.0f} km to {heading} · "
                     f"ring {alert.ring}/{alert.ring_count}")
    else:
        title = f"{location} · {clock}"
    ax.set_title(title, color=_TITLE, fontsize=14, pad=22)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=_BG, bbox_inches="tight")
    fig.clf()
    return buf.getvalue()


__all__ = ["render_radar"]
