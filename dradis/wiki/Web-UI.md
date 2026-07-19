# Web UI

The DRADIS Web UI is accessible directly from the Home Assistant sidebar via HA Ingress — no external port or network configuration required.

The UI uses a **vertical left sidebar** with collapsible sections: **Settings**, **Tools**, **Tasks**, **Scheduled Monitors**, **Live Monitors**, and **HA Monitors**. All sections except Settings are collapsed by default — click any header to expand it.

> **v3.0:** DRADIS is one agent on the main model. The former per-capability model selectors are gone — capabilities are just tools, enabled and authenticated under **Tools**.

---

## Settings → DRADIS

Runtime settings for the DRADIS agent. Saved to `/data/dradis_settings.json`, effective on the next message (no restart).

| Field | Default | Description |
|-------|---------|-------------|
| Provider | `openrouter` | LLM provider: OpenRouter, OpenAI, GitHub Models, Gemini, Groq. |
| Model | *(see below)* | Main model. Click 🔄 to fetch models, ⚡ to speed-test (tok/s) and keep the top 5. |
| Fallback Provider | *(blank)* | Provider used when the primary call fails. |
| Fallback Model | *(blank)* | Model to retry with on API error. Blank = no fallback. |
| Agent instructions | `You are DRADIS, a versatile AI assistant.` | System prompt — role, behaviour, persistent facts about you. |
| Startup message | `✅ DRADIS online and ready.` | Telegram message sent when the add-on starts. |
| Conversation history | `true` | Prepend the last N exchanges as context. |
| Conversation history depth | `2` | Past exchanges kept in context (resets on restart). |
| Max completion tokens | `2048` | Caps the reply (`max_tokens`) so prompt+reply fit the context window. Keep 2048 for Groq 8K. |
| Log token usage | `off` | When on, appends `🔢 in N · out N` to every chat and task reply. |
| Log tools used | `off` | When on, appends `🔧 tool1, tool2` (the tools DRADIS called that turn) to every chat and task reply. |
| Timezone | `UTC` | Timezone for all cron expressions. |

**Model loading by provider:** OpenRouter 🔄 fetches free ≥30B tool-calling models (⚡ speed-tests, keeps top 5); OpenAI fetches the GPT-4o family; GitHub Models / Gemini use fixed presets; Groq 🔄 fetches its LLM models (Whisper excluded).

---

## Settings → MQTT / Home Assistant

| Field | Default | Description |
|-------|---------|-------------|
| Broker host | `core-mosquitto` | Hostname/IP of the MQTT broker. |
| Port | `1883` | MQTT broker port. |
| Username / Password | *(blank)* | MQTT auth (blank if none). |
| Statestream prefix | `homeassistant` | Base topic prefix — must match `base_topic` in `mqtt_discoverystream_alt`. |

Click **Test connection** to verify the broker is reachable.

---

## Tools

Each capability contributes tools the single agent can call (see [Tools](Tools) for the full tool list). They are enabled and authenticated here; the agent always uses the **main model**. Common fields per capability:

| Field | Description |
|-------|-------------|
| Enabled | Activate the capability (requires its API key / OAuth token). |
| Test connection | Verify the backend is reachable (Web Search, Weather…). |
| Additional instructions | Appended to the system prompt **when one of this capability's tools is attached** to a run. |

**Capabilities:** Web Search (Tavily), Weather (Open-Meteo), Google Calendar, Gmail, Google Tasks, URL Fetch (Jina Reader). Per-capability provider/model selectors no longer exist — a notice in each panel explains this.

**Voice (message transcription)** is separate: it transcribes incoming Telegram voice messages via Groq Whisper, then hands the text to the agent. It keeps its own settings:

| Field | Default | Description |
|-------|---------|-------------|
| Enabled | `false` | Activate voice transcription. Requires `groq_api_key`. |
| Whisper Model | `whisper-large-v3-turbo` | Click 🔄 to fetch Whisper models. |
| Language | `it` | ISO 639-1 transcription language code. |
| Send transcription | `true` | Echo transcribed text to Telegram before the reply. |

---

## Tasks

See [Tasks](Tasks) for full details and examples.

| Action | Description |
|--------|-------------|
| `+` button | Create a new task. |
| Sidebar item | Open the task form. Green/red dot = enabled/disabled. |
| **Tools** | Choose which tools the task may use — *All available tools* or *Selected tools* (grouped by capability). Fewer tools = smaller prompt. |
| **▶ Test Task** | Run immediately without altering the cron schedule. |
| **⎘ Copy** | Duplicate the task (disabled by default). |
| **🗑 Delete** | Remove the task. |

---

## Scheduled Monitors

See [Monitors](Monitors) for full details.

| Action | Description |
|--------|-------------|
| `+` button | Create a new monitor. |
| Sidebar item | Open the form. Green/red dot = enabled/disabled. |
| **▶ Test Monitor** | Trigger immediate execution; result to Telegram. |
| **⎘ Copy** | Duplicate the monitor (disabled by default). |
| **🗑 Delete** | Remove the monitor. |

---

## Live Monitors

See [Live-Monitors](Live-Monitors) for full details.

| Action | Description |
|--------|-------------|
| `+` button | Create a new live monitor. |
| Sidebar item | Open the form. Green/red dot = running/stopped. |
| Status badge | 🟢 Running / 🔴 Stopped — fetched live. |
| **⎘ Copy** | Duplicate the monitor (disabled by default). |
| **🗑 Delete** | Remove the monitor. |

---

## HA Monitors

See [HA-Monitors](HA-Monitors) for full details.

| Action | Description |
|--------|-------------|
| `+` button | Create a new HA monitor. |
| Sidebar item | Open the form. Green/red dot = running/stopped. |
| **🔍 Discover** | Browse entities currently publishing to the MQTT broker. |
| Status badge | 🟢 Running / 🔴 Stopped. |
| **🗑 Delete** | Remove the monitor. |
