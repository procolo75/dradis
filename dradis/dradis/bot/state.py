"""
bot/state.py
────────────
Global state, startup configuration, settings helpers, agent builders,
fallback logic, history management, Markdown→HTML converter, and voice
transcription. All other bot modules import from here.

The _telegram_bot, _main_loop, and _scheduler globals are set by main.py
during startup and mutated by bot.scheduler at runtime.
"""

import asyncio
import html
import json
import os
import re
import tempfile
import time
import traceback
from pathlib import Path

from groq import Groq as GroqClient
from telegram import Bot as _TelegramBot
from telegram.constants import ParseMode
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import core as agent_core
from web.store import SETTINGS_DEFAULTS, SETTINGS_FILE
from agents.gcal    import GCAL_TOKEN_FILE, create_gcal_agent
from agents.gmail   import GMAIL_TOKEN_FILE, create_gmail_agent
from agents.gtasks  import GTASKS_TOKEN_FILE, create_gtasks_agent
from agents.weather    import create_weather_agent
from agents.web_search import create_web_search_agent

# ── Startup options ───────────────────────────────────────────────────────────

def _load_startup_options() -> dict:
    try:
        with open("/data/options.json") as f:
            return json.load(f)
    except Exception as e:
        raise RuntimeError(f"Cannot read /data/options.json: {e}")


_startup_options = _load_startup_options()

TELEGRAM_TOKEN       = _startup_options["telegram_bot_token"]
ALLOWED_CHAT_ID      = int(_startup_options["telegram_allowed_chat_id"])
TAVILY_API_KEY       = _startup_options.get("tavily_api_key", "")
GOOGLE_CLIENT_ID     = _startup_options.get("google_client_id", "")
GOOGLE_CLIENT_SECRET = _startup_options.get("google_client_secret", "")
WEB_PORT             = 8099

API_KEYS = {
    "openrouter": _startup_options.get("openrouter_api_key", ""),
    "openai":     _startup_options.get("openai_api_key", ""),
    "github":     _startup_options.get("github_token", ""),
    "gemini":     _startup_options.get("gemini_api_key", ""),
    "groq":       _startup_options.get("groq_api_key", ""),
}
agent_core.setup(API_KEYS)

_groq_client: GroqClient | None = (
    GroqClient(api_key=API_KEYS["groq"]) if API_KEYS.get("groq") else None
)

# ── Runtime globals (set by main.py) ─────────────────────────────────────────

_scheduler: AsyncIOScheduler              = AsyncIOScheduler()
_telegram_bot                             = None
_main_loop: asyncio.AbstractEventLoop | None = None

# ── Extra bot registry ────────────────────────────────────────────────────────

_extra_bots:     dict[str, "_TelegramBot"] = {}
_extra_chat_ids: dict[str, int]            = {}


def get_bot_and_chat(bot_id: str = "default") -> tuple:
    if bot_id and bot_id != "default" and bot_id in _extra_bots:
        return _extra_bots[bot_id], _extra_chat_ids[bot_id]
    return _telegram_bot, ALLOWED_CHAT_ID


def reload_extra_bots() -> None:
    from web.store import load_bots
    global _extra_bots, _extra_chat_ids
    _extra_bots.clear()
    _extra_chat_ids.clear()
    for b in load_bots():
        bid = b.get("id", "").strip()
        tok = b.get("token", "").strip()
        cid = b.get("chat_id")
        if bid and tok and cid:
            try:
                _extra_bots[bid]     = _TelegramBot(token=tok)
                _extra_chat_ids[bid] = int(cid)
                print(f"[DRADIS] Extra bot loaded: id={bid!r} name={b.get('name')!r}")
            except Exception as e:
                print(f"[DRADIS] WARNING: cannot init bot {bid!r}: {e}")

# ── Settings helpers ──────────────────────────────────────────────────────────

_LEGACY_SETTINGS_MAP = {
    "openrouter_model":  "model",
    "istruzioni_agente": "agent_instructions",
    "memoria_attiva":    "history_enabled",
    "num_conversazioni": "history_depth",
    "messaggio_avvio":   "startup_message",
    "ws_abilitato":      "ws_enabled",
    "ws_modello":        "ws_model",
    "ws_istruzioni":     "ws_instructions",
    "meteo_abilitato":   "weather_enabled",
    "meteo_provider":    "weather_provider",
    "meteo_modello":     "weather_model",
    "meteo_istruzioni":  "weather_instructions",
}


