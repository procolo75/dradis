"""
web/store.py
────────────
Shared data layer for the DRADIS web server.

Owns: file path constants, load/save helpers, callback registrations,
provider configuration, cron validation, and OAuth pending-code state.
Imported by all route modules and by bot/state.py.
"""

import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from apscheduler.triggers.cron import CronTrigger

# ── File paths (all resolved at import time) ──────────────────────────────────

AGENTS_FILE        = Path("/data/agents.json")
TASKS_FILE         = Path("/data/tasks.json")
MONITORS_FILE      = Path("/data/monitors.json")
LIVE_MONITORS_FILE = Path("/data/live_monitors.json")
HA_MONITORS_FILE   = Path("/data/ha_monitors.json")
OPTIONS_FILE       = Path("/data/options.json")
SETTINGS_FILE      = Path("/data/dradis_settings.json")

# ── Provider registry ─────────────────────────────────────────────────────────

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
    {"id": "gemini-2.0-flash",                 "name": "Gemini 2.0 Flash"},
    {"id": "gemini-2.0-flash-lite",            "name": "Gemini 2.0 Flash Lite"},
    {"id": "gemini-2.5-pro-preview-03-25",     "name": "Gemini 2.5 Pro Preview"},
    {"id": "gemini-1.5-pro",                   "name": "Gemini 1.5 Pro"},
    {"id": "gemini-1.5-flash",                 "name": "Gemini 1.5 Flash"},
    {"id": "gemini-1.5-flash-8b",              "name": "Gemini 1.5 Flash 8B"},
]

# ── Settings schema ───────────────────────────────────────────────────────────

SETTINGS_KEYS = [
    "provider", "agent_instructions", "model", "fallback_provider", "fallback_model",
    "history_enabled", "history_depth", "startup_message", "timezone",
    "ws_enabled", "ws_provider", "ws_model", "ws_instructions",
    "ws_fallback_provider", "ws_fallback_model",
    "read_url_enabled",
    "weather_enabled", "weather_provider", "weather_model", "weather_instructions",
    "weather_fallback_provider", "weather_fallback_model",
    "voice_enabled", "voice_provider", "voice_model", "voice_language", "voice_send_transcription",
    "gcal_enabled", "gcal_provider", "gcal_model", "gcal_instructions",
    "gcal_fallback_provider", "gcal_fallback_model",
    "gmail_enabled", "gmail_provider", "gmail_model", "gmail_instructions",
    "gmail_fallback_provider", "gmail_fallback_model",
    "gtasks_enabled", "gtasks_provider", "gtasks_model", "gtasks_instructions",
    "gtasks_fallback_provider", "gtasks_fallback_model",
    "mqtt_host", "mqtt_port", "mqtt_username", "mqtt_password", "mqtt_statestream_prefix",
]

SETTINGS_DEFAULTS: dict = {
    "provider":             "openrouter",
    "agent_instructions":   "You are DRADIS, a versatile AI assistant.",
    "model":                "nvidia/nemotron-3-nano-30b-a3b:free",
    "history_enabled":      True,
    "history_depth":        2,
    "startup_message":      "✅ DRADIS online and ready.",
    "timezone":             "UTC",
    "ws_enabled":           False,
    "ws_provider":          "openrouter",
    "ws_model":             "nvidia/nemotron-3-nano-30b-a3b:free",
    "ws_instructions":      "",
    "read_url_enabled":     False,
    "weather_enabled":      False,
    "weather_provider":     "openrouter",
    "weather_model":        "nvidia/nemotron-3-nano-30b-a3b:free",
    "weather_instructions": "",
    "voice_enabled":            False,
    "voice_provider":           "groq",
    "voice_model":              "whisper-large-v3-turbo",
    "voice_language":           "it",
    "voice_send_transcription": True,
    "gcal_enabled":             False,
    "gcal_provider":            "openrouter",
    "gcal_model":               "nvidia/nemotron-3-nano-30b-a3b:free",
    "gcal_instructions":        "",
    "gmail_enabled":            False,
    "gmail_provider":           "openrouter",
    "gmail_model":              "nvidia/nemotron-3-nano-30b-a3b:free",
    "gmail_instructions":       "",
    "gtasks_enabled":           False,
    "gtasks_provider":          "openrouter",
    "gtasks_model":             "nvidia/nemotron-3-nano-30b-a3b:free",
    "gtasks_instructions":      "",
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
    "gtasks_fallback_provider":      "",
    "gtasks_fallback_model":         "",
    "mqtt_host":                     "core-mosquitto",
    "mqtt_port":                     1883,
    "mqtt_username":                 "",
    "mqtt_password":                 "",
    "mqtt_statestream_prefix":       "homeassistant",
}

