import asyncio
import html
import json
import os
import re
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import uvicorn
from groq import Groq as GroqClient
from telegram import Update, BotCommand
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
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
    set_gcal_code_event, pop_gcal_pending_code,
    set_gmail_code_event, pop_gmail_pending_code,
)
import agent_core
from agents.gcal import GCAL_TOKEN_FILE, _build_gcal_flow, create_gcal_agent, fetch_gcal_events
from agents.gmail import GMAIL_TOKEN_FILE, _build_gmail_flow, create_gmail_agent, fetch_gmail_inbox
from agents.weather import create_weather_agent, fetch_weather
from agents.web_search import create_web_search_agent, fetch_web_search

WEB_PORT = 8099

# Keyword sets used for intent-based pre-fetching (avoids LLM tool-decision call)
_WEATHER_KW = {"weather","meteo","forecast","temperatura","previsioni","pioggia","vento","sole","caldo","freddo","neve","umidità","uv"}
_WS_KW      = {"search","cerca","news","notizie","trova","recenti","internet","latest","aggiornamenti","article","articolo"}
_GCAL_KW    = {"calendar","agenda","appuntamento","evento","schedule","meeting","riunione","domani","settimana","oggi","eventi","impegni"}
_GMAIL_KW   = {"email","mail","inbox","posta","messaggio","gmail","unread","non lette","mittente","oggetto"}

_WEATHER_LOCATION_RE = re.compile(
    r'(?:meteo|weather|forecast|previsioni)\s+(?:a|in|di|for|at)\s+([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ\s]{1,30}?)(?:\s*[?!,.]|\s+(?:oggi|domani|this|tomorrow|next)|\s*$)',
    re.IGNORECASE,
)


def _extract_weather_location(text: str) -> str | None:
    m = _WEATHER_LOCATION_RE.search(text)
    return m.group(1).strip() if m else None


# Maps Team member agent names to token-tracking categories
_MEMBER_TOKEN_MAP = {
    "weather":    "weather",
    "web_search": "ws",
    "gcal":       "gcal",
    "gmail":      "gmail",
}


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
    "model":         ("fallback_model",         "provider",         "fallback_provider"),
    "ws_model":      ("ws_fallback_model",       "ws_provider",      "ws_fallback_provider"),
    "weather_model": ("weather_fallback_model",  "weather_provider", "weather_fallback_provider"),
    "gcal_model":    ("gcal_fallback_model",     "gcal_provider",    "gcal_fallback_provider"),
    "gmail_model":   ("gmail_fallback_model",    "gmail_provider",   "gmail_fallback_provider"),
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
    "openrouter_model":      "model",
    "istruzioni_agente":     "agent_instructions",
    "mostra_metriche":       "show_metrics",
    "memoria_attiva":        "history_enabled",
    "num_conversazioni":     "history_depth",
    "messaggio_avvio":       "startup_message",
    "ws_abilitato":          "ws_enabled",
    "ws_modello":            "ws_model",
    "ws_istruzioni":         "ws_instructions",
    "ws_mostra_metriche":    "ws_show_metrics",
    "meteo_abilitato":       "weather_enabled",
    "meteo_provider":        "weather_provider",
    "meteo_modello":         "weather_model",
    "meteo_istruzioni":      "weather_instructions",
    "meteo_mostra_metriche": "weather_show_metrics",
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


# ── Metrics formatting ────────────────────────────────────────────────────────

def _safe_sum(a: str, b: str) -> str:
    try:
        return str(int(a) + int(b))
    except (ValueError, TypeError):
        return "?"


def _count_model_calls(msgs: list) -> int:
    return sum(1 for m in msgs if getattr(m, "role", "") == "assistant")


def _val_metric(m, key: str) -> str:
    if m is None:
        return "?"
    v = m.get(key) if isinstance(m, dict) else getattr(m, key, None)
    if v is None:
        return "?"
    if isinstance(v, list):
        try:
            return str(sum(int(x) for x in v if x is not None))
        except (TypeError, ValueError):
            return str(v)
    return str(v)


def format_metrics(response, duration: float) -> str:
    try:
        m           = response.metrics
        msgs        = response.messages or []
        model_calls = _count_model_calls(msgs)
        model       = getattr(response, "model", None) or "?"
        # Use agno-tracked duration when available (e.g. Team member RunOutput)
        actual_dur  = getattr(m, "duration", None) or duration
        return (
            f"📊 {actual_dur:.1f}s | 🤖 {model} | 📞 {model_calls}\n"
            f"🔢 in:{_val_metric(m,'input_tokens')} "
            f"out:{_val_metric(m,'output_tokens')} "
            f"tot:{_safe_sum(_val_metric(m,'input_tokens'), _val_metric(m,'output_tokens'))}"
        )
    except Exception as e:
        return f"📊 {duration:.1f}s | metrics error: {e}"


