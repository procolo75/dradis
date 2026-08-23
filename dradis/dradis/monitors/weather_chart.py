"""
monitors/weather_chart.py
──────────────────────────
Generates one PNG chart per selected variable (multi-model overlay) and returns
them as a list[bytes]. No LLM used.

Supported models (weather_models config param):
  ecmwf_ifs04              ECMWF IFS HRES 9 km  (no uv_index)
  icon_eu                  DWD ICON EU 7 km      (no precip_prob, no uv_index)
  meteofrance_arpege_europe Météo-France ARPEGE  (no precip_prob, no uv_index)
  gfs025                   NOAA GFS global       (all variables)
  italia_meteo_arpae       ItaliaMeteo ARPAE 2i  (no precip_prob, no uv_index)

Supported variables (chart_variables config param):
  temperature_2m              Temperature 2 m
  precipitation               Precipitation (numeric lanes, 3 h totals)
  wind_speed_10m              Wind speed 10 m (numeric lanes)
  wind_gusts_10m              Wind gusts 10 m (numeric lanes, 3 h peaks)
  wind_direction_10m          Wind direction 10 m (arrow lanes)
  relative_humidity_2m        Relative humidity 2 m
  geopotential_height_500hPa  Geopotential 500 hPa
  temperature_850hPa          Temperature 850 hPa
  cloud_cover                 Cloud cover (numeric lanes)
  precipitation_probability   Precipitation probability (numeric lanes)

Three rendering styles, picked per variable in VARIABLES: a line plot by default, one
horizontal lane per model carrying either arrows ("compass") or printed values ("lanes"),
and bars ("bar") for what is left. Lanes exist because overlapping multi-model bars and
0-360 degree scatters were unreadable.
"""

import io
import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from geocode import geocode

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

# ── Model registry ────────────────────────────────────────────────────────────

MODELS = {
    "ecmwf_ifs04": {
        "label":   "ECMWF IFS 9km",
        "url":     "https://api.open-meteo.com/v1/forecast",
        "param":   "ecmwf_ifs025",
        "exclude": {"uv_index"},
    },
    "icon_eu": {
        "label":   "ICON EU 7km",
        "url":     "https://api.open-meteo.com/v1/forecast",
        "param":   "icon_eu",
        "exclude": {"precipitation_probability", "uv_index"},
    },
    "meteofrance_arpege_europe": {
        "label":   "MF ARPEGE Europe",
        "url":     "https://api.open-meteo.com/v1/forecast",
        "param":   "meteofrance_arpege_europe",
        "exclude": {"precipitation_probability", "uv_index"},
    },
    "gfs025": {
        "label":   "GFS Global",
        "url":     "https://api.open-meteo.com/v1/forecast",
        "param":   "gfs_global",
        "exclude": set(),
    },
    "italia_meteo_arpae": {
        "label":   "ItaliaMeteo ARPAE",
        "url":     "https://api.open-meteo.com/v1/forecast",
        "param":   "italia_meteo_arpae_icon_2i",
        "exclude": {"precipitation_probability", "uv_index"},
    },
}

# ── Variable registry ─────────────────────────────────────────────────────────

VARIABLES = {
    "temperature_2m": {
        "label": "Temperature 2m",
        "unit":  "°C",
        "bar":   False,
    },
    "precipitation": {
        "label":     "Precipitation",
        "unit":      "mm",
        "bar":       False,
        "lanes":     True,
        "decimals":  1,      # millimetre totals are fractional; rounding would zero them
        "aggregate": "sum",  # hourly rainfall accumulates: sample and two thirds vanish
    },
    "wind_speed_10m": {
        "label": "Wind 10m",
        "unit":  "km/h",
        "bar":   False,
        "lanes": True,
    },
    "wind_gusts_10m": {
        "label":     "Wind Gusts 10m",
        "unit":      "km/h",
        "bar":       False,
        "lanes":     True,
        "aggregate": "max",  # a gust chart exists to show the peak, not a spot reading
    },
    "wind_direction_10m": {
        "label":   "Wind Direction 10m",
        "unit":    "°",
        "bar":     False,
        "compass": True,   # rendered as per-model arrow lanes (see _plot_wind_arrows)
    },
    "relative_humidity_2m": {
        "label": "Humidity 2m",
        "unit":  "%",
        "bar":   False,
    },
    "geopotential_height_500hPa": {
        "label": "Geopotential 500 hPa",
        "unit":  "m",
        "bar":   False,
    },
    "temperature_850hPa": {
        "label": "Temperature 850 hPa",
        "unit":  "°C",
        "bar":   False,
    },
    "apparent_temperature": {
        "label": "Apparent Temperature",
        "unit":  "°C",
        "bar":   False,
    },
    "precipitation_probability": {
        "label": "Precip. Probability",
        "unit":  "%",
        "bar":   False,
        "lanes": True,
    },
    "pressure_msl": {
        "label": "Sea Level Pressure",
        "unit":  "hPa",
        "bar":   False,
    },
    "cloud_cover": {
        "label": "Cloud Cover",
        "unit":  "%",
        "bar":   False,
        "lanes": True,   # rendered as per-model numeric lanes (see _plot_value_lanes)
    },
    "uv_index": {
        "label": "UV Index",
        "unit":  "",
        "bar":   True,
    },
}

