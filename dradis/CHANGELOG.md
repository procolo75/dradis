# CHANGELOG

## [2.4.0] - 2026-04-25
- **Fix — tzlocal warning at boot**: added `ENV TZ=UTC` to Dockerfile. Alpine has no `/etc/localtime` or `/etc/timezone`, so `tzlocal` (used internally by APScheduler) could not detect the system timezone and emitted a `UserWarning` on every start. Setting the `TZ` environment variable is read first by `tzlocal` and silences the warning. UTC remains the correct default — the user-facing timezone for scheduled tasks is configurable from the Web UI.
- **Fix — tool call loop (Gmail 10+ LLM calls)**: added `tool_call_limit` parameter to `create_agent()` in `agent_core.py`. Without a limit, Agno's tool-use loop could cycle indefinitely when a sub-agent had multiple tools available. Limits applied: `4` for Gmail and Google Calendar (complex multi-step tasks), `2` for Weather and Web Search (single-tool agents). This caps the worst-case LLM calls per sub-agent and prevents runaway token consumption.
- **Web UI — removed Documentation panel**: the inline docs tab has been removed from the Web UI sidebar. Documentation is maintained in `DOCS.md` and the GitHub Wiki.

## [2.3.0] - 2026-04-25
- **Bug fix — `tot:0` display**: `format_metrics` was reading `RunMetrics.total_tokens`, a field agno never writes to (always stays 0). Fixed by computing total as `input_tokens + output_tokens` directly.
- **Performance — pre-fetch + inject pattern**: each request now checks the user message for intent keywords before building the Team. When a match is found (e.g. calendar keywords detected), DRADIS pre-fetches the raw API data in Python (all detected members run in parallel), then injects the data into the member agent's system prompt and removes the fetch tool. This reduces each matched member from **2 LLM calls** (tool-decision + formatting) to **1 LLM call** (formatting only). Members without a keyword match fall back to the tool-based path (no regression).
- **New helpers**: `_prefetch_context(message, settings)` orchestrator; `_extract_weather_location(text)` regex extractor; `_safe_sum(a, b)` for robust integer addition; keyword sets `_WEATHER_KW`, `_WS_KW`, `_GCAL_KW`, `_GMAIL_KW`.
- **Agent file changes**: each agent module now exposes a standalone async fetch function (`fetch_weather`, `fetch_web_search`, `fetch_gcal_events`, `fetch_gmail_inbox`) callable from `main.py`; factory functions accept an optional `prefetched_data` parameter.

## [2.2.0] - 2026-04-24
- **Agent Team architecture**: migrated from single-agent-with-tools to agno `Team` (coordinate mode). When one or more sub-agents are enabled, DRADIS creates a `Team` with each enabled sub-agent as an independent `Agent` member; with no sub-agents the legacy single-agent path is used unchanged.
- **Parallel execution**: with `mode="coordinate"` agno runs multiple members concurrently — e.g. a request needing both weather and calendar triggers both members in parallel instead of sequentially.
- **Scalable architecture**: adding a new member requires only creating a new agent factory file and appending it to `_build_members()` — no changes to `handle_message`, `run_scheduled_task`, metrics, or token tracking.
- **Additional instructions preserved**: each member's system prompt still includes the per-agent `*_instructions` setting from the Web UI exactly as before.
- **Token tracking refactor**: `_add_tokens` is now called per member via `team_response.member_responses` (with `store_member_responses=True`); per-agent token counters in `/tokens` are unchanged.
- **Metrics refactor**: per-member metrics use `RunMetrics.duration` from agno when available; `format_metrics` falls back to manual wall-clock time.
- **Code cleanup**: removed `create_calendar_tools`, `create_gmail_tools`, `create_weather_tool`, `create_web_search_tool` factory functions (replaced by `create_*_agent`); removed internal sub-agent creation inside tool functions; removed `ws_metrics`/`weather_metrics`/`gcal_metrics`/`gmail_metrics` mutable-list pattern.

## [2.1.0] - 2026-04-23
- **Test Task button**: each task in the Web UI now has a "▶ Test Task" button that triggers immediate one-off execution without modifying the cron schedule. The result is delivered to Telegram exactly as a scheduled run would. New backend endpoint `POST /api/tasks/{task_id}/run` dispatches the task asynchronously via `asyncio.create_task`.

