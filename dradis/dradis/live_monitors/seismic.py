"""
live_monitors/seismic.py
─────────────────────────
LLM-free live monitor: polls the INGV GOSSIP JSON API and sends Telegram alerts
for seismic events on the Campanian volcanoes. Pure in-memory state tracking.

States:
  Automatico → first notification (preliminary data, magnitude often absent)
  Rivisto    → update notification (final, reviewed data)
  Bollettino → silently ignored

Quiet hours:
  quiet_start / quiet_end (HH:MM): alerts are queued in memory and delivered
  all at once at the first poll after the quiet period ends.
  Supports cross-midnight intervals (e.g. 23:00–07:00).
"""

import asyncio
import html
import logging
from datetime import datetime, timedelta, timezone, time as time_t
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

_LOGGER = logging.getLogger(__name__)

POLL_INTERVAL_SEC = 60
JSON_URL          = "https://terremoti.ov.ingv.it/gossip/{area}/events.json"
MAX_AGE_HOURS     = 48

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


def _normalize_state(raw: str) -> str:
    r = (raw or "").strip().lower()
    if "rivisto" in r:
        return "rivisto"
    if "bollettino" in r:
        return "bollettino"
    return "automatico"


def _parse_event(raw: dict, area: str) -> dict:
    event_id = str(raw.get("id") or raw.get("epoch") or "")
    state    = _normalize_state(raw.get("class", "automatico"))
    magnitude = _extract_magnitude(raw)
    loc       = raw.get("location") or {}
    depth     = loc.get("depth")
    latitude  = loc.get("latitude")
    longitude = loc.get("longitude")
    epoch     = raw.get("epoch")
    event_dt  = datetime.fromtimestamp(epoch, tz=timezone.utc) if epoch else None
    link = ""
    if event_id and event_dt:
        link = (
            f"https://terremoti.ov.ingv.it/gossip/{area}"
            f"/{event_dt.year}/event_{event_id}.html"
        )
    return {
        "event_id": event_id,
        "area":     area,
        "state":    state,
        "magnitude": magnitude,
        "depth":    depth,
        "latitude": latitude,
        "longitude": longitude,
        "event_dt": event_dt,
        "link":     link,
    }


class SeismicLiveMonitor:
    """
    Polls INGV GOSSIP JSON API with in-memory event tracking.

    _seen: dict[event_id, {"state": str, "notified_states": list[str]}]

    First poll is silent (syncs state without notifications). Subsequent
    polls only notify on new events or state promotions since startup.
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
        self._seen: dict[str, dict] = {}
        self._task: asyncio.Task | None = None
        notify_str = "notifications ON" if self._notify_enabled else "logging only"
        print(f"[SeismicMonitor] '{self.name}' init ({notify_str})")

    def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self._run(), name=f"live_seismic:{self.monitor_id}"
            )
            print(
                f"[SeismicMonitor] '{self.name}' started "
                f"(areas={self.areas}, poll={POLL_INTERVAL_SEC}s)"
            )

    def stop(self):
        if self._task and not self._task.done():
            self._task.cancel()
            print(f"[SeismicMonitor] '{self.name}' stopped")

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

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
            return t >= s or t < e
        except (ValueError, AttributeError, ZoneInfoNotFoundError):
            return False

    async def _run(self):
        await self._poll(notify=False)
        while True:
            try:
                await asyncio.sleep(POLL_INTERVAL_SEC)
                await self._poll(notify=True)
            except asyncio.CancelledError:
                return
            except Exception as e:
                _LOGGER.error("[SeismicMonitor] '%s' error: %s", self.name, e)
                await asyncio.sleep(POLL_INTERVAL_SEC)

    async def _fetch_area(self, client: httpx.AsyncClient, area: str) -> list[dict]:
        url = JSON_URL.format(area=area)
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, list) else []
        except Exception as e:
            _LOGGER.warning("[SeismicMonitor] '%s' fetch area=%s failed: %s", self.name, area, e)
            return []

    async def _poll(self, notify: bool):
        notify   = notify and self._notify_enabled
        in_quiet = self._in_quiet_hours()
        cutoff   = (datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)).timestamp()

        if self._was_in_quiet and not in_quiet and self._pending_quiet and notify:
            quiet_label = html.escape(f"{self._quiet_start}–{self._quiet_end}")
            header = f"🔕 <i>Notifications queued during quiet hours ({quiet_label}):</i>"
            try:
                await self._send(header)
            except Exception as e:
                _LOGGER.warning("[SeismicMonitor] error sending flush header: %s", e)
            for pending_ev, pending_is_update in self._pending_quiet:
                await self._send_alert(pending_ev, pending_is_update)
            self._pending_quiet.clear()

        self._was_in_quiet = in_quiet

        async with httpx.AsyncClient(timeout=15) as client:
            results = await asyncio.gather(
                *[self._fetch_area(client, area) for area in self.areas],
                return_exceptions=False,
            )

        for area, raw_events in zip(self.areas, results):
            for raw in raw_events:
                epoch = raw.get("epoch", 0)
                if epoch and epoch < cutoff:
                    continue

                ev       = _parse_event(raw, area)
                event_id = ev["event_id"]
                state    = ev["state"]

                if not event_id:
                    continue

                row = self._seen.get(event_id)

                if state == "bollettino":
                    if row is None:
                        self._seen[event_id] = {"state": state, "notified_states": []}
                    continue

                if row is None:
                    notified = [state] if notify else []
                    self._seen[event_id] = {"state": state, "notified_states": notified}
                    if notify:
                        if in_quiet:
                            self._pending_quiet.append((ev, False))
                        else:
                            await self._send_alert(ev, is_update=False)
                    continue

                old_state = row["state"]
                self._seen[event_id]["state"] = state

                if not notify:
                    continue
                if state in row["notified_states"]:
                    continue
                if STATE_RANK.get(state, 0) <= STATE_RANK.get(old_state, 0):
                    continue

                self._seen[event_id]["notified_states"] = row["notified_states"] + [state]
                is_update = "automatico" in row["notified_states"]
                if in_quiet:
                    self._pending_quiet.append((ev, is_update))
                else:
                    await self._send_alert(ev, is_update=is_update)

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

        label = "UPDATE" if is_update else "NEW"
        print(f"[SeismicMonitor] '{self.name}' [{label}] {area_label} {mag_str} (id={ev['event_id']})")
        try:
            await self._send("\n".join(lines))
        except Exception as e:
            print(f"[SeismicMonitor] '{self.name}' send error: {e}")


class SeismicMonitorManager:
    """Owns all SeismicLiveMonitor instances. Reloaded by the bot on startup and config changes."""

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
