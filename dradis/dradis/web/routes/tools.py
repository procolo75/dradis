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

_FOOTBALL_PROVIDERS = {"provider1", "provider2", "provider3", "provider4"}


def _football_windows(windows: str, early_start: int, early_end: int, early_max_odds: float,
                      late_start: int, late_end: int, late_max_odds: float):
    """The window list the Test API table is computed against.

    Built from query parameters and handed to the monitor's own `_window_specs`,
    so the table cannot answer with a different rule than the poll would.
    """
    from live_monitors.football import MINUTE_CEILING, MINUTE_FLOOR, _window_specs
    for label, start, end in (("early", early_start, early_end), ("late", late_start, late_end)):
        if not MINUTE_FLOOR <= start < end <= MINUTE_CEILING:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid {label} window {start}-{end}: expected {MINUTE_FLOOR} ≤ start < end ≤ {MINUTE_CEILING}",
            )
    if early_max_odds < 0 or late_max_odds < 0:
        raise HTTPException(status_code=400, detail="Maximum odds cannot be negative (0 = no cap)")
    return _window_specs({
        "windows":               [w for w in windows.split(",") if w.strip()],
        "window_early_start":    early_start,
        "window_early_end":      early_end,
        "window_early_max_odds": early_max_odds,
        "window_late_start":     late_start,
        "window_late_end":       late_end,
        "window_late_max_odds":  late_max_odds,
    })


@router.get("/api/football/inplaying")
async def football_inplaying(windows: str = "early,late",
                             early_start: int = 55, early_end: int = 65, early_max_odds: float = 2.0,
                             late_start: int = 75, late_end: int = 81, late_max_odds: float = 0.0):
    import bot.state as _state
    if not _state.RAPIDAPI_FOOTBALL_KEY:
        raise HTTPException(status_code=400, detail="rapidapi_football_key not configured in add-on settings")
    specs = _football_windows(windows, early_start, early_end, early_max_odds,
                              late_start, late_end, late_max_odds)
    try:
        from live_monitors.football import fetch_inplaying_data
        matches = await fetch_inplaying_data(specs)
        return {"count": len(matches), "matches": matches}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/football/provider/{provider_name}")
async def football_provider_test(provider_name: str, windows: str = "early,late",
                                 early_start: int = 55, early_end: int = 65, early_max_odds: float = 2.0,
                                 late_start: int = 75, late_end: int = 81, late_max_odds: float = 0.0):
    import bot.state as _state
    if provider_name not in _FOOTBALL_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Unknown provider: {provider_name}")
    if not _state.RAPIDAPI_FOOTBALL_KEY:
        raise HTTPException(status_code=400, detail="rapidapi_football_key not configured in add-on settings")
    specs = _football_windows(windows, early_start, early_end, early_max_odds,
                              late_start, late_end, late_max_odds)
    from live_monitors.football import fetch_provider_data
    return await fetch_provider_data(provider_name, specs)


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


# ── Car Mode ──────────────────────────────────────────────────────────────────

# A realistic storm alert rather than a lorem-ipsum string: the point of the test
# is to hear what the wording sounds like out loud, which a made-up sentence
# cannot tell you. It carries one of every construction the sanitiser rewrites —
# markup, a compound unit, a ratio, a bearing in degrees, a compass point and a
# separator — so anything broken is audible in a single message.
_CAR_MODE_SAMPLE = (
    "⛈️ <b>Temporale nel raggio — Casa</b>\n"
    "\U0001f4cd Fronte a <b>12 km</b> a O (270°)\n"
    "\U0001f3af Anello 2/4 · entro 20 km\n"
    "\U0001f9ed Rotta costante: ti arriva addosso\n"
    "\U0001f697 In movimento a 80 km/h verso NE\n"
    "\U0001f522 47 fulmini in 30 min (settore NE)"
)


@router.post("/api/carmode/test")
async def carmode_test():
    """Send the sample alert in Car Mode wording, whatever the toggle is set to.

    Deliberately calls `to_spoken` rather than `for_car`: you test this to decide
    whether to switch Car Mode on, so it has to work while it is still off.
    """
    import bot.state as _state
    from car_mode import to_spoken

    bot, chat_id = _state.get_bot_and_chat("default")
    if not bot:
        raise HTTPException(status_code=400, detail="No Telegram bot configured")
    try:
        await bot.send_message(chat_id=chat_id, text=to_spoken(_CAR_MODE_SAMPLE))
        return {"ok": True, "message": "Test message sent"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