## [2.0.0] - 2026-04-23
- **Refactor — agents/ folder**: all sub-agent code extracted from `main.py` into dedicated modules (`agents/gcal.py`, `agents/gmail.py`, `agents/weather.py`, `agents/web_search.py`). `main.py` now contains only Telegram handlers, the cron scheduler, and OAuth flows.
- **New `agent_core.py`**: shared utilities (`create_agent`, provider helpers, `_now_str`, token tracking) moved to a single module imported by both `main.py` and all agent files, eliminating circular imports.
- **Token optimisation — tool docstrings**: removed `*_HIDDEN_INSTRUCTIONS` constants injected into DRADIS's system prompt. Routing logic (trigger phrases, call conditions) moved into each tool's docstring, which agno passes to the LLM as the tool description. Saves ~200–400 tokens per call when all agents are enabled.
- **Docs — Architecture section**: added hub-and-spoke diagram and source-layout table to `DOCS.md`.

## [1.9.0] - 2026-04-21
- **Bug fix — metrics call count**: `_count_tool_calls` was incorrectly excluding assistant messages that contained only a tool call (no text), so the call counter showed 1 instead of 2 when a sub-agent was used. Renamed to `_count_model_calls` and now counts all assistant messages.
- **Bug fix — token display**: `_val_metric` now sums list values (agno can return token counts as a per-step list); previously the raw list was displayed instead of the total.
- **Token counter**: cumulative input/output token tracking per agent (DRADIS, Weather, Web Search, Calendar, Gmail), persisted to `/data/dradis_token_stats.json`. Two new Telegram commands: `/tokens` (show breakdown) and `/tokens_reset` (reset counters).

## [1.8.8] - 2026-04-20
- **Icon fix**: properly cropped icon.png (removed white background, 256×256); replaced inline radar SVG in Web UI sidebar with the new DRADIS AI branded image (base64 PNG)

## [1.8.7] - 2026-04-20
- **Docs & Wiki**: added Gmail scheduled task examples (morning digest, evening inbox summary, weekly report) to DOCS.md, README.md, and GitHub Wiki; fixed missing `google_gmail_token.json` entry in Persistent Data table; updated icon description

## [1.8.6] - 2026-04-20
- **Icon update**: replaced add-on icon with new DRADIS AI branded artwork; added `panel_icon: mdi:radar` to config.yaml for the HA sidebar

## [1.8.5] - 2026-04-20
- **Timezone-aware datetime for all agents**: all sub-agents (Gmail, Calendar, Weather, Web Search) and the main agent now receive the current date **and time** in the configured IANA timezone (e.g. `It is 20 April 2026, 14:35 (Europe/Rome).`) instead of only the date in server local time. Added `_now_str(tz_name)` helper using `zoneinfo`.

## [1.8.4] - 2026-04-20
- **Icon**: added radar-sweep icon (`icon.png`) for the Home Assistant add-on dashboard and a matching inline SVG logo in the Web UI sidebar header

## [1.8.3] - 2026-04-20
- **`/info` command**: Google Calendar and Gmail sections now show Provider and Model when enabled

## [1.8.2] - 2026-04-20
- **Google setup guide**: merged the Calendar and Gmail OAuth2 setup docs into a single unified guide (Part 1: one-time Google Cloud setup covering both APIs; Part 2: per-service auth). Fixes confusion about needing to enable the Gmail API separately. Updated UI panels, inline Documentation panel, and DOCS.md.

## [1.8.1] - 2026-04-20
- **Voice language dropdown**: replaced free-text ISO 639-1 input with a `<select>` covering 40 languages; existing saved values are preserved via standard `<select>.value` assignment

## [1.8.0] - 2026-04-20
- **Gmail**: DRADIS can now read and send emails — ask it to check your inbox, search messages, or send an email directly from Telegram
- Authentication via `/gmailauth` command (same Google Cloud credentials as Calendar, one-time setup)
- New Gmail section in the Web UI with provider, model, and metrics settings

