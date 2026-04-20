# DRADIS Agentic AI for Home Assistant

DRADIS is a Home Assistant add-on that exposes a conversational AI agent controllable via Telegram. The agent is fully configurable from the built-in Web UI and the HA Configuration tab — no code changes required.

## Features

- **Branded icon**: custom DRADIS AI icon in the HA add-on dashboard and sidebar
- **Multi-provider LLM**: OpenRouter, OpenAI, GitHub Models, Gemini, Groq
- **Web Search** sub-agent powered by Tavily (optional)
- **Weather** sub-agent powered by Open-Meteo (free, no API key)
- **Voice** transcription via Groq Whisper (optional)
- **Google Calendar** — read, create, and delete events via OAuth2 (optional)
- **Gmail** — read inbox, search, and send emails via OAuth2 (optional)
- **Scheduled Tasks** — cron-based automation delivered to Telegram
- **Model speed-test** — ranks models by tok/s, keeps top 5
- **Conversation history** with configurable depth
- All settings managed at runtime from the Web UI — no restart required

## Installation

1. In Home Assistant go to **Settings → Add-ons → Add-on Store → ⋮ → Repositories**
2. Add: `https://github.com/procolo75/dradis`
3. Find **DRADIS** in the store and click **Install**

## Usage Examples

**Voice appointment** *(requires Voice + Google Calendar)*
> 🎙️ *"Add a meeting with Marco on Friday at 3pm"*
> → DRADIS transcribes the audio, creates the event in Google Calendar, and confirms in Telegram.

**Weather**
> *"What's the weather in Milan tomorrow?"*
> → DRADIS calls the Weather sub-agent and replies with current conditions and a 3-day forecast.

**Web search**
> *"What are the latest Home Assistant announcements?"*
> → DRADIS searches the web via Tavily and sends a summarised answer.

**Daily appointments digest** *(scheduled task)*
> Every morning at 8:00, DRADIS automatically sends a Telegram message with your Google Calendar events for the day.
> Cron: `0 8 * * *` — Instructions: *"Fetch today's calendar events and send a summary to Telegram."*

**Morning briefing** *(scheduled task)*
> Cron: `0 7 * * 1-5` (weekdays at 7:00) — Instructions: *"Search for today's top tech news and send a summary to Telegram."*

**Morning email digest** *(scheduled task)*
> Every morning DRADIS checks your unread emails and sends a summary to Telegram.
> Cron: `0 8 * * 1-5` — Instructions: *"Check unread emails and send a brief summary of each to Telegram."*

## Documentation

Full documentation is available in the **Documentation** tab of the add-on page in Home Assistant, or directly in the Web UI under **Other → Documentation**.
Full docs also on the [GitHub Wiki](https://github.com/procolo75/dradis/wiki).