# Maps old key names to current names for transparent migration.
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

_LEGACY_AGENT_MAP = {
    "nome":       "name",
    "modello":    "model",
    "istruzioni": "instructions",
    "attivo":     "active",
}


# ── Migration helpers ─────────────────────────────────────────────────────────

def _migrate_settings(data: dict) -> dict:
    return {_LEGACY_SETTINGS_MAP.get(k, k): v for k, v in data.items()}


def _migrate_agent(agent: dict) -> dict:
    return {_LEGACY_AGENT_MAP.get(k, k): v for k, v in agent.items()}


# ── Load / save helpers ───────────────────────────────────────────────────────

def load_agents() -> list[dict]:
    try:
        return [_migrate_agent(a) for a in json.loads(AGENTS_FILE.read_text())]
    except Exception:
        return []


def save_agents(agents: list[dict]) -> None:
    AGENTS_FILE.write_text(json.dumps(agents, ensure_ascii=False, indent=2))


def load_tasks() -> list[dict]:
    try:
        return json.loads(TASKS_FILE.read_text())
    except Exception:
        return []


def save_tasks(tasks: list[dict]) -> None:
    TASKS_FILE.write_text(json.dumps(tasks, ensure_ascii=False, indent=2))


def load_monitors() -> list[dict]:
    try:
        return json.loads(MONITORS_FILE.read_text())
    except Exception:
        return []


def save_monitors(monitors: list[dict]) -> None:
    MONITORS_FILE.write_text(json.dumps(monitors, ensure_ascii=False, indent=2))


def load_live_monitors() -> list[dict]:
    try:
        return json.loads(LIVE_MONITORS_FILE.read_text())
    except Exception:
        return []


def save_live_monitors(items: list[dict]) -> None:
    LIVE_MONITORS_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2))


def load_ha_monitors() -> list[dict]:
    try:
        return json.loads(HA_MONITORS_FILE.read_text())
    except Exception:
        return []


def save_ha_monitors(items: list[dict]) -> None:
    HA_MONITORS_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2))


def load_settings() -> dict:
    result = dict(SETTINGS_DEFAULTS)
    try:
        overrides = _migrate_settings(json.loads(SETTINGS_FILE.read_text()))
        result.update({k: overrides[k] for k in SETTINGS_KEYS if k in overrides})
    except Exception:
        pass
    return result


def save_settings(settings: dict) -> None:
    filtered = {k: v for k, v in settings.items() if k in SETTINGS_KEYS}
    SETTINGS_FILE.write_text(json.dumps(filtered, ensure_ascii=False, indent=2))


# ── Callback registrations ────────────────────────────────────────────────────

_on_tasks_changed: Callable | None = None
_run_task_fn: Callable | None = None
_on_monitors_changed: Callable | None = None
_run_monitor_fn: Callable | None = None
_on_live_monitors_changed: Callable | None = None
_get_live_monitor_status_fn: Callable | None = None
_on_ha_monitors_changed: Callable | None = None
_get_ha_monitor_status_fn: Callable | None = None


def register_tasks_changed_callback(fn: Callable) -> None:
    global _on_tasks_changed
    _on_tasks_changed = fn


