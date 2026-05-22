"""
bot/scheduler.py
─────────────────
APScheduler cron jobs for scheduled tasks and monitors.
Reload helpers for live monitors and HA monitors.
"""

import asyncio
import html
import traceback

from apscheduler.triggers.cron import CronTrigger
from telegram.constants import ParseMode

import bot.state as _state
from web.store import (
    load_tasks,
    load_monitors,
    load_live_monitors,
    load_ha_monitors,
)
from monitors.thunderstorm import run_thunderstorm_monitor
from monitors.rain         import run_rain_monitor
from monitors.seismic      import run_seismic_monitor
from live_monitors.lightning import live_monitor_manager
from live_monitors.ha        import ha_monitor_manager
from live_monitors.seismic   import seismic_monitor_manager

_TG_MAX_LEN = 4096


async def _send_chunked(text: str, parse_mode: str = ParseMode.HTML) -> None:
    """Split text on line boundaries and send as multiple Telegram messages if needed."""
    lines  = text.split("\n")
    chunk  = ""
    first  = True
    for line in lines:
        candidate = (chunk + "\n" + line) if chunk else line
        if len(candidate) > _TG_MAX_LEN:
            if chunk:
                if not first:
                    await asyncio.sleep(0.5)
                await _state._telegram_bot.send_message(
                    chat_id=_state.ALLOWED_CHAT_ID,
                    text=chunk,
                    parse_mode=parse_mode,
                    read_timeout=30,
                    write_timeout=30,
                )
                first = False
            chunk = line
        else:
            chunk = candidate
    if chunk:
        if not first:
            await asyncio.sleep(0.5)
        await _state._telegram_bot.send_message(
            chat_id=_state.ALLOWED_CHAT_ID,
            text=chunk,
            parse_mode=parse_mode,
            read_timeout=30,
            write_timeout=30,
        )


_MONITOR_RUNNERS = {
    "thunderstorm": run_thunderstorm_monitor,
    "rain":         run_rain_monitor,
    "seismic":      run_seismic_monitor,
}


# ── Scheduled Tasks ───────────────────────────────────────────────────────────

async def run_scheduled_task(task: dict):
    if not _state._telegram_bot:
        return
    task_name    = task.get("name", "Task")
    instructions = task.get("instructions", "").strip()
    if not instructions:
        return

    settings      = _state.read_settings()
    system_prompt = _state.build_system_prompt()
    model         = settings.get("model",    _state.SETTINGS_DEFAULTS["model"])
    provider      = settings.get("provider", _state.SETTINGS_DEFAULTS["provider"])

    members  = _state._build_members(settings)
    executor = _state._build_executor(system_prompt, model, provider, members, settings)
    print(f"[DRADIS] Scheduled task '{task_name}': model={model} members={[m.name for m in members]}")

    response, used_fallback, error = await _state._run_with_fallback(
        executor         = executor,
        prompt           = instructions,
        settings         = settings,
        system_prompt    = system_prompt,
        primary_model    = model,
        primary_provider = provider,
        context_label    = f"Task '{task_name}'",
    )

    if error is not None:
        fb_model_id = _state._apply_fallback_settings(settings).get("model", model) if used_fallback else model
        if used_fallback:
            await _state._send_error_telegram(
                f"❌ Task <b>{html.escape(task_name)}</b> — primary (<code>{html.escape(model)}</code>) "
                f"and fallback (<code>{html.escape(fb_model_id)}</code>) both failed: {html.escape(str(error))}"
            )
        else:
            await _state._send_error_telegram(
                f"❌ Task <b>{html.escape(task_name)}</b> failed (<code>{html.escape(model)}</code>): "
                f"{html.escape(str(error))}\n<i>No fallback model configured.</i>"
            )
        return

    if used_fallback:
        await _state._send_error_telegram(_state._build_fallback_used_msg(settings, model, task_name))

    member_responses = _state._collect_member_responses(response)
    text  = (response.content or "").strip()
    label = _state._agents_label(member_responses) + f" · <i>{html.escape(task_name)}</i>"

    if text:
        await _state._telegram_bot.send_message(
            chat_id=_state.ALLOWED_CHAT_ID,
            text=_state.md_to_html(text) + f"\n\n{label}",
            parse_mode=ParseMode.HTML,
        )


