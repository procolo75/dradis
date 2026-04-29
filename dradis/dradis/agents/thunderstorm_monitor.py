"""
agents/thunderstorm_monitor.py
───────────────────────────────
LLM-free monitor: fetches atmospheric instability data from Open-Meteo for a
given location and computes an hourly thunderstorm risk factor in pure Python.
Result is sent to Telegram as a formatted text message.

Risk formula (each band 0-10):
  score = (
      0.35 * norm_cape      # CAPE normalised to [0,10]  (max ref: 3000 J/kg)
    + 0.30 * norm_li        # Lifted Index: maps [-8,+4] -> [10,0]
    + 0.15 * norm_precip    # Precipitation probability [0,100] -> [0,10]
    + 0.10 * norm_gusts     # Wind gusts [0,100 km/h] -> [0,10]
    + 0.10 * norm_cin       # CIN inverted [0,200] -> [10,0]  (high CIN suppresses)
  )

Risk levels:
  0.0 - 2.5 : GREEN  LOW
  2.5 - 5.0 : YELLOW MODERATE
  5.0 - 7.5 : ORANGE HIGH
  7.5 - 10  : RED    SEVERE
"""

import html
import statistics
from datetime import datetime, date
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

# ── constants ──────────────────────────────────────────────────────────────────

_CAPE_MAX   = 3000.0
_LI_MIN     = -8.0
_LI_MAX     =  4.0
_CIN_MAX    = 200.0
_GUSTS_MAX  = 100.0

_BANDS = {
    "it": [
        ("NOTTE      00-06", range(0, 6)),
        ("MATTINA    06-12", range(6, 12)),
        ("POMERIGGIO 12-18", range(12, 18)),
        ("SERA       18-24", range(18, 24)),
    ],
    "en": [
        ("NIGHT      00-06", range(0, 6)),
        ("MORNING    06-12", range(6, 12)),
        ("AFTERNOON  12-18", range(12, 18)),
        ("EVENING    18-24", range(18, 24)),
    ],
}

_RISK_EMOJI = {
    "it": [
        (7.5, "🔴 SEVERO"),
        (5.0, "🟠 ALTO"),
        (2.5, "🟡 MODERATO"),
        (0.0, "🟢 BASSO"),
    ],
    "en": [
        (7.5, "🔴 SEVERE"),
        (5.0, "🟠 HIGH"),
        (2.5, "🟡 MODERATE"),
        (0.0, "🟢 LOW"),
    ],
}

_STRINGS = {
    "it": {
        "title":       "⛈️ <b>Monitor Temporali — {name}</b>",
        "forecast":    "Previsione {days} giorn{suffix}",
        "day_suffix":  lambda d: "o" if d == 1 else "i",
        "gusts":       "Raffiche {v} km/h",
        "precip":      "Precip {v}%",
        "daily_max":   "➤ Rischio massimo giornaliero: {label} ({score}/10)",
        "footer":      "<i>Monitor DRADIS · Open-Meteo · nessun LLM utilizzato</i>",
    },
    "en": {
        "title":       "⛈️ <b>Thunderstorm Monitor — {name}</b>",
        "forecast":    "Forecast {days} day{suffix}",
        "day_suffix":  lambda d: "" if d == 1 else "s",
        "gusts":       "Gusts {v} km/h",
        "precip":      "Precip {v}%",
        "daily_max":   "➤ Peak daily risk: {label} ({score}/10)",
        "footer":      "<i>DRADIS Monitor · Open-Meteo · no LLM used</i>",
    },
}


def _risk_label(score: float, lang: str = "it") -> str:
    for threshold, label in _RISK_EMOJI.get(lang, _RISK_EMOJI["it"]):
        if score >= threshold:
            return label
    return _RISK_EMOJI.get(lang, _RISK_EMOJI["it"])[-1][1]


