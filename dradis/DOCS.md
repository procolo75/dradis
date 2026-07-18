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

A capability's tools are available when it is **Enabled** and authenticated. **Chat** gets all available tools; a **task** can select exactly which tools to attach (fewer tools = smaller prompt). `bot/state.py:build_tools(settings, selected)` assembles the list. A capability's *Additional instructions* are appended to the system prompt when any of its tools are attached.

### One model + fallback

The single agent runs on the main model (**Settings → DRADIS**). On an API error or empty reply, `run_dradis()` retries once on the configured **fallback** model/provider and sends a Telegram warning (`⚠️ fallback triggered ✅`); if that also fails, a `❌ Both … failed` notification is sent. Per-capability model/provider/fallback settings are no longer used (v3) — the Web UI hides them with a notice.

### Token budget & observability

`max_tokens` (**Settings → DRADIS → Max completion tokens**, default 2048) caps every reply. Each run logs the exact billed `prompt_tokens`. Enable **Settings → DRADIS → Log token usage** to append `🔢 in N · out N` to every chat and task reply.

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
| `monitors/weather_chart.py` | Weather Charts monitor — LLM-free, fetches hourly Open-Meteo forecasts for up to 5 models, generates one PNG chart per variable and returns `list[bytes]` |
| `live_monitors/lightning.py` | Lightning live monitor — LLM-free, persistent MQTT listener; `LightningLiveMonitor` + `LiveMonitorManager` singleton |
| `live_monitors/ha.py` | HA Monitor — persistent MQTT listener for Home Assistant entity state changes; `HaLiveMonitor` + `HaMonitorManager` singleton |
| `live_monitors/seismic.py` | Seismic live monitor — polls INGV GOSSIP JSON API every 60 s, alerts on new events and state promotions |
| `live_monitors/football.py` | Football Betting live monitor — polls RapidAPI every 5 min (clock-aligned); `FootballLiveMonitor` + `FootballMonitorManager` singleton |
| `web/store.py` | Shared data layer: load/save functions, callback registrations, cron validation, provider helpers, OAuth state |
| `web/models.py` | Pydantic request models for all API endpoints |
| `web/routes/settings.py` | FastAPI routes: settings CRUD, config, server timezone |
| `web/routes/agents.py` | FastAPI routes: agents CRUD, model listing, speed test, voice |
| `web/routes/tasks.py` | FastAPI routes: task CRUD, cron validation, manual run |
| `web/routes/monitors.py` | FastAPI routes: scheduled monitor, live monitor, HA monitor CRUD; geocode; HA test/discover |
| `web/routes/tools.py` | FastAPI routes: Google OAuth callbacks, web search test, weather test |
| `web/routes/bots.py` | FastAPI routes: extra Telegram bot CRUD, test-connection endpoint |
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
| Log token usage | `off` | When on, appends `🔢 in N · out N` (input/output tokens) to every chat and task reply. |
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

`search_web` returns up to 5 results with full page content. `read_url` fetches the page at the given URL and returns its content as markdown (max 8 000 characters). A dedicated synthesis LLM formats the output into a concise answer.

| Field | Default | Description |
|-------|---------|-------------|
| Enabled | `false` | Activate web search delegation. Requires `tavily_api_key` in the Configuration tab for query-based search. URL reading works without any additional key. |
| Test connection | — | Sends a test query to Tavily and reports the result inline. |
| LLM Provider | `openrouter` | Provider for the synthesis LLM (independent from DRADIS). |
| Model | — | Model used to synthesise search results. Click 🔄 to load, ⚡ to speed-test. |
| Fallback Provider | *(blank)* | Provider to use if the primary model call fails. |
| Fallback Model | *(blank)* | Model to retry with on API error. Leave blank to disable fallback. |
| Additional instructions | — | Optional extra instructions appended to the synthesis agent's system prompt. |

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
| Additional instructions | — | Optional extra instructions appended to the synthesis agent's system prompt. |

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
| Additional instructions | — | Optional extra instructions appended to the calendar sub-agent's system prompt. |

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
| Additional instructions | — | Optional extra instructions appended to the Gmail sub-agent's system prompt. |

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
| Additional instructions | — | Optional extra instructions appended to the Google Tasks sub-agent's system prompt. |

The shortcut command `/todo` lists all open tasks directly without going through the DRADIS team routing — zero overhead.

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

### Scheduled Monitors

Scheduled monitors fetch data from external APIs and compute results entirely in Python, then deliver them to your Telegram chat on a cron schedule. By default no LLM is invoked — output is deterministic and costs no tokens. Monitors are stored in `/data/monitors.json`.

Click `+` in the **Scheduled Monitors** sidebar header to create a new monitor. Each monitor has:

| Field | Description |
|-------|-------------|
| Name | Display name shown in the sidebar. |
| Enabled | Toggle — a green dot in the sidebar shows the monitor is active. |
| Monitor type | Type of data source: **⛈️ Thunderstorm risk**, **🌧️ Rain alert**, **📊 Weather Charts** (all Open-Meteo, no API key required), **🌍 Seismic report** (INGV GOSSIP), or **☁️ Google Drive Backup**. |
| Response language | Language of the Telegram report: 🇮🇹 **Italiano** (default) or 🇬🇧 **English**. |
| Location | City name or geographic description (e.g. *Bacoli*, *Naples*, *Rome*). Resolved to coordinates via Open-Meteo geocoding. A live hint shows the resolved name and coordinates as you type. |
| Forecast days | *(Thunderstorm only)* Number of days to fetch (1–7, default 2). |
| Hours ahead | *(Rain alert only)* How many hours ahead to check for rain (1–24, default 2). |
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
| Precipitation | mm | Bar | Always sent (shows 0 if no rain expected) |
| Precipitation Probability | % | Bar | ECMWF IFS + GFS only; always sent |
| Wind Speed 10m | km/h | Line | All models |
| Humidity 2m | % | Line | All models |
| Sea Level Pressure | hPa | Line | All models |
| Cloud Cover | % | Line | All models |
| UV Index | — | Bar | GFS only; suppressed if all-zero |
| Geopotential 500 hPa | m | Line | All models |
| Temperature 850 hPa | °C | Line | All models |