# High-contrast colors, hue-spaced, bright on dark background
_COLORS = ["#29b6f6", "#ff5252", "#69f0ae", "#ffd740", "#e040fb", "#ff6d00", "#40c4ff"]


# ── Fetch data for a single model ────────────────────────────────────────────

async def _fetch_model(
    lat: float,
    lon: float,
    model_id: str,
    variables: list[str],
    days: int,
) -> dict:
    cfg = MODELS[model_id]
    effective_vars = [v for v in variables if v not in cfg.get("exclude", set())]
    if not effective_vars:
        raise ValueError(f"no supported variables for model {model_id}")
    params: dict = {
        "latitude":      lat,
        "longitude":     lon,
        "hourly":        ",".join(effective_vars),
        "timezone":      "auto",
        # One day more than asked for: the API always starts at 00:00 of the current day,
        # and everything before the run time is clipped away (see _clip_window), so the
        # last requested day would otherwise come up short by the hours already elapsed.
        "forecast_days": min(days + 1, 16),
    }
    if cfg["param"]:
        params["models"] = cfg["param"]
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(cfg["url"], params=params)
    resp.raise_for_status()
    return resp.json()


# ── Timestamp parsing ────────────────────────────────────────────────────────

def _parse_times(data: dict, tz: ZoneInfo) -> list[datetime]:
    raw = data.get("hourly", {}).get("time", [])
    result = []
    for t in raw:
        try:
            result.append(datetime.fromisoformat(t).replace(tzinfo=tz))
        except Exception:
            pass
    return result


# ── Forecast window ──────────────────────────────────────────────────────────

def _window_start(now: datetime) -> datetime:
    """First hour worth plotting: the next whole hour, pushed up to the 0/3/6/9… grid.

    Open-Meteo always answers from 00:00 of the current day, so a monitor running at 10:16
    would otherwise spend a third of a 3-day chart on forecasts that have already happened.
    Anchoring to the 3-hour grid — rather than starting at 11:00 — keeps the labels of the
    lane charts, which are drawn every 3 hours, on round times that line up with the
    0/6/12/18 ticks and with the midnight and midday lines."""
    start = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return start + timedelta(hours=(-start.hour) % 3)


def _clip_window(times: list[datetime], hourly: dict,
                 start: datetime, end: datetime) -> tuple[list[datetime], dict]:
    """Cut times and every hourly series to [start, end) using the same indices.

    The renderers pair times[i] with series[i] by position, so the series have to be cut
    with exactly the mask the timestamps were cut with — clipping only the timestamps
    would slide every value onto the wrong hour."""
    keep = [i for i, t in enumerate(times) if start <= t < end]
    clipped = {
        name: [values[i] for i in keep if i < len(values)]
        for name, values in hourly.items()
        if name != "time"
    }
    return [times[i] for i in keep], clipped


# Variables always sent even if all values are zero. precipitation/probability
# convey "no rain expected"; cloud_cover must render even for a fully clear
# forecast so a user-selected chart never silently disappears.
_ALWAYS_SEND = {"precipitation", "precipitation_probability", "cloud_cover"}