def _norm(val, lo, hi) -> float:
    if hi == lo:
        return 0.0
    return max(0.0, min(10.0, (val - lo) / (hi - lo) * 10.0))


def _band_mean(hourly: dict, field: str, hour_range: range, base: int) -> float | None:
    vals = hourly.get(field, [])
    bucket = [
        vals[base + h]
        for h in hour_range
        if base + h < len(vals) and vals[base + h] is not None
    ]
    return round(statistics.mean(bucket), 2) if bucket else None


def _band_max(hourly: dict, field: str, hour_range: range, base: int) -> float | None:
    vals = hourly.get(field, [])
    bucket = [
        vals[base + h]
        for h in hour_range
        if base + h < len(vals) and vals[base + h] is not None
    ]
    return round(max(bucket), 2) if bucket else None


def _compute_risk(cape, li, cin, gusts, precip) -> float:
    n_cape   = _norm(cape   or 0, 0,       _CAPE_MAX)
    n_li     = _norm(li     or _LI_MAX, _LI_MAX, _LI_MIN)
    n_cin    = _norm(cin    or 0, 0,       _CIN_MAX)
    n_gusts  = _norm(gusts  or 0, 0,       _GUSTS_MAX)
    n_precip = _norm(precip or 0, 0,       100.0)
    n_cin_inv = 10.0 - n_cin
    score = (
        0.35 * n_cape
        + 0.30 * n_li
        + 0.15 * n_precip
        + 0.10 * n_gusts
        + 0.10 * n_cin_inv
    )
    return round(score, 1)


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
        "hourly":        (
            "cape,lifted_index,convective_inhibition,"
            "wind_gusts_10m,precipitation_probability,weather_code"
        ),
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
) -> str:
    s = _STRINGS.get(lang, _STRINGS["it"])
    bands = _BANDS.get(lang, _BANDS["it"])

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
        day_scores: list[float] = []

        for band_label, hour_range in bands:
            cape   = _band_mean(hourly, "cape",                      hour_range, base)
            li     = _band_mean(hourly, "lifted_index",              hour_range, base)
            cin    = _band_mean(hourly, "convective_inhibition",     hour_range, base)
            gusts  = _band_max( hourly, "wind_gusts_10m",            hour_range, base)
            precip = _band_mean(hourly, "precipitation_probability", hour_range, base)

            if all(v is None for v in [cape, li, cin, gusts, precip]):
                continue

            score = _compute_risk(cape, li, cin, gusts, precip)
            day_scores.append(score)
            label = _risk_label(score, lang)

            parts: list[str] = []
            if cape   is not None: parts.append(f"CAPE {int(cape)} J/kg")
            if li     is not None: parts.append(f"LI {li:+.1f}")
            if cin    is not None: parts.append(f"CIN {int(cin)} J/kg")
            if gusts  is not None: parts.append(s["gusts"].format(v=int(gusts)))
            if precip is not None: parts.append(s["precip"].format(v=int(precip)))

            lines.append(f"  <code>{band_label}</code>  {label}  <b>{score:.1f}/10</b>")
            lines.append(f"    <i>{' · '.join(parts)}</i>")

        if day_scores:
            day_max = max(day_scores)
            lines.append(s["daily_max"].format(label=_risk_label(day_max, lang), score=f"{day_max:.1f}"))
        lines.append("")

    lines.append(s["footer"])
    return "\n".join(lines)


async def run_thunderstorm_monitor(monitor: dict, tz_name: str = "UTC") -> str:
    location = monitor.get("location", "").strip()
    if not location:
        raise ValueError("Monitor: 'location' field is required")
    days = max(1, min(int(monitor.get("days", 2)), 7))
    lang = monitor.get("language", "it")
    lat, lon, resolved = await _geocode(location)
    data = await _fetch_instability(lat, lon, days)
    hourly = data.get("hourly", {})
    return _format_report(resolved, lat, lon, days, hourly, tz_name, lang)
