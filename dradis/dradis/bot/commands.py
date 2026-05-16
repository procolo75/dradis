"""
bot/commands.py
────────────────
Telegram command handlers that require extra state:
  /info, /gcalauth, /gmailauth, /gtasksauth, /todo
"""

import asyncio
import html
from urllib.parse import parse_qs, urlparse

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

import bot.state as _state
from web.store import (
    pop_gcal_pending_code,
    pop_gmail_pending_code,
    pop_gtasks_pending_code,
    set_gcal_code_event,
    set_gmail_code_event,
    set_gtasks_code_event,
)
from agents.gcal    import GCAL_TOKEN_FILE, _build_gcal_flow, create_gcal_agent
from agents.gmail   import GMAIL_TOKEN_FILE, _build_gmail_flow, create_gmail_agent
from agents.gtasks  import GTASKS_TOKEN_FILE, _build_gtasks_flow, create_gtasks_agent

_SETTINGS_DEFAULTS = _state.SETTINGS_DEFAULTS


# ── /info ─────────────────────────────────────────────────────────────────────

async def cmd_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != _state.ALLOWED_CHAT_ID:
        return
    settings = _state.read_settings()

    lines = [
        "<b>DRADIS</b>",
        f"Provider: {settings.get('provider', _SETTINGS_DEFAULTS['provider'])}",
        f"Model: {settings.get('model', _SETTINGS_DEFAULTS['model'])}",
        f"History: {'on' if settings.get('history_enabled', True) else 'off'} "
        f"({settings.get('history_depth', _SETTINGS_DEFAULTS['history_depth'])} exchanges)",
    ]

    for key, label in [("ws_enabled", "Web Search"), ("weather_enabled", "Weather")]:
        on = settings.get(key, False)
        lines += ["", f"<b>{label}</b>", f"Status: {'enabled' if on else 'disabled'}"]
        if on:
            model_key = "ws_model" if key == "ws_enabled" else "weather_model"
            lines.append(f"Model: {settings.get(model_key, _SETTINGS_DEFAULTS[model_key])}")

    voice_on = settings.get("voice_enabled", False)
    lines += ["", "<b>Voice</b>", f"Status: {'enabled' if voice_on else 'disabled'}"]
    if voice_on:
        lines.append(f"Model: {settings.get('voice_model', _SETTINGS_DEFAULTS['voice_model'])}")
        lines.append(f"Language: {settings.get('voice_language', _SETTINGS_DEFAULTS['voice_language'])}")

    for key, label, token_file, auth_cmd in [
        ("gcal_enabled",   "Google Calendar", GCAL_TOKEN_FILE,   "/gcalauth"),
        ("gmail_enabled",  "Gmail",           GMAIL_TOKEN_FILE,  "/gmailauth"),
        ("gtasks_enabled", "Google Tasks",    GTASKS_TOKEN_FILE, "/gtasksauth"),
    ]:
        on   = settings.get(key, False)
        auth = token_file.exists()
        lines += ["", f"<b>{label}</b>", f"Status: {'enabled' if on else 'disabled'}"]
        if on:
            prov_key  = key.replace("_enabled", "_provider")
            model_key = key.replace("_enabled", "_model")
            lines.append(f"Provider: {settings.get(prov_key, _SETTINGS_DEFAULTS.get(prov_key, ''))}")
            lines.append(f"Model: {settings.get(model_key, _SETTINGS_DEFAULTS.get(model_key, ''))}")
            lines.append(f"Auth: {'✅ connected' if auth else f'❌ not authenticated — send {auth_cmd}'}")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ── Google Calendar OAuth ─────────────────────────────────────────────────────

_gcal_pending_flow = None


async def _gcal_complete_auth(flow, code: str, message) -> bool:
    global _gcal_pending_flow
    import web.store as _store
    _ev = _store._gcal_code_event
    try:
        loop  = asyncio.get_event_loop()
        creds = await loop.run_in_executor(
            None,
            lambda: (flow.fetch_token(code=code), flow.credentials)[1],
        )
        GCAL_TOKEN_FILE.write_text(creds.to_json())
        _gcal_pending_flow = None
        if _ev and not _ev.is_set():
            _ev.set()
        await message.reply_text(
            "✅ <b>Google Calendar connected!</b> You can now ask DRADIS about your calendar.",
            parse_mode=ParseMode.HTML,
        )
        return True
    except Exception as e:
        await message.reply_text(
            f"❌ Authorization failed: {html.escape(str(e))}",
            parse_mode=ParseMode.HTML,
        )
        return False


async def _gcal_auth_background(event: asyncio.Event, flow, message):
    try:
        await asyncio.wait_for(event.wait(), timeout=300)
        code = pop_gcal_pending_code()
        if code and not GCAL_TOKEN_FILE.exists():
            await _gcal_complete_auth(flow, code, message)
    except asyncio.TimeoutError:
        if not GCAL_TOKEN_FILE.exists():
            await message.reply_text("⏱ Authorization timed out (5 min). Send /gcalauth to try again.")


