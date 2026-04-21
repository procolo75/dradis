import asyncio
import html
import json
import os
import re
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import httpx
import uvicorn
from groq import Groq as GroqClient
from agno.agent import Agent
from agno.models.openai.like import OpenAILike
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
    register_tasks_changed_callback, load_tasks,
    set_gcal_code_event, pop_gcal_pending_code,
    set_gmail_code_event, pop_gmail_pending_code,
)

WEB_PORT = 8099


def _load_startup_options() -> dict:
    try:
        with open("/data/options.json") as f:
            return json.load(f)
    except Exception as e:
        raise RuntimeError(f"Cannot read /data/options.json: {e}")

_startup_options = _load_startup_options()
TELEGRAM_TOKEN   = _startup_options["telegram_bot_token"]
ALLOWED_CHAT_ID  = int(_startup_options["telegram_allowed_chat_id"])
TAVILY_API_KEY      = _startup_options.get("tavily_api_key", "")
GOOGLE_CLIENT_ID     = _startup_options.get("google_client_id", "")
GOOGLE_CLIENT_SECRET = _startup_options.get("google_client_secret", "")

API_KEYS = {
    "openrouter": _startup_options.get("openrouter_api_key", ""),
    "openai":     _startup_options.get("openai_api_key", ""),
    "github":     _startup_options.get("github_token", ""),
    "gemini":     _startup_options.get("gemini_api_key", ""),
    "groq":       _startup_options.get("groq_api_key", ""),
}

_groq_client: GroqClient | None = (
    GroqClient(api_key=API_KEYS["groq"]) if API_KEYS.get("groq") else None
)

_scheduler: AsyncIOScheduler = AsyncIOScheduler()
_telegram_bot = None

# ── Google Calendar ───────────────────────────────────────────────────────────

GCAL_TOKEN_FILE    = Path("/data/google_calendar_token.json")
GCAL_SCOPES        = ["https://www.googleapis.com/auth/calendar"]
GCAL_REDIRECT_URI  = "http://localhost:8099/gcalauth/callback"
_gcal_pending_flow = None


def _build_gcal_flow():
    from google_auth_oauthlib.flow import Flow
    client_config = {
        "installed": {
            "client_id":      GOOGLE_CLIENT_ID,
            "client_secret":  GOOGLE_CLIENT_SECRET,
            "auth_uri":       "https://accounts.google.com/o/oauth2/auth",
            "token_uri":      "https://oauth2.googleapis.com/token",
            "redirect_uris":  [GCAL_REDIRECT_URI],
        }
    }
    return Flow.from_client_config(client_config, scopes=GCAL_SCOPES, redirect_uri=GCAL_REDIRECT_URI)