def _set_time_limits(ax, model_ids: list, model_data: dict) -> None:
    """Pin the x-axis to the data, plus an hour of margin on each side.

    Neither `ax.text` nor `quiver` feeds the autoscaler, so a lane chart used to take its
    limits from the midnight lines drawn afterwards — which cut off every hour past the
    last midnight (eight of them on a 3-day chart starting at midday). The margin keeps
    the first and last label, centred on their timestamp, from being clipped in half."""
    span = [t for m in model_ids for t in model_data[m][0]]
    if not span:
        return
    hour = 1 / 24
    ax.set_xlim(mdates.date2num(min(span)) - hour, mdates.date2num(max(span)) + hour)


# ── Wind-direction arrows (compass) ──────────────────────────────────────────

def _plot_wind_arrows(ax, var_id: str, model_ids: list, model_data: dict) -> None:
    """Render wind direction as one horizontal lane per model. Every few hours a
    uniform-length arrow points downwind (the way the wind blows *toward*). This
    replaces the multi-model 0–360° scatter, which was an unreadable cloud of
    overlapping points once more than one model was plotted."""
    step = 3  # hours between arrows, to keep each lane uncluttered
    n = len(model_ids)
    for lane, model_id in enumerate(model_ids):
        times, hourly = model_data[model_id]
        raw = hourly.get(var_id, [])
        if not raw:
            continue
        xs, us, vs = [], [], []
        for i in range(0, min(len(times), len(raw)), step):
            d = raw[i]
            if d is None:
                continue
            rad = math.radians(d)
            # Meteorological bearing is the direction the wind blows *from*; point
            # the arrow downwind (θ+180). In the screen frame (angles="uv", N up /
            # E right) a "from" bearing θ is (sinθ, cosθ), so downwind is negated.
            us.append(-math.sin(rad))
            vs.append(-math.cos(rad))
            xs.append(mdates.date2num(times[i]))
        if not xs:
            continue
        color = _COLORS[lane % len(_COLORS)]
        ax.quiver(xs, [lane] * len(xs), us, vs,
                  color=color, angles="uv", pivot="mid",
                  scale=34, scale_units="width", width=0.0028,
                  headwidth=4, headlength=5, headaxislength=4.5)
    ax.xaxis_date()
    ax.set_yticks(range(n))
    ax.set_yticklabels([MODELS.get(m, {}).get("label", m) for m in model_ids])
    ax.set_ylim(-0.6, n - 0.4)
    ax.text(0.004, 1.015, "arrows point downwind", transform=ax.transAxes,
            fontsize=8, color="#9e9e9e", ha="left", va="bottom")
    _set_time_limits(ax, model_ids, model_data)


# ── Numeric value lanes ──────────────────────────────────────────────────────

def _plot_value_lanes(ax, var_id: str, model_ids: list, model_data: dict,
                      note: str, decimals: int = 0, aggregate: str | None = None) -> None:
    """Render a variable as one horizontal lane per model, printing the value every few
    hours instead of drawing bars or lines. Multi-model bars overlapped into an unreadable
    block; the numbers stay comparable model-by-model at a glance. The unit is carried by
    the chart title and the note, so the values are printed bare.

    Sampling one hour in three suits variables that describe a state (cloud cover, wind
    direction). It does not suit rainfall, which accumulates — two thirds of it would be
    skipped — nor gusts, whose point is the peak. Those pass aggregate="sum" / "max" and
    each label then covers the whole window."""
    step = 3  # hours between labels, same cadence as the wind lanes
    # Colour by position in the model selection, not by lane, so a model keeps the same
    # colour across every chart even when a lane below it is dropped.
    colors = {m: _COLORS[i % len(_COLORS)] for i, m in enumerate(model_ids)}
    # Models that do not carry this variable (see MODELS[...]["exclude"]) would leave a
    # labelled but empty lane, which reads as "no rain expected" rather than "no data".
    model_ids = [m for m in model_ids if model_data[m][1].get(var_id)]
    n = len(model_ids)
    # A 3-day lane holds ~24 labels with room to spare; a 7-day one holds ~56 and
    # the three-digit values start touching. Size the text to the longest lane.
    longest = max((len(model_data[m][0]) for m in model_ids), default=0)
    labels = longest // step
    fontsize = 13 if labels <= 28 else 10.5 if labels <= 42 else 8.5
    for lane, model_id in enumerate(model_ids):
        times, hourly = model_data[model_id]
        raw = hourly[var_id]
        color = colors[model_id]
        end = min(len(times), len(raw))
        for i in range(0, end, step):
            if aggregate:
                window = [float(x) for x in raw[i:min(i + step, end)] if x is not None]
                if not window:
                    continue
                v = sum(window) if aggregate == "sum" else max(window)
            elif raw[i] is None:
                continue
            else:
                v = float(raw[i])
            # A bare "0" reads faster than "0.0", and dimming it keeps a mostly-dry
            # rain lane from drowning its few real values in a row of zeros.
            text = "0" if v == 0 else f"{v:.{decimals}f}"
            ax.text(mdates.date2num(times[i]), lane, text,
                    color="#555555" if v == 0 else color,
                    fontsize=fontsize, ha="center", va="center", clip_on=True)
    ax.xaxis_date()
    ax.set_yticks(range(n))
    ax.set_yticklabels([MODELS.get(m, {}).get("label", m) for m in model_ids])
    ax.set_ylim(-0.6, n - 0.4)
    ax.text(0.004, 1.015, note, transform=ax.transAxes,
            fontsize=8, color="#9e9e9e", ha="left", va="bottom")
    _set_time_limits(ax, model_ids, model_data)


