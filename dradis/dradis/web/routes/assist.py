"""
web/routes/assist.py
────────────────────
Routes: the HA Assist conversation endpoint and its Web UI panel.

`POST /api/assist` is what the DRADIS integration in Home Assistant calls with
whatever the voice pipeline did not handle locally. It is the Telegram chat by
another door: same `chat_turn`, same tools, same history. Only the answer's
shape differs — spoken prose instead of Telegram HTML.
"""

import hmac
import html
import time

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

import assist
import speaker
from voice import for_voice
from web.store import load_settings

router = APIRouter()

# Said instead of an answer when no model answered. The details (model ids,
# provider errors) go to Telegram: read aloud they are noise.
_NO_ANSWER = {
    "it": "Scusa, in questo momento non riesco a rispondere.",
    "en": "Sorry, I can't answer right now.",
}


class AssistRequest(BaseModel):
    text:            str
    language:        str = "it"
    conversation_id: str | None = None
    device_id:       str | None = None


def _lang(language: str) -> str:
    return (language or "it").split("-")[0].lower()


def _check_token(authorization: str | None) -> None:
    expected = assist.ensure_token()
    given = (authorization or "").removeprefix("Bearer ").strip()
    if not given or not hmac.compare_digest(given, expected):
        raise HTTPException(status_code=401, detail="Invalid DRADIS token")


async def _answer(question: str, language: str, remember: bool = True,
                  device_id: str | None = None) -> dict:
    import bot.state as _state
    settings = _state.read_settings()
    lang = _lang(language)
    started = time.monotonic()

    channel = {"source": "assist", "device_id": device_id, "language": lang}
    turn = await _direct_command(question, settings, channel, remember)
    if turn is None:
        turn = await _state.chat_turn(question, settings, source="assist", remember=remember,
                                      channel=channel)
    mirror = settings.get("assist_mirror_telegram", False) and remember
    if turn.error:
        await _state._send_error_telegram(f"🎙️ {html.escape(question)}\n\n{turn.error}")
        speech = _NO_ANSWER.get(lang, _NO_ANSWER["en"])
    else:
        speech = for_voice(_state.md_to_html(turn.text), lang) if turn.text \
            else _NO_ANSWER.get(lang, _NO_ANSWER["en"])
        if mirror:
            # Same footer as the chat (tokens / tools, each behind its own toggle).
            await _state.send_telegram(f"🎙️ <i>{html.escape(question)}</i>\n\n"
                                       + _state.md_to_html(turn.text or "—")
                                       + _state.reply_footer(settings, turn.result))
            for notice in turn.notices:
                await _state._send_error_telegram(notice)

    elapsed = round(time.monotonic() - started, 1)
    assist.STATUS["last_request"] = {"at": int(time.time()), "seconds": elapsed,
                                     "question": question[:120], "ok": not turn.error}
    print(f"[DRADIS] Assist: answered in {elapsed}s ({'ok' if not turn.error else 'error'})")
    return {"speech": speech}


async def _direct_command(question: str, settings: dict, channel: dict, remember: bool):
    """/rain, /storm and /stations by voice, without the model
    (bot/control.py: match_radar, match_stations).
    Returns a ChatTurn, or None when the sentence is not such a command."""
    import bot.state as _state
    from bot import control
    from web.store import load_live_monitors
    if not settings.get("control_tools_enabled", True):
        return None
    commands = control.match_radar(question, load_live_monitors())
    place = None if commands else control.match_stations(question)
    if not commands and place is None:
        return None
    tool = "radar_now" if commands else "station_readings"
    print(f"[DRADIS] Assist: direct command {commands or ('stations', place)}")
    token = _state.CHANNEL.set(channel)
    try:
        text = (await control.run_radar_command(commands) if commands
                else await control.station_readings(place))
    except Exception as e:
        print(f"[DRADIS] Assist: direct command failed: {e}")
        text = (f"Non disponibile: {e}" if channel.get("language") == "it"
                else f"Not available: {e}")
    finally:
        _state.CHANNEL.reset(token)
    # Kept in the shared history like any answer: "e domani?" must follow on.
    if remember and settings.get("history_enabled", True):
        depth = settings.get("history_depth", 2)
        _state.save_turn("user", question, depth)
        _state.save_turn("assistant", text, depth)
    # No model ran: the footer says so instead of counting tokens.
    result = type("DirectResult", (), {"prompt_tokens": 0, "completion_tokens": 0,
                                       "tools_used": [f"{tool} (direct)"],
                                       "tool_errors": []})()
    return _state.ChatTurn(text=text, result=result)