async def cmd_gcalauth(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _gcal_pending_flow
    if update.effective_user.id != _state.ALLOWED_CHAT_ID:
        return
    if not _state.GOOGLE_CLIENT_ID or not _state.GOOGLE_CLIENT_SECRET:
        await update.message.reply_text(
            "❌ <code>google_client_id</code> and <code>google_client_secret</code> are not configured.\n"
            "Add them in the add-on <b>Configuration</b> tab and restart.",
            parse_mode=ParseMode.HTML,
        )
        return

    args = context.args or []
    if not args:
        event = asyncio.Event()
        set_gcal_code_event(event)
        flow = _build_gcal_flow(_state.GOOGLE_CLIENT_ID, _state.GOOGLE_CLIENT_SECRET)
        _gcal_pending_flow = flow
        auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent")
        msg = (
            "📅 <b>Google Calendar — Authorization</b>\n\n"
            "1. Open this link in your browser:\n"
            f"<code>{html.escape(auth_url)}</code>\n\n"
            "2. Sign in with your Google account and grant access.\n"
            "3. Your browser will redirect back to DRADIS automatically ✅\n\n"
            "<i>If the redirect fails (HA on a different device), copy the full URL "
            "from the browser address bar and send it as:\n"
            "/gcalauth &lt;url&gt;</i>"
        )
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
        asyncio.create_task(_gcal_auth_background(event, flow, update.message))
        return

    raw  = " ".join(args)
    code = parse_qs(urlparse(raw).query).get("code", [raw])[0]
    if not code:
        await update.message.reply_text(
            "❌ Could not parse the authorization code. Make sure you copied the full redirect URL.",
            parse_mode=ParseMode.HTML,
        )
        return
    flow = _gcal_pending_flow or _build_gcal_flow(_state.GOOGLE_CLIENT_ID, _state.GOOGLE_CLIENT_SECRET)
    await _gcal_complete_auth(flow, code, update.message)


# ── Gmail OAuth ───────────────────────────────────────────────────────────────

_gmail_pending_flow = None


async def _gmail_complete_auth(flow, code: str, message) -> bool:
    global _gmail_pending_flow
    import web.store as _store
    _ev = _store._gmail_code_event
    try:
        loop  = asyncio.get_event_loop()
        creds = await loop.run_in_executor(
            None,
            lambda: (flow.fetch_token(code=code), flow.credentials)[1],
        )
        GMAIL_TOKEN_FILE.write_text(creds.to_json())
        _gmail_pending_flow = None
        if _ev and not _ev.is_set():
            _ev.set()
        await message.reply_text(
            "✅ <b>Gmail connected!</b> You can now ask DRADIS to read or send emails.",
            parse_mode=ParseMode.HTML,
        )
        return True
    except Exception as e:
        await message.reply_text(
            f"❌ Authorization failed: {html.escape(str(e))}",
            parse_mode=ParseMode.HTML,
        )
        return False


async def _gmail_auth_background(event: asyncio.Event, flow, message):
    try:
        await asyncio.wait_for(event.wait(), timeout=300)
        code = pop_gmail_pending_code()
        if code and not GMAIL_TOKEN_FILE.exists():
            await _gmail_complete_auth(flow, code, message)
    except asyncio.TimeoutError:
        if not GMAIL_TOKEN_FILE.exists():
            await message.reply_text("⏱ Authorization timed out (5 min). Send /gmailauth to try again.")


async def cmd_gmailauth(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _gmail_pending_flow
    if update.effective_user.id != _state.ALLOWED_CHAT_ID:
        return
    if not _state.GOOGLE_CLIENT_ID or not _state.GOOGLE_CLIENT_SECRET:
        await update.message.reply_text(
            "❌ <code>google_client_id</code> and <code>google_client_secret</code> are not configured.\n"
            "Add them in the add-on <b>Configuration</b> tab and restart.",
            parse_mode=ParseMode.HTML,
        )
        return

    args = context.args or []
    if not args:
        event = asyncio.Event()
        set_gmail_code_event(event)
        flow = _build_gmail_flow(_state.GOOGLE_CLIENT_ID, _state.GOOGLE_CLIENT_SECRET)
        _gmail_pending_flow = flow
        auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent")
        msg = (
            "📧 <b>Gmail — Authorization</b>\n\n"
            "1. Open this link in your browser:\n"
            f"<code>{html.escape(auth_url)}</code>\n\n"
            "2. Sign in with your Google account and grant access.\n"
            "3. Your browser will redirect back to DRADIS automatically ✅\n\n"
            "<i>If the redirect fails (HA on a different device), copy the full URL "
            "from the browser address bar and send it as:\n"
            "/gmailauth &lt;url&gt;</i>"
        )
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
        asyncio.create_task(_gmail_auth_background(event, flow, update.message))
        return

    raw  = " ".join(args)
    code = parse_qs(urlparse(raw).query).get("code", [raw])[0]
    if not code:
        await update.message.reply_text(
            "❌ Could not parse the authorization code. Make sure you copied the full redirect URL.",
            parse_mode=ParseMode.HTML,
        )
        return
    flow = _gmail_pending_flow or _build_gmail_flow(_state.GOOGLE_CLIENT_ID, _state.GOOGLE_CLIENT_SECRET)
    await _gmail_complete_auth(flow, code, update.message)


# ── Google Tasks OAuth ────────────────────────────────────────────────────────

_gtasks_pending_flow = None


async def _gtasks_complete_auth(flow, code: str, message) -> bool:
    global _gtasks_pending_flow
    import web.store as _store
    _ev = _store._gtasks_code_event
    try:
        loop  = asyncio.get_event_loop()
        creds = await loop.run_in_executor(
            None,
            lambda: (flow.fetch_token(code=code), flow.credentials)[1],
        )
        GTASKS_TOKEN_FILE.write_text(creds.to_json())
        _gtasks_pending_flow = None
        if _ev and not _ev.is_set():
            _ev.set()
        await message.reply_text(
            "✅ <b>Google Tasks connected!</b> You can now ask DRADIS about your tasks.",
            parse_mode=ParseMode.HTML,
        )
        return True
    except Exception as e:
        await message.reply_text(
            f"❌ Authorization failed: {html.escape(str(e))}",
            parse_mode=ParseMode.HTML,
        )
        return False


async def _gtasks_auth_background(event: asyncio.Event, flow, message):
    try:
        await asyncio.wait_for(event.wait(), timeout=300)
        code = pop_gtasks_pending_code()
        if code and not GTASKS_TOKEN_FILE.exists():
            await _gtasks_complete_auth(flow, code, message)
    except asyncio.TimeoutError:
        if not GTASKS_TOKEN_FILE.exists():
            await message.reply_text("⏱ Authorization timed out (5 min). Send /gtasksauth to try again.")


async def cmd_gtasksauth(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _gtasks_pending_flow
    if update.effective_user.id != _state.ALLOWED_CHAT_ID:
        return
    if not _state.GOOGLE_CLIENT_ID or not _state.GOOGLE_CLIENT_SECRET:
        await update.message.reply_text(
            "❌ <code>google_client_id</code> and <code>google_client_secret</code> are not configured.\n"
            "Add them in the add-on <b>Configuration</b> tab and restart.",
            parse_mode=ParseMode.HTML,
        )
        return

    args = context.args or []
    if not args:
        event = asyncio.Event()
        set_gtasks_code_event(event)
        flow = _build_gtasks_flow(_state.GOOGLE_CLIENT_ID, _state.GOOGLE_CLIENT_SECRET)
        _gtasks_pending_flow = flow
        auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent")
        msg = (
            "📝 <b>Google Tasks — Authorization</b>\n\n"
            "1. Open this link in your browser:\n"
            f"<code>{html.escape(auth_url)}</code>\n\n"
            "2. Sign in with your Google account and grant access.\n"
            "3. Your browser will redirect back to DRADIS automatically ✅\n\n"
            "<i>If the redirect fails (HA on a different device), copy the full URL "
            "from the browser address bar and send it as:\n"
            "/gtasksauth &lt;url&gt;</i>"
        )
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
        asyncio.create_task(_gtasks_auth_background(event, flow, update.message))
        return

    raw  = " ".join(args)
    code = parse_qs(urlparse(raw).query).get("code", [raw])[0]
    if not code:
        await update.message.reply_text(
            "❌ Could not parse the authorization code. Make sure you copied the full redirect URL.",
            parse_mode=ParseMode.HTML,
        )
        return
    flow = _gtasks_pending_flow or _build_gtasks_flow(_state.GOOGLE_CLIENT_ID, _state.GOOGLE_CLIENT_SECRET)
    await _gtasks_complete_auth(flow, code, update.message)


# ── /todo ─────────────────────────────────────────────────────────────────────

async def cmd_todo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != _state.ALLOWED_CHAT_ID:
        return
    settings = _state.read_settings()
    if not settings.get("gtasks_enabled", False):
        await update.message.reply_text("📝 Google Tasks is not enabled. Enable it from the Web UI.")
        return
    if not GTASKS_TOKEN_FILE.exists():
        await update.message.reply_text("📝 Google Tasks not authenticated. Send /gtasksauth to connect.")
        return
    agent = create_gtasks_agent(settings)
    try:
        response = await agent.arun("List all open tasks")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {html.escape(str(e))}")
        return
    text = (response.content or "").strip()
    if text:
        await update.message.reply_text(
            _state.md_to_html(text) + "\n\n<i>🤖 DRADIS · Google Tasks</i>",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text("📭 No open tasks.")