def _init_settings() -> None:
    if not SETTINGS_FILE.exists():
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(SETTINGS_DEFAULTS, ensure_ascii=False, indent=2))


def read_settings() -> dict:
    result = dict(SETTINGS_DEFAULTS)
    try:
        raw = json.loads(SETTINGS_FILE.read_text())
        result.update({_LEGACY_SETTINGS_MAP.get(k, k): v for k, v in raw.items()})
    except Exception:
        pass
    return result


def build_system_prompt() -> str:
    settings     = read_settings()
    tz_name      = settings.get("timezone", "UTC") or "UTC"
    instructions = settings.get("agent_instructions", SETTINGS_DEFAULTS["agent_instructions"])
    return f"It is {agent_core._now_str(tz_name)} ({tz_name}).\n{instructions}"


# ── Conversation history ──────────────────────────────────────────────────────

_history: list[dict] = []


def save_turn(role: str, text: str, history_depth: int) -> None:
    _history.append({"role": role, "content": text})
    max_msg = history_depth * 2
    while len(_history) > max_msg:
        _history.pop(0)


def build_context(question: str) -> str:
    if not _history:
        return question
    history = "\n".join(
        f"{'User' if m['role'] == 'user' else 'DRADIS'}: {m['content']}"
        for m in _history
    )
    return f"Conversation history:\n{history}\n\nUser: {question}"


# ── Telegram helpers ──────────────────────────────────────────────────────────

async def send_telegram(text: str, bot_id: str = "default",
                        parse_mode: str = ParseMode.HTML) -> None:
    bot, chat_id = get_bot_and_chat(bot_id)
    if not bot:
        return
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode)
    except Exception as ex:
        print(f"[DRADIS] send_telegram(bot_id={bot_id!r}) error: {ex}")


async def _send_error_telegram(msg: str, bot_id: str = "default") -> None:
    await send_telegram(msg, bot_id=bot_id)


# ── Markdown → HTML ───────────────────────────────────────────────────────────

_FUNCTION_TAG_RE = re.compile(r'<function=[^>]+>.*?</function>', re.DOTALL)


def md_to_html(text: str) -> str:
    text = _FUNCTION_TAG_RE.sub('', text).strip()
    text = html.escape(text)
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'\*(.+?)\*',     r'<i>\1</i>', text)
    text = re.sub(r'`(.+?)`',       r'<code>\1</code>', text)
    return text


# ── read_url tool ─────────────────────────────────────────────────────────────

async def read_url(url: str) -> str:
    """Fetch and return the text content of a web page given its URL.
    Call this when the user explicitly provides a URL starting with http:// or https://.
    Do NOT call this for questions or search queries."""
    import httpx
    if not url.startswith("http://") and not url.startswith("https://"):
        return "Error: a valid URL starting with http:// or https:// is required."
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"https://r.jina.ai/{url}",
            headers={"Accept": "text/plain"},
            follow_redirects=True,
        )
    return resp.text[:8000]


# ── Fallback logic ────────────────────────────────────────────────────────────

_FALLBACK_MAP = {
    "model":         ("fallback_model",          "provider",         "fallback_provider"),
    "ws_model":      ("ws_fallback_model",        "ws_provider",      "ws_fallback_provider"),
    "weather_model": ("weather_fallback_model",   "weather_provider", "weather_fallback_provider"),
    "gcal_model":    ("gcal_fallback_model",      "gcal_provider",    "gcal_fallback_provider"),
    "gmail_model":   ("gmail_fallback_model",     "gmail_provider",   "gmail_fallback_provider"),
    "gtasks_model":  ("gtasks_fallback_model",    "gtasks_provider",  "gtasks_fallback_provider"),
}

_AGENT_TO_FB_MODEL_KEY = {
    "web_search": "ws_fallback_model",
    "weather":    "weather_fallback_model",
    "gcal":       "gcal_fallback_model",
    "gmail":      "gmail_fallback_model",
    "gtasks":     "gtasks_fallback_model",
}


def _apply_fallback_settings(settings: dict) -> dict:
    s = dict(settings)
    for pm_key, (fb_m_key, pv_key, fb_pv_key) in _FALLBACK_MAP.items():
        fb_model = (s.get(fb_m_key) or "").strip()
        fb_prov  = (s.get(fb_pv_key) or "").strip()
        if fb_model:
            s[pm_key] = fb_model
            if fb_prov:
                s[pv_key] = fb_prov
    return s


