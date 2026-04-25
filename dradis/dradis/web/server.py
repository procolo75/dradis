import json
import re
import time
import asyncio
from pathlib import Path
from typing import Callable
from uuid import uuid4
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import httpx
from apscheduler.triggers.cron import CronTrigger

AGENTS_FILE   = Path("/data/agents.json")
TASKS_FILE    = Path("/data/tasks.json")
OPTIONS_FILE  = Path("/data/options.json")
SETTINGS_FILE = Path("/data/dradis_settings.json")
HTML_FILE     = Path(__file__).parent / "index.html"

PROVIDERS = [
    {"id": "openrouter", "label": "OpenRouter",    "base_url": "https://openrouter.ai/api/v1"},
    {"id": "openai",     "label": "OpenAI",        "base_url": "https://api.openai.com/v1"},
    {"id": "github",     "label": "GitHub Models", "base_url": "https://models.inference.ai.azure.com"},
    {"id": "gemini",     "label": "Gemini",        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/"},
    {"id": "groq",       "label": "Groq",          "base_url": "https://api.groq.com/openai/v1"},
]

_PROVIDER_KEY_MAP = {
    "openrouter": "openrouter_api_key",
    "openai":     "openai_api_key",
    "github":     "github_token",
    "gemini":     "gemini_api_key",
    "groq":       "groq_api_key",
}

GITHUB_MODELS = [
    {"id": "gpt-4o",                      "name": "GPT-4o"},
    {"id": "gpt-4o-mini",                 "name": "GPT-4o Mini"},
    {"id": "Phi-3.5-MoE-instruct",        "name": "Phi 3.5 MoE"},
    {"id": "Phi-3.5-mini-instruct",       "name": "Phi 3.5 Mini"},
    {"id": "Meta-Llama-3.1-70B-Instruct", "name": "Llama 3.1 70B"},
    {"id": "Meta-Llama-3.1-8B-Instruct",  "name": "Llama 3.1 8B"},
    {"id": "Mistral-Nemo",                "name": "Mistral Nemo"},
    {"id": "Mistral-large",               "name": "Mistral Large"},
]

GEMINI_MODELS = [
    {"id": "gemini-2.0-flash",        "name": "Gemini 2.0 Flash"},
    {"id": "gemini-2.0-flash-lite",   "name": "Gemini 2.0 Flash Lite"},
    {"id": "gemini-2.5-pro-preview-03-25", "name": "Gemini 2.5 Pro Preview"},
    {"id": "gemini-1.5-pro",          "name": "Gemini 1.5 Pro"},
    {"id": "gemini-1.5-flash",        "name": "Gemini 1.5 Flash"},
    {"id": "gemini-1.5-flash-8b",     "name": "Gemini 1.5 Flash 8B"},
]

SETTINGS_KEYS = [
    "provider", "agent_instructions", "model", "fallback_provider", "fallback_model",
    "show_metrics", "history_enabled", "history_depth", "startup_message", "timezone",
    "ws_enabled", "ws_provider", "ws_model", "ws_instructions", "ws_show_metrics",
    "ws_fallback_provider", "ws_fallback_model",
    "weather_enabled", "weather_provider", "weather_model", "weather_instructions", "weather_show_metrics",
    "weather_fallback_provider", "weather_fallback_model",
    "voice_enabled", "voice_provider", "voice_model", "voice_language", "voice_send_transcription", "voice_metrics",
    "gcal_enabled", "gcal_provider", "gcal_model", "gcal_instructions", "gcal_show_metrics",
    "gcal_fallback_provider", "gcal_fallback_model",
    "gmail_enabled", "gmail_provider", "gmail_model", "gmail_instructions", "gmail_show_metrics",
    "gmail_fallback_provider", "gmail_fallback_model",
]

# Maps old key names to current names for transparent migration.
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

_LEGACY_AGENT_MAP = {
    "nome":      "name",
    "modello":   "model",
    "istruzioni": "instructions",
    "attivo":    "active",
}

app = FastAPI(title="DRADIS Web UI")


def _migrate_settings(data: dict) -> dict:
    """Rename any legacy Italian keys to their current English equivalents."""
    return {_LEGACY_SETTINGS_MAP.get(k, k): v for k, v in data.items()}


def _migrate_agent(agent: dict) -> dict:
    """Rename any legacy Italian fields in an agent record."""
    return {_LEGACY_AGENT_MAP.get(k, k): v for k, v in agent.items()}


def load_agents() -> list[dict]:
    try:
        return [_migrate_agent(a) for a in json.loads(AGENTS_FILE.read_text())]
    except Exception:
        return []


def save_agents(agents: list[dict]):
    AGENTS_FILE.write_text(json.dumps(agents, ensure_ascii=False, indent=2))


def load_tasks() -> list[dict]:
    try:
        return json.loads(TASKS_FILE.read_text())
    except Exception:
        return []


def save_tasks(tasks: list[dict]):
    TASKS_FILE.write_text(json.dumps(tasks, ensure_ascii=False, indent=2))


_on_tasks_changed: Callable | None = None
_run_task_fn: Callable | None = None


def register_tasks_changed_callback(fn: Callable):
    global _on_tasks_changed
    _on_tasks_changed = fn


def _notify_tasks_changed():
    if _on_tasks_changed:
        _on_tasks_changed()


def register_run_task_callback(fn: Callable):
    global _run_task_fn
    _run_task_fn = fn


SETTINGS_DEFAULTS = {
    "provider":             "openrouter",
    "agent_instructions":   "You are DRADIS, a versatile AI assistant.",
    "model":                "nvidia/nemotron-3-nano-30b-a3b:free",
    "show_metrics":         False,
    "history_enabled":      True,
    "history_depth":        2,
    "startup_message":      "✅ DRADIS online and ready.",
    "timezone":             "UTC",
    "ws_enabled":           False,
    "ws_provider":          "openrouter",
    "ws_model":             "nvidia/nemotron-3-nano-30b-a3b:free",
    "ws_instructions":      "",
    "ws_show_metrics":      False,
    "weather_enabled":      False,
    "weather_provider":     "openrouter",
    "weather_model":        "nvidia/nemotron-3-nano-30b-a3b:free",
    "weather_instructions": "",
    "weather_show_metrics": False,
    "voice_enabled":            False,
    "voice_provider":           "groq",
    "voice_model":              "whisper-large-v3-turbo",
    "voice_language":           "it",
    "voice_send_transcription": True,
    "voice_metrics":            False,
    "gcal_enabled":             False,
    "gcal_provider":            "openrouter",
    "gcal_model":               "nvidia/nemotron-3-nano-30b-a3b:free",
    "gcal_instructions":        "",
    "gcal_show_metrics":        False,
    "gmail_enabled":            False,
    "gmail_provider":           "openrouter",
    "gmail_model":              "nvidia/nemotron-3-nano-30b-a3b:free",
    "gmail_instructions":       "",
    "gmail_show_metrics":       False,
    "fallback_provider":             "",
    "fallback_model":                "",
    "ws_fallback_provider":          "",
    "ws_fallback_model":             "",
    "weather_fallback_provider":     "",
    "weather_fallback_model":        "",
    "gcal_fallback_provider":        "",
    "gcal_fallback_model":           "",
    "gmail_fallback_provider":       "",
    "gmail_fallback_model":          "",
}

def load_settings() -> dict:
    result = dict(SETTINGS_DEFAULTS)
    try:
        overrides = _migrate_settings(json.loads(SETTINGS_FILE.read_text()))
        result.update({k: overrides[k] for k in SETTINGS_KEYS if k in overrides})
    except Exception:
        pass
    return result


def save_settings(settings: dict):
    filtered = {k: v for k, v in settings.items() if k in SETTINGS_KEYS}
    SETTINGS_FILE.write_text(json.dumps(filtered, ensure_ascii=False, indent=2))


class AgentPayload(BaseModel):
    provider:     str
    model:        str
    instructions: str
    active:       bool = True


class TaskPayload(BaseModel):
    name:         str
    enabled:      bool = False
    cron:         str  = "0 8 * * *"
    instructions: str  = ""


class SettingsPayload(BaseModel):
    provider:             str  = "openrouter"
    agent_instructions:   str  = "You are DRADIS, a versatile AI assistant."
    model:                str  = "nvidia/nemotron-3-nano-30b-a3b:free"
    show_metrics:         bool = False
    history_enabled:      bool = True
    history_depth:        int  = 2
    startup_message:      str  = "✅ DRADIS online and ready."
    timezone:             str  = "UTC"
    ws_enabled:           bool = False
    ws_provider:          str  = "openrouter"
    ws_model:             str  = "nvidia/nemotron-3-nano-30b-a3b:free"
    ws_instructions:      str  = ""
    ws_show_metrics:      bool = False
    weather_enabled:      bool = False
    weather_provider:     str  = "openrouter"
    weather_model:        str  = "nvidia/nemotron-3-nano-30b-a3b:free"
    weather_instructions: str  = ""
    weather_show_metrics: bool = False
    voice_enabled:            bool = False
    voice_provider:           str  = "groq"
    voice_model:              str  = "whisper-large-v3-turbo"
    voice_language:           str  = "it"
    voice_send_transcription: bool = True
    voice_metrics:            bool = False
    gcal_enabled:             bool = False
    gcal_provider:            str  = "openrouter"
    gcal_model:               str  = "nvidia/nemotron-3-nano-30b-a3b:free"
    gcal_instructions:        str  = ""
    gcal_show_metrics:        bool = False
    gmail_enabled:            bool = False
    gmail_provider:           str  = "openrouter"
    gmail_model:              str  = "nvidia/nemotron-3-nano-30b-a3b:free"
    gmail_instructions:       str  = ""
    gmail_show_metrics:       bool = False
    fallback_provider:             str = ""
    fallback_model:                str = ""
    ws_fallback_provider:          str = ""
    ws_fallback_model:             str = ""
    weather_fallback_provider:     str = ""
    weather_fallback_model:        str = ""
    gcal_fallback_provider:        str = ""
    gcal_fallback_model:           str = ""
    gmail_fallback_provider:       str = ""
    gmail_fallback_model:          str = ""


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML_FILE.read_text(encoding="utf-8")


@app.get("/api/config")
async def get_config():
    return {"providers": PROVIDERS}


@app.get("/api/settings")
async def get_settings():
    return load_settings()


@app.put("/api/settings")
async def update_settings(payload: SettingsPayload):
    data = payload.model_dump()
    if data.get("voice_enabled"):
        groq_key = _get_provider_api_key("groq")
        if not groq_key:
            raise HTTPException(
                status_code=400,
                detail="Groq API key is required to enable the Voice agent. Set groq_api_key in the add-on Configuration tab.",
            )
    tz = data.get("timezone", "UTC") or "UTC"
    try:
        CronTrigger.from_crontab("0 0 * * *", timezone=tz)
    except Exception:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid timezone: '{tz}'. Use an IANA name such as 'Europe/Rome' or 'UTC'.",
        )
    save_settings(data)
    return data


