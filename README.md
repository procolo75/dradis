# DRADIS Agentic AI for Home Assistant

DRADIS is a Home Assistant app that exposes a conversational AI agent controllable via Telegram. The agent is fully configurable from the built-in Web UI and the HA Configuration tab — no code changes required.

## Features

- **Branded icon**: custom DRADIS AI icon in the HA app dashboard and sidebar
- **Multi-provider LLM**: OpenRouter, OpenAI, GitHub Models, Gemini, Groq
- **Web Search** sub-agent: query search via Tavily (optional, free API key)
- **URL Fetch** tool: DRADIS fetches URL content via Jina Reader (free, no API key) and analyses it directly — no extra LLM call
- **Weather** sub-agent powered by Open-Meteo (free, no API key)
- **Voice** transcription via Groq Whisper (optional)
- **Google Calendar** — read, create, and delete events via OAuth2 (optional)
- **Gmail** — read inbox, search, and send emails via OAuth2 (optional)
- **Google Tasks** — manage to-do lists via natural language (create, list, complete, delete, update) via OAuth2 (optional)
- **Scheduled Tasks** — cron-based automation delivered to Telegram
- **Scheduled Monitors** — LLM-free cron-based monitors (Thunderstorm risk, Rain alert) that fetch data from Open-Meteo and compute results in Python — no token cost, deterministic output
- **Live Monitors** — persistent push-based monitors that stay connected and react to external events in real time, with no polling and no cron schedule. First type: ⚡ **Lightning alert** via MQTT — sends a Telegram alert on the first strike within a configurable radius after each cooldown period
- **HA Monitors** — monitor Home Assistant entities via `mqtt_statestream`. Select entities from the broker via 🔍 Discover, define LLM instructions, and receive Telegram alerts when state changes. Per-entity cooldown avoids spam. Works anywhere that can reach the MQTT broker — no HA Supervisor API dependency
- **Collapsible sidebar** — all Web UI sidebar sections (Agents, Tools, Tasks, Scheduled Monitors, Live Monitors, HA Monitors) are collapsed by default for a clean overview; click any header to expand
- **Duplicate task / monitor** — copy any task or monitor with the ⎘ button; the copy is created disabled and ready to edit
- **Fallback model** — each agent has a configurable fallback provider and model; when any agent fails (including sub-agents) DRADIS retries with the fallback and notifies which specific models switched; if both fail, a clear `❌` message lists all model names
- **Telegram error notifications** — all API failures are reported via Telegram
- **Model speed-test** — ranks models by tok/s, keeps top 5
- **Conversation history** with configurable depth
- **Token counter** — `/tokens` shows cumulative token usage per agent, broken down by model actually used (primary and fallback tracked separately), with Input / Output / Cache read / Cache write; `/tokens_reset` resets all counters
- All settings managed at runtime from the Web UI — no restart required

## Installation

1. In Home Assistant go to **Settings → Apps → Install App → ⋮ → Repositories**
2. Add: `https://github.com/procolo75/dradis`
3. Find **DRADIS** in the store and click **Install**

## Usage Examples

**Voice appointment** *(requires Voice + Google Calendar)*
> 🎙️ *"Add a meeting with Marco on Friday at 3pm"*
> → DRADIS transcribes the audio, creates the event in Google Calendar, and confirms in Telegram.

**Weather**
> *"What's the weather in Milan tomorrow?"*
> → DRADIS calls the Weather sub-agent and replies with current conditions and a multi-day forecast.

**Web search**
> *"What are the latest Home Assistant announcements?"*
> → DRADIS searches the web via Tavily and sends a summarised answer.

**Read a URL**
> *"Summarise this article: https://www.example.com/article"*
> → DRADIS calls `read_url` directly, fetches the page via Jina Reader, and analyses the content with its own model. No API key required, no extra LLM call.

