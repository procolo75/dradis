"""
bot/control.py
──────────────
The Telegram menu as tools: run tasks and scheduled monitors, switch them on
and off, the /rain and /storm radar views, Car Mode, the weather stations — by
voice through HA Assist, or by just asking in the chat. Live and HA monitors
are not driven by voice (user's choice); only their on-demand views are.

Every name the model can pass is an ENUM built from the configured tasks and
monitors at the moment the turn starts. The model cannot invent a name or add a
word to it; that is what made the HA MCP experiment unusable (v4.15.0).

A task or monitor started BY VOICE also speaks its result, when it is ready, on
the assistant that asked: the turn's channel (`state.current_channel()`) carries
the device, and `speaker.speak` is handed to the runner as `on_result`. Started
from the chat, it behaves exactly as the Telegram menu does.
"""

import asyncio
import re
import time

import bot.state as _state
import speaker
from voice import for_voice
from web.store import (
    load_live_monitors, load_monitors, load_settings, load_tasks,
    set_car_mode, toggle_monitor, toggle_task,
)

_SNAPSHOT_TYPES = {"rain_front": "rain", "storm_front": "storm"}
# Live and HA monitors are deliberately absent: they are not driven by voice.
# Their on-demand views (/rain, /storm) are tools of their own.
_KINDS = {
    "task":    (load_tasks,    toggle_task),
    "monitor": (load_monitors, toggle_monitor),
}


def _names(items: list[dict]) -> list[str]:
    return sorted({i["name"] for i in items if i.get("name")}, key=str.lower)


def _find(items: list[dict], name: str) -> dict | None:
    return next((i for i in items if i.get("name") == name), None)


def _channel() -> dict:
    return _state.current_channel()


def _lang() -> str:
    return _channel().get("language") or load_settings().get("voice_language", "it") or "it"


def _voice_result():
    """`on_result` for a runner: speak on the asking device — only when the turn
    came from a voice assistant."""
    ch = _channel()
    if ch.get("source") != "assist":
        return None
    lang, device_id = ch.get("language") or "it", ch.get("device_id")

    async def say(text: str) -> None:
        await speaker.speak(text, lang, device_id, load_settings())
    return say


def _plain(html_text: str) -> str:
    return for_voice(html_text, _lang())


# ── Tools ─────────────────────────────────────────────────────────────────────

async def run_task(name: str) -> str:
    from bot.scheduler import run_scheduled_task
    task = _find(load_tasks(), name)
    if not task:
        return f"No task named {name!r}."
    say = _voice_result()
    asyncio.create_task(run_scheduled_task(task, on_result=say))
    return (f"Task {name!r} started. Its result goes to Telegram"
            + (" and will be spoken here when ready." if say else ".")
            + " Just confirm it started.")


async def run_monitor(name: str) -> str:
    from bot.scheduler import run_scheduled_monitor
    monitor = _find(load_monitors(), name)
    if not monitor:
        return f"No monitor named {name!r}."
    say = _voice_result()
    asyncio.create_task(run_scheduled_monitor(monitor, on_result=say))
    return (f"Monitor {name!r} started. Its report goes to Telegram"
            + (" and will be spoken here when ready." if say else ".")
            + " Just confirm it started.")


async def radar_now(kind: str, name: str = "") -> str:
    """/rain or /storm by voice. Without a name, every monitor of that kind
    answers in turn: the menu's keyboard has no spoken equivalent, and asking
    "which one?" back costs a round for an answer that fits in two sentences."""
    from bot.handlers import _take_snapshot
    from live_monitors.snapshot import format_caption
    wanted = {"rain": "rain_front", "storm": "storm_front"}.get(kind)
    if not wanted:
        return f"Unknown kind {kind!r}: rain or storm."
    monitors = [m for m in load_live_monitors() if m.get("type") == wanted]
    if name:
        monitors = [m for m in monitors if m.get("name") == name]
    if not monitors:
        return f"No {kind} monitor" + (f" named {name!r}." if name else " configured.")
    # The picture cannot travel through the model: it goes to Telegram directly,
    # always from the chat, from a voice assistant only with the copy switched on.
    to_telegram = _channel().get("source") != "assist" or load_settings().get("assist_mirror_telegram")
    parts = []
    for cfg in sorted(monitors, key=lambda m: m.get("name", "").lower()):
        try:
            snap = await _take_snapshot(_SNAPSHOT_TYPES[cfg["type"]], cfg)
        except Exception as e:
            parts.append(f"{cfg['name']}: radar not available ({e}).")
            continue
        if to_telegram:
            await _send_snapshot(snap, cfg)
        # The caption already opens with the monitor's name: prefixing it again
        # made the speaker say "Temporali Bacoli, Temporali Bacoli".
        parts.append(_plain(format_caption(snap, voice=True)))
    return "\n".join(parts)


