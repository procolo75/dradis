# DRADIS Agentic AI for Home Assistant — Documentation

DRADIS is a Home Assistant app that exposes a conversational AI agent controllable via Telegram. All settings are managed from the built-in Web UI and the HA Configuration tab — no code changes required.

---

## Icon

DRADIS displays a radar-sweep icon in the Home Assistant app dashboard (`icon.png`) and in the Web UI sidebar header, matching the dark/cyan color scheme.

---

## Architecture

DRADIS uses an **agno Team** design (`coordinate` mode): a DRADIS leader agent orchestrates a team of specialist member agents. When the user sends a message the leader decides which members to invoke, runs them **in parallel**, and synthesises their responses into a single reply. If no sub-agents are enabled, DRADIS falls back to a single-agent path with no overhead.

### Fallback model (v2.5.0 — improved in v2.7.0, v2.8.3–2.8.4)

Each agent (DRADIS, Web Search, Weather, Google Calendar, Gmail, Google Tasks) supports an independent fallback provider and model. When an API call fails, DRADIS:

1. Detects the failure — agno never re-raises model errors; instead it sets `response.status = "ERROR"` and puts the error message in `response.content`. DRADIS checks both `status == "ERROR"` and empty content to catch all failure modes (rate limits, provider errors, context-window exceeded, etc.)
2. Sends a Telegram warning: *"⚠️ Primary model failed — replied via fallback ✅"* if the fallback succeeds
3. Rebuilds the executor (and any sub-agents) using the fallback settings and retries
4. If the fallback also fails, sends a final `❌ Both primary and fallback models failed` Telegram notification with both model names

The logic is centralised in the `_run_with_fallback()` helper, shared by both `handle_message` and `run_scheduled_task`.

Fallback settings are configured from the Web UI. Leaving the fallback model blank disables the feature for that agent.

### Telegram API error notifications (v2.5.0)

All API call failures send a Telegram notification:

- **Primary model failure**: if the primary model returns an error or empty response, a warning is sent before the fallback retry (or the error is surfaced directly if no fallback is configured).
- **Fallback model failure**: a separate ❌ notification is sent if the fallback also fails.
- **Scheduled task failures**: the same logic applies during cron-scheduled task execution.

### Tool call limit (v2.4.0)

Each sub-agent is created with a `tool_call_limit` to prevent runaway tool-use loops: **4** for Gmail, Google Calendar, and Google Tasks (which may need multiple sequential tool calls for complex operations such as list-then-complete or list-then-delete), **2** for Weather and Web Search (single-tool agents). The limit is enforced by agno's `Agent.tool_call_limit` parameter and caps the worst-case LLM calls per sub-agent regardless of model behaviour.

**Routing** is driven by each member's tool docstrings — the team leader LLM decides which members to invoke based on the user message and the tool descriptions. No keyword matching or hidden text is injected.

**Additional instructions**: each member applies its own per-agent `*_instructions` setting from the Web UI, appended to the member's system prompt at runtime.

**Extensibility**: adding a new member requires creating a new `create_X_agent(settings)` factory file and registering it in `_build_members()` in `main.py`. No changes to message handling, metrics, or token tracking are needed.

**Source layout:**

| File | Responsibility |
|------|---------------|
| `main.py` | Telegram bot, message handlers, cron scheduler, OAuth flows, team assembly |
| `agent_core.py` | `create_agent()`, `create_team()`, provider helpers, token tracking |
| `agents/web_search.py` | Web Search member agent — `create_web_search_agent()` |
| `agents/weather.py` | Weather member agent — `fetch_weather()` + `create_weather_agent()` |
| `agents/gmail.py` | Gmail member agent — `create_gmail_agent()` + OAuth token management |
| `agents/gcal.py` | Google Calendar member agent — `create_gcal_agent()` + OAuth token management |
| `agents/gtasks.py` | Google Tasks member agent — `create_gtasks_agent()` + OAuth token management |
| `agents/thunderstorm_monitor.py` | Thunderstorm risk monitor — LLM-free, fetches Open-Meteo instability data, computes risk score in Python |
| `agents/rain_monitor.py` | Rain alert monitor — LLM-free, fetches 15-min precipitation data from Open-Meteo, sends alert only when rain is forecast |

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

  *Each service uses a separate token file — even if Calendar is already connected, run `/gmailauth` and `/gtasksauth` separately for the other services.*

  *If the automatic redirect doesn't work (HA on a different device), copy the full URL from the browser address bar and send it as `/gcalauth <url>`, `/gmailauth <url>`, or `/gtasksauth <url>`.*