@app.get("/api/agents")
async def list_agents():
    return load_agents()


@app.put("/api/agents/{agent_id}")
async def update_agent(agent_id: str, payload: AgentPayload):
    agents = load_agents()
    for i, a in enumerate(agents):
        if a["id"] == agent_id:
            agents[i] = {**a, **payload.model_dump()}
            save_agents(agents)
            return agents[i]
    raise HTTPException(status_code=404, detail="Agent not found")


# ── Scheduled Tasks ───────────────────────────────────────────────────────────

def _get_configured_tz() -> str:
    """Return the IANA timezone name from settings, falling back to UTC."""
    return load_settings().get("timezone", "UTC") or "UTC"


def _validate_cron_expr(expr: str, tz: str = "UTC") -> tuple[bool, str | None, str | None]:
    """Validate a 5-part cron expression using APScheduler with the given IANA timezone.

    Returns (valid, error_message, next_fire_iso).
    """
    expr = expr.strip()
    if not expr:
        return False, "Expression is empty", None
    try:
        trigger   = CronTrigger.from_crontab(expr, timezone=tz)
        now_utc   = datetime.now(timezone.utc)
        next_fire = trigger.get_next_fire_time(None, now_utc)
        next_iso  = next_fire.isoformat() if next_fire else None
        return True, None, next_iso
    except Exception as e:
        return False, str(e), None


