"""
agents/seismic_live_monitor.py
──────────────────────────────
LLM-free live monitor: polling del feed RSS GOSSIP (INGV Osservatorio Vesuviano).
Invia alert Telegram per eventi sismici sui vulcani campani.
Tracking puramente in-memory (dict per processo); nessun DB su disco.

Logica stati
────────────
  Automatico → prima notifica (dati preliminari, magnitudo spesso assente)
  Rivisto    → aggiornamento (dati definitivi)
  Bollettino → ignorato silenziosamente

Comportamento enabled/disabled
────────────────────────────────
  enabled: true  → loop attivo, notifiche Telegram inviate
  enabled: false → loop attivo, nessuna notifica Telegram
  entry assente  → niente di niente

Quiet hours
────────────
  quiet_start / quiet_end (HH:MM): gli alert vengono accodati in-memory
  e inviati tutti insieme al primo poll successivo all'uscita dall'intervallo.
  Supporta intervalli cross-mezzanotte (es. 23:00–07:00).
"""

import asyncio
import email.utils
import html
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, time as time_t
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

_LOGGER = logging.getLogger(__name__)

POLL_INTERVAL_SEC = 60
RSS_URL           = "https://terremoti.ov.ingv.it/gossip/report.xml"

AREA_LABELS = {
    "flegrei":   "Campi Flegrei",
    "vesuvio":   "Vesuvio",
    "ischia":    "Isola di Ischia",
    "regionale": "Golfo di Napoli",
}

STATE_RANK = {
    "automatico": 0,
    "rivisto":    1,
    "bollettino": 2,
}

MAG_ICONS = {
    3.0: "🔴",
    2.0: "🟠",
    1.0: "🟡",
    0.0: "⚪",
}

AREA_CENTROIDS: dict[str, tuple[float, float]] = {
    "flegrei":   (40.827, 14.139),
    "vesuvio":   (40.821, 14.426),
    "ischia":    (40.731, 13.897),
    "regionale": (40.833, 14.233),
}


def _mag_icon(mag: float | None) -> str:
    if mag is None:
        return "⚪"
    for threshold in sorted(MAG_ICONS, reverse=True):
        if mag >= threshold:
            return MAG_ICONS[threshold]
    return "⚪"


def _normalize_state(raw: str) -> str:
    r = raw.strip().lower()
    if "rivisto" in r:
        return "rivisto"
    if "bollettino" in r:
        return "bollettino"
    return "automatico"


# ── RSS parser ────────────────────────────────────────────────────────────────

