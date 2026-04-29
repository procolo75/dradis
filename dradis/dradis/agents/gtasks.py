import asyncio
from pathlib import Path

from agent_core import create_agent, _now_str
from web.server import SETTINGS_DEFAULTS

GTASKS_TOKEN_FILE   = Path("/data/google_tasks_token.json")
GTASKS_SCOPES       = ["https://www.googleapis.com/auth/tasks"]
GTASKS_REDIRECT_URI = "http://localhost:8099/gtasksauth/callback"


async def _notify_token_expired():
    try:
        import main as _main
        await _main._send_error_telegram(
            "🔑 <b>Google Tasks token scaduto o revocato.</b>\n"
            "Invia <code>/gtasksauth</code> per riconnetterti."
        )
    except Exception as ex:
        print(f"[DRADIS] Could not send token-expired notification: {ex}")


def _build_gtasks_flow(client_id: str, client_secret: str):
    from google_auth_oauthlib.flow import Flow
    client_config = {
        "installed": {
            "client_id":     client_id,
            "client_secret": client_secret,
            "auth_uri":      "https://accounts.google.com/o/oauth2/auth",
            "token_uri":     "https://oauth2.googleapis.com/token",
            "redirect_uris": [GTASKS_REDIRECT_URI],
        }
    }
    return Flow.from_client_config(client_config, scopes=GTASKS_SCOPES, redirect_uri=GTASKS_REDIRECT_URI)


def _get_gtasks_creds():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request as GoogleRequest
    from google.auth.exceptions import RefreshError
    if not GTASKS_TOKEN_FILE.exists():
        return None
    creds = Credentials.from_authorized_user_file(str(GTASKS_TOKEN_FILE), GTASKS_SCOPES)
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(GoogleRequest())
            GTASKS_TOKEN_FILE.write_text(creds.to_json())
        except RefreshError as e:
            print(f"[DRADIS] Google Tasks token refresh failed ({e}), deleting token file.")
            GTASKS_TOKEN_FILE.unlink(missing_ok=True)
            import asyncio as _aio
            try:
                loop = _aio.get_event_loop()
                if loop.is_running():
                    loop.create_task(_notify_token_expired())
            except Exception:
                pass
            return None
    return creds


def _sync_list_tasks(task_list: str) -> str:
    from googleapiclient.discovery import build as gtasks_build
    creds = _get_gtasks_creds()
    if not creds:
        return "NOT_AUTHENTICATED"
    service = gtasks_build("tasks", "v1", credentials=creds)
    result  = service.tasks().list(
        tasklist=task_list,
        showCompleted=False,
        showHidden=False,
        maxResults=50,
    ).execute()
    tasks = result.get("items", [])
    if not tasks:
        return "No open tasks."
    lines = []
    for t in tasks:
        due   = f" (due: {t['due'][:10]})" if t.get("due") else ""
        notes = f" — {t['notes']}" if t.get("notes") else ""
        lines.append(f"- [{t['id']}] {t.get('title', '(no title)')}{due}{notes}")
    return "\n".join(lines)


def _sync_create_task(task_list: str, title: str, notes: str, due: str) -> str:
    from googleapiclient.discovery import build as gtasks_build
    creds = _get_gtasks_creds()
    if not creds:
        return "NOT_AUTHENTICATED"
    service = gtasks_build("tasks", "v1", credentials=creds)
    body = {"title": title}
    if notes:
        body["notes"] = notes
    if due:
        if len(due) == 10:
            due = due + "T00:00:00.000Z"
        body["due"] = due
    result = service.tasks().insert(tasklist=task_list, body=body).execute()
    return f"Task created: {result.get('title', title)} (id: {result.get('id', '?')})"


def _sync_complete_task(task_list: str, task_id: str) -> str:
    from googleapiclient.discovery import build as gtasks_build
    creds = _get_gtasks_creds()
    if not creds:
        return "NOT_AUTHENTICATED"
    service = gtasks_build("tasks", "v1", credentials=creds)
    task    = service.tasks().get(tasklist=task_list, task=task_id).execute()
    service.tasks().patch(
        tasklist=task_list, task=task_id, body={"status": "completed"}
    ).execute()
    return f"Task '{task.get('title', task_id)}' marked as completed."


def _sync_delete_task(task_list: str, task_id: str) -> str:
    from googleapiclient.discovery import build as gtasks_build
    creds = _get_gtasks_creds()
    if not creds:
        return "NOT_AUTHENTICATED"
    service = gtasks_build("tasks", "v1", credentials=creds)
    task    = service.tasks().get(tasklist=task_list, task=task_id).execute()
    service.tasks().delete(tasklist=task_list, task=task_id).execute()
    return f"Task '{task.get('title', task_id)}' deleted."


