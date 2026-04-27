import asyncio
import base64
from email.message import EmailMessage
from pathlib import Path

from agent_core import create_agent, _now_str
from web.server import SETTINGS_DEFAULTS

GMAIL_TOKEN_FILE   = Path("/data/google_gmail_token.json")
GMAIL_SCOPES       = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://mail.google.com/",
]
GMAIL_REDIRECT_URI = "http://localhost:8099/gmailauth/callback"


async def _notify_token_expired(service: str, cmd: str):
    """Send a Telegram notification when a Google token has been revoked."""
    try:
        import main as _main  # imported lazily to avoid circular imports
        await _main._send_error_telegram(
            f"🔑 <b>{service} token scaduto o revocato.</b>\n"
            f"Invia <code>{cmd}</code> per riconnetterti."
        )
    except Exception as ex:
        print(f"[DRADIS] Could not send token-expired notification: {ex}")


def _build_gmail_flow(client_id: str, client_secret: str):
    from google_auth_oauthlib.flow import Flow
    client_config = {
        "installed": {
            "client_id":     client_id,
            "client_secret": client_secret,
            "auth_uri":      "https://accounts.google.com/o/oauth2/auth",
            "token_uri":     "https://oauth2.googleapis.com/token",
            "redirect_uris": [GMAIL_REDIRECT_URI],
        }
    }
    return Flow.from_client_config(client_config, scopes=GMAIL_SCOPES, redirect_uri=GMAIL_REDIRECT_URI)


def _get_gmail_creds():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request as GoogleRequest
    from google.auth.exceptions import RefreshError
    if not GMAIL_TOKEN_FILE.exists():
        return None
    creds = Credentials.from_authorized_user_file(str(GMAIL_TOKEN_FILE), GMAIL_SCOPES)
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(GoogleRequest())
            GMAIL_TOKEN_FILE.write_text(creds.to_json())
        except RefreshError as e:
            # Token revoked or expired (invalid_grant): delete it so the user
            # gets a clean re-auth prompt instead of a cryptic crash.
            print(f"[DRADIS] Gmail token refresh failed ({e}), deleting token file.")
            GMAIL_TOKEN_FILE.unlink(missing_ok=True)
            import asyncio as _aio
            try:
                loop = _aio.get_event_loop()
                if loop.is_running():
                    loop.create_task(_notify_token_expired("Gmail", "/gmailauth"))
            except Exception:
                pass
            return None
    return creds


def _sync_get_emails(max_results: int) -> str:
    from googleapiclient.discovery import build as gmail_build
    creds = _get_gmail_creds()
    if not creds:
        return "NOT_AUTHENTICATED"
    service = gmail_build("gmail", "v1", credentials=creds)
    result  = service.users().messages().list(userId="me", maxResults=max_results).execute()
    return _format_message_list(service, result.get("messages", []))


def _sync_get_unread_emails(max_results: int) -> str:
    from googleapiclient.discovery import build as gmail_build
    creds = _get_gmail_creds()
    if not creds:
        return "NOT_AUTHENTICATED"
    service = gmail_build("gmail", "v1", credentials=creds)
    result  = service.users().messages().list(userId="me", q="is:unread", maxResults=max_results).execute()
    return _format_message_list(service, result.get("messages", []))


def _sync_search_emails(query: str, max_results: int) -> str:
    from googleapiclient.discovery import build as gmail_build
    creds = _get_gmail_creds()
    if not creds:
        return "NOT_AUTHENTICATED"
    service = gmail_build("gmail", "v1", credentials=creds)
    result  = service.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
    return _format_message_list(service, result.get("messages", []))


def _format_message_list(service, messages: list) -> str:
    if not messages:
        return "No emails found."
    lines = []
    for msg in messages:
        try:
            detail  = service.users().messages().get(
                userId="me", id=msg["id"], format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            ).execute()
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


async def fetch_gmail_inbox(max_results: int = 10) -> str:
    import asyncio as _asyncio
    loop = _asyncio.get_running_loop()
    return await loop.run_in_executor(None, _sync_get_emails, max_results)


def create_gmail_agent(settings: dict, prefetched_data: str | None = None):
    tz_name = settings.get("timezone", "UTC") or "UTC"
    _not_auth_msg = "Gmail not authenticated. Send /gmailauth to connect."

    base_prompt = (
        f"It is {_now_str(tz_name)} ({tz_name}). "
        "You are an email assistant. Present emails clearly and concisely "
        "in the same language the user used. Never invent data not present in the results. "
        "Include the message ID in brackets so the user can reference it. "
        + settings.get("gmail_instructions", "")
    )

    async def search_emails(query: str, max_results: int = 10) -> str:
        """Search Gmail emails using Gmail query syntax (e.g. 'from:user@example.com subject:invoice').
        Call this when the user wants to find emails from a specific sender, subject, or date.
        Trigger phrases: 'cerca email da X', 'find emails about Y', 'search mail'."""
        loop = asyncio.get_running_loop()
        raw  = await loop.run_in_executor(None, _sync_search_emails, query, max_results)
        if raw == "NOT_AUTHENTICATED":
            return _not_auth_msg
        return raw

    async def send_email(to: str, subject: str, body: str) -> str:
        """Send an email via Gmail. to is the recipient address, subject is the email subject, body is plain text.
        Call this when the user wants to send, compose, or write an email.
        Trigger phrases: 'manda una email', 'send email to', 'scrivi a', 'reply'.
        IMPORTANT: never call this without first confirming the recipient and subject with the user."""
        loop   = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, _sync_send_email, to, subject, body)
        if result == "NOT_AUTHENTICATED":
            return _not_auth_msg
        return result

    if prefetched_data:
        return create_agent(
            system_prompt=base_prompt + f"\n\nPre-fetched inbox:\n{prefetched_data}",
            model=settings.get("gmail_model", SETTINGS_DEFAULTS["gmail_model"]),
            provider=settings.get("gmail_provider", SETTINGS_DEFAULTS["gmail_provider"]),
            tools=[search_emails, send_email],
            name="gmail",
            tool_call_limit=4,
        )

    async def get_emails(max_results: int = 10) -> str:
        """Get the latest emails from Gmail inbox.
        Call this when the user asks to check or list their emails or inbox.
        Trigger phrases: 'controlla le email', 'check my email', 'show inbox', 'any emails'."""
        loop = asyncio.get_running_loop()
        raw  = await loop.run_in_executor(None, _sync_get_emails, max_results)
        if raw == "NOT_AUTHENTICATED":
            return _not_auth_msg
        return raw

    async def get_unread_emails(max_results: int = 10) -> str:
        """Get unread emails from Gmail.
        Call this when the user asks specifically about unread messages.
        Trigger phrases: 'email non lette', 'unread emails', 'nuovi messaggi'."""
        loop = asyncio.get_running_loop()
        raw  = await loop.run_in_executor(None, _sync_get_unread_emails, max_results)
        if raw == "NOT_AUTHENTICATED":
            return _not_auth_msg
        return raw

    return create_agent(
        system_prompt=base_prompt,
        model=settings.get("gmail_model", SETTINGS_DEFAULTS["gmail_model"]),
        provider=settings.get("gmail_provider", SETTINGS_DEFAULTS["gmail_provider"]),
        tools=[get_emails, get_unread_emails, search_emails, send_email],
        name="gmail",
        tool_call_limit=4,
    )
