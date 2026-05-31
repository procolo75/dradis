"""
web/server.py
─────────────
DRADIS web server entry point. Creates the FastAPI application and registers
all route modules. Shared state lives in web/store.py; models in web/models.py.

Re-exports everything that main.py and agents import from this module so that
downstream imports remain unchanged after the refactoring.
"""

from fastapi import FastAPI

from web.routes.settings import router as settings_router
from web.routes.agents   import router as agents_router
from web.routes.tasks    import router as tasks_router
from web.routes.monitors import router as monitors_router
from web.routes.tools    import router as tools_router

# ── Re-exports for backwards-compatible imports in main.py and agents/ ────────
from web.store import (  # noqa: F401
    AGENTS_FILE, TASKS_FILE, MONITORS_FILE, LIVE_MONITORS_FILE, HA_MONITORS_FILE,
    OPTIONS_FILE, SETTINGS_FILE,
    PROVIDERS, GITHUB_MODELS, GEMINI_MODELS,
    SETTINGS_KEYS, SETTINGS_DEFAULTS,
    load_agents, save_agents,
    load_tasks, save_tasks,
    load_monitors, save_monitors,
    load_live_monitors, save_live_monitors,
    load_ha_monitors, save_ha_monitors,
    load_settings, save_settings,
    register_tasks_changed_callback, register_run_task_callback,
    register_monitors_changed_callback, register_run_monitor_callback,
    register_live_monitors_changed_callback, register_live_monitor_status_callback,
    register_ha_monitors_changed_callback, register_ha_monitor_status_callback,
    set_gcal_code_event, pop_gcal_pending_code,
    set_gmail_code_event, pop_gmail_pending_code,
    set_gtasks_code_event, pop_gtasks_pending_code,
    set_gdrive_code_event, pop_gdrive_pending_code,
)

app = FastAPI(title="DRADIS Web UI")

app.include_router(settings_router)
app.include_router(agents_router)
app.include_router(tasks_router)
app.include_router(monitors_router)
app.include_router(tools_router)
