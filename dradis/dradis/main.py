import asyncio
import html
import json
import os
import re
import tempfile
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import uvicorn
from groq import Groq as GroqClient
from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    MessageHandler,
    CommandHandler,
    filters,
    ContextTypes,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from web.server import (
    app as web_app,
    SETTINGS_DEFAULTS, SETTINGS_FILE, PROVIDERS,
    save_settings, SETTINGS_KEYS,
    register_tasks_changed_callback, register_run_task_callback, load_tasks,
    register_monitors_changed_callback, register_run_monitor_callback, load_monitors,
    register_live_monitors_changed_callback, register_live_monitor_status_callback, load_live_monitors,
    register_ha_monitors_changed_callback, register_ha_monitor_status_callback, load_ha_monitors,
    set_gcal_code_event, pop_gcal_pending_code,
    set_gmail_code_event, pop_gmail_pending_code,
    set_gtasks_code_event, pop_gtasks_pending_code,
)
import agent_core
from agents.gcal import GCAL_TOKEN_FILE, _build_gcal_flow, create_gcal_agent
from agents.gmail import GMAIL_TOKEN_FILE, _build_gmail_flow, create_gmail_agent
from agents.gtasks import GTASKS_TOKEN_FILE, _build_gtasks_flow, create_gtasks_agent
from agents.weather import create_weather_agent
from agents.web_search import create_web_search_agent
from agents.thunderstorm_monitor import run_thunderstorm_monitor
from agents.rain_monitor import run_rain_monitor
from agents.seismic_monitor import run_seismic_monitor
from agents.lightning_live_monitor import live_monitor_manager
from agents.ha_live_monitor import ha_monitor_manager
from agents.seismic_live_monitor import seismic_monitor_manager

WEB_PORT = 8099


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

_scheduler: AsyncIOScheduler = AsyncIOScheduler()
_telegram_bot = None
_main_loop: asyncio.AbstractEventLoop | None = None


async def _send_error_telegram(msg: str):
    global _telegram_bot
    if _telegram_bot:
        try:
            await _telegram_bot.send_message(
                chat_id=ALLOWED_CHAT_ID,
                text=msg,
                parse_mode=ParseMode.HTML,
            )
        except Exception as ex:
            print(f"[DRADIS] Could not send error notification: {ex}")


_FALLBACK_MAP = {
    "model":          ("fallback_model",          "provider",          "fallback_provider"),
    "ws_model":       ("ws_fallback_model",        "ws_provider",       "ws_fallback_provider"),

    "weather_model":  ("weather_fallback_model",   "weather_provider",  "weather_fallback_provider"),
    "gcal_model":     ("gcal_fallback_model",      "gcal_provider",     "gcal_fallback_provider"),
    "gmail_model":    ("gmail_fallback_model",     "gmail_provider",    "gmail_fallback_provider"),
    "gtasks_model":   ("gtasks_fallback_model",    "gtasks_provider",   "gtasks_fallback_provider"),
}

# Maps sub-agent name → the fallback model settings key for that agent
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

# ── Markdown → HTML ───────────────────────────────────────────────────────────

_FUNCTION_TAG_RE = re.compile(r'<function=[^>]+>.*?</function>', re.DOTALL)


def md_to_html(text: str) -> str:
    text = _FUNCTION_TAG_RE.sub('', text).strip()
    text = html.escape(text)
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'\*(.+?)\*',     r'<i>\1</i>', text)
    text = re.sub(r'`(.+?)`',       r'<code>\1</code>', text)
    return text


def build_system_prompt() -> str:
    settings     = read_settings()
    tz_name      = settings.get("timezone", "UTC") or "UTC"
    instructions = settings.get("agent_instructions", SETTINGS_DEFAULTS["agent_instructions"])
    return f"It is {agent_core._now_str(tz_name)} ({tz_name}).\n{instructions}"


# ── Conversation history ──────────────────────────────────────────────────────

_history: list[dict] = []


def _init_settings():
    if not SETTINGS_FILE.exists():
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(
            json.dumps(SETTINGS_DEFAULTS, ensure_ascii=False, indent=2)
        )


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


def read_settings() -> dict:
    result = dict(SETTINGS_DEFAULTS)
    try:
        raw = json.loads(SETTINGS_FILE.read_text())
        result.update({_LEGACY_SETTINGS_MAP.get(k, k): v for k, v in raw.items()})
    except Exception:
        pass
    return result


def save_turn(role: str, text: str, history_depth: int):
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


# ── read_url tool (no LLM — plain HTTP fetch via Jina Reader) ─────────────────

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


# ── Team / agent builder ──────────────────────────────────────────────────────


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
    """Return True when agno returned a failed or empty response.

    Agno never re-raises model errors (rate limit, provider errors, etc.) —
    it catches them internally, sets response.status = "ERROR", puts the error
    message in response.content, and returns normally. We must detect both cases:
    - status == "ERROR": explicit agno error (rate limit, provider error, …)
    - empty content: agno swallowed the error without setting status

    IMPORTANT: RunStatus is class RunStatus(str, Enum) with error = "ERROR".
    str(RunStatus.error) returns "RunStatus.error" in Python < 3.11, so we
    must compare directly via == or check .value — never use str() conversion.
    """
    try:
        status = getattr(response, "status", None)
        if status is not None:
            # Direct == uses str.__eq__ on the mixin value ("ERROR") — works on all Python versions
            if status == "ERROR" or getattr(status, "value", None) == "ERROR":
                return True
        return not (response.content or "").strip()
    except Exception:
        return True


