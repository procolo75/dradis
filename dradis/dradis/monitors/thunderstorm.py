"""
monitors/thunderstorm.py
────────────────────────
LLM-free monitor: fetches CAPE, LI and CIN from Open-Meteo for a given location
and computes a Thunderstorm Risk Score (TRS) in pure Python.

Risk formula — multiplicative composite (TRS ∈ [0.0, 1.0]):
  TRS = CAPE_norm × LI_norm × CIN_norm

  CAPE_norm = min(CAPE / 1200, 1.0)            # Mediterranean: 800 J/kg ≈ 67%
  LI_norm   = min(max(-LI / 5.0, 0.0), 1.0)   # LI -3°C = 60%; saturates at -5°C
  CIN_norm  = max(1.0 - |CIN| / 100.0, 0.0)   # CIN 0 → 1.0, CIN 100 → 0.0

Risk levels:
  0.0 – 0.2  : 🟢 TRASCURABILE / NEGLIGIBLE
  0.2 – 0.4  : 🟡 BASSO / LOW
  0.4 – 0.6  : 🟡 MODERATO / MODERATE
  0.6 – 0.8  : 🟠 ELEVATO / HIGH
  0.8 – 1.0  : 🔴 MOLTO ELEVATO / VERY HIGH
"""

import html
import statistics
from datetime import datetime, date
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

# Default calibration constants (Mediterranean). Per-monitor overrides come from the saved config.
_CAPE_SAT_DEFAULT = 1200.0
_LI_SAT_DEFAULT   =    5.0
_CIN_SUPP_DEFAULT =  100.0

_BANDS = [
    ("00–06", range(0, 6)),
    ("06–12", range(6, 12)),
    ("12–18", range(12, 18)),
    ("18–24", range(18, 24)),
]

_RISK_EMOJI = {
    "it": [
        (0.8, "🔴 MOLTO ELEVATO"),
        (0.6, "🟠 ELEVATO"),
        (0.4, "🟡 MODERATO"),
        (0.2, "🟡 BASSO"),
        (0.0, "🟢 TRASCURABILE"),
    ],
    "en": [
        (0.8, "🔴 VERY HIGH"),
        (0.6, "🟠 HIGH"),
        (0.4, "🟡 MODERATE"),
        (0.2, "🟡 LOW"),
        (0.0, "🟢 NEGLIGIBLE"),
    ],
}

_STRINGS = {
    "it": {
        "title":      "⛈️ <b>Monitor Temporali — {name}</b>",
        "forecast":   "Previsione {days} giorn{suffix}",
        "day_suffix": lambda d: "o" if d == 1 else "i",
        "daily_max":  "➤ Rischio max: {label}  ({trs:.2f})",
        "footer":     "<i>Monitor DRADIS · Open-Meteo · nessun LLM utilizzato</i>",
    },
    "en": {
        "title":      "⛈️ <b>Thunderstorm Monitor — {name}</b>",
        "forecast":   "Forecast {days} day{suffix}",
        "day_suffix": lambda d: "" if d == 1 else "s",
        "daily_max":  "➤ Peak risk: {label}  ({trs:.2f})",
        "footer":     "<i>DRADIS Monitor · Open-Meteo · no LLM used</i>",
    },
}


def _risk_label(trs: float, lang: str = "it") -> str:
    for threshold, label in _RISK_EMOJI.get(lang, _RISK_EMOJI["it"]):
        if trs >= threshold:
            return label
    return _RISK_EMOJI.get(lang, _RISK_EMOJI["it"])[-1][1]


def _norm_cape(cape: float, sat: float) -> float:
    return min(max(cape, 0.0) / sat, 1.0)


def _norm_li(li: float, sat: float) -> float:
    return min(max(-li / sat, 0.0), 1.0)


def _norm_cin(cin: float, supp: float) -> float:
    return max(1.0 - abs(cin) / supp, 0.0)


def _compute_trs(cape, cin, li, cape_sat: float, li_sat: float, cin_supp: float) -> float:
    if cape is None or li is None:
        return 0.0
    return round(
        _norm_cape(cape, cape_sat) * _norm_li(li, li_sat) * _norm_cin(cin or 0.0, cin_supp),
        3,
    )


