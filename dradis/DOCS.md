# DRADIS Agentic AI for Home Assistant — Documentation

DRADIS is a Home Assistant add-on that exposes a conversational AI agent controllable via Telegram. All settings are managed from the built-in Web UI and the HA Configuration tab — no code changes required.

---

## Requirements

- Home Assistant with Supervisor (HAOS or Supervised)
- A Telegram bot (created via [@BotFather](https://t.me/BotFather))
- An API key for at least one supported LLM provider (OpenRouter, OpenAI, GitHub Models, Gemini, or Groq)
- *(Optional)* A [Tavily](https://tavily.com) API key for the Web Search sub-agent
- *(Optional)* A [Groq](https://console.groq.com) API key for the Voice sub-agent (required to enable voice transcription)
- *(Optional)* Google Cloud OAuth2 credentials for the Google Calendar sub-agent

---

## Installation

1. In Home Assistant go to **Settings → Add-ons → Add-on Store → ⋮ → Repositories**
2. Add the repository URL: `https://github.com/procolo75/dradis`
3. Find **DRADIS** in the store and click **Install**
4. Fill in the **Configuration** tab with your credentials
5. Start the add-on

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
| `google_client_id` | str | *(Optional)* Google OAuth2 client ID — required for Google Calendar |
| `google_client_secret` | password | *(Optional)* Google OAuth2 client secret — required for Google Calendar |

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
- **Google Calendar credential** *(optional)*: no Google username or password is stored — only a free OAuth2 credential is needed. Steps:
  1. Go to [console.cloud.google.com](https://console.cloud.google.com) and create or select a project
  2. **APIs & Services → Library** → search *Google Calendar API* → **Enable**
  3. **APIs & Services → OAuth consent screen** → choose **External** → fill in app name (e.g. *DRADIS*) and your email → save
  4. Still in consent screen → **Test users** → add your own Google account email → save
  5. **Credentials → Create credentials → OAuth client ID → Desktop app** → any name → **Create**
  6. Copy the **Client ID** and **Client Secret** from the dialog
  7. Paste them in the add-on Configuration tab (`google_client_id`, `google_client_secret`) and **restart the add-on**
  8. Send `/gcalauth` to the Telegram bot → click the authorization link it replies with
  9. Sign in with your Google account, grant access → **the browser redirects back to DRADIS automatically** ✅
  10. The bot confirms the connection. Enable Google Calendar in the Web UI and save.

  *If the automatic redirect doesn't work (HA on a different device from the browser), copy the full URL from the browser address bar and send it as `/gcalauth <url>`.*

---

## Web UI

After startup, the add-on exposes a web panel accessible directly from the Home Assistant sidebar (via HA Ingress — no external port required).

The UI uses a **vertical left sidebar** with four sections: **Settings**, **Agents**, **Tasks**, and **Other**.

### Settings → DRADIS

Lets you edit all non-sensitive DRADIS settings at runtime without restarting the add-on. Changes are saved to `/data/dradis_settings.json` and take effect immediately on the next message.

| Field | Default | Description |
|-------|---------|-------------|
| Provider | `openrouter` | LLM provider: OpenRouter, OpenAI, GitHub Models, Gemini, or Groq. Select the provider whose API key is configured in the Configuration tab. |
| Model | *(see below)* | Model for the selected provider. Click 🔄 to fetch the available list, then ⚡ to speed-test all models in parallel (measures tok/s) and keep the top 5 sorted fastest first. Changing the provider clears the model list. |
| Agent instructions | `You are DRADIS, a versatile AI assistant.` | System prompt — defines the agent's role, behaviour, and any persistent facts about the user (name, preferences, language, etc.). |
| Startup message | `✅ DRADIS online and ready.` | Telegram message sent when the add-on starts. |
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

Configure the built-in Web Search sub-agent, powered by [Tavily](https://tavily.com). A green dot in the sidebar indicates the agent is active.

When enabled, DRADIS automatically decides when to call `search_web` — no prompt engineering required. Tavily returns up to 5 results; a dedicated synthesis LLM formats them into a concise answer.

| Field | Default | Description |
|-------|---------|-------------|
| Enabled | `false` | Activate web search delegation. Requires `tavily_api_key` in the Configuration tab. |
| Test connection | — | Sends a test query to Tavily and reports the result inline. |
| LLM Provider | `openrouter` | Provider for the synthesis LLM (independent from DRADIS). |
| Model | — | Model used to synthesise search results. Click 🔄 to load, ⚡ to speed-test. |
| Additional instructions | — | Optional extra instructions appended to the synthesis agent's system prompt. |
| Show metrics | `false` | Send a separate 🔍 metrics message after each web search (tokens, latency, model). |

### Agents → Weather

Configure the built-in Weather sub-agent, powered by [Open-Meteo](https://open-meteo.com) (free, no API key required). A green dot in the sidebar indicates the agent is active.

When enabled, DRADIS automatically calls `get_weather` when the user asks about current weather, forecasts, temperature, rain, wind, or UV index — in any language. The tool fetches current conditions and a 3-day forecast; a synthesis LLM formats the data into a clear response.

| Field | Default | Description |
|-------|---------|-------------|
| Enabled | `false` | Activate weather lookup delegation. No API key required. |
| Test connection | — | Pings Open-Meteo and reports the current temperature in Rome as a sanity check. |
| LLM Provider | `openrouter` | Provider for the synthesis LLM (independent from DRADIS). |
| Model | — | Model used to synthesise weather data. Click 🔄 to load, ⚡ to speed-test. |
| Additional instructions | — | Optional extra instructions appended to the synthesis agent's system prompt. |
| Show metrics | `false` | Send a separate 🌤 metrics message after each weather lookup (tokens, latency, model). |

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
| Additional instructions | — | Optional extra instructions appended to the calendar sub-agent's system prompt. |
| Show metrics | `false` | Send a separate 📅 metrics message after each calendar operation (tokens, latency, model). |

### Agents → Custom agents

Each agent defined in `/data/agents.json` appears as a sidebar item. Click to edit its provider, model, instructions, and active toggle. A green dot indicates the agent is active.

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

When a task fires, the agent response is sent to your Telegram chat with a label identifying the task name. The active DRADIS model and all enabled sub-agents are used exactly as for regular messages. Cron jobs reload immediately on save/delete — no add-on restart required.

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

DRADIS calls the Weather sub-agent (Open-Meteo, no API key needed) and replies with current conditions and a 3-day forecast including temperature, rain probability, wind, and UV index.

---

### Web search
> *"What are the latest Home Assistant announcements?"*

DRADIS routes the request to the Web Search sub-agent (Tavily), retrieves up to 5 results, and sends a concise summarised answer.

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

## Telegram Commands

Type `/` in Telegram to see the full command list with descriptions.

| Command | Description |
|---------|-------------|
| `/info` | Show status and configuration of all agents (provider, model, metrics, history, sub-agents) |
| `/menu` | List all available commands |
| `/gcalauth` | Start Google Calendar OAuth2 authorization. Send without arguments to use the automatic redirect flow; send `/gcalauth <url>` to manually paste the redirect URL (fallback for HA on a separate device). |

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
- Multiple labels are combined, e.g. `🤖 DRADIS · Web Search · Weather`
- For scheduled tasks the task name is appended: `🤖 DRADIS · <task name>`

---

## Persistent Data

All persistent data is stored in the Supervisor `/data/` folder, which survives restarts and add-on updates.

| File | Content |
|------|---------|
| `/data/options.json` | Add-on configuration managed by HA (API keys, etc.) |
| `/data/dradis_settings.json` | Runtime settings edited from the Web UI |
| `/data/agents.json` | Custom sub-agent configuration (managed from Web UI) |
| `/data/tasks.json` | Scheduled task configuration (managed from Web UI) |
| `/data/google_calendar_token.json` | Google Calendar OAuth2 token (auto-refreshed) |
