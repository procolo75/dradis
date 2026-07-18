# Web UI

The DRADIS Web UI is accessible directly from the Home Assistant sidebar via HA Ingress — no external port or network configuration required.

The UI uses a **vertical left sidebar** with eight collapsible sections: **Settings**, **Agents**, **Tools**, **Tasks**, **Scheduled Monitors**, **Live Monitors**, and **HA Monitors**. All sections except Settings are collapsed by default — click any header to expand it.

---

## Settings → DRADIS

Runtime settings for the main DRADIS agent. Changes are saved to `/data/dradis_settings.json` and take effect on the next message (no restart required).

| Field | Default | Description |
|-------|---------|-------------|
| Provider | `openrouter` | LLM provider: OpenRouter, OpenAI, GitHub Models, Gemini, or Groq. Select the provider whose API key is configured in the Configuration tab. |
| Model | *(see below)* | Model for the selected provider. Click 🔄 to fetch the available list, then ⚡ to speed-test all models in parallel (measures tok/s) and keep the top 5 sorted fastest first. |
| Fallback Provider | *(blank)* | Provider to use when the primary model call fails. |
| Fallback Model | *(blank)* | Model to retry with on API error. Leave blank to disable fallback. |
| Agent instructions | `You are DRADIS, a versatile AI assistant.` | System prompt — defines the agent's role, behaviour, and persistent facts about the user. |
| Startup message | `✅ DRADIS online and ready.` | Telegram message sent when the add-on starts. |
| Conversation history | `true` | Prepend the last N exchanges as context to each request. |
| Conversation history depth | `2` | Number of past exchanges kept in context (resets on restart). |
| Max completion tokens | `2048` | Caps the model reply (`max_tokens`) so prompt+reply fit the model context window. Keep at 2048 for the Groq 8K free tier. |
| Timezone | `UTC` | Timezone used to interpret all cron expressions for tasks and monitors. |

**Model selection by provider:**

| Provider | How models are loaded |
|----------|-----------------------|
| OpenRouter | 🔄 fetches free ≥30B tool-calling models from the API; ⚡ speed-tests in parallel; top 5 fastest kept |
| OpenAI | 🔄 fetches GPT-4o, GPT-4o Mini, GPT-4 Turbo, and other tool-capable models |
| GitHub Models | Fixed preset: GPT-4o, GPT-4o Mini, Phi 3.5 MoE, Phi 3.5 Mini, Llama 3.1 70B, Llama 3.1 8B, Mistral Nemo, Mistral Large |
| Gemini | Fixed preset: Gemini 2.0 Flash, 2.0 Flash Lite, 2.5 Pro Preview, 1.5 Pro, 1.5 Flash, 1.5 Flash 8B |
| Groq | 🔄 fetches LLM models from the Groq API (Whisper models excluded) |

---

## Settings → MQTT / Home Assistant

| Field | Default | Description |
|-------|---------|-------------|
| Broker host | `core-mosquitto` | Hostname or IP of the MQTT broker. |
| Port | `1883` | MQTT broker port. |
| Username | *(blank)* | MQTT username (leave blank if no authentication). |
| Password | *(blank)* | MQTT password. |
| Statestream prefix | `homeassistant` | Base topic prefix — must match `base_topic` in `mqtt_discoverystream_alt` config. |

Click **Save** to apply. Click **Test connection** to verify the broker is reachable.

---

## Agents → Web Search

See [Agents → Web Search](Agents#web-search).

| Field | Default | Description |
|-------|---------|-------------|
| Enabled | `false` | Activate web search delegation. Requires `tavily_api_key`. |
| Test connection | — | Sends a test query to Tavily and shows the result. |
| LLM Provider / Model | — | Synthesis LLM for formatting search results. |
| Fallback Provider / Model | *(blank)* | Retry model on API failure. |
| Additional instructions | — | Appended to the synthesis agent's system prompt. |

---

## Agents → Weather

See [Agents → Weather](Agents#weather).

| Field | Default | Description |
|-------|---------|-------------|
| Enabled | `false` | Activate weather lookup. No API key required. |
| Test connection | — | Pings Open-Meteo and shows current temperature in Rome. |
| LLM Provider / Model | — | Synthesis LLM for formatting weather data. |
| Fallback Provider / Model | *(blank)* | Retry model on API failure. |
| Additional instructions | — | Appended to the synthesis agent's system prompt. |

---

## Agents → Voice

See [Agents → Voice](Agents#voice).

| Field | Default | Description |
|-------|---------|-------------|
| Enabled | `false` | Activate voice transcription. Requires `groq_api_key`. |
| Test connection | — | Verifies the Groq API key can reach the Whisper endpoint. |
| Whisper Model | `whisper-large-v3-turbo` | Click 🔄 to fetch Whisper models. |
| Language | `it` | ISO 639-1 transcription language code. |
| Send transcription | `true` | Echo transcribed text to Telegram before the reply. |

---

## Agents → Google Calendar / Gmail / Google Tasks

Each Google agent panel includes:

| Field | Default | Description |
|-------|---------|-------------|
| Enabled | `false` | Activate. Toggle disabled when credentials are not configured. |
| Authentication status | — | Shows whether the OAuth token is present. |
| LLM Provider / Model | — | Sub-agent model for formatting API responses. |
| Fallback Provider / Model | *(blank)* | Retry model on API failure. |
| Additional instructions | — | Appended to the sub-agent's system prompt. |

---

## Tasks

See [Tasks](Tasks) for full details and examples.

| Action | Description |
|--------|-------------|
| `+` button | Create a new task. |
| Sidebar item | Click to open the task form. Green/red dot = enabled/disabled. |
| **▶ Test Task** | Trigger immediate execution without altering the cron schedule. |
| **⎘ Copy** | Duplicate the task (disabled by default). |
| **🗑 Delete** | Remove the task. |

---

## Scheduled Monitors

See [Monitors](Monitors) for full details.

| Action | Description |
|--------|-------------|
| `+` button | Create a new monitor. |
| Sidebar item | Click to open the monitor form. Green/red dot = enabled/disabled. |
| **▶ Test Monitor** | Trigger immediate execution. Result delivered to Telegram. |
| **⎘ Copy** | Duplicate the monitor (disabled by default). |
| **🗑 Delete** | Remove the monitor. |

---

## Live Monitors

See [Live-Monitors](Live-Monitors) for full details.

| Action | Description |
|--------|-------------|
| `+` button | Create a new live monitor. |
| Sidebar item | Click to open the form. Green/red dot = running/stopped. |
| Status badge | 🟢 Running / 🔴 Stopped — fetched live from the backend. |
| **⎘ Copy** | Duplicate the monitor (disabled by default). |
| **🗑 Delete** | Remove the monitor. |

---

## HA Monitors

See [HA-Monitors](HA-Monitors) for full details.

| Action | Description |
|--------|-------------|
| `+` button | Create a new HA monitor. |
| Sidebar item | Click to open the form. Green/red dot = running/stopped. |
| **🔍 Discover** | Browse entities currently publishing to the MQTT broker. |
| Status badge | 🟢 Running / 🔴 Stopped. |
| **🗑 Delete** | Remove the monitor. |