def _get_gcal_creds():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request as GoogleRequest
    if not GCAL_TOKEN_FILE.exists():
        return None
    creds = Credentials.from_authorized_user_file(str(GCAL_TOKEN_FILE), GCAL_SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(GoogleRequest())
        GCAL_TOKEN_FILE.write_text(creds.to_json())
    return creds


def _sync_get_raw_events(days_ahead: int) -> str:
    from googleapiclient.discovery import build as gcal_build
    creds = _get_gcal_creds()
    if not creds:
        return "NOT_AUTHENTICATED"
    service  = gcal_build("calendar", "v3", credentials=creds)
    now      = datetime.now(timezone.utc)
    result   = service.events().list(
        calendarId="primary",
        timeMin=now.isoformat(),
        timeMax=(now + timedelta(days=days_ahead)).isoformat(),
        maxResults=20,
        singleEvents=True,
        orderBy="startTime",
    ).execute()
    events = result.get("items", [])
    if not events:
        return f"No events in the next {days_ahead} day(s)."
    lines = []
    for e in events:
        start = e["start"].get("dateTime", e["start"].get("date", "?"))
        lines.append(f"- [{e['id']}] {start}: {e.get('summary', '(no title)')}")
    return "\n".join(lines)


def _sync_delete_event(event_id: str) -> str:
    from googleapiclient.discovery import build as gcal_build
    creds = _get_gcal_creds()
    if not creds:
        return "NOT_AUTHENTICATED"
    service = gcal_build("calendar", "v3", credentials=creds)
    service.events().delete(calendarId="primary", eventId=event_id).execute()
    return f"Event deleted successfully."


def _sync_create_raw_event(title: str, start_dt: str, end_dt: str, description: str) -> str:
    from googleapiclient.discovery import build as gcal_build
    creds = _get_gcal_creds()
    if not creds:
        return "NOT_AUTHENTICATED"
    service = gcal_build("calendar", "v3", credentials=creds)
    event   = service.events().insert(
        calendarId="primary",
        body={
            "summary":     title,
            "description": description,
            "start":       {"dateTime": start_dt, "timeZone": "UTC"},
            "end":         {"dateTime": end_dt,   "timeZone": "UTC"},
        },
    ).execute()
    return f"Event created: {title} ({start_dt} → {end_dt}). Link: {event.get('htmlLink', '')}"


def create_calendar_tools(settings: dict, gcal_metrics: list):
    _not_auth_msg = "Google Calendar not authenticated. Send /gcalauth to connect."

    async def get_calendar_events(days_ahead: int = 7) -> str:
        """Get Google Calendar events for the next N days (default: 7). Returns event IDs needed for deletion."""
        loop = asyncio.get_running_loop()
        raw  = await loop.run_in_executor(None, _sync_get_raw_events, days_ahead)
        if raw == "NOT_AUTHENTICATED":
            return _not_auth_msg

        gcal_model    = settings.get("gcal_model", "")    or SETTINGS_DEFAULTS["gcal_model"]
        gcal_provider = settings.get("gcal_provider", "") or SETTINGS_DEFAULTS["gcal_provider"]
        try:
            _tz = settings.get("timezone", "UTC") or "UTC"
            gcal_agent = create_agent(
                system_prompt=(
                    f"It is {_now_str(_tz)} ({_tz}). "
                    "You are a calendar assistant. Present the events clearly and concisely "
                    "in the same language the user used. Never invent events not present in the data. "
                    "Include the event ID in brackets at the end of each line so the user can reference it. "
                    + settings.get("gcal_instructions", "")
                ),
                model=gcal_model,
                provider=gcal_provider,
            )
            t0       = time.time()
            response = await gcal_agent.arun(f"Calendar events for the next {days_ahead} day(s):\n{raw}")
            gcal_metrics.append((response, time.time() - t0))
            _add_tokens("gcal", response)
            return response.content or raw
        except Exception:
            gcal_metrics.append((None, 0))
            return raw

    async def create_calendar_event(
        title: str,
        start_datetime: str,
        end_datetime: str,
        description: str = "",
    ) -> str:
        """Create a new Google Calendar event. start_datetime and end_datetime must be ISO 8601 with timezone (e.g. 2026-04-20T10:00:00+02:00)."""
        loop   = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None, _sync_create_raw_event, title, start_datetime, end_datetime, description
        )
        gcal_metrics.append((None, 0))
        if result == "NOT_AUTHENTICATED":
            return _not_auth_msg
        return result

    async def delete_calendar_event(event_id: str) -> str:
        """Delete a Google Calendar event by its ID. First call get_calendar_events to find the event ID."""
        loop   = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, _sync_delete_event, event_id)
        gcal_metrics.append((None, 0))
        if result == "NOT_AUTHENTICATED":
            return _not_auth_msg
        return result

    return [get_calendar_events, create_calendar_event, delete_calendar_event]


# ── Gmail ─────────────────────────────────────────────────────────────────────

GMAIL_TOKEN_FILE   = Path("/data/google_gmail_token.json")
GMAIL_SCOPES       = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://mail.google.com/",
]
GMAIL_REDIRECT_URI  = "http://localhost:8099/gmailauth/callback"
_gmail_pending_flow = None


def _build_gmail_flow():
    from google_auth_oauthlib.flow import Flow
    client_config = {
        "installed": {
            "client_id":      GOOGLE_CLIENT_ID,
            "client_secret":  GOOGLE_CLIENT_SECRET,
            "auth_uri":       "https://accounts.google.com/o/oauth2/auth",
            "token_uri":      "https://oauth2.googleapis.com/token",
            "redirect_uris":  [GMAIL_REDIRECT_URI],
        }
    }
    return Flow.from_client_config(client_config, scopes=GMAIL_SCOPES, redirect_uri=GMAIL_REDIRECT_URI)


def _get_gmail_creds():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request as GoogleRequest
    if not GMAIL_TOKEN_FILE.exists():
        return None
    creds = Credentials.from_authorized_user_file(str(GMAIL_TOKEN_FILE), GMAIL_SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(GoogleRequest())
        GMAIL_TOKEN_FILE.write_text(creds.to_json())
    return creds


def create_gmail_tools(settings: dict, gmail_metrics: list):
    _not_auth_msg = "Gmail not authenticated. Send /gmailauth to connect."

    async def get_emails(max_results: int = 10) -> str:
        """Get the latest emails from Gmail inbox."""
        loop = asyncio.get_running_loop()
        raw  = await loop.run_in_executor(None, _sync_get_emails, max_results)
        if raw == "NOT_AUTHENTICATED":
            return _not_auth_msg
        return await _synthesise_gmail(raw, "inbox emails", settings, gmail_metrics)

    async def get_unread_emails(max_results: int = 10) -> str:
        """Get unread emails from Gmail."""
        loop = asyncio.get_running_loop()
        raw  = await loop.run_in_executor(None, _sync_get_unread_emails, max_results)
        if raw == "NOT_AUTHENTICATED":
            return _not_auth_msg
        return await _synthesise_gmail(raw, "unread emails", settings, gmail_metrics)

    async def search_emails(query: str, max_results: int = 10) -> str:
        """Search Gmail emails by query (same syntax as Gmail search bar)."""
        loop = asyncio.get_running_loop()
        raw  = await loop.run_in_executor(None, _sync_search_emails, query, max_results)
        if raw == "NOT_AUTHENTICATED":
            return _not_auth_msg
        return await _synthesise_gmail(raw, f"search '{query}'", settings, gmail_metrics)

    async def send_email(to: str, subject: str, body: str) -> str:
        """Send an email via Gmail. to is the recipient address, subject is the email subject, body is plain text."""
        loop   = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, _sync_send_email, to, subject, body)
        gmail_metrics.append((None, 0))
        if result == "NOT_AUTHENTICATED":
            return _not_auth_msg
        return result

    return [get_emails, get_unread_emails, search_emails, send_email]


