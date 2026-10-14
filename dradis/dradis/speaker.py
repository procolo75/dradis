"""
speaker.py
──────────
DRADIS speaks on a Home Assistant voice assistant — at any time, not only as
the reply to a question.

A task started by voice finishes minutes later; its result has to be SAID on the
assistant that asked, not just posted to Telegram. A live monitor that fires has
to be able to say so too. Both go through `speak()`.

Through HA's own API (the add-on declares `homeassistant_api`):
  - `assist_satellite.*` → `assist_satellite.announce`: the satellite speaks with
    its pipeline's voice. The natural target for a voice assistant.
  - `media_player.*`     → `tts.speak` with a TTS engine chosen in the settings,
    for a speaker that is only a media player.

Where: the device that asked (its `device_id` arrives with every Assist request)
is resolved to its own satellite or player; otherwise the default speaker from
the settings.

No telegram or LLM imports: tested on its own (`tests/test_speaker.py`).
"""

import os
import re

import httpx

from voice import for_voice

SUPERVISOR = "http://supervisor"
_S6_ENV_DIR = "/run/s6/container_environment"
_TIMEOUT = 15
# HA answers a service call only when the service has finished — and an
# announcement finishes when it has been READ. A to-do list read aloud outlasted
# 15 s: DRADIS dropped the connection mid-call, HA cancelled the announcement
# and the satellite was left blinking. The speaking call waits as long as a
# speech can last.
_SPEAK_TIMEOUT = 180

# An announcement must not land on a satellite that is still answering. A
# voice-started task finished ~2 s after DRADIS said "started": the result was
# announced while the satellite was still speaking that reply, the two
# overlapped and the ring stayed lit in the reply's state. Wait for `idle`.
_IDLE_STATE = "idle"
_IDLE_WAIT_MAX = 60.0
_IDLE_POLL = 1.0

# A spoken answer longer than this is a lecture. Cut at a sentence end and say
# where the rest is: the full text always goes to Telegram as well.
SPEECH_MAX_CHARS = 600
_REST_ON_TELEGRAM = {"it": "Il resto è su Telegram.", "en": "The rest is on Telegram."}
_SENTENCE_END = re.compile(r"[.!?…](?=\s|$)")

_DOMAINS = ("assist_satellite", "media_player")