def _cron_task(task: dict):
    if _state._main_loop:
        asyncio.run_coroutine_threadsafe(run_scheduled_task(task), _state._main_loop)


def reload_task_jobs():
    tz = _state.read_settings().get("timezone", "UTC") or "UTC"
    for job in list(_state._scheduler.get_jobs()):
        if not job.id.startswith("monitor:"):
            job.remove()
    for task in load_tasks():
        if task.get("enabled") and task.get("cron"):
            try:
                _state._scheduler.add_job(
                    _cron_task,
                    CronTrigger.from_crontab(task["cron"], timezone=tz),
                    args=[task],
                    id=task["id"],
                    replace_existing=True,
                    misfire_grace_time=60,
                )
                print(f"[DRADIS] Scheduled task '{task['name']}' cron={task['cron']} tz={tz}")
            except Exception as e:
                print(f"[DRADIS] WARNING: invalid cron for task '{task.get('name')}': {e}")


# ── Scheduled Monitors ────────────────────────────────────────────────────────

async def run_scheduled_monitor(monitor: dict):
    if not _state._telegram_bot:
        return
    monitor_name = monitor.get("name", "Monitor")
    monitor_type = monitor.get("type", "thunderstorm")
    alert_mode   = monitor.get("alert_mode", "direct")
    runner = _MONITOR_RUNNERS.get(monitor_type)
    if not runner:
        await _state._send_error_telegram(
            f"⚠️ Monitor <b>{html.escape(monitor_name)}</b>: unknown type '{html.escape(monitor_type)}'"
        )
        return

    settings = _state.read_settings()
    tz_name  = settings.get("timezone", "UTC") or "UTC"
    print(f"[DRADIS] Monitor '{monitor_name}' type={monitor_type} alert_mode={alert_mode}")

    try:
        text = await runner(monitor, tz_name=tz_name)
    except Exception as e:
        exc_desc = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
        traceback.print_exc()
        print(f"[DRADIS] Monitor '{monitor_name}' error: {exc_desc}")
        await _state._send_error_telegram(
            f"❌ Monitor <b>{html.escape(monitor_name)}</b> failed: {html.escape(exc_desc)}"
        )
        return

    if not text:
        return

    if alert_mode == "llm":
        await _run_monitor_llm(monitor_name, text, monitor.get("instructions", ""), settings)
    else:
        try:
            await _send_chunked(text)
        except Exception as e:
            print(f"[DRADIS] Monitor '{monitor_name}' send_message error: {e}")
            await _state._send_error_telegram(
                f"❌ Monitor <b>{html.escape(monitor_name)}</b> — send error: {html.escape(str(e))}"
            )


async def _run_monitor_llm(monitor_name: str, report_text: str, instructions: str, settings: dict):
    sys_prompt = _state.build_system_prompt()
    model      = settings.get("model",    _state.SETTINGS_DEFAULTS["model"])
    provider   = settings.get("provider", _state.SETTINGS_DEFAULTS["provider"])
    members    = _state._build_members(settings)
    executor   = _state._build_executor(sys_prompt, model, provider, members, settings)

    user_instr = instructions.strip() or "Send this report to the user via Telegram."
    prompt = (
        f"Monitor report from '{monitor_name}':\n\n"
        f"{report_text}\n\n"
        f"Instructions: {user_instr}"
    )
    print(f"[DRADIS] Monitor '{monitor_name}' LLM: model={model} members={[m.name for m in members]}")

    response, used_fallback, error = await _state._run_with_fallback(
        executor         = executor,
        prompt           = prompt,
        settings         = settings,
        system_prompt    = sys_prompt,
        primary_model    = model,
        primary_provider = provider,
        context_label    = f"Monitor:{monitor_name}",
    )
    if error is not None:
        fb_model_id = _state._apply_fallback_settings(settings).get("model", model) if used_fallback else model
        model_info  = f"{html.escape(model)} + fallback {html.escape(fb_model_id)}" if used_fallback else html.escape(model)
        await _state._send_error_telegram(
            f"❌ Monitor <b>{html.escape(monitor_name)}</b> (LLM) failed ({model_info}): {html.escape(str(error))}"
        )
        return

    if response is not None and _state._telegram_bot:
        text = (response.content or "").strip()
        if text:
            try:
                await _state._telegram_bot.send_message(
                    chat_id=_state.ALLOWED_CHAT_ID,
                    text=_state.md_to_html(text) + f"\n\n<i>🤖 DRADIS · {html.escape(monitor_name)}</i>",
                    parse_mode=ParseMode.HTML,
                )
            except Exception as e:
                await _state._send_error_telegram(
                    f"❌ Monitor <b>{html.escape(monitor_name)}</b> — LLM send error: {html.escape(str(e))}"
                )


