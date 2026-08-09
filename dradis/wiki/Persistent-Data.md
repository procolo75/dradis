# Persistent Data

All persistent data is stored in the Supervisor `/data/` folder, which survives restarts and app updates.

| File | Content |
|------|---------|
| `/data/options.json` | App configuration managed by HA (API keys, etc.) |
| `/data/dradis_settings.json` | Runtime settings edited from the Web UI |
| `/data/agents.json` | Legacy custom-agent config (unused in v3) |
| `/data/tasks.json` | Scheduled task configuration (managed from Web UI) |
| `/data/monitors.json` | Scheduled monitor configuration (managed from Web UI) |
| `/data/live_monitors.json` | Live monitor configuration (managed from Web UI) |
| `/data/lightning_state.json` | Lightning monitor threat state — restored on startup so a storm in progress is not lost on restart |
| `/data/lightning_rec/` | Raw strike recordings, only when **Record strikes** is enabled (daily rotation, 7-day retention) |
| `/data/ha_monitors.json` | HA monitor configuration (managed from Web UI) |
| `/data/google_calendar_token.json` | Google Calendar OAuth2 token (auto-refreshed) |
| `/data/google_gmail_token.json` | Gmail OAuth2 token (auto-refreshed) |
| `/data/google_tasks_token.json` | Google Tasks OAuth2 token (auto-refreshed) |

---

## Conversation History

When **Conversation history** is enabled, the last N exchanges (configurable via **Conversation history depth**) are prepended to each request as context. This buffer is in-memory and resets on restart.

To give DRADIS persistent knowledge about the user (name, preferences, language, etc.), add it directly to the **Agent instructions** field in the Web UI Settings tab.

---

## Token usage footer

Replies carry no agent/tool footer in v3. When **Log token usage** is enabled (Settings → DRADIS), each chat and task reply ends with an italic token line:

- `🔢 in 2450 · out 180` — input/output tokens for that turn (summed across all tool rounds) — when **Log token usage** is on
- `🔧 web_search, get_weather` — the tools DRADIS called that turn (deduped) — when **Log tools used** is on

Both toggles are independent; when both are on the two lines stack. When both are off, no footer is shown.