def _sync_get_emails(max_results: int) -> str:
    from googleapiclient.discovery import build as gmail_build
    creds = _get_gmail_creds()
    if not creds:
        return "NOT_AUTHENTICATED"
    service  = gmail_build("gmail", "v1", credentials=creds)
    result   = service.users().messages().list(userId="me", maxResults=max_results).execute()
    return _format_message_list(service, result.get("messages", []))


def _sync_get_unread_emails(max_results: int) -> str:
    from googleapiclient.discovery import build as gmail_build
    creds = _get_gmail_creds()
    if not creds:
        return "NOT_AUTHENTICATED"
    service  = gmail_build("gmail", "v1", credentials=creds)
    result   = service.users().messages().list(userId="me", q="is:unread", maxResults=max_results).execute()
    return _format_message_list(service, result.get("messages", []))


def _sync_search_emails(query: str, max_results: int) -> str:
    from googleapiclient.discovery import build as gmail_build
    creds = _get_gmail_creds()
    if not creds:
        return "NOT_AUTHENTICATED"
    service  = gmail_build("gmail", "v1", credentials=creds)
    result   = service.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
    return _format_message_list(service, result.get("messages", []))


def _format_message_list(service, messages: list) -> str:
    if not messages:
        return "No emails found."
    lines = []
    for msg in messages:
        try:
            detail  = service.users().messages().get(userId="me", id=msg["id"], format="metadata",
                                                     metadataHeaders=["From", "Subject", "Date"]).execute()
            headers = {h["name"]: h["value"] for h in detail.get("payload", {}).get("headers", [])}
            snippet = detail.get("snippet", "")[:120]
            lines.append(
                f"[{msg['id']}] {headers.get('Date', '?')} | From: {headers.get('From', '?')} | "
                f"Subject: {headers.get('Subject', '(no subject)')} | {snippet}"
            )
        except Exception as e:
            lines.append(f"[{msg['id']}] Error reading message: {e}")
    return "\n".join(lines)


def _sync_send_email(to: str, subject: str, body: str) -> str:
    import base64
    from email.message import EmailMessage
    from googleapiclient.discovery import build as gmail_build
    creds = _get_gmail_creds()
    if not creds:
        return "NOT_AUTHENTICATED"
    msg = EmailMessage()
    msg["To"]      = to
    msg["Subject"] = subject
    msg.set_content(body)
    raw     = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service = gmail_build("gmail", "v1", credentials=creds)
    service.users().messages().send(userId="me", body={"raw": raw}).execute()
    return f"Email sent to {to} — Subject: {subject}"


async def _synthesise_gmail(raw: str, context: str, settings: dict, gmail_metrics: list) -> str:
    gmail_model    = settings.get("gmail_model", "")    or SETTINGS_DEFAULTS["gmail_model"]
    gmail_provider = settings.get("gmail_provider", "") or SETTINGS_DEFAULTS["gmail_provider"]
    try:
        _tz = settings.get("timezone", "UTC") or "UTC"
        gmail_agent = create_agent(
            system_prompt=(
                f"It is {_now_str(_tz)} ({_tz}). "
                "You are an email assistant. Present the emails clearly and concisely "
                "in the same language the user used. Never invent data not present in the results. "
                "Include the message ID in brackets so the user can reference it. "
                + settings.get("gmail_instructions", "")
            ),
            model=gmail_model,
            provider=gmail_provider,
        )
        t0       = time.time()
        response = await gmail_agent.arun(f"Gmail {context}:\n{raw}")
        gmail_metrics.append((response, time.time() - t0))
        _add_tokens("gmail", response)
        return response.content or raw
    except Exception:
        gmail_metrics.append((None, 0))
        return raw