# ── Team / agent builder ──────────────────────────────────────────────────────

async def _prefetch_context(user_message: str, settings: dict) -> dict[str, str]:
    lower = user_message.lower()
    coros: dict = {}

    if settings.get("weather_enabled") and any(kw in lower for kw in _WEATHER_KW):
        loc = _extract_weather_location(user_message)
        if loc:
            coros["weather"] = fetch_weather(loc)

    if settings.get("ws_enabled") and TAVILY_API_KEY and any(kw in lower for kw in _WS_KW):
        coros["web_search"] = fetch_web_search(user_message, TAVILY_API_KEY)

    if settings.get("gcal_enabled") and GCAL_TOKEN_FILE.exists() and any(kw in lower for kw in _GCAL_KW):
        coros["gcal"] = fetch_gcal_events()

    if settings.get("gmail_enabled") and GMAIL_TOKEN_FILE.exists() and any(kw in lower for kw in _GMAIL_KW):
        coros["gmail"] = fetch_gmail_inbox()

    if not coros:
        return {}

    results = await asyncio.gather(*coros.values(), return_exceptions=True)
    output  = {}
    for k, v in zip(coros.keys(), results):
        if isinstance(v, str):
            output[k] = v
        elif isinstance(v, Exception):
            print(f"[DRADIS] Prefetch error for '{k}': {v}")
            asyncio.create_task(_send_error_telegram(
                f"⚠️ Sub-agent <b>{html.escape(k)}</b> prefetch failed: {html.escape(str(v))}"
            ))
    return output


def _build_members(settings: dict, prefetched: dict | None = None) -> list:
    pf = prefetched or {}
    members = []
    if settings.get("ws_enabled") and TAVILY_API_KEY:
        members.append(create_web_search_agent(settings, TAVILY_API_KEY, pf.get("web_search")))
    if settings.get("weather_enabled"):
        members.append(create_weather_agent(settings, pf.get("weather")))
    if settings.get("gcal_enabled") and GCAL_TOKEN_FILE.exists():
        members.append(create_gcal_agent(settings, pf.get("gcal")))
    if settings.get("gmail_enabled") and GMAIL_TOKEN_FILE.exists():
        members.append(create_gmail_agent(settings, pf.get("gmail")))
    return members


def _build_executor(system_prompt: str, model: str, provider: str, members: list):
    if members:
        return agent_core.create_team(system_prompt, model, provider, members)
    return agent_core.create_agent(system_prompt, model, provider)


def _collect_member_responses(response) -> list:
    return getattr(response, "member_responses", [])


def _agents_label(member_responses: list) -> str:
    invoked = {mr.agent_name for mr in member_responses if mr.agent_name}
    parts = ["DRADIS"]
    if "web_search" in invoked: parts.append("Web Search")
    if "weather"    in invoked: parts.append("Weather")
    if "gcal"       in invoked: parts.append("Google Calendar")
    if "gmail"      in invoked: parts.append("Gmail")
    return "🤖 " + " · ".join(parts)


def _track_tokens(response, member_responses: list):
    agent_core._add_tokens("dradis", response)
    for mr in member_responses:
        cat = _MEMBER_TOKEN_MAP.get(mr.agent_name, "")
        if cat:
            agent_core._add_tokens(cat, mr)