async def _send_snapshot(snap, cfg: dict) -> None:
    from live_monitors.snapshot import format_caption
    bot, chat_id = _state.get_bot_and_chat("default")
    if not bot:
        return
    caption, parse_mode = _state.for_car(
        format_caption(snap, voice=_state.car_mode_enabled()), cfg.get("language", "it"))
    caption = caption[:1023]
    try:
        if snap.picture:
            await bot.send_photo(chat_id=chat_id, photo=snap.picture, caption=caption,
                                 parse_mode=parse_mode, read_timeout=60, write_timeout=60)
        else:
            await bot.send_message(chat_id=chat_id, text=caption, parse_mode=parse_mode,
                                   disable_web_page_preview=True)
    except Exception as e:
        print(f"[DRADIS] radar_now: Telegram send failed: {e}")


async def set_enabled(kind: str, enabled: bool, name: str = "") -> str:
    if kind == "car_mode":
        set_car_mode(bool(enabled))
        return f"Car Mode is now {'on' if enabled else 'off'}."
    if kind not in _KINDS:
        return f"Unknown kind {kind!r}."
    load, toggle = _KINDS[kind]
    item = _find(load(), name)
    if not item:
        return f"No {kind.replace('_', ' ')} named {name!r}."
    # The store only toggles: flip only when the state differs, so "turn it
    # off" said twice does not turn it back on.
    if bool(item.get("enabled", True)) != bool(enabled):
        toggle(item["id"])
    return f"{kind.replace('_', ' ').capitalize()} {name!r} is now {'enabled' if enabled else 'disabled'}."


async def station_readings(place: str = "") -> str:
    from bot.handlers import _geocode_for_stations, stations_text
    from live_monitors import gauges as gauge_source
    from live_monitors.position import position_manager
    from web.store import load_positions
    if not gauge_source.is_enabled():
        return "MeteoHub is switched off (Settings → MeteoHub)."
    place = (place or "").strip()
    if place:
        try:
            lat, lon, label = await _geocode_for_stations(place)
        except ValueError:
            return f"Place not found: {place!r}."
    else:
        resolved = None
        for p in load_positions():
            fix = position_manager.resolve(p["id"], time.time())
            if fix is not None:
                resolved = (fix[0].lat, fix[0].lon, p.get("name") or p["id"])
                break
        if resolved is None:
            return "No position with a usable fix: ask for a place by name."
        lat, lon, label = resolved
    try:
        text = await stations_text(lat, lon, label, _lang(),
                                   voice=_channel().get("source") == "assist")
    except Exception as e:
        raise _state.agent_core.ToolError(f"MeteoHub read failed: {e}") from e
    return _plain(text)


# ── Direct radar commands (no model) ──────────────────────────────────────────
#
# "Cosa dice il radar?" went to the forecast tool, with a joke on top; "lancia il
# comando rain" ran a scheduled monitor that happens to be called "Pioggia". A
# command must not depend on a model choosing well most of the time, so these
# are matched here, before any model is involved — /rain and /storm by voice,
# as deterministic as the Telegram commands. Words agreed with the user; a
# forecast question ("sta per piovere?", "piove domani?") has no "radar" in it
# and still goes to the model.

_RAIN_WORDS  = {"pioggia", "piove", "piovere"}
_STORM_WORDS = {"temporale", "temporali", "fulmine", "fulmini"}
_TRIGGER_WORDS = _RAIN_WORDS | _STORM_WORDS | {"radar", "rain", "storm"}


def _normalise(text: str) -> str:
    import unicodedata
    plain = "".join(c for c in unicodedata.normalize("NFKD", (text or "").casefold())
                    if not unicodedata.combining(c))
    return " ".join(re.split(r"[^a-z0-9]+", plain.replace("_", " "))).strip()