GMAIL_HIDDEN_INSTRUCTIONS = (
    "You have access to Gmail tools: `get_emails`, `get_unread_emails`, `search_emails`, `send_email`.\n"
    "Call `get_emails` when:\n"
    "- The user asks to check or list their emails or inbox\n"
    "- Phrases: 'controlla le email', 'check my email', 'show inbox', 'any emails'\n"
    "Call `get_unread_emails` when:\n"
    "- The user asks specifically about unread messages\n"
    "- Phrases: 'email non lette', 'unread emails', 'nuovi messaggi'\n"
    "Call `search_emails` when:\n"
    "- The user wants to find emails from a specific sender, subject, or date\n"
    "- Phrases: 'cerca email da X', 'find emails about Y', 'search mail'\n"
    "Call `send_email` when:\n"
    "- The user wants to send, compose, or write an email\n"
    "- Phrases: 'manda una email', 'send email to', 'scrivi a', 'reply'\n"
    "- If recipient or subject is missing, ask the user before calling `send_email`\n"
    "- NEVER send an email without confirming the recipient and subject with the user first"
)


GCAL_HIDDEN_INSTRUCTIONS = (
    "You have access to Google Calendar tools: `get_calendar_events`, `create_calendar_event`, `delete_calendar_event`.\n"
    "Call `get_calendar_events` when:\n"
    "- The user asks about their schedule, agenda, appointments, or upcoming events\n"
    "- Phrases: 'cosa ho in agenda', 'appuntamenti', 'eventi', 'calendario', 'my schedule', 'upcoming events'\n"
    "Call `create_calendar_event` when:\n"
    "- The user wants to add, create, or schedule a meeting, event, appointment, or reminder\n"
    "- Phrases: 'aggiungi', 'crea evento', 'metti in agenda', 'segna', 'schedule', 'add event'\n"
    "- Infer date/time from the message; use ISO 8601 with timezone (e.g. '2026-04-20T10:00:00+02:00')\n"
    "- If duration not specified, default to 1 hour\n"
    "Call `delete_calendar_event` when:\n"
    "- The user wants to delete, remove, or cancel an event\n"
    "- Phrases: 'cancella', 'elimina', 'rimuovi evento', 'delete event', 'cancel meeting'\n"
    "- IMPORTANT: first call `get_calendar_events` to retrieve the event ID, then call `delete_calendar_event` with that ID\n"
    "- NEVER confirm a deletion without actually calling `delete_calendar_event`"
)


def _api_key_for_provider(provider_id: str) -> str:
    return API_KEYS.get(provider_id, "")


# ── Markdown → HTML ───────────────────────────────────────────────────────────

_FUNCTION_TAG_RE = re.compile(r'<function=[^>]+>.*?</function>', re.DOTALL)


def md_to_html(text: str) -> str:
    text = _FUNCTION_TAG_RE.sub('', text).strip()
    text = html.escape(text)
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'\*(.+?)\*',     r'<i>\1</i>', text)
    text = re.sub(r'`(.+?)`',       r'<code>\1</code>', text)
    return text


def _now_str(tz_name: str | None = None) -> str:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        tz = ZoneInfo(tz_name or "UTC")
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")
    return datetime.now(tz).strftime("%d %B %Y, %H:%M")


def build_system_prompt() -> str:
    settings     = read_settings()
    tz_name      = settings.get("timezone", "UTC") or "UTC"
    instructions = settings.get("agent_instructions", SETTINGS_DEFAULTS["agent_instructions"])
    return f"It is {_now_str(tz_name)} ({tz_name}).\n{instructions}"


# ── Conversation history ──────────────────────────────────────────────────────

_history: list[dict] = []


def _init_settings():
    if not SETTINGS_FILE.exists():
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(
            json.dumps(SETTINGS_DEFAULTS, ensure_ascii=False, indent=2)
        )


# ── Token counter ─────────────────────────────────────────────────────────────

TOKEN_STATS_FILE = Path("/data/dradis_token_stats.json")
_TOKEN_STATS: dict = {}

_TOKEN_CATEGORIES = ("dradis", "weather", "ws", "gcal", "gmail")


def _load_token_stats() -> dict:
    default = {k: {"in": 0, "out": 0} for k in _TOKEN_CATEGORIES}
    try:
        data = json.loads(TOKEN_STATS_FILE.read_text())
        for k in default:
            if k not in data:
                data[k] = {"in": 0, "out": 0}
        return data
    except Exception:
        return default


def _save_token_stats():
    try:
        TOKEN_STATS_FILE.write_text(json.dumps(_TOKEN_STATS, ensure_ascii=False))
    except Exception as e:
        print(f"[DRADIS] WARNING: could not save token stats: {e}")


def _extract_tokens(response) -> tuple[int, int]:
    try:
        m = response.metrics
        def _sum_key(key):
            v = m.get(key) if isinstance(m, dict) else getattr(m, key, None)
            if v is None:
                return 0
            if isinstance(v, list):
                return sum(int(x) for x in v if x is not None)
            return int(v)
        return _sum_key("input_tokens"), _sum_key("output_tokens")
    except Exception:
        return 0, 0


def _add_tokens(category: str, response):
    if response is None:
        return
    in_t, out_t = _extract_tokens(response)
    if in_t == 0 and out_t == 0:
        return
    _TOKEN_STATS[category]["in"]  += in_t
    _TOKEN_STATS[category]["out"] += out_t
    _save_token_stats()


