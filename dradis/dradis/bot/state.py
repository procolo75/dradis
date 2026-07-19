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
from agents.gcal    import GCAL_TOKEN_FILE, gcal_tools
from agents.gmail   import GMAIL_TOKEN_FILE, gmail_tools
from agents.gtasks  import GTASKS_TOKEN_FILE, gtasks_tools
from agents.weather    import weather_tools
from agents.web_search import web_search_tools

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
TAVILY_API_KEY        = _startup_options.get("tavily_api_key", "")
GOOGLE_CLIENT_ID      = _startup_options.get("google_client_id", "")
GOOGLE_CLIENT_SECRET  = _startup_options.get("google_client_secret", "")
RAPIDAPI_FOOTBALL_KEY = _startup_options.get("rapidapi_football_key", "")
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


def history_messages() -> list[dict]:
    """Return prior turns as OpenAI chat messages (role/content) for the runtime."""
    return [{"role": m["role"], "content": m["content"]} for m in _history]


# ── Telegram helpers ──────────────────────────────────────────────────────────

async def send_telegram(text: str, bot_id: str = "default",
                        parse_mode: str = ParseMode.HTML) -> bool:
    """Send a Telegram message. Returns True on confirmed delivery, False otherwise.
    Callers that need to react to delivery failure (e.g. live monitors that gate
    state flags on a successful send) must inspect the return value."""
    bot, chat_id = get_bot_and_chat(bot_id)
    if not bot:
        return False
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode)
        return True
    except Exception as ex:
        print(f"[DRADIS] send_telegram(bot_id={bot_id!r}) error: {ex}")
        return False


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


# ── Tool registry ─────────────────────────────────────────────────────────────
#
# DRADIS is ONE agent that owns a flat set of tools (no coordinator, no
# sub-agents). Each capability contributes tool specs; which ones are attached
# depends on: enabled flag + auth (for chat, all available) and, for a task, the
# explicit per-tool selection. The model decides which tool to call.

async def read_url(url: str) -> str:
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


READ_URL_TOOL = {
    "name": "read_url", "fn": read_url, "capability": None,
    "description": "Fetch and return the text content of a web page. Call this only when the user explicitly provides an http:// or https:// URL. Do NOT call it for questions or search queries.",
    "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
}

# Capability metadata: id + UI label + settings key holding extra instructions.
CAPABILITIES = [
    {"id": "web_search", "label": "Web Search",     "instr": "ws_instructions"},
    {"id": "weather",    "label": "Weather",         "instr": "weather_instructions"},
    {"id": "gcal",       "label": "Google Calendar", "instr": "gcal_instructions"},
    {"id": "gmail",      "label": "Gmail",           "instr": "gmail_instructions"},
    {"id": "gtasks",     "label": "Google Tasks",    "instr": "gtasks_instructions"},
]


def _capability_tool_groups(settings: dict) -> dict:
    """Return {capability_id: [tool specs]} for enabled + authenticated capabilities."""
    groups: dict = {}
    if settings.get("ws_enabled") and TAVILY_API_KEY:
        groups["web_search"] = web_search_tools(settings, TAVILY_API_KEY)
    if settings.get("weather_enabled"):
        groups["weather"] = weather_tools(settings)
    if settings.get("gcal_enabled") and GCAL_TOKEN_FILE.exists():
        groups["gcal"] = gcal_tools(settings)
    if settings.get("gmail_enabled") and GMAIL_TOKEN_FILE.exists():
        groups["gmail"] = gmail_tools(settings)
    if settings.get("gtasks_enabled") and GTASKS_TOKEN_FILE.exists():
        groups["gtasks"] = gtasks_tools(settings)
    for cap_id, specs in groups.items():
        for t in specs:
            t["capability"] = cap_id
    return groups


def build_tools(settings: dict, selected=None) -> list[dict]:
    """Return the tool specs to attach.

    selected: None or ["*"] → all available; a list of tool names and/or
    capability ids → just those; [] → no tools.
    """
    groups = _capability_tool_groups(settings)
    flat: list[dict] = []
    for specs in groups.values():
        flat.extend(specs)
    if settings.get("read_url_enabled"):
        flat.append(READ_URL_TOOL)
    if selected is None or (isinstance(selected, list) and "*" in selected):
        return flat
    sel = set(selected)
    return [t for t in flat if t["name"] in sel or (t.get("capability") and t["capability"] in sel)]


def available_tools(settings: dict) -> list[dict]:
    """Flat tool catalogue for the Web UI (only enabled + authenticated tools)."""
    groups = _capability_tool_groups(settings)
    out = []
    for cap in CAPABILITIES:
        for t in groups.get(cap["id"], []):
            out.append({"capability": cap["id"], "capability_label": cap["label"],
                        "name": t["name"], "description": t["description"]})
    if settings.get("read_url_enabled"):
        out.append({"capability": "read_url", "capability_label": "Read URL",
                    "name": "read_url", "description": READ_URL_TOOL["description"]})
    return out


