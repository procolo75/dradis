# Telegram Commands

All commands are available only to the user ID configured in `telegram_allowed_chat_id`.

| Command | Description |
|---------|-------------|
| `/menu` | Show all available commands with descriptions |
| `/info` | Show current configuration: provider, model, history, and which tools are enabled |
| `/tasks` | List all tasks (✅ enabled / ⏸ disabled) as inline buttons. Tap a button to run the task immediately regardless of its enabled state |
| `/monitors` | List all scheduled and live monitors as inline buttons. Tap a scheduled monitor to run it; tap a live monitor to see its 🟢/🔴 status |
| `/hamonitors` | List all HA monitors with 🟢/🔴 running status. Tap one to see its name, mode, cooldown, and entity list |
| `/rain` | Snapshot of a 🌧️ Rain front monitor — the radar picture it would send right now, and where it believes it is. See [below](#rain-and-storm) |
| `/storm` | The same for a 🌩️ Storm front monitor |
| `/manage` | Toggle enable/disable for any task, monitor, live monitor, or HA monitor. Shows all components grouped by type with ✅/⏸ badges; tap a row to toggle it |
| `/car` | Toggle 🚗 Car Mode — messages rewritten as plain spoken prose for CarPlay. `/car on` / `/car off` set it explicitly. See [below](#car) |
| `/gcalauth` | Start the Google Calendar OAuth2 flow. Sends an authorization link; browser redirects back to DRADIS automatically after you grant access |
| `/gmailauth` | Start the Gmail OAuth2 flow (same flow as Calendar) |
| `/gtasksauth` | Start the Google Tasks OAuth2 flow (same flow as Calendar) |
| `/backupauth` | Start the Google Drive Backup OAuth2 flow. Grants `drive.file` scope only. After authorization, create a monitor of type ☁️ Google Drive Backup in the Web UI |

## /rain and /storm

Weather monitors are quiet by design — most of the time correctly so. These two commands let you ask, right now, *what does this monitor actually see, and does it know where I am*, without waiting for weather.

They answer with **one message**: the picture the monitor would attach to a real alert, and a caption saying where it thinks it is.

```
🌧️ Casa — situazione ora
🟢 Attivo

📍 Origine: posizione «Telefono di Procolo»
   44.89930, 7.11060 · apri la mappa
   fix di 47 s fa · ±12 m · 96 km/h verso NE

📡 Radar delle 17:35 (8 min fa) · copertura 100%
🌧️ Fronte a 12 km a SO · max 19.4 mm/h (forte)
🌬️ Pioggia verso E a 60 km/h
🧭 Incontro fra 14 min, a 1 km da te

🎯 Nessun evento aperto
ℹ️ Ho solo guardato: nessun avviso inviato, niente cambiato nel monitor.
```

The first two lines are the monitor and, when something is wrong with it, its data source — reported **separately**. A storm monitor whose lightning feed has not connected is *active and blind*, which is a different instruction to you than *switched off*, so the two never share a badge.

Times are shown in the timezone configured in DRADIS settings, not the add-on container's clock.

### They change nothing, and that is the point

A snapshot **perceives without deciding**. It rebuilds the picture with the same code a normal check uses, but never touches the machinery that decides whether to alert you.

That restraint is not tidiness. The guarantee that one storm can only ever produce a handful of messages rests on a counter of how many have already been sent. A test that nudged that counter would **silence the real alert** an hour later; one that opened an event would let a single cell send a second full set of messages. Both would fail silently, and you would only find out during weather. Every caption ends by stating that nothing was changed.

### Checking the position

The caption shows the coordinates to five decimals and a **tap-to-open map link** — because no number answers *"is it actually where I am"* as directly as looking.

For a monitor that follows a phone it also reports how old the fix is, how precise it is, and how fast you are moving and in which direction.

It deliberately does **not** report a distance from the monitor's `location` field. For a monitor that follows a phone those coordinates are dead configuration — whatever default it happened to be created with — so a line like *"176 km from the configured location"* measures from nowhere. `/monitors` used to make the same mistake and stopped in v4.1.1.

When a monitor is blind, the command says so *and still shows the fix*:

```
⚠️ Cieco: the last fix is 41 min old, past the 15 min limit
Per questo non manda avvisi.
```

This is the difference between a status display and a diagnostic. Internally the monitor asks "is this position usable" and gets back nothing; the command asks "what is the last position" and applies the limits itself, so it can tell you *why*.

### Picking a monitor

| You type | What happens |
|---|---|
| `/rain` | One rain monitor configured: straight to the picture. Several: inline buttons |
| `/rain casa` | Matches the name, case-insensitive, anywhere in it |
| `/storm`, `/storm <name>` | The same, for storm front monitors |

### Monitors that are switched off

`/rain` still works: the radar image is downloaded on demand, so you can check a monitor while you are still setting it up. The caption says the picture was fetched on the spot and that nothing will arrive on its own until you enable it.

`/storm` cannot do this, and says so. Lightning is only buffered while the subscription is up, so a stopped storm monitor has nothing to show — you still get its position and configuration.

---

## /car

Turns 🚗 **Car Mode** on and off. No argument toggles; `/car on` and `/car off` are explicit, so a dictated command cannot flip it back by accident.

DRADIS alerts are built to be looked at — an icon on every line, bold text, abbreviated units, and a radar chart attached to weather alerts. Read aloud by CarPlay, that becomes emoji announced by name, `45°` and `2/4` spelled as symbols, URLs read character by character, and photo messages frequently not read at all.

In Car Mode every alert, scheduled report and chat answer is rewritten as plain spoken prose:

```
⛈️ <b>Temporale nel raggio — Casa</b>
📍 Fronte a <b>12 km</b> a O (270°)
🎯 Anello 2/4 · entro 20 km
🚗 In movimento a 80 km/h verso NE
```

becomes

```
Temporale nel raggio, Casa. Fronte a 12 chilometri a ovest (270 gradi).
Anello 2 su 4, entro 20 chilometri. In movimento a 80 chilometri orari verso nord-est.
```

Note `O` → *ovest*: spoken, the compass abbreviation for west is the Italian conjunction "or". That one is invisible on screen and total in the car.

Charts are not sent while Car Mode is on, and a chart-only scheduled report is replaced by a line saying it is waiting rather than disappearing. Snapshots you asked for with `/rain` and `/storm` keep their picture — you requested those, so you are looking at the screen.

The conversion is deterministic: no model call, no added latency, no tokens. `/info` reports the state, which persists across restarts. Full details in [Web UI → Settings → Car Mode](Web-UI#settings--car-mode).

> Activation is manual by design. GPS speed cannot distinguish *stopped in traffic* from *parked and walked away* — physically the same signal.

---

## /gcalauth, /gmailauth, /gtasksauth, /backupauth

These commands start the Google OAuth2 authorization flow:

1. Send the command — DRADIS replies with an authorization link.
2. Open the link in your browser and sign in with your Google account.
3. Grant access — your browser redirects back to DRADIS automatically ✅.

**If the automatic redirect doesn't work** (HA on a different device than the browser):

- Copy the full URL from the browser address bar after granting access.
- Send it back to the bot: `/gcalauth <url>`, `/gmailauth <url>`, `/gtasksauth <url>`, or `/backupauth <url>`.

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
