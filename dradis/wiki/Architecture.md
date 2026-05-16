# Architecture

## Overview

DRADIS uses an **agno Team** design (`coordinate` mode): a DRADIS leader agent orchestrates a team of specialist member agents. When the user sends a message the leader decides which members to invoke, runs them **in parallel**, and synthesises their responses into a single reply. If no sub-agents are enabled, DRADIS falls back to a single-agent path with no overhead.

## Source Layout

```
dradis/
├── main.py                  # Entry point — wires all components together
├── core.py                  # create_agent(), create_team(), provider helpers
├── agents/
│   ├── gcal.py              # Google Calendar sub-agent + OAuth
│   ├── gmail.py             # Gmail sub-agent + OAuth
│   ├── gtasks.py            # Google Tasks sub-agent + OAuth
│   ├── weather.py           # Weather sub-agent (Open-Meteo)
│   └── web_search.py        # Web Search sub-agent (Tavily + Jina)
├── bot/
│   ├── state.py             # Global state, startup, settings, history, fallback engine
│   ├── scheduler.py         # Task/monitor cron jobs, live-monitor lifecycle
│   ├── commands.py          # /info, /gcalauth, /gmailauth, /gtasksauth, /todo
│   └── handlers.py          # Telegram message, voice, and callback handlers
├── monitors/
│   ├── thunderstorm.py      # Thunderstorm risk monitor (Open-Meteo, no LLM)
│   ├── rain.py              # Rain alert monitor (Open-Meteo, no LLM)
│   └── seismic.py           # Seismic report monitor (INGV GOSSIP, no LLM)
├── live_monitors/
│   ├── lightning.py         # Lightning live monitor (MQTT + DBSCAN)
│   ├── ha.py                # HA entity monitor (MQTT statestream)
│   └── seismic.py           # Seismic live monitor (INGV GOSSIP polling)
└── web/
    ├── store.py             # Shared data layer: load/save, callbacks, cron validation
    ├── models.py            # Pydantic request models
    ├── server.py            # FastAPI app assembly
    └── routes/
        ├── settings.py      # GET /, /api/config, /api/settings, /api/server-timezone
        ├── agents.py        # /api/agents, /api/models, /api/speedtest, /api/voice-*
        ├── tasks.py         # /api/tasks CRUD, /api/tasks/validate-cron
        ├── monitors.py      # /api/monitors, /api/live-monitors, /api/ha-monitors CRUD
        └── tools.py         # Google OAuth callbacks, /api/websearch-test, /api/weather-test
```

## Fallback Model

Each agent (DRADIS, Web Search, Weather, Google Calendar, Gmail, Google Tasks) supports an independent fallback provider and model. When an API call fails, DRADIS:

1. Detects the failure — agno never re-raises model errors; it sets `response.status = "ERROR"` and puts the error message in `response.content`. DRADIS checks both `status == "ERROR"` and empty content to catch all failure modes.
2. Sends a Telegram warning: *"⚠️ Primary model failed — replied via fallback ✅"* if the fallback succeeds.
3. Rebuilds the executor (and any sub-agents) using the fallback settings and retries.
4. If the fallback also fails, sends a `❌ Both primary and fallback models failed` Telegram notification.

The logic is centralised in `_run_with_fallback()` in `bot/state.py`, shared by both `handle_message` and `run_scheduled_task`.

## Scheduling

DRADIS uses **APScheduler** (`AsyncIOScheduler`) for cron-based jobs. Both task and monitor jobs share the same scheduler instance. Cron wrappers (`_cron_task`, `_cron_monitor`) use `asyncio.run_coroutine_threadsafe(coro, _main_loop)` to ensure coroutines always run in the correct event loop, regardless of how APScheduler dispatches them.

Live monitors run as persistent asyncio tasks — no cron, no polling. They connect on startup (or on save), listen for push events, and reconnect automatically on disconnect.

## Data Flow — Regular Message

```
User (Telegram)
  → handle_message()                  [bot/handlers.py]
  → _run_with_fallback()              [bot/state.py]
    → _build_executor()               [bot/state.py]
      → _build_members()              [bot/state.py]
        → create_weather_agent()      [agents/weather.py]
        → create_web_search_agent()   [agents/web_search.py]
        → create_gcal_agent()         [agents/gcal.py]
        → ...
    → Team.arun(message)              [agno]
  → send_message(result)              [Telegram]
```

## Data Flow — Scheduled Monitor

```
APScheduler (cron fire)
  → _cron_monitor()                   [bot/scheduler.py]
  → run_scheduled_monitor()           [bot/scheduler.py]
    → _MONITOR_RUNNERS[type](monitor) [monitors/thunderstorm.py etc.]
    → send_message(report)            [Telegram]
```
