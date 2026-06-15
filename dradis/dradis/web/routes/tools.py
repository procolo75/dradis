"""
web/routes/tools.py
────────────────────
Routes: OAuth callbacks (Google Calendar, Gmail, Tasks), connectivity tests
(web search, weather), and Google service status endpoints.
"""

import json
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

import web.store as _store

router = APIRouter()


# ── Football Betting ──────────────────────────────────────────────────────────

@router.get("/api/football/inplaying")
async def football_inplaying():
    import bot.state as _state
    if not _state.RAPIDAPI_FOOTBALL_KEY:
        raise HTTPException(status_code=400, detail="rapidapi_football_key not configured in add-on settings")
    try:
        from live_monitors.football import fetch_inplaying_data
        matches = await fetch_inplaying_data()
        return {"count": len(matches), "matches": matches}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Web Search ────────────────────────────────────────────────────────────────

@router.post("/api/websearch-test")
async def test_websearch():
    key = _store._get_tavily_key()
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


# ── Weather ───────────────────────────────────────────────────────────────────

@router.get("/api/weather-test")
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


# ── Google Calendar OAuth ─────────────────────────────────────────────────────

@router.get("/gcalauth/callback")
async def gcal_oauth_callback(code: str = None, error: str = None):
    if error:
        return HTMLResponse(
            f"<h2 style='font-family:sans-serif;color:#c00'>❌ Authorization failed: {error}</h2>"
            "<p style='font-family:sans-serif'>Return to Telegram and send /gcalauth to try again.</p>"
        )
    if not code:
        return HTMLResponse(
            "<h2 style='font-family:sans-serif;color:#c00'>❌ No authorization code received.</h2>"
        )
    _store._gcal_pending_code = code
    if _store._gcal_code_event:
        _store._gcal_code_event.set()
    return HTMLResponse(
        "<h2 style='font-family:sans-serif;color:#080'>✅ Google Calendar connected!</h2>"
        "<p style='font-family:sans-serif'>You can close this tab and return to Telegram.</p>"
    )


@router.get("/api/gcal-status")
async def get_gcal_status():
    opts = {}
    try:
        opts = json.loads(_store.OPTIONS_FILE.read_text())
    except Exception:
        pass
    return {
        "credentials_configured": bool(opts.get("google_client_id") and opts.get("google_client_secret")),
        "authenticated": Path("/data/google_calendar_token.json").exists(),
    }


# ── Gmail OAuth ───────────────────────────────────────────────────────────────

@router.get("/gmailauth/callback")
async def gmail_oauth_callback(code: str = None, error: str = None):
    if error:
        return HTMLResponse(
            f"<h2 style='font-family:sans-serif;color:#c00'>❌ Authorization failed: {error}</h2>"
            "<p style='font-family:sans-serif'>Return to Telegram and send /gmailauth to try again.</p>"
        )
    if not code:
        return HTMLResponse(
            "<h2 style='font-family:sans-serif;color:#c00'>❌ No authorization code received.</h2>"
        )
    _store._gmail_pending_code = code
    if _store._gmail_code_event:
        _store._gmail_code_event.set()
    return HTMLResponse(
        "<h2 style='font-family:sans-serif;color:#080'>✅ Gmail connected!</h2>"
        "<p style='font-family:sans-serif'>You can close this tab and return to Telegram.</p>"
    )


@router.get("/api/gmail-status")
async def get_gmail_status():
    opts = {}
    try:
        opts = json.loads(_store.OPTIONS_FILE.read_text())
    except Exception:
        pass
    return {
        "credentials_configured": bool(opts.get("google_client_id") and opts.get("google_client_secret")),
        "authenticated": Path("/data/google_gmail_token.json").exists(),
    }


# ── Google Tasks OAuth ────────────────────────────────────────────────────────

@router.get("/gtasksauth/callback")
async def gtasks_oauth_callback(code: str = None, error: str = None):
    if error:
        return HTMLResponse(
            f"<h2 style='font-family:sans-serif;color:#c00'>❌ Authorization failed: {error}</h2>"
            "<p style='font-family:sans-serif'>Return to Telegram and send /gtasksauth to try again.</p>"
        )
    if not code:
        return HTMLResponse(
            "<h2 style='font-family:sans-serif;color:#c00'>❌ No authorization code received.</h2>"
        )
    _store._gtasks_pending_code = code
    if _store._gtasks_code_event:
        _store._gtasks_code_event.set()
    return HTMLResponse(
        "<h2 style='font-family:sans-serif;color:#080'>✅ Google Tasks connected!</h2>"
        "<p style='font-family:sans-serif'>You can close this tab and return to Telegram.</p>"
    )


@router.get("/api/gtasks-status")
async def get_gtasks_status():
    opts = {}
    try:
        opts = json.loads(_store.OPTIONS_FILE.read_text())
    except Exception:
        pass
    return {
        "credentials_configured": bool(opts.get("google_client_id") and opts.get("google_client_secret")),
        "authenticated": Path("/data/google_tasks_token.json").exists(),
    }


# ── Google Drive Backup OAuth ─────────────────────────────────────────────────

@router.get("/backupauth/callback")
async def gdrive_oauth_callback(code: str = None, error: str = None):
    if error:
        return HTMLResponse(
            f"<h2 style='font-family:sans-serif;color:#c00'>❌ Authorization failed: {error}</h2>"
            "<p style='font-family:sans-serif'>Return to Telegram and send /backupauth to try again.</p>"
        )
    if not code:
        return HTMLResponse(
            "<h2 style='font-family:sans-serif;color:#c00'>❌ No authorization code received.</h2>"
        )
    _store._gdrive_pending_code = code
    if _store._gdrive_code_event:
        _store._gdrive_code_event.set()
    return HTMLResponse(
        "<h2 style='font-family:sans-serif;color:#080'>☁️ Google Drive Backup connected!</h2>"
        "<p style='font-family:sans-serif'>You can close this tab and return to Telegram.</p>"
    )


@router.get("/api/backup-status")
async def get_backup_status():
    opts = {}
    try:
        opts = json.loads(_store.OPTIONS_FILE.read_text())
    except Exception:
        pass
    return {
        "credentials_configured": bool(opts.get("google_client_id") and opts.get("google_client_secret")),
        "authenticated": Path("/data/gdrive_backup_token.json").exists(),
    }
