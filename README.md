# DRADIS Agentic AI for Home Assistant

DRADIS is a Home Assistant add-on that exposes a conversational AI agent controllable via Telegram. The agent is fully configurable from the built-in Web UI and the HA Configuration tab — no code changes required.

## Architecture (v3.0)

DRADIS is **one agent** with a **flat set of tools** — no coordinator, no sub-agents, no orchestration framework. On each message the model is called with the system prompt, the conversation and the selected tool schemas; when it requests a tool the runtime runs the function, feeds the result back, and loops until a plain-text answer is produced. This runs on a thin tool-calling loop over the `openai` SDK, pointed at any OpenAI-compatible provider.

> v3.0 removed the **agno** framework. A probe on Groq's `gpt-oss-120b` measured a raw request with 8 tool schemas at ~800 prompt tokens versus ~8800 through agno — the framework added ~8000 tokens per request, making the 8K free-tier limit unreachable. Removing it was the fix.

## Features

- **Multi-provider LLM**: OpenRouter, OpenAI, GitHub Models, Gemini, Groq — switch provider and model at runtime from the Web UI
- **One model + fallback** — the single agent runs on the main model; on an API error or empty reply it retries once on the configured fallback model and posts `⚠️ fallback triggered — <error>`; if both fail, a clear `❌` message is sent
- **Per-tool selection for tasks** — each task attaches only the tools it needs, keeping the prompt small (key to Groq's 8000 tokens-per-minute limit)
- **Token budget** — a `max_tokens` completion cap and an optional *Log token usage* toggle that appends `🔢 in N · out N` to every chat and task reply
- **Conversation history** with configurable depth
- **Telegram error notifications** — all API failures are reported via Telegram
- **Model speed-test** — ranks models by tok/s, keeps top 5

### Tools

The single agent calls these tools when relevant; each capability is enabled and authenticated independently:

- **Web Search** — query search via Tavily (optional free API key) + URL reading via Jina Reader (free, no key required)
- **Weather** — powered by Open-Meteo (free, no API key); current conditions + up to 16-day forecast
- **Google Calendar** — read, create, and delete events via OAuth2 (optional)
- **Gmail** — read inbox, search, and send emails via OAuth2 (optional)
- **Google Tasks** — manage to-do lists via natural language (create, list, complete, delete, update) via OAuth2 (optional)
- **Voice** — incoming voice messages are transcribed via Groq Whisper, then handled like text (optional; requires Groq API key)

### Automation

- **Scheduled Tasks** — cron-based LLM tasks delivered to Telegram; each task selects exactly which tools it may use (all available, or a specific set), so the prompt stays small
- **Scheduled Monitors** — LLM-free cron-based monitors that fetch data and compute results in Python (zero token cost, deterministic output):
  - ⛈️ **Thunderstorm risk** — multiplicative TRS score (0.0–1.0) from CAPE × LI × CIN via Open-Meteo; auto climate calibration from location country (Mediterranean / Continental / Northern Europe); 5 risk levels per time band
  - 🌧️ **Rain alert** — 15-min precipitation data from Open-Meteo; silent when clear
  - 🌍 **Seismic report** — event statistics from INGV GOSSIP JSON API (Campi Flegrei, Vesuvio, Ischia, Golfo di Napoli)
  - ☁️ **Google Drive Backup** — uploads all sensitive DRADIS config files to a dedicated "DRADIS Backup" Drive folder; `drive.file` scope only (no full Drive access)
- **Live Monitors** — persistent push-based monitors that react to external events in real time:
  - ⚡ **Lightning alert** — persistent MQTT listener; pure-Python DBSCAN clustering on a 15-min sliding window classifies each storm cell as APPROACHING/RETREATING/STATIONARY; zone-based alerts (initial detection, zone crossing, periodic re-alert every 10 min, all-clear after 15 min of silence); multi-storm support; no cron, no LLM
  - 🌍 **Seismic live** — polls INGV GOSSIP JSON API every 60 s; alerts on new events and Automatic→Revised promotions; quiet-hours support
- **HA Monitors** — monitor any Home Assistant entity via MQTT statestream; two alert modes:
  - **LLM**: DRADIS processes the state change with your binding instructions (can send Telegram, email, create tasks, etc.)
  - **Direct Telegram**: fixed-format message, zero LLM cost
  - Per-entity cooldown, state filter, 🔍 Discover entities from broker
  - Requires: Mosquitto broker add-on + MQTT integration + `mqtt_discoverystream_alt` (HACS)

### Web UI

- Vertical left sidebar with collapsible sections: Settings, Tools (Web Search, Weather, Calendar, Gmail, Tasks, URL Fetch, Voice), Tasks, Scheduled Monitors, Live Monitors, HA Monitors
- All settings managed at runtime — no restart required
- Live cron validation with next-fire preview
- Live geocoding for monitors (city name → lat/lon)
- Duplicate any task or monitor with ⎘ Copy
- Test any task or monitor with ▶ immediately

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
> → DRADIS calls the Weather tool and replies with current conditions and a multi-day forecast.

**Web search**
> *"What are the latest Home Assistant announcements?"*
> → DRADIS searches the web via Tavily and sends a summarised answer.

**Read a URL**
> *"Summarise this article: https://www.example.com/article"*
> → DRADIS fetches the page via Jina Reader and analyses the content. No API key required.

**Lightning alert** *(live monitor — no cron, no LLM, no token cost)*
> DRADIS opens a persistent MQTT connection and runs DBSCAN clustering every 2 minutes on a 15-min sliding window buffer. Zone-based alerts fire on initial detection, zone crossings, and periodic re-alerts while approaching.
>
> ```
> 🔴 Storm approaching — Bacoli
> 📍 Distance: 28.3 km to NW (315°)
> 🏷 Zone: Near zone (15–30 km)
> 🚀 ~42 km/h — estimated arrival: 40 min
> 🕐 14:32
> ```
>
> Configure in Web UI → **Live Monitors** → `+` → Type: ⚡ Lightning alert

**Seismic live alert** *(live monitor — INGV GOSSIP)*
> DRADIS polls the INGV seismic API every 60 s for Campi Flegrei, Vesuvio, Ischia, and Golfo di Napoli. Sends an alert on new events and when Automatic events are promoted to Revised. Quiet-hours support.
> Configure in Web UI → **Live Monitors** → `+` → Type: 🌍 Seismic live

**HA sensor alert** *(HA monitor)*
> DRADIS subscribes to selected entities via MQTT. When the state changes, an alert is sent via LLM instructions or a Direct Telegram template.
> Full setup guide: [Wiki → HA Monitors](https://github.com/procolo75/dradis/wiki/HA-Monitors)

**Google Drive Backup** *(scheduled monitor)*
> Send `/backupauth`, complete the OAuth2 flow, then create a monitor of type ☁️ Google Drive Backup. DRADIS uploads all config and token files to a "DRADIS Backup" folder in your Drive every week — only files it created are accessible (`drive.file` scope).
> `Cron: 0 6 * * 1` — Monitor type: ☁️ Google Drive Backup

**Daily thunderstorm risk digest** *(scheduled monitor)*
> Every morning DRADIS fetches CAPE, Lifted Index and CIN from Open-Meteo and computes a multiplicative TRS score (0.0–1.0) per time band. Calibration constants are auto-selected from the location's country. No LLM, no token cost.
>
> ```
> ⛈️ Thunderstorm Monitor — Bacoli
> 📍 40.7967, 14.0735 | Forecast 2 days
>
> 📅 2 June 2026
>   00–06  🟢 NEGLIGIBLE  0.04
>   12–18  🟡 MODERATE  0.44
>   18–24  🟠 HIGH  0.63
> ➤ Peak risk: 🟠 HIGH  (0.63)
> ```
>
> `Cron: 0 7 * * *` — Monitor type: ⛈️ Thunderstorm risk

**Daily seismic report** *(scheduled monitor)*
> Every morning at 8:00 DRADIS sends a statistical report of the last 24 hours of seismic activity (magnitude and depth histograms, event list).
> `Cron: 0 8 * * *` — Monitor type: 🌍 Seismic report

**Hourly rain alert** *(scheduled monitor)*
> Checks every hour whether rain is expected in the next 2 hours. Silent when clear.
> `Cron: 0 * * * *` — Monitor type: 🌧️ Rain alert

**Daily appointments digest** *(scheduled task)*
> `Cron: 0 8 * * *` — Instructions: *"Fetch today's calendar events and send a summary to Telegram."*

**Morning briefing** *(scheduled task)*
> `Cron: 0 7 * * 0-4` — Instructions: *"Search for today's top tech news and send a summary to Telegram."*

**Task management** *(requires Google Tasks)*
> *"Add buy milk and call the doctor"* → Creates two tasks and confirms.
> *"What do I have to do?"* → Lists all open tasks.
> *"Mark task 2 as done"* → Marks it completed.

## Telegram Commands

| Command | Description |
|---------|-------------|
| `/info` | Status and configuration |
| `/menu` | List all available commands |
| `/tasks` | List all tasks (✅ enabled / ⏸ disabled) as inline buttons — tap one to run it immediately |
| `/monitors` | List all scheduled and live monitors — tap a scheduled one to run it; tap a live one for 🟢/🔴 status |
| `/hamonitors` | List all HA monitors with 🟢/🔴 running status — tap one for details |
| `/manage` | Toggle enable/disable for any task, monitor, live monitor, or HA monitor from Telegram |
| `/gcalauth` | Connect Google Calendar (OAuth2) |
| `/gmailauth` | Connect Gmail (OAuth2) |
| `/gtasksauth` | Connect Google Tasks (OAuth2) |
| `/backupauth` | Connect Google Drive for automatic backups (`drive.file` scope only) |

## Documentation

Full documentation is available in the **Documentation** tab of the add-on page in Home Assistant and on the [GitHub Wiki](https://github.com/procolo75/dradis/wiki).