def match_radar(text: str, live_monitors: list[dict]) -> list[tuple[str, str]]:
    """[(kind, name)] to run for this sentence, [] when it is not a radar command.
    name "" = every monitor of that kind; a monitor's name in the sentence
    narrows its kind to that one."""
    norm = _normalise(text)
    words = set(norm.split())
    kinds: list[str] = []
    # Added by the user: the one forecast-sounding phrase that means the radar.
    if "sta per piovere" in f" {norm} ":
        words |= {"radar", "pioggia"}
    if "rain" in words:
        kinds.append("rain")
    if "storm" in words:
        kinds.append("storm")
    if not kinds and "radar" in words:
        if words & _RAIN_WORDS:
            kinds.append("rain")
        if words & _STORM_WORDS:
            kinds.append("storm")
        if not kinds:
            kinds = ["rain", "storm"]
    out = []
    padded = f" {norm} "
    for kind in kinds:
        wanted = "rain_front" if kind == "rain" else "storm_front"
        # A name made only of the trigger words ("Temporali", "Pioggia") cannot
        # narrow anything: "radar temporali" asks for the kind, not that monitor.
        named = [m["name"] for m in live_monitors
                 if m.get("type") == wanted and m.get("name")
                 and not set(_normalise(m["name"]).split()) <= _TRIGGER_WORDS
                 and f" {_normalise(m['name'])} " in padded]
        # The longest name wins: "Temporali 2" over "Temporali".
        named.sort(key=lambda n: -len(_normalise(n)))
        out.append((kind, named[0] if named else ""))
    return out


# /stations by voice: "station" is an awkward word to say, so the readout answers
# to three Italian ones (agreed with the user). A place after a preposition is
# read there ("misure a Napoli"); without one, at the user's position.
_STATION_TRIGGER = re.compile(r"\b(misur[ae]|stazion[ei]|sensor[ei])\b(.*)$", re.IGNORECASE | re.DOTALL)
_STATION_PLACE   = re.compile(r"\b(?:vicino\s+a|presso|ad|a|di|in)\s+(.+)$", re.IGNORECASE)
_PLACE_FILLER    = {"meteo", "qui", "adesso", "ora", "oggi", "casa", "me", "mia", "posizione"}


def match_stations(text: str) -> str | None:
    """None when the sentence is not a stations command; otherwise the place
    to read ("" = the user's position)."""
    m = _STATION_TRIGGER.search(text or "")
    if not m:
        return None
    place = ""
    p = _STATION_PLACE.search(m.group(2))
    if p:
        words = [w for w in re.split(r"[^\w'’]+", p.group(1)) if w]
        while words and words[-1].casefold() in _PLACE_FILLER:
            words.pop()
        if words and not all(w.casefold() in _PLACE_FILLER for w in words):
            place = " ".join(words)
    return place


async def run_radar_command(commands: list[tuple[str, str]]) -> str:
    """Run what `match_radar` found and return the spoken text."""
    parts = [await radar_now(kind, name) for kind, name in commands]
    return "\n".join(p for p in parts if p)


# ── Specs ─────────────────────────────────────────────────────────────────────

def _enum(names: list[str]) -> dict:
    return {"type": "string", "enum": names}


def control_tools(settings: dict) -> list[dict]:
    """The tool specs, with the names configured right now. A tool whose list
    is empty is left out: an empty enum is a tool that can only fail."""
    tasks    = _names(load_tasks())
    monitors = _names(load_monitors())
    live     = load_live_monitors()
    radars   = _names([m for m in live if m.get("type") in _SNAPSHOT_TYPES])
    toggles  = _names(load_tasks() + load_monitors())
    specs = []
    if tasks:
        specs.append({"name": "run_task", "fn": run_task,
                      "description": "Run a DRADIS task now.",
                      "parameters": {"type": "object", "properties": {"name": _enum(tasks)},
                                     "required": ["name"]}})
    if monitors:
        specs.append({"name": "run_monitor", "fn": run_monitor,
                      "description": "Run a DRADIS scheduled monitor (report) now.",
                      "parameters": {"type": "object", "properties": {"name": _enum(monitors)},
                                     "required": ["name"]}})
    if radars:
        specs.append({"name": "radar_now", "fn": radar_now,
                      "description": "Radar NOW from the user's rain or storm monitors (= /rain, "
                                     "/storm): is rain or a storm arriving in the next minutes, "
                                     "how far, from where. Omit name to hear every monitor of "
                                     "that kind.",
                      "parameters": {"type": "object", "properties": {
                          "kind": {"type": "string", "enum": ["rain", "storm"]},
                          "name": _enum(radars)},
                          "required": ["kind"]}})
    kind = {"type": "string", "enum": ["task", "monitor", "car_mode"]}
    props = {"kind": kind, "enabled": {"type": "boolean"}}
    if toggles:
        props["name"] = _enum(toggles)
    specs.append({"name": "set_enabled", "fn": set_enabled,
                  "description": "Enable or disable a DRADIS task or scheduled monitor, or Car Mode (no name).",
                  "parameters": {"type": "object", "properties": props,
                                 "required": ["kind", "enabled"]}})
    if settings.get("meteohub_enabled"):
        specs.append({"name": "station_readings", "fn": station_readings,
                      "description": "Latest weather station readings near a place, or near the user if no place.",
                      "parameters": {"type": "object",
                                     "properties": {"place": {"type": "string"}}}})
    return specs