def _cron_monitor(monitor: dict):
    if _state._main_loop:
        asyncio.run_coroutine_threadsafe(run_scheduled_monitor(monitor), _state._main_loop)


def reload_monitor_jobs():
    tz = _state.read_settings().get("timezone", "UTC") or "UTC"
    for job in list(_state._scheduler.get_jobs()):
        if job.id.startswith("monitor:"):
            job.remove()
    for monitor in load_monitors():
        if monitor.get("enabled") and monitor.get("cron"):
            try:
                _state._scheduler.add_job(
                    _cron_monitor,
                    CronTrigger.from_crontab(monitor["cron"], timezone=tz),
                    args=[monitor],
                    id=f"monitor:{monitor['id']}",
                    replace_existing=True,
                    misfire_grace_time=60,
                )
                print(f"[DRADIS] Scheduled monitor '{monitor['name']}' cron={monitor['cron']} tz={tz}")
            except Exception as e:
                print(f"[DRADIS] WARNING: invalid cron for monitor '{monitor.get('name')}': {e}")


# ── Live Monitors ─────────────────────────────────────────────────────────────

def reload_live_monitors():
    settings = _state.read_settings()
    tz_name  = settings.get("timezone", "UTC") or "UTC"

    async def _send(text: str):
        if _state._telegram_bot:
            await _state._telegram_bot.send_message(
                chat_id=_state.ALLOWED_CHAT_ID,
                text=text,
                parse_mode=ParseMode.HTML,
            )

    configs = load_live_monitors()
    live_monitor_manager.reload(configs, _send, tz_name)
    seismic_monitor_manager.reload(configs, _send, tz_name)


def _live_status_dispatcher(monitor_id: str) -> str:
    cfg = next((m for m in load_live_monitors() if m["id"] == monitor_id), None)
    if cfg and cfg.get("type") == "seismic":
        return seismic_monitor_manager.status(monitor_id)
    return live_monitor_manager.status(monitor_id)


# ── HA Monitors ───────────────────────────────────────────────────────────────

def reload_ha_monitors():
    settings = _state.read_settings()
    tz_name  = settings.get("timezone", "UTC") or "UTC"
    mqtt_cfg = {k: settings[k] for k in [
        "mqtt_host", "mqtt_port", "mqtt_username", "mqtt_password", "mqtt_statestream_prefix"
    ] if k in settings}

    async def _send(text: str):
        if _state._telegram_bot:
            await _state._telegram_bot.send_message(
                chat_id=_state.ALLOWED_CHAT_ID,
                text=text,
                parse_mode=ParseMode.HTML,
            )

    async def _llm(prompt: str) -> str:
        s          = _state.read_settings()
        sys_prompt = _state.build_system_prompt()
        model      = s.get("model",    _state.SETTINGS_DEFAULTS["model"])
        provider   = s.get("provider", _state.SETTINGS_DEFAULTS["provider"])
        members    = _state._build_members(s)
        executor   = _state._build_executor(sys_prompt, model, provider, members, s)
        response, _, error = await _state._run_with_fallback(
            executor         = executor,
            prompt           = prompt,
            settings         = s,
            system_prompt    = sys_prompt,
            primary_model    = model,
            primary_provider = provider,
            context_label    = "HAMonitor",
        )
        if error or response is None:
            return ""
        return (response.content or "").strip()

    ha_monitor_manager.reload(load_ha_monitors(), _send, _llm, mqtt_cfg, tz_name)
