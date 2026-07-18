# Installation

## Requirements

- Home Assistant with Supervisor (HAOS or Supervised)
- A Telegram bot — created via [@BotFather](https://t.me/BotFather)
- An API key for at least one supported LLM provider (OpenRouter, OpenAI, GitHub Models, Gemini, or Groq)
- *(Optional)* A [Tavily](https://tavily.com) API key for the Web Search tool
- *(Optional)* A [Groq](https://console.groq.com) API key for voice transcription
- *(Optional)* Google Cloud OAuth2 credentials — one credential covers Google Calendar, Gmail, and Google Tasks

## Steps

1. In Home Assistant go to **Settings → Apps → Install App → ⋮ → Repositories**
2. Add the repository URL: `https://github.com/procolo75/dradis`
3. Find **DRADIS** in the store and click **Install**
4. Go to the **Configuration** tab (see [Configuration](Configuration) for all fields)
5. Click **Start**

After startup, DRADIS sends a confirmation message to your Telegram chat and exposes the Web UI via the Home Assistant sidebar.

## Updating

Home Assistant checks for new versions automatically. When a new version is available, an "Update" button appears in the add-on page. Click it to update — no configuration changes are required between releases unless explicitly noted in the [CHANGELOG](https://github.com/procolo75/dradis/blob/main/dradis/CHANGELOG.md).