**Lightning alert** *(live monitor — no cron, no LLM, no token cost)*
> DRADIS opens a persistent MQTT connection and listens for lightning strikes in real time. When a strike is detected within the configured radius (e.g. 50 km from Bacoli), it sends an immediate Telegram alert:
>
> ⚡ **Fulmine rilevato — Bacoli**
> 📍 Distanza: **23.4 km** a SE (138°)
> 🔕 Prossimo alert tra 30 min
> 🕐 14:37
>
> No cron schedule. Reconnects automatically on disconnect.
> Configure in Web UI → **Live Monitors** → `+` → Type: ⚡ Lightning alert

**HA sensor unavailable alert** *(HA monitor — LLM-driven)*
> DRADIS subscribes to selected door/window sensors via MQTT statestream. When any sensor state becomes `unavailable`, the LLM generates a contextual alert:
>
> ⚠️ Il sensore **Porta Cucina** non risponde (`unavailable`).
> Potrebbe avere la batteria scarica o aver perso la connessione Zigbee. Controlla il dispositivo al più presto.
>
> Configure in Web UI → **HA Monitors** → `+` → press 🔍 Discover → select entities → write instructions.
> Requires: Mosquitto add-on, `mqtt_statestream` with `retain: true`.

**Daily thunderstorm risk digest** *(scheduled monitor)*
> Every morning DRADIS fetches atmospheric instability data and sends a risk summary by time band — no LLM, no token cost.
> Cron: `0 7 * * *` — Monitor type: ⛈️ Thunderstorm risk (Open-Meteo)

**Hourly rain alert** *(scheduled monitor)*
> Checks every hour whether rain is expected in the next 2 hours. Silent when clear.
> Cron: `0 * * * *` — Monitor type: 🌧️ Rain alert (Open-Meteo)

**Daily appointments digest** *(scheduled task)*
> Every morning at 8:00, DRADIS automatically sends a Telegram message with your Google Calendar events for the day.
> Cron: `0 8 * * *` — Instructions: *"Fetch today's calendar events and send a summary to Telegram."*

**Morning briefing** *(scheduled task)*
> Cron: `0 7 * * 1-5` (weekdays at 7:00) — Instructions: *"Search for today's top tech news and send a summary to Telegram."*

**Morning email digest** *(scheduled task)*
> Every morning DRADIS checks your unread emails and sends a summary to Telegram.
> Cron: `0 8 * * 1-5` — Instructions: *"Check unread emails and send a brief summary of each to Telegram."*

**Aviation TAF briefing** *(scheduled task, requires Web Search)*
> Every morning DRADIS fetches the Terminal Aerodrome Forecast for your airport, decodes it, and sends a plain-language summary (wind, visibility, ceiling, significant weather) to Telegram.
> Cron: `0 6 * * *` — Instructions: *"Fetch the latest TAF for airport LIRN from aviationweather.gov, decode it, and send a plain-language summary to Telegram."*

**Task management** *(requires Google Tasks)*
> *"Aggiungi comprare latte e chiamare il medico"*
> → DRADIS creates two tasks in your Google Tasks list and confirms.
> *"Cosa ho da fare?"* → Lists all open tasks with IDs.
> *"Segna come fatto il task 2"* → Marks the task as completed.

## Telegram Commands

| Command | Description |
|---------|-------------|
| `/info` | Status and configuration of all agents |
| `/menu` | List all available commands |
| `/tasks` | List enabled tasks as inline buttons — tap one to run it immediately |
| `/monitors` | List scheduled monitors (tap to run) and live monitors (tap to see 🟢/🔴 status) |
| `/tokens` | Show token usage per agent — per model actually used (primary + fallback separate), with Input / Output / Cache read / Cache write / Total; also shows last reset date |
| `/tokens_reset` | Reset all token counters and record timestamp |
| `/gcalauth` | Connect Google Calendar (OAuth2) |
| `/gmailauth` | Connect Gmail (OAuth2) |
| `/gtasksauth` | Connect Google Tasks (OAuth2) |
| `/todo` | List open Google Tasks |

## Documentation

Full documentation is available in the **Documentation** tab of the app page in Home Assistant and on the [GitHub Wiki](https://github.com/procolo75/dradis/wiki).
