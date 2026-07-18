"""
bot/handlers.py
────────────────
Telegram message and callback query handlers:
  handle_message, handle_voice, cmd_menu, cmd_tasks, cmd_monitors,
  handle_task_callback, handle_monitor_callback, handle_live_monitor_callback.
"""

import asyncio
import html
import os
import tempfile
import time

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

import bot.state as _state
from bot.scheduler import (
    run_scheduled_task,
    run_scheduled_monitor,
    _live_status_dispatcher,
)
from live_monitors.ha import ha_monitor_manager
from web.store import (
    load_tasks,
    load_monitors,
    load_live_monitors,
    load_ha_monitors,
    toggle_task,
    toggle_monitor,
    toggle_live_monitor,
    toggle_ha_monitor,
)

COMMANDS = [
    BotCommand("info",       "Status and configuration of all agents"),
    BotCommand("menu",       "List all available commands"),
    BotCommand("tasks",      "List and run tasks (all, including disabled)"),
    BotCommand("monitors",   "List and run monitors (all, including disabled)"),
    BotCommand("hamonitors", "List HA monitors and their status"),
    BotCommand("manage",     "Enable / disable tasks and monitors"),
    BotCommand("gcalauth",   "Connect Google Calendar (OAuth2)"),
    BotCommand("gmailauth",  "Connect Gmail (OAuth2)"),
    BotCommand("gtasksauth",  "Connect Google Tasks (OAuth2)"),
    BotCommand("backupauth",  "Connect Google Drive for automatic backups (OAuth2)"),
]