---

## Web UI

After startup, the app exposes a web panel accessible directly from the Home Assistant sidebar (via HA Ingress — no external port required).

The UI uses a **vertical left sidebar** with four sections: **Settings**, **Agents**, **Tasks**, and **Other**.

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
| Show metrics | `false` | Send token usage and response time after each reply. |
| Conversation history depth | `2` | Number of past exchanges kept in context (resets on restart). |
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
| Show metrics | `false` | Send a separate 🔍 metrics message after each web search (tokens, latency, model). |

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
| Show metrics | `false` | Send a separate 🌤 metrics message after each weather lookup (tokens, latency, model). |

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
| Show metrics | `false` | Send a separate 🎙️ metrics message with transcription latency and model name. |

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
| Show metrics | `false` | Send a separate 📅 metrics message after each calendar operation (tokens, latency, model). |

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
| Show metrics | `false` | Send a separate 📧 metrics message after each Gmail operation (tokens, latency, model). |

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
| Show metrics | `false` | Send a separate 📝 metrics message after each Tasks operation (tokens, latency, model). |

The shortcut command `/todo` lists all open tasks directly without going through the DRADIS team routing — zero overhead.

### Monitors

Create LLM-free scheduled monitors that fetch data from external APIs and compute results entirely in Python. Monitors run on a cron schedule and deliver results to your Telegram chat. No model is invoked — output is deterministic and costs no tokens. Monitors are stored in `/data/monitors.json`.

Click `+` in the Monitors sidebar header to create a new monitor. Each monitor has:

| Field | Description |
|-------|-------------|
| Name | Display name shown in the sidebar. |
| Enabled | Toggle — a green dot in the sidebar shows the monitor is active. |
| Monitor type | Type of data source: **⛈️ Thunderstorm risk** or **🌧️ Rain alert** (both Open-Meteo, no API key required). |
| Response language | Language of the Telegram report: 🇮🇹 **Italiano** (default) or 🇬🇧 **English**. |
| Location | City name or geographic description (e.g. *Bacoli*, *Naples*, *Rome*). Resolved to coordinates via Open-Meteo geocoding. A live hint shows the resolved name and coordinates as you type. |
| Forecast days | *(Thunderstorm only)* Number of days to fetch (1–7, default 2). |
| Hours ahead | *(Rain alert only)* How many hours ahead to check for rain (1–24, default 2). |
| Schedule preset | Dropdown of common schedules. |
| Cron expression | Raw 5-part cron with live validation and next-fire preview. |

#### Thunderstorm risk monitor

