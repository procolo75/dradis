# Configuration

All API keys and credentials are configured in the add-on **Configuration** tab in Home Assistant. All runtime settings (provider, model, agent options, etc.) are managed from the **Web UI** and do not require a restart.

## Configuration Tab Fields

| Field | Type | Description |
|-------|------|-------------|
| `telegram_bot_token` | password | Telegram bot token (from BotFather) |
| `telegram_allowed_chat_id` | int | Telegram user ID allowed to interact |
| `openrouter_api_key` | password | *(Optional)* OpenRouter API key |
| `openai_api_key` | password | *(Optional)* OpenAI API key |
| `github_token` | password | *(Optional)* GitHub Personal Access Token for GitHub Models |
| `gemini_api_key` | password | *(Optional)* Google Gemini API key |
| `groq_api_key` | password | *(Optional)* Groq API key — required for the Voice sub-agent |
| `tavily_api_key` | password | *(Optional)* Tavily API key — required for the Web Search sub-agent |
| `google_client_id` | str | *(Optional)* Google OAuth2 client ID — required for Google Calendar, Gmail, and/or Google Tasks |
| `google_client_secret` | password | *(Optional)* Google OAuth2 client secret |

At least one LLM provider key is required. The active provider is selected from the Web UI.

## How to Get Your API Keys

- **Telegram bot token**: open Telegram, start a chat with [@BotFather](https://t.me/BotFather), send `/newbot` and follow the prompts — you will receive a token like `123456:ABC-DEF...`
- **Telegram user ID**: start a chat with [@userinfobot](https://t.me/userinfobot) — it will reply with your numeric ID
- **OpenRouter API key**: sign up at [openrouter.ai](https://openrouter.ai), go to **Settings → Keys** to create a key
- **OpenAI API key**: sign up at [platform.openai.com](https://platform.openai.com), go to **API keys**
- **GitHub token**: go to [github.com/settings/tokens](https://github.com/settings/tokens) — a classic token with no scopes is sufficient for GitHub Models
- **Gemini API key**: sign up at [aistudio.google.com](https://aistudio.google.com), click **Get API key**
- **Groq API key**: sign up at [console.groq.com](https://console.groq.com), go to **API Keys**
- **Tavily API key** *(optional)*: sign up at [tavily.com](https://tavily.com) — the free tier includes 1 000 searches/month

## Google OAuth2 Credential

One credential covers Google Calendar, Gmail, and Google Tasks.

**Part 1 — One-time Google Cloud setup (do this once for all Google services):**

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and create or select a project
2. **APIs & Services → Library** → enable the APIs you need: *Google Calendar API*, *Gmail API*, *Tasks API*
3. **APIs & Services → OAuth consent screen** → choose **External** → fill in app name (e.g. *DRADIS*) and your email → save
4. Still in the consent screen → **Publishing status** → click **Publish app** → confirm

   > ⚠️ **This step is essential.** If the app stays in **Testing** mode, Google revokes the refresh token every 7 days. Publishing makes the token permanent. No Google review is required for personal use.

5. **Credentials → Create credentials → OAuth client ID → Desktop app** → any name → **Create**
6. Copy the **Client ID** and **Client Secret** from the dialog
7. Paste them in the add-on Configuration tab (`google_client_id`, `google_client_secret`) and **restart the add-on**

**Part 2 — Authorize each service (run once per service):**

- **Calendar**: send `/gcalauth` to the Telegram bot → click the link → sign in → grant access → browser redirects back to DRADIS automatically ✅
- **Gmail**: send `/gmailauth` → same flow
- **Tasks**: send `/gtasksauth` → same flow

Each service uses a separate token file. If the automatic redirect doesn't work (HA on a different device), copy the full URL from the browser address bar and send it as `/gcalauth <url>`, `/gmailauth <url>`, or `/gtasksauth <url>`.

## Persistent Data Files

All runtime data is stored in `/data/` (inside the add-on container — never exposed in source):

| File | Description |
|------|-------------|
| `/data/options.json` | HA Configuration tab values |
| `/data/dradis_settings.json` | Web UI settings (provider, model, agent options, etc.) |
| `/data/agents.json` | Custom sub-agent definitions |
| `/data/tasks.json` | Scheduled LLM tasks |
| `/data/monitors.json` | Scheduled monitors (thunderstorm, rain, seismic) |
| `/data/live_monitors.json` | Live monitors (lightning, seismic live) |
| `/data/ha_monitors.json` | HA entity monitors |
| `/data/google_calendar_token.json` | Google Calendar OAuth token |
| `/data/google_gmail_token.json` | Gmail OAuth token |
| `/data/google_tasks_token.json` | Google Tasks OAuth token |
