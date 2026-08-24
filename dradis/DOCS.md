# DRADIS Agentic AI for Home Assistant — Documentation

DRADIS is a Home Assistant app that exposes a conversational AI agent controllable via Telegram. All settings are managed from the built-in Web UI and the HA Configuration tab — no code changes required.

---

## Icon

DRADIS displays a radar-sweep icon in the Home Assistant app dashboard (`icon.png`) and in the Web UI sidebar header, matching the dark/cyan color scheme.

---

## Architecture (v3.0 — single agent, no framework)

DRADIS is **one agent** that owns a **flat set of tools**. There is no coordinator, no sub-agents and no orchestration framework. When a message arrives, the model is called with the system prompt, the conversation and the selected tool schemas; if it asks to call a tool, the runtime executes the function, feeds the result back, and loops until the model returns a plain-text answer.

This replaced the previous **agno** `Team` design in v3.0. A probe on Groq's `gpt-oss-120b` showed a raw `/chat/completions` request with 8 tool schemas costs ~800 prompt tokens, while the same call through agno cost ~8800 — the framework added ~8000 tokens per request, which made the 8K free-tier limit unreachable. Removing agno was the fix.

### The runtime — `core.py`

`run_agent(system_prompt, user_prompt, tools, model, provider, …)` is a thin tool-calling loop over the `openai` SDK, pointed at any OpenAI-compatible provider via `base_url`. A "tool" is a plain spec: `{"name", "description", "parameters" (JSON schema), "fn" (async callable)}`. The loop calls the model, runs any `tool_calls`, appends their results, and repeats up to a bounded number of rounds. Only the exact tool schemas selected are sent — nothing else.

### Capabilities and tool selection

Each capability contributes tool specs (`agents/*.py` → `*_tools(settings)`):

| Capability | Tools |
|-----------|-------|
| Web Search | `search_web` |
| Weather | `get_weather` |
| Google Calendar | `get_calendar_events`, `create_calendar_event`, `delete_calendar_event` |
| Gmail | `get_emails`, `get_unread_emails`, `search_emails`, `send_email` |
| Google Tasks | `list_tasks`, `create_task`, `complete_task`, `delete_task`, `update_task` |
| Read URL | `read_url` |

A capability's tools are available when it is **Enabled** and authenticated. **Chat** gets all available tools; a **task** and an **HA monitor (LLM mode)** each select exactly which tools to attach (fewer tools = smaller prompt; HA monitors default to *none*). `bot/state.py:build_tools(settings, selected)` assembles the list — `[]` = no tools, `["*"]` = all, a list = only those. A capability's *Additional instructions* are added to the system prompt when any of its tools are attached, under a header that names the capability and its tool names and states that the line applies **only while that tool is in use**. Without that attribution they read as standing rules and shape unrelated answers — a weather instruction would change how a web search is written.

### One model + fallback

The single agent runs on the main model (**Settings → DRADIS**). On an API error or empty reply, `run_dradis()` retries once on the configured **fallback** model/provider and sends a Telegram warning (`⚠️ fallback triggered ✅`); if that also fails, a `❌ Both … failed` notification is sent. Per-capability model/provider/fallback settings are no longer used (v3) — the Web UI hides them with a notice.

### Token budget & observability

The limit that actually bites on a free tier is not the context window — it is **tokens-per-minute**, a rolling 60-second budget that counts *every* request a turn makes, prompt and completion, summed. Groq's free tier allows 8000. A turn re-sends the whole conversation on each tool round, so a task that reads a web page and then calls one more tool can spend 10 000 tokens across three rounds and be refused, while the identical task on the identical page passes when the model happens to answer a round earlier.

The two numbers are separate and both are needed. The **context window** belongs to the model (gpt-oss-120b holds 131 072 tokens); the **per-minute budget** belongs to the plan behind the API key. DRADIS budgets against whichever is tighter, which on a free tier is always the plan — so raising the plan is a number to change in **Settings → DRADIS → Provider tokens per minute**, not a release to wait for.

DRADIS keeps itself inside that budget rather than discovering it from a rejection:

- **`max_tokens`** (**Settings → DRADIS → Max completion tokens**, default 2048) caps every reply. Providers reserve it from the window, so on an 8K model the prompt must fit in the remaining ~6K.
- **Sampling temperature** (default `0.2`) fixes how many tool rounds a prompt costs. Left at the provider default (~1.0) the same prompt answers in one round on one run and calls another tool on the next — on an 8K budget that is the difference between a task that works and one that is refused.
- **Max tool rounds** (default `3`) bounds the worst case. Every round re-sends the transcript, so this multiplies the cost of a task.
- **Provider tokens per minute** (default `0`) is that budget. `0` means the figure DRADIS knows — 8000 for Groq, uncapped elsewhere. Set it to your plan's ceiling when you leave the free tier.
- Tool results are **trimmed to fit** what is left of the window and of the minute, with `[… content truncated to fit the model budget …]` appended so the model knows it has part of a page rather than all of one. A result dropped whole to make room says so differently, and says it is gone — a model told its page was merely "truncated" goes and fetches the rest, which is the round the trim was paying for.
- The **size estimate is measured, not assumed**. Text is priced at 2.2 characters per token, the worst ratio observed rather than the average one (a page three-quarters made of URLs tokenises nothing like prose), tool schemas are counted, and after every round the provider's own `usage.prompt_tokens` corrects the estimate upward for the rest of the process if it was optimistic.
- Requests are **paced**: if the next call would not fit in the rolling minute, the runtime waits for the budget to free up instead of being refused. A rate-limited call is retried once after the delay the provider asks for, and the fallback model waits too when it sits on the provider that just refused — the ceiling belongs to the API key, not the model.
- A request bigger than *a whole minute* is a different problem, and waiting cannot solve it: it is **trimmed before it is sent**, and if the provider still refuses it (`413 … Limit 8000, Requested 8081`) that refusal is read as a measurement, applied to the estimate, and the request is cut down and sent again.
- The **final round carries no tool call at all**, in the history as well as the request. Sending a transcript full of tool calls with no tools attached is what Groq reads as `tool_choice: none`, and gpt-oss answers it with another tool call, which the provider refuses with a 400 — losing a turn that had already paid for its context.
- A tool **called twice with identical arguments in one turn** is answered from the first result instead of being run again.
- Each round logs `round=N prompt=… completion=… cumulative=… tpm_used=… est=… x…`, which distinguishes "the page was too big" from "too many rounds" from "the fetch failed and was retried"; `est=` and the multiplier after it are what DRADIS thought the request would cost and how far it has learned to correct itself.

Enable **Settings → DRADIS → Log token usage** to append `🔢 in N · out N` to every chat and task reply. Enable **Settings → DRADIS → Log tools used** to append `🔧 tool1, tool2` (the tools DRADIS called that turn) — useful to see which capabilities each chat or task exercised.

### Tool failures

A tool that cannot do its job raises `ToolError`. The message still reaches the model — a failed tool is not a failed turn — and it is also recorded on the result, so **Settings → DRADIS → Report tool failures** (default *on*) can put it in front of you:

```
⚠️ Task Rassegna stampa: tool failure — the answer below may be incomplete.
• read_url: HTTP 429 from r.jina.ai reading https://example.com/article
```

It is sent as its own message, ahead of the reply, and unlike the `🔢`/`🔧` footer it survives Car Mode. Token counts are diagnostics and can be dropped; a failed tool decides whether the answer under it can be trusted at all.

**Extensibility**: adding a capability means writing a `X_tools(settings)` builder in `agents/X.py` and registering it in `bot/state.py:_capability_tool_groups()` and `web/store.py:available_tool_catalogue()`.

**Source layout:**

| File | Responsibility |
|------|---------------|
| `main.py` | Entry point — wires bot, scheduler, web server, and live monitors together |
| `bot/state.py` | Global state, startup options, settings, history, fallback engine, `_run_with_fallback()`, extra-bot registry (`get_bot_and_chat`, `reload_extra_bots`, `send_telegram`) |
| `bot/scheduler.py` | Task and monitor cron jobs, live-monitor lifecycle, `reload_*()` functions |
| `bot/commands.py` | Telegram command handlers: `/info`, `/gcalauth`, `/gmailauth`, `/gtasksauth`, `/backupauth` |
| `backup/gdrive.py` | Google Drive backup module — OAuth2 flow, file upload, `run_backup_monitor()` |
| `bot/handlers.py` | Telegram message, voice, and callback handlers |
| `core.py` | Agent runtime — `run_agent()` tool-calling loop over the `openai` SDK, `AgentResult`, provider/context helpers (no agno) |
| `bot/state.py` | Tool registry & runner — `build_tools()`, `run_dradis()`, capabilities, history, fallback, extra-bot registry |
| `agents/web_search.py` | Web Search tools — `web_search_tools()` (Tavily) |
| `agents/weather.py` | Weather tools — `fetch_weather()` + `weather_tools()` (Open-Meteo) |
| `agents/gmail.py` | Gmail tools — `gmail_tools()` + OAuth token management |
| `agents/gcal.py` | Google Calendar tools — `gcal_tools()` + OAuth token management |
| `agents/gtasks.py` | Google Tasks tools — `gtasks_tools()` + OAuth token management |
| `monitors/thunderstorm.py` | Thunderstorm risk monitor — LLM-free, fetches Open-Meteo instability + pressure-level data, computes multiplicative TRS (0.0–1.0) in Python |
| `monitors/rain.py` | Rain alert monitor — LLM-free, fetches 15-min precipitation data from Open-Meteo, sends alert only when rain is forecast |
| `monitors/seismic.py` | Seismic report monitor — LLM-free, fetches INGV GOSSIP JSON API, sends statistical report |
| `monitors/campania_alert.py` | Civil Protection alert monitor (Campania) — LLM-free, reads today's and tomorrow's bulletin from the Centro Funzionale REST API, reports the 8 alert zones on each, silent below the configured level |
| `monitors/weather_chart.py` | Weather Charts monitor — LLM-free, fetches hourly Open-Meteo forecasts for up to 5 models, generates one PNG chart per variable and returns `list[bytes]` |
| `live_monitors/storm_front.py` | Storm front live monitor — LLM-free; feed lifecycle, persistence, quiet hours, message formatting; `StormFrontLiveMonitor` + `StormFrontMonitorManager` singleton |
| `live_monitors/storm_front_core.py` | Pure decision core — ring/sector grid, per-sector front, CBDR verdict, event machine. No I/O, fully unit-tested |
| `live_monitors/storm_front_chart.py` | Polar radar attached to ring messages (matplotlib, object API, rendered off the event loop) |
| `live_monitors/blitzortung.py` | Blitzortung MQTT feed — connection, strike buffer, health (`feed_ok`, `connected_for`) |
| `live_monitors/geo.py` | Pure geo maths — Haversine, bearings, compass labels, geohash topic derivation |
| `live_monitors/position.py` | Named positions — one MQTT listener on the HA broker serving every configured phone; `PositionSource` + `PositionManager` singleton |
| `live_monitors/position_core.py` | Pure position logic — lat/lon pairing, fix age, speed/course, discontinuity detection. No I/O, fully unit-tested |
| `live_monitors/replay.py` | Offline replay of a recorded storm through the decision core — used to tune sensitivity on real data |
| `live_monitors/ha.py` | HA Monitor — persistent MQTT listener for Home Assistant entity state changes; `HaLiveMonitor` + `HaMonitorManager` singleton |
| `live_monitors/seismic.py` | Seismic live monitor — polls INGV GOSSIP JSON API every 60 s, alerts on new events and state promotions |
| `live_monitors/football.py` | Football Betting live monitor — polls RapidAPI every 5 min (clock-aligned); `FootballLiveMonitor` + `FootballMonitorManager` singleton |
| `web/store.py` | Shared data layer: load/save functions, callback registrations, cron validation, provider helpers, OAuth state |
| `web/models.py` | Pydantic request models for all API endpoints |
| `web/routes/settings.py` | FastAPI routes: settings CRUD, config, server timezone |
| `web/routes/agents.py` | FastAPI routes: agents CRUD, model listing, speed test, voice |
| `web/routes/tasks.py` | FastAPI routes: task CRUD, cron validation, manual run |
| `web/routes/monitors.py` | FastAPI routes: scheduled monitor, live monitor, HA monitor CRUD; geocode; HA test/discover |
| `geocode.py` | Place name → coordinates, shared by every monitor, the weather tool and the UI hint |
| `web/routes/tools.py` | FastAPI routes: Google OAuth callbacks, web search test, weather test |
| `web/routes/bots.py` | FastAPI routes: extra Telegram bot CRUD, test-connection endpoint |
| `web/routes/positions.py` | FastAPI routes: named position CRUD, coordinate-entity discovery, connection test |
| `web/server.py` | FastAPI app assembly — includes all routers, re-exports store symbols |