_LEGACY_SETTINGS_MAP = {
    "openrouter_model":   "model",
    "istruzioni_agente":  "agent_instructions",
    "mostra_metriche":    "show_metrics",
    "memoria_attiva":     "history_enabled",
    "num_conversazioni":  "history_depth",
    "messaggio_avvio":    "startup_message",
    "ws_abilitato":       "ws_enabled",
    "ws_modello":         "ws_model",
    "ws_istruzioni":      "ws_instructions",
    "ws_mostra_metriche": "ws_show_metrics",
    "meteo_abilitato":    "weather_enabled",
    "meteo_provider":     "weather_provider",
    "meteo_modello":      "weather_model",
    "meteo_istruzioni":   "weather_instructions",
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


# ── Agent ─────────────────────────────────────────────────────────────────────

def _base_url_for_provider(provider_id: str) -> str:
    for p in PROVIDERS:
        if p["id"] == provider_id:
            return p["base_url"]
    return PROVIDERS[0]["base_url"]


WEATHER_HIDDEN_INSTRUCTIONS = (
    "You have access to a weather lookup tool via `get_weather`. "
    "Call it when:\n"
    "- The user asks about current weather, forecast, temperature, rain, wind, or UV index\n"
    "- The user uses phrases like \"che tempo fa\", \"previsioni\", \"meteo\", \"weather\", \"forecast\", \"temperature\"\n"
    "Pass a city name or geographic location to `get_weather`."
)

WS_HIDDEN_INSTRUCTIONS = (
    "You have access to a web search sub-agent via the `search_web` tool. "
    "Call it when:\n"
    "- The user asks for current news, prices, weather, or recent events\n"
    "- The user uses phrases like \"search for\", \"look up\", \"find online\", \"latest on\"\n"
    "- You need up-to-date information that may have changed since your training cutoff\n"
    "Pass a concise, optimised search query to `search_web`."
)


def create_agent(system_prompt: str, model: str, provider: str, tools: list | None = None) -> Agent:
    return Agent(
        name="DRADIS",
        model=OpenAILike(
            id=model,
            api_key=_api_key_for_provider(provider),
            base_url=_base_url_for_provider(provider),
        ),
        instructions=system_prompt,
        tools=tools or [],
        markdown=False,
    )


def create_weather_tool(settings: dict, weather_metrics: list):
    async def get_weather(location: str) -> str:
        """Get current weather and 3-day forecast for a location."""
        async with httpx.AsyncClient(timeout=10) as client:
            geo = await client.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": location, "count": 1, "language": "en", "format": "json"},
            )
        results = geo.json().get("results", [])
        if not results:
            return f"Location '{location}' not found. Do not invent weather data."
        r = results[0]
        lat, lon, name = r["latitude"], r["longitude"], r.get("name", location)

        async with httpx.AsyncClient(timeout=10) as client:
            fc = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat, "longitude": lon,
                    "current": "temperature_2m,relative_humidity_2m,precipitation,wind_speed_10m,weather_code",
                    "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,weather_code",
                    "timezone": "auto",
                    "forecast_days": 3,
                },
            )
        data = fc.json()
        current = data.get("current", {})
        daily   = data.get("daily", {})
        raw_text = f"Location: {name}\nCurrent: {current}\nDaily forecast (3 days): {daily}"

        _tz = settings.get("timezone", "UTC") or "UTC"
        weather_agent = create_agent(
            system_prompt=(
                f"It is {_now_str(_tz)} ({_tz}). "
                "You are a meteorologist. Summarise the weather data clearly and concisely "
                "in the same language the user used. Never invent data not present in the results. "
                + settings.get("weather_instructions", "")
            ),
            model=settings.get("weather_model", SETTINGS_DEFAULTS["weather_model"]),
            provider=settings.get("weather_provider", SETTINGS_DEFAULTS["weather_provider"]),
        )
        t0 = time.time()
        response = await weather_agent.arun(f"Weather data for {name}:\n{raw_text}")
        weather_metrics.append((response, time.time() - t0))
        _add_tokens("weather", response)
        return response.content or ""

    return get_weather