@router.post("/api/assist")
async def assist_converse(req: AssistRequest, authorization: str | None = Header(default=None)):
    _check_token(authorization)
    if not load_settings().get("assist_enabled", True):
        raise HTTPException(status_code=503, detail="HA Assist is disabled in DRADIS")
    question = req.text.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Empty text")
    return await _answer(question, req.language, device_id=req.device_id)


@router.get("/api/assist/status")
async def assist_status(authorization: str | None = Header(default=None)):
    # The Web UI calls this without a token. The HA integration's manual setup
    # sends one, and must be told when it is wrong.
    if authorization:
        _check_token(authorization)
    return {**assist.STATUS, "token_set": assist.TOKEN_FILE.exists(),
            "integration_version": assist._version(assist.INTEGRATION_SRC)}


@router.get("/api/assist/entities")
async def assist_entities():
    """Speakers (assist satellites first, then media players) and TTS engines
    from Home Assistant, for the panel's menus."""
    try:
        return await speaker.list_entities()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Cannot read Home Assistant entities: {e}")


class SpeakTest(BaseModel):
    speaker: str | None = None
    tts:     str | None = None


@router.post("/api/assist/speak-test")
async def assist_speak_test(req: SpeakTest | None = None):
    """Say one sentence on the default speaker — the path a result arriving
    later, or a live monitor, takes when no device just asked. The panel sends
    what is selected right now, saved or not."""
    settings = dict(load_settings())
    if req is not None:
        if req.speaker is not None:
            settings["assist_speaker_entity"] = req.speaker
        if req.tts is not None:
            settings["assist_tts_entity"] = req.tts
    lang = _lang(settings.get("voice_language", "it"))
    text = ("Ciao, sono DRADIS. Se mi senti, l'altoparlante funziona." if lang == "it"
            else "Hello, this is DRADIS. If you can hear me, the speaker works.")
    target = await speaker.speak(text, lang, None, settings)
    if not target:
        raise HTTPException(status_code=400, detail="Nothing was said: choose a speaker "
                            "(and a TTS engine for a media player), then check the add-on log.")
    return {"ok": True, "entity": target}


@router.get("/api/assist/control-cost")
async def assist_control_cost():
    """What the control tools add to every message, in estimated tokens."""
    import core as agent_core
    from bot.control import control_tools
    try:
        specs = control_tools(load_settings())
    except Exception as e:
        print(f"[DRADIS] Assist: control-cost failed: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")
    schemas = [{"type": "function", "function": {k: t[k] for k in ("name", "description", "parameters")}}
               for t in specs]
    return {"tools": [t["name"] for t in specs], "tokens": agent_core.estimate_schema_tokens(schemas)}


@router.get("/api/assist/token")
async def assist_token(x_ingress_path: str | None = Header(default=None)):
    """The secret, for adding the integration by hand. Only through the add-on's
    own page: the Supervisor's ingress proxy sets X-Ingress-Path, a direct call
    on the internal network does not."""
    if not x_ingress_path:
        raise HTTPException(status_code=403, detail="Open the DRADIS panel to see the token")
    import socket
    import bot.state as _state
    # The container's hostname is the add-on's name on the hassio network
    # (local-dradis for a local add-on), which is what HA must call.
    return {"token": assist.ensure_token(),
            "url": f"http://{socket.gethostname()}:{_state.WEB_PORT}"}


@router.post("/api/assist/test")
async def assist_test():
    """Re-install if needed, re-announce, and ask one question that leaves no
    trace in the history. From the Web UI, behind ingress: no token needed."""
    import bot.state as _state
    try:
        outcome = assist.install_integration()
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Cannot install the HA integration: {e}")
    await assist.announce(assist.ensure_token(), _state.WEB_PORT)
    lang = _state.read_settings().get("voice_language", "it")
    answer = await _answer("Rispondi in una frase: sei operativo?" if _lang(lang) == "it"
                           else "Answer in one sentence: are you up?", lang, remember=False)
    return {"install": outcome, **assist.STATUS, **answer}
