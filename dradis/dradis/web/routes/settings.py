"""
web/routes/settings.py
───────────────────────
Routes: configuration, settings CRUD, timezone.
"""

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pathlib import Path

from apscheduler.triggers.cron import CronTrigger

from web.store import (
    PROVIDERS,
    _get_provider_api_key,
    _get_configured_tz,
    load_settings,
    save_settings,
)
from web.models import SettingsPayload

router = APIRouter()

HTML_FILE = Path(__file__).parent.parent / "index.html"


@router.get("/", response_class=HTMLResponse)
async def index():
    return HTML_FILE.read_text(encoding="utf-8")


@router.get("/api/config")
async def get_config():
    return {"providers": PROVIDERS}


@router.get("/api/settings")
async def get_settings():
    return load_settings()


@router.put("/api/settings")
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


@router.get("/api/server-timezone")
async def get_server_timezone():
    return {"tz": _get_configured_tz()}
