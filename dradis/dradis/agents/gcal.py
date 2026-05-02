import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_core import create_agent, _now_str
from web.server import SETTINGS_DEFAULTS

GCAL_TOKEN_FILE   = Path("/data/google_calendar_token.json")
GCAL_SCOPES       = ["https://www.googleapis.com/auth/calendar"]
GCAL_REDIRECT_URI = "http://localhost:8099/gcalauth/callback"


async def _notify_token_expired():
    try:
        import main as _main
        await _main._send_error_telegram(
            "🔑 <b>Google Calendar token scaduto o revocato.</b>\n"
            "Invia <code>/gcalauth</code> per riconnetterti."
        )
    except Exception as ex:
        print(f"[DRADIS] Could not send token-expired notification: {ex}")


def _build_gcal_flow(client_id: str, client_secret: str):
    from google_auth_oauthlib.flow import Flow
    client_config = {
        "installed": {
            "client_id":     client_id,
            "client_secret": client_secret,
            "auth_uri":      "https://accounts.google.com/o/oauth2/auth",
            "token_uri":     "https://oauth2.googleapis.com/token",
            "redirect_uris": [GCAL_REDIRECT_URI],
        }
    }
    return Flow.from_client_config(client_config, scopes=GCAL_SCOPES, redirect_uri=GCAL_REDIRECT_URI)


def _get_gcal_creds():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request as GoogleRequest
    from google.auth.exceptions import RefreshError
    if not GCAL_TOKEN_FILE.exists():
        return None
    creds = Credentials.from_authorized_user_file(str(GCAL_TOKEN_FILE), GCAL_SCOPES)
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(GoogleRequest())
            GCAL_TOKEN_FILE.write_text(creds.to_json())
        except RefreshError as e:
            print(f"[DRADIS] GCal token refresh failed ({e}), deleting token file.")
            GCAL_TOKEN_FILE.unlink(missing_ok=True)
            import asyncio as _aio
            try:
                loop = _aio.get_event_loop()
                if loop.is_running():
                    loop.create_task(_notify_token_expired())
            except Exception:
                pass
            return None
    return creds


def _sync_get_raw_events(days_ahead: int) -> str:
    from googleapiclient.discovery import build as gcal_build
    creds = _get_gcal_creds()
    if not creds:
        return "NOT_AUTHENTICATED"
    service = gcal_build("calendar", "v3", credentials=creds)
    now     = datetime.now(timezone.utc)
    result  = service.events().list(
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
    return "Event deleted successfully."


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


def create_gcal_agent(settings: dict):
    tz_name = settings.get("timezone", "UTC") or "UTC"
    _not_auth_msg = "Google Calendar not authenticated. Send /gcalauth to connect."

    base_prompt = (
        f"It is {_now_str(tz_name)} ({tz_name}). "
        "You are a Google Calendar assistant. Present calendar data clearly and concisely "
        "in the same language the user used. Never invent events not present in the data. "
        "Include the event ID in brackets at the end of each line so the user can reference it. "
        + settings.get("gcal_instructions", "")
    )

    async def create_calendar_event(
        title: str,
        start_datetime: str,
        end_datetime: str,
        description: str = "",
    ) -> str:
        """Create a new Google Calendar event. start_datetime and end_datetime must be ISO 8601 with timezone (e.g. 2026-04-20T10:00:00+02:00).
        Call this when the user wants to add, create, or schedule a meeting, event, appointment, or reminder.
        Trigger phrases: 'aggiungi', 'crea evento', 'metti in agenda', 'segna', 'schedule', 'add event'.
        Infer date/time from the message. If duration is not specified, default to 1 hour."""
        loop   = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None, _sync_create_raw_event, title, start_datetime, end_datetime, description
        )
        if result == "NOT_AUTHENTICATED":
            return _not_auth_msg
        return result

    async def delete_calendar_event(event_id: str) -> str:
        """Delete a Google Calendar event by its ID.
        Call this when the user wants to delete, remove, or cancel an event.
        Trigger phrases: 'cancella', 'elimina', 'rimuovi evento', 'delete event', 'cancel meeting'.
        IMPORTANT: first call get_calendar_events to retrieve the event ID, then call this with that ID.
        Never confirm a deletion without actually calling this tool."""
        loop   = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, _sync_delete_event, event_id)
        if result == "NOT_AUTHENTICATED":
            return _not_auth_msg
        return result

    async def get_calendar_events(days_ahead: int = 7) -> str:
        """Get Google Calendar events for the next N days (default: 7). Returns event IDs needed for deletion.
        Call this when the user asks about their schedule, agenda, appointments, or upcoming events.
        Trigger phrases: 'cosa ho in agenda', 'appuntamenti', 'eventi', 'calendario', 'my schedule', 'upcoming events'."""
        loop = asyncio.get_running_loop()
        raw  = await loop.run_in_executor(None, _sync_get_raw_events, days_ahead)
        if raw == "NOT_AUTHENTICATED":
            return _not_auth_msg
        return raw

    return create_agent(
        system_prompt=base_prompt,
        model=settings.get("gcal_model", SETTINGS_DEFAULTS["gcal_model"]),
        provider=settings.get("gcal_provider", SETTINGS_DEFAULTS["gcal_provider"]),
        tools=[get_calendar_events, create_calendar_event, delete_calendar_event],
        name="gcal",
        tool_call_limit=4,
    )
