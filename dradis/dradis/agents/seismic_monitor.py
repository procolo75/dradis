"""
agents/seismic_monitor.py
─────────────────────────
LLM-free scheduled monitor: fetches earthquake events from the INGV GOSSIP
JSON API and returns a formatted HTML report for the configured area and time
range.

Source: https://terremoti.ov.ingv.it/gossip/{area}/events.json
- `magnitudos` array is present only on Rivisto events (Automatico = no magnitude yet).
- Magnitude type "D" = Md (duration magnitude).
"""

import html
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

_BASE_URL = "https://terremoti.ov.ingv.it/gossip/{area}/events.json"

_AREA_LABELS = {
    "flegrei":   "Campi Flegrei",
    "vesuvio":   "Vesuvio",
    "ischia":    "Isola di Ischia",
    "regionale": "Golfo di Napoli",
}

_TIME_RANGE_LABELS = {
    "it": {
        "since_day_start":   "Da inizio giornata",
        "last_24h":          "Ultime 24 ore",
        "since_week_start":  "Da inizio settimana",
        "last_7d":           "Ultimi 7 giorni",
        "since_month_start": "Da inizio mese",
        "last_month":        "Ultimo mese",
        "since_year_start":  "Da inizio anno",
        "last_year":         "Ultimo anno",
    },
    "en": {
        "since_day_start":   "Since start of day",
        "last_24h":          "Last 24 hours",
        "since_week_start":  "Since start of week",
        "last_7d":           "Last 7 days",
        "since_month_start": "Since start of month",
        "last_month":        "Last month",
        "since_year_start":  "Since start of year",
        "last_year":         "Last year",
    },
}

_MAG_BINS = [
    ("nd",  None,  None, "⚪", "n.d."),
    ("neg", None,  0.0,  "⚪", "< 0"),
    ("0",   0.0,   1.0,  "⚪", "0 – 0.99"),
    ("1",   1.0,   2.0,  "🟡", "1 – 1.99"),
    ("2",   2.0,   3.0,  "🟠", "2 – 2.99"),
    ("3",   3.0,   4.0,  "🔴", "3 – 3.99"),
    ("4p",  4.0,   None, "🔴", "4+"),
]

_DEPTH_BINS = [
    (None, 1.0,   "0 – 1 km"),
    (1.0,  2.0,   "1 – 2 km"),
    (2.0,  5.0,   "2 – 5 km"),
    (5.0,  10.0,  "5 – 10 km"),
    (10.0, None,  "10+ km"),
]