**Chart appearance:** 16×5 inch figure at 150 dpi, dark theme (#111 background), five high-contrast colours (blue / red / green / amber / magenta), 2-px line width. Each chart title includes the variable name, location, forecast days, and generation timestamp.

**Precipitation and precipitation probability** are sent even when all values are zero, so the absence of bars communicates "no rain expected." All other bar-type variables are suppressed if no model returns any non-zero value.

**Configuration fields:**

| Field | Description |
|-------|-------------|
| Location | City name resolved via Open-Meteo geocoding. |
| Forecast days | Number of days to fetch (1–7, default 3). |
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
| Location | City name resolved via Open-Meteo geocoding. |
| Hours ahead | How far ahead to look for rain (1–24, default 2). |
| Language | 🇮🇹 Italiano / 🇬🇧 English. |
| Cron | How often to check (e.g. `0 * * * *` = every hour). |

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
| ⚡ Lightning alert | Location, Radius (km), Language |
| 🌍 Seismic live | Areas, Quiet hours |
| ⚽ Football Betting | Minute windows, Quiet hours (API pause) |

There is no cron field and no "run now" action — the monitor is always-on when enabled.

#### Lightning alert

Subscribes to geohash-based MQTT topics covering the configured location and its 8 neighbouring cells. All incoming strikes within `radius_km` are collected in a **15-minute sliding window buffer**. Every 2 minutes a polling task runs **pure-Python DBSCAN** (eps = 8 km, min_samples = 2) to identify storm cells, then reduces all activity to a **single scalar** — the distance of the *nearest significant cell* — appended to a **30-minute series**. That series (not per-cell centroids) is the single source of truth for the approach trend, velocity and ETA, so it does not reset when DBSCAN re-labels cells. A single **threat state machine** per monitor turns that into one coherent thread per storm episode.

**Threat levels:**

| Level | Meaning |
|-------|---------|
| 🟢 CLEAR | No significant activity, or quiet for ≥ 25 min |
| 🟡 WATCH | Significant activity within 50 km, approach not yet confirmed |
| 🔴 WARNING | Confirmed approach (≥ 2 approaching polls, ≥ 3 strikes) and close (≤ 15 km) or short ETA (≤ 30 min) |

Trend over the series is classified as APPROACHING / RETREATING / STATIONARY / UNKNOWN (last 3 samples, > 0.5 km/sample threshold) and shown in the WATCH message.

**Alert triggers (level-based):**

| Event | Trigger | Icon |
|-------|---------|------|
| Watch | First significant activity within 50 km | 🟡 |
| Warning | Approach confirmed and close / short-ETA | 🔴 |
| Periodic re-alert | Every 10 min while in WARNING | 🔴 |
| De-escalation | Storm weakens (12-min gap) → drops WARNING to WATCH | 🟡 |
| All clear | No significant activity for 25 consecutive minutes | ✅ |

Alerts fire **only on level change** (plus the periodic WARNING re-alert). Hysteresis on both escalation (confirmation polls) and de-escalation (quiet gap) prevents the old clear ↔ approaching flapping.

**Behaviour:**
1. On app startup (or save), if the monitor is enabled: a persistent MQTT task and a 2-minute polling task are created.
2. The MQTT task connects to the broker, subscribes to geohash topics, and fills the strike buffer.
3. Every 2 minutes: `_evaluate` runs DBSCAN, refreshes the nearest-activity series, computes the target threat level, and fires an alert on any level change.
4. The state machine advances **only on a confirmed Telegram send**; a dropped alert is retried on the next poll.
5. On disconnect, waits 15 seconds and reconnects automatically.

**Alert format — watch:**

```
🟡 Storm in the area — Bacoli
📍 Activity at 28.3 km to NW (315°)
📊 Approaching
🔢 Strikes (last 15 min): 9
🕐 14:20
```

**Alert format — warning:**

```
🔴 Storm WARNING — Bacoli
📍 Approaching: 12.0 km to NW (315°)
🚀 ~42 km/h — estimated arrival: 18 min
🔢 Strikes (last 15 min): 24
🕐 14:32
```

**Alert format — all clear:**

```
✅ Storm threat cleared — Bacoli
🔇 No lightning for 25 min
🕐 15:10
```

**Example configuration:**

| Field | Value |
|-------|-------|
| Name | Bacoli Lightning |
| Type | ⚡ Lightning alert |
| Location | Bacoli (auto-resolves to 40.7961, 14.0820) |
| Radius | 50 km |
| Language | 🇮🇹 Italiano |

**Duplicating a live monitor:** click **⎘ Copy** to create a copy named `Copy of <name>`. The duplicate is disabled by default, with all fields copied. Useful for monitoring multiple locations.

#### Football Betting

Polls [football-betting-odds1.p.rapidapi.com](https://rapidapi.com/fluis.lacasse/api/football-betting-odds1) every 5 minutes — always at clock-aligned boundaries (:00, :05, :10, :15 …) regardless of when DRADIS started. Sends a Telegram alert when a statistically favourable signal is detected in a live match. Requires `rapidapi_football_key` in the Configuration tab.

**Alert conditions (all must be true):**
1. Match is in the **2nd half** (`periodID == "3"`)
2. Match minute falls inside a configured **minute window** (default: 55′–65′ and/or 75′–81′)
3. **Goal difference == 1** (exactly one team ahead)
4. The **losing team's next-goal odds are lower** than the winning team's — a market signal that the losing team is expected to equalise

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
| Minute windows | Checkboxes for 55′–65′ and 75′–81′ (both enabled by default). More windows coming in a future release. |
| API pause | Time range during which API calls are suppressed (default 23:00–07:00). Leave blank to disable. |

**🔍 Test API button:** fetches all current live matches and renders them in a table with columns: minute, league, home, away, score, next-goal odds (home / away), and a 🔔 signal flag. Matches that meet all alert conditions are highlighted in green; matches in a window with 1-goal difference but without the odds signal are highlighted in yellow.

**Deduplication:** one alert is sent per match per window. The alert key (`match_id:window`) is pruned automatically when the match leaves the live feed — a new alert fires if the same match re-enters a window.

**More options coming soon:** additional minute windows, configurable goal-difference threshold, minimum-odds filter, and league filtering are planned for upcoming releases.

**Example configuration:**

| Field | Value |
|-------|-------|
| Name | Football Betting |
| Type | ⚽ Football Betting (RapidAPI) |
| Minute windows | 55′–65′ ✅, 75′–81′ ✅ |
| API pause | 23:00 – 07:00 |

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

DRADIS routes the request to the Web Search sub-agent, which calls `read_url` via Jina Reader. The page content is fetched, truncated to 8 000 characters, and synthesised into a concise summary. No API key required.

---

### Lightning alert *(live monitor)*

DRADIS opens a persistent MQTT connection and listens for lightning strike data in real time. Strikes within the configured radius are collected in a 15-minute sliding window; a DBSCAN clustering task fires every 2 minutes to identify storm cells and track each one's approach trajectory. Alerts are zone-based (initial detection, zone crossing, periodic re-alert every 10 min if approaching, all-clear after 15 min of silence) — no manual cooldown, no polling, no LLM.

| Field | Value |
|-------|-------|
| Type | ⚡ Lightning alert |
| Location | Bacoli (auto-resolves to lat/lon) |
| Radius | 50 km |
| Language | 🇮🇹 Italiano |

Sample alert (zone approaching):
```
🔴 Temporale in avvicinamento — Bacoli
📍 Distanza: 28.3 km a NO (315°)
🏷 Zona: Zona vicina (15–30 km)
🚀 ~42 km/h — arrivo stimato: 40 min
🕐 14:32
```

Sample alert (all clear):
```
✅ Temporale dissolto — Bacoli
🔇 Nessun fulmine negli ultimi 15 min
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
| `/monitors` | List enabled scheduled monitors (tap to run immediately) and live monitors (tap to see 🟢 Running / 🔴 Stopped status). |
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
| `/data/ha_monitors.json` | HA monitor configuration (managed from Web UI) |
| `/data/google_calendar_token.json` | Google Calendar OAuth2 token (auto-refreshed) |
| `/data/google_gmail_token.json` | Gmail OAuth2 token (auto-refreshed) |
| `/data/google_tasks_token.json` | Google Tasks OAuth2 token (auto-refreshed) |
| `/data/gdrive_backup_token.json` | Google Drive Backup OAuth2 token — used by the ☁️ Google Drive Backup monitor (auto-refreshed) |