def supervisor_token() -> str:
    """The Supervisor token, from the environment or from s6's copy of it
    (the HA base images may start the process with a cleaned environment)."""
    for name in ("SUPERVISOR_TOKEN", "HASSIO_TOKEN"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
        try:
            with open(os.path.join(_S6_ENV_DIR, name)) as f:
                value = f.read().strip()
        except OSError:
            continue
        if value:
            return value
    return ""


def in_quiet_hours(start: str, end: str, tz_name: str = "UTC", now=None) -> bool:
    """True inside the "HH:MM"–"HH:MM" window (end excluded), which may cross
    midnight. An empty or malformed bound disables it. `now` (a time) for tests."""
    from datetime import datetime, time as time_t
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    if not start or not end:
        return False
    try:
        sh, sm = map(int, start.split(":"))
        eh, em = map(int, end.split(":"))
        qs, qe = time_t(sh, sm), time_t(eh, em)
    except (ValueError, AttributeError):
        return False
    if now is None:
        try:
            tz = ZoneInfo(tz_name or "UTC")
        except ZoneInfoNotFoundError:
            tz = ZoneInfo("UTC")
        now = datetime.now(tz).time()
    now = now.replace(second=0, microsecond=0)
    if qs <= qe:
        return qs <= now < qe
    return now >= qs or now < qe


def trim_for_speech(text: str, lang: str = "it", limit: int = SPEECH_MAX_CHARS) -> str:
    """Spoken form of `text` (already Telegram HTML or plain), cut at the last
    sentence end before `limit`."""
    spoken = for_voice(text, lang)
    if len(spoken) <= limit:
        return spoken
    head = spoken[:limit]
    ends = list(_SENTENCE_END.finditer(head))
    cut = ends[-1].end() if ends else head.rfind(" ")
    if cut <= 0:
        cut = limit
    return head[:cut].rstrip() + " " + _REST_ON_TELEGRAM.get(lang, _REST_ON_TELEGRAM["en"])


async def _ha(client: httpx.AsyncClient, method: str, path: str, json=None,
              timeout: float | None = None):
    kwargs = {"timeout": timeout} if timeout else {}
    resp = await client.request(method, f"{SUPERVISOR}/core/api/{path}", json=json,
                                headers={"Authorization": f"Bearer {supervisor_token()}"},
                                **kwargs)
    resp.raise_for_status()
    return resp


async def wait_until_idle(client: httpx.AsyncClient, entity_id: str,
                          max_wait: float = _IDLE_WAIT_MAX, poll: float = _IDLE_POLL,
                          sleep=None) -> bool:
    """Wait until an assist satellite is idle (not listening, processing or
    responding). True when it is; False after `max_wait` — the caller speaks
    anyway rather than lose the message."""
    import asyncio
    sleep = sleep or asyncio.sleep
    waited = 0.0
    while True:
        try:
            state = (await _ha(client, "GET", f"states/{entity_id}")).json().get("state")
        except Exception:
            return False                       # cannot tell: do not hold the message
        if state == _IDLE_STATE:
            return True
        if waited >= max_wait:
            print(f"[DRADIS] Speaker: {entity_id} still {state!r} after {max_wait:.0f}s, speaking anyway")
            return False
        await sleep(poll)
        waited += poll


async def resolve_target(client: httpx.AsyncClient, device_id: str | None,
                         default_entity: str = "") -> str:
    """The entity to speak on: the asking device's satellite (or player), else
    the default from the settings, else ""."""
    if device_id:
        resp = await _ha(client, "POST", "template", {
            "template": "{{ device_entities('%s') | join(',') }}" % device_id.replace("'", "")})
        entities = [e.strip() for e in resp.text.split(",") if e.strip()]
        for domain in _DOMAINS:
            hit = next((e for e in entities if e.startswith(domain + ".")), None)
            if hit:
                return hit
    return (default_entity or "").strip()


async def speak(text: str, lang: str = "it", device_id: str | None = None,
                settings: dict | None = None, client: httpx.AsyncClient | None = None) -> str:
    """Say `text` on a voice assistant. Returns the entity spoken on, or "" when
    nothing was said. Never raises: a speaker that is off must not break the
    task or monitor that wanted to speak."""
    settings = settings or {}
    message = trim_for_speech(text, lang)
    if not message:
        return ""
    own = client is None
    client = client or httpx.AsyncClient(timeout=_TIMEOUT)
    try:
        target = await resolve_target(client, device_id, settings.get("assist_speaker_entity", ""))
        if not target:
            print("[DRADIS] Speaker: no assistant to speak on (no device, no default speaker)")
            return ""
        if target.startswith("assist_satellite."):
            await wait_until_idle(client, target)
            await _ha(client, "POST", "services/assist_satellite/announce",
                      {"entity_id": target, "message": message}, timeout=_SPEAK_TIMEOUT)
        else:
            tts = (settings.get("assist_tts_entity") or "").strip()
            if not tts:
                print(f"[DRADIS] Speaker: {target} is a media player and no TTS engine is set")
                return ""
            await _ha(client, "POST", "services/tts/speak",
                      {"entity_id": tts, "media_player_entity_id": target, "message": message},
                      timeout=_SPEAK_TIMEOUT)
        print(f"[DRADIS] Speaker: said {len(message)} chars on {target}")
        return target
    except Exception as e:
        # The type, not just the text: a timeout's message is empty, and
        # "cannot speak: " told nobody anything.
        print(f"[DRADIS] Speaker: cannot speak: {type(e).__name__}: {e}")
        return ""
    finally:
        if own:
            await client.aclose()


async def list_entities(client: httpx.AsyncClient | None = None) -> dict:
    """Candidates for the settings: speakers (satellites first) and TTS engines."""
    own = client is None
    client = client or httpx.AsyncClient(timeout=_TIMEOUT)
    try:
        states = (await _ha(client, "GET", "states")).json()
    finally:
        if own:
            await client.aclose()

    def pick(prefix):
        return sorted(({"id": s["entity_id"],
                        "name": (s.get("attributes") or {}).get("friendly_name") or s["entity_id"]}
                       for s in states if s.get("entity_id", "").startswith(prefix)),
                      key=lambda e: e["name"].lower())
    return {"speakers": pick("assist_satellite.") + pick("media_player."), "tts": pick("tts.")}