def _extract_magnitude(event: dict) -> float | None:
    mags = event.get("magnitudos")
    if not mags or not isinstance(mags, list):
        return None
    # Prefer type "D" (Md); fall back to first entry
    preferred = next((m for m in mags if m.get("type") == "D"), mags[0])
    val = preferred.get("value")
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _range_start(time_range: str, tz: ZoneInfo) -> datetime:
    now = datetime.now(tz)
    if time_range == "last_24h":
        return now - timedelta(hours=24)
    if time_range == "last_7d":
        return now - timedelta(days=7)
    if time_range == "last_month":
        return now - timedelta(days=30)
    if time_range == "last_year":
        return now - timedelta(days=365)
    if time_range == "since_year_start":
        return now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    if time_range == "since_month_start":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if time_range == "since_week_start":
        return (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    if time_range == "since_day_start":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    return now - timedelta(hours=24)


async def run_seismic_monitor(monitor: dict, tz_name: str = "UTC") -> str:
    area       = monitor.get("seismic_area", "flegrei")
    time_range = monitor.get("time_range", "last_24h")
    lang       = monitor.get("language", "it")

    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")

    url = _BASE_URL.format(area=area)
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        events = resp.json()

    if not isinstance(events, list):
        raise ValueError(f"Unexpected response format from INGV API: {type(events)}")

    cutoff       = _range_start(time_range, tz)
    cutoff_epoch = cutoff.timestamp()

    filtered = [e for e in events if e.get("epoch", 0) >= cutoff_epoch]
    filtered.sort(key=lambda e: e.get("epoch", 0), reverse=True)

    # Attach parsed magnitude to each event (avoids double-parsing later)
    for e in filtered:
        e["_mag"] = _extract_magnitude(e)

    area_label  = html.escape(_AREA_LABELS.get(area, area))
    range_label = _TIME_RANGE_LABELS.get(lang, _TIME_RANGE_LABELS["it"]).get(time_range, time_range)
    now_str     = datetime.now(tz).strftime("%d/%m/%Y %H:%M")

    it = lang != "en"

    if it:
        title       = f"🌍 <b>Report sismico — {area_label}</b>"
        subtitle    = f"Periodo: {range_label} | {now_str}"
        no_event    = "Nessun evento sismico nel periodo selezionato."
        footer      = "<i>Monitor DRADIS · INGV GOSSIP · nessun LLM utilizzato</i>"
        s_total     = lambda n: f"📊 <b>{n} event{'i' if n != 1 else 'o'}</b>"
        s_split     = lambda a, r: f" — Automatici: {a} · Rivisti: {r}"
        hdr_mag     = "📈 <b>Magnitudo (Md)</b>"
        hdr_depth   = "📐 <b>Profondità</b>"
        lbl_nd      = "n.d."
    else:
        title       = f"🌍 <b>Seismic report — {area_label}</b>"
        subtitle    = f"Period: {range_label} | {now_str}"
        no_event    = "No seismic events in the selected period."
        footer      = "<i>DRADIS Monitor · INGV GOSSIP · no LLM used</i>"
        s_total     = lambda n: f"📊 <b>{n} event{'s' if n != 1 else ''}</b>"
        s_split     = lambda a, r: f" — Automatic: {a} · Revised: {r}"
        hdr_mag     = "📈 <b>Magnitude (Md)</b>"
        hdr_depth   = "📐 <b>Depth</b>"
        lbl_nd      = "n.d."

    lines = [title, subtitle, ""]

    if not filtered:
        lines += [no_event, "", footer]
        return "\n".join(lines)

    total      = len(filtered)
    auto_count = sum(1 for e in filtered if (e.get("class") or "").lower() == "automatico")
    rev_count  = sum(1 for e in filtered if (e.get("class") or "").lower() == "rivisto")

    lines.append(s_total(total) + s_split(auto_count, rev_count))
    lines.append("")

    # ── Magnitude distribution ────────────────────────────────────────────────
    lines.append(hdr_mag)
    for key, lo, hi, icon, label in _MAG_BINS:
        if key == "nd":
            count = sum(1 for e in filtered if e["_mag"] is None)
        elif key == "neg":
            count = sum(1 for e in filtered if e["_mag"] is not None and e["_mag"] < 0)
        elif hi is None:
            count = sum(1 for e in filtered if e["_mag"] is not None and e["_mag"] >= lo)
        else:
            count = sum(1 for e in filtered if e["_mag"] is not None and lo <= e["_mag"] < hi)
        if count:
            lines.append(f"  {icon} {label}: <b>{count}</b>")

    lines.append("")

    # ── Depth distribution ────────────────────────────────────────────────────
    lines.append(hdr_depth)
    depth_nd = 0
    for lo, hi, label in _DEPTH_BINS:
        count = 0
        for e in filtered:
            d = (e.get("location") or {}).get("depth")
            if d is None:
                continue
            if lo is None and d < hi:
                count += 1
            elif hi is None and d >= lo:
                count += 1
            elif lo is not None and hi is not None and lo <= d < hi:
                count += 1
        if count:
            lines.append(f"  🔵 {label}: <b>{count}</b>")
    depth_nd = sum(
        1 for e in filtered
        if (e.get("location") or {}).get("depth") is None
    )
    if depth_nd:
        lines.append(f"  ⚪ {lbl_nd}: <b>{depth_nd}</b>")

    lines += ["", footer]
    return "\n".join(lines)