# ── Message handler ───────────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != _state.ALLOWED_CHAT_ID:
        return
    settings        = _state.read_settings()
    history_enabled = settings.get("history_enabled", True)
    history_depth   = settings.get("history_depth", 2)

    question = update.message.text
    model    = settings.get("model", _state.SETTINGS_DEFAULTS["model"])
    history  = _state.history_messages() if history_enabled else None

    # Chat gets all available tools; the single agent decides which to call.
    result, used_fallback, error, fb_reason = await _state.run_dradis(
        question, settings, selected=None, history=history, context_label="chat",
    )

    if error is not None:
        fb_model_id = _state._apply_fallback_settings(settings).get("model", model)
        if used_fallback:
            err_msg = (
                f"❌ Both primary (<code>{html.escape(model)}</code>) and "
                f"fallback (<code>{html.escape(fb_model_id)}</code>) models failed: "
                f"{html.escape(str(error))}"
            )
        else:
            err_msg = (
                f"❌ Model error (<code>{html.escape(model)}</code>): {html.escape(str(error))}\n"
                "<i>No fallback model configured.</i>"
            )
        await _state._send_error_telegram(err_msg)
        await update.message.reply_text(err_msg, parse_mode=ParseMode.HTML)
        return

    if used_fallback:
        await _state._send_error_telegram(_state._fallback_msg(fb_reason))

    text   = (result.content or "").strip()
    footer = _state.token_footer(settings, result)
    footer = f"\n\n<i>{footer}</i>" if footer else ""

    if history_enabled:
        _state.save_turn("user", question, history_depth)
        _state.save_turn("assistant", text, history_depth)

    if text:
        await update.message.reply_text(
            _state.md_to_html(text) + footer,
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text(
            f"⚠️ Model <code>{html.escape(model)}</code> returned no text.{footer}",
            parse_mode=ParseMode.HTML,
        )


# ── Voice handler ─────────────────────────────────────────────────────────────

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != _state.ALLOWED_CHAT_ID:
        return
    settings = _state.read_settings()
    if not settings.get("voice_enabled", False):
        await update.message.reply_text("🎙️ Voice agent is not enabled. You can enable it from the Web UI.")
        return

    voice_model     = settings.get("voice_model",    _state.SETTINGS_DEFAULTS["voice_model"])
    voice_language  = settings.get("voice_language", _state.SETTINGS_DEFAULTS["voice_language"])
    send_transcript = settings.get("voice_send_transcription", True)
    t0    = time.time()
    voice = update.message.voice

    try:
        tg_file = await context.bot.get_file(voice.file_id)
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp_path = tmp.name
        await tg_file.download_to_drive(tmp_path)
    except Exception as e:
        await update.message.reply_text(
            f"❌ Could not download voice message: {html.escape(str(e))}",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        transcription = await _state.transcribe_voice(tmp_path, voice_model, voice_language)
    except Exception as e:
        await update.message.reply_text(
            f"❌ Transcription error: {html.escape(str(e))}",
            parse_mode=ParseMode.HTML,
        )
        return
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    print(f"[DRADIS] Voice transcribed in {time.time() - t0:.1f}s: {transcription[:80]!r}")

    if send_transcript:
        await update.message.reply_text(f"🎙️ {html.escape(transcription)}", parse_mode=ParseMode.HTML)

    class _VoiceMessage:
        def __init__(self, real_msg, text: str):
            self._msg = real_msg
            self.text = text
        def __getattr__(self, name):
            return getattr(self._msg, name)

    class _VoiceUpdate:
        def __init__(self, real_update: Update, text: str):
            self.effective_user = real_update.effective_user
            self.message        = _VoiceMessage(real_update.message, text)

    await handle_message(_VoiceUpdate(update, transcription), context)


# ── Menu / task / monitor commands ────────────────────────────────────────────

async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != _state.ALLOWED_CHAT_ID:
        return
    lines = "\n".join(f"/{c.command} — {c.description}" for c in COMMANDS)
    await update.message.reply_text(f"<b>DRADIS Commands:</b>\n\n{lines}", parse_mode=ParseMode.HTML)


# ── List helpers (shared by /tasks, /monitors, /hamonitors, /manage) ──────────

def _by_name(items: list) -> list:
    """Sort a list of config dicts alphabetically by name (case-insensitive)."""
    return sorted(items, key=lambda x: (x.get("name") or "").lower())


def _monitor_detail(m: dict) -> str:
    """Short parenthetical detail for a scheduled monitor (area/location)."""
    if m.get("type") == "seismic":
        return m.get("seismic_area", "?")
    return m.get("location", "?")


def _live_monitor_detail(m: dict) -> str:
    """Short parenthetical detail for a live monitor."""
    t = m.get("type")
    if t == "seismic":
        return ", ".join(m.get("areas", [])) or "—"
    if t == "football_betting":
        return "⚽ live"
    return m.get("location", "?")


async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != _state.ALLOWED_CHAT_ID:
        return
    tasks = load_tasks()
    if not tasks:
        await update.message.reply_text("No tasks configured. Add tasks from the Web UI.")
        return
    keyboard = []
    for t in _by_name(tasks):
        badge = "✅" if t.get("enabled") else "⏸"
        keyboard.append([InlineKeyboardButton(f"{badge} {t['name']}", callback_data=f"task:{t['id']}")])
    await update.message.reply_text(
        "Select a task to run:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def cmd_monitors(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != _state.ALLOWED_CHAT_ID:
        return
    scheduled = load_monitors()
    live      = load_live_monitors()
    if not scheduled and not live:
        await update.message.reply_text("No monitors configured. Add monitors from the Web UI.")
        return
    keyboard = []
    for m in _by_name(scheduled):
        badge  = "✅" if m.get("enabled") else "⏸"
        keyboard.append([InlineKeyboardButton(
            f"{badge} {m['name']} ({_monitor_detail(m)})",
            callback_data=f"monitor:{m['id']}",
        )])
    for m in _by_name(live):
        status = _live_status_dispatcher(m["id"])
        badge  = "🟢" if status == "running" else "🔴"
        label  = f"{badge} {m['name']} ({_live_monitor_detail(m)})"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"live:{m['id']}")])
    sections = []
    if scheduled:
        sections.append("Scheduled monitors — tap to run now:")
    if live:
        sections.append("Live monitors — tap to see status:")
    await update.message.reply_text(
        "\n\n".join(sections),
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def cmd_ha_monitors(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != _state.ALLOWED_CHAT_ID:
        return
    ha_monitors = load_ha_monitors()
    if not ha_monitors:
        await update.message.reply_text("No HA monitors configured. Add them from the Web UI.")
        return
    keyboard = []
    for m in _by_name(ha_monitors):
        status = ha_monitor_manager.status(m["id"])
        badge  = "🟢" if status == "running" else "🔴"
        keyboard.append([InlineKeyboardButton(f"{badge} {m['name']}", callback_data=f"ha:{m['id']}")])
    await update.message.reply_text(
        "HA monitors — tap to see status:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# ── Callback query handlers ───────────────────────────────────────────────────

async def handle_task_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != _state.ALLOWED_CHAT_ID:
        await query.answer()
        return
    task_id = query.data.removeprefix("task:")
    task    = next((t for t in load_tasks() if t["id"] == task_id), None)
    await query.answer()
    if not task:
        await query.message.reply_text("❌ Task not found.")
        return
    await query.message.reply_text(
        f"▶️ Launching task <b>{html.escape(task['name'])}</b>…",
        parse_mode=ParseMode.HTML,
    )
    asyncio.create_task(run_scheduled_task(task))


async def handle_monitor_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != _state.ALLOWED_CHAT_ID:
        await query.answer()
        return
    monitor_id = query.data.removeprefix("monitor:")
    monitor    = next((m for m in load_monitors() if m["id"] == monitor_id), None)
    await query.answer()
    if not monitor:
        await query.message.reply_text("❌ Monitor not found.")
        return
    detail = (
        monitor.get("seismic_area", "?")
        if monitor.get("type") == "seismic"
        else monitor.get("location", "?")
    )
    await query.message.reply_text(
        f"▶️ Launching monitor <b>{html.escape(monitor['name'])}</b> ({html.escape(detail)})…",
        parse_mode=ParseMode.HTML,
    )
    asyncio.create_task(run_scheduled_monitor(monitor))


async def handle_ha_monitor_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != _state.ALLOWED_CHAT_ID:
        await query.answer()
        return
    monitor_id = query.data.removeprefix("ha:")
    monitor    = next((m for m in load_ha_monitors() if m["id"] == monitor_id), None)
    await query.answer()
    if not monitor:
        await query.message.reply_text("❌ HA monitor not found.")
        return
    status    = ha_monitor_manager.status(monitor_id)
    badge     = "🟢 Running" if status == "running" else "🔴 Stopped"
    entities  = monitor.get("entities", [])
    mode      = monitor.get("alert_mode", "llm").upper()
    cooldown  = monitor.get("cooldown_min", 60)
    ent_label = ", ".join(entities) if entities else "—"
    msg = (
        f"🏠 <b>{html.escape(monitor['name'])}</b>\n"
        f"Status: {badge}\n"
        f"Mode: {mode} — Cooldown: {cooldown} min\n"
        f"Entities ({len(entities)}): {html.escape(ent_label)}"
    )
    await query.message.reply_text(msg, parse_mode=ParseMode.HTML)


async def handle_live_monitor_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != _state.ALLOWED_CHAT_ID:
        await query.answer()
        return
    item_id = query.data.removeprefix("live:")
    monitor = next((m for m in load_live_monitors() if m["id"] == item_id), None)
    await query.answer()
    if not monitor:
        await query.message.reply_text("❌ Live monitor not found.")
        return
    mtype  = monitor.get("type", "lightning")
    status = _live_status_dispatcher(item_id)
    badge  = "🟢 Running" if status == "running" else "🔴 Stopped"
    if mtype == "seismic":
        areas = ", ".join(monitor.get("areas", [])) or "—"
        msg = (f"🌍 <b>{html.escape(monitor['name'])}</b>\n"
               f"Areas: {html.escape(areas)}\n"
               f"Status: {badge}\n"
               f"Polling: 60s")
    elif mtype == "football_betting":
        windows = ", ".join(monitor.get("windows") or ["55-65", "75-81"])
        msg = (f"⚽ <b>{html.escape(monitor['name'])}</b>\n"
               f"Tipo: Football Betting Live\n"
               f"Finestre: {windows}'\n"
               f"Status: {badge}\n"
               f"Polling: 300s")
    else:
        msg = (f"⚡ <b>{html.escape(monitor['name'])}</b>\n"
               f"📍 {html.escape(monitor.get('location', '?'))}\n"
               f"Status: {badge}\n"
               f"Radius: {monitor.get('radius_km', '?')} km — Cooldown: automatic (5/15/30 min)")
    await query.message.reply_text(msg, parse_mode=ParseMode.HTML)


# ── /manage ───────────────────────────────────────────────────────────────────

def _build_manage_keyboard() -> tuple[str, InlineKeyboardMarkup]:
    tasks    = load_tasks()
    monitors = load_monitors()
    live     = load_live_monitors()
    ha       = load_ha_monitors()
    keyboard = []
    if tasks:
        keyboard.append([InlineKeyboardButton("── 📋 Tasks ──", callback_data="mgmt:noop")])
        for t in _by_name(tasks):
            badge = "✅" if t.get("enabled") else "⏸"
            keyboard.append([InlineKeyboardButton(f"{badge} {t['name']}", callback_data=f"mgmt:task:{t['id']}")])
    if monitors:
        keyboard.append([InlineKeyboardButton("── 🌩 Monitors ──", callback_data="mgmt:noop")])
        for m in _by_name(monitors):
            badge = "✅" if m.get("enabled") else "⏸"
            keyboard.append([InlineKeyboardButton(f"{badge} {m['name']} ({_monitor_detail(m)})", callback_data=f"mgmt:monitor:{m['id']}")])
    if live:
        keyboard.append([InlineKeyboardButton("── ⚡ Live ──", callback_data="mgmt:noop")])
        for m in _by_name(live):
            badge = "✅" if m.get("enabled") else "⏸"
            keyboard.append([InlineKeyboardButton(f"{badge} {m['name']} ({_live_monitor_detail(m)})", callback_data=f"mgmt:live:{m['id']}")])
    if ha:
        keyboard.append([InlineKeyboardButton("── 🏠 HA ──", callback_data="mgmt:noop")])
        for m in _by_name(ha):
            badge = "✅" if m.get("enabled") else "⏸"
            keyboard.append([InlineKeyboardButton(f"{badge} {m['name']}", callback_data=f"mgmt:ha:{m['id']}")])
    total = len(tasks) + len(monitors) + len(live) + len(ha)
    text  = f"🔧 <b>Manage</b> — {total} component{'s' if total != 1 else ''}\nTap to toggle enable/disable:"
    return text, InlineKeyboardMarkup(keyboard)


async def cmd_manage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != _state.ALLOWED_CHAT_ID:
        return
    if not any([load_tasks(), load_monitors(), load_live_monitors(), load_ha_monitors()]):
        await update.message.reply_text("No components configured. Add them from the Web UI.")
        return
    text, markup = _build_manage_keyboard()
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def handle_mgmt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != _state.ALLOWED_CHAT_ID:
        await query.answer()
        return
    parts = query.data.split(":", 2)
    if len(parts) < 3 or parts[1] == "noop":
        await query.answer()
        return
    _, kind, item_id = parts
    toggle_fn = {
        "task":    toggle_task,
        "monitor": toggle_monitor,
        "live":    toggle_live_monitor,
        "ha":      toggle_ha_monitor,
    }.get(kind)
    if not toggle_fn:
        await query.answer()
        return
    new_state = toggle_fn(item_id)
    if new_state is None:
        await query.answer("❌ Not found.")
        return
    await query.answer("✅ Enabled" if new_state else "⏸ Disabled")
    text, markup = _build_manage_keyboard()
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    except Exception:
        pass