def _check_member_failures(response, settings: dict) -> list[str]:
    """Return names of failed sub-agents that have a fallback model configured.

    Agno catches sub-agent LLM errors internally and sets the member response
    status to ERROR without propagating the failure to the top-level response.
    This helper inspects member_responses so _run_with_fallback can detect
    recoverable sub-agent failures and rebuild with fallback models.
    Only members with a fallback configured are returned (unrecoverable ones
    are ignored so we don't discard a valid partial response for nothing).
    """
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
    """Run *executor* with *prompt*; on exception OR empty response retry with
    the configured fallback model.

    Returns (response, used_fallback: bool, error: Exception|None).
    *error* is set only when both primary and fallback fail.
    """
    response      = None
    primary_error = None
    member_failed = False

    # ── Primary attempt ───────────────────────────────────────────────────────
    try:
        response = await executor.arun(prompt)
    except Exception as e:
        primary_error = e
        print(f"[DRADIS] {context_label} primary arun exception: {e}")

    # Agno never re-raises model errors — detect via status=="ERROR" or empty content
    if response is not None and primary_error is None and _is_failed_response(response):
        status = getattr(response, "status", None)
        is_error_status = status is not None and (status == "ERROR" or getattr(status, "value", None) == "ERROR")
        reason = f"status={getattr(status, 'value', status)}" if is_error_status else "empty content"
        primary_error = RuntimeError(
            f"Model {primary_model!r} failed ({reason}): {(response.content or '').strip()[:200]}"
        )
        print(f"[DRADIS] {context_label} failed response ({reason}) — triggering fallback")

    # Detect sub-agent failures even when DRADIS main response succeeded.
    # Agno catches member LLM errors internally (status=ERROR on member_responses)
    # without bubbling them up to the top-level response.
    if primary_error is None and response is not None:
        recoverable = _check_member_failures(response, settings)
        if recoverable:
            primary_error = RuntimeError(f"Sub-agent(s) failed: {', '.join(recoverable)}")
            member_failed = True
            print(f"[DRADIS] {context_label} member failure(s): {recoverable} — triggering fallback")

    if primary_error is None:
        return response, False, None

    # ── Fallback attempt ──────────────────────────────────────────────────────
    # DRADIS main failures require fallback_model to be set.
    # Sub-agent failures only need the specific agent's fallback (already confirmed above).
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
    """Build Telegram notification text describing which fallback models were activated."""
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


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle Telegram voice messages: transcribe via Groq Whisper then route as text."""
    if update.effective_user.id != ALLOWED_CHAT_ID:
        return

    settings = read_settings()
    if not settings.get("voice_enabled", False):
        await update.message.reply_text("🎙️ Voice agent is not enabled. You can enable it from the Web UI.")
        return

    voice_model     = settings.get("voice_model",    SETTINGS_DEFAULTS["voice_model"])
    voice_language  = settings.get("voice_language", SETTINGS_DEFAULTS["voice_language"])
    send_transcript = settings.get("voice_send_transcription", True)

    t0    = time.time()
    voice = update.message.voice

    try:
        tg_file = await context.bot.get_file(voice.file_id)
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp_path = tmp.name
        await tg_file.download_to_drive(tmp_path)
    except Exception as e:
        print(f"[DRADIS] Voice download error: {e}")
        await update.message.reply_text(
            f"❌ Could not download voice message: {html.escape(str(e))}",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        transcription = await transcribe_voice(tmp_path, voice_model, voice_language)
    except Exception as e:
        print(f"[DRADIS] Voice transcription error: {e}")
        await update.message.reply_text(
            f"❌ Transcription error: {html.escape(str(e))}",
            parse_mode=ParseMode.HTML,
        )
        return
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    duration = time.time() - t0
    print(f"[DRADIS] Voice transcribed in {duration:.1f}s: {transcription[:80]!r}")

    if send_transcript:
        await update.message.reply_text(
            f"🎙️ {html.escape(transcription)}",
            parse_mode=ParseMode.HTML,
        )

    class _VoiceMessage:
        def __init__(self, real_msg, text: str):
            self._msg = real_msg
            self.text = text

        def __getattr__(self, name):
            return getattr(self._msg, name)

    class _VoiceUpdate:
        def __init__(self, real_update: Update, text: str):
            self.effective_user = real_update.effective_user
            self.message        = _VoiceMessage(real_update.message, text)

    await handle_message(_VoiceUpdate(update, transcription), context)


# ── Scheduled Tasks ──────────────────────────────────────────────────────────

async def run_scheduled_task(task: dict):
    global _telegram_bot
    if not _telegram_bot:
        return
    task_name    = task.get("name", "Task")
    instructions = task.get("instructions", "").strip()
    if not instructions:
        return

    settings      = read_settings()
    system_prompt = build_system_prompt()
    model         = settings.get("model",    SETTINGS_DEFAULTS["model"])
    provider      = settings.get("provider", SETTINGS_DEFAULTS["provider"])

    members  = _build_members(settings)
    executor = _build_executor(system_prompt, model, provider, members, settings)
    print(f"[DRADIS] Scheduled task '{task_name}': model={model} members={[m.name for m in members]}")

    response, used_fallback, error = await _run_with_fallback(
        executor       = executor,
        prompt         = instructions,
        settings       = settings,
        system_prompt  = system_prompt,
        primary_model    = model,
        primary_provider = provider,
        context_label    = f"Task '{task_name}'",
    )

    if error is not None:
        fb_model_id = _apply_fallback_settings(settings).get("model", model) if used_fallback else model
        if used_fallback:
            await _send_error_telegram(
                f"❌ Task <b>{html.escape(task_name)}</b> — primary (<code>{html.escape(model)}</code>) "
                f"and fallback (<code>{html.escape(fb_model_id)}</code>) both failed: {html.escape(str(error))}"
            )
        else:
            await _send_error_telegram(
                f"❌ Task <b>{html.escape(task_name)}</b> failed (<code>{html.escape(model)}</code>): "
                f"{html.escape(str(error))}\n<i>No fallback model configured.</i>"
            )
        return

    if used_fallback:
        await _send_error_telegram(_build_fallback_used_msg(settings, model, task_name))

    member_responses = _collect_member_responses(response)
    text  = (response.content or "").strip()
    label = _agents_label(member_responses) + f" · <i>{html.escape(task_name)}</i>"

    if text:
        await _telegram_bot.send_message(
            chat_id=ALLOWED_CHAT_ID,
            text=md_to_html(text) + f"\n\n{label}",
            parse_mode=ParseMode.HTML,
        )


def _cron_task(task: dict):
    if _main_loop:
        asyncio.run_coroutine_threadsafe(run_scheduled_task(task), _main_loop)


def reload_task_jobs():
    tz = read_settings().get("timezone", "UTC") or "UTC"
    # Remove only task jobs (monitors have id prefixed with 'monitor:')
    for job in list(_scheduler.get_jobs()):
        if not job.id.startswith("monitor:"):
            job.remove()
    for task in load_tasks():
        if task.get("enabled") and task.get("cron"):
            try:
                _scheduler.add_job(
                    _cron_task,
                    CronTrigger.from_crontab(task["cron"], timezone=tz),
                    args=[task],
                    id=task["id"],
                    replace_existing=True,
                    misfire_grace_time=60,
                )
                print(f"[DRADIS] Scheduled task '{task['name']}' cron={task['cron']} tz={tz}")
            except Exception as e:
                print(f"[DRADIS] WARNING: invalid cron for task '{task.get('name')}': {e}")


# ── Monitors runner ──────────────────────────────────────────────────────────

_MONITOR_RUNNERS = {
    "thunderstorm": run_thunderstorm_monitor,
    "rain":         run_rain_monitor,
    "seismic":      run_seismic_monitor,
}


async def run_scheduled_monitor(monitor: dict):
    global _telegram_bot
    if not _telegram_bot:
        return
    monitor_name = monitor.get("name", "Monitor")
    monitor_type = monitor.get("type", "thunderstorm")
    runner = _MONITOR_RUNNERS.get(monitor_type)
    if not runner:
        await _send_error_telegram(
            f"⚠️ Monitor <b>{html.escape(monitor_name)}</b>: unknown type '{html.escape(monitor_type)}'"
        )
        return

    settings = read_settings()
    tz_name  = settings.get("timezone", "UTC") or "UTC"
    print(f"[DRADIS] Monitor '{monitor_name}' type={monitor_type} location={monitor.get('location')}")

    try:
        text = await runner(monitor, tz_name=tz_name)
    except Exception as e:
        exc_desc = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
        traceback.print_exc()
        print(f"[DRADIS] Monitor '{monitor_name}' error: {exc_desc}")
        await _send_error_telegram(
            f"❌ Monitor <b>{html.escape(monitor_name)}</b> failed: {html.escape(exc_desc)}"
        )
        return

    if text:
        try:
            await _telegram_bot.send_message(
                chat_id=ALLOWED_CHAT_ID,
                text=text,
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            print(f"[DRADIS] Monitor '{monitor_name}' send_message error: {e}")
            await _send_error_telegram(
                f"❌ Monitor <b>{html.escape(monitor_name)}</b> — errore invio report: {html.escape(str(e))}"
            )


def _cron_monitor(monitor: dict):
    if _main_loop:
        asyncio.run_coroutine_threadsafe(run_scheduled_monitor(monitor), _main_loop)


def reload_monitor_jobs():
    tz = read_settings().get("timezone", "UTC") or "UTC"
    # Remove only monitor jobs (tasks are prefixed differently)
    for job in list(_scheduler.get_jobs()):
        if job.id.startswith("monitor:"):
            job.remove()
    for monitor in load_monitors():
        if monitor.get("enabled") and monitor.get("cron"):
            try:
                _scheduler.add_job(
                    _cron_monitor,
                    CronTrigger.from_crontab(monitor["cron"], timezone=tz),
                    args=[monitor],
                    id=f"monitor:{monitor['id']}",
                    replace_existing=True,
                    misfire_grace_time=60,
                )
                print(f"[DRADIS] Scheduled monitor '{monitor['name']}' cron={monitor['cron']} tz={tz}")
            except Exception as e:
                print(f"[DRADIS] WARNING: invalid cron for monitor '{monitor.get('name')}': {e}")


def reload_live_monitors():
    settings = read_settings()
    tz_name  = settings.get("timezone", "UTC") or "UTC"

    async def _send(text: str):
        if _telegram_bot:
            await _telegram_bot.send_message(
                chat_id=ALLOWED_CHAT_ID,
                text=text,
                parse_mode=ParseMode.HTML,
            )

    configs = load_live_monitors()
    live_monitor_manager.reload(configs, _send, tz_name)
    seismic_monitor_manager.reload(configs, _send, tz_name)


def _live_status_dispatcher(monitor_id: str) -> str:
    cfg = next((m for m in load_live_monitors() if m["id"] == monitor_id), None)
    if cfg and cfg.get("type") == "seismic":
        return seismic_monitor_manager.status(monitor_id)
    return live_monitor_manager.status(monitor_id)


def reload_ha_monitors():
    settings = read_settings()
    tz_name  = settings.get("timezone", "UTC") or "UTC"
    mqtt_cfg = {k: settings[k] for k in [
        "mqtt_host", "mqtt_port", "mqtt_username", "mqtt_password", "mqtt_statestream_prefix"
    ] if k in settings}

    async def _send(text: str):
        if _telegram_bot:
            await _telegram_bot.send_message(
                chat_id=ALLOWED_CHAT_ID,
                text=text,
                parse_mode=ParseMode.HTML,
            )

    async def _llm(prompt: str) -> str:
        s          = read_settings()
        sys_prompt = build_system_prompt()
        model      = s.get("model",    SETTINGS_DEFAULTS["model"])
        provider   = s.get("provider", SETTINGS_DEFAULTS["provider"])
        members    = _build_members(s)
        executor   = _build_executor(sys_prompt, model, provider, members, s)
        response, _, error = await _run_with_fallback(
            executor         = executor,
            prompt           = prompt,
            settings         = s,
            system_prompt    = sys_prompt,
            primary_model    = model,
            primary_provider = provider,
            context_label    = "HAMonitor",
        )
        if error or response is None:
            return ""
        return (response.content or "").strip()

    ha_monitor_manager.reload(load_ha_monitors(), _send, _llm, mqtt_cfg, tz_name)


# ── Telegram handlers ─────────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_CHAT_ID:
        return
    settings        = read_settings()
    history_enabled = settings.get("history_enabled", True)
    history_depth   = settings.get("history_depth", 2)

    question      = update.message.text
    system_prompt = build_system_prompt()
    prompt        = build_context(question) if history_enabled else question

    model    = settings.get("model",    SETTINGS_DEFAULTS["model"])
    provider = settings.get("provider", SETTINGS_DEFAULTS["provider"])

    members  = _build_members(settings)
    executor = _build_executor(system_prompt, model, provider, members, settings)
    print(f"[DRADIS] model: {model} | members: {[m.name for m in members]}")

    response, used_fallback, error = await _run_with_fallback(
        executor         = executor,
        prompt           = prompt,
        settings         = settings,
        system_prompt    = system_prompt,
        primary_model    = model,
        primary_provider = provider,
        context_label    = "handle_message",
    )

    if error is not None:
        fb_model_id = _apply_fallback_settings(settings).get("model", model) if used_fallback else model
        if used_fallback:
            err_msg = (
                f"❌ Both primary (<code>{html.escape(model)}</code>) and "
                f"fallback (<code>{html.escape(fb_model_id)}</code>) models failed: "
                f"{html.escape(str(error))}"
            )
            await _send_error_telegram(err_msg)
            await update.message.reply_text(err_msg, parse_mode=ParseMode.HTML)
        else:
            err_msg = (
                f"❌ Model error (<code>{html.escape(model)}</code>): {html.escape(str(error))}\n"
                "<i>No fallback model configured.</i>"
            )
            await _send_error_telegram(err_msg)
            await update.message.reply_text(err_msg, parse_mode=ParseMode.HTML)
        return

    if used_fallback:
        await _send_error_telegram(_build_fallback_used_msg(settings, model))

    member_responses = _collect_member_responses(response)
    text = (response.content or "").strip()

    if history_enabled:
        save_turn("user", question, history_depth)
        save_turn("assistant", text, history_depth)

    agents_label = _agents_label(member_responses)

    if text:
        await update.message.reply_text(
            md_to_html(text) + f"\n\n<i>{agents_label}</i>",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text(
            f"⚠️ Model <code>{html.escape(model)}</code> returned no text (tool-call only response).\n\n<i>{agents_label}</i>",
            parse_mode=ParseMode.HTML,
        )


async def cmd_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_CHAT_ID:
        return
    settings = read_settings()

    lines = [
        "<b>DRADIS</b>",
        f"Provider: {settings.get('provider', SETTINGS_DEFAULTS['provider'])}",
        f"Model: {settings.get('model', SETTINGS_DEFAULTS['model'])}",
        f"History: {'on' if settings.get('history_enabled', True) else 'off'} "
        f"({settings.get('history_depth', SETTINGS_DEFAULTS['history_depth'])} exchanges)",
    ]

    ws_on = settings.get("ws_enabled", False)
    lines += ["", "<b>Web Search</b>", f"Status: {'enabled' if ws_on else 'disabled'}"]
    if ws_on:
        lines.append(f"Model: {settings.get('ws_model', SETTINGS_DEFAULTS['ws_model'])}")

    weather_on = settings.get("weather_enabled", False)
    lines += ["", "<b>Weather</b>", f"Status: {'enabled' if weather_on else 'disabled'}"]
    if weather_on:
        lines.append(f"Model: {settings.get('weather_model', SETTINGS_DEFAULTS['weather_model'])}")

    voice_on = settings.get("voice_enabled", False)
    lines += ["", "<b>Voice</b>", f"Status: {'enabled' if voice_on else 'disabled'}"]
    if voice_on:
        lines.append(f"Model: {settings.get('voice_model', SETTINGS_DEFAULTS['voice_model'])}")
        lines.append(f"Language: {settings.get('voice_language', SETTINGS_DEFAULTS['voice_language'])}")

    gcal_on   = settings.get("gcal_enabled", False)
    gcal_auth = GCAL_TOKEN_FILE.exists()
    lines += ["", "<b>Google Calendar</b>", f"Status: {'enabled' if gcal_on else 'disabled'}"]
    if gcal_on:
        lines.append(f"Provider: {settings.get('gcal_provider', SETTINGS_DEFAULTS['gcal_provider'])}")
        lines.append(f"Model: {settings.get('gcal_model', SETTINGS_DEFAULTS['gcal_model'])}")
        lines.append(f"Auth: {'✅ connected' if gcal_auth else '❌ not authenticated — send /gcalauth'}")

    gmail_on   = settings.get("gmail_enabled", False)
    gmail_auth = GMAIL_TOKEN_FILE.exists()
    lines += ["", "<b>Gmail</b>", f"Status: {'enabled' if gmail_on else 'disabled'}"]
    if gmail_on:
        lines.append(f"Provider: {settings.get('gmail_provider', SETTINGS_DEFAULTS['gmail_provider'])}")
        lines.append(f"Model: {settings.get('gmail_model', SETTINGS_DEFAULTS['gmail_model'])}")
        lines.append(f"Auth: {'✅ connected' if gmail_auth else '❌ not authenticated — send /gmailauth'}")

    gtasks_on   = settings.get("gtasks_enabled", False)
    gtasks_auth = GTASKS_TOKEN_FILE.exists()
    lines += ["", "<b>Google Tasks</b>", f"Status: {'enabled' if gtasks_on else 'disabled'}"]
    if gtasks_on:
        lines.append(f"Provider: {settings.get('gtasks_provider', SETTINGS_DEFAULTS['gtasks_provider'])}")
        lines.append(f"Model: {settings.get('gtasks_model', SETTINGS_DEFAULTS['gtasks_model'])}")
        lines.append(f"Auth: {'✅ connected' if gtasks_auth else '❌ not authenticated — send /gtasksauth'}")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def _gcal_complete_auth(flow, code: str, message) -> bool:
    from web.server import _gcal_code_event as _ev
    global _gcal_pending_flow
    try:
        loop  = asyncio.get_event_loop()
        creds = await loop.run_in_executor(
            None,
            lambda: (flow.fetch_token(code=code), flow.credentials)[1],
        )
        GCAL_TOKEN_FILE.write_text(creds.to_json())
        _gcal_pending_flow = None
        if _ev and not _ev.is_set():
            _ev.set()
        await message.reply_text(
            "✅ <b>Google Calendar connected!</b> You can now ask DRADIS about your calendar.",
            parse_mode=ParseMode.HTML,
        )
        return True
    except Exception as e:
        await message.reply_text(
            f"❌ Authorization failed: {html.escape(str(e))}",
            parse_mode=ParseMode.HTML,
        )
        return False


async def _gcal_auth_background(event: asyncio.Event, flow, message):
    try:
        await asyncio.wait_for(event.wait(), timeout=300)
        code = pop_gcal_pending_code()
        if code and not GCAL_TOKEN_FILE.exists():
            await _gcal_complete_auth(flow, code, message)
    except asyncio.TimeoutError:
        if not GCAL_TOKEN_FILE.exists():
            await message.reply_text("⏱ Authorization timed out (5 min). Send /gcalauth to try again.")


_gcal_pending_flow = None


async def cmd_gcalauth(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _gcal_pending_flow
    if update.effective_user.id != ALLOWED_CHAT_ID:
        return

    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        await update.message.reply_text(
            "❌ <code>google_client_id</code> and <code>google_client_secret</code> are not configured.\n"
            "Add them in the add-on <b>Configuration</b> tab and restart.",
            parse_mode=ParseMode.HTML,
        )
        return

    args = context.args or []

    if not args:
        event = asyncio.Event()
        set_gcal_code_event(event)
        flow = _build_gcal_flow(GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET)
        _gcal_pending_flow = flow
        auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent")

        msg = (
            "📅 <b>Google Calendar — Authorization</b>\n\n"
            "1. Open this link in your browser:\n"
            f"<code>{html.escape(auth_url)}</code>\n\n"
            "2. Sign in with your Google account and grant access.\n"
            "3. Your browser will redirect back to DRADIS automatically ✅\n\n"
            "<i>If the redirect fails (HA on a different device), copy the full URL "
            "from the browser address bar and send it as:\n"
            "/gcalauth &lt;url&gt;</i>"
        )
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
        asyncio.create_task(_gcal_auth_background(event, flow, update.message))
        return

    raw  = " ".join(args)
    code = parse_qs(urlparse(raw).query).get("code", [raw])[0]

    if not code:
        await update.message.reply_text(
            "❌ Could not parse the authorization code. "
            "Make sure you copied the full redirect URL.",
            parse_mode=ParseMode.HTML,
        )
        return

    flow = _gcal_pending_flow or _build_gcal_flow(GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET)
    await _gcal_complete_auth(flow, code, update.message)


async def _gmail_complete_auth(flow, code: str, message) -> bool:
    from web.server import _gmail_code_event as _ev
    global _gmail_pending_flow
    try:
        loop  = asyncio.get_event_loop()
        creds = await loop.run_in_executor(
            None,
            lambda: (flow.fetch_token(code=code), flow.credentials)[1],
        )
        GMAIL_TOKEN_FILE.write_text(creds.to_json())
        _gmail_pending_flow = None
        if _ev and not _ev.is_set():
            _ev.set()
        await message.reply_text(
            "✅ <b>Gmail connected!</b> You can now ask DRADIS to read or send emails.",
            parse_mode=ParseMode.HTML,
        )
        return True
    except Exception as e:
        await message.reply_text(
            f"❌ Authorization failed: {html.escape(str(e))}",
            parse_mode=ParseMode.HTML,
        )
        return False


async def _gmail_auth_background(event: asyncio.Event, flow, message):
    try:
        await asyncio.wait_for(event.wait(), timeout=300)
        code = pop_gmail_pending_code()
        if code and not GMAIL_TOKEN_FILE.exists():
            await _gmail_complete_auth(flow, code, message)
    except asyncio.TimeoutError:
        if not GMAIL_TOKEN_FILE.exists():
            await message.reply_text("⏱ Authorization timed out (5 min). Send /gmailauth to try again.")


_gmail_pending_flow = None


async def cmd_gmailauth(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _gmail_pending_flow
    if update.effective_user.id != ALLOWED_CHAT_ID:
        return

    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        await update.message.reply_text(
            "❌ <code>google_client_id</code> and <code>google_client_secret</code> are not configured.\n"
            "Add them in the add-on <b>Configuration</b> tab and restart.",
            parse_mode=ParseMode.HTML,
        )
        return

    args = context.args or []

    if not args:
        event = asyncio.Event()
        set_gmail_code_event(event)
        flow = _build_gmail_flow(GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET)
        _gmail_pending_flow = flow
        auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent")

        msg = (
            "📧 <b>Gmail — Authorization</b>\n\n"
            "1. Open this link in your browser:\n"
            f"<code>{html.escape(auth_url)}</code>\n\n"
            "2. Sign in with your Google account and grant access.\n"
            "3. Your browser will redirect back to DRADIS automatically ✅\n\n"
            "<i>If the redirect fails (HA on a different device), copy the full URL "
            "from the browser address bar and send it as:\n"
            "/gmailauth &lt;url&gt;</i>"
        )
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
        asyncio.create_task(_gmail_auth_background(event, flow, update.message))
        return

    raw  = " ".join(args)
    code = parse_qs(urlparse(raw).query).get("code", [raw])[0]

    if not code:
        await update.message.reply_text(
            "❌ Could not parse the authorization code. "
            "Make sure you copied the full redirect URL.",
            parse_mode=ParseMode.HTML,
        )
        return

    flow = _gmail_pending_flow or _build_gmail_flow(GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET)
    await _gmail_complete_auth(flow, code, update.message)


async def _gtasks_complete_auth(flow, code: str, message) -> bool:
    from web.server import _gtasks_code_event as _ev
    global _gtasks_pending_flow
    try:
        loop  = asyncio.get_event_loop()
        creds = await loop.run_in_executor(
            None,
            lambda: (flow.fetch_token(code=code), flow.credentials)[1],
        )
        GTASKS_TOKEN_FILE.write_text(creds.to_json())
        _gtasks_pending_flow = None
        if _ev and not _ev.is_set():
            _ev.set()
        await message.reply_text(
            "✅ <b>Google Tasks connected!</b> You can now ask DRADIS about your tasks.",
            parse_mode=ParseMode.HTML,
        )
        return True
    except Exception as e:
        await message.reply_text(
            f"❌ Authorization failed: {html.escape(str(e))}",
            parse_mode=ParseMode.HTML,
        )
        return False


async def _gtasks_auth_background(event: asyncio.Event, flow, message):
    try:
        await asyncio.wait_for(event.wait(), timeout=300)
        code = pop_gtasks_pending_code()
        if code and not GTASKS_TOKEN_FILE.exists():
            await _gtasks_complete_auth(flow, code, message)
    except asyncio.TimeoutError:
        if not GTASKS_TOKEN_FILE.exists():
            await message.reply_text("⏱ Authorization timed out (5 min). Send /gtasksauth to try again.")


_gtasks_pending_flow = None


async def cmd_gtasksauth(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _gtasks_pending_flow
    if update.effective_user.id != ALLOWED_CHAT_ID:
        return

    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        await update.message.reply_text(
            "❌ <code>google_client_id</code> and <code>google_client_secret</code> are not configured.\n"
            "Add them in the add-on <b>Configuration</b> tab and restart.",
            parse_mode=ParseMode.HTML,
        )
        return

    args = context.args or []

    if not args:
        event = asyncio.Event()
        set_gtasks_code_event(event)
        flow = _build_gtasks_flow(GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET)
        _gtasks_pending_flow = flow
        auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent")

        msg = (
            "📝 <b>Google Tasks — Authorization</b>\n\n"
            "1. Open this link in your browser:\n"
            f"<code>{html.escape(auth_url)}</code>\n\n"
            "2. Sign in with your Google account and grant access.\n"
            "3. Your browser will redirect back to DRADIS automatically ✅\n\n"
            "<i>If the redirect fails (HA on a different device), copy the full URL "
            "from the browser address bar and send it as:\n"
            "/gtasksauth &lt;url&gt;</i>"
        )
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
        asyncio.create_task(_gtasks_auth_background(event, flow, update.message))
        return

    raw  = " ".join(args)
    code = parse_qs(urlparse(raw).query).get("code", [raw])[0]

    if not code:
        await update.message.reply_text(
            "❌ Could not parse the authorization code. "
            "Make sure you copied the full redirect URL.",
            parse_mode=ParseMode.HTML,
        )
        return

    flow = _gtasks_pending_flow or _build_gtasks_flow(GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET)
    await _gtasks_complete_auth(flow, code, update.message)


async def cmd_todo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_CHAT_ID:
        return
    settings = read_settings()
    if not settings.get("gtasks_enabled", False):
        await update.message.reply_text(
            "📝 Google Tasks is not enabled. Enable it from the Web UI.",
        )
        return
    if not GTASKS_TOKEN_FILE.exists():
        await update.message.reply_text(
            "📝 Google Tasks not authenticated. Send /gtasksauth to connect.",
        )
        return
    agent = create_gtasks_agent(settings)
    try:
        response = await agent.arun("List all open tasks")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {html.escape(str(e))}")
        return
    text = (response.content or "").strip()
    if text:
        await update.message.reply_text(
            md_to_html(text) + "\n\n<i>🤖 DRADIS · Google Tasks</i>",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text("📭 No open tasks.")


COMMANDS = [
    BotCommand("info",       "Status and configuration of all agents"),
    BotCommand("menu",       "List all available commands"),
    BotCommand("tasks",      "List and run enabled tasks"),
    BotCommand("monitors",   "List and run enabled monitors"),
    BotCommand("gcalauth",   "Connect Google Calendar (OAuth2)"),
    BotCommand("gmailauth",  "Connect Gmail (OAuth2)"),
    BotCommand("gtasksauth", "Connect Google Tasks (OAuth2)"),
    BotCommand("todo",       "List open Google Tasks"),
]


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_CHAT_ID:
        return
    lines = "\n".join(f"/{c.command} — {c.description}" for c in COMMANDS)
    await update.message.reply_text(f"<b>DRADIS Commands:</b>\n\n{lines}", parse_mode=ParseMode.HTML)


async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_CHAT_ID:
        return
    tasks = [t for t in load_tasks() if t.get("enabled")]
    if not tasks:
        await update.message.reply_text("No enabled tasks. Enable tasks from the Web UI.")
        return
    keyboard = [[InlineKeyboardButton(t["name"], callback_data=f"task:{t['id']}")] for t in tasks]
    await update.message.reply_text(
        "Select a task to run:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def cmd_monitors(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_CHAT_ID:
        return
    scheduled = [m for m in load_monitors() if m.get("enabled")]
    live      = [m for m in load_live_monitors() if m.get("enabled")]
    if not scheduled and not live:
        await update.message.reply_text("No enabled monitors. Enable monitors from the Web UI.")
        return
    keyboard = []
    for m in scheduled:
        detail = m.get("seismic_area", "?") if m.get("type") == "seismic" else m.get("location", "?")
        keyboard.append([InlineKeyboardButton(
            f"{m['name']} ({detail})",
            callback_data=f"monitor:{m['id']}",
        )])
    for m in live:
        status = _live_status_dispatcher(m["id"])
        badge  = "🟢" if status == "running" else "🔴"
        if m.get("type") == "seismic":
            areas = ", ".join(m.get("areas", [])) or "—"
            label = f"{badge} {m['name']} ({areas})"
        else:
            label = f"{badge} {m['name']} ({m.get('location', '?')})"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"live:{m['id']}")])
    header = "Scheduled monitors — tap to run now:" if scheduled else ""
    if live:
        header += ("\n\n" if header else "") + "Live monitors — tap to see status:"
    await update.message.reply_text(
        header.strip(),
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handle_task_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != ALLOWED_CHAT_ID:
        await query.answer()
        return
    task_id = query.data.removeprefix("task:")
    task    = next((t for t in load_tasks() if t["id"] == task_id), None)
    await query.answer()
    if not task:
        await query.message.reply_text("❌ Task not found.")
        return
    await query.message.reply_text(
        f"▶️ Launching task <b>{html.escape(task['name'])}</b>…",
        parse_mode=ParseMode.HTML,
    )
    asyncio.create_task(run_scheduled_task(task))


async def handle_monitor_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != ALLOWED_CHAT_ID:
        await query.answer()
        return
    monitor_id = query.data.removeprefix("monitor:")
    monitor    = next((m for m in load_monitors() if m["id"] == monitor_id), None)
    await query.answer()
    if not monitor:
        await query.message.reply_text("❌ Monitor not found.")
        return
    detail = (
        monitor.get("seismic_area", "?")
        if monitor.get("type") == "seismic"
        else monitor.get("location", "?")
    )
    await query.message.reply_text(
        f"▶️ Launching monitor <b>{html.escape(monitor['name'])}</b> "
        f"({html.escape(detail)})…",
        parse_mode=ParseMode.HTML,
    )
    asyncio.create_task(run_scheduled_monitor(monitor))


async def handle_live_monitor_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != ALLOWED_CHAT_ID:
        await query.answer()
        return
    item_id = query.data.removeprefix("live:")
    monitor = next((m for m in load_live_monitors() if m["id"] == item_id), None)
    await query.answer()
    if not monitor:
        await query.message.reply_text("❌ Live monitor not found.")
        return
    mtype  = monitor.get("type", "lightning")
    status = _live_status_dispatcher(item_id)
    badge  = "🟢 Running" if status == "running" else "🔴 Stopped"
    if mtype == "seismic":
        areas = ", ".join(monitor.get("areas", [])) or "—"
        msg = (f"🌍 <b>{html.escape(monitor['name'])}</b>\n"
               f"Aree: {html.escape(areas)}\n"
               f"Status: {badge}\n"
               f"Polling: 60s — DB: /data/seismic.db")
    else:
        msg = (f"⚡ <b>{html.escape(monitor['name'])}</b>\n"
               f"📍 {html.escape(monitor.get('location', '?'))}\n"
               f"Status: {badge}\n"
               f"Radius: {monitor.get('radius_km', '?')} km — Cooldown: automatic (5/15/30 min)")
    await query.message.reply_text(msg, parse_mode=ParseMode.HTML)


# ── Entrypoint ────────────────────────────────────────────────────────────────

def build_telegram_app():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("info",       cmd_info))
    app.add_handler(CommandHandler("menu",       cmd_menu))
    app.add_handler(CommandHandler("tasks",      cmd_tasks))
    app.add_handler(CommandHandler("monitors",   cmd_monitors))
    app.add_handler(CommandHandler("gcalauth",   cmd_gcalauth))
    app.add_handler(CommandHandler("gmailauth",  cmd_gmailauth))
    app.add_handler(CommandHandler("gtasksauth", cmd_gtasksauth))
    app.add_handler(CommandHandler("todo",       cmd_todo))
    app.add_handler(CallbackQueryHandler(handle_task_callback,         pattern=r"^task:"))
    app.add_handler(CallbackQueryHandler(handle_monitor_callback,      pattern=r"^monitor:"))
    app.add_handler(CallbackQueryHandler(handle_live_monitor_callback, pattern=r"^live:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    return app


async def _register_commands(bot):
    try:
        await bot.delete_my_commands()
        await bot.set_my_commands(COMMANDS)
        print(f"[DRADIS] Telegram commands registered: {[c.command for c in COMMANDS]}")
    except Exception as e:
        print(f"[DRADIS] WARNING: could not register commands: {e}")


async def main():
    global _telegram_bot, _main_loop
    _init_settings()
    _main_loop = asyncio.get_running_loop()
    telegram_app = build_telegram_app()
    web_server   = uvicorn.Server(
        uvicorn.Config(web_app, host="0.0.0.0", port=WEB_PORT, log_level="warning")
    )
    async with telegram_app:
        await telegram_app.start()
        await telegram_app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        await _register_commands(telegram_app.bot)
        _telegram_bot = telegram_app.bot
        _scheduler.start()
        reload_task_jobs()
        reload_monitor_jobs()
        register_tasks_changed_callback(reload_task_jobs)
        register_run_task_callback(run_scheduled_task)
        register_monitors_changed_callback(reload_monitor_jobs)
        register_run_monitor_callback(run_scheduled_monitor)
        register_live_monitors_changed_callback(reload_live_monitors)
        register_live_monitor_status_callback(_live_status_dispatcher)
        reload_live_monitors()
        register_ha_monitors_changed_callback(reload_ha_monitors)
        register_ha_monitor_status_callback(ha_monitor_manager.status)
        reload_ha_monitors()
        settings    = read_settings()
        startup_msg = settings.get("startup_message", SETTINGS_DEFAULTS["startup_message"])
        await telegram_app.bot.send_message(chat_id=ALLOWED_CHAT_ID, text=startup_msg)
        print(f"[DRADIS] Started. Web UI on port {WEB_PORT}.")
        await web_server.serve()
        _scheduler.shutdown(wait=False)
        await telegram_app.updater.stop()
        await telegram_app.stop()


if __name__ == "__main__":
    asyncio.run(main())