## [1.7.9] - 2026-04-19
- **Docs**: added usage examples to README, DOCS, and GitHub Wiki (voice appointment, weather, web search, scheduled tasks)

## [1.7.8] - 2026-04-19
- **Rename**: add-on display name changed to "DRADIS Agentic AI for Home Assistant" across config.yaml, README, DOCS, and Web UI

## [1.7.7] - 2026-04-19
- **Timezone dropdown**: replaced free-text input with a grouped `<select>` covering ~60 IANA timezones across Europe, Americas, Asia, Africa, and Pacific; legacy free-text values are preserved as a custom option if not found in the list

## [1.7.6] - 2026-04-19
- **Timezone setting for scheduled tasks**: new `timezone` field in Settings → DRADIS (default `UTC`). Accepts any IANA timezone name (e.g. `Europe/Rome`). Applied to `CronTrigger.from_crontab()` in both the scheduler (`main.py`) and the validation endpoint — cron expressions are now interpreted in the configured timezone, not the container's system timezone
- Invalid timezone names are rejected with HTTP 400 on settings save
- `_serverTz` in the UI is updated immediately on settings save so the cron label reflects the new timezone without reloading
- Documentation updated in `DOCS.md` and Web UI Documentation panel

## [1.7.5] - 2026-04-19
- **Fix cron timezone mismatch**: validation endpoint now uses the system local timezone (matching `AsyncIOScheduler` in main.py) instead of hardcoded UTC — `0 22 * * *` on a UTC+2 server correctly fires at 22:00 local, not 22:00 UTC
- **Server timezone in UI**: cron expression label now shows `(server time: UTC+2)` so the user always knows what timezone cron values are interpreted in; `GET /api/server-timezone` endpoint added
- **Next fire time in local time**: validation hint now shows the next fire as a local timestamp, e.g. `✅ Valid — next: 20/04/2026, 22:00:00 (server time UTC+2)`

## [1.7.4] - 2026-04-19
- **Cron validation**: `create_task` and `update_task` now reject invalid cron expressions with HTTP 400 instead of silently ignoring them; `GET /api/tasks/validate-cron?expr=...` endpoint returns `{valid, error, next_fire}`
- **Live cron feedback in UI**: the hint under the cron expression field now shows ✅ Valid + next scheduled fire time (UTC), or ❌ with the exact error — validated on render, on preset selection, and on every keystroke (debounced 400 ms)

## [1.7.3] - 2026-04-19
- **Documentation rewrite**: created `DOCS.md` for the HA Documentation tab (separate from Info tab); web UI Documentation panel rewritten to match exactly, covering all current features: all 5 LLM providers, all 10 config keys, model selection per provider, Web Search, Weather, Voice, Google Calendar (including `delete_calendar_event`), Custom agents, Tasks, all 3 Telegram commands, agent label combinations, conversation history, and persistent data
- `README.md` simplified to a feature overview pointing to the Documentation tab

## [1.7.2] - 2026-04-19
- **Fix agent label**: `gcal_metrics` now receives a `(None, 0)` marker on every calendar tool call (not only when sub-agent runs), so the label correctly shows `🤖 DRADIS · Google Calendar` regardless of which tool was used; `None` entries are skipped when displaying metrics
- **Add `delete_calendar_event` tool**: the agent can now actually delete events instead of hallucinating confirmation; requires the event ID returned by `get_calendar_events`
- `get_calendar_events` now includes the event ID `[id]` in each line so the agent can reference it for deletion
- `GCAL_HIDDEN_INSTRUCTIONS` updated with delete trigger phrases and an explicit rule: never confirm a deletion without calling the tool

## [1.7.1] - 2026-04-19
- **Fix Google Calendar tools not being called**: strengthened `GCAL_HIDDEN_INSTRUCTIONS` with bullet-point trigger phrases in both Italian and English (matching the proven format of Weather/Web Search)
- Added default-duration fallback (1 hour) when the user doesn't specify an end time
- `get_calendar_events` now falls back to raw data if the sub-agent fails instead of propagating the exception to the main agent
- Fixed empty-string model setting: `settings.get("gcal_model", "") or SETTINGS_DEFAULTS[...]` correctly falls back to the default when the value is an empty string
- Replaced deprecated `asyncio.get_event_loop()` with `asyncio.get_running_loop()` inside async tool functions