def _build_members(settings: dict) -> list:
    members = []
    if settings.get("ws_enabled") and TAVILY_API_KEY:
        members.append(create_web_search_agent(settings, TAVILY_API_KEY))
    if settings.get("weather_enabled"):
        members.append(create_weather_agent(settings))
    if settings.get("gcal_enabled") and GCAL_TOKEN_FILE.exists():
        members.append(create_gcal_agent(settings))
    if settings.get("gmail_enabled") and GMAIL_TOKEN_FILE.exists():
        members.append(create_gmail_agent(settings))
    if settings.get("gtasks_enabled") and GTASKS_TOKEN_FILE.exists():
        members.append(create_gtasks_agent(settings))
    return members


def _build_executor(system_prompt: str, model: str, provider: str, members: list, settings: dict):
    tools = [read_url] if settings.get("read_url_enabled") else []
    if tools:
        system_prompt = (
            system_prompt
            + " When the user provides a URL starting with http:// or https://, "
            "call read_url to fetch the page content and answer based on it."
        )
    if members:
        member_names = {m.name for m in members}
        routing_rules = []
        if tools and "web_search" in member_names:
            routing_rules.append(
                "- If the user provides a specific URL starting with http:// or https://, "
                "call read_url directly. Do NOT delegate to web_search for URLs."
            )
        if "web_search" in member_names:
            routing_rules.append(
                "- If the user asks a question or wants to search the web without providing a URL, "
                "delegate ONLY to the web_search member."
            )
        if "weather" in member_names and "web_search" in member_names:
            routing_rules.append(
                "- If the user asks about weather, forecasts, temperature, rain, wind, "
                "thunderstorm risk or any meteorological topic, delegate ONLY to the "
                "Weather member. Do NOT call Web Search for weather questions."
            )
        if "gtasks" in member_names:
            routing_rules.append(
                "- For task management, to-do lists, or Google Tasks: delegate ONLY to the "
                "'gtasks' member. Trigger phrases: 'cosa ho da fare', 'todo', 'task', "
                "'aggiungi task', 'lista attività', 'segna come fatto', 'add task', 'show tasks'."
            )
        if routing_rules:
            system_prompt = (
                system_prompt
                + "\n\nROUTING RULES (follow strictly):\n"
                + "\n".join(routing_rules)
            )
        return agent_core.create_team(system_prompt, model, provider, members, tools=tools)
    return agent_core.create_agent(system_prompt, model, provider, tools=tools)


def _collect_member_responses(response) -> list:
    return getattr(response, "member_responses", [])


def _agents_label(member_responses: list) -> str:
    invoked = {mr.agent_name for mr in member_responses if mr.agent_name}
    parts = ["DRADIS"]
    if "web_search" in invoked: parts.append("Web Search")
    if "weather"    in invoked: parts.append("Weather")
    if "gcal"       in invoked: parts.append("Google Calendar")
    if "gmail"      in invoked: parts.append("Gmail")
    if "gtasks"     in invoked: parts.append("Google Tasks")
    return "🤖 " + " · ".join(parts)


def _is_failed_response(response) -> bool:
    try:
        status = getattr(response, "status", None)
        if status is not None:
            if status == "ERROR" or getattr(status, "value", None) == "ERROR":
                return True
        return not (response.content or "").strip()
    except Exception:
        return True


def _check_member_failures(response, settings: dict) -> list[str]:
    recoverable = []
    for mr in getattr(response, "member_responses", []):
        status = getattr(mr, "status", None)
        is_error = status is not None and (
            status == "ERROR" or getattr(status, "value", None) == "ERROR"
        )
        if is_error:
            name = getattr(mr, "agent_name", "")
            fb_key = _AGENT_TO_FB_MODEL_KEY.get(name, "")
            if fb_key and (settings.get(fb_key) or "").strip():
                recoverable.append(name)
    return recoverable