def register_run_task_callback(fn: Callable) -> None:
    global _run_task_fn
    _run_task_fn = fn


def register_monitors_changed_callback(fn: Callable) -> None:
    global _on_monitors_changed
    _on_monitors_changed = fn


def register_run_monitor_callback(fn: Callable) -> None:
    global _run_monitor_fn
    _run_monitor_fn = fn


def register_live_monitors_changed_callback(fn: Callable) -> None:
    global _on_live_monitors_changed
    _on_live_monitors_changed = fn


def register_live_monitor_status_callback(fn: Callable) -> None:
    global _get_live_monitor_status_fn
    _get_live_monitor_status_fn = fn


def register_ha_monitors_changed_callback(fn: Callable) -> None:
    global _on_ha_monitors_changed
    _on_ha_monitors_changed = fn


def register_ha_monitor_status_callback(fn: Callable) -> None:
    global _get_ha_monitor_status_fn
    _get_ha_monitor_status_fn = fn


def _notify_tasks_changed() -> None:
    if _on_tasks_changed:
        _on_tasks_changed()


def _notify_monitors_changed() -> None:
    if _on_monitors_changed:
        _on_monitors_changed()


def _notify_live_monitors_changed() -> None:
    if _on_live_monitors_changed:
        _on_live_monitors_changed()


def _notify_ha_monitors_changed() -> None:
    if _on_ha_monitors_changed:
        _on_ha_monitors_changed()


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


def _get_tavily_key() -> str:
    try:
        return json.loads(OPTIONS_FILE.read_text()).get("tavily_api_key", "")
    except Exception:
        return ""


# ── Cron validation ───────────────────────────────────────────────────────────

def _get_configured_tz() -> str:
    return load_settings().get("timezone", "UTC") or "UTC"


def _validate_cron_expr(expr: str, tz: str = "UTC") -> tuple[bool, str | None, str | None]:
    """Validate a 5-part cron expression. Returns (valid, error_message, next_fire_iso)."""
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


# ── Model-listing helpers (shared with routes/agents.py) ─────────────────────

_SPEEDTEST_PROMPT     = "Respond in one sentence: what is artificial intelligence?"
_SPEEDTEST_MAX_TOKENS = 60
_SPEEDTEST_TOP_N      = 5


def _parse_size_b(m: dict) -> float:
    arch = m.get("architecture", {})
    raw  = arch.get("num_parameters")
    if raw:
        try:
            val = float(raw)
            return val if val < 10_000 else val / 1_000_000_000
        except (ValueError, TypeError):
            pass
    text = (m.get("name", "") + " " + m.get("id", "")).lower()
    hits = re.findall(r'(\d+(?:\.\d+)?)b', text)
    return max((float(n) for n in hits), default=0.0)


# ── OAuth pending-code state ──────────────────────────────────────────────────

_gcal_pending_code: str | None = None
_gcal_code_event: asyncio.Event | None = None

_gmail_pending_code: str | None = None
_gmail_code_event: asyncio.Event | None = None

_gtasks_pending_code: str | None = None
_gtasks_code_event: asyncio.Event | None = None


def set_gcal_code_event(event: asyncio.Event) -> None:
    global _gcal_code_event
    _gcal_code_event = event


def pop_gcal_pending_code() -> str | None:
    global _gcal_pending_code
    code = _gcal_pending_code
    _gcal_pending_code = None
    return code


def set_gmail_code_event(event: asyncio.Event) -> None:
    global _gmail_code_event
    _gmail_code_event = event


def pop_gmail_pending_code() -> str | None:
    global _gmail_pending_code
    code = _gmail_pending_code
    _gmail_pending_code = None
    return code


def set_gtasks_code_event(event: asyncio.Event) -> None:
    global _gtasks_code_event
    _gtasks_code_event = event


def pop_gtasks_pending_code() -> str | None:
    global _gtasks_pending_code
    code = _gtasks_pending_code
    _gtasks_pending_code = None
    return code