## [1.7.0] - 2026-04-19
- **Google Calendar — improved OAuth flow**: browser now redirects back to DRADIS automatically after authorization (no URL to copy); FastAPI `/gcalauth/callback` captures the code via `asyncio.Event`; URL-paste fallback still supported for HA on a separate device
- **Google Calendar — sub-agent**: raw Calendar API response is now processed by a configurable LLM sub-agent (same pattern as Weather) before being returned to the main agent
- **Google Calendar — model/provider/metrics**: new settings `gcal_provider`, `gcal_model`, `gcal_instructions`, `gcal_show_metrics`; Web UI panel now has provider dropdown, model selector with 🔄 + ⚡ speed test, instructions textarea, and metrics toggle
- Agent label shows `🤖 DRADIS · Google Calendar` when the calendar sub-agent is invoked
- Web UI setup guide updated to reflect the simplified 10-step flow (step 9 is now automatic)

## [1.6.0] - 2026-04-19
- Added **Google Calendar** integration: DRADIS can read and create events on the user's primary Google Calendar
- Two tools: `get_calendar_events(days_ahead)` and `create_calendar_event(title, start_datetime, end_datetime, description)`
- OAuth2 authentication via `/gcalauth` Telegram command: sends auth URL, user grants access in browser, pastes the redirect URL back — token saved to `/data/google_calendar_token.json` and auto-refreshed
- `google_client_id` (str) and `google_client_secret` (password) added to the add-on Configuration tab
- New Web UI sidebar item **Google Calendar** under Agents: enable toggle, auth status indicator, and setup guide
- `/info` command shows Google Calendar status and auth state
- `google-auth-oauthlib` and `google-api-python-client` added to `requirements.txt`

## [1.5.0] - 2026-04-19
- Added **Scheduled Tasks**: create recurring tasks from the Web UI that DRADIS executes automatically via cron schedule
- Each task has a name, enable toggle, cron expression, and instruction text sent to the main agent
- Agent automatically selects the right tools (Web Search, Weather) based on the instruction content — same logic as regular Telegram messages
- Results are delivered to the configured Telegram chat with a label identifying the task name
- Web UI: new **Tasks** sidebar section with a `+` button to create tasks; each task appears as a sidebar item with an enabled/disabled dot
- Task panel includes: cron preset dropdown (Every minute / Every hour / Daily at 8:00 or 20:00 / Every Monday / Weekdays 9–18) and a free-text cron input with live human-readable description, plus Delete button
- Backend: new endpoints `GET/POST /api/tasks`, `PUT/DELETE /api/tasks/{id}` backed by `/data/tasks.json`
- Scheduler: APScheduler `AsyncIOScheduler` with `CronTrigger.from_crontab()` — jobs reload immediately on any task save/delete without restart
- `apscheduler` added to `requirements.txt`

## [1.4.0] - 2026-04-19
- Added **Voice sub-agent**: DRADIS now handles Telegram voice messages (OGG audio) by transcribing them via the Groq Whisper API before passing the text to the main agent
- Default model: `whisper-large-v3-turbo` (same as in agno-agent reference implementation)
- New settings: `voice_enabled` (default: `false`), `voice_model`, `voice_language` (default: `it`), `voice_send_transcription` (default: `true`), `voice_metrics` (default: `false`)
- **Groq API key required** to enable the Voice agent — enforced at both backend (`PUT /api/settings` returns 400 if key absent) and frontend (Enabled toggle disabled with warning banner when key not configured)
- New Web UI sidebar item **Voice** under Agents: warning banner, Groq connection test, Whisper model dropdown (🔄 loads only Whisper models, separate from LLM model list), language field, send-transcription toggle, metrics toggle
- New backend endpoints: `GET /api/voice-models` (returns only Groq Whisper models), `POST /api/voice-test`
- `_fetch_groq_voice_models()` added to server.py — inverse of `_fetch_groq_models()`: includes only models with `whisper` in the ID
- When `voice_send_transcription=true`, a `🎙️ <transcription>` message is sent before the agent reply
- When `voice_metrics=true`, transcription latency and model ID are sent as a separate metrics message
- Groq SDK call runs in a thread executor to avoid blocking the asyncio event loop
- Temp `.ogg` file is always cleaned up (try/finally) after transcription
- `/info` Telegram command now shows Voice status, model, and language
- `groq` added to `requirements.txt`