def _parse_rss(xml_text: str) -> list[dict]:
    """
    Parsa il feed RSS GOSSIP.
    GUID formato: serenade-GOSSIP.id.<event_id>.<revision>
    Link formato: .../gossip/<area>/2026/event_<id>.html
    Orario evento estratto dal <title> (UTC); fallback a pubDate.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        _LOGGER.error("RSS parse error: %s", e)
        return []

    channel = root.find("channel")
    if channel is None:
        return []

    events = []
    for item in channel.findall("item"):
        guid_el    = item.find("guid")
        title_el   = item.find("title")
        link_el    = item.find("link")
        desc_el    = item.find("description")
        pubdate_el = item.find("pubDate")

        if guid_el is None:
            continue

        guid    = (guid_el.text    or "").strip()
        title   = (title_el.text   or "").strip() if title_el   is not None else ""
        link    = (link_el.text    or "").strip() if link_el    is not None else ""
        desc    = (desc_el.text    or "").strip() if desc_el    is not None else ""
        pubdate = (pubdate_el.text or "").strip() if pubdate_el is not None else ""

        # Actual event time from title: "Evento sismico {Area} - YYYY/MM/DD HH:MM:SS"
        event_dt: datetime | None = None
        if " - " in title:
            dt_part = title.rsplit(" - ", 1)[-1].strip()
            try:
                event_dt = datetime.strptime(dt_part, "%Y/%m/%d %H:%M:%S").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                pass
        if event_dt is None and pubdate:
            try:
                event_dt = email.utils.parsedate_to_datetime(pubdate)
            except Exception:
                pass

        # event_id: serenade-GOSSIP.id.52767.173329 → "52767"
        event_id = guid
        parts = guid.split(".")
        if len(parts) >= 3:
            event_id = parts[2]

        # area dal link
        area = "sconosciuta"
        for key in AREA_LABELS:
            if f"/{key}/" in link:
                area = key
                break

        # Magnitudo (assente negli automatici — "magnitudo non definita")
        magnitude = None
        for line in desc.splitlines():
            if "magnitudo" in line.lower() and "non definita" not in line.lower():
                for token in line.split():
                    try:
                        v = float(token.replace(",", "."))
                        if -2.0 < v < 10.0:
                            magnitude = v
                            break
                    except ValueError:
                        continue
                if magnitude is not None:
                    break

        # Profondità
        depth = None
        for line in desc.splitlines():
            if "profondità" in line.lower() or "depth" in line.lower():
                for token in line.split():
                    try:
                        v = float(token.replace(",", "."))
                        if 0 <= v < 200:
                            depth = v
                            break
                    except ValueError:
                        continue
                if depth is not None:
                    break

        # Tipo
        tipo_raw = "Automatico"
        for line in desc.splitlines():
            line = line.strip()
            if line.lower().startswith("tipo"):
                tokens = line.split()
                if len(tokens) >= 2:
                    tipo_raw = tokens[-1]
                break

        latitude = None
        for line in desc.splitlines():
            if "lat" in line.lower():
                for token in line.split():
                    try:
                        v = float(token.replace(",", "."))
                        if 35.0 <= v <= 48.0:
                            latitude = v
                            break
                    except ValueError:
                        continue
                if latitude is not None:
                    break

        longitude = None
        for line in desc.splitlines():
            if "lon" in line.lower():
                for token in line.split():
                    try:
                        v = float(token.replace(",", "."))
                        if 6.0 <= v <= 19.0:
                            longitude = v
                            break
                    except ValueError:
                        continue
                if longitude is not None:
                    break

        events.append({
            "event_id":  event_id,
            "area":      area,
            "title":     title,
            "link":      link,
            "magnitude": magnitude,
            "depth":     depth,
            "latitude":  latitude,
            "longitude": longitude,
            "event_dt":  event_dt,
            "tipo_raw":  tipo_raw,
            "state":     _normalize_state(tipo_raw),
        })

    return events


# ── Monitor ───────────────────────────────────────────────────────────────────

class SeismicLiveMonitor:
    """
    Polling RSS GOSSIP con tracking in-memory degli eventi già notificati.

    _seen: dict[event_id, {"state": str, "notified_states": list[str]}]

    Al riavvio il primo poll silenzioso ricarica lo stato corrente del feed
    senza notificare nulla, quindi gli alert successivi riguardano solo
    eventi davvero nuovi o avanzamenti di stato rispetto al feed al momento
    dell'avvio.
    """

    def __init__(self, cfg: dict, telegram_send_fn, tz_name: str = "UTC"):
        self.monitor_id      = cfg["id"]
        self.name            = cfg.get("name", "Sismi")
        self.areas           = cfg.get("areas", ["flegrei", "vesuvio", "ischia"])
        self.tz_name         = tz_name
        self._send           = telegram_send_fn
        self._notify_enabled = cfg.get("_notify_enabled", True)
        self._quiet_start    = (cfg.get("quiet_start") or "").strip()
        self._quiet_end      = (cfg.get("quiet_end")   or "").strip()
        self._was_in_quiet   = False
        self._pending_quiet: list[tuple[dict, bool]] = []
        self._seen: dict[str, dict] = {}   # event_id → {state, notified_states}
        self._task: asyncio.Task | None = None
        notify_str = "notifiche ON" if self._notify_enabled else "solo logging"
        print(f"[SeismicMonitor] '{self.name}' init ({notify_str})")

    def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self._run(), name=f"live_seismic:{self.monitor_id}"
            )
            print(
                f"[SeismicMonitor] '{self.name}' avviato "
                f"(aree={self.areas}, poll={POLL_INTERVAL_SEC}s)"
            )

    def stop(self):
        if self._task and not self._task.done():
            self._task.cancel()
            print(f"[SeismicMonitor] '{self.name}' fermato")

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    # ── Quiet hours ──────────────────────────────────────────────────────────

    def _in_quiet_hours(self) -> bool:
        if not self._quiet_start or not self._quiet_end:
            return False
        try:
            sh, sm = map(int, self._quiet_start.split(":"))
            eh, em = map(int, self._quiet_end.split(":"))
            s = time_t(sh, sm)
            e = time_t(eh, em)
            t = datetime.now(ZoneInfo(self.tz_name)).time().replace(second=0, microsecond=0)
            if s <= e:
                return s <= t < e
            return t >= s or t < e   # cross-midnight
        except (ValueError, AttributeError, ZoneInfoNotFoundError):
            return False

    # ── Loop ────────────────────────────────────────────────────────────────

    async def _run(self):
        # Prima iterazione silenziosa: sincronizza _seen con il feed corrente
        # senza inviare alcuna notifica.
        await self._poll(notify=False)

        while True:
            try:
                await asyncio.sleep(POLL_INTERVAL_SEC)
                await self._poll(notify=True)
            except asyncio.CancelledError:
                return
            except Exception as e:
                _LOGGER.error("[SeismicMonitor] '%s' errore: %s", self.name, e)
                await asyncio.sleep(POLL_INTERVAL_SEC)

    async def _poll(self, notify: bool):
        notify = notify and self._notify_enabled

        in_quiet = self._in_quiet_hours()

        # Flush degli eventi accodati al termine del silenzio
        if self._was_in_quiet and not in_quiet and self._pending_quiet and notify:
            quiet_label = html.escape(f"{self._quiet_start}–{self._quiet_end}")
            header = f"🔕 <i>Notifiche accumulate durante il silenzio ({quiet_label}):</i>"
            try:
                await self._send(header)
            except Exception as e:
                _LOGGER.warning("[SeismicMonitor] errore invio header flush: %s", e)
            for pending_ev, pending_is_update in self._pending_quiet:
                await self._send_alert(pending_ev, pending_is_update)
            self._pending_quiet.clear()

        self._was_in_quiet = in_quiet

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(RSS_URL)
                resp.raise_for_status()
                xml_text = resp.text
        except Exception as e:
            _LOGGER.warning("[SeismicMonitor] '%s' fetch fallito: %s", self.name, e)
            return

        for ev in _parse_rss(xml_text):
            event_id = ev["event_id"]
            state    = ev["state"]
            area     = ev["area"]

            row = self._seen.get(event_id)

            # Bollettino: registra ma non notificare
            if state == "bollettino":
                if row is None:
                    self._seen[event_id] = {"state": state, "notified_states": []}
                return

            # Area non monitorata: registra silenziosamente
            if area not in self.areas:
                if row is None:
                    self._seen[event_id] = {"state": state, "notified_states": []}
                elif STATE_RANK.get(row["state"], 0) < STATE_RANK.get(state, 0):
                    self._seen[event_id]["state"] = state
                continue

            # Evento mai visto
            if row is None:
                notified = [state] if notify else []
                self._seen[event_id] = {"state": state, "notified_states": notified}
                if notify:
                    if in_quiet:
                        self._pending_quiet.append((ev, False))
                    else:
                        await self._send_alert(ev, is_update=False)
                continue

            # Evento già noto — aggiorna sempre lo stato in memory
            self._seen[event_id]["state"] = state

            if not notify:
                continue
            if state in row["notified_states"]:
                continue
            if STATE_RANK.get(state, 0) <= STATE_RANK.get(row["state"], 0):
                continue

            # Avanzamento di stato mai notificato
            self._seen[event_id]["notified_states"] = row["notified_states"] + [state]
            is_update = "automatico" in row["notified_states"]
            if in_quiet:
                self._pending_quiet.append((ev, is_update))
            else:
                await self._send_alert(ev, is_update=is_update)

    # ── Messaggi ─────────────────────────────────────────────────────────────

    async def _send_alert(self, ev: dict, is_update: bool):
        try:
            tz = ZoneInfo(self.tz_name)
        except ZoneInfoNotFoundError:
            tz = ZoneInfo("UTC")

        area_label = AREA_LABELS.get(ev["area"], ev["area"])
        mag        = ev["magnitude"]
        depth      = ev["depth"]
        icon       = _mag_icon(mag)
        mag_str    = f"Md {mag:.1f}" if mag is not None else "n.d."
        depth_str  = f"{depth:.1f} km" if depth is not None else "n.d."
        link       = ev.get("link", "").strip()

        event_dt: datetime | None = ev.get("event_dt")
        if event_dt is not None:
            time_str = event_dt.astimezone(tz).strftime("%d/%m/%Y %H:%M")
        else:
            time_str = datetime.now(tz).strftime("%d/%m/%Y %H:%M")

        if is_update:
            header = f"{icon} <b>Aggiornamento scossa — {html.escape(area_label)}</b>"
            note   = "🔄 Dati <b>rivisti</b> (definitivi)"
        elif ev["state"] == "rivisto":
            header = f"{icon} <b>Terremoto — {html.escape(area_label)}</b>"
            note   = "✅ Dati <b>rivisti</b>"
        else:
            header = f"{icon} <b>Terremoto — {html.escape(area_label)}</b>"
            note   = "⚠️ Rilevamento <b>automatico</b> — dati preliminari"

        lat = ev.get("latitude")
        lon = ev.get("longitude")
        if lat is None or lon is None:
            lat, lon = AREA_CENTROIDS.get(ev["area"], (None, None))

        lines = [
            header,
            f"📊 Magnitudo: <b>{mag_str}</b>",
            f"📐 Profondità: {depth_str}",
            note,
        ]
        if link:
            lines.append(f"🔗 <a href='{html.escape(link)}'>Scheda evento</a>")
        if lat is not None and lon is not None:
            map_url = f"https://www.google.com/maps?q={lat},{lon}"
            lines.append(f"🗺 <a href='{html.escape(map_url)}'>Apri in Maps</a>")
        lines.append(f"🕐 {time_str} · <code>#{ev['event_id']}</code>")

        label = "UPDATE" if is_update else "NUOVO"
        print(f"[SeismicMonitor] '{self.name}' [{label}] {area_label} {mag_str} (id={ev['event_id']})")
        try:
            await self._send("\n".join(lines))
        except Exception as e:
            print(f"[SeismicMonitor] '{self.name}' errore invio: {e}")


# ── Manager ───────────────────────────────────────────────────────────────────

class SeismicMonitorManager:
    """
    Gestisce tutte le istanze SeismicLiveMonitor.

    enabled=true  → loop attivo + notifiche Telegram
    enabled=false → loop attivo, nessuna notifica
    entry assente → niente di niente
    """

    def __init__(self):
        self._monitors: dict[str, SeismicLiveMonitor] = {}

    def reload(self, configs: list[dict], telegram_send_fn, tz_name: str):
        wanted: set[str] = set()
        for cfg in configs:
            if cfg.get("type") != "seismic":
                continue
            mid = cfg["id"]
            wanted.add(mid)
            if mid in self._monitors:
                self._monitors[mid].stop()
            cfg_with_notify = {**cfg, "_notify_enabled": bool(cfg.get("enabled", True))}
            m = SeismicLiveMonitor(cfg_with_notify, telegram_send_fn, tz_name)
            self._monitors[mid] = m
            m.start()
        for mid in list(self._monitors):
            if mid not in wanted:
                self._monitors[mid].stop()
                del self._monitors[mid]

    def stop_all(self):
        for m in self._monitors.values():
            m.stop()
        self._monitors.clear()

    def status(self, monitor_id: str) -> str:
        m = self._monitors.get(monitor_id)
        return "running" if (m and m.is_running()) else "stopped"


seismic_monitor_manager = SeismicMonitorManager()