@app.get("/api/tasks/validate-cron")
async def validate_cron(expr: str = ""):
    tz = _get_configured_tz()
    valid, error, next_fire = _validate_cron_expr(expr, tz)
    return {"valid": valid, "error": error, "next_fire": next_fire, "tz": tz}


@app.get("/api/server-timezone")
async def get_server_timezone():
    return {"tz": _get_configured_tz()}


@app.get("/api/tasks")
async def list_tasks():
    return load_tasks()


@app.post("/api/tasks")
async def create_task(payload: TaskPayload):
    valid, error, _ = _validate_cron_expr(payload.cron)
    if not valid:
        raise HTTPException(status_code=400, detail=f"Invalid cron expression: {error}")
    tasks = load_tasks()
    task = {
        "id":           str(uuid4()),
        "created_at":   datetime.now(timezone.utc).isoformat(),
        **payload.model_dump(),
    }
    tasks.append(task)
    save_tasks(tasks)
    _notify_tasks_changed()
    return task


@app.put("/api/tasks/{task_id}")
async def update_task(task_id: str, payload: TaskPayload):
    valid, error, _ = _validate_cron_expr(payload.cron)
    if not valid:
        raise HTTPException(status_code=400, detail=f"Invalid cron expression: {error}")
    tasks = load_tasks()
    for i, t in enumerate(tasks):
        if t["id"] == task_id:
            tasks[i] = {**t, **payload.model_dump()}
            save_tasks(tasks)
            _notify_tasks_changed()
            return tasks[i]
    raise HTTPException(status_code=404, detail="Task not found")