## [1.3.0] - 2026-04-19
- Multi-provider LLM support: OpenRouter, OpenAI, GitHub Models, Gemini, Groq
- Each provider has its own API key field in the HA Configuration tab (`openai_api_key`, `github_token`, `gemini_api_key`, `groq_api_key`)
- Provider dropdown in Web UI (DRADIS, Web Search, Weather panels) now dynamically loads models for the selected provider via the new `GET /api/models?provider=` endpoint
- Speed test (`⚡`) works across all providers via new `POST /api/speedtest?provider=` endpoint
- Changing the provider clears the model list and shows a hint to reload
- Settings key `openrouter_model` renamed to `model` (auto-migrated on first load)
- GitHub Models and Gemini use curated hardcoded lists (Gemini's OpenAI-compatible models endpoint returns IDs with `models/` prefix which breaks filtering; static list is more reliable)
- Legacy `/api/openrouter/models` and `/api/openrouter/speedtest` endpoints kept as aliases for backward compatibility
- `create_agent()` now selects the correct API key per provider via `_api_key_for_provider()`

## [1.2.0] - 2026-04-19
- Full English translation: all Python function/variable names, settings keys, sidebar labels, HTML element IDs, and JS code now in English
- Settings key renames (persisted JSON): `istruzioni_agente`→`agent_instructions`, `mostra_metriche`→`show_metrics`, `memoria_attiva`→`history_enabled`, `num_conversazioni`→`history_depth`, `messaggio_avvio`→`startup_message`, `ws_abilitato`→`ws_enabled`, `ws_modello`→`ws_model`, `ws_istruzioni`→`ws_instructions`, `ws_mostra_metriche`→`ws_show_metrics`, `meteo_*`→`weather_*`
- AgentPayload field renames: `modello`→`model`, `istruzioni`→`instructions`, `attivo`→`active`
- "Meteo" sub-agent renamed to "Weather" throughout (UI, code, docs)
- Documentation panel updated: removed stale `/openrouter_model_test` and `/dradis_model_set` commands (removed in v0.7.0), added API key acquisition steps with links
- README updated: sidebar section names translated, API key guide added
- Open-Meteo geocoding language parameter changed from `"it"` to `"en"`

## [1.1.0] - 2026-04-19
- Added **Weather sub-agent** powered by Open-Meteo (free, no API key required)
- DRADIS automatically calls `get_weather` when the user asks about weather, forecasts, temperature, rain, or wind
- Geocoding via Open-Meteo geocoding API (city name → lat/lon); 3-day forecast with current conditions
- New sidebar item **Weather** in Web UI (same pattern as Web Search): enabled toggle, connection test, LLM provider/model selector with 🔄 load and ⚡ speed test, additional instructions, show metrics toggle
- New settings keys: `weather_enabled`, `weather_provider`, `weather_model`, `weather_instructions`, `weather_show_metrics`
- New backend endpoint `GET /api/meteo-test` for connection validation
- `/info` Telegram command now shows Weather status
- Response label includes `🌤 Weather` when the weather tool is invoked

## [1.0.0] - 2026-04-19
- Fully redesigned Web UI: replaced horizontal tab bar with a vertical left sidebar (Open WebUI style)
- Applied Home Assistant dark theme: primary background `#111111`, cards `#1c1c1c`, blue accent `#03a9f4`
- Sidebar with three sections: **Settings** (DRADIS config), **Agents** (Web Search + custom agents from `agents.json`), **Other** (Documentation)
- Removed the "Sub-agents" tab: each agent is now a direct sidebar item with a green/grey dot indicating its active state
- Web Search dot in sidebar updates automatically on save and on settings load
- All existing logic (forms, API calls, speed test, save) unchanged

## [0.9.3] - 2026-04-18
- Fixed web search hallucination: removed `topic="news"` and `days=30` (too restrictive, caused empty results on general queries); added early return with explicit "no results" message when Tavily returns nothing; reinforced synthesis prompt with strict instruction to use only retrieved content

## [0.9.2] - 2026-04-18
- Fixed bug: saving Settings tab was overwriting Web Search settings with defaults. The save payload now merges with current server settings, preserving all `ws_*` fields.

## [0.9.1] - 2026-04-18
- Telegram `/help` command renamed to `/info`; now shows status of all agents: DRADIS (provider, model, metrics, history), Web Search (enabled/disabled, model), and any configured sub-agents (active status, model)
- Web Search tab: added ⚡ speed test button (same behaviour as Settings tab — enabled after loading models, sorted fastest first with tok/s tags)

## [0.9.0] - 2026-04-18
- Every response now includes a `🤖 Agents: DRADIS` label (or `🤖 Agents: DRADIS · Web Search` when web search is invoked) appended as italic text at the end of the reply
- Removed persistent user memory: `user_memory.json`, rule-based extraction, `[MEMORY:]` tag mechanism, and `build_system_prompt(memory)` — users configure name/preferences directly in Agent instructions
- Removed Telegram commands `/memory` and `/clear_memory`
- `build_system_prompt()` simplified to inject only current date + agent instructions
- Web UI: toggle label updated from "Memory active" → "Conversation history"; depth label updated from "Conversations in memory" → "Conversation history depth"
- Documentation tab updated to reflect removed memory layer and commands

## [0.8.3] - 2026-04-18
- Inject current date into main system prompt and web search synthesis prompt so the LLM knows today's date and stops framing answers as if it's 2024

## [0.8.2] - 2026-04-18
- Fixed web search returning stale results: Tavily now called with `topic="news"` and `days=30` to prioritise recent content

## [0.8.1] - 2026-04-18
- Fixed metrics: WS metrics now collected via a shared list instead of being sent immediately; `handle_message()` sends a single combined message with labelled sections (🔍 Web Search / 🤖 DRADIS) at the end of each turn

## [0.8.0] - 2026-04-18
- Added Web Search sub-agent powered by Tavily: DRADIS can now delegate web searches to a dedicated sub-agent with its own LLM (provider + model configurable independently)
- New `tavily_api_key` field in add-on Configuration tab (type: password)
- New "Web Search" tab in the Web UI with: enabled toggle, Tavily connection test button, LLM provider/model selector (with 🔄 load), additional instructions textarea, show metrics toggle
- `WS_HIDDEN_INSTRUCTIONS` injected into DRADIS system prompt when web search is enabled — tells the orchestrator when to call `search_web` (not visible in UI)
- Web search metrics sent as a separate Telegram message prefixed with 🔍 (same pattern as DRADIS metrics)
- `create_agent()` now accepts optional `tools` parameter
- New settings keys: `ws_enabled`, `ws_provider`, `ws_model`, `ws_instructions`, `ws_show_metrics`
- New backend endpoint `POST /api/websearch-test` for connection validation

## [0.7.0] - 2026-04-18
- Removed `/openrouter_model_test` and `/dradis_model_set` Telegram commands — these commands had persistent bugs and the feature can be managed entirely from the Web UI
- Removed associated helpers: `_fetch_candidate_models`, `_measure_tok_s`, `_run_speedtest`, `_callback_set_model`, `_last_test_results`
- Removed `InlineKeyboardButton`, `InlineKeyboardMarkup`, `CallbackQueryHandler` imports (no longer needed)
- Cleaned up error messages in `handle_message` that referenced the removed commands

## [0.6.0] - 2026-04-18
- Fixed root cause of apparent `/dradis_model_set` failure: when a newly selected model returns tool-call-only responses (no text), `handle_message` was silently dropping the message — user perceived this as the model not having changed. Now shows an explicit warning with the model name and a hint to switch models.
- Added try/except around `agent.arun()` in `handle_message`: API errors (rate limit, model unavailable, etc.) now produce a visible `❌ Model error` message instead of silently failing.
- Added `print("[DRADIS] Using model: ...")` in `handle_message` so every request logs the active model for debugging.
- Refactored `_callback_set_model`: removed the early `query.answer()` and instead call `query.answer(text=...)` AFTER the save — gives the user immediate Telegram popup feedback ("✅ ModelName") confirming the model was set, even if the subsequent `edit_message_text` fails.
- `_callback_set_model` error path also calls `query.answer(text=..., show_alert=True)` so errors are visible as an alert popup, not just silently logged.

## [0.5.9] - 2026-04-17
- Fixed crash in `update_from_response`: `response.content` is `None` when the model returns a tool-call response with no text — added early `None` guard returning `("", False)` instead of passing `None` to `re.findall`
- Guarded `reply_text` in `handle_message` to skip sending empty messages when content is `None`/empty

## [0.5.8] - 2026-04-17
- Fixed `/dradis_model_set` callback not changing the model: results were stored in a `dict[chat_id → list]` but callback queries don't reliably expose `effective_chat` in all PTB versions — replaced with a simple global `list` (single-user bot, no per-chat key needed)
- Fixed descriptions still saying "≥30B" after lowering the Telegram test threshold to ≥14B (README, inline docs, command description); the Web UI filter remains ≥30B independently
- `save_settings` call is now done before `edit_message_text` so the model is always saved even if the Telegram edit fails

## [0.5.7] - 2026-04-17
- Fixed `/dradis_model_set` callback doing nothing: model names containing HTML special characters (`<`, `>`, `&`) were breaking the HTML reply and Telegram rejected it silently — added `html.escape()` on all model names/IDs before inserting into HTML messages
- Added try/except with logging to `_callback_set_model` so errors are visible in logs instead of being swallowed
- Fixed speed test returning fewer than 5 results: lowered `_SPEEDTEST_MIN_B` from 30B to 14B to match the reference implementation and ensure enough candidates are tested

## [0.5.6] - 2026-04-17
- New Telegram command `/openrouter_model_test`: fetches all free ≥30B tool-calling models from OpenRouter, speed-tests them in parallel (tok/s metric, same logic as Web UI), returns top 5 sorted fastest first
- New Telegram command `/dradis_model_set`: shows inline keyboard buttons with last speed-test results; tapping a button immediately writes the chosen model to `dradis_settings.json`
- Added `CallbackQueryHandler` for `set_model:*` callbacks
- Added `httpx` and `InlineKeyboardButton/InlineKeyboardMarkup` imports to `main.py`
- `COMMANDS` list updated — `/menu` and Telegram autocomplete include the new commands
- Updated README, inline documentation

## [0.5.5] - 2026-04-17
- Fixed model filter: tool-calling check now uses `supported_parameters` contains `"tools"` (was using `architecture.instruct_type` — more accurate)
- Fixed size extraction: uses `architecture.num_parameters` first (actual API field), regex on name+id as fallback
- Fixed free-model check: also accepts models with `:free` suffix (not just `pricing == "0"`)
- Fixed speed test metric: now measures **tok/s** (tokens per second) instead of total latency ms — better reflects actual throughput
- Speed test now uses a real prompt (`max_tokens=60`) instead of "Hi" with 5 tokens — results are more representative
- Speed test returns top 5 models only, sorted by tok/s descending (fastest first)
- Added `_rebuildModelSelect()` helper — active model is always preserved in the dropdown even if not in top-5
- Semaphore reduced to 4 concurrent requests (matches reference implementation)
- Updated README and inline documentation

## [0.5.4] - 2026-04-17
- Model field in Settings tab is now a dropdown instead of a free-text input
- Added 🔄 button: fetches all free ≥30B tool-calling models from OpenRouter API and populates the dropdown
- Added ⚡ button: runs parallel speed tests on all loaded models (max 5 concurrent) and re-sorts the list fastest-first with latency in ms
- New backend endpoints: `GET /api/openrouter/models`, `POST /api/openrouter/speedtest`
- Added `httpx` to requirements
- Updated README and inline documentation

## [0.5.3] - 2026-04-17
- Fixed Telegram commands not updating: removed `post_init` (exceptions there are silently swallowed); commands are now registered explicitly after `start_polling()` with try/except and log confirmation
- Added `[DRADIS] show_metrics=...` log line on every message to diagnose metrics state

## [0.5.2] - 2026-04-17
- **Critical fix**: Web UI API calls were using root-relative paths (`/api/...`) which hit the HA API instead of the add-on backend when accessed via HA Ingress. All fetch calls now use `API_BASE` computed from `window.location.pathname`, so they correctly resolve through the ingress proxy regardless of the access path.

## [0.5.1] - 2026-04-17
- Fixed Web UI form not loading when `/api/agents` or `/api/config` failed: each API call now has its own fallback, settings load independently
- Fixed metrics never showing: removed falsy `response.metrics` guard (free models return `{}`); metrics display now always fires when enabled
- Fixed metric values all showing `?`: `_val_metric()` handles both dict and object forms of `RunResponse.metrics`
- Fixed stale Telegram commands from previous versions: `delete_my_commands()` called before `set_my_commands()` on startup
- Renamed Telegram commands to English: `/memory`, `/clear_memory`
- All user-facing strings translated to English (Telegram responses, conversation context labels, log messages, memory tag `[MEMORY:]`)
- Documentation: added Settings table with field descriptions and default values

## [0.5.0] - 2026-04-17
- HA Configuration tab now holds only API keys and credentials; all runtime settings moved to Web UI
- `read_settings()` reads exclusively from `/data/dradis_settings.json` (no longer falls back to `options.json` for runtime fields)
- Default settings written to `/data/dradis_settings.json` automatically on first start
- Added `provider` field to Web UI settings (saved and applied to agent `base_url`)
- Documentation updated: removed non-key fields from HA Configuration table

## [0.4.0] - 2026-04-17
- Web UI: added DRADIS Settings tab (instructions, model, provider, startup message, memory/metrics toggles, conversation history size)
- Web UI: added Documentation tab with inline reference
- DRADIS settings now editable from Web UI and persisted in `/data/dradis_settings.json` (overrides HA options)
- Fixed startup Telegram message not being sent after 0.3.0 architecture change (moved send after `start_polling`)
- Fixed persistent memory: added `fsync`, error handling, and directory check on write
- Removed unused `lingua` config field
- All documentation rewritten in English

## [0.3.0] - 2026-04-17
- Web UI for sub-agent management (HA Ingress panel, accessible from sidebar)
- Sidebar with agent list, central form with name/provider/model/instructions/active
- REST API FastAPI: full CRUD on /data/agents.json
- Telegram bot and web server run in the same asyncio loop
- Added `lingua` field in config.yaml (values: it, en)

## [0.2.0] - 2026-04-17
- Automatic rule-based fact extraction from user messages (name, city) without LLM dependency
- Memory system prompt made more explicit and imperative for small models
- Removed `/ricorda` and `/dimentica` commands (replaced by automatic extraction)
- Added `/cancella_memoria` command for full reset

## [0.1.9] - 2026-04-17
- Agent instructions configurable from HA Configuration tab (`agent_instructions`)
- Removed hardcoded "Home Assistant" reference from system prompt

## [0.1.8] - 2026-04-17
- API keys and Telegram token moved to HA Configuration tab (type `password`, shown with asterisks)
- Removed `python-dotenv` dependency and `keys.env` file
- Added `openrouter_model` as a configurable field from the UI

## [0.1.7] - 2026-04-17
- Telegram command list: automatic registration via `set_my_commands()` on startup (autocomplete with `/`)
- New `/menu` command listing all available commands

## [0.1.6] - 2026-04-17
- Persistent user memory: JSON in `/data/user_memory.json`
- Agent automatically saves facts with tag `[MEMORY: key=value]`
- New commands: `/memory`, `/remember key=value`, `/forget key`

## [0.1.5] - 2026-04-17
- Improved Telegram output: Markdown → HTML conversion (`**bold**`, `*italic*`, `` `code` ``)

## [0.1.4] - 2026-04-17
- Added `startup_message` option in config.yaml (default: "✅ DRADIS online and ready.")

## [0.1.3] - initial
- Telegram message on add-on startup