---

## Requirements

- Home Assistant with Supervisor (HAOS or Supervised)
- A Telegram bot (created via [@BotFather](https://t.me/BotFather))
- An API key for at least one supported LLM provider (OpenRouter, OpenAI, GitHub Models, Gemini, or Groq)
- *(Optional)* A [Tavily](https://tavily.com) API key for the Web Search sub-agent
- *(Optional)* A [Groq](https://console.groq.com) API key for the Voice sub-agent (required to enable voice transcription)
- *(Optional)* Google Cloud OAuth2 credentials — one credential covers the Google Calendar, Gmail, and Google Tasks sub-agents

---

## Installation

1. In Home Assistant go to **Settings → Apps → Install App → ⋮ → Repositories**
2. Add the repository URL: `https://github.com/procolo75/dradis`
3. Find **DRADIS** in the store and click **Install**
4. Fill in the **Configuration** tab with your credentials
5. Start the app

---

## Configuration (HA tab)

Only API keys and credentials go here. All other settings are managed at runtime from the Web UI.

| Field | Type | Description |
|-------|------|-------------|
| `telegram_bot_token` | password | Telegram bot token (from BotFather) |
| `telegram_allowed_chat_id` | int | Telegram user ID allowed to interact |
| `openrouter_api_key` | password | *(Optional)* OpenRouter API key |
| `openai_api_key` | password | *(Optional)* OpenAI API key |
| `github_token` | password | *(Optional)* GitHub Personal Access Token for GitHub Models |
| `gemini_api_key` | password | *(Optional)* Google Gemini API key |
| `groq_api_key` | password | *(Optional)* Groq API key — required for the Voice sub-agent |
| `tavily_api_key` | password | *(Optional)* Tavily API key — required for the Web Search sub-agent |
| `google_client_id` | str | *(Optional)* Google OAuth2 client ID — required for Google Calendar, Gmail, and/or Google Tasks |
| `google_client_secret` | password | *(Optional)* Google OAuth2 client secret — required for Google Calendar, Gmail, and/or Google Tasks |
| `rapidapi_football_key` | password | *(Optional)* RapidAPI key — required for the Football Betting live monitor |

Fill in at least one LLM provider key. The active provider is selected from the Web UI.

### How to get your API keys

- **Telegram bot token**: open Telegram, start a chat with [@BotFather](https://t.me/BotFather), send `/newbot` and follow the prompts — you will receive a token like `123456:ABC-DEF...`
- **Telegram user ID**: start a chat with [@userinfobot](https://t.me/userinfobot) — it will reply with your numeric ID
- **OpenRouter API key**: sign up at [openrouter.ai](https://openrouter.ai), go to **Settings → Keys** to create a key
- **OpenAI API key**: sign up at [platform.openai.com](https://platform.openai.com), go to **API keys**
- **GitHub token**: go to [github.com/settings/tokens](https://github.com/settings/tokens) — a classic token with no scopes is sufficient for GitHub Models
- **Gemini API key**: sign up at [aistudio.google.com](https://aistudio.google.com), click **Get API key**
- **Groq API key**: sign up at [console.groq.com](https://console.groq.com), go to **API Keys**
- **Tavily API key** *(optional)*: sign up at [tavily.com](https://tavily.com) — the free tier includes 1 000 searches/month
- **Google OAuth2 credential** *(optional — required for Calendar, Gmail, and/or Tasks)*: no Google username or password is stored. **One credential covers all three services.**

  **Part 1 — One-time Google Cloud setup (do this once for all Google services):**
  1. Go to [console.cloud.google.com](https://console.cloud.google.com) and create or select a project
  2. **APIs & Services → Library** → enable the APIs you need: *Google Calendar API*, *Gmail API*, *Tasks API*. *(Enable only the ones you need — all are free.)*
  3. **APIs & Services → OAuth consent screen** → choose **External** → fill in app name (e.g. *DRADIS*) and your email → save
  4. Still in the consent screen → **Publishing status** → click **Publish app** → confirm

     > ⚠️ **This step is essential.** If the app stays in **Testing** mode, Google automatically revokes the refresh token every 7 days, forcing you to re-authenticate repeatedly. Publishing makes the token permanent. No Google review is required for personal use.

  5. **Credentials → Create credentials → OAuth client ID → Desktop app** → any name → **Create**
  6. Copy the **Client ID** and **Client Secret** from the dialog
  7. Paste them in the app Configuration tab (`google_client_id`, `google_client_secret`) and **restart the app**

  **Part 2 — Authorize each service (run once per service):**
  - **Calendar**: send `/gcalauth` to the Telegram bot → click the link → sign in → grant access → **browser redirects back to DRADIS automatically** ✅. Enable Google Calendar in the Web UI and save.
  - **Gmail**: send `/gmailauth` to the Telegram bot → click the link → sign in → grant access → **browser redirects back to DRADIS automatically** ✅. Enable Gmail in the Web UI and save.
  - **Tasks**: send `/gtasksauth` to the Telegram bot → click the link → sign in → grant access → **browser redirects back to DRADIS automatically** ✅. Enable Google Tasks in the Web UI and save.
  - **Drive Backup**: send `/backupauth` to the Telegram bot → click the link → sign in → grant access → **browser redirects back to DRADIS automatically** ✅. Then create a monitor of type ☁️ Google Drive Backup in the Web UI.

  *Each service uses a separate token file — even if Calendar is already connected, run `/gmailauth` and `/gtasksauth` separately for the other services.*

  *If the automatic redirect doesn't work (HA on a different device), copy the full URL from the browser address bar and send it as `/gcalauth <url>`, `/gmailauth <url>`, or `/gtasksauth <url>`.*

---

## Web UI

After startup, the app exposes a web panel accessible directly from the Home Assistant sidebar (via HA Ingress — no external port required).

The UI uses a **vertical left sidebar** with seven collapsible sections: **Settings**, **Agents**, **Tools**, **Tasks**, **Scheduled Monitors**, **Live Monitors**, and **HA Monitors**. All sections except Settings are collapsed by default — click any header to expand it.

### Settings → DRADIS

Lets you edit all non-sensitive DRADIS settings at runtime without restarting the app. Changes are saved to `/data/dradis_settings.json` and take effect immediately on the next message.

| Field | Default | Description |
|-------|---------|-------------|
| Provider | `openrouter` | LLM provider: OpenRouter, OpenAI, GitHub Models, Gemini, or Groq. Select the provider whose API key is configured in the Configuration tab. |
| Model | *(see below)* | Model for the selected provider. Click 🔄 to fetch the available list, then ⚡ to speed-test all models in parallel (measures tok/s) and keep the top 5 sorted fastest first. Changing the provider clears the model list. |
| Fallback Provider | *(blank)* | Provider to use when the primary model call fails. Leave blank to use the same provider as the primary. |
| Fallback Model | *(blank)* | Model to retry with on API error. Leave blank to disable fallback. Click 🔄 to load models for the fallback provider, ⚡ to speed-test. |
| Agent instructions | `You are DRADIS, a versatile AI assistant.` | System prompt — defines the agent's role, behaviour, and any persistent facts about the user (name, preferences, language, etc.). |
| Startup message | `✅ DRADIS online and ready.` | Telegram message sent when the app starts. |
| Conversation history | `true` | Prepend the last N exchanges as context to each request. |
| Conversation history depth | `2` | Number of past exchanges kept in context (resets on restart). |
| Max completion tokens | `2048` | Caps the model's reply length (passed as `max_tokens`) so prompt+reply stay inside the model context window. Keep it at 2048 for the Groq 8K free tier; raise it for larger-context providers. |
| Sampling temperature | `0.2` | Passed as `temperature`. Low keeps the number of tool rounds — and so the token cost of a task — the same on every run; at the provider default (~1.0) an identical prompt costs a different number of rounds each time. |
| Max tool rounds | `3` | How many times the model may call tools before it must answer in text. Every round re-sends the whole conversation, so this multiplies the token cost of a task. |
| Provider tokens per minute | `0` | The rolling 60-second token budget of your plan, counting prompt and reply of every round. `0` uses what DRADIS knows: 8000 for Groq's free tier, no cap for anyone else. This is the limit that refuses a task — the model's context window is a separate, usually much larger number. |
| Log token usage | `off` | When on, appends `🔢 in N · out N` (input/output tokens) to every chat and task reply. |
| Log tools used | `off` | When on, appends `🔧 tool1, tool2` (the tools DRADIS called that turn, deduped) to every chat and task reply; shows `🔧 no tools` when the reply used none. |
| Report tool failures | `on` | When on, sends a `⚠️` Telegram message naming any tool that failed this turn (page unreachable, account disconnected) ahead of the reply, so an answer built on missing data is not mistaken for a good one. Unlike the two log footers above it is not suppressed in Car Mode. |
| Timezone for scheduled tasks | `UTC` | Timezone used to interpret all cron expressions. Select from the dropdown (covers Europe, Americas, Asia, Africa, Pacific). Changes take effect on next save — no restart required. |

**Model selection by provider:**

| Provider | How models are loaded |
|----------|-----------------------|
| OpenRouter | 🔄 fetches free ≥30B tool-calling models from the API; ⚡ speed-tests them in parallel; top 5 fastest are kept |
| OpenAI | 🔄 fetches GPT-4o, GPT-4o Mini, GPT-4 Turbo, and other tool-capable models from the API |
| GitHub Models | Fixed preset: GPT-4o, GPT-4o Mini, Phi 3.5 MoE, Phi 3.5 Mini, Llama 3.1 70B, Llama 3.1 8B, Mistral Nemo, Mistral Large |
| Gemini | Fixed preset: Gemini 2.0 Flash, 2.0 Flash Lite, 2.5 Pro Preview, 1.5 Pro, 1.5 Flash, 1.5 Flash 8B |
| Groq | 🔄 fetches LLM models from the API (Whisper models excluded) |

### Agents → Web Search

Configure the built-in Web Search sub-agent. A green dot in the sidebar indicates the agent is active.

When enabled, DRADIS automatically decides which tool to call — no prompt engineering required. Two tools are available:

| Tool | When used | Backend |
|------|-----------|---------|
| `search_web` | User asks a question or wants to search for information | [Tavily](https://tavily.com) — requires `tavily_api_key` |
| `read_url` | User provides a specific URL to read or summarise | [Jina Reader](https://jina.ai/reader/) — free, no API key required |

`search_web` returns up to 5 results with full page content. `read_url` fetches the page at the given URL and returns its text as markdown. It is a direct tool of the single agent — there is no synthesis sub-agent and no second LLM call (that architecture was removed in v2.12.0); DRADIS reads the page with its own model. The result is capped at 12 000 characters and then trimmed again by the runtime to whatever the model's window and the minute's token budget actually allow. A non-2xx response from Jina Reader raises a tool failure rather than being handed to the model as if it were the page.

Two things happen to the page before the model sees it. **Image tags are removed** — a page of signed thumbnail URLs costs thousands of tokens and says nothing. And **the extraction is checked**: Jina decides for itself which part of a page is the content, and it gets it wrong on pages built as lists. On a school news archive it kept the month menu and dropped all eighteen headlines, leaving the model a navigation bar to summarise — which it answered by calling `read_url` again. When what comes back is nearly all addresses rather than words, DRADIS reads the page a second time as plain text and keeps whichever reading carries more prose. That second read costs one HTTP request and no model tokens.

| Field | Default | Description |
|-------|---------|-------------|
| Enabled | `false` | Activate web search delegation. Requires `tavily_api_key` in the Configuration tab for query-based search. URL reading works without any additional key. |
| Test connection | — | Sends a test query to Tavily and reports the result inline. |
| LLM Provider | `openrouter` | Provider for the synthesis LLM (independent from DRADIS). |
| Model | — | Model used to synthesise search results. Click 🔄 to load, ⚡ to speed-test. |
| Fallback Provider | *(blank)* | Provider to use if the primary model call fails. |
| Fallback Model | *(blank)* | Model to retry with on API error. Leave blank to disable fallback. |
| Additional instructions | — | Optional extra instructions that apply **only when this capability's tools are used**. |

### Agents -> Weather

Configure the built-in Weather sub-agent, powered by [Open-Meteo](https://open-meteo.com) (free, no API key required). A green dot in the sidebar indicates the agent is active.

When enabled, DRADIS automatically calls `get_weather` when the user asks about current weather, forecasts, temperature, rain, wind, or UV index — in any language. The tool fetches current conditions and up to 16 days of forecast; a synthesis LLM formats the data into a clear response.

| Field | Default | Description |
|-------|---------|-------------|
| Enabled | `false` | Activate weather lookup delegation. No API key required. |
| Test connection | — | Pings Open-Meteo and reports the current temperature in Rome as a sanity check. |
| LLM Provider | `openrouter` | Provider for the synthesis LLM (independent from DRADIS). |
| Model | — | Model used to synthesise weather data. Click 🔄 to load, ⚡ to speed-test. |
| Fallback Provider | *(blank)* | Provider to use if the primary model call fails. |
| Fallback Model | *(blank)* | Model to retry with on API error. Leave blank to disable fallback. |
| Additional instructions | — | Optional extra instructions that apply **only when this capability's tools are used**. |

#### Weather variables fetched

| Resolution | Variables |
|---|---|
| **Current** | temperature, humidity, precipitation, wind speed & gusts, weather code, cloud cover |
| **Hourly** | temperature, humidity, dew point, precipitation probability, precipitation, showers, weather code, wind speed & gusts, cloud cover — summarised by time band (night/morning/afternoon/evening) |
| **Daily** | temp max/min, precipitation sum, weather code, wind speed & gusts max, precipitation probability max |

> **Thunderstorm risk** is handled by the dedicated **Thunderstorm risk monitor** (Monitors section) — it fetches CAPE, Lifted Index, CIN, and other convective variables from Open-Meteo and computes a risk score entirely in Python, with no LLM call and no token cost.

#### Team routing

When both Weather and Web Search sub-agents are active, DRADIS injects an explicit routing rule into the team leader system prompt: weather and meteorological queries are delegated exclusively to the Weather member. Web Search is never called for weather questions.

### Agents → Voice

Configure voice message transcription, powered by [Groq Whisper](https://console.groq.com). When enabled, DRADIS accepts Telegram voice messages (OGG audio), transcribes them using the Groq Whisper API, and passes the transcribed text to the main agent as if the user had typed it. A green dot in the sidebar indicates the agent is active. **Requires `groq_api_key`** in the Configuration tab — the Enabled toggle is disabled automatically when the key is missing.

| Field | Default | Description |
|-------|---------|-------------|
| Enabled | `false` | Activate voice transcription. Requires `groq_api_key` in the Configuration tab. |
| Test connection | — | Verifies the Groq API key can reach the Whisper models endpoint. |
| Whisper Model | `whisper-large-v3-turbo` | Groq Whisper model for transcription. Click 🔄 to fetch available Whisper models (this list is separate from LLM models). |
| Language | `it` | ISO 639-1 language code for transcription (e.g. `en`, `fr`, `de`). |
| Send transcription | `true` | Echo the transcribed text to Telegram as `🎙️ <text>` before the agent replies. |

### Agents → Google Calendar

Connect DRADIS to your primary Google Calendar. When enabled, the agent automatically calls the appropriate calendar tool when the user asks about their schedule or wants to manage events. **Requires `google_client_id` and `google_client_secret`** in the Configuration tab — see setup steps under *How to get your API keys* above.

Three tools are available:

| Tool | Description |
|------|-------------|
| `get_calendar_events` | Fetches events for the next N days (default: 7). Returns each event with its ID so it can be referenced for deletion. |
| `create_calendar_event` | Creates a new event with title, start/end datetime (ISO 8601 with timezone), and an optional description. If the user does not specify a duration, defaults to 1 hour. |
| `delete_calendar_event` | Deletes an event by ID. DRADIS always calls `get_calendar_events` first to retrieve the ID before deleting. |

A calendar sub-agent formats the raw API response using the configured LLM model before replying to the user.

| Field | Default | Description |
|-------|---------|-------------|
| Enabled | `false` | Activate calendar access. The toggle is disabled when credentials are not configured. |
| Authentication status | — | Shows whether the OAuth2 token is present. If not authenticated, send `/gcalauth` to the bot and follow the steps. |
| LLM Provider | `openrouter` | Provider for the calendar formatting sub-agent (independent from DRADIS). |
| Model | — | Model for the sub-agent. Click 🔄 to load, ⚡ to speed-test. |
| Fallback Provider | *(blank)* | Provider to use if the primary model call fails. |
| Fallback Model | *(blank)* | Model to retry with on API error. Leave blank to disable fallback. |
| Additional instructions | — | Optional extra instructions that apply **only when this capability's tools are used**. |

### Agents → Gmail

Connect DRADIS to your Gmail inbox. When enabled, the agent can read emails and send messages on your behalf. **Requires `google_client_id` and `google_client_secret`** in the Configuration tab — see *Gmail credential* under *How to get your API keys* above.

Four tools are available:

| Tool | Description |
|------|-------------|
| `get_emails` | Fetches the latest N emails from the inbox (default: 10). Returns sender, subject, date, and a short snippet. |
| `get_unread_emails` | Fetches unread emails only. |
| `search_emails` | Searches Gmail using any query supported by the Gmail search bar (e.g. `from:boss@example.com`, `subject:invoice`). |
| `send_email` | Sends a plain-text email. DRADIS always confirms recipient and subject before sending if they are not specified. |

A synthesis sub-agent formats the raw email data using the configured LLM model before replying to the user.

| Field | Default | Description |
|-------|---------|-------------|
| Enabled | `false` | Activate Gmail access. The toggle is disabled when credentials are not configured. |
| Authentication status | — | Shows whether the Gmail OAuth2 token is present. If not authenticated, send `/gmailauth` to the bot and follow the steps. |
| LLM Provider | `openrouter` | Provider for the email formatting sub-agent (independent from DRADIS). |
| Model | — | Model for the sub-agent. Click 🔄 to load, ⚡ to speed-test. |
| Fallback Provider | *(blank)* | Provider to use if the primary model call fails. |
| Fallback Model | *(blank)* | Model to retry with on API error. Leave blank to disable fallback. |
| Additional instructions | — | Optional extra instructions that apply **only when this capability's tools are used**. |

### Agents → Google Tasks

Connect DRADIS to your Google Tasks. When enabled, the agent manages your to-do lists via natural language in Telegram. **Requires `google_client_id` and `google_client_secret`** in the Configuration tab — see setup steps under *How to get your API keys* above.

Five tools are available:

| Tool | Description |
|------|-------------|
| `list_tasks` | Fetches all open tasks in the specified list (default: `@default`). Returns each task with its ID in brackets so it can be referenced for future operations. |
| `create_task` | Creates a new task with a title, optional notes, and an optional due date (YYYY-MM-DD format). |
| `complete_task` | Marks a task as completed by ID. DRADIS always calls `list_tasks` first to retrieve the ID before completing. |
| `delete_task` | Permanently deletes a task by ID. DRADIS always calls `list_tasks` first to retrieve the ID before deleting. |
| `update_task` | Renames a task or updates its notes by ID. DRADIS always calls `list_tasks` first to retrieve the ID. |

A synthesis sub-agent formats the raw task data using the configured LLM model before replying to the user.

| Field | Default | Description |
|-------|---------|-------------|
| Enabled | `false` | Activate Google Tasks access. The toggle is disabled when credentials are not configured. |
| Authentication status | — | Shows whether the OAuth2 token is present. If not authenticated, send `/gtasksauth` to the bot and follow the steps. |
| LLM Provider | `openrouter` | Provider for the tasks sub-agent (independent from DRADIS). |
| Model | — | Model for the sub-agent. Click 🔄 to load, ⚡ to speed-test. |
| Fallback Provider | *(blank)* | Provider to use if the primary model call fails. |
| Fallback Model | *(blank)* | Model to retry with on API error. Leave blank to disable fallback. |
| Additional instructions | — | Optional extra instructions that apply **only when this capability's tools are used**. |

The shortcut command `/todo` lists all open tasks directly without going through the DRADIS team routing — zero overhead.

### Settings → Car Mode

DRADIS messages are built to be **looked at**: an icon on every line, bold text, abbreviated units, `·` and `—` between facts, and for weather a radar chart attached as a photo. Behind the wheel that format breaks down. CarPlay hands the notification to the speech engine, which announces emoji by name ("cloud with lightning and rain"), spells `45°` and `2/4` as symbols, reads URLs character by character, and often says nothing beyond "Image" when a photo is attached.

With **Car Mode** on, every alert, scheduled report and chat answer is rewritten as plain spoken prose:

- icons and markup removed;
- links removed whole — every one DRADIS sends is a tap target, and a tap target with no link behind it is an instruction you cannot follow;
- coordinates, record ids and instrument diagnostics (fix age, accuracy, "non si sta muovendo", radar coverage) dropped;
- units spelled out — `12 km/h` → *12 chilometri orari*, `±12 m` → *più o meno 12 metri*;
- compass points expanded — `O` → *ovest* (spoken, the abbreviation is the conjunction "or");
- ratios turned into words — `Anello 2/4` → *Anello 2 su 4*;
- dates spelled out — `21/08/2026 14:32` → *21 agosto 2026 14:32*. A ratio and a date are the same characters, so the day-month form is only read as a date when a clock follows it (`21/08 14:32`); `Anello 2/4` stays a ratio;
- lines joined into sentences.

The conversion is deterministic: no model call, no added latency on an urgent alert, no tokens spent. Monitors keep using their own configured language.

**What is not sent:** radar charts and scheduled chart reports. A picture is exactly what a driver cannot use, and a photo notification is the one CarPlay tends not to read at all. A chart-only scheduled report is replaced by a line saying it is waiting, so it never disappears silently.

**What is not said:** anything that describes the instrument rather than the weather — the token footer (`🔢 in N · out N`), the tools used (`🔧 …`), the monitor signature, coordinates, map links, record ids, fix age and accuracy, "non si sta muovendo", radar coverage, the open-event state, and the "nothing was changed" reassurance. All of it is worth a block on screen and nothing at all through a speaker.

**What is always said:** every line that explains a failure — a blind monitor and why, a switched-off one, a stale feed, a position that no longer exists. Silence and calm must not sound the same.

**What keeps its picture:** snapshots you asked for with `/rain` and `/storm`. You requested those, so you are looking at the screen — the caption is still converted.

**Not converted:** the output of `/info`, `/manage`, `/menu`, `/tasks`, `/monitors` and `/hamonitors`. They answer a button you just pressed.

| Field | Default | Description |
|-------|---------|-------------|
| Enabled | `off` | Turn Car Mode on. Also togglable from Telegram with `/car`. Takes effect on the next message — nothing needs reloading. |
| Test message | — | Sends a sample storm alert in Car Mode wording to your default Telegram bot, **whatever the toggle is set to** — you use it to decide whether to switch Car Mode on. Listening to it through CarPlay is the only real test. |

> Activation is manual by design. GPS speed cannot tell *stopped in traffic* from *parked and walked away* — physically the same signal — so an automatic trigger would switch off exactly when you are still driving.

### Settings → Telegram Bots

Configure additional Telegram bots. Each monitor, live monitor, HA monitor, and task can independently choose which bot delivers its notifications — the default DRADIS bot (configured in the HA Configuration tab) is always available as the fallback.

Extra bots are stored in `/data/dradis_settings.json` and never committed to version control.

| Field | Description |
|-------|-------------|
| Name | Label shown in the bot selector dropdown inside each monitor/task form. |
| Bot Token | Telegram Bot API token (from [@BotFather](https://t.me/BotFather)). Stored as plain text in `/data/`. |
| Chat ID | Telegram chat or group ID that the bot should send messages to. |

**Actions per bot:**
- **🔗 Test** — sends a verification message to the configured chat ID to confirm the bot is reachable.
- **✏️ Edit** — update name, token, or chat ID.
- **🗑️ Delete** — removes the bot. Monitors/tasks that were using it will automatically fall back to the DRADIS default bot on their next execution.

Bot instances are loaded into a runtime registry at startup and refreshed immediately when you add, edit, or delete a bot — no restart required.

---

### Settings → MQTT / Home Assistant

Configure the MQTT broker connection used by HA Monitors. Required before creating any HA Monitor.

| Field | Default | Description |
|-------|---------|-------------|
| Broker host | `core-mosquitto` | Hostname or IP of the MQTT broker. Use `core-mosquitto` for the HA Mosquitto add-on. |
| Port | `1883` | MQTT broker port. |
| Username | *(blank)* | MQTT username (leave blank if the broker has no authentication). |
| Password | *(blank)* | MQTT password. |
| Statestream prefix | `homeassistant` | Base topic prefix used by `mqtt_discoverystream_alt`. Must match the `base_topic` set in `configuration.yaml`. |

Click **Save** to apply. Changes take effect immediately — no restart required.

### Settings → Positions

A **position** is a phone DRADIS can follow. A Storm front monitor selects one and then watches wherever *that* phone is, so while travelling it can tell you whether you are driving into the storm rather than away from it. Add one per phone — yours, another family member's — and give each a name you will recognise in an alert, because that name is what the alert is titled with.

Positions are stored under `positions` in `/data/dradis_settings.json`, and they all share **one** MQTT connection. Nothing connects until you add one.

#### Publishing a phone's position (Home Assistant side)

A `device_tracker` keeps its coordinates in **attributes**, and `mqtt_statestream` publishes **states**. Turning on `publish_attributes` would expose them, but it is a *global* switch that floods the broker with every attribute of every included entity. Expose just the two values instead, in `configuration.yaml`:

```yaml
template:
  - sensor:
      - name: "Phone latitude"
        unique_id: phone_latitude
        state: "{{ state_attr('device_tracker.my_phone', 'latitude') | round(5) }}"
        availability: "{{ state_attr('device_tracker.my_phone', 'latitude') is not none }}"
      - name: "Phone longitude"
        unique_id: phone_longitude
        state: "{{ state_attr('device_tracker.my_phone', 'longitude') | round(5) }}"
        availability: "{{ state_attr('device_tracker.my_phone', 'longitude') is not none }}"
      - name: "Phone GPS accuracy"
        unique_id: phone_gps_accuracy
        state: "{{ state_attr('device_tracker.my_phone', 'gps_accuracy') | int(0) }}"
        availability: "{{ state_attr('device_tracker.my_phone', 'gps_accuracy') is not none }}"

mqtt_statestream:
  base_topic: homeassistant
  publish_attributes: false
  publish_timestamps: true
  include:
    entities:
      - sensor.phone_latitude
      - sensor.phone_longitude
      - sensor.phone_gps_accuracy
```

Repeat the three sensors per phone, with distinct names.

`round(5)` is ~1 metre of resolution — precise enough for anything the storm front does, and it damps the GPS jitter that would otherwise republish constantly. The `availability` guard keeps `unknown` off the topic while the app has no fix yet.

**If you already have an `mqtt_statestream` block, merge into it.** Home Assistant accepts only one, and a second would silently drop your current includes.

**Keep `publish_timestamps: true`.** It publishes `last_updated` next to each value, which is the only way to date the *retained* message received on connect. Without it a position from yesterday arrives looking brand new and no staleness check can catch it.

Finally, raise the Companion app's location update frequency (*Settings → Companion app → Manage sensors → Location sensors*). The default "significant change" reporting can lag by minutes at motorway speed, which is exactly when this has to be right. Consider triggering high-accuracy mode from your car's Bluetooth so it only runs while you drive.

#### Fields per position

| Field | Default | Description |
|-------|---------|-------------|
| Name | *(blank)* | What alerts are titled with. Make it recognisable. |
| Latitude entity | *(blank)* | Topic path after the prefix, e.g. `sensor/phone_latitude`. |
| Longitude entity | *(blank)* | As above. Both are required. |
| GPS accuracy entity | *(blank)* | Optional. When unset, the accuracy threshold is not applied. |
| Maximum fix age | `15` min | Older and a monitor following this position stops alerting until it comes back. Tripled while a storm is in progress. |
| Maximum GPS accuracy | `500` m | Vaguer fixes are not used. |
| Statestream prefix override | *(blank)* | Defaults to the global MQTT prefix. |

**🔍 Detect entities** listens on MQTT for three seconds and keeps only what looks like a coordinate, so you pick from a handful of candidates rather than every entity on the broker. When exactly one candidate matches a field it is filled in for you; when several do, each field's dropdown offers only its own kind. If nothing is recognised — an unusual naming scheme — every entity is offered instead, so this is never a dead end.

**Test connection** uses the values **currently on screen**, saved or not: testing a form you have not saved yet is the normal case. It connects with its own throwaway client, so the running manager is never disturbed. It reports the fix, its age, its accuracy and your speed, and names the threshold that failed — "no position at all" and "a position from two hours ago" are different problems with different fixes.

**Deleting a position** does not rewrite the monitors following it. Silently converting them back to a fixed place would put them somewhere you never asked to watch, so they freeze instead, and their form shows the dangling reference.

### Scheduled Monitors

Scheduled monitors fetch data from external APIs and compute results entirely in Python, then deliver them to your Telegram chat on a cron schedule. By default no LLM is invoked — output is deterministic and costs no tokens. Monitors are stored in `/data/monitors.json`.

Click `+` in the **Scheduled Monitors** sidebar header to create a new monitor. Each monitor has:

| Field | Description |
|-------|-------------|
| Name | Display name shown in the sidebar. |
| Enabled | Toggle — a green dot in the sidebar shows the monitor is active. |
| Monitor type | Type of data source: **⛈️ Thunderstorm risk**, **🌧️ Rain alert**, **📊 Weather Charts** (all Open-Meteo, no API key required), **🌍 Seismic report** (INGV GOSSIP), **🚨 Civil Protection alert** (Centro Funzionale Campania, today + tomorrow), or **☁️ Google Drive Backup**. |
| Response language | Language of the Telegram report: 🇮🇹 **Italiano** (default) or 🇬🇧 **English**. |
| Location | City name or geographic description (e.g. *Bacoli*, *Napoli*, *Roma*). Resolved to coordinates via Open-Meteo geocoding, keeping the most populous match; add a country to disambiguate (*Springfield, US*). A live hint shows the resolved name and coordinates as you type. |
| Forecast days | *(Thunderstorm only)* Number of days to fetch (1–7, default 2). |
| Hours ahead | *(Rain alert only)* How many hours ahead to check for rain (1–24, default 2). |
| Alert from level | *(Civil Protection alert only)* Minimum zone level, on either day, that triggers a message: 🟡 Giallo (default), 🟠 Arancione, 🔴 Rosso, or 🟢 Sempre. |
| Alert mode | **Direct Telegram** (default): sends the report immediately without consuming tokens. **LLM**: passes the generated report to the full DRADIS agent together with custom instructions — the agent can send Telegram messages, emails, create tasks, etc. |
| DRADIS Instructions | *(LLM mode only)* Instructions for the agent: what to do with the report. If empty, the agent sends the report to Telegram. |
| Schedule preset | Dropdown of common schedules. |
| Cron expression | Raw 5-part cron with live validation and next-fire preview. |
| Telegram bot | Bot used to send the monitor output. Defaults to the DRADIS bot; select any extra bot configured in **Settings → Telegram Bots**. |

#### Thunderstorm risk monitor

Fetches atmospheric instability data from [Open-Meteo](https://open-meteo.com) (free, no API key required) and computes a **Thunderstorm Risk Score (TRS)** for each time band of each forecast day. No LLM is used — all computation happens in Python.

**Variables fetched (hourly):** CAPE, Lifted Index (LI), Convective Inhibition (CIN) — all provided directly by Open-Meteo, no pressure-level variables required.

**Risk formula — multiplicative composite (TRS ∈ 0.0–1.0):**

```
TRS = CAPE_norm × LI_norm × CIN_norm
```

The multiplicative structure means that if any single ingredient is absent the score collapses to zero — mirroring how convection requires all ingredients simultaneously. K-Index was evaluated but dropped: it proved unreliable for the Mediterranean because dry air at 700 hPa suppresses it even under genuine convective risk.

| Component | Normalisation | Notes |
|---|---|---|
| CAPE_norm | `min(CAPE / 1200, 1.0)` | Mediterranean: 800 J/kg = 67%, saturates at 1200 J/kg |
| LI_norm | `min(max(−LI / 5, 0), 1.0)` | LI −3°C = 60%; saturates at −5°C |
| CIN_norm | `max(1 − \|CIN\| / 100, 0.0)` | CIN = 0 → 1.0 (no cap); CIN ≥ 100 J/kg → 0.0 (fully suppressed) |

**Risk levels:**

| TRS | Level |
|---|---|
| 0.0 – 0.2 | 🟢 TRASCURABILE / NEGLIGIBLE |
| 0.2 – 0.4 | 🟡 BASSO / LOW |
| 0.4 – 0.6 | 🟡 MODERATO / MODERATE |
| 0.6 – 0.8 | 🟠 ELEVATO / HIGH |
| 0.8 – 1.0 | 🔴 MOLTO ELEVATO / VERY HIGH |

The Telegram message shows one line per time band (NIGHT 00–06, MORNING 06–12, AFTERNOON 12–18, EVENING 18–24) with the TRS score (0.00–1.00) and risk label only. Raw parameter values (CAPE, LI, CIN) are not shown in the message. Each day ends with the daily peak risk level.

**Testing a monitor manually:** each monitor form includes a **▶ Test Monitor** button that triggers an immediate execution. The result is delivered to Telegram within seconds.

**Duplicating a monitor:** click **⎘ Copy** in any monitor form to create a copy named `Copy of <name>`. The duplicate is disabled by default, with the same cron, type, location, and all other fields. It is immediately selected in the sidebar and ready to edit.

#### Weather Charts monitor

Fetches hourly forecasts from [Open-Meteo](https://open-meteo.com) (free, no API key required) for up to 5 NWP models and sends **one PNG chart per selected variable** as separate Telegram photos. No LLM is used.

**Supported models:**

| Model key | API parameter | Coverage | Notes |
|-----------|--------------|----------|-------|
| ECMWF IFS 9km | `ecmwf_ifs025` | Global | ~10-day horizon; no UV index |
| ICON EU 7km | `icon_eu` | Europe | 5-day horizon |
| Météo-France ARPEGE | `meteofrance_arpege_europe` | Europe | 4-day horizon |
| GFS Global | `gfs_global` | Global | 16-day horizon; supports all variables |
| ItaliaMeteo ARPAE | `italia_meteo_arpae_icon_2i` | Italy | 2 km, 48h horizon |

**Supported variables:**

| Variable | Unit | Chart type | Notes |
|----------|------|-----------|-------|
| Temperature 2m | °C | Line | All models |
| Apparent Temperature | °C | Line | All models |
| Precipitation | mm | Numbers | One lane per model; 3-hour totals. Always sent (0 if no rain expected) |
| Precipitation Probability | % | Numbers | One lane per model. ECMWF IFS + GFS only; always sent |
| Wind Speed 10m | km/h | Numbers | One lane per model |
| Wind Gusts 10m | km/h | Numbers | One lane per model; 3-hour peaks |
| Wind Direction 10m | ° | Arrows | One lane per model; arrows point downwind |
| Humidity 2m | % | Line | All models |
| Sea Level Pressure | hPa | Line | All models |
| Cloud Cover | % | Numbers | One lane per model. Always sent (0 if clear sky) |
| UV Index | — | Bar | GFS only; suppressed if all-zero |
| Geopotential 500 hPa | m | Line | All models |
| Temperature 850 hPa | °C | Line | All models |

**Chart appearance:** 16×5 inch figure at 150 dpi, dark theme (#111 background), five high-contrast colours (blue / red / green / amber / magenta), 2-px line width. Each chart title includes the variable name, location, forecast days, and generation timestamp. The x-axis is in the **local time of the location**, with a bright vertical line at midnight and a fainter one at midday, over a dashed grid every 6 hours.

**Forecast window:** Open-Meteo always answers from 00:00 of the current day, so everything before the run is discarded and the series starts at the next 3-hour mark (a run at 10:16 starts at 12:00) — which keeps the labels of the lane charts on round hours, aligned with the ticks and the midnight line. "3 days" therefore means 72 hours ahead of the run, not three calendar days.

**Precipitation, precipitation probability and cloud cover** are sent even when all values are zero, so a lane of zeros communicates "no rain / clear sky." All other bar-type variables are suppressed if no model returns any non-zero value.

**Lane charts.** Wind direction, cloud cover, precipitation, precipitation probability, wind speed and wind gusts are drawn as **lanes** — one horizontal lane per model, sampled every 3 hours, labelled on the y-axis with the model name instead of a legend. Wind direction shows a uniform arrow pointing downwind (the way the wind blows toward), which avoids both the compass wraparound problem (359°→0°) of a line and the unreadable point cloud a multi-model 0–360° scatter produces. The other five print the **value as a bare number** (the unit is in the chart title), because overlapping semi-transparent bars from several models were illegible. Zero values are dimmed so the real ones stand out in a mostly dry lane.

Two of them aggregate the 3-hour window rather than sampling one hour out of three, which would silently drop data: **precipitation** shows the window **total** (rainfall accumulates), **wind gusts** the window **peak** (a gust chart exists to show the maximum). Models that do not carry a variable are omitted from that chart instead of showing an empty lane.

**Configuration fields:**

| Field | Description |
|-------|-------------|
| Location | City name resolved via Open-Meteo geocoding — the most populous match wins. Add a country to disambiguate: *Springfield, US*. |
| Forecast days | Number of days to plot (1–7, default 3), counted **from the moment the monitor runs**, not from midnight. |
| Weather models | Select one or more models (checkboxes with description). |
| Variables to plot | Select one or more variables. Each generates a separate chart image sent to Telegram. |
| Cron | Schedule (e.g. `0 7 * * *` = daily at 07:00). |

**Example configuration:**

| Field | Value |
|-------|-------|
| Name | Morning Weather Charts |
| Type | 📊 Weather Charts (Open-Meteo) |
| Location | Naples |
| Forecast days | 3 |
| Weather models | ECMWF IFS 9km ✅, ICON EU 7km ✅, GFS Global ✅ |
| Variables | Temperature 2m ✅, Precipitation ✅, Wind Speed 10m ✅, Precip. Probability ✅ |
| Cron | `0 7 * * *` |

#### Rain alert monitor

Fetches 15-minute precipitation data from [Open-Meteo](https://open-meteo.com) (free, no API key required) for the next 24 hours and checks whether rain is forecast within the configured time window. **If no precipitation is expected, no Telegram message is sent** — the monitor is completely silent when conditions are clear.

When rain is detected, the Telegram message lists every 15-minute slot in the window:

- 🔵 slots with precipitation > 0 mm (amount shown in mm)
- ⚪ dry slots (shown for context)
- 💧 total precipitation for the window at the end

**Configuration:**

| Field | Description |
|---|---|
| Location | City name resolved via Open-Meteo geocoding — the most populous match wins. Add a country to disambiguate: *Springfield, US*. |
| Hours ahead | How far ahead to look for rain (1–24, default 2). |
| Language | 🇮🇹 Italiano / 🇬🇧 English. |
| Cron | How often to check (e.g. `0 * * * *` = every hour). |

#### Civil Protection alert monitor (Campania)

Reads the alert bulletins issued by the **Centro Funzionale Multirischi di Protezione Civile della Regione Campania** — **today's and tomorrow's, always both** — and reports the alert level of all eight alert zones on each. No API key, no LLM.

Both days rather than a choice between them: the question this monitor answers is *is there an alert*, and half an answer to that is worse than none. Tomorrow is also the actionable half — today's window is already running by the time anyone reads the message.

**Why an API and not the website.** `centrofunzionale.regione.campania.it` is an Angular single-page app: the HTML it serves is an empty shell and the bulletin is drawn client-side, so `read_url` — and any other HTML fetcher — gets back `Caricamento in corso...` and nothing else. The page's own JavaScript bundle calls a public, unauthenticated REST backend that returns the bulletin already structured, and that is what this monitor reads. Nothing here parses HTML, images or PDF.

**Endpoints** (base `https://centrofunzionale.regione.campania.it/CentroFunzionalePortaleRest/rest/bollettinometeo`), both fetched concurrently on every run:

| Bulletin | Endpoint |
|---|---|
| Oggi | `findLastAllertaNew` |
| Domani | `findAllertaDomaniNew` |

These are the two endpoints the site's own home-page alert map calls. The `findLastBollettino` pair, used until v4.7.4, answers a different question and returns today's window with a null `dataDa`.

The bulletin is issued in the morning and is valid from 14:00 to 14:00 the following day. **An empty zone list means different things on the two days**, and the site's map treats them differently: today's window is already running, so no zone listed means *nessuna allerta* and the map is painted green; tomorrow's map stays grey — undecided — until the response's `checkAvviso` flag turns true, and only then is a zone-less bulletin a green one. The report mirrors that: `🟢 Nessuna allerta su tutte le zone` for a day the region has declared quiet, `Bollettino non ancora emesso` only for a day it has not yet decided. Either way the validity window is known and shown.

**`fenomeni` and `scenari` are fetched only if the alert endpoints stop carrying them.** Those endpoints feed a map that needs nothing but the zone and its level — the site fetches the prose separately, per zone, when one is clicked. It currently arrives inline, and when a zone in alert arrives without it, `findByIdBollettino/{id}` is asked for the text. No extra request on the common path, and none at all on a day with no alert.

**Tomorrow is fetched tolerantly, today is not.** If the *tomorrow* endpoint fails, the error is reported inside the message and today's alert still goes out — an alert that exists must reach the phone even when the other half of the request is down. If the *today* endpoint fails, that is the monitor failing and the scheduler says so.

**Alert levels** (as defined by the region):

| Level | Meaning |
|---|---|
| 1 | 🟢 Verde — nessuna allerta |
| 2 | 🟡 Giallo — criticità ordinaria |
| 3 | 🟠 Arancione — criticità moderata |
| 4 | 🔴 Rosso — criticità elevata |

**Alert zones.** The API returns the zone as a bare number; the names live only in the site's JavaScript bundle and are carried in `monitors/campania_alert.py`:

| # | Zone |
|---|---|
| 1 | Piana campana, Napoli, Isole, Area Vesuviana |
| 2 | Alto Volturno e Matese |
| 3 | Penisola sorrentino-amalfitana, Monti di Sarno e Monti Picentini |
| 4 | Alta Irpinia e Sannio |
| 5 | Tusciano e Alto Sele |
| 6 | Piana Sele e Alto Cilento |
| 7 | Tanagro |
| 8 | Basso Cilento |

**If every zone is below the configured level on both days, no Telegram message is sent** — same contract as the rain alert monitor. A single zone reaching the level on either day is enough to fire, so an orange tomorrow reports even when today is entirely green.

When it does fire, the message carries one block per day: the validity window, the notice number and issue time, one line per zone in alert with its risk type, and the green zones compacted into a single line. The bulletin repeats the same `fenomeni` and `scenari` text on every zone in alert, so within each day they are de-duplicated and printed once (scenarios truncated at 900 characters).

**Configuration:**

| Field | Description |
|---|---|
| Alert from level | Minimum level, on either day, that triggers a message (default 🟡 Giallo). |
| Language | 🇮🇹 Italiano / 🇬🇧 English. |
| Cron | Suggested `30 14 * * *` — just after the bulletin takes effect and around when tomorrow's is published. |

---

### Tasks

Create recurring automated tasks that DRADIS executes on a cron schedule and delivers to your Telegram chat. Tasks are stored in `/data/tasks.json`.

Click `+` in the Tasks sidebar header to create a new task. Each task has:

| Field | Description |
|-------|-------------|
| Name | Display name shown in the sidebar. |
| Enabled | Toggle — a green dot in the sidebar shows the task is active. |
| Schedule preset | Dropdown of common schedules: Every minute, Every hour, Daily at 8:00, Daily at 20:00, Every Monday at 9:00, Weekdays 9–18 every hour. |
| Cron expression | Raw 5-part cron field (minute hour day month weekday). Editing it directly sets the preset to "Custom…" and shows a live human-readable description below the field. |
| Instructions | What DRADIS should do at this time — passed to the agent, which calls whichever of the attached tools it needs. |
| Tools | Which tools this task may use. **All available tools** (default) attaches every enabled + authenticated tool; **Selected tools** lets you tick exactly the ones the task needs (grouped by capability). Fewer tools = smaller prompt — the way to keep multi-step Gmail/Calendar tasks under the Groq 8K free-tier limit. |
| Telegram bot | Bot used to deliver the task response. Defaults to the DRADIS bot; select any extra bot configured in **Settings → Telegram Bots**. |

When a task fires, the agent response is sent to your Telegram chat. DRADIS runs as a single agent on the main model with the tools you selected. Cron jobs reload immediately on save/delete — no app restart required.

> **Tip (v3.0):** for a *mail → calendar* task, select just `get_unread_emails` and `create_calendar_event`. That keeps each request small and well under Groq's 8000 tokens-per-minute limit, even across the read → create → summarise steps.

**Testing a task manually:** each task form includes a **▶ Test Task** button. Clicking it triggers an immediate one-off execution of the task without altering the cron schedule. The result is delivered to Telegram exactly as a scheduled run would. This is useful for verifying instructions before enabling a task or debugging an existing one — no need to modify the cron expression to `* * * * *` just for a quick check.

**Duplicating a task:** click **⎘ Copy** in any task form to create a copy named `Copy of <name>`. The duplicate is disabled by default, with the same cron and instructions. It is immediately selected in the sidebar and ready to edit.

---

### Live Monitors

Create persistent push-based monitors that stay connected to an external data source and react to events in real time — no cron schedule, no LLM, no token cost. Live monitors are stored in `/data/live_monitors.json` (separate from scheduled monitors).

Click `+` in the **Live Monitors** sidebar header to create a new live monitor. Each monitor has a **Name**, **Enabled** toggle, and **Type** selector. Additional fields depend on the type:

| Type | Required fields |
|------|----------------|
| 🌩️ Storm front / CBDR | Where to watch, Location *(fixed only)*, Radius (km), Updates per storm, Radar, Language, Quiet hours *(optional)* |
| 🌧️ Rain front | Where to watch, Location *(fixed only)*, Radius (km), Updates per event, Minimum intensity, Hail *(optional)*, Radar, Language, Quiet hours *(optional)* |
| 🌍 Seismic live | Areas, Quiet hours |
| ⚽ Football Betting | Minute windows, Quiet hours (API pause) |

There is no cron field and no "run now" action — the monitor is always-on when enabled.

#### Storm front / CBDR

Subscribes to the geohash MQTT topics covering the configured location. Incoming strikes are buffered for **10 minutes** as `(time, lat, lon)` — deliberately **without** their distance, which is derived at evaluation time.

##### The grid

Every 60 seconds the buffer is binned into a fixed grid of **concentric rings × 12 sectors of 30°**. `(lat, lon) → (sector, distance)` is a pure function of the strike and the origin: there is no assignment step, no `min_samples`, no neighbour search, so **nothing can be re-labelled between two polls**. Two consecutive frames differ only by strikes that arrived and strikes that aged out. That stability is the property the DBSCAN generations and the global-percentile generation both lacked, and it is what makes the whole thing trustworthy. It is also O(n).

Rings are derived proportionally from the radius, so the shape of the alert ladder does not change when you change the radius:

| Updates per storm | Ring edges at R = 30 km |
|---|---|
| 2 | 30 / 12 km |
| 3 | 30 / 16.5 / 7.5 km |
| 4 *(default)* | 30 / 19.5 / 12 / 6 km |

The feed observes out to **1.6 × the radius** (48 km at R = 30). Those strikes never trigger anything — they exist so the bearing history is already populated by the time the front crosses the alert radius, which is what lets the *first* message carry a track verdict.

##### The front

Each sector's **front** is the leading edge of its activity: a low quantile of the distances in that sector, floored at the **3rd-nearest strike**. The floor is the load-bearing half. Blitzortung mislocates strikes by a few km routinely and occasionally by much more, and the floor means one or two phantom strikes can never pull the front inward, however busy the sector is. That is a structural guarantee, not a threshold to tune.

A sector needs at least 4 strikes in the window to count at all. The **dominant** sector is the active one with the nearest front, and only the dominant sector drives the ring — which is what makes several simultaneous cells safe: they can never produce two parallel message streams.

##### CBDR — will it hit me?

The mariner's collision rule: *constant bearing, decreasing range*. A descending ring is the precondition; the discriminant is whether the cell's bearing holds or rotates.

The rotation is converted into **sideways travel in kilometres** (`lever_arm × sin(rotation)`), because degrees are not comparable across distances: the same 25° swing is 12 km of sideways travel at 28 km out but only 2 km at 5 km out, so a degree threshold would label a storm about to hit you "a glancing pass" exactly when it is closest. The two bearings compared are each averaged over several polls, and the sampling windows are placed further apart than the analysis window is wide — polls 60 s apart share ~90 % of their strikes, so closer samples would not be independent and averaging them would buy nothing.

| Sideways travel | Verdict |
|---|---|
| ≤ 2.5 km | 🧭 Constant bearing — heading straight for you |
| 2.5 – 4 km | 🧭 Track not yet determinable |
| ≥ 4 km | 🧭 Glancing pass — going by to the *(side)* |

Calibrated over 175 seeded simulated scenarios: **zero** head-on storms mislabelled as grazing (the dangerous error), 73 of 75 grazing storms correctly identified.

**There is no ETA.** The only temporal figure in any message is the measured wall-clock time between two confirmed ring crossings ("from 27 to 18 km in 9 min") — an observation, not an extrapolation. The sequence of messages in the chat *is* the approach timeline.

##### When it speaks

A ring is announced **at most once per event**. There is no periodic re-alert of any kind, and no message when the storm retreats. Two independent barriers keep it quiet: the ring index has 15 % hysteresis on the way out, and an announced ring is never announced again.

| Event | Trigger |
|-------|---------|
| Ring message | The front reaches a ring deeper than any announced so far, confirmed over 2 consecutive polls |
| All clear ✅ | No activity inside the radius for 10 minutes, and the feed has been connected for at least 2 minutes |

This yields **at most `ring_count` messages plus one all-clear per storm** — for any input whatsoever. A direct hit is 5 messages; a glancing storm 2–3; a distant cell parked for hours, 1 or 2 and then silence.

##### Two invariants

These replace the threshold tuning of the previous six generations, and both are enforced by tests:

- **A · Bounded messages.** `notified_ring` increases strictly at every message and resets only when the event closes. The v3.3.0 field failure — a weak cell parked between thresholds re-alerting every 10 minutes for hours — is not unlikely here, it is *arithmetically impossible*.
- **B · Every state has a reachable exit.** The only thing holding an event open is activity inside the radius, over a sliding window with unconditional expiry. No exit condition mentions speed, ring, bearing, verdict or ETA. Escalation reads the front, de-escalation reads activity in the same window and the same radius, so one variable holds the event open and it decays to zero on its own.

##### Behaviour

1. On startup (or a config change) the MQTT feed and a 60-second poll task start, and any saved state less than 30 minutes old is restored from `/data/storm_front_state.json` — so a storm in progress does not re-announce rings it already announced.
2. Blitzortung's own strike timestamp is used when present, so a reconnect backlog ages out instead of counting as current.
3. A diagnostic line is logged each poll: `[StormFront] name | ring=2/4 notified=2 front=18.3km sec=10 act=3 n=22 evt=ACTIVE`.
4. The state advances **on a confirmed Telegram send, and on an unconfirmed one**; only a *refused* send is retried on the next poll, rebuilt from the *current* frame so the retry is never stale. Delivery is three-valued — `DELIVERED`, `REFUSED`, `UNCONFIRMED` — because a `TimedOut` on a photo upload means the answer never came back, not that nothing arrived, and with a chart attached it usually did. Treating that as a failure re-sent every ring message of an event. The trade: an unconfirmed send that arrived would duplicate every message, one that did not costs a single ring out of a ladder that still has the deeper rings and the all-clear.
5. If the feed goes down, everything freezes: no alerts, and the all-clear countdown restarts from zero on reconnect. A dead socket and a clear sky are indistinguishable if you only count strikes, so the monitor refuses to guess. After 15 minutes of silence, or repeated connection failures, it reports **🟠 Degraded**.

**Quiet hours** silence the outer rings and the all-clear. A front already within 40 % of the radius always gets through. Suppressed alerts are still committed, so they are not retried every minute until the window ends.

**Radius** is clamped to **10–60 km**, in the UI and again in the monitor.

##### Where to watch — a fixed place, or a phone

**Where to watch** chooses where the radar is centred. `📌 A fixed place` (the default, and what every monitor configured before v4.1.0 inherits) uses the Location field and never consults the position manager. Selecting a position instead makes the monitor follow that phone — and the Location field disappears, because it is no longer used for anything.

Nothing in the collision logic changed for this, and the reason is worth stating. The strike buffer holds **absolute** coordinates and the geometry is rebuilt against the current origin on every poll, so a moving origin simply produces a different — and correct — frame. CBDR then follows for free: it compares bearings and ranges *measured from the origin*, so a moving origin turns those into **relative** bearings and ranges, which is exactly what the mariner's rule was always defined on. Constant relative bearing with decreasing relative range means collision whether or not you are under way.

What is not free is telling **moving** apart from **being moved**:

- A continuous track is signal and is kept — that is the whole point.
- A **jump** is a change of reference frame: a mislocated fix, or the position returning after a blackout somewhere else entirely. The stored bearings were measured from somewhere else, so the CBDR history is dropped. The *event* stays open on purpose — reopening it would reset the notification ladder and let one storm emit a second full set of ring messages, which is precisely what invariant A exists to prevent.
- A single wild fix is **held until the next report agrees with it**, the same rule ring descents follow. One bad reading cannot move the radar 300 km.

When you are moving, ring alerts carry an extra line:

```
⚡ Temporale più vicino — Cellulare di Procolo
📍 Fronte a 18 km a N (0°)
🎯 Anello 2/4 · entro 20 km
🧭 Rotta costante: ti arriva addosso
🚗 Ti stai dirigendo verso il temporale a 90 km/h
🔢 22 fulmini in 10 min (settore N)
🕐 12:05
```

The 🚗 line appears when your course is within 45° of the front, otherwise `🚗 In movimento a 90 km/h verso NE`. It **explains** the track verdict above it rather than competing with it: the CBDR reading already accounts for your motion, so the two lines can never disagree. Below the GPS noise floor the monitor reports nothing at all rather than inventing a heading — a fabricated course on a 20 km lever arm is enough to claim you are driving into a storm.

The header is the **position's name**, not the configured place: it says where these distances were measured from, which is what you need to know when several phones are monitored.

###### With no usable position, it freezes

There is **no fallback**. When the fix is missing, too old or too imprecise — or the position was deleted — the monitor does not know where it is and therefore perceives nothing.

That is the same blindness it already handles when the strike feed drops, and it is handled the same way rather than with new state: no alerts, and **no all-clear**. A monitor that cannot tell "nothing is happening" from "I cannot see" would otherwise cheerfully report that the storm has cleared. The freeze is silent and lifts by itself when the position returns; the CBDR history is dropped at that point, since the bearings from before the blackout were measured wherever you were then.

While a storm is in progress the age budget is **tripled**, so losing GPS in a tunnel does not blind the monitor mid-event: the last known position is still the best evidence available.

Blitzortung topics are geohash cells (~110 km each) derived from the origin, so the subscription is rebuilt only when you travel far enough to change the cell set — in practice almost never. Without it the monitor would quietly stop hearing the sky you moved into while still reporting itself healthy. The strike buffer survives a re-aim: absolute coordinates stay true wherever you go. A monitor following a position connects on its **first fix** rather than at start-up, since until then it has nothing to derive topics from.

##### The radar

Each ring message carries a polar plot: rings and sectors *are* the model, so the picture is a literal photograph of the monitor's state — strikes coloured and sized by age (so the direction of travel is visible at a glance), the dominant sector highlighted, the front marked, north up. It renders in ~45 ms on a worker thread, and any failure degrades to text-only: a picture must never be able to stop an alert. Image and text arrive as **one** Telegram message, so a ring alert notifies once, not twice.

**Alert format — ring:**

```
⚡ Temporale più vicino — Bacoli
📍 Fronte a 18 km a NO (307°)
🎯 Anello 2/4 · entro 20 km
🧭 Rotta costante: ti arriva addosso
⏱️ Da 27 a 18 km in 9 min
🔢 22 fulmini in 10 min (settore NO)
🕐 14:29
```

**Alert format — all clear:**

```
✅ Temporale cessato — Bacoli
🔇 Nessuna attività entro 30 km da 10 min
📉 Massimo avvicinamento: 5 km (anello 4/4) alle 14:42
🕐 15:05
```

**Example configuration:**

| Field | Value |
|-------|-------|
| Name | Bacoli Temporali |
| Type | 🌩️ Storm front / CBDR |
| Location | Bacoli (auto-resolves to 40.7961, 14.0820) |
| Radius | 30 km |
| Updates per storm | 4 |
| Radar | on |
| Language | 🇮🇹 Italiano |

**Duplicating a live monitor:** click **⎘ Copy** to create a copy named `Copy of <name>`. The duplicate is disabled by default, with all fields copied. Useful for monitoring multiple locations.

#### Football Betting

Polls [football-betting-odds1.p.rapidapi.com](https://rapidapi.com/fluis.lacasse/api/football-betting-odds1) every 5 minutes — always at clock-aligned boundaries (:00, :05, :10, :15 …) regardless of when DRADIS started. Sends a Telegram alert when a statistically favourable signal is detected in a live match. Requires `rapidapi_football_key` in the Configuration tab.

**Alert conditions (all must be true):**
1. Match is in the **2nd half** (`periodID == "3"`)
2. Match minute falls inside one of the two enabled **minute windows** (defaults: 55′–65′ and 75′–81′; both bounds are settable per monitor and exclusive, so 55–65 covers the 56th to the 64th minute)
3. **Goal difference == 1** (exactly one team ahead)
4. The **losing team's next-goal odds are lower** than the winning team's — a market signal that the losing team is expected to equalise
5. The losing team's next-goal odds are **below that window's maximum odds**. Each window carries its own maximum; `0` means no maximum, and then condition 4 alone decides.

**Provider fallback:** the API is queried via `provider1` → `provider2` → `provider3` → `provider4`; the first successful response wins.

**Alert message:**
```
⚽ SEGNALE SCOMMESSA LIVE

🏆 Ethiopia - Premier League
Negele Arsi Ketema vs Hawassa Kenema SC
1-0  ⏱ 57'
```

**Configuration fields:**

| Field | Description |
|-------|-------------|
| First window | Checkbox + start / end minute (default 55 → 65) + its own maximum odds (default `2.0`). |
| Second window | Checkbox + start / end minute (default 75 → 81) + its own maximum odds (default `0`, i.e. no maximum — what this window did before the cap became per-window). |
| Maximum odds | Per window. Alert only when the losing team's next-goal odds are below this value; `0` disables the check for that window. The 🔍 Test API table honours the same rule, computed by the monitor's own `_window_specs`. Unchecking a window disables its three fields. |
| API pause | Time range during which API calls are suppressed (default 23:00–07:00). Leave blank to disable. |

**🔍 Test API button:** fetches all current live matches and renders them in a table with columns: minute, league, home, away, score, next-goal odds (home / away), and a 🔔 signal flag. Matches that meet all alert conditions are highlighted in green; matches in a window with 1-goal difference but without the odds signal are highlighted in yellow.

**Deduplication:** one alert is sent per match per window. The alert key is `match_id:early` / `match_id:late` — the window *id*, not its minutes, so moving a window does not change what has already been alerted. It is pruned automatically when the match leaves the live feed — a new alert fires if the same match re-enters a window.

**More options coming soon:** configurable goal-difference threshold and league filtering are planned for upcoming releases.

**Example configuration:**

| Field | Value |
|-------|-------|
| Name | Football Betting |
| Type | ⚽ Football Betting (RapidAPI) |
| First window | ✅ 55′ → 65′, max odds 2.0 |
| Second window | ✅ 75′ → 81′, max odds 2.5 |
| API pause | 23:00 – 07:00 |

---

#### Rain front

The twin of the storm front, with the national weather radar in place of the lightning network. The storm front answers *"is there lightning coming"*; this answers *"is there rain coming, and will it and I actually meet"*.

##### The source

The Dipartimento della Protezione Civile publishes an Open Access composite of the 24 national radars. **No API key, no registration, no quota** — and one file covers the whole country, so a second monitor watching a different town costs nothing extra. The feed is a shared singleton, reference-counted by its consumers: it runs while at least one rain front monitor is enabled and stops with the last one.

| | |
|---|---|
| Product | `SRI` — surface rainfall intensity, **float32 already in mm/h** |
| Grid | 1200 × 1400 px at 1 km, Transverse Mercator on a sphere (12.5°E / 42.0°N) |
| Cadence | 5 minutes |
| Coverage | Italy only — see *Test radar coverage* below |
| Optional | `POH` — probability of hail, fetched only when **Also mention hail** is on |

The geotransform is read from each file's own GeoTIFF tags rather than hardcoded. If DPC ever re-grids the product the monitor notices instead of silently placing every measurement a few kilometres from where it belongs.

##### The radar is ten minutes behind

Products carry a nominal time and become available **about ten minutes later**. This is a property of the source, measured against the live service, and it shapes two things:

- **Every message states the time of the measurement**, not the time of sending: `📡 Radar delle 17:00 (10 min fa)`. A picture that implies *now* is a promise the source cannot keep, and it falls apart the first time you look out of the window.
- **When the drift is measurable the geometry is advected forward** to compensate. Rain moving at 60 km/h has travelled 10 km since it was seen; the monitor corrects for exactly that before measuring distances.

Polling is scheduled from the cadence the API itself declares, and the lag is learned per product, so each cycle costs roughly one small metadata request plus one download.

##### The front

Every 60 seconds — not every 5 minutes, because *you* may be moving — the newest raster is binned into the same **rings × 12 sectors of 30°** grid the storm front uses, and the whole ring ladder, hysteresis, two-poll confirmation, event lifecycle and bounded-message invariant are inherited unchanged.

One thing could **not** be inherited: the front estimator. The storm front takes the 15th percentile of the distances in a sector, which is the right answer for lightning — discharges are sparse and individually mislocated, so a low quantile robustly finds their leading edge. A radar sector is not sparse but *filled*, and in a filled sector the distance distribution has density proportional to `r`, so the 15th percentile sits at roughly 0.39 of the outer radius **no matter where the edge actually is**. Measured on a real product over Arezzo: the nearest rain was 3.7 km away and the quantile reported 14.7 km — it would have placed the front outside the innermost ring while you were getting wet.

For a field the robust leading edge is the **5th nearest wet pixel**: immune to isolated speckle, and within about a kilometre of the true edge at every site tested.

##### It measures the encounter instead of inferring it

This is what a radar field offers that a scatter of discharges cannot: **its own velocity**, recovered by phase correlation between two consecutive rasters. Your velocity is already known from the position source. With both vectors in hand the question *"will it hit me"* is closed-form rather than inferred:

```
v_rel = v_rain − v_you
t     = −(r · v_rel) / |v_rel|²      → minutes to closest approach
miss  = |r + v_rel · t|              → by how much it misses
```

So the alert can say `🧭 Rotta d'incontro: ti raggiunge fra 26 min` or `🧭 Ti sfiora: passa a N a 14 km, fra 21 min` — times and distances that were measured, not forecast.

**It refuses to invent one.** The correlation is gated on peak-to-sidelobe ratio: the peak divided by the best rival outside a small exclusion box. Calibrated against real products at four sites, every physically absurd reading scored 1.0–1.9 and every reading that agreed across box sizes and baselines scored 3.1–6.5, so the threshold sits in an empty gap. On scattered summer convection the gate **refuses often** — that is the honest answer, not a degraded mode, and it is why the inherited CBDR verdict remains as the fallback. CBDR needs no velocity at all: it reads the rotation of a relative bearing, which a moving observer supplies on its own. When neither can answer, the alert reports distance and bearing and nothing more.

**"You are under the rain" is measured, not inferred from the ring.** The innermost ring is a fifth of the radius — 4 km at a 20 km radius — so reaching it means the front is close, not that anything is falling on you. Each alert therefore reads `peak_in_disc(grid, effective, 2 km)`, the strongest intensity within 2 km of the fix, and the `🔵 Pioggia su di te` heading and the `🧭 Sei sotto la pioggia` line require it to reach the monitor's own `min_mmh` — the same bar the rest of the message uses, so the two halves cannot disagree. Below it the alert keeps its ring and its distance and stops claiming the part it cannot see. `None`, meaning no coverage over the observer, is neither a claim nor a denial. Two kilometres rather than the single pixel because the picture is 10–15 minutes old and is only advected when the drift cleared its gate, which at drizzle intensities it usually does not.

**Drizzle says what drizzle is worth.** Below `DRIZZLE_MMH` (0.5 mm/h, the first edge of the intensity table) the alert adds one line: at that intensity the return evaporates before the ground more often than not, and near a coast it competes with sea clutter and anomalous propagation. A dry pavement under a correct radar picture is a thing the message now explains rather than leaving the user to discover.

**And it will not resolve what the raster cannot.** The composite is 1 km per pixel and its products are 5 minutes apart, so one pixel of displacement is 12 km/h. Below **half a pixel per frame** the correlation peak sat where it started and the bearing comes entirely from the sub-pixel parabolic fit, which returns a direction for noise as readily as for a measurement. `field_motion` therefore reports a confidently-measured standstill there — the same answer it already gave for an exactly-zero shift — and the floor is derived from `pixel_m` and the frame interval rather than fixed in km/h. A "3 km/h" drift with a compass bearing on it, printed under a front that was closing, is the reading this removes.

**A drift that points away is tied to the approach.** When the motion is genuinely resolved, points away, and the ring is nonetheless descending, both facts hold: a stratiform mass can drift one way while its leading edge advances by growth. The ring descent is measured against the observer and the drift is not, so the verdict stays with CBDR and only the drift line changes, to `🌬️ Il grosso della pioggia deriva verso NO a 20 km/h, ma il fronte continua ad avvicinarsi`. Two lines that cannot both be read as standalone facts are never presented as if they could.

##### Three ways it can go blind

An unusable position, a raster older than 25 minutes, or a disc the radar network cannot see into. All three are the same problem the storm front already solves — *not knowing* is not the same as *nothing happening* — and all three are handled the same way: no alerts, and **no all-clear**. `-9999` in the product means "outside coverage", never "no rain"; confusing the two would turn the edge of the network into a permanent dry spell.

##### Settings

| Field | Description |
|-------|-------------|
| Where to watch / Location | Identical to the storm front: a named position it follows, or fixed coordinates. |
| Alert radius | 10–60 km. The disc is observed out to 1.6× that. |
| Updates per event | Hard cap of 2, 3 or 4 ring messages per event, plus one all-clear. |
| **Rain worth telling you about** | Minimum intensity in mm/h: `0.2` even drizzle, **`1` proper rain (recommended)**, `4` a real shower, `10` heavy rain only. The radar sees down to a damp mist; set this too low and a grey afternoon keeps the event open for hours. |
| **Also mention hail** | Fetches the probability-of-hail product too and adds a line when the approaching front carries a real risk. One extra download every 5 minutes. |
| Radar picture | Attaches the actual radar crop to each ring message. |

**📡 Test radar coverage:** fetches the newest product on demand and reports whether the service is reachable, how late the product was published, what share of the watched disc the network can actually see, and the current intensity at your point. The coverage figure is the load-bearing one — a monitor watching a blind spot would report permanent calm. Points such as Pantelleria are genuinely outside the network and are reported as such.

##### The picture

Where the storm front draws a polar scatter of discharges — a photograph of the *model*, because lightning has no image of its own — the rain front attaches **the measurement itself**: the composite cropped to the observation disc, coloured in mm/h, with the ring ladder, the dominant sector, the front marker, an arrow for the measured drift and a cross at the closest point of approach. The crop is centred on the advected origin, the same one the numbers were computed against, so picture and text always agree. Rendering runs on a worker thread and any failure degrades to text — a picture must never be able to stop an alert.

**Alert format — ring:**

```
🌧️ Pioggia nel raggio — Casa
📍 Fronte a 26 km a O (270°)
🌧️ Intensità massima 18.0 mm/h (forte)
🎯 Anello 1/4 · entro 30 km
🧭 Rotta d'incontro: ti raggiunge fra 26 min
🌬️ La pioggia si muove verso E a 60 km/h
🔢 241 km² di pioggia nel settore O
📡 Radar delle 17:00 (10 min fa)
🕐 17:10
```

**Alert format — all clear:**

```
✅ Pioggia cessata — Casa
🔇 Niente pioggia entro 30 km da 10 min
📉 Massimo avvicinamento: 7 km (anello 3/4) alle 16:40
📡 Radar delle 17:25 (10 min fa)
🕐 17:35
```

**Example configuration:**

| Field | Value |
|-------|-------|
| Name | Rain — home |
| Type | 🌧️ Rain front (Protezione Civile radar) |
| Where to watch | Casa *(or a named position to follow)* |
| Alert radius | 30 km |
| Updates per event | 4 |
| Rain worth telling you about | 1 mm/h |
| Radar picture | ✅ |

---

### HA Monitors

Monitor any Home Assistant entity via MQTT and receive a Telegram alert whenever its state changes. Each monitor has a configurable **alert mode**: **LLM** (DRADIS writes the message using your instructions and its full capabilities) or **Direct Telegram** (immediate fixed-format message, no LLM call). Per-entity cooldown and an optional state filter prevent spam. HA monitors are stored in `/data/ha_monitors.json`.

**Prerequisites:**
- Mosquitto broker add-on (HA Add-on store)
- MQTT integration (HA Devices & Services)
- `mqtt_discoverystream_alt` custom integration installed via HACS

**Quick setup:**

1. Install `mqtt_discoverystream_alt` from HACS and add to `configuration.yaml`:

```yaml
mqtt_discoverystream_alt:
  - base_topic: homeassistant
    publish_attributes: true
    publish_timestamps: true
    publish_retain: true
    republish_time: 1
    publish_discovery: true
    include:
      entities:
        - switch.your_entity_here
```

2. In the DRADIS Web UI go to **Settings → MQTT / Home Assistant**, fill in broker host/port/credentials, set **Statestream prefix** to `homeassistant`, and click **Save**.
3. Expand **HA Monitors** → click `+` → 🔍 **Discover** entities → select **Alert mode** → configure LLM instructions or message template → click **Save**.

**Configuration fields:**

| Field | Description |
|-------|-------------|
| Name | Display name shown in the sidebar. |
| Enabled | Toggle — a green dot in the sidebar shows the monitor is active. |
| Entities | One or more HA entities to watch. Type a domain/entity (e.g. `switch.lights`) or click **🔍 Discover** to browse entities currently publishing to the broker. |
| State filter | Optional comma-separated list of states that trigger an alert (e.g. `on, off`). Leave blank to alert on any state change. |
| Alert mode | **LLM** — DRADIS processes the state change with your instructions. **Direct Telegram** — sends a fixed-format message immediately, no LLM call. |
| DRADIS Instructions | *(LLM mode only)* What DRADIS should do when the state changes. Examples: *"Send a Telegram message warning the switch is off."* / *"Send an email with subject 'Sensor alert'."* |
| Message template | *(Direct mode only)* Fixed message text sent to Telegram. Supports placeholders: `{entity}`, `{state}`, `{previous_state}`, `{time}`. |
| Alert language | Language of the alert: 🇮🇹 Italiano or 🇬🇧 English. |
| Cooldown per entity (minutes) | Minimum time between alerts for the same entity (1–1440 min, default 60). Prevents spam on rapidly toggling sensors. |
| Telegram bot | Bot used to send alerts. Defaults to the DRADIS bot; select any extra bot configured in **Settings → Telegram Bots**. |
| Status badge | Shows 🟢 Running or 🔴 Stopped, fetched live from the backend. |

→ Full setup guide: [Wiki → HA Monitors](https://github.com/procolo75/dradis/wiki/HA-Monitors)

---

## Usage Examples

### Voice appointment
*Requires: Voice sub-agent + Google Calendar*

Send a Telegram voice message:
> 🎙️ *"Add a meeting with Marco on Friday at 3pm"*

DRADIS transcribes the audio via Groq Whisper, interprets the request, creates the event in Google Calendar, and confirms via Telegram.

---

### Car Mode *(no LLM, no token cost)*
> `/car`

Send it before setting off. Every alert, scheduled report and chat answer is rewritten so CarPlay can read it aloud, and charts are left out — a photo notification is announced as "Image", or not read at all.

```
⛈️ Temporale nel raggio — Casa          Temporale nel raggio, Casa.
📍 Fronte a 12 km a O (270°)      →     Fronte a 12 chilometri a ovest (270 gradi).
🎯 Anello 2/4 · entro 20 km             Anello 2 su 4, entro 20 chilometri.
🚗 In movimento a 80 km/h verso NE      In movimento a 80 chilometri orari verso nord-est.
```

Note `O` → *ovest*: spoken, the compass abbreviation for west is the Italian conjunction "or" — invisible on screen, total in the car. Send `/car` again to switch back. See [Settings → Car Mode](#settings--car-mode).

---

### Weather query
> *"What's the weather in Milan tomorrow?"*

DRADIS calls the Weather sub-agent (Open-Meteo, no API key needed) and replies with current conditions and a multi-day forecast including temperature, rain probability, wind, and UV index.

---

### Web search
> *"What are the latest Home Assistant announcements?"*

DRADIS routes the request to the Web Search sub-agent (Tavily), retrieves up to 5 results, and sends a concise summarised answer.

---

### Read a specific URL
> *"Summarise this article: https://www.example.com/article"*

DRADIS calls `read_url` directly — no sub-agent, no extra LLM call — and reads the page with its own model. The content is fetched via Jina Reader, trimmed to fit the model's remaining budget, and summarised. No API key required. If the page cannot be fetched you get a `⚠️` notice saying so, rather than a summary of Jina's error page.

---

### Storm front *(live monitor)*

DRADIS opens a persistent MQTT connection and listens for lightning strikes in real time. Every 60 seconds the last 10 minutes of strikes are binned into concentric rings × 12 sectors, and each sector's leading edge — its *front* — drives a short ladder of alerts. A ring is announced at most once, so **one storm produces at most 4 messages plus an all-clear**, with no periodic re-alerting. Each message says whether the storm is on a collision course or will pass by, decided by CBDR (constant bearing, decreasing range), and carries a polar radar of the situation. No API polling, no cooldown to configure, no LLM.

See [Storm front / CBDR](#storm-front--cbdr) under Live Monitors for the full algorithm.

| Field | Value |
|-------|-------|
| Type | 🌩️ Storm front / CBDR |
| Location | Bacoli (auto-resolves to lat/lon) |
| Radius | 30 km |
| Updates per storm | 4 |
| Language | 🇮🇹 Italiano |

Sample alert (warning):
```
🔴 ALLERTA temporale — Bacoli
📍 In avvicinamento: 12.4 km a NO (315°)
🚀 ~42 km/h — arrivo stimato: 18 min
🔢 Fulmini ultimi 15 min: 76
🕐 14:32
```

Sample alert (all clear):
```
✅ Cessato allarme temporale — Bacoli
🔇 Attività residua a 62 km, in allontanamento
🕐 15:10
```

No API key required. No Google account. Reconnects automatically on disconnect.

---

### Daily thunderstorm risk digest *(scheduled monitor)*

Every morning DRADIS fetches atmospheric instability data for the next 2 days and sends a convective risk summary divided by time band — with no LLM call, no token cost, and deterministic output.

| Field | Value |
|-------|-------|
| Monitor type | ⛈️ Thunderstorm risk (Open-Meteo) |
| Location | your city (e.g. *Bacoli*) |
| Forecast days | 2 |
| Cron | `0 7 * * *` |

The Telegram message shows one line per time band (NIGHT / MORNING / AFTERNOON / EVENING) with TRS score (0.00–1.00) and risk level only (🟢 TRASCURABILE · 🟡 BASSO · 🟡 MODERATO · 🟠 ELEVATO · 🔴 MOLTO ELEVATO), plus the daily peak at the end of each day.

---

### Hourly rain alert *(scheduled monitor)*

Check every hour whether rain is expected in the next 2 hours. No notification is sent when skies are clear — only when precipitation is actually forecast.

| Field | Value |
|-------|-------|
| Monitor type | 🌧️ Rain alert (Open-Meteo) |
| Location | your city (e.g. *Bacoli*) |
| Hours ahead | 2 |
| Cron | `0 * * * *` |

The Telegram message lists each 15-minute slot with the expected precipitation in mm (🔵 rainy / ⚪ dry) and the total at the end.

---

### Civil Protection alert *(scheduled monitor)*

Get the Campania alert bulletin every afternoon — today's and tomorrow's together — but only when at least one zone is yellow or worse on either day. Green days are silent.

| Field | Value |
|-------|-------|
| Monitor type | 🚨 Civil Protection alert (Campania) |
| Alert from level | 🟡 Giallo |
| Cron | `30 14 * * *` |

```
🚨 Allerta Protezione Civile — Campania
🕐 22/08/2026 10:40 (Europe/Rome)

📅 OGGI — dal 21/08/2026 14:00 al 22/08/2026 14:00
Avviso n. 72 del 2026 · emesso 21/08/2026 11:00
🟡 GIALLO — Zona 1 · Piana campana, Napoli, Isole, Area Vesuviana
   ↳ Idrogeologico per temporali
🟢 Verdi: 4, 6, 7, 8

📅 DOMANI — dal 22/08/2026 14:00 al 23/08/2026 14:00
Bollettino non ancora emesso.
```

---

### Daily appointments digest *(scheduled task)*

Every morning DRADIS sends a Telegram message with your Google Calendar events for the day.

| Field | Value |
|-------|-------|
| Cron | `0 8 * * *` |
| Instructions | `Fetch today's calendar events and send a tidy summary to Telegram.` |

*Requires: Google Calendar sub-agent enabled.*

---

### Morning news briefing *(scheduled task)*

Every weekday morning DRADIS searches the web for the latest tech news and delivers a digest.

| Field | Value |
|-------|-------|
| Cron | `0 7 * * 1-5` |
| Instructions | `Search for today's top technology news and send a short summary to Telegram.` |

*Requires: Web Search sub-agent enabled.*

---

### Morning email digest *(scheduled task)*

Every weekday morning DRADIS checks unread emails and sends a summary to Telegram.

| Field | Value |
|-------|-------|
| Cron | `0 8 * * 1-5` |
| Instructions | `Check unread emails and send a brief summary of each (sender, subject, key points) to Telegram.` |

*Requires: Gmail sub-agent enabled.*

---

### Evening inbox summary *(scheduled task)*

At the end of each weekday DRADIS reports any new emails received during the day.

| Field | Value |
|-------|-------|
| Cron | `0 18 * * 1-5` |
| Instructions | `Fetch unread emails from today and send a summary to Telegram. If there are none, just say the inbox is clear.` |

*Requires: Gmail sub-agent enabled.*

---

### Weekly email report *(scheduled task)*

Every Monday morning DRADIS delivers a summary of the previous week's emails.

| Field | Value |
|-------|-------|
| Cron | `0 9 * * 1` |
| Instructions | `Search for emails received in the last 7 days. Summarise the most important ones by sender and topic, and send the report to Telegram.` |

*Requires: Gmail sub-agent enabled.*

---

### Email-to-calendar sync *(scheduled task)*

Every 12 hours DRADIS scans recent emails for deadlines or appointments and creates the corresponding Google Calendar events.

| Field | Value |
|-------|-------|
| Cron | `0 */12 * * *` |
| Instructions | `Read all emails received in the last 12 hours, including those with no subject. Ignore any automated notifications sent by Google Calendar itself. For each email that mentions a deadline, meeting, appointment, or event with a specific date and time, create the corresponding event in Google Calendar. Do not send any summary to Telegram — just create the events silently.` |

*Requires: Gmail sub-agent and Google Calendar sub-agent both enabled.*

---

### Aviation TAF briefing *(scheduled task)*

Every morning DRADIS fetches the Terminal Aerodrome Forecast (TAF) for a configured airport, decodes the encoded meteorological notation, and sends a plain-language summary — wind direction and speed, visibility, cloud ceiling, significant weather phenomena — to Telegram.

| Field | Value |
|-------|-------|
| Cron | `0 6 * * *` |
| Instructions | `Fetch the latest TAF for airport ICAO code LIRN (replace with your airport). Search the web for "TAF LIRN site:aviationweather.gov" or use https://aviationweather.gov/api/data/taf?ids=LIRN&format=json to get the raw forecast. Decode the TAF and send a clear plain-language summary to Telegram: validity period, wind (direction and speed in knots), visibility, significant weather (rain, thunderstorms, fog, snow), and cloud layers (few/scattered/broken/overcast). Highlight any conditions below VFR minimums (visibility < 5 km or ceiling < 1 500 ft).` |

*Requires: Web Search sub-agent enabled.*
*Replace `LIRN` with the ICAO code of your airport (e.g. `EGLL` for London Heathrow, `KJFK` for New York JFK, `LFPG` for Paris CDG).*

---

## Telegram Commands

Type `/` in Telegram to see the full command list with descriptions.

| Command | Description |
|---------|-------------|
| `/info` | Show status and configuration of all agents (provider, model, history, sub-agents) |
| `/menu` | List all available commands |
| `/tasks` | List all enabled tasks as Telegram inline buttons. Tap a button to run the task immediately — DRADIS confirms launch and delivers the result to Telegram. |
| `/monitors` | List enabled scheduled monitors (tap to run immediately) and live monitors (tap to see 🟢 Running / 🟠 Degraded / 🔴 Stopped status). |
| `/rain` | Snapshot of a 🌧️ Rain front monitor: the radar picture it would send right now, plus where it thinks it is. One monitor, straight to the picture; several, inline buttons; `/rain <name>` to pick one directly. Works even on a disabled monitor — the radar image is fetched on demand. **Changes nothing**: it perceives without deciding, so it can never suppress or duplicate a real alert. |
| `/storm` | The same for a 🌩️ Storm front monitor. Lightning can only be buffered while the subscription is up, so a stopped monitor reports its position and configuration and says why it cannot show more. |
| `/car` | Toggle 🚗 **Car Mode** — messages rewritten as plain spoken prose, with no icons, links or charts, so CarPlay can read them aloud. `/car on` and `/car off` set it explicitly, so a dictated command cannot flip it back by accident. State is shown by `/info` and persists across restarts. See [Settings → Car Mode](#settings--car-mode). |
| `/gcalauth` | Start Google Calendar OAuth2 authorization. Send without arguments to use the automatic redirect flow; send `/gcalauth <url>` to manually paste the redirect URL (fallback for HA on a separate device). |
| `/gmailauth` | Start Gmail OAuth2 authorization. Same flow as `/gcalauth` but authorizes Gmail read and send scopes. Send `/gmailauth <url>` as fallback if the automatic redirect fails. |
| `/gtasksauth` | Start Google Tasks OAuth2 authorization. Same flow as `/gcalauth`. Send `/gtasksauth <url>` as fallback if the automatic redirect fails. |
| `/backupauth` | Start Google Drive Backup OAuth2 authorization. Grants `drive.file` scope — DRADIS can only access files it created. After authorization, create a monitor of type ☁️ Google Drive Backup in the Web UI. Send `/backupauth <url>` as fallback if the automatic redirect fails. |
| `/todo` | List all open Google Tasks. Shortcut that calls the Tasks sub-agent directly without going through the DRADIS team routing. |

---

## Conversation History

When **Conversation history** is enabled, the last N exchanges (configurable via **Conversation history depth**) are prepended to each request as context. This buffer is in-memory and resets on restart.

To give DRADIS persistent knowledge about the user (name, preferences, language, etc.), add it directly to the **Agent instructions** field in the Web UI Settings tab.

---

## Agent Label

Every DRADIS response includes an italic footer indicating which agent(s) processed the request:

- `🤖 DRADIS` — standard reply
- `🤖 DRADIS · Web Search` — reply involved the web search sub-agent
- `🤖 DRADIS · Weather` — reply involved the weather sub-agent
- `🤖 DRADIS · Google Calendar` — reply involved the Google Calendar sub-agent
- `🤖 DRADIS · Gmail` — reply involved the Gmail sub-agent
- `🤖 DRADIS · Google Tasks` — reply involved the Google Tasks sub-agent
- Multiple labels are combined, e.g. `🤖 DRADIS · Web Search · Weather`
- For scheduled tasks the task name is appended: `🤖 DRADIS · <task name>`

---

## Persistent Data

All persistent data is stored in the Supervisor `/data/` folder, which survives restarts and app updates.

| File | Content |
|------|---------|
| `/data/options.json` | App configuration managed by HA (API keys, etc.) |
| `/data/dradis_settings.json` | Runtime settings edited from the Web UI |
| `/data/agents.json` | Custom sub-agent configuration (managed from Web UI) |
| `/data/tasks.json` | Scheduled task configuration (managed from Web UI) |
| `/data/monitors.json` | Scheduled monitor configuration (managed from Web UI) |
| `/data/live_monitors.json` | Live monitor configuration (managed from Web UI) |
| `/data/storm_front_state.json` | Storm front event state — restored on startup (if under 30 min old) so a storm in progress does not re-announce rings |
| `/data/rain_front_state.json` | Rain front event state — same rule, so rain in progress does not re-announce rings |
| `/data/ha_monitors.json` | HA monitor configuration (managed from Web UI) |
| `/data/google_calendar_token.json` | Google Calendar OAuth2 token (auto-refreshed) |
| `/data/google_gmail_token.json` | Gmail OAuth2 token (auto-refreshed) |
| `/data/google_tasks_token.json` | Google Tasks OAuth2 token (auto-refreshed) |
| `/data/gdrive_backup_token.json` | Google Drive Backup OAuth2 token — used by the ☁️ Google Drive Backup monitor (auto-refreshed) |
