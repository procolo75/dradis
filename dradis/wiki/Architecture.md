# Architecture

## Overview (v3.0)

DRADIS is **one agent** with a **flat set of tools** — no coordinator, no sub-agents, no orchestration framework. On each message the model is called with the system prompt, the conversation and the selected tool schemas; when it requests a tool the runtime runs the function, feeds the result back, and loops until a plain-text answer is produced.

**Why no framework:** v3.0 removed **agno**. A probe on Groq's `gpt-oss-120b` measured a raw `/chat/completions` request with 8 tool schemas at ~800 prompt tokens, versus ~8800 through agno — the framework added ~8000 tokens per request, making the 8K free-tier limit unreachable. The runtime now sends only what's needed.

**Tools & selection:** each capability (Web Search, Weather, Calendar, Gmail, Tasks, Read URL) exposes plain tool specs via `agents/*.py:*_tools(settings)`. Chat gets all available tools; a **task selects exactly which tools** to attach (`build_tools(settings, selected)`). The single agent runs on the main model with one fallback; per-capability model settings are retired. A `max_tokens` cap (default 2048) and a **TPM probe** in the UI keep token usage observable.

## Source Layout

```
dradis/
├── main.py                  # Entry point — wires all components together
├── core.py                  # run_agent() tool-calling loop over the openai SDK (no agno)
├── agents/                  # each module exposes *_tools(settings) → tool specs
│   ├── gcal.py              # Google Calendar tools + OAuth
│   ├── gmail.py             # Gmail tools + OAuth
│   ├── gtasks.py            # Google Tasks tools + OAuth
│   ├── weather.py           # Weather tools (Open-Meteo)
│   └── web_search.py        # Web Search tools (Tavily + Jina)
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

DRADIS runs on one main model. If a call fails (API error, rate limit) or returns empty content, `run_dradis()` in `bot/state.py` retries once on the configured **fallback** model/provider and posts `⚠️ fallback triggered — <error>` to Telegram. If the fallback also fails, a `❌ Both … failed` notification is sent. Leaving the fallback model blank disables the retry.

## Scheduling

DRADIS uses **APScheduler** (`AsyncIOScheduler`) for cron-based jobs. Both task and monitor jobs share the same scheduler instance. Cron wrappers (`_cron_task`, `_cron_monitor`) use `asyncio.run_coroutine_threadsafe(coro, _main_loop)` to ensure coroutines always run in the correct event loop, regardless of how APScheduler dispatches them.

Live monitors run as persistent asyncio tasks — no cron, no polling. They connect on startup (or on save), listen for push events, and reconnect automatically on disconnect.

## Data Flow — Regular Message

```
User (Telegram)
  → handle_message()                  [bot/handlers.py]
  → run_dradis(question, settings)    [bot/state.py]
    → build_tools(settings, None)     [bot/state.py]  # all available tools
    → run_agent(system, prompt, tools, model, provider)   [core.py]
        loop: model → tool_calls? → run fn → feed result → repeat → final text
    → (on error) retry once on the fallback model
  → send_message(result.content)      [Telegram]
```

## Data Flow — Scheduled Monitor

```
APScheduler (cron fire)
  → _cron_monitor()                   [bot/scheduler.py]
  → run_scheduled_monitor()           [bot/scheduler.py]
    → _MONITOR_RUNNERS[type](monitor) [monitors/thunderstorm.py etc.]
    → send_message(report)            [Telegram]
```
