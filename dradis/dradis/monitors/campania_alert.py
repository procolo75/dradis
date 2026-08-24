"""
monitors/campania_alert.py
──────────────────────────
LLM-free monitor: reads the civil-protection alert bulletins issued by the Centro
Funzionale Multirischi of Regione Campania — today's and tomorrow's, always both —
and sends a Telegram alert only when one of the eight alert zones reaches the
configured level on either day.

Both days, not one or the other: the question this monitor answers is "is there an
alert", and half an answer to that is worse than none. Tomorrow's bulletin is also
the actionable one — today's window is already running by the time it is read.

Why an API and not the website: centrofunzionale.regione.campania.it is an
Angular single-page app. The HTML it serves is an empty shell — the bulletin is
drawn client-side — so read_url (and any other HTML fetcher) gets back
"Caricamento in corso..." and nothing else. The page's own JavaScript bundle
calls a public, unauthenticated REST backend that returns the bulletin already
structured, which is what this monitor reads. Nothing here parses HTML or PDF.

Which of that backend's endpoints: the two the home page's own alert map calls,
`findLastAllertaNew` and `findAllertaDomaniNew`. The `findLastBollettino` pair
this monitor used until v4.7.4 answers a different question and leaves `dataDa`
null on today's window, which is where the "dal ?" in the message came from.

An empty zone list is not the same answer on the two days, and the site does not
treat it as one: today's map is painted green when nothing is in alert, while
tomorrow's is painted grey — "not decided yet" — until `checkAvviso` turns true.
Mirrored here, because "bollettino non ancora emesso" printed over a day the
region has already declared quiet is the same wrong answer in the other direction.

The validity window belongs to the avviso, and only to it. A real one carries its
own hours and they vary widely — across 235 avvisi from 2024-2026 the window
starts at eighteen distinct hours, 00:00 the most common at a quarter of them and
14:00 second at one in seven. On a day with no avviso the backend still answers
`dataDa`/`dataA`, computed as today 14:00 → tomorrow 14:00, with every other field
null. That is a placeholder, not a window anyone declared, so nothing prints its
hours: a day with no avviso is headed by its date alone.

Levels, as the region defines them:
  1 : 🟢 VERDE      — nessuna allerta
  2 : 🟡 GIALLO     — criticità ordinaria
  3 : 🟠 ARANCIONE  — criticità moderata
  4 : 🔴 ROSSO      — criticità elevata
"""

import asyncio
import html
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

_BASE_URL = ("https://centrofunzionale.regione.campania.it"
             "/CentroFunzionalePortaleRest/rest/bollettinometeo")

_ENDPOINTS = {
    "today":    f"{_BASE_URL}/findLastAllertaNew",
    "tomorrow": f"{_BASE_URL}/findAllertaDomaniNew",
}

# Same wrapper, keyed by bulletin id. See _with_detail.
_DETAIL_URL = f"{_BASE_URL}/findByIdBollettino/{{bulletin_id}}"

# The API returns the zone as a bare number; the names live only in the site's
# JavaScript bundle, so they are carried here.
ZONES = {
    1: "Piana campana, Napoli, Isole, Area Vesuviana",
    2: "Alto Volturno e Matese",
    3: "Penisola sorrentino-amalfitana, Monti di Sarno e Monti Picentini",
    4: "Alta Irpinia e Sannio",
    5: "Tusciano e Alto Sele",
    6: "Piana Sele e Alto Cilento",
    7: "Tanagro",
    8: "Basso Cilento",
}

_LEVELS = {
    1: ("🟢", "VERDE",     "GREEN"),
    2: ("🟡", "GIALLO",    "YELLOW"),
    3: ("🟠", "ARANCIONE", "ORANGE"),
    4: ("🔴", "ROSSO",     "RED"),
}

# The bulletin repeats the same scenario text on every zone in alert. Printed
# once it is context; printed eight times it is the whole message.
_SCENARIOS_MAX_CHARS = 900

