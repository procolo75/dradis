# Architecture

## Overview (v3.0)

DRADIS is **one agent** with a **flat set of tools** — no coordinator, no sub-agents, no orchestration framework. On each message the model is called with the system prompt, the conversation and the selected tool schemas; when it requests a tool the runtime runs the function, feeds the result back, and loops until a plain-text answer is produced.

**Why no framework:** v3.0 removed **agno**. A probe on Groq's `gpt-oss-120b` measured a raw `/chat/completions` request with 8 tool schemas at ~800 prompt tokens, versus ~8800 through agno — the framework added ~8000 tokens per request, making the 8K free-tier limit unreachable. The runtime now sends only what's needed.

**Tools & selection:** each capability (Web Search, Weather, Calendar, Gmail, Tasks, Read URL) exposes plain tool specs via `agents/*.py:*_tools(settings)`. Chat gets all available tools; a **task selects exactly which tools** to attach (`build_tools(settings, selected)`). The single agent runs on the main model with one fallback; per-capability model settings are retired.

## Source Layout

```
dradis/
├── main.py                  # Entry point — wires all components together
├── core.py                  # run_agent() tool-calling loop over the openai SDK (no agno)
├── car_mode.py              # Pure sanitiser: any message → prose CarPlay can read aloud (no I/O)
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
│   ├── seismic.py           # Seismic report monitor (INGV GOSSIP, no LLM)
│   └── campania_alert.py    # Civil Protection alert monitor (Campania, today + tomorrow, no LLM)
├── live_monitors/
│   ├── storm_front.py       # Storm front live monitor — lifecycle, persistence, formatting
│   ├── storm_front_core.py  # Pure decision core: ring/sector grid, front, CBDR, event machine (no I/O)
│   ├── storm_front_chart.py # Polar radar attached to ring messages (matplotlib, off the event loop)
│   ├── rain_front.py        # Rain front live monitor — inherits the storm front's tracker
│   ├── rain_front_chart.py  # The actual radar crop attached to ring messages
│   ├── radar.py             # Protezione Civile radar feed: shared singleton, refcounted, no API key
│   ├── radar_core.py        # Pure raster maths: projection, sampling, field motion, CPA (no I/O)
│   ├── snapshot.py          # /rain and /storm: perceive without deciding, and word the result
│   ├── blitzortung.py       # Blitzortung MQTT feed: connection, strike buffer, health
│   ├── geo.py               # Pure geo maths: Haversine, bearings, geohash topics
│   ├── position.py          # Named positions: one MQTT listener serving them all (singleton)
│   ├── position_core.py     # Pure position logic: pairing, age, speed/course, jumps (no I/O)
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
        ├── positions.py     # /api/positions CRUD, entity discovery, connection test
        └── tools.py         # Google OAuth callbacks, /api/websearch-test, /api/weather-test
```

## Token Budget

Two ceilings, and they are not the same number. The **context window** belongs to the model — `MODEL_CONTEXT_WINDOW` in `core.py`, 131 072 for gpt-oss-120b and most of the current Groq line-up. The **tokens-per-minute budget** belongs to the plan behind the API key — `PROVIDER_TPM`, 8000 on Groq's free tier, overridable from **Settings → DRADIS → Provider tokens per minute**. `ceiling_for(model, provider)` takes the tighter of the two, which on a free tier is always the plan. Keeping them apart is not pedantry: while the table claimed gpt-oss held 8192 tokens, paying for a larger plan would have changed nothing at all.

A turn re-sends its whole transcript on every tool round, so the cost is cumulative and the budget is spent by the minute, not by the request:

- `estimate_tokens()` prices text at 2.2 characters per token — the worst measured ratio, not the average, because the estimate exists to stay under a hard limit. `estimate_schema_tokens()` adds the tool definitions, which are prompt tokens like any other. `estimate_request_tokens()` is the single function the pacer, the trimmer and the tool-result sizer all use, so they cannot disagree about what a request costs.
- `calibrate()` closes the loop: `usage.prompt_tokens` is the provider stating what it billed for a payload we had already priced, so an under-count corrects itself after one round instead of after one refusal. Kept per provider, upward only, clamped at 2×.
- `record_tpm()` / `tpm_wait_seconds()` are a rolling 60-second bucket, keyed by provider because the ceiling belongs to the API key — the fallback model draws from the same one. If the next call would not fit, `_create_with_pacing()` waits.
- Waiting only answers *not now*. A request larger than a whole minute is refused whatever the clock says, so `_fit_to_hard_limit()` cuts it down before it is sent, and `request_too_large()` reads Groq's `413 … Limit 8000, Requested 8081` as a measurement — fed to `calibrate()`, then the request is trimmed against the stated limit and sent again.
- `_trim_to_window()` drops the oldest tool results, sparing the newest. What replaces one says the content is gone for good; the truncation marker, which says a result was merely shortened, is what taught a model to fetch the rest.
- `flatten_tool_transcript()` rewrites the final round's history as plain prose. A transcript full of `tool_calls` sent with no tools attached is what Groq reads as `tool_choice: none`, and gpt-oss answers it with another tool call — a 400 that costs the whole turn.
- `run_agent()` keeps a per-turn memo of `(tool, arguments)`: the same call twice is answered from the first result.

`read_url` (`bot/state.py`) sits on the other side of the same budget. It drops image tags, then checks the extraction: when Jina's readability pass returns nearly all addresses and no prose — `prose_chars(page) / len(page) < MIN_PROSE_SHARE` — the page is read again with `X-Respond-With: text` and the richer reading wins. One HTTP request, no model tokens; the alternative is a model reasoning over a navigation menu.

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