def _build_metrics_parts(response, duration: float, member_responses: list, settings: dict) -> list:
    member_map = {mr.agent_name: mr for mr in member_responses}
    parts = []
    if settings.get("ws_show_metrics") and "web_search" in member_map:
        parts.append("🔍 Web Search\n" + format_metrics(member_map["web_search"], 0.0))
    if settings.get("weather_show_metrics") and "weather" in member_map:
        parts.append("🌤 Weather\n" + format_metrics(member_map["weather"], 0.0))
    if settings.get("gcal_show_metrics") and "gcal" in member_map:
        parts.append("📅 Google Calendar\n" + format_metrics(member_map["gcal"], 0.0))
    if settings.get("gmail_show_metrics") and "gmail" in member_map:
        parts.append("📧 Gmail\n" + format_metrics(member_map["gmail"], 0.0))
    if settings.get("show_metrics"):
        parts.append("🤖 DRADIS\n" + format_metrics(response, duration))
    return parts


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

    voice_model      = settings.get("voice_model",    SETTINGS_DEFAULTS["voice_model"])
    voice_language   = settings.get("voice_language", SETTINGS_DEFAULTS["voice_language"])
    send_transcript  = settings.get("voice_send_transcription", True)
    voice_metrics_on = settings.get("voice_metrics", False)

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

    if voice_metrics_on:
        await update.message.reply_text(
            f"🎙️ Voice\n📊 {duration:.1f}s | 🤖 {voice_model}"
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

    prefetched = await _prefetch_context(instructions, settings)
    members    = _build_members(settings, prefetched)
    executor   = _build_executor(system_prompt, model, provider, members)
    print(f"[DRADIS] Scheduled task '{task_name}': model={model} members={[m.name for m in members]} prefetched={list(prefetched)}")

    start_time = time.time()
    try:
        response = await executor.arun(instructions)
    except Exception as e:
        fb_model = (settings.get("fallback_model") or "").strip()
        if fb_model:
            await _send_error_telegram(
                f"⚠️ Task <b>{html.escape(task_name)}</b> model error (<code>{html.escape(model)}</code>): {html.escape(str(e))}\n"
                "🔄 Retrying with fallback model…"
            )
            fb_settings = _apply_fallback_settings(settings)
            fb_model_id = fb_settings.get("model", model)
            fb_provider = fb_settings.get("provider", provider)
            fb_members  = _build_members(fb_settings, prefetched)
            fb_executor = _build_executor(system_prompt, fb_model_id, fb_provider, fb_members)
            print(f"[DRADIS] Task fallback: model={fb_model_id} provider={fb_provider}")
            try:
                response = await fb_executor.arun(instructions)
            except Exception as e2:
                await _send_error_telegram(
                    f"❌ Task <b>{html.escape(task_name)}</b> fallback also failed (<code>{html.escape(fb_model_id)}</code>): {html.escape(str(e2))}"
                )
                return
        else:
            await _send_error_telegram(
                f"❌ Scheduled task <b>{html.escape(task_name)}</b> failed: {html.escape(str(e))}"
            )
            return
    duration         = time.time() - start_time
    member_responses = _collect_member_responses(response)
    _track_tokens(response, member_responses)

    text  = (response.content or "").strip()
    label = _agents_label(member_responses) + f" · <i>{html.escape(task_name)}</i>"

    if text:
        await _telegram_bot.send_message(
            chat_id=ALLOWED_CHAT_ID,
            text=md_to_html(text) + f"\n\n{label}",
            parse_mode=ParseMode.HTML,
        )

    parts = _build_metrics_parts(response, duration, member_responses, settings)
    if parts:
        await _telegram_bot.send_message(
            chat_id=ALLOWED_CHAT_ID,
            text="\n\n".join(parts),
        )


def reload_task_jobs():
    tz = read_settings().get("timezone", "UTC") or "UTC"
    _scheduler.remove_all_jobs()
    for task in load_tasks():
        if task.get("enabled") and task.get("cron"):
            try:
                _scheduler.add_job(
                    run_scheduled_task,
                    CronTrigger.from_crontab(task["cron"], timezone=tz),
                    args=[task],
                    id=task["id"],
                    replace_existing=True,
                    misfire_grace_time=60,
                )
                print(f"[DRADIS] Scheduled task '{task['name']}' cron={task['cron']} tz={tz}")
            except Exception as e:
                print(f"[DRADIS] WARNING: invalid cron for task '{task.get('name')}': {e}")


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

    prefetched = await _prefetch_context(question, settings)
    members    = _build_members(settings, prefetched)
    executor   = _build_executor(system_prompt, model, provider, members)
    print(f"[DRADIS] model: {model} | members: {[m.name for m in members]} | prefetched: {list(prefetched)}")

    start_time = time.time()
    try:
        response = await executor.arun(prompt)
    except Exception as e:
        print(f"[DRADIS] arun error: {e}")
        fb_model = (settings.get("fallback_model") or "").strip()
        if fb_model:
            await _send_error_telegram(
                f"⚠️ Model error (<code>{html.escape(model)}</code>): {html.escape(str(e))}\n"
                "🔄 Retrying with fallback model…"
            )
            fb_settings = _apply_fallback_settings(settings)
            fb_model_id = fb_settings.get("model", model)
            fb_provider = fb_settings.get("provider", provider)
            fb_members  = _build_members(fb_settings, prefetched)
            fb_executor = _build_executor(system_prompt, fb_model_id, fb_provider, fb_members)
            print(f"[DRADIS] Fallback: model={fb_model_id} provider={fb_provider}")
            try:
                response = await fb_executor.arun(prompt)
            except Exception as e2:
                await _send_error_telegram(
                    f"❌ Fallback also failed (<code>{html.escape(fb_model_id)}</code>): {html.escape(str(e2))}"
                )
                await update.message.reply_text(
                    f"❌ Both primary and fallback models failed.\n"
                    f"Fallback error (<code>{html.escape(fb_model_id)}</code>): {html.escape(str(e2))}",
                    parse_mode=ParseMode.HTML,
                )
                return
        else:
            await update.message.reply_text(
                f"❌ Model error (<code>{html.escape(model)}</code>): {html.escape(str(e))}",
                parse_mode=ParseMode.HTML,
            )
            return
    duration         = time.time() - start_time
    member_responses = _collect_member_responses(response)
    _track_tokens(response, member_responses)

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
    elif not response.content:
        await update.message.reply_text(
            f"⚠️ Model <code>{html.escape(model)}</code> returned no text (tool-call only response).\n\n<i>{agents_label}</i>",
            parse_mode=ParseMode.HTML,
        )

    parts = _build_metrics_parts(response, duration, member_responses, settings)
    if parts:
        await update.message.reply_text("\n\n".join(parts))


async def cmd_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_CHAT_ID:
        return
    settings = read_settings()

    lines = [
        "<b>DRADIS</b>",
        f"Provider: {settings.get('provider', SETTINGS_DEFAULTS['provider'])}",
        f"Model: {settings.get('model', SETTINGS_DEFAULTS['model'])}",
        f"Metrics: {'on' if settings.get('show_metrics') else 'off'}",
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


COMMANDS = [
    BotCommand("info",         "Status and configuration of all agents"),
    BotCommand("menu",         "List all available commands"),
    BotCommand("tokens",       "Show total token usage"),
    BotCommand("tokens_reset", "Reset token counters"),
    BotCommand("gcalauth",     "Connect Google Calendar (OAuth2)"),
    BotCommand("gmailauth",    "Connect Gmail (OAuth2)"),
]


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_CHAT_ID:
        return
    lines = "\n".join(f"/{c.command} — {c.description}" for c in COMMANDS)
    await update.message.reply_text(f"<b>DRADIS Commands:</b>\n\n{lines}", parse_mode=ParseMode.HTML)


async def cmd_tokens(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_CHAT_ID:
        return
    labels = {
        "dradis":  "🤖 DRADIS",
        "weather": "🌤 Weather",
        "ws":      "🔍 Web Search",
        "gcal":    "📅 Calendar",
        "gmail":   "📧 Gmail",
    }
    lines = ["<b>Token usage</b>"]
    total_in = total_out = 0
    for key, label in labels.items():
        s    = agent_core._TOKEN_STATS.get(key, {"in": 0, "out": 0})
        i, o = s["in"], s["out"]
        total_in  += i
        total_out += o
        lines.append(f"{label}: in {i:,} | out {o:,} | tot {i + o:,}")
    lines.append(
        f"\n<b>Total: in {total_in:,} | out {total_out:,} | tot {total_in + total_out:,}</b>"
    )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_tokens_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_CHAT_ID:
        return
    for key in agent_core._TOKEN_STATS:
        agent_core._TOKEN_STATS[key] = {"in": 0, "out": 0}
    agent_core._save_token_stats()
    await update.message.reply_text("✅ Token counters reset.")


# ── Entrypoint ────────────────────────────────────────────────────────────────

def build_telegram_app():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("info",         cmd_info))
    app.add_handler(CommandHandler("menu",         cmd_menu))
    app.add_handler(CommandHandler("tokens",       cmd_tokens))
    app.add_handler(CommandHandler("tokens_reset", cmd_tokens_reset))
    app.add_handler(CommandHandler("gcalauth",     cmd_gcalauth))
    app.add_handler(CommandHandler("gmailauth",    cmd_gmailauth))
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
    global _telegram_bot
    _init_settings()
    agent_core.init_token_stats()
    telegram_app = build_telegram_app()
    web_server   = uvicorn.Server(
        uvicorn.Config(web_app, host="0.0.0.0", port=WEB_PORT, log_level="warning")
    )
    async with telegram_app:
        await telegram_app.start()
        await telegram_app.updater.start_polling()
        await _register_commands(telegram_app.bot)
        _telegram_bot = telegram_app.bot
        _scheduler.start()
        reload_task_jobs()
        register_tasks_changed_callback(reload_task_jobs)
        register_run_task_callback(run_scheduled_task)
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
