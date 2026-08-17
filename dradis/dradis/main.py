"""
main.py
────────
DRADIS entry point. Wires the Telegram bot, APScheduler, and uvicorn web
server together and starts the asyncio event loop.

All business logic lives in bot/ and web/; this file only performs startup
orchestration.
"""

import asyncio
import os
import shutil

import uvicorn
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

import bot.state as _state
from live_monitors.storm_front import (
    migrate_lightning_configs, migrate_position_source_configs,
)
from bot.scheduler import (
    reload_task_jobs,
    reload_monitor_jobs,
    reload_live_monitors,
    reload_ha_monitors,
    run_scheduled_task,
    run_scheduled_monitor,
    _live_status_dispatcher,
)
from bot.handlers import (
    COMMANDS,
    handle_message,
    handle_voice,
    cmd_menu,
    cmd_tasks,
    cmd_monitors,
    cmd_ha_monitors,
    cmd_manage,
    cmd_car,
    cmd_rain,
    cmd_storm,
    handle_task_callback,
    handle_monitor_callback,
    handle_ha_monitor_callback,
    handle_live_monitor_callback,
    handle_mgmt_callback,
    handle_snapshot_callback,
)
from bot.commands import (
    cmd_info,
    cmd_gcalauth,
    cmd_gmailauth,
    cmd_gtasksauth,
    cmd_backupauth,
)
from web.server import app as web_app
from web.store import (
    load_live_monitors,
    save_live_monitors,
    register_tasks_changed_callback,
    register_run_task_callback,
    register_monitors_changed_callback,
    register_run_monitor_callback,
    register_live_monitors_changed_callback,
    register_live_monitor_status_callback,
    register_ha_monitors_changed_callback,
    register_ha_monitor_status_callback,
    register_bots_changed_callback,
    register_positions_changed_callback,
    migrate_legacy_position,
)
from live_monitors.ha import ha_monitor_manager


def build_telegram_app():
    app = ApplicationBuilder().token(_state.TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("info",       cmd_info))
    app.add_handler(CommandHandler("menu",       cmd_menu))
    app.add_handler(CommandHandler("tasks",      cmd_tasks))
    app.add_handler(CommandHandler("monitors",   cmd_monitors))
    app.add_handler(CommandHandler("hamonitors", cmd_ha_monitors))
    app.add_handler(CommandHandler("rain",       cmd_rain))
    app.add_handler(CommandHandler("storm",      cmd_storm))
    app.add_handler(CommandHandler("manage",     cmd_manage))
    app.add_handler(CommandHandler("car",        cmd_car))
    app.add_handler(CommandHandler("gcalauth",   cmd_gcalauth))
    app.add_handler(CommandHandler("gmailauth",  cmd_gmailauth))
    app.add_handler(CommandHandler("gtasksauth",  cmd_gtasksauth))
    app.add_handler(CommandHandler("backupauth",  cmd_backupauth))
    app.add_handler(CallbackQueryHandler(handle_task_callback,         pattern=r"^task:"))
    app.add_handler(CallbackQueryHandler(handle_monitor_callback,      pattern=r"^monitor:"))
    app.add_handler(CallbackQueryHandler(handle_ha_monitor_callback,   pattern=r"^ha:"))
    app.add_handler(CallbackQueryHandler(handle_live_monitor_callback, pattern=r"^live:"))
    app.add_handler(CallbackQueryHandler(handle_mgmt_callback,         pattern=r"^mgmt:"))
    app.add_handler(CallbackQueryHandler(handle_snapshot_callback,     pattern=r"^snap:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    return app


async def _register_commands(bot):
    try:
        await bot.delete_my_commands()
        await bot.set_my_commands(COMMANDS)
        print(f"[DRADIS] Telegram commands registered: {[c.command for c in COMMANDS]}")
    except Exception as e:
        print(f"[DRADIS] WARNING: could not register commands: {e}")


def _migrate_live_monitors() -> None:
    """Convert monitors saved under the retired 'lightning' type (v3.x).

    Runs before the first reload: without it a monitor configured before v4.0.0
    would keep a type no manager claims, so it would simply never start —
    silently, with no error the user could see.
    """
    try:
        configs, migrated = migrate_lightning_configs(load_live_monitors())
        if migrated:
            save_live_monitors(configs)
            print(f"[DRADIS] Migrated {migrated} lightning monitor(s) to storm_front")
            for stale in ("/data/lightning_state.json",):
                try:
                    os.remove(stale)
                except OSError:
                    pass
            shutil.rmtree("/data/lightning_rec", ignore_errors=True)
    except Exception as e:
        print(f"[DRADIS] WARNING: lightning migration failed: {e}")


def _migrate_positions() -> None:
    """Fold the unreleased single-position shape into the named position list.

    v4.1.0 kept one position in the global settings and marked monitors with
    `position_source: "live"`. Positions are now named entities a monitor selects
    by id, so both halves move together: without this a monitor would point at a
    source that no longer exists and would simply stay frozen.
    """
    try:
        new_id = migrate_legacy_position()
        configs, migrated = migrate_position_source_configs(
            load_live_monitors(), new_id)
        if migrated:
            save_live_monitors(configs)
            print(f"[DRADIS] Migrated {migrated} monitor(s) to named positions")
        elif new_id:
            print("[DRADIS] Migrated the saved position to the named list")
    except Exception as e:
        print(f"[DRADIS] WARNING: position migration failed: {e}")


async def main():
    _state._init_settings()
    _state._main_loop = asyncio.get_running_loop()
    telegram_app = build_telegram_app()
    web_server   = uvicorn.Server(
        uvicorn.Config(web_app, host="0.0.0.0", port=_state.WEB_PORT, log_level="warning")
    )
    async with telegram_app:
        await telegram_app.start()
        await telegram_app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        await _register_commands(telegram_app.bot)
        _state._telegram_bot = telegram_app.bot
        register_bots_changed_callback(_state.reload_extra_bots)
        register_positions_changed_callback(reload_live_monitors)
        _state.reload_extra_bots()
        _state._scheduler.start()

        register_tasks_changed_callback(reload_task_jobs)
        register_run_task_callback(run_scheduled_task)
        register_monitors_changed_callback(reload_monitor_jobs)
        register_run_monitor_callback(run_scheduled_monitor)
        register_live_monitors_changed_callback(reload_live_monitors)
        register_live_monitor_status_callback(_live_status_dispatcher)
        register_ha_monitors_changed_callback(reload_ha_monitors)
        register_ha_monitor_status_callback(ha_monitor_manager.status)

        reload_task_jobs()
        reload_monitor_jobs()
        _migrate_live_monitors()
        _migrate_positions()
        reload_live_monitors()
        reload_ha_monitors()

        settings    = _state.read_settings()
        startup_msg = settings.get("startup_message", _state.SETTINGS_DEFAULTS["startup_message"])
        await telegram_app.bot.send_message(chat_id=_state.ALLOWED_CHAT_ID, text=startup_msg)
        print(f"[DRADIS] Started. Web UI on port {_state.WEB_PORT}.")
        await web_server.serve()

        _state._scheduler.shutdown(wait=False)
        await telegram_app.updater.stop()
        await telegram_app.stop()


if __name__ == "__main__":
    asyncio.run(main())
