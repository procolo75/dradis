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
  precipitation               Precipitation
  wind_speed_10m              Wind speed 10 m
  relative_humidity_2m        Relative humidity 2 m
  geopotential_height_500hPa  Geopotential 500 hPa
  temperature_850hPa          Temperature 850 hPa
"""

import io
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
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
        "label": "Precipitation",
        "unit":  "mm",
        "bar":   True,
    },
    "wind_speed_10m": {
        "label": "Wind 10m",
        "unit":  "km/h",
        "bar":   False,
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
        "bar":   True,
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
    },
    "uv_index": {
        "label": "UV Index",
        "unit":  "",
        "bar":   True,
    },
}

# High-contrast colors, hue-spaced, bright on dark background
_COLORS = ["#29b6f6", "#ff5252", "#69f0ae", "#ffd740", "#e040fb", "#ff6d00", "#40c4ff"]


# ── Geocoding ────────────────────────────────────────────────────────────────

async def _geocode(location: str) -> tuple[float, float, str]:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": location, "count": 1, "language": "en", "format": "json"},
        )
    results = resp.json().get("results", [])
    if not results:
        raise ValueError(f"Location not found: {location!r}")
    r = results[0]
    return r["latitude"], r["longitude"], r.get("name", location)


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
        "forecast_days": days,
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


# Variables always sent even if all values are zero (to convey "no rain expected")
_ALWAYS_SEND = {"precipitation", "precipitation_probability"}


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

    ylabel = f"{label} ({unit})" if unit else label
    ax.set_ylabel(ylabel, color="#9e9e9e", fontsize=10)

    handles, hlabels = ax.get_legend_handles_labels()
    if handles:
        ax.legend(handles, hlabels, loc="upper right", fontsize=9,
                  facecolor="#1e1e1e", edgecolor="#444",
                  labelcolor="#e1e1e1", framealpha=0.85)

    ax.xaxis.set_major_locator(mdates.HourLocator(byhour=[0, 6, 12, 18]))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m\n%H:%M"))
    plt.setp(ax.xaxis.get_majorticklabels(), rotation=0, ha="center", fontsize=9, color="#9e9e9e")

    # Midnight vertical lines
    first_times = next(iter(model_data.values()))[0]
    seen: set = set()
    for t in first_times:
        d = t.date()
        if d not in seen:
            seen.add(d)
            ax.axvline(t, color="#444", linewidth=0.8, linestyle="-")

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

    lat, lon, resolved = await _geocode(location)

    model_data: dict = {}
    for model_id in selected_models:
        try:
            data   = await _fetch_model(lat, lon, model_id, selected_vars, days)
            times  = _parse_times(data, tz)
            hourly = data.get("hourly", {})
            if times:
                model_data[model_id] = (times, hourly)
                print(f"[DRADIS] weather_chart: {model_id} OK ({len(times)} h)")
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
