# Tasks

LLM Scheduled Tasks let you automate recurring DRADIS actions on a cron schedule. Tasks are stored in `/data/tasks.json`.

## How It Works

When a task fires, its **Instructions** field is sent to the DRADIS agent exactly as if you had typed them in Telegram. DRADIS runs as a single agent on the main model with the **tools** you selected for the task, and calls whichever ones it needs. The result is delivered to your Telegram chat with a label identifying the task name.

Cron jobs reload immediately on save/delete — no add-on restart required.

## Tool selection & the Groq 8K limit (v3.0)

On the Groq free tier the limit is **8000 tokens-per-minute**, and every tool schema you attach adds ~1000 tokens to each request. So the way to keep a task under the limit is to attach only the tools it needs.

Each task has a **Tools** setting:

| Mode | Behavior |
|------|----------|
| **All available tools** (default) | Attaches every enabled + authenticated tool. |
| **Selected tools** | Tick exactly the tools the task needs, grouped by capability. |

For example, a *mail → calendar* task should select just `get_unread_emails` and `create_calendar_event`. A completion cap (**Settings → DRADIS → Max completion tokens**, default 2048) is also applied. Use **Settings → DRADIS → TPM probe** (🔬) to see how many prompt tokens the current model costs per tool.

## Creating a Task

Click `+` in the **Tasks** sidebar header.

| Field | Description |
|-------|-------------|
| Name | Display name shown in the sidebar and in the Telegram label. |
| Enabled | Green dot in sidebar when active. |
| Schedule preset | Common schedules: Every minute, Every hour, Daily at 8:00, Daily at 20:00, Every Monday at 9:00, Weekdays 9–18 every hour. |
| Cron expression | 5-part cron (minute hour day month weekday). Editing it directly sets the preset to "Custom…" and shows a live human-readable description + next fire time. |
| Instructions | What DRADIS should do. Written in natural language — the agent selects tools automatically. |

## Cron Reference

```
┌──────── minute  (0–59)
│ ┌────── hour    (0–23)
│ │ ┌──── day     (1–31)
│ │ │ ┌── month   (1–12)
│ │ │ │ ┌ weekday (0–6: 0=Mon … 6=Sun; or: mon tue wed thu fri sat sun)
│ │ │ │ │
* * * * *
```

| Preset | Cron |
|--------|------|
| Every minute | `* * * * *` |
| Every hour | `0 * * * *` |
| Daily at 08:00 | `0 8 * * *` |
| Daily at 20:00 | `0 20 * * *` |
| Every Monday at 09:00 | `0 9 * * 0` |
| Weekdays 09–18 every hour | `0 9-18 * * 0-4` |

The timezone is configured in **Settings → DRADIS → Timezone for scheduled tasks** (default UTC).

## Testing a Task

Each task form has a **▶ Test Task** button that triggers an immediate one-off execution without modifying the cron schedule. The result is delivered to Telegram exactly as a scheduled run would.

## Duplicating a Task

Click **⎘ Copy** in any task form to create a copy named `Copy of <name>`. The duplicate is disabled by default, with the same cron and instructions. It is immediately selected in the sidebar and ready to edit.

## Examples

### Daily appointments digest
```
Cron: 0 8 * * *
Instructions: Fetch today's calendar events and send a tidy summary to Telegram.
```
*Requires: Google Calendar enabled.*

### Morning news briefing
```
Cron: 0 7 * * 0-4
Instructions: Search for today's top technology news and send a short summary to Telegram.
```
*Requires: Web Search enabled.*

### Evening inbox digest
```
Cron: 0 18 * * 0-4
Instructions: Summarise unread emails received today and send the digest to Telegram.
```
*Requires: Gmail enabled.*

### Weekly to-do list
```
Cron: 0 9 * * 0
Instructions: List all open tasks and send them to Telegram with a motivational note.
```
*Requires: Google Tasks enabled.*