@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: str):
    tasks = load_tasks()
    tasks = [t for t in tasks if t["id"] != task_id]
    save_tasks(tasks)
    _notify_tasks_changed()
    return {"ok": True}


@app.post("/api/tasks/{task_id}/run")
async def run_task_now(task_id: str):
    if not _run_task_fn:
        raise HTTPException(status_code=503, detail="Task runner not available")
    task = next((t for t in load_tasks() if t["id"] == task_id), None)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if not task.get("instructions", "").strip():
        raise HTTPException(status_code=400, detail="Task has no instructions")
    asyncio.create_task(_run_task_fn(task))
    return {"ok": True}


# ── Web Search helpers ────────────────────────────────────────────────────────

def _get_tavily_key() -> str:
    try:
        return json.loads(OPTIONS_FILE.read_text()).get("tavily_api_key", "")
    except Exception:
        return ""


@app.post("/api/websearch-test")
async def test_websearch():
    key = _get_tavily_key()
    if not key:
        raise HTTPException(status_code=400, detail="Tavily API key not configured in add-on settings")
    try:
        from tavily import TavilyClient
        result = TavilyClient(api_key=key).search("What is artificial intelligence?", max_results=1)
        if result.get("results"):
            return {"ok": True, "message": "Connection successful"}
        return {"ok": False, "message": "No results returned"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Weather helpers ───────────────────────────────────────────────────────────

@app.get("/api/weather-test")
async def test_weather():
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={"latitude": 41.9, "longitude": 12.48, "current": "temperature_2m"},
            )
            resp.raise_for_status()
            data = resp.json()
        if "current" in data:
            temp = data["current"].get("temperature_2m", "?")
            return {"ok": True, "message": f"Connection successful (Rome: {temp}°C)"}
        return {"ok": False, "message": "Unexpected response format"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Provider helpers ──────────────────────────────────────────────────────────

def _get_provider_api_key(provider_id: str) -> str:
    try:
        opts = json.loads(OPTIONS_FILE.read_text())
        key_name = _PROVIDER_KEY_MAP.get(provider_id, "")
        return opts.get(key_name, "")
    except Exception:
        return ""


def _get_provider_base_url(provider_id: str) -> str:
    for p in PROVIDERS:
        if p["id"] == provider_id:
            return p["base_url"]
    return PROVIDERS[0]["base_url"]


_SPEEDTEST_PROMPT     = "Respond in one sentence: what is artificial intelligence?"
_SPEEDTEST_MAX_TOKENS = 60
_SPEEDTEST_TOP_N      = 5


def _parse_size_b(m: dict) -> float:
    """Extract model size in billions. Uses architecture.num_parameters first, then regex."""
    arch = m.get("architecture", {})
    raw = arch.get("num_parameters")
    if raw:
        try:
            val = float(raw)
            return val if val < 10_000 else val / 1_000_000_000
        except (ValueError, TypeError):
            pass
    text = (m.get("name", "") + " " + m.get("id", "")).lower()
    hits = re.findall(r'(\d+(?:\.\d+)?)b', text)
    return max((float(n) for n in hits), default=0.0)


async def _fetch_openrouter_models(api_key: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            "https://openrouter.ai/api/v1/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        resp.raise_for_status()
    result = []
    for m in resp.json().get("data", []):
        model_id = m.get("id", "")
        pricing  = m.get("pricing", {})
        is_free  = (
            model_id.endswith(":free") or
            (pricing.get("prompt") == "0" and pricing.get("completion") == "0")
        )
        if not is_free:
            continue
        if "tools" not in m.get("supported_parameters", []):
            continue
        size = _parse_size_b(m)
        if size < 30:
            continue
        result.append({
            "id":             model_id,
            "name":           m.get("name", model_id),
            "size_b":         size,
            "context_length": m.get("context_length", 0),
        })
    result.sort(key=lambda x: x["size_b"])
    return result


async def _fetch_openai_models(api_key: str) -> list[dict]:
    _OPENAI_TOOL_MODELS = {
        "gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-4-turbo-preview",
        "gpt-3.5-turbo", "gpt-4", "gpt-4-0125-preview",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            "https://api.openai.com/v1/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        resp.raise_for_status()
    result = []
    for m in resp.json().get("data", []):
        mid = m.get("id", "")
        if any(mid.startswith(k) for k in _OPENAI_TOOL_MODELS):
            result.append({"id": mid, "name": mid})
    result.sort(key=lambda x: x["id"])
    return result



async def _fetch_groq_models(api_key: str) -> list[dict]:
    _GROQ_EXCLUDE = ("whisper", "distil-", "guard")
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            "https://api.groq.com/openai/v1/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        resp.raise_for_status()
    result = []
    for m in resp.json().get("data", []):
        mid = m.get("id", "")
        if any(mid.startswith(ex) or ex in mid for ex in _GROQ_EXCLUDE):
            continue
        result.append({"id": mid, "name": m.get("id", mid)})
    result.sort(key=lambda x: x["id"])
    return result


async def _fetch_groq_voice_models(api_key: str) -> list[dict]:
    """Return only Groq speech-to-text (Whisper) models."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            "https://api.groq.com/openai/v1/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        resp.raise_for_status()
    result = [
        {"id": m["id"], "name": m["id"]}
        for m in resp.json().get("data", [])
        if "whisper" in m.get("id", "")
    ]
    result.sort(key=lambda x: x["id"])
    return result


@app.get("/api/voice-models")
async def get_voice_models():
    """Return Groq Whisper models available for voice transcription."""
    api_key = _get_provider_api_key("groq")
    if not api_key:
        raise HTTPException(status_code=400, detail="Groq API key not configured in add-on settings")
    try:
        return await _fetch_groq_voice_models(api_key)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/voice-test")
async def test_voice():
    """Verify the Groq API key can reach the Whisper models endpoint."""
    api_key = _get_provider_api_key("groq")
    if not api_key:
        raise HTTPException(status_code=400, detail="Groq API key not configured in add-on settings")
    try:
        models = await _fetch_groq_voice_models(api_key)
        if models:
            return {"ok": True, "message": f"Groq reachable — {len(models)} Whisper model(s) available"}
        return {"ok": False, "message": "Groq reachable but no Whisper models found"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Google Calendar OAuth callback ───────────────────────────────────────────

_gcal_pending_code: str | None = None
_gcal_code_event: asyncio.Event | None = None


def set_gcal_code_event(event: asyncio.Event):
    global _gcal_code_event
    _gcal_code_event = event


def pop_gcal_pending_code() -> str | None:
    global _gcal_pending_code
    code = _gcal_pending_code
    _gcal_pending_code = None
    return code


@app.get("/gcalauth/callback")
async def gcal_oauth_callback(code: str = None, error: str = None):
    """Loopback redirect target for Google OAuth. Signals the waiting Telegram command."""
    global _gcal_pending_code
    if error:
        return HTMLResponse(
            f"<h2 style='font-family:sans-serif;color:#c00'>❌ Authorization failed: {error}</h2>"
            "<p style='font-family:sans-serif'>Return to Telegram and send /gcalauth to try again.</p>"
        )
    if not code:
        return HTMLResponse(
            "<h2 style='font-family:sans-serif;color:#c00'>❌ No authorization code received.</h2>"
        )
    _gcal_pending_code = code
    if _gcal_code_event:
        _gcal_code_event.set()
    return HTMLResponse(
        "<h2 style='font-family:sans-serif;color:#080'>✅ Google Calendar connected!</h2>"
        "<p style='font-family:sans-serif'>You can close this tab and return to Telegram.</p>"
    )


@app.get("/api/gcal-status")
async def get_gcal_status():
    """Return whether Google Calendar credentials are configured and authenticated."""
    opts = {}
    try:
        opts = json.loads(OPTIONS_FILE.read_text())
    except Exception:
        pass
    client_id     = opts.get("google_client_id", "")
    client_secret = opts.get("google_client_secret", "")
    token_file    = Path("/data/google_calendar_token.json")
    return {
        "credentials_configured": bool(client_id and client_secret),
        "authenticated": token_file.exists(),
    }


# ── Gmail OAuth callback ──────────────────────────────────────────────────────

_gmail_pending_code: str | None = None
_gmail_code_event: asyncio.Event | None = None


def set_gmail_code_event(event: asyncio.Event):
    global _gmail_code_event
    _gmail_code_event = event


def pop_gmail_pending_code() -> str | None:
    global _gmail_pending_code
    code = _gmail_pending_code
    _gmail_pending_code = None
    return code


@app.get("/gmailauth/callback")
async def gmail_oauth_callback(code: str = None, error: str = None):
    """Loopback redirect target for Gmail OAuth. Signals the waiting Telegram command."""
    global _gmail_pending_code
    if error:
        return HTMLResponse(
            f"<h2 style='font-family:sans-serif;color:#c00'>❌ Authorization failed: {error}</h2>"
            "<p style='font-family:sans-serif'>Return to Telegram and send /gmailauth to try again.</p>"
        )
    if not code:
        return HTMLResponse(
            "<h2 style='font-family:sans-serif;color:#c00'>❌ No authorization code received.</h2>"
        )
    _gmail_pending_code = code
    if _gmail_code_event:
        _gmail_code_event.set()
    return HTMLResponse(
        "<h2 style='font-family:sans-serif;color:#080'>✅ Gmail connected!</h2>"
        "<p style='font-family:sans-serif'>You can close this tab and return to Telegram.</p>"
    )


@app.get("/api/gmail-status")
async def get_gmail_status():
    """Return whether Gmail credentials are configured and authenticated."""
    opts = {}
    try:
        opts = json.loads(OPTIONS_FILE.read_text())
    except Exception:
        pass
    client_id     = opts.get("google_client_id", "")
    client_secret = opts.get("google_client_secret", "")
    token_file    = Path("/data/google_gmail_token.json")
    return {
        "credentials_configured": bool(client_id and client_secret),
        "authenticated": token_file.exists(),
    }


@app.get("/api/models")
async def get_models(provider: str = "openrouter"):
    api_key = _get_provider_api_key(provider)
    try:
        if provider == "openrouter":
            return await _fetch_openrouter_models(api_key)
        elif provider == "openai":
            return await _fetch_openai_models(api_key)
        elif provider == "github":
            return GITHUB_MODELS
        elif provider == "gemini":
            return GEMINI_MODELS
        elif provider == "groq":
            return await _fetch_groq_models(api_key)
        else:
            raise HTTPException(status_code=400, detail=f"Unknown provider: {provider}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# Keep legacy endpoint as alias.
@app.get("/api/openrouter/models")
async def get_openrouter_models():
    return await get_models("openrouter")


class SpeedtestPayload(BaseModel):
    models: list[str]


@app.post("/api/speedtest")
async def run_speedtest(payload: SpeedtestPayload, provider: str = "openrouter"):
    api_key  = _get_provider_api_key(provider)
    base_url = _get_provider_base_url(provider)
    sem      = asyncio.Semaphore(4)

    async def test_one(model_id: str) -> dict:
        async with sem:
            t0 = time.monotonic()
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.post(
                        f"{base_url.rstrip('/')}/chat/completions",
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type":  "application/json",
                            "X-Title":       "DRADIS-speedtest",
                        },
                        json={
                            "model":      model_id,
                            "messages":   [{"role": "user", "content": _SPEEDTEST_PROMPT}],
                            "max_tokens": _SPEEDTEST_MAX_TOKENS,
                        },
                    )
                elapsed = time.monotonic() - t0
                if resp.status_code == 200:
                    data = resp.json()
                    token_out = (
                        data.get("usage", {}).get("completion_tokens") or
                        len((data.get("choices") or [{}])[0]
                            .get("message", {}).get("content", "").split())
                    )
                    tok_s = round(token_out / elapsed, 1) if elapsed > 0 and token_out else None
                    return {"id": model_id, "tok_s": tok_s, "ok": tok_s is not None}
                return {"id": model_id, "tok_s": None, "ok": False}
            except Exception:
                return {"id": model_id, "tok_s": None, "ok": False}

    all_results = await asyncio.gather(*[test_one(m) for m in payload.models])
    successful  = sorted(
        [r for r in all_results if r["ok"]],
        key=lambda x: x["tok_s"],
        reverse=True,
    )
    failed = [r for r in all_results if not r["ok"]]
    return (successful + failed)[:_SPEEDTEST_TOP_N]


# Keep legacy endpoint as alias.
@app.post("/api/openrouter/speedtest")
async def run_speedtest_legacy(payload: SpeedtestPayload):
    return await run_speedtest(payload, provider="openrouter")