# ── Single-variable chart ────────────────────────────────────────────────────

def _generate_single_chart(
    location_name: str,
    var_id: str,
    model_data: dict,   # {model_id: (times: list[datetime], hourly: dict)}
    days: int,
) -> bytes:
    vinfo = VARIABLES.get(var_id, {"label": var_id, "unit": "", "bar": False})
    unit  = vinfo.get("unit", "")
    label = vinfo.get("label", var_id)

    fig, ax = plt.subplots(1, 1, figsize=(16, 5), facecolor="#111111")
    fig.patch.set_facecolor("#111111")
    ax.set_facecolor("#1e1e1e")
    ax.tick_params(colors="#9e9e9e", labelsize=10)
    for spine in ax.spines.values():
        spine.set_color("#333")
    ax.grid(True, color="#2a2a2a", linewidth=0.5, linestyle="--")

    model_ids = list(model_data.keys())

    # Both lane renderers label their y-ticks with model names, so they need
    # neither a y-axis label nor a legend.
    lane_mode = vinfo.get("compass") or vinfo.get("lanes")

    if vinfo.get("compass"):
        # Wind direction: per-model arrow lanes (unreadable as a multi-model scatter).
        _plot_wind_arrows(ax, var_id, model_ids, model_data)
    elif vinfo.get("lanes"):
        # Per-model numeric lanes: readable where overlapping bars or lines were not.
        aggregate = vinfo.get("aggregate")
        note = f"{label.lower()} in {unit}" if unit else label.lower()
        if aggregate:
            note += ", 3h total" if aggregate == "sum" else ", 3h peak"
        _plot_value_lanes(ax, var_id, model_ids, model_data, note,
                          vinfo.get("decimals", 0), aggregate)
    else:
        for mi, model_id in enumerate(model_ids):
            times, hourly = model_data[model_id]
            raw_vals = hourly.get(var_id, [])
            if not raw_vals or all(v is None for v in raw_vals):
                continue
            # for bar vars not in _ALWAYS_SEND, skip models with all-zero data
            if vinfo.get("bar") and var_id not in _ALWAYS_SEND:
                if not any(v is not None and v > 0 for v in raw_vals):
                    continue
            vals  = [float(v) if v is not None else float("nan") for v in raw_vals]
            color = _COLORS[mi % len(_COLORS)]
            mlabel = MODELS.get(model_id, {}).get("label", model_id)

            if vinfo.get("bar"):
                width = 1 / 24 * 0.8  # bar width = 1 hour in matplotlib date units
                ax.bar(times, vals, width=width, color=color, alpha=0.6, label=mlabel)
            else:
                ax.plot(times, vals, color=color, linewidth=2.2, label=mlabel, alpha=0.95)

        # Percentage variables are bounded 0–100; pin the axis so bars are not
        # auto-scaled to the data max (which would exaggerate low probabilities).
        if unit == "%":
            ax.set_ylim(0, 100)

    # Value charts get a y-axis label + model legend; the lane charts do not.
    if not lane_mode:
        ylabel = f"{label} ({unit})" if unit else label
        ax.set_ylabel(ylabel, color="#9e9e9e", fontsize=10)

        handles, hlabels = ax.get_legend_handles_labels()
        if handles:
            ax.legend(handles, hlabels, loc="upper right", fontsize=9,
                      facecolor="#1e1e1e", edgecolor="#444",
                      labelcolor="#e1e1e1", framealpha=0.85)

    # The timestamps are tz-aware and matplotlib converts those to UTC to place them.
    # Without the same tz on the locator and the formatter, the ticks are labelled in UTC
    # while the data sits at local time — the whole axis then reads one or two hours off.
    first_times = next(iter(model_data.values()))[0]
    tz = first_times[0].tzinfo if first_times else None

    ax.xaxis.set_major_locator(mdates.HourLocator(byhour=[0, 6, 12, 18], tz=tz))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m\n%H:%M", tz=tz))
    plt.setp(ax.xaxis.get_majorticklabels(), rotation=0, ha="center", fontsize=9, color="#9e9e9e")

    # Midnight and midday lines. Tested on the hour rather than on "first sample of a new
    # date": the series no longer starts at midnight, so that test would have drawn its
    # first line at whatever hour the monitor happened to run.
    for t in first_times:
        if t.hour == 0:
            ax.axvline(t, color="#666", linewidth=1.2, linestyle="-")
        elif t.hour == 12:
            ax.axvline(t, color="#444", linewidth=0.9, linestyle="-")

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    title = f"{label}{f' ({unit})' if unit else ''}  ·  {location_name}  ·  {days}d  ·  {now_str}"
    fig.suptitle(title, color="#e1e1e1", fontsize=11, fontweight="bold")
    plt.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf.read()


