# DRADIS Agentic AI for Home Assistant — Wiki

DRADIS is a Home Assistant add-on that exposes a conversational AI agent controllable via Telegram. All settings are managed from the built-in Web UI.

## Contents

| Page | Description |
|------|-------------|
| [Installation](Installation) | How to install DRADIS from a custom HA repository |
| [Configuration](Configuration) | API keys, credentials, and HA tab options |
| [Architecture](Architecture) | How DRADIS is structured internally |
| [Agents](Agents) | Web Search, Weather, Voice, Google Calendar, Gmail, Google Tasks |
| [Tasks](Tasks) | LLM-powered recurring tasks with cron scheduling |
| [Monitors](Monitors) | Scheduled monitors: Thunderstorm, Rain, Seismic |
| [Live-Monitors](Live-Monitors) | Persistent push monitors: Lightning, Seismic live |
| [HA-Monitors](HA-Monitors) | Home Assistant entity monitors via MQTT |
| [Web-UI](Web-UI) | Web UI reference — all panels and fields |
| [Telegram-Commands](Telegram-Commands) | All Telegram bot commands |
| [Troubleshooting](Troubleshooting) | Common problems and solutions |

## Quick Start

1. [Install the add-on](Installation) from the custom repository
2. Fill in your credentials in the **Configuration** tab
3. Start the add-on — DRADIS sends a startup message to your Telegram chat
4. Open the **Web UI** from the HA sidebar to enable sub-agents and create tasks
5. Talk to the bot on Telegram