_STRINGS = {
    "it": {
        "title":       "🚨 <b>Allerta Protezione Civile — Campania</b>",
        "today":       "📅 <b>OGGI</b>",
        "tomorrow":    "📅 <b>DOMANI</b>",
        # "n. 72/2026" is a slash between digits, which Car Mode reads aloud as
        # the ratio "72 su 2026". Spelled out, it survives being spoken.
        "notice":      "Avviso n. {n} del {year} · emesso {issued}",
        "validity":    "dal {start} al {end}",
        "green":       "🟢 Verdi: {zones}",
        "all_green":   "🟢 Nessuna allerta su tutte le zone.",
        "not_issued":  "<i>Bollettino non ancora emesso.</i>",
        "unavailable": "<i>Bollettino non raggiungibile: {reason}</i>",
        "phenomena":   "<b>Fenomeni previsti</b>",
        "scenarios":   "<b>Scenari di evento</b>",
        "signature":   "Firmato: {who}",
        "footer":      "<i>Monitor DRADIS · Centro Funzionale Campania · nessun LLM utilizzato</i>",
    },
    "en": {
        "title":       "🚨 <b>Civil Protection Alert — Campania</b>",
        "today":       "📅 <b>TODAY</b>",
        "tomorrow":    "📅 <b>TOMORROW</b>",
        "notice":      "Notice no. {n} of {year} · issued {issued}",
        "validity":    "from {start} to {end}",
        "green":       "🟢 Green: {zones}",
        "all_green":   "🟢 No alert on any zone.",
        "not_issued":  "<i>Bulletin not published yet.</i>",
        "unavailable": "<i>Bulletin unreachable: {reason}</i>",
        "phenomena":   "<b>Expected phenomena</b>",
        "scenarios":   "<b>Event scenarios</b>",
        "signature":   "Signed: {who}",
        "footer":      "<i>DRADIS Monitor · Centro Funzionale Campania · no LLM used</i>",
    },
}


async def _get_json(url: str) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, headers={"Accept": "application/json"})
    resp.raise_for_status()
    return resp.json()


async def _with_detail(data: dict) -> dict:
    """Fill in the bulletin's own prose when the alert endpoint omits it.

    The two endpoints feed the home page's map, which needs nothing but `zona`
    and `livello` — the site fetches `fenomeni` and `scenari` separately, when a
    zone is clicked. They come back inline today, and this monitor would lose
    half its message the day that changes, so the detail endpoint is asked only
    when the zones in alert arrive with no text at all: no extra request on the
    common path, and none whatsoever on a day with no alert.
    """
    alerted = [z for z in (data.get("bollettinoMeteoBindList") or [])
               if (z.get("livello") or 1) > 1]
    bulletin_id = data.get("idBollettino")
    if not alerted or bulletin_id is None:
        return data
    if any(z.get("fenomeni") or z.get("scenari") for z in alerted):
        return data

    # Best-effort: the levels are already in hand, and an alert without its
    # scenario text still has to reach the phone.
    try:
        detail = await _get_json(_DETAIL_URL.format(bulletin_id=bulletin_id))
    except Exception:
        return data
    zones = (detail or {}).get("bollettinoMeteoBindList") or []
    return {**data, "bollettinoMeteoBindList": zones} if zones else data


async def _fetch_bulletin(day: str) -> dict:
    """Fetch the bulletin JSON for 'today' or 'tomorrow'."""
    data = await _get_json(_ENDPOINTS.get(day, _ENDPOINTS["today"]))
    return await _with_detail(data) if isinstance(data, dict) else data


def _is_issued(data, day: str) -> bool:
    """Has the region already decided this window, or is it still undecided?

    Only tomorrow can be undecided, and the site says so through `checkAvviso`:
    its map for tomorrow is grey until that flag turns true, green afterwards.
    Today's window is running, so its map is green whenever no zone is listed —
    an empty list there means "nessuna allerta", never "not published yet".
    """
    if day != "tomorrow":
        return True
    return bool(isinstance(data, dict) and data.get("checkAvviso"))


def _level_label(level: int, lang: str) -> str:
    emoji, it_name, en_name = _LEVELS.get(level, _LEVELS[1])
    return f"{emoji} {en_name if lang == 'en' else it_name}"


def _day_only(raw) -> str:
    """The date out of the API's "HH:MM - DD/MM/YYYY", without the clock."""
    text = str(raw or "").strip()
    return text.partition(" - ")[2].strip() if " - " in text else text


def _stamp(raw) -> str:
    """Turn the API's "HH:MM - DD/MM/YYYY" into "DD/MM/YYYY HH:MM".

    Not cosmetics: Car Mode only reads a slashed group as a date, rather than as
    a ratio, when a clock follows it — and the API puts the clock in front.
    """
    text = str(raw or "").strip()
    if " - " in text:
        left, _, right = text.partition(" - ")
        if ":" in left and "/" in right:
            return f"{right.strip()} {left.strip()}"
    return text


def _dedup(values) -> list[str]:
    """Keep the distinct non-empty strings, in the order the zones list them."""
    out: list[str] = []
    for v in values:
        v = (v or "").strip()
        if v and v not in out:
            out.append(v)
    return out