def _sync_update_task(task_list: str, task_id: str, title: str, notes: str) -> str:
    from googleapiclient.discovery import build as gtasks_build
    creds = _get_gtasks_creds()
    if not creds:
        return "NOT_AUTHENTICATED"
    service = gtasks_build("tasks", "v1", credentials=creds)
    body = {}
    if title:
        body["title"] = title
    if notes:
        body["notes"] = notes
    if not body:
        return "Nothing to update."
    service.tasks().patch(tasklist=task_list, task=task_id, body=body).execute()
    return "Task updated."


def create_gtasks_agent(settings: dict):
    tz_name      = settings.get("timezone", "UTC") or "UTC"
    _not_auth_msg = "Google Tasks not authenticated. Send /gtasksauth to connect."

    base_prompt = (
        f"It is {_now_str(tz_name)} ({tz_name}). "
        "You are a Google Tasks assistant. Manage the user's task list clearly and concisely "
        "in the same language the user used. Always show task IDs in brackets so the user "
        "can reference them for future operations. Never show completed tasks when listing. "
        + settings.get("gtasks_instructions", "")
    )

    async def list_tasks(task_list: str = "@default") -> str:
        """List all open tasks in a Google Tasks list.
        Call this when the user wants to see, check, or review their tasks, to-do list, or things to do.
        Trigger phrases: 'cosa ho da fare', 'lista task', 'todo', 'mostrami i task', 'what do I have to do', 'show tasks'."""
        loop   = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, _sync_list_tasks, task_list)
        if result == "NOT_AUTHENTICATED":
            return _not_auth_msg
        return result

    async def create_task(
        title: str,
        notes: str = "",
        due: str = "",
        task_list: str = "@default",
    ) -> str:
        """Create a new task in Google Tasks.
        Call this when the user wants to add, create, or note down a task or to-do item.
        Trigger phrases: 'aggiungi', 'crea task', 'aggiungi alla lista', 'add task', 'remember to', 'ricorda di'.
        The due parameter is optional and must be a date in YYYY-MM-DD format."""
        loop   = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, _sync_create_task, task_list, title, notes, due)
        if result == "NOT_AUTHENTICATED":
            return _not_auth_msg
        return result

    async def complete_task(task_id: str, task_list: str = "@default") -> str:
        """Mark a task as completed in Google Tasks.
        Call this when the user marks a task as done, finished, or completed.
        Trigger phrases: 'fatto', 'completato', 'segna come fatto', 'done', 'mark complete'.
        IMPORTANT: first call list_tasks to retrieve the task ID, then call this with that ID."""
        loop   = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, _sync_complete_task, task_list, task_id)
        if result == "NOT_AUTHENTICATED":
            return _not_auth_msg
        return result

    async def delete_task(task_id: str, task_list: str = "@default") -> str:
        """Delete a task from Google Tasks permanently.
        Call this when the user wants to remove or delete a task (not just complete it).
        Trigger phrases: 'cancella', 'elimina task', 'rimuovi', 'delete task', 'remove task'.
        IMPORTANT: first call list_tasks to retrieve the task ID, then call this with that ID."""
        loop   = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, _sync_delete_task, task_list, task_id)
        if result == "NOT_AUTHENTICATED":
            return _not_auth_msg
        return result

    async def update_task(
        task_id: str,
        title: str = "",
        notes: str = "",
        task_list: str = "@default",
    ) -> str:
        """Update the title or notes of an existing task in Google Tasks.
        Call this when the user wants to rename, edit, or modify a task.
        Trigger phrases: 'rinomina', 'modifica task', 'cambia il nome', 'update task', 'edit task'.
        IMPORTANT: first call list_tasks to retrieve the task ID, then call this with that ID."""
        loop   = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, _sync_update_task, task_list, task_id, title, notes)
        if result == "NOT_AUTHENTICATED":
            return _not_auth_msg
        return result

    return create_agent(
        system_prompt=base_prompt,
        model=settings.get("gtasks_model") or settings.get("model", SETTINGS_DEFAULTS["model"]),
        provider=settings.get("gtasks_provider") or settings.get("provider", SETTINGS_DEFAULTS["provider"]),
        tools=[list_tasks, create_task, complete_task, delete_task, update_task],
        name="gtasks",
        tool_call_limit=4,
    )
