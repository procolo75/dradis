"""
bot/scheduler.py
─────────────────
APScheduler cron jobs for scheduled tasks and monitors.
Reload helpers for live monitors and HA monitors.
"""

import asyncio
import html
import time
import traceback

from apscheduler.triggers.cron import CronTrigger
from telegram.constants import ParseMode

import bot.state as _state
import speaker
from web.store import (
    load_tasks,
    load_monitors,
    load_live_monitors,
    load_ha_monitors,
    load_positions,
)
from monitors.thunderstorm   import run_thunderstorm_monitor
from monitors.rain           import run_rain_monitor
from monitors.seismic        import run_seismic_monitor
from monitors.weather_chart  import run_weather_chart_monitor
from monitors.campania_alert import run_campania_alert_monitor
from backup.gdrive           import run_backup_monitor
from live_monitors.storm_front import storm_front_monitor_manager
from live_monitors.rain_front import rain_front_monitor_manager
from live_monitors.hail_front import hail_front_monitor_manager
from live_monitors.position   import position_manager
from live_monitors           import gauges as gauge_source
from live_monitors.ha        import ha_monitor_manager
from live_monitors.seismic   import seismic_monitor_manager
from live_monitors.football  import football_monitor_manager

_TG_MAX_LEN = 4096


async def _send_chunked(text: str, parse_mode: str = ParseMode.HTML,
                        bot_id: str = "default") -> None:
    """Split text on line boundaries and send as multiple Telegram messages if needed."""
    bot, chat_id = _state.get_bot_and_chat(bot_id)
    if not bot:
        return
    # Before splitting, never after: Car Mode collapses the report onto one line,
    # so chunking the original would cut at offsets that no longer exist.
    text, parse_mode = _state.for_car(text, parse_mode=parse_mode)
    # A line longer than the Telegram limit cannot be placed by the loop below,
    # which only ever cuts between lines — it would hand the whole thing to
    # send_message and get the message rejected. Rare with the original layout,
    # routine in Car Mode, where the entire report IS one line.
    lines: list[str] = []
    for line in text.split("\n"):
        while len(line) > _TG_MAX_LEN:
            cut = line.rfind(" ", 0, _TG_MAX_LEN)
            if cut <= 0:
                cut = _TG_MAX_LEN
            lines.append(line[:cut])
            line = line[cut:].lstrip()
        lines.append(line)
    chunk  = ""
    first  = True
    for line in lines:
        candidate = (chunk + "\n" + line) if chunk else line
        if len(candidate) > _TG_MAX_LEN:
            if chunk:
                if not first:
                    await asyncio.sleep(0.5)
                await bot.send_message(
                    chat_id=chat_id,
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
        await bot.send_message(
            chat_id=chat_id,
            text=chunk,
            parse_mode=parse_mode,
            read_timeout=30,
            write_timeout=30,
        )


_MONITOR_RUNNERS = {
    "thunderstorm":   run_thunderstorm_monitor,
    "rain":           run_rain_monitor,
    "seismic":        run_seismic_monitor,
    "weather_chart":  run_weather_chart_monitor,
    "campania_alert": run_campania_alert_monitor,
    "backup":         run_backup_monitor,
}


# ── Scheduled Tasks ───────────────────────────────────────────────────────────

async def run_scheduled_task(task: dict, on_result=None):
    """Run one task and send its answer to Telegram.

    `on_result(text)` is awaited with what the task produced — or with a short
    failure sentence — for a caller that has to hear it as well: a task started
    by voice speaks its result on the assistant that asked (bot/control.py)."""
    if not _state._telegram_bot:
        return
    task_name    = task.get("name", "Task")
    instructions = task.get("instructions", "").strip()
    bot_id       = task.get("telegram_bot_id", "default")
    if not instructions:
        return

    settings = _state.read_settings()
    model    = settings.get("model", _state.SETTINGS_DEFAULTS["model"])

    result, used_fallback, error, fb_reason = await _state.run_dradis(
        instructions, settings,
        selected      = _state.task_tool_selection(task),
        context_label = f"Task '{task_name}'",
    )

    if error is not None:
        fb_model_id = _state._apply_fallback_settings(settings).get("model", model) if used_fallback else model
        if used_fallback:
            await _state._send_error_telegram(
                f"❌ Task <b>{html.escape(task_name)}</b> — primary (<code>{html.escape(model)}</code>) "
                f"and fallback (<code>{html.escape(fb_model_id)}</code>) both failed: {html.escape(str(error))}",
                bot_id=bot_id,
            )
        else:
            await _state._send_error_telegram(
                f"❌ Task <b>{html.escape(task_name)}</b> failed (<code>{html.escape(model)}</code>): "
                f"{html.escape(str(error))}\n<i>No fallback model configured.</i>",
                bot_id=bot_id,
            )
        await _report(on_result, f"Task {task_name} failed: the model did not answer.")
        return

    if used_fallback:
        await _state._send_error_telegram(
            _state._fallback_msg(fb_reason, task_name), bot_id=bot_id,
        )

    # Ahead of the answer, not after it: by the time the reply has been read the
    # damage of trusting it is done.
    tool_errors = _state._tool_errors_msg(settings, result, task_name)
    if tool_errors:
        await _state._send_error_telegram(tool_errors, bot_id=bot_id)

    text   = (result.content or "").strip()
    footer = _state.reply_footer(settings, result)

    if text:
        bot, chat_id = _state.get_bot_and_chat(bot_id)
        if bot:
            body, parse_mode = _state.for_car(_state.md_to_html(text) + footer)
            await bot.send_message(
                chat_id=chat_id,
                text=body,
                parse_mode=parse_mode,
            )
    await _report(on_result, _state.md_to_html(text) if text
                  else f"Task {task_name}: no answer.")


async def _report(on_result, text: str) -> None:
    """Hand a result to the caller that asked for it; its failure is not ours."""
    if on_result is None or not text:
        return
    try:
        await on_result(text)
    except Exception as e:
        print(f"[DRADIS] on_result failed: {e}")


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

async def run_scheduled_monitor(monitor: dict, on_result=None):
    """Run one scheduled monitor and deliver its report. `on_result(text)` as in
    `run_scheduled_task`: the report, the LLM's rewrite of it, or one sentence
    saying a chart went to Telegram."""
    if not _state._telegram_bot:
        return
    monitor_name = monitor.get("name", "Monitor")
    monitor_type = monitor.get("type", "thunderstorm")
    alert_mode   = monitor.get("alert_mode", "direct")
    bot_id       = monitor.get("telegram_bot_id", "default")
    runner = _MONITOR_RUNNERS.get(monitor_type)
    if not runner:
        await _state._send_error_telegram(
            f"⚠️ Monitor <b>{html.escape(monitor_name)}</b>: unknown type '{html.escape(monitor_type)}'",
            bot_id=bot_id,
        )
        return

    settings = _state.read_settings()
    tz_name  = settings.get("timezone", "UTC") or "UTC"
    print(f"[DRADIS] Monitor '{monitor_name}' type={monitor_type} alert_mode={alert_mode}")

    try:
        result = await runner(monitor, tz_name=tz_name)
    except Exception as e:
        exc_desc = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
        traceback.print_exc()
        print(f"[DRADIS] Monitor '{monitor_name}' error: {exc_desc}")
        await _state._send_error_telegram(
            f"❌ Monitor <b>{html.escape(monitor_name)}</b> failed: {html.escape(exc_desc)}",
            bot_id=bot_id,
        )
        await _report(on_result, f"Monitor {monitor_name} failed.")
        return

    # Image result — send as photo(s), ignore alert_mode
    photos: list[bytes] = []
    if isinstance(result, bytes):
        photos = [result]
    elif isinstance(result, list) and result and isinstance(result[0], bytes):
        photos = result

    if photos:
        await _report(on_result, f"Report {monitor_name}: chart sent to Telegram.")
        # A chart monitor sends a picture and no words at all, so Car Mode has
        # nothing to rewrite — but a picture is exactly what a driver cannot use.
        # Say the report happened and leave it waiting: dropping it silently
        # would be the one outcome the user could never notice.
        if _state.car_mode_enabled():
            await _state.send_telegram(
                f"Report {monitor_name}: chart not sent while Car Mode is on.",
                bot_id=bot_id,
            )
            return
        bot, chat_id = _state.get_bot_and_chat(bot_id)
        if bot:
            for i, photo in enumerate(photos):
                try:
                    if i > 0:
                        await asyncio.sleep(0.5)
                    await bot.send_photo(
                        chat_id=chat_id,
                        photo=photo,
                        read_timeout=60,
                        write_timeout=60,
                    )
                except Exception as e:
                    print(f"[DRADIS] Monitor '{monitor_name}' send_photo error: {e}")
                    await _state._send_error_telegram(
                        f"❌ Monitor <b>{html.escape(monitor_name)}</b> — send_photo error: {html.escape(str(e))}",
                        bot_id=bot_id,
                    )
        return

    text = result
    if not text:
        await _report(on_result, f"Monitor {monitor_name}: nothing to report.")
        return

    if alert_mode == "llm":
        rewritten = await _run_monitor_llm(monitor_name, text, monitor.get("instructions", ""),
                                           settings, bot_id=bot_id)
        await _report(on_result, _state.md_to_html(rewritten) if rewritten else text)
    else:
        await _report(on_result, text)
        try:
            await _send_chunked(text, bot_id=bot_id)
        except Exception as e:
            print(f"[DRADIS] Monitor '{monitor_name}' send_message error: {e}")
            await _state._send_error_telegram(
                f"❌ Monitor <b>{html.escape(monitor_name)}</b> — send error: {html.escape(str(e))}",
                bot_id=bot_id,
            )


async def _run_monitor_llm(monitor_name: str, report_text: str, instructions: str,
                           settings: dict, bot_id: str = "default") -> str:
    """Rewrite a report with the model and send it. Returns the rewrite ("" on
    failure) so a caller listening for the result can hear it."""
    model = settings.get("model", _state.SETTINGS_DEFAULTS["model"])

    user_instr = instructions.strip() or "Send this report to the user via Telegram."
    prompt = (
        f"Monitor report from '{monitor_name}':\n\n"
        f"{report_text}\n\n"
        f"Instructions: {user_instr}"
    )
    # A monitor LLM run just reformats a ready report — no tools needed.
    result, used_fallback, error, _ = await _state.run_dradis(
        prompt, settings, selected=[], context_label=f"Monitor:{monitor_name}",
    )
    if error is not None:
        fb_model_id = _state._apply_fallback_settings(settings).get("model", model) if used_fallback else model
        model_info  = f"{html.escape(model)} + fallback {html.escape(fb_model_id)}" if used_fallback else html.escape(model)
        await _state._send_error_telegram(
            f"❌ Monitor <b>{html.escape(monitor_name)}</b> (LLM) failed ({model_info}): {html.escape(str(error))}",
            bot_id=bot_id,
        )
        return

    if result is not None:
        text = (result.content or "").strip()
        if text:
            try:
                bot, chat_id = _state.get_bot_and_chat(bot_id)
                if bot:
                    # The signature names the monitor that spoke, which matters
                    # on screen where several look alike. Read aloud it is a
                    # trailing "DRADIS, Meteo" after every report, so Car Mode
                    # drops it the same way it drops the token footer.
                    signature = (
                        "" if _state.car_mode_enabled()
                        else f"\n\n<i>🤖 DRADIS · {html.escape(monitor_name)}</i>"
                    )
                    body, parse_mode = _state.for_car(
                        _state.md_to_html(text) + signature)
                    await bot.send_message(
                        chat_id=chat_id,
                        text=body,
                        parse_mode=parse_mode,
                    )
            except Exception as e:
                await _state._send_error_telegram(
                    f"❌ Monitor <b>{html.escape(monitor_name)}</b> — LLM send error: {html.escape(str(e))}",
                    bot_id=bot_id,
                )
        return text
    return ""


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

    def _make_send(cfg: dict):
        bid  = cfg.get("telegram_bot_id", "default")
        lang = cfg.get("language", "it")
        telegram_send = _make_telegram_send(bid, lang)
        if not cfg.get("speak_alerts"):
            return telegram_send

        async def _send(text: str, photo: bytes | None = None) -> bool | None:
            # Spoken as well, on the HA Assist default speaker. Fire and forget:
            # the delivery status the monitor gates its state on stays Telegram's,
            # and a speaker that is off must not delay or fail the alert. The
            # voice has its own quiet hours: a 3 a.m. alert may well belong on
            # the phone and not in the living room.
            if speaker.in_quiet_hours(cfg.get("speak_quiet_start", ""),
                                      cfg.get("speak_quiet_end", ""), tz_name):
                print(f"[DRADIS] '{cfg.get('name')}': voice quiet hours, alert not spoken")
            else:
                asyncio.create_task(speaker.speak(text, lang, None, _state.read_settings()))
            return await telegram_send(text, photo)
        return _send

    def _make_telegram_send(bid: str, lang: str):
        async def _send(text: str, photo: bytes | None = None) -> bool | None:
            # Propagate delivery status so callers (storm front monitor) can gate
            # state flags on a confirmed send. Three states, not two: see
            # `state.classify_send_failure` for why a timed-out photo upload must
            # not be reported as "not delivered".
            #
            # With a photo the text rides along as the CAPTION, so the picture and
            # its explanation are ONE Telegram message and one notification — two
            # separate sends would buzz the user's phone twice per ring.
            #
            # In Car Mode the picture is dropped and the text goes on its own: a
            # photo notification on CarPlay is announced as "Image" or not read
            # at all, which turns the alert that matters most into the one the
            # driver cannot hear. The chart has already been rendered by the time
            # we get here — wasting it costs one render and keeps every monitor
            # unaware of Car Mode, which is worth more than the saving.
            if photo is None or _state.car_mode_enabled():
                return await _state.send_telegram(text, bot_id=bid, lang=lang)
            bot, chat_id = _state.get_bot_and_chat(bid)
            if not bot:
                return _state.REFUSED
            started = time.monotonic()
            try:
                # All four timeouts are given explicitly. Only read and write were
                # set before, so a pool timeout — which never reaches Telegram at
                # all — arrived as the same `TimedOut` as a read timeout on an
                # upload that did, and the two demand opposite answers.
                await bot.send_photo(chat_id=chat_id, photo=photo, caption=text,
                                     parse_mode=ParseMode.HTML,
                                     connect_timeout=20, pool_timeout=20,
                                     read_timeout=60, write_timeout=120)
                return _state.DELIVERED
            except Exception as e:
                print(f"[DRADIS] send_photo(bot_id={bid!r}) {type(e).__name__} "
                      f"after {time.monotonic() - started:.1f}s: {e}")
                return _state.classify_send_failure(e)
        return _send

    # Every step is isolated. These run in sequence on one call, so an exception
    # anywhere used to abandon the rest silently: a manager that failed to reload
    # left the ones after it still running their old configuration, which is
    # exactly what "I disabled it in /manage and nothing happened" looks like
    # from the outside. One broken component must not take the others with it.
    def _step(label: str, fn, *args) -> None:
        try:
            fn(*args)
        except Exception as e:
            traceback.print_exc()
            print(f"[DRADIS] WARNING: {label} reload failed: {e}")

    # The position manager goes first, so a monitor that follows a position finds
    # the feed already aimed at the right entities. The gauge source is settings
    # only — no feed, no task — so it just has to be current before the monitors
    # that read it are built.
    _step("position manager", position_manager.configure, settings, load_positions())
    _step("gauge source", gauge_source.configure, settings)

    configs = load_live_monitors()
    _step("storm front monitors", storm_front_monitor_manager.reload,
          configs, _make_send, tz_name)
    _step("rain front monitors", rain_front_monitor_manager.reload,
          configs, _make_send, tz_name)
    _step("hail front monitors", hail_front_monitor_manager.reload,
          configs, _make_send, tz_name)
    _step("seismic monitors", seismic_monitor_manager.reload,
          configs, _make_send, tz_name)
    _step("football monitors", football_monitor_manager.reload,
          configs, _make_send, tz_name)


def _live_status_dispatcher(monitor_id: str) -> str:
    cfg = next((m for m in load_live_monitors() if m["id"] == monitor_id), None)
    if cfg and cfg.get("type") == "seismic":
        return seismic_monitor_manager.status(monitor_id)
    if cfg and cfg.get("type") == "football_betting":
        return football_monitor_manager.status(monitor_id)
    if cfg and cfg.get("type") == "rain_front":
        return rain_front_monitor_manager.status(monitor_id)
    if cfg and cfg.get("type") == "hail_front":
        return hail_front_monitor_manager.status(monitor_id)
    return storm_front_monitor_manager.status(monitor_id)


# ── HA Monitors ───────────────────────────────────────────────────────────────

def reload_ha_monitors():
    settings = _state.read_settings()
    tz_name  = settings.get("timezone", "UTC") or "UTC"
    mqtt_cfg = {k: settings[k] for k in [
        "mqtt_host", "mqtt_port", "mqtt_username", "mqtt_password", "mqtt_statestream_prefix"
    ] if k in settings}

    def _make_send(cfg: dict):
        bid  = cfg.get("telegram_bot_id", "default")
        lang = cfg.get("language", "it")
        async def _send(text: str):
            await _state.send_telegram(text, bot_id=bid, lang=lang)
        return _send

    async def _llm(prompt: str, selected) -> str:
        # `selected` is the monitor's own tool selection: [] = no tools,
        # ["*"] = all available, or a list of tool names / capability ids.
        s = _state.read_settings()
        result, _, error, _ = await _state.run_dradis(
            prompt, s, selected=selected, context_label="HAMonitor",
        )
        if error or result is None:
            return ""
        return (result.content or "").strip()

    ha_monitor_manager.reload(load_ha_monitors(), _make_send, _llm, mqtt_cfg, tz_name)