Fetches atmospheric instability data from [Open-Meteo](https://open-meteo.com) (free, no API key required) and computes a risk score for each time band of each forecast day. No LLM is used — all computation happens in Python.

**Variables fetched (hourly):** CAPE, Lifted Index, Convective Inhibition (CIN), Wind Gusts, Precipitation Probability.

**Risk score formula (0–10):**

| Component | Weight | Rationale |
|---|---|---|
| CAPE (normalised to 3000 J/kg) | 35% | Primary energy driver |
| Lifted Index (mapped from +4 to -8 K) | 30% | Atmospheric instability indicator |
| Precipitation probability | 15% | Proxy for moisture and trigger |
| Wind gusts (normalised to 100 km/h) | 10% | Convective intensity indicator |
| CIN inverted (normalised to 200 J/kg) | 10% | High CIN suppresses storms |

**Risk levels:**

| Score | Level |
|---|---|
| 0.0 – 2.5 | 🟢 LOW |
| 2.5 – 5.0 | 🟡 MODERATE |
| 5.0 – 7.5 | 🟠 HIGH |
| 7.5 – 10.0 | 🔴 SEVERE |

The Telegram message shows one line per time band (NIGHT 00–06, MORNING 06–12, AFTERNOON 12–18, EVENING 18–24) with the raw parameter values and the computed risk label, plus a daily maximum at the end of each day.

**Testing a monitor manually:** each monitor form includes a **▶ Test Monitor** button that triggers an immediate execution. The result is delivered to Telegram within seconds.

**Duplicating a monitor:** click **⎘ Copy** in any monitor form to create a copy named `Copy of <name>`. The duplicate is disabled by default, with the same cron, type, location, and all other fields. It is immediately selected in the sidebar and ready to edit.

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
| Instructions | What DRADIS should do at this time — passed directly to the main agent, which automatically selects the right tools (Web Search, Weather, Google Calendar, etc.) as needed. |

When a task fires, the agent response is sent to your Telegram chat with a label identifying the task name. The active DRADIS model and all enabled sub-agents are used exactly as for regular messages. Cron jobs reload immediately on save/delete — no app restart required.

**Testing a task manually:** each task form includes a **▶ Test Task** button. Clicking it triggers an immediate one-off execution of the task without altering the cron schedule. The result is delivered to Telegram exactly as a scheduled run would. This is useful for verifying instructions before enabling a task or debugging an existing one — no need to modify the cron expression to `* * * * *` just for a quick check.

**Duplicating a task:** click **⎘ Copy** in any task form to create a copy named `Copy of <name>`. The duplicate is disabled by default, with the same cron and instructions. It is immediately selected in the sidebar and ready to edit.

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

### Daily thunderstorm risk digest *(monitor)*

Every morning DRADIS fetches atmospheric instability data for the next 2 days and sends a convective risk summary divided by time band — with no LLM call, no token cost, and deterministic output.

| Field | Value |
|-------|-------|
| Monitor type | ⛈️ Thunderstorm risk (Open-Meteo) |
| Location | your city (e.g. *Bacoli*) |
| Forecast days | 2 |
| Cron | `0 7 * * *` |

The Telegram message shows one line per time band (NIGHT / MORNING / AFTERNOON / EVENING) with CAPE, Lifted Index, CIN, wind gusts, precipitation probability, and a risk level (🟢 LOW · 🟡 MODERATE · 🟠 HIGH · 🔴 SEVERE).

---

### Hourly rain alert *(monitor)*

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
| `/info` | Show status and configuration of all agents (provider, model, metrics, history, sub-agents) |
| `/menu` | List all available commands |
| `/tasks` | List all enabled tasks as Telegram inline buttons. Tap a button to run the task immediately — DRADIS confirms launch and delivers the result to Telegram. |
| `/monitors` | List all enabled monitors as Telegram inline buttons. Tap a button to run the monitor immediately — result is delivered to Telegram within seconds. |
| `/tokens` | Show cumulative token usage (input / output / total) broken down by agent: DRADIS, Weather, Web Search, Calendar, Gmail, Google Tasks |
| `/tokens_reset` | Reset all token counters to zero |
| `/gcalauth` | Start Google Calendar OAuth2 authorization. Send without arguments to use the automatic redirect flow; send `/gcalauth <url>` to manually paste the redirect URL (fallback for HA on a separate device). |
| `/gmailauth` | Start Gmail OAuth2 authorization. Same flow as `/gcalauth` but authorizes Gmail read and send scopes. Send `/gmailauth <url>` as fallback if the automatic redirect fails. |
| `/gtasksauth` | Start Google Tasks OAuth2 authorization. Same flow as `/gcalauth`. Send `/gtasksauth <url>` as fallback if the automatic redirect fails. |
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
| `/data/monitors.json` | Monitor configuration (managed from Web UI) |
| `/data/google_calendar_token.json` | Google Calendar OAuth2 token (auto-refreshed) |
| `/data/google_gmail_token.json` | Gmail OAuth2 token (auto-refreshed) |
| `/data/google_tasks_token.json` | Google Tasks OAuth2 token (auto-refreshed) |
| `/data/dradis_token_stats.json` | Cumulative token usage counters (input/output per agent, persisted across restarts) |