async def _run_with_fallback(
    executor,
    prompt: str,
    settings: dict,
    system_prompt: str,
    primary_model: str,
    primary_provider: str,
    context_label: str = "DRADIS",
) -> tuple:
    """Run executor with prompt; on exception or empty response retry with the fallback model.

    Returns (response, used_fallback: bool, error: Exception|None).
    error is set only when both primary and fallback fail.
    """
    response      = None
    primary_error = None
    member_failed = False

    try:
        response = await executor.arun(prompt)
    except Exception as e:
        primary_error = e
        print(f"[DRADIS] {context_label} primary arun exception: {e}")

    if response is not None and primary_error is None and _is_failed_response(response):
        status = getattr(response, "status", None)
        is_error_status = status is not None and (status == "ERROR" or getattr(status, "value", None) == "ERROR")
        reason = f"status={getattr(status, 'value', status)}" if is_error_status else "empty content"
        primary_error = RuntimeError(
            f"Model {primary_model!r} failed ({reason}): {(response.content or '').strip()[:200]}"
        )
        print(f"[DRADIS] {context_label} failed response ({reason}) — triggering fallback")

    if primary_error is None and response is not None:
        recoverable = _check_member_failures(response, settings)
        if recoverable:
            primary_error = RuntimeError(f"Sub-agent(s) failed: {', '.join(recoverable)}")
            member_failed = True
            print(f"[DRADIS] {context_label} member failure(s): {recoverable} — triggering fallback")

    if primary_error is None:
        return response, False, None

    fb_model = (settings.get("fallback_model") or "").strip()
    if not fb_model and not member_failed:
        return None, False, primary_error

    fb_settings = _apply_fallback_settings(settings)
    fb_model_id = fb_settings.get("model", primary_model)
    fb_provider = fb_settings.get("provider", primary_provider)
    fb_members  = _build_members(fb_settings)
    fb_executor = _build_executor(system_prompt, fb_model_id, fb_provider, fb_members, fb_settings)
    print(f"[DRADIS] {context_label} fallback: model={fb_model_id} provider={fb_provider}")

    try:
        response = await fb_executor.arun(prompt)
        if _is_failed_response(response):
            status = getattr(response, "status", None)
            is_error_status = status is not None and (status == "ERROR" or getattr(status, "value", None) == "ERROR")
            reason = f"status={getattr(status, 'value', status)}" if is_error_status else "empty content"
            raise RuntimeError(
                f"Fallback model {fb_model_id!r} also failed ({reason}): "
                f"{(response.content or '').strip()[:200]}"
            )
        return response, True, None
    except Exception as e2:
        print(f"[DRADIS] {context_label} fallback arun exception: {e2}")
        return None, True, e2


def _build_fallback_used_msg(settings: dict, primary_model: str, task_name: str | None = None) -> str:
    fb_settings = _apply_fallback_settings(settings)
    lines = []
    fb_main = fb_settings.get("model", primary_model)
    if fb_main != primary_model:
        lines.append(
            f"DRADIS: <code>{html.escape(primary_model)}</code> → <code>{html.escape(fb_main)}</code>"
        )
    for agent_name, model_key in [
        ("web_search", "ws_model"),
        ("weather",    "weather_model"),
        ("gcal",       "gcal_model"),
        ("gmail",      "gmail_model"),
        ("gtasks",     "gtasks_model"),
    ]:
        orig   = (settings.get(model_key) or "").strip()
        fb_val = (fb_settings.get(model_key) or "").strip()
        if orig and fb_val and fb_val != orig:
            lines.append(
                f"{agent_name}: <code>{html.escape(orig)}</code> → <code>{html.escape(fb_val)}</code>"
            )
    prefix = f"⚠️ Task <b>{html.escape(task_name)}</b>: " if task_name else "⚠️ "
    detail = "\n" + "\n".join(f"  • {l}" for l in lines) if lines else ""
    return f"{prefix}fallback triggered ✅{detail}"


# ── Voice transcription ───────────────────────────────────────────────────────

async def transcribe_voice(file_path: str, model: str, language: str) -> str:
    """Transcribe an OGG voice message to text using the Groq Whisper API."""
    if _groq_client is None:
        raise RuntimeError("Groq API key not configured — cannot transcribe voice message.")

    def _sync_transcribe():
        with open(file_path, "rb") as f:
            result = _groq_client.audio.transcriptions.create(
                file=(os.path.basename(file_path), f.read()),
                model=model,
                language=language,
                response_format="text",
            )
        return result.strip() if isinstance(result, str) else result.text.strip()

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_transcribe)
