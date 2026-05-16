# Telegram Commands

All commands are available only to the user ID configured in `telegram_allowed_chat_id`.

| Command | Description |
|---------|-------------|
| `/menu` | Show all available commands with descriptions |
| `/info` | Show current configuration: provider, model, history, and status of each sub-agent |
| `/tasks` | List all enabled tasks as inline buttons. Tap a button to run the task immediately |
| `/monitors` | List all enabled monitors (scheduled and live) as inline buttons. Tap a scheduled monitor to run it; tap a live monitor to see its status (🟢/🔴) |
| `/gcalauth` | Start the Google Calendar OAuth2 flow. Sends an authorization link; browser redirects back to DRADIS automatically after you grant access |
| `/gmailauth` | Start the Gmail OAuth2 flow (same flow as Calendar) |
| `/gtasksauth` | Start the Google Tasks OAuth2 flow (same flow as Calendar) |
| `/todo` | Shortcut — list all open Google Tasks without going through DRADIS team routing |

## /gcalauth, /gmailauth, /gtasksauth

These commands start the Google OAuth2 authorization flow:

1. Send the command — DRADIS replies with an authorization link.
2. Open the link in your browser and sign in with your Google account.
3. Grant access — your browser redirects back to DRADIS automatically ✅.

**If the automatic redirect doesn't work** (HA on a different device than the browser):

- Copy the full URL from the browser address bar after granting access.
- Send it back to the bot: `/gcalauth <url>`, `/gmailauth <url>`, or `/gtasksauth <url>`.

The OAuth token is saved to `/data/` and auto-refreshed. Each service requires its own authorization.

## /info Output Example

```
DRADIS
Provider: openrouter
Model: meta-llama/llama-3.1-70b-instruct:free
History: on (2 exchanges)

Web Search
Status: enabled
Model: meta-llama/llama-3.1-70b-instruct:free

Weather
Status: enabled
Model: meta-llama/llama-3.1-70b-instruct:free

Voice
Status: disabled

Google Calendar
Status: enabled
Provider: openrouter
Model: meta-llama/llama-3.1-70b-instruct:free
Auth: ✅ connected

Gmail
Status: disabled

Google Tasks
Status: disabled
```