def _band_mean(hourly: dict, field: str, hour_range: range, base: int) -> float | None:
    vals = hourly.get(field, [])
    bucket = [
        vals[base + h]
        for h in hour_range
        if base + h < len(vals) and vals[base + h] is not None
    ]
    return round(statistics.mean(bucket), 2) if bucket else None


async def _geocode(location: str) -> tuple[float, float, str]:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": location, "count": 1, "language": "it", "format": "json"},
        )
    results = resp.json().get("results", [])
    if not results:
        raise ValueError(f"Location not found: {location!r}")
    r = results[0]
    return r["latitude"], r["longitude"], r.get("name", location)


async def _fetch_instability(lat: float, lon: float, days: int) -> dict:
    params = {
        "latitude":      lat,
        "longitude":     lon,
        "hourly":        "cape,convective_inhibition,lifted_index",
        "timezone":      "auto",
        "forecast_days": days,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get("https://api.open-meteo.com/v1/forecast", params=params)
    resp.raise_for_status()
    return resp.json()


def _format_report(
    location_name: str,
    lat: float,
    lon: float,
    days: int,
    hourly: dict,
    tz_name: str,
    lang: str = "it",
    cape_sat: float = _CAPE_SAT_DEFAULT,
    li_sat: float = _LI_SAT_DEFAULT,
    cin_supp: float = _CIN_SUPP_DEFAULT,
) -> str:
    s = _STRINGS.get(lang, _STRINGS["it"])
    bands = _BANDS

    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")

    times = hourly.get("time", [])
    lines: list[str] = []

    forecast_str = s["forecast"].format(days=days, suffix=s["day_suffix"](days))
    lines.append(s["title"].format(name=html.escape(location_name)))
    lines.append(f"📍 {lat:.4f}, {lon:.4f} | {forecast_str}")
    lines.append(f"🕐 {datetime.now(tz).strftime('%d/%m/%Y %H:%M')} ({tz_name})")
    lines.append("")

    for day_idx in range(days):
        base = day_idx * 24
        if base >= len(times):
            break
        day_str = times[base][:10]
        try:
            day_label = date.fromisoformat(day_str).strftime("%-d %B %Y")
        except Exception:
            day_label = day_str

        lines.append(f"📅 <b>{day_label}</b>")
        day_trs: list[float] = []

        for band_label, hour_range in bands:
            cape = _band_mean(hourly, "cape",                  hour_range, base)
            li   = _band_mean(hourly, "lifted_index",          hour_range, base)
            cin  = _band_mean(hourly, "convective_inhibition", hour_range, base)

            if cape is None and li is None:
                continue

            trs = _compute_trs(cape, cin, li, cape_sat, li_sat, cin_supp)
            day_trs.append(trs)
            label = _risk_label(trs, lang)
            lines.append(f"  <code>{band_label}</code>  {label}  <b>{trs:.2f}</b>")

        if day_trs:
            peak = max(day_trs)
            lines.append(s["daily_max"].format(label=_risk_label(peak, lang), trs=peak))
        lines.append("")

    lines.append(s["footer"])
    return "\n".join(lines)


async def run_thunderstorm_monitor(monitor: dict, tz_name: str = "UTC") -> str:
    location = monitor.get("location", "").strip()
    if not location:
        raise ValueError("Monitor: 'location' field is required")
    days     = max(1, min(int(monitor.get("days", 2)), 7))
    lang     = monitor.get("language", "it")
    cape_sat = float(monitor.get("cape_sat", _CAPE_SAT_DEFAULT))
    li_sat   = float(monitor.get("li_sat",   _LI_SAT_DEFAULT))
    cin_supp = float(monitor.get("cin_supp", _CIN_SUPP_DEFAULT))
    lat, lon, resolved = await _geocode(location)
    data = await _fetch_instability(lat, lon, days)
    hourly = data.get("hourly", {})
    return _format_report(resolved, lat, lon, days, hourly, tz_name, lang, cape_sat, li_sat, cin_supp)