def _max_level(data) -> int:
    """Highest zone level in a bulletin; 1 when it holds nothing to report."""
    if not isinstance(data, dict):
        return 1
    zones = data.get("bollettinoMeteoBindList") or []
    return max((z.get("livello") or 1) for z in zones) if zones else 1


def _day_block(data, s: dict, heading: str, lang: str, issued: bool = True) -> list[str]:
    """Render one day's bulletin: heading, validity, zones, phenomena, scenarios."""
    lines = [heading]

    # A failed fetch for one day must not cost the other day's alert, so it is
    # reported in place instead of raising. See run_campania_alert_monitor.
    if isinstance(data, BaseException):
        reason = f"{type(data).__name__}: {data}" if str(data) else type(data).__name__
        lines.append(s["unavailable"].format(reason=html.escape(reason)))
        return lines

    zones = data.get("bollettinoMeteoBindList") or []

    # No avviso, no window: the hours the backend answers with here are its own
    # invention (see the module docstring), so only the day itself is printed.
    if not zones:
        # dataA is deliberately not a fallback: it names the day the window
        # would have ended, which is the day after the one being reported.
        day = _day_only(data.get("dataDa"))
        lines[0] = f"{heading} ({html.escape(day)})" if day else heading
        lines.append(s["all_green"] if issued else s["not_issued"])
        return lines

    validity = s["validity"].format(
        start=html.escape(_stamp(data.get("dataDa")) or "?"),
        end=html.escape(_stamp(data.get("dataA")) or "?"))
    lines[0] = f"{heading} — {validity}"

    if data.get("numeroAvviso") is not None:
        lines.append(s["notice"].format(
            n=data["numeroAvviso"], year=data.get("anno", ""),
            issued=html.escape(_stamp(data.get("dataEmissione")) or "?")))

    alerted = sorted((z for z in zones if (z.get("livello") or 1) > 1),
                     key=lambda z: (-(z.get("livello") or 1), z.get("zona") or 0))
    green   = sorted(z.get("zona") for z in zones if (z.get("livello") or 1) <= 1)

    if not alerted:
        lines.append(s["all_green"])
        return lines

    for z in alerted:
        num   = z.get("zona")
        name  = html.escape(ZONES.get(num, f"Zona {num}"))
        label = _level_label(z.get("livello") or 1, lang)
        risks = html.escape((z.get("tipoRischi") or "").strip())
        line  = f"{label} — <b>Zona {num}</b> · {name}"
        lines.append(line + (f"\n   ↳ {risks}" if risks else ""))

    if green:
        lines.append(s["green"].format(zones=", ".join(str(n) for n in green)))

    phenomena = _dedup(z.get("fenomeni") for z in alerted)
    if phenomena:
        lines += ["", s["phenomena"]]
        lines += [html.escape(p) for p in phenomena]

    scenarios = _dedup(z.get("scenari") for z in alerted)
    if scenarios:
        text = "\n".join(scenarios)
        if len(text) > _SCENARIOS_MAX_CHARS:
            text = text[:_SCENARIOS_MAX_CHARS].rstrip() + "…"
        lines += ["", s["scenarios"], html.escape(text)]

    signer = (data.get("firmaBollettino") or "").strip()
    if signer:
        lines.append(s["signature"].format(who=html.escape(signer)))

    return lines


def _format_report(today, tomorrow, tz_name: str, lang: str) -> str:
    s = _STRINGS.get(lang, _STRINGS["it"])
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")

    lines = [
        s["title"],
        f"🕐 {datetime.now(tz).strftime('%d/%m/%Y %H:%M')} ({tz_name})",
        "",
    ]
    lines += _day_block(today, s, s["today"], lang, _is_issued(today, "today"))
    lines += [""]
    lines += _day_block(tomorrow, s, s["tomorrow"], lang, _is_issued(tomorrow, "tomorrow"))
    lines += ["", s["footer"]]
    return "\n".join(lines)


async def run_campania_alert_monitor(monitor: dict, tz_name: str = "UTC") -> str:
    lang      = monitor.get("language", "it")
    min_level = max(1, min(int(monitor.get("min_level", 2)), 4))

    # Tomorrow is fetched tolerantly and today is not. A civil-protection alert
    # that exists today must reach the phone even if the other endpoint is down,
    # so its failure is carried into the message; today's failure is the monitor
    # failing, and the scheduler says so.
    today, tomorrow = await asyncio.gather(
        _fetch_bulletin("today"),
        _fetch_bulletin("tomorrow"),
        return_exceptions=True,
    )
    if isinstance(today, BaseException):
        raise today

    if max(_max_level(today), _max_level(tomorrow)) < min_level:
        return ""

    return _format_report(today, tomorrow, tz_name, lang)