# ── Monitor entry point ───────────────────────────────────────────────────────

async def run_weather_chart_monitor(monitor: dict, tz_name: str = "UTC") -> list[bytes]:
    location = monitor.get("location", "").strip()
    if not location:
        raise ValueError("Monitor: 'location' field is required")

    selected_models = [m for m in (monitor.get("weather_models") or []) if m in MODELS]
    selected_vars   = [v for v in (monitor.get("chart_variables") or []) if v in VARIABLES]
    days            = max(1, min(int(monitor.get("days", 3)), 7))

    if not selected_models:
        selected_models = ["ecmwf_ifs04"]
    if not selected_vars:
        selected_vars = ["temperature_2m", "precipitation", "wind_speed_10m"]

    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")

    lat, lon, resolved = await geocode(location)

    # `days` counts forward from the run, not from midnight: one window shared by every
    # model and every variable, so all the charts of one run cover the same hours.
    start = _window_start(datetime.now(tz))
    end   = start + timedelta(days=days)

    model_data: dict = {}
    for model_id in selected_models:
        try:
            data   = await _fetch_model(lat, lon, model_id, selected_vars, days)
            times  = _parse_times(data, tz)
            hourly = data.get("hourly", {})
            times, hourly = _clip_window(times, hourly, start, end)
            if times:
                model_data[model_id] = (times, hourly)
                print(f"[DRADIS] weather_chart: {model_id} OK ({len(times)} h "
                      f"from {times[0]:%d/%m %H:%M})")
        except Exception as e:
            print(f"[DRADIS] weather_chart: {model_id} failed — {e}")

    if not model_data:
        raise RuntimeError("All weather models failed")

    # Determine which variables to send:
    # _ALWAYS_SEND vars: include if at least one model has the variable (even all-zero)
    # other bar vars: include only if at least one model has a value > 0
    # line vars: include if at least one model has any non-None value
    def _should_send(var_id: str) -> bool:
        is_bar = VARIABLES.get(var_id, {}).get("bar", False)
        for m in model_data:
            vals = model_data[m][1].get(var_id, [])
            if not vals:
                continue
            if var_id in _ALWAYS_SEND:
                return any(x is not None for x in vals)
            if is_bar:
                return any(x is not None and x > 0 for x in vals)
            return any(x is not None for x in vals)
        return False

    charts: list[bytes] = []
    for var_id in selected_vars:
        if _should_send(var_id):
            charts.append(_generate_single_chart(resolved, var_id, model_data, days))
            print(f"[DRADIS] weather_chart: chart generated for {var_id}")

    if not charts:
        raise RuntimeError("No chart could be generated")
    return charts
