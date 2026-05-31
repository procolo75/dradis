"""
backup/gdrive.py
─────────────────
Google Drive backup module for DRADIS.

Uploads sensitive configuration files to a "DRADIS Backup" folder on the
authenticated user's Google Drive. Uses a dedicated OAuth token with
drive.file scope (can only access files it created — no full Drive access).

Files backed up:
  /data/options.json              — add-on config (API keys, tokens)
  /data/dradis_settings.json      — DRADIS settings
  /data/google_calendar_token.json
  /data/google_gmail_token.json
  /data/google_tasks_token.json
  /data/tasks.json
  /data/monitors.json
  /data/live_monitors.json
  /data/ha_monitors.json
  /data/agents.json
"""

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

GDRIVE_TOKEN_FILE   = Path("/data/gdrive_backup_token.json")
GDRIVE_SCOPES       = ["https://www.googleapis.com/auth/drive.file"]
GDRIVE_REDIRECT_URI = "http://localhost:8099/backupauth/callback"
GDRIVE_FOLDER_NAME  = "DRADIS Backup"

_BACKUP_FILES = [
    Path("/data/options.json"),
    Path("/data/dradis_settings.json"),
    Path("/data/google_calendar_token.json"),
    Path("/data/google_gmail_token.json"),
    Path("/data/google_tasks_token.json"),
    Path("/data/gdrive_backup_token.json"),
    Path("/data/tasks.json"),
    Path("/data/monitors.json"),
    Path("/data/live_monitors.json"),
    Path("/data/ha_monitors.json"),
    Path("/data/agents.json"),
]


def build_gdrive_flow(client_id: str, client_secret: str):
    from google_auth_oauthlib.flow import Flow
    client_config = {
        "installed": {
            "client_id":     client_id,
            "client_secret": client_secret,
            "auth_uri":      "https://accounts.google.com/o/oauth2/auth",
            "token_uri":     "https://oauth2.googleapis.com/token",
            "redirect_uris": [GDRIVE_REDIRECT_URI],
        }
    }
    return Flow.from_client_config(
        client_config, scopes=GDRIVE_SCOPES, redirect_uri=GDRIVE_REDIRECT_URI
    )


def _get_gdrive_creds():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request as GoogleRequest
    from google.auth.exceptions import RefreshError

    if not GDRIVE_TOKEN_FILE.exists():
        return None
    creds = Credentials.from_authorized_user_file(str(GDRIVE_TOKEN_FILE), GDRIVE_SCOPES)
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(GoogleRequest())
            GDRIVE_TOKEN_FILE.write_text(creds.to_json())
        except RefreshError as e:
            print(f"[DRADIS] GDrive backup token refresh failed ({e}), deleting token.")
            GDRIVE_TOKEN_FILE.unlink(missing_ok=True)
            return None
    return creds


def _get_or_create_folder(service, folder_name: str) -> str:
    """Return the Drive folder ID, creating it if it doesn't exist."""
    query = (
        f"name='{folder_name}' and mimeType='application/vnd.google-apps.folder' "
        "and trashed=false"
    )
    results = service.files().list(q=query, fields="files(id, name)").execute()
    files = results.get("files", [])
    if files:
        return files[0]["id"]

    folder_meta = {
        "name":     folder_name,
        "mimeType": "application/vnd.google-apps.folder",
    }
    folder = service.files().create(body=folder_meta, fields="id").execute()
    return folder["id"]


def _upload_or_update(service, folder_id: str, file_path: Path) -> str:
    """Upload a file to Drive; if a same-named file already exists in the folder, update it."""
    from googleapiclient.http import MediaFileUpload

    name  = file_path.name
    query = f"name='{name}' and '{folder_id}' in parents and trashed=false"
    res   = service.files().list(q=query, fields="files(id)").execute()
    existing = res.get("files", [])

    media = MediaFileUpload(str(file_path), mimetype="application/json", resumable=False)

    if existing:
        file_id = existing[0]["id"]
        service.files().update(fileId=file_id, media_body=media).execute()
        return f"updated {name}"
    else:
        meta = {"name": name, "parents": [folder_id]}
        service.files().create(body=meta, media_body=media, fields="id").execute()
        return f"created {name}"


def _sync_run_backup() -> tuple[list[str], list[str]]:
    """Synchronous backup — called via run_in_executor. Returns (uploaded, skipped)."""
    from googleapiclient.discovery import build as gdrive_build

    creds = _get_gdrive_creds()
    if not creds:
        raise RuntimeError("Google Drive not authenticated. Send /backupauth to connect.")

    service   = gdrive_build("drive", "v3", credentials=creds)
    folder_id = _get_or_create_folder(service, GDRIVE_FOLDER_NAME)

    uploaded: list[str] = []
    skipped:  list[str] = []

    for fp in _BACKUP_FILES:
        if not fp.exists():
            skipped.append(fp.name)
            continue
        result = _upload_or_update(service, folder_id, fp)
        uploaded.append(result)

    return uploaded, skipped


async def run_backup_monitor(monitor: dict, tz_name: str = "UTC") -> str:
    """Async entry point called by bot/scheduler._MONITOR_RUNNERS."""
    loop = asyncio.get_running_loop()
    uploaded, skipped = await loop.run_in_executor(None, _sync_run_backup)

    now = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    lines = [
        f"☁️ <b>DRADIS Backup — Google Drive</b>",
        f"🕐 {now}",
        "",
    ]

    if uploaded:
        lines.append(f"✅ <b>{len(uploaded)} file{'s' if len(uploaded) != 1 else ''} backed up:</b>")
        for item in uploaded:
            lines.append(f"  • {item}")

    if skipped:
        lines.append(f"\n⚪ Skipped (not found): {', '.join(skipped)}")

    lines += ["", "<i>DRADIS Backup · Google Drive · drive.file scope</i>"]
    return "\n".join(lines)