def create_web_search_tool(settings: dict, ws_metrics: list):
    from tavily import TavilyClient
    tavily_client = TavilyClient(api_key=TAVILY_API_KEY)

    async def search_web(query: str) -> str:
        """Search the web for current information and return a synthesised answer."""
        raw = tavily_client.search(query=query, max_results=5)
        results = raw.get("results", [])
        if not results:
            return "No web results found for this query. Do not invent information."
        results_text = "\n\n".join(
            f"Title: {r['title']}\n{r['content']}\nURL: {r['url']}"
            for r in results
        )
        _tz = settings.get("timezone", "UTC") or "UTC"
        ws_agent = create_agent(
            system_prompt=(
                f"It is {_now_str(_tz)} ({_tz}). "
                "You are a web research assistant. Synthesise ONLY the information "
                "present in the search results below into a clear, concise answer. "
                "If the results do not contain enough information, say so explicitly. "
                "Never invent or assume facts not present in the results. "
                + settings.get("ws_instructions", "")
            ),
            model=settings.get("ws_model", SETTINGS_DEFAULTS["ws_model"]),
            provider=settings.get("ws_provider", SETTINGS_DEFAULTS["ws_provider"]),
        )
        t0 = time.time()
        response = await ws_agent.arun(
            f"Search query: {query}\n\nSearch results:\n{results_text}"
        )
        duration = time.time() - t0
        ws_metrics.append((response, duration))
        _add_tokens("ws", response)
        return response.content or ""

    return search_web


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
        return (
            f"📊 {duration:.1f}s | 🤖 {model} | 📞 {model_calls}\n"
            f"🔢 in:{_val_metric(m,'input_tokens')} "
            f"out:{_val_metric(m,'output_tokens')} "
            f"tot:{_val_metric(m,'total_tokens')}"
        )
    except Exception as e:
        return f"📊 {duration:.1f}s | metrics error: {e}"


# ── Voice transcription ───────────────────────────────────────────────────────

async def transcribe_voice(file_path: str, model: str, language: str) -> str:
    """Transcribe an OGG voice message to text using the Groq Whisper API.

    Raises RuntimeError if the Groq client is not initialised (missing API key).
    The SDK call is synchronous and runs in a thread executor to avoid blocking
    the asyncio event loop.
    """
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
    """Handle Telegram voice messages: transcribe via Groq Whisper then route as text.

    Flow:
      1. Validate user and voice_enabled setting; silently ignore if not applicable.
      2. Download the .ogg file to a temporary path.
      3. Transcribe with Groq Whisper.
      4. Delete the temp file (always, even on error).
      5. Optionally send the transcription back to Telegram.
      6. Optionally send voice metrics (latency + model).
      7. Pass the transcribed text to handle_message via a lightweight shim.
    """
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
        """Shim that replaces .text with the transcription; delegates all else to the real message."""
        def __init__(self, real_msg, text: str):
            self._msg = real_msg
            self.text = text

        def __getattr__(self, name):
            return getattr(self._msg, name)

    class _VoiceUpdate:
        """Minimal shim so handle_message reads .text from a voice message."""
        def __init__(self, real_update: Update, text: str):
            self.effective_user = real_update.effective_user
            self.message        = _VoiceMessage(real_update.message, text)

    await handle_message(_VoiceUpdate(update, transcription), context)


# ── Scheduled Tasks ──────────────────────────────────────────────────────────