def task_tool_selection(task: dict):
    """Resolve a task's tool selection. Supports the new `tools` field (list of
    tool names / capability ids) and the legacy `agents` field. None/["*"] = all."""
    sel = task.get("tools")
    if sel is None:
        sel = task.get("agents")
    if sel is None or (isinstance(sel, list) and "*" in sel):
        return None
    return sel


def _system_prompt(settings: dict, tools: list[dict]) -> str:
    base    = build_system_prompt()  # time + agent_instructions (reads settings itself)
    caps_in = {t.get("capability") for t in tools if t.get("capability")}
    extra   = []
    for cap in CAPABILITIES:
        if cap["id"] in caps_in:
            instr = (settings.get(cap["instr"]) or "").strip()
            if instr:
                extra.append(instr)
    if any(t["name"] == "read_url" for t in tools):
        extra.append("When the user gives an http:// or https:// URL, call read_url to fetch the page and answer from it.")
    return base + ("\n\n" + "\n".join(extra) if extra else "")


# ── Runner (single agent, one model, with fallback) ───────────────────────────

def _apply_fallback_settings(settings: dict) -> dict:
    """Swap the main model/provider for the configured fallback (used for messaging)."""
    s = dict(settings)
    fb_model = (s.get("fallback_model") or "").strip()
    fb_prov  = (s.get("fallback_provider") or "").strip()
    if fb_model:
        s["model"] = fb_model
        if fb_prov:
            s["provider"] = fb_prov
    return s


async def run_dradis(
    user_prompt: str,
    settings: dict,
    *,
    selected=None,
    history: list[dict] | None = None,
    context_label: str = "DRADIS",
) -> tuple:
    """Run DRADIS as ONE agent with the selected tools, retrying once on the
    fallback model.

    Returns (AgentResult|None, used_fallback: bool, error|None, fb_reason|None)
    where fb_reason is the primary error that triggered the fallback (set whenever
    the fallback ran, whether it then succeeded or not)."""
    agent_core.set_generation_config(settings.get("max_tokens"))
    max_tokens = settings.get("max_tokens") or None
    model      = settings.get("model",    SETTINGS_DEFAULTS["model"])
    provider   = settings.get("provider", SETTINGS_DEFAULTS["provider"])
    tools      = build_tools(settings, selected)
    system     = _system_prompt(settings, tools)

    tool_names = ", ".join(t["name"] for t in tools) or "none"
    print(f"[DRADIS] {context_label}: model={model} provider={provider} "
          f"tools={len(tools)} [{tool_names}] window={agent_core.context_window_for(model)}")

    async def _attempt(m: str, p: str):
        return await agent_core.run_agent(
            system, user_prompt, tools, m, p,
            max_tokens=max_tokens, history=history,
        )

    error = None
    try:
        res = await _attempt(model, provider)
        if not res.failed:
            print(f"[DRADIS] {context_label} ok · prompt_tokens={res.prompt_tokens} "
                  f"completion={res.completion_tokens} tools_used={res.tools_used}")
            return res, False, None, None
        error = RuntimeError(f"Model {model!r} returned empty content.")
    except Exception as e:
        error = e
        print(f"[DRADIS] {context_label} primary error: {e}")

    fb_model = (settings.get("fallback_model") or "").strip()
    if not fb_model:
        return None, False, error, None
    fb_provider = (settings.get("fallback_provider") or "").strip() or provider
    print(f"[DRADIS] {context_label} fallback → {fb_model} ({fb_provider})")
    try:
        res = await _attempt(fb_model, fb_provider)
        if res.failed:
            raise RuntimeError(f"Fallback model {fb_model!r} returned empty content.")
        print(f"[DRADIS] {context_label} fallback ok · prompt_tokens={res.prompt_tokens}")
        return res, True, None, error
    except Exception as e2:
        print(f"[DRADIS] {context_label} fallback error: {e2}")
        return None, True, e2, error


def reply_footer(settings: dict, result) -> str:
    """Optional per-message footer appended to chat & task replies. Each line is
    gated by its own setting: 'Log token usage' adds input/output token counts,
    'Log tools used' adds the tools DRADIS called this turn (deduped, in order).
    Returns a ready-to-append HTML fragment (leading blank line + <i>…</i>), or "".
    """
    if result is None:
        return ""
    lines: list[str] = []
    if settings.get("token_usage_enabled"):
        lines.append(f"🔢 in {result.prompt_tokens} · out {result.completion_tokens}")
    if settings.get("tools_usage_enabled"):
        used = list(dict.fromkeys(result.tools_used or []))
        lines.append("🔧 " + (", ".join(used) if used else "no tools"))
    if not lines:
        return ""
    # Telegram HTML parse_mode supports only a small tag set (no <br>); use a real
    # newline to separate the footer lines inside the italic block.
    return "\n\n<i>" + "\n".join(lines) + "</i>"


def _fallback_msg(reason, task_name: str | None = None) -> str:
    """Telegram note when the fallback model was used — just that it triggered and
    the error that caused it."""
    prefix = f"⚠️ Task <b>{html.escape(task_name)}</b>: " if task_name else "⚠️ "
    return f"{prefix}fallback triggered — {html.escape(str(reason))}"


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
