# DRADIS Agentic AI for Home Assistant

DRADIS is a Home Assistant add-on that exposes a conversational AI agent controllable via Telegram. The agent is fully configurable from the built-in Web UI and the HA Configuration tab — no code changes required.

## Features

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
3. Find **DRADIS Agentic AI for Home Assistant** in the store and click **Install**

## Documentation

Full documentation is available in the **Documentation** tab of the add-on page in Home Assistant, or directly in the Web UI under **Other → Documentation**.