async def run_scheduled_task(task: dict):
    global _telegram_bot
    if not _telegram_bot:
        return
    task_name = task.get("name", "Task")
    instructions = task.get("instructions", "").strip()
    if not instructions:
        return

    settings = read_settings()
    system_prompt = build_system_prompt()
    model    = settings.get("model", SETTINGS_DEFAULTS["model"])
    provider = settings.get("provider", SETTINGS_DEFAULTS["provider"])

    ws_metrics: list      = []
    weather_metrics: list = []
    gcal_metrics: list    = []
    gmail_metrics: list   = []
    tools = []
    if settings.get("ws_enabled") and TAVILY_API_KEY:
        system_prompt += "\n\n" + WS_HIDDEN_INSTRUCTIONS
        tools.append(create_web_search_tool(settings, ws_metrics))
    if settings.get("weather_enabled"):
        system_prompt += "\n\n" + WEATHER_HIDDEN_INSTRUCTIONS
        tools.append(create_weather_tool(settings, weather_metrics))
    if settings.get("gcal_enabled") and GCAL_TOKEN_FILE.exists():
        system_prompt += "\n\n" + GCAL_HIDDEN_INSTRUCTIONS
        tools.extend(create_calendar_tools(settings, gcal_metrics))
    if settings.get("gmail_enabled") and GMAIL_TOKEN_FILE.exists():
        system_prompt += "\n\n" + GMAIL_HIDDEN_INSTRUCTIONS
        tools.extend(create_gmail_tools(settings, gmail_metrics))

    print(f"[DRADIS] Scheduled task '{task_name}': model={model}")
    agent = create_agent(system_prompt, model, provider, tools=tools)
    start_time = time.time()
    try:
        response = await agent.arun(instructions)
    except Exception as e:
        await _telegram_bot.send_message(
            chat_id=ALLOWED_CHAT_ID,
            text=f"❌ Scheduled task <b>{html.escape(task_name)}</b> failed: {html.escape(str(e))}",
            parse_mode=ParseMode.HTML,
        )
        return
    duration = time.time() - start_time
    _add_tokens("dradis", response)

    text = (response.content or "").strip()
    agents_used = ["DRADIS"]
    if ws_metrics:
        agents_used.append("Web Search")
    if weather_metrics:
        agents_used.append("Weather")
    if gcal_metrics:
        agents_used.append("Google Calendar")
    if gmail_metrics:
        agents_used.append("Gmail")
    label = "🤖 " + " · ".join(agents_used) + f" · <i>{html.escape(task_name)}</i>"

    if text:
        await _telegram_bot.send_message(
            chat_id=ALLOWED_CHAT_ID,
            text=md_to_html(text) + f"\n\n{label}",
            parse_mode=ParseMode.HTML,
        )

    if settings.get("show_metrics"):
        await _telegram_bot.send_message(
            chat_id=ALLOWED_CHAT_ID,
            text=f"🤖 {html.escape(task_name)}\n" + format_metrics(response, duration),
        )
    if settings.get("gcal_show_metrics") and gcal_metrics:
        for gcal_resp, gcal_dur in gcal_metrics:
            if gcal_resp is not None:
                await _telegram_bot.send_message(
                    chat_id=ALLOWED_CHAT_ID,
                    text="📅 Google Calendar\n" + format_metrics(gcal_resp, gcal_dur),
                )
    if settings.get("gmail_show_metrics") and gmail_metrics:
        for gmail_resp, gmail_dur in gmail_metrics:
            if gmail_resp is not None:
                await _telegram_bot.send_message(
                    chat_id=ALLOWED_CHAT_ID,
                    text="📧 Gmail\n" + format_metrics(gmail_resp, gmail_dur),
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

    model    = settings.get("model", SETTINGS_DEFAULTS["model"])
    provider = settings.get("provider", SETTINGS_DEFAULTS["provider"])

    ws_metrics: list      = []
    weather_metrics: list = []
    gcal_metrics: list    = []
    gmail_metrics: list   = []
    tools = []
    if settings.get("ws_enabled") and TAVILY_API_KEY:
        system_prompt += "\n\n" + WS_HIDDEN_INSTRUCTIONS
        tools.append(create_web_search_tool(settings, ws_metrics))
    if settings.get("weather_enabled"):
        system_prompt += "\n\n" + WEATHER_HIDDEN_INSTRUCTIONS
        tools.append(create_weather_tool(settings, weather_metrics))
    if settings.get("gcal_enabled") and GCAL_TOKEN_FILE.exists():
        system_prompt += "\n\n" + GCAL_HIDDEN_INSTRUCTIONS
        tools.extend(create_calendar_tools(settings, gcal_metrics))
    if settings.get("gmail_enabled") and GMAIL_TOKEN_FILE.exists():
        system_prompt += "\n\n" + GMAIL_HIDDEN_INSTRUCTIONS
        tools.extend(create_gmail_tools(settings, gmail_metrics))

    print(f"[DRADIS] model: {model} | ws: {settings.get('ws_enabled', False)} | weather: {settings.get('weather_enabled', False)} | gcal: {settings.get('gcal_enabled', False)} | gmail: {settings.get('gmail_enabled', False)}")
    agent      = create_agent(system_prompt, model, provider, tools=tools)
    start_time = time.time()
    try:
        response = await agent.arun(prompt)
    except Exception as e:
        print(f"[DRADIS] agent.arun error: {e}")
        await update.message.reply_text(
            f"❌ Model error (<code>{html.escape(model)}</code>): {html.escape(str(e))}",
            parse_mode=ParseMode.HTML,
        )
        return
    duration = time.time() - start_time
    _add_tokens("dradis", response)

    text = (response.content or "").strip()

    if history_enabled:
        save_turn("user", question, history_depth)
        save_turn("assistant", text, history_depth)

    agents_used  = ["DRADIS"]
    if ws_metrics:
        agents_used.append("Web Search")
    if weather_metrics:
        agents_used.append("Weather")
    if gcal_metrics:
        agents_used.append("Google Calendar")
    if gmail_metrics:
        agents_used.append("Gmail")
    agents_label = "🤖 " + " · ".join(agents_used)

    if text:
        final_text = md_to_html(text) + f"\n\n<i>{agents_label}</i>"
        await update.message.reply_text(final_text, parse_mode=ParseMode.HTML)
    elif not response.content:
        await update.message.reply_text(
            f"⚠️ Model <code>{html.escape(model)}</code> returned no text (tool-call only response).\n\n<i>{agents_label}</i>",
            parse_mode=ParseMode.HTML,
        )
    metrics_on    = settings.get("show_metrics", False)
    ws_metrics_on = settings.get("ws_show_metrics", False)
    print(f"[DRADIS] show_metrics={metrics_on} ws_show_metrics={ws_metrics_on}")
    parts = []
    if ws_metrics_on and ws_metrics:
        for ws_resp, ws_dur in ws_metrics:
            parts.append("🔍 Web Search\n" + format_metrics(ws_resp, ws_dur))
    if settings.get("weather_show_metrics") and weather_metrics:
        for wr, wd in weather_metrics:
            parts.append("🌤 Weather\n" + format_metrics(wr, wd))
    if settings.get("gcal_show_metrics") and gcal_metrics:
        for gcal_resp, gcal_dur in gcal_metrics:
            parts.append("📅 Google Calendar\n" + format_metrics(gcal_resp, gcal_dur))
    if settings.get("gmail_show_metrics") and gmail_metrics:
        for gmail_resp, gmail_dur in gmail_metrics:
            parts.append("📧 Gmail\n" + format_metrics(gmail_resp, gmail_dur))
    if metrics_on:
        parts.append("🤖 DRADIS\n" + format_metrics(response, duration))
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

    gcal_on = settings.get("gcal_enabled", False)
    gcal_auth = GCAL_TOKEN_FILE.exists()
    lines += ["", "<b>Google Calendar</b>", f"Status: {'enabled' if gcal_on else 'disabled'}"]
    if gcal_on:
        lines.append(f"Provider: {settings.get('gcal_provider', SETTINGS_DEFAULTS['gcal_provider'])}")
        lines.append(f"Model: {settings.get('gcal_model', SETTINGS_DEFAULTS['gcal_model'])}")
        lines.append(f"Auth: {'✅ connected' if gcal_auth else '❌ not authenticated — send /gcalauth'}")

    gmail_on = settings.get("gmail_enabled", False)
    gmail_auth = GMAIL_TOKEN_FILE.exists()
    lines += ["", "<b>Gmail</b>", f"Status: {'enabled' if gmail_on else 'disabled'}"]
    if gmail_on:
        lines.append(f"Provider: {settings.get('gmail_provider', SETTINGS_DEFAULTS['gmail_provider'])}")
        lines.append(f"Model: {settings.get('gmail_model', SETTINGS_DEFAULTS['gmail_model'])}")
        lines.append(f"Auth: {'✅ connected' if gmail_auth else '❌ not authenticated — send /gmailauth'}")

    agents = []
    try:
        with open("/data/agents.json") as f:
            agents = json.load(f)
    except Exception:
        pass
    if agents:
        lines += ["", "<b>Sub-agents</b>"]
        for a in agents:
            status = "✅" if a.get("active", True) else "⏸"
            lines.append(f"{status} {a.get('id', '?')} — {a.get('model', '?')}")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def _gcal_complete_auth(flow, code: str, message) -> bool:
    """Exchange OAuth code for token and save. Returns True on success."""
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
    """Background task: wait for loopback callback then complete auth."""
    try:
        await asyncio.wait_for(event.wait(), timeout=300)
        code = pop_gcal_pending_code()
        if code and not GCAL_TOKEN_FILE.exists():
            await _gcal_complete_auth(flow, code, message)
    except asyncio.TimeoutError:
        if not GCAL_TOKEN_FILE.exists():
            await message.reply_text("⏱ Authorization timed out (5 min). Send /gcalauth to try again.")


async def cmd_gcalauth(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /gcalauth.
    No args  → start OAuth flow, wait for loopback callback automatically.
    With URL → fallback: parse code from the redirect URL and exchange manually.
    """
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
        # ── Primary flow: loopback callback ──────────────────────────────────
        event = asyncio.Event()
        set_gcal_code_event(event)
        flow = _build_gcal_flow()
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

    # ── Fallback: user pasted the redirect URL ────────────────────────────────
    raw  = " ".join(args)
    code = parse_qs(urlparse(raw).query).get("code", [raw])[0]

    if not code:
        await update.message.reply_text(
            "❌ Could not parse the authorization code. "
            "Make sure you copied the full redirect URL.",
            parse_mode=ParseMode.HTML,
        )
        return

    flow = _gcal_pending_flow or _build_gcal_flow()
    await _gcal_complete_auth(flow, code, update.message)


async def _gmail_complete_auth(flow, code: str, message) -> bool:
    """Exchange OAuth code for Gmail token and save. Returns True on success."""
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
    """Background task: wait for loopback callback then complete Gmail auth."""
    try:
        await asyncio.wait_for(event.wait(), timeout=300)
        code = pop_gmail_pending_code()
        if code and not GMAIL_TOKEN_FILE.exists():
            await _gmail_complete_auth(flow, code, message)
    except asyncio.TimeoutError:
        if not GMAIL_TOKEN_FILE.exists():
            await message.reply_text("⏱ Authorization timed out (5 min). Send /gmailauth to try again.")


async def cmd_gmailauth(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /gmailauth.
    No args  → start OAuth flow, wait for loopback callback automatically.
    With URL → fallback: parse code from the redirect URL and exchange manually.
    """
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
        flow = _build_gmail_flow()
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

    flow = _gmail_pending_flow or _build_gmail_flow()
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
        s    = _TOKEN_STATS.get(key, {"in": 0, "out": 0})
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
    for key in _TOKEN_STATS:
        _TOKEN_STATS[key] = {"in": 0, "out": 0}
    _save_token_stats()
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
    global _telegram_bot, _TOKEN_STATS
    _init_settings()
    _TOKEN_STATS = _load_token_stats()
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
