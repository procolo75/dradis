# Tools

DRADIS is **one agent** with a flat set of tools (v3.0 — no sub-agents, no coordinator, no framework). The model decides which tool to call; the runtime executes it and feeds the result back. Everything runs on the **main model** configured in **Settings → DRADIS** (with the single fallback model on API error).

Each capability below is enabled and authenticated independently from the **Web UI → Tools** section. A capability's *Additional instructions* are appended to the system prompt **only when one of its tools is attached** to the run. Per-capability model/provider settings no longer exist — the single agent always uses the main model.

For a **task**, you can select exactly which tools to attach (see [Tasks](Tasks)); **chat** gets all available tools.

---

## Web Search

Powered by [Tavily](https://tavily.com) (query-based search) and [Jina Reader](https://jina.ai/reader/) (URL reading).

**Requires**: `tavily_api_key` in the Configuration tab (for query search; URL reading is free).

| Tool | Trigger | Backend |
|------|---------|---------|
| `search_web` | A question that needs current information | Tavily — up to 5 results, content trimmed to ~800 chars each |
| `read_url` | The user provides a specific http/https URL | Jina Reader — page text as markdown (max 8 000 chars) |

**Settings:** Enabled · Test connection · Additional instructions.

---

## Weather

Powered by [Open-Meteo](https://open-meteo.com) — free, no API key required.

**Tool:** `get_weather(location, days)` — current conditions + up to 16 days of forecast. Hourly data is aggregated into four time bands per day to keep the result small.

> **Thunderstorm risk** is handled by the dedicated Thunderstorm Monitor (see [Monitors](Monitors)) — LLM-free, computed in Python.

**Settings:** Enabled · Additional instructions.

---

## Google Calendar

**Requires**: `google_client_id` + `google_client_secret`, and `/gcalauth` once.

| Tool | Description |
|------|-------------|
| `get_calendar_events` | Events for the next N days (default 7), each with its ID. |
| `create_calendar_event` | Create an event (title, start/end ISO 8601, optional description; default 1 h). |
| `delete_calendar_event` | Delete by ID (the agent calls `get_calendar_events` first). |

Token at `/data/google_calendar_token.json`. DRADIS notifies via Telegram when it must be renewed.

**Settings:** Enabled · Additional instructions.

---

## Gmail

**Requires**: `google_client_id` + `google_client_secret`, and `/gmailauth` once.

| Tool | Description |
|------|-------------|
| `get_emails` | Latest N emails (default 10): sender, subject, date, snippet. |
| `get_unread_emails` | Unread emails only. |
| `search_emails` | Gmail query syntax (e.g. `from:boss@example.com`). |
| `send_email` | Send a plain-text email. |

Token at `/data/google_gmail_token.json`.

**Settings:** Enabled · Additional instructions.

---

## Google Tasks

**Requires**: `google_client_id` + `google_client_secret`, and `/gtasksauth` once.

| Tool | Description |
|------|-------------|
| `list_tasks` | Open tasks in a list (default `@default`), each with its ID. |
| `create_task` | Create a task (title, optional notes, optional due YYYY-MM-DD). |
| `complete_task` | Mark completed by ID (the agent calls `list_tasks` first). |
| `delete_task` | Delete permanently by ID. |
| `update_task` | Rename or update notes by ID. |

Token at `/data/google_tasks_token.json`.

**Settings:** Enabled · Additional instructions.

---

## URL Fetch

`read_url` fetches any page's text via Jina Reader (free, no key). Enable it under **Web UI → Tools → URL Fetch**.

---

## Voice (message transcription)

Powered by [Groq Whisper](https://console.groq.com). **Requires**: `groq_api_key`.

Incoming Telegram voice messages (OGG) are transcribed and then handled like text. This is a transcription step, not a tool the agent calls — it keeps its own Whisper model setting.

| Field | Default | Description |
|-------|---------|-------------|
| Enabled | `false` | Activate voice transcription. |
| Whisper Model | `whisper-large-v3-turbo` | Click 🔄 to fetch available Whisper models. |
| Language | `it` | ISO 639-1 code (e.g. `en`, `fr`). |
| Send transcription | `true` | Echo the transcribed text as `🎙️ <text>` before the reply. |
