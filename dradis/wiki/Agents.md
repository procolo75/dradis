# Agents

DRADIS sub-agents are specialist AI assistants that the leader agent delegates to automatically. Each sub-agent can be independently configured with its own LLM provider, model, fallback, and additional instructions.

All sub-agents are configured from the **Web UI → Agents** section.

---

## Web Search

Powered by [Tavily](https://tavily.com) (query-based search) and [Jina Reader](https://jina.ai/reader/) (URL reading).

**Requires**: `tavily_api_key` in the Configuration tab (for query-based search; URL reading is free).

**Tools:**

| Tool | Trigger | Backend |
|------|---------|---------|
| `search_web` | User asks a question that requires current information | Tavily — returns up to 5 results with full page content |
| `read_url` | User provides a specific URL to read or summarise | Jina Reader — fetches and returns page content as markdown (max 8 000 chars) |

**Key settings:**

| Field | Description |
|-------|-------------|
| Enabled | Activate delegation. Requires `tavily_api_key` for query search. |
| Test connection | Sends a test query to Tavily and reports the result. |
| LLM Provider / Model | Synthesis LLM (independent from DRADIS). |
| Fallback Provider / Model | Retry model on API failure. |
| Additional instructions | Appended to the synthesis agent's system prompt. |

---

## Weather

Powered by [Open-Meteo](https://open-meteo.com) — free, no API key required.

**Tool:** `get_weather(location, days)` — fetches current conditions and up to 16 days of forecast; a synthesis LLM formats the data.

**Variables fetched:**

| Resolution | Variables |
|---|---|
| Current | temperature, humidity, precipitation, wind speed & gusts, weather code, cloud cover |
| Hourly | temperature, humidity, dew point, precipitation probability, precipitation, showers, weather code, wind speed & gusts, cloud cover |
| Daily | temp max/min, precipitation sum, weather code, wind speed & gusts max, precipitation probability max |

Hourly data is aggregated into four time bands (night/morning/afternoon/evening) per day to reduce token usage.

> **Thunderstorm risk** is handled by the dedicated Thunderstorm Monitor (see [Monitors](Monitors)) — it fetches CAPE, Lifted Index, CIN, and other convective variables and computes a risk score in Python with no LLM call.

---

## Voice

Powered by [Groq Whisper](https://console.groq.com).

**Requires**: `groq_api_key` in the Configuration tab.

When enabled, DRADIS accepts Telegram voice messages (OGG audio), transcribes them via Groq Whisper, and passes the transcribed text to the main agent.

**Key settings:**

| Field | Default | Description |
|-------|---------|-------------|
| Enabled | `false` | Activate voice transcription. Requires `groq_api_key`. |
| Whisper Model | `whisper-large-v3-turbo` | Click 🔄 to fetch available Whisper models. |
| Language | `it` | ISO 639-1 language code (e.g. `en`, `fr`). |
| Send transcription | `true` | Echo the transcribed text to Telegram as `🎙️ <text>` before the agent reply. |

---

## Google Calendar

**Requires**: `google_client_id` + `google_client_secret` in the Configuration tab, and running `/gcalauth` once to authorise the OAuth flow.

**Tools:**

| Tool | Description |
|------|-------------|
| `get_calendar_events` | Fetches events for the next N days (default 7). Returns each event with its ID. |
| `create_calendar_event` | Creates a new event with title, start/end datetime (ISO 8601), optional description. Defaults to 1-hour duration. |
| `delete_calendar_event` | Deletes an event by ID. DRADIS always calls `get_calendar_events` first to get the ID. |

Token stored at `/data/google_calendar_token.json`. DRADIS sends a Telegram notification when the token needs to be renewed.

---

## Gmail

**Requires**: `google_client_id` + `google_client_secret` in the Configuration tab, and running `/gmailauth` once.

**Tools:**

| Tool | Description |
|------|-------------|
| `get_emails` | Fetches the latest N emails (default 10). Returns sender, subject, date, snippet. |
| `get_unread_emails` | Fetches unread emails only. |
| `search_emails` | Searches Gmail using any Gmail search query (e.g. `from:boss@example.com`). |
| `send_email` | Sends a plain-text email. |

Token stored at `/data/google_gmail_token.json`.

---

## Google Tasks

**Requires**: `google_client_id` + `google_client_secret` in the Configuration tab, and running `/gtasksauth` once.

**Tools:**

| Tool | Description |
|------|-------------|
| `list_tasks` | Fetches all open tasks in the specified list (default `@default`). Returns each task with its ID. |
| `create_task` | Creates a new task with title, optional notes, and optional due date (YYYY-MM-DD). |
| `complete_task` | Marks a task as completed by ID. DRADIS calls `list_tasks` first to get the ID. |
| `delete_task` | Permanently deletes a task by ID. |
| `update_task` | Renames a task or updates its notes by ID. |

Shortcut: `/todo` lists all open tasks directly without going through the DRADIS team routing.

Token stored at `/data/google_tasks_token.json`.
