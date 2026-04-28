# DRADIS Agentic AI for Home Assistant

DRADIS is a Home Assistant app that exposes a conversational AI agent controllable via Telegram. The agent is fully configurable from the built-in Web UI and the HA Configuration tab — no code changes required.

## Features

- **Branded icon**: custom DRADIS AI icon in the HA app dashboard and sidebar
- **Multi-provider LLM**: OpenRouter, OpenAI, GitHub Models, Gemini, Groq
- **Web Search** sub-agent powered by Tavily (optional)
- **Weather** sub-agent powered by Open-Meteo (free, no API key)
- **Voice** transcription via Groq Whisper (optional)
- **Google Calendar** — read, create, and delete events via OAuth2 (optional)
- **Gmail** — read inbox, search, and send emails via OAuth2 (optional)
- **Scheduled Tasks** — cron-based automation delivered to Telegram
- **Monitors** — LLM-free scheduled monitors that fetch data and compute results in Python (no token cost, deterministic output)
- **Fallback model** — each agent has a configurable fallback provider and model; on any API error (rate limit, provider error, empty response) DRADIS automatically retries with the fallback and notifies via Telegram; if both fail, a clear `❌` message lists both model names
- **Telegram error notifications** — all API failures are reported via Telegram
- **Model speed-test** — ranks models by tok/s, keeps top 5
- **Conversation history** with configurable depth
- **Token counter** — `/tokens` shows cumulative input/output usage per agent; `/tokens_reset` resets
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

**Daily thunderstorm risk digest** *(monitor)*
> Every morning DRADIS fetches atmospheric instability data and sends a risk summary by time band — no LLM, no token cost.
> Cron: `0 7 * * *` — Monitor type: ⛈️ Thunderstorm risk (Open-Meteo)

**Daily appointments digest** *(scheduled task)*
> Every morning at 8:00, DRADIS automatically sends a Telegram message with your Google Calendar events for the day.
> Cron: `0 8 * * *` — Instructions: *"Fetch today's calendar events and send a summary to Telegram."*

**Morning briefing** *(scheduled task)*
> Cron: `0 7 * * 1-5` (weekdays at 7:00) — Instructions: *"Search for today's top tech news and send a summary to Telegram."*

**Morning email digest** *(scheduled task)*
> Every morning DRADIS checks your unread emails and sends a summary to Telegram.
> Cron: `0 8 * * 1-5` — Instructions: *"Check unread emails and send a brief summary of each to Telegram."*

## Telegram Commands

| Command | Description |
|---------|-------------|
| `/info` | Status and configuration of all agents |
| `/menu` | List all available commands |
| `/tasks` | List enabled tasks as inline buttons — tap one to run it immediately |
| `/monitors` | List enabled monitors as inline buttons — tap one to run it immediately |
| `/tokens` | Show cumulative token usage (input/output/total) per agent |
| `/tokens_reset` | Reset token counters to zero |
| `/gcalauth` | Connect Google Calendar (OAuth2) |
| `/gmailauth` | Connect Gmail (OAuth2) |

## Documentation

Full documentation is available in the **Documentation** tab of the app page in Home Assistant and on the [GitHub Wiki](https://github.com/procolo75/dradis/wiki).
