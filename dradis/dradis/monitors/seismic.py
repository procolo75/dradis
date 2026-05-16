"""
monitors/seismic.py
───────────────────
LLM-free scheduled monitor: fetches earthquake events from the INGV GOSSIP
JSON API and returns a formatted HTML report for the configured area and time
range. Source: https://terremoti.ov.ingv.it/gossip/{area}/events.json
"""

import html
import math
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
    ("0",   0.0,   1.0,  "⚪", "0 – 0.9"),
    ("1",   1.0,   2.0,  "🟡", "1 – 1.9"),
    ("2",   2.0,   3.0,  "🟠", "2 – 2.9"),
    ("3",   3.0,   4.0,  "🔴", "3 – 3.9"),
    ("4p",  4.0,   None, "🔴", "4+"),
]

_DEPTH_CEIL    = 30
_EVENT_LIST_MAX = 80


def _mag_icon(mag: float | None) -> str:
    if mag is None:
        return "⚪"
    if mag >= 3.0:
        return "🔴"
    if mag >= 2.0:
        return "🟠"
    if mag >= 1.0:
        return "🟡"
    return "⚪"


def _extract_magnitude(event: dict) -> float | None:
    mags = event.get("magnitudos")
    if not mags or not isinstance(mags, list):
        return None
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


def _depth_section(filtered: list[dict], lbl_nd: str) -> list[str]:
    pairs: list[tuple[float, float | None]] = []
    nd_events: list[dict] = []
    for e in filtered:
        d = (e.get("location") or {}).get("depth")
        if d is None:
            nd_events.append(e)
        else:
            pairs.append((float(d), e["_mag"]))

    if not pairs and not nd_events:
        return []

    lines: list[str] = []

    if pairs:
        max_d = max(d for d, _ in pairs)
        n_bins = min(math.ceil(max_d), _DEPTH_CEIL)

        for lo in range(0, n_bins):
            hi = lo + 1
            in_bin = [(d, m) for d, m in pairs if lo <= d < hi]
            if not in_bin:
                continue
            max_mag = max((m for _, m in in_bin if m is not None), default=None)
            count   = len(in_bin)
            mag_str = f"Md {max_mag:.1f}" if max_mag is not None else lbl_nd
            lines.append(f"  🔵 {lo}–{hi} km: <b>{count}</b> ev · max {mag_str}")

        deep = [(d, m) for d, m in pairs if d >= _DEPTH_CEIL]
        if deep:
            max_mag = max((m for _, m in deep if m is not None), default=None)
            count   = len(deep)
            mag_str = f"Md {max_mag:.1f}" if max_mag is not None else lbl_nd
            lines.append(f"  🔵 {_DEPTH_CEIL}+ km: <b>{count}</b> ev · max {mag_str}")

    if nd_events:
        lines.append(f"  ⚪ {lbl_nd}: <b>{len(nd_events)}</b>")

    return lines


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

    for e in filtered:
        e["_mag"] = _extract_magnitude(e)

    area_label  = html.escape(_AREA_LABELS.get(area, area))
    range_label = _TIME_RANGE_LABELS.get(lang, _TIME_RANGE_LABELS["it"]).get(time_range, time_range)
    now_str     = datetime.now(tz).strftime("%d/%m/%Y %H:%M")

    it = lang != "en"

    if it:
        title     = f"🌍 <b>Report sismico — {area_label}</b>"
        subtitle  = f"Periodo: {range_label} | {now_str}"
        no_event  = "Nessun evento sismico nel periodo selezionato."
        footer    = "<i>Monitor DRADIS · INGV GOSSIP · nessun LLM utilizzato</i>"
        s_total   = lambda n: f"📊 <b>{n} event{'i' if n != 1 else 'o'}</b>"
        s_split   = lambda a, r: f" — Automatici: {a} · Rivisti: {r}"
        hdr_mag   = "📈 <b>Magnitudo (Md)</b>"
        hdr_depth = "📐 <b>Profondità</b>"
        hdr_list  = "📋 <b>Lista eventi</b>"
        lbl_nd    = "n.d."
        lbl_more  = lambda n: f"  … e altri {n} eventi"
    else:
        title     = f"🌍 <b>Seismic report — {area_label}</b>"
        subtitle  = f"Period: {range_label} | {now_str}"
        no_event  = "No seismic events in the selected period."
        footer    = "<i>DRADIS Monitor · INGV GOSSIP · no LLM used</i>"
        s_total   = lambda n: f"📊 <b>{n} event{'s' if n != 1 else ''}</b>"
        s_split   = lambda a, r: f" — Automatic: {a} · Revised: {r}"
        hdr_mag   = "📈 <b>Magnitude (Md)</b>"
        hdr_depth = "📐 <b>Depth</b>"
        hdr_list  = "📋 <b>Event list</b>"
        lbl_nd    = "n.d."
        lbl_more  = lambda n: f"  … and {n} more events"

    lines = [title, subtitle, ""]

    if not filtered:
        lines += [no_event, "", footer]
        return "\n".join(lines)

    total      = len(filtered)
    auto_count = sum(1 for e in filtered if (e.get("class") or "").lower() == "automatico")
    rev_count  = sum(1 for e in filtered if (e.get("class") or "").lower() == "rivisto")

    lines.append(s_total(total) + s_split(auto_count, rev_count))
    lines.append("")

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

    lines.append(hdr_depth)
    depth_lines = _depth_section(filtered, lbl_nd)
    lines.extend(depth_lines)

    lines.append("")

    lines.append(hdr_list)
    shown = filtered[:_EVENT_LIST_MAX]
    for e in shown:
        mag   = e["_mag"]
        icon  = _mag_icon(mag)
        mag_s = f"Md {mag:.1f}" if mag is not None else lbl_nd
        d     = (e.get("location") or {}).get("depth")
        dep_s = f"{d:.1f} km" if d is not None else lbl_nd
        epoch = e.get("epoch")
        if epoch:
            dt_s = datetime.fromtimestamp(epoch, tz=tz).strftime("%d/%m %H:%M")
        else:
            dt_s = "??/??"
        cls       = (e.get("class") or "").lower()
        state_ico = "✅" if "rivisto" in cls else "⚠️"
        lines.append(f"  {icon} {dt_s}  {mag_s}  {dep_s}  {state_ico}")

    if total > _EVENT_LIST_MAX:
        lines.append(lbl_more(total - _EVENT_LIST_MAX))

    lines += ["", footer]
    return "\n".join(lines)
