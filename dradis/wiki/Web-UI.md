# Web UI

The DRADIS Web UI is accessible directly from the Home Assistant sidebar via HA Ingress — no external port or network configuration required.

The UI uses a **vertical left sidebar** with collapsible sections: **Settings**, **Tools**, **Tasks**, **Scheduled Monitors**, **Live Monitors**, and **HA Monitors**. All sections except Settings are collapsed by default — click any header to expand it.

> **v3.0:** DRADIS is one agent on the main model. The former per-capability model selectors are gone — capabilities are just tools, enabled and authenticated under **Tools**.

---

## Settings → DRADIS

Runtime settings for the DRADIS agent. Saved to `/data/dradis_settings.json`, effective on the next message (no restart).

| Field | Default | Description |
|-------|---------|-------------|
| Provider | `openrouter` | LLM provider: OpenRouter, OpenAI, GitHub Models, Gemini, Groq. |
| Model | *(see below)* | Main model. Click 🔄 to fetch models, ⚡ to speed-test (tok/s) and keep the top 5. |
| Fallback Provider | *(blank)* | Provider used when the primary call fails. |
| Fallback Model | *(blank)* | Model to retry with on API error. Blank = no fallback. |
| Agent instructions | `You are DRADIS, a versatile AI assistant.` | System prompt — role, behaviour, persistent facts about you. |
| Startup message | `✅ DRADIS online and ready.` | Telegram message sent when the add-on starts. |
| Conversation history | `true` | Prepend the last N exchanges as context. |
| Conversation history depth | `2` | Past exchanges kept in context (resets on restart). |
| Max completion tokens | `2048` | Caps the reply (`max_tokens`) so prompt+reply fit the context window. Keep 2048 for Groq 8K. |
| Log token usage | `off` | When on, appends `🔢 in N · out N` to every chat and task reply. |
| Log tools used | `off` | When on, appends `🔧 tool1, tool2` (the tools DRADIS called that turn) to every chat and task reply. |
| Timezone | `UTC` | Timezone for all cron expressions. |

**Model loading by provider:** OpenRouter 🔄 fetches free ≥30B tool-calling models (⚡ speed-tests, keeps top 5); OpenAI fetches the GPT-4o family; GitHub Models / Gemini use fixed presets; Groq 🔄 fetches its LLM models (Whisper excluded).

---

## Settings → Car Mode

DRADIS messages are built to be **looked at**: an icon on every line, bold text, abbreviated units, `·` and `—` between facts, and a radar chart attached to weather alerts. Behind the wheel that format breaks down — CarPlay announces emoji by name, spells `45°` and `2/4` as symbols, reads URLs character by character, and often says nothing beyond "Image" when a photo is attached.

With Car Mode on, every alert, scheduled report and chat answer is rewritten as plain spoken prose: icons, markup and links removed, coordinates and record ids dropped, units spelled out (`12 km/h` → *12 chilometri orari*), compass points expanded (`O` → *ovest*), ratios turned into words (`Anello 2/4` → *Anello 2 su 4*), dates spelled out (`21/08/2026 14:32` → *21 agosto 2026 14:32*), and lines joined into sentences. Monitors keep their own configured language.

The conversion is deterministic — no model call, no added latency on an urgent alert, no tokens spent.

| Field | Default | Description |
|-------|---------|-------------|
| Enabled | `off` | Turn Car Mode on. Also togglable from Telegram with `/car`. Takes effect on the next message — nothing needs reloading. |
| Test message | — | Sends a sample storm alert in Car Mode wording, **whatever the toggle is set to** — you use it to decide whether to switch Car Mode on. Listening to it through CarPlay is the only real test. |

**Not sent in Car Mode:** radar charts, and scheduled chart reports (replaced by a line saying the report is waiting, so it never disappears silently).

**Not said in Car Mode:** anything describing the instrument rather than the weather — the token footer (`🔢 in N · out N`), the tools used (`🔧 …`), the monitor signature, coordinates, map links, record ids, fix age and accuracy, "non si sta muovendo", radar coverage, the open-event state, and the "nothing was changed" reassurance.

**Always said:** every line that explains a failure — a blind monitor and why, a switched-off one, a stale feed, a position that no longer exists. Silence and calm must not sound the same.

**Keeps its picture:** a snapshot you asked for with `/rain` or `/storm`. You requested it, so you are looking at the screen — the caption is still converted, and stripped of the coordinates, the map link and the fix diagnostics.

**Not converted:** the output of `/info`, `/manage`, `/menu`, `/tasks`, `/monitors`, `/hamonitors`. They answer a button you just pressed.

The sidebar dot shows whether Car Mode is still on.

> Activation is manual by design. GPS speed cannot distinguish *stopped in traffic* from *parked and walked away* — physically the same signal — so an automatic trigger would switch off exactly when you are still driving.

---

## Settings → MQTT / Home Assistant

| Field | Default | Description |
|-------|---------|-------------|
| Broker host | `core-mosquitto` | Hostname/IP of the MQTT broker. |
| Port | `1883` | MQTT broker port. |
| Username / Password | *(blank)* | MQTT auth (blank if none). |
| Statestream prefix | `homeassistant` | Base topic prefix — must match `base_topic` in `mqtt_discoverystream_alt`. |

Click **Test connection** to verify the broker is reachable.

---

## Settings → Positions

A **position** is a phone DRADIS can follow. A Storm front or Rain front monitor selects one and then watches wherever *that* phone is, so while travelling it can tell you whether you are driving into the storm — or the rain — rather than away from it. Add one per phone — yours, another family member's — and give each a name you will recognise in an alert, because that name is what the alert is titled with.

Positions are stored under `positions` in `/data/dradis_settings.json`, and they all share **one** MQTT connection. Nothing connects until you add one.

### Publishing a phone's position (Home Assistant side)

A `device_tracker` keeps its coordinates in **attributes**, and `mqtt_statestream` publishes **states**. Turning on `publish_attributes` would expose them, but it is a *global* switch that floods the broker with every attribute of every included entity. Expose just the two values instead, in `configuration.yaml`:

```yaml
template:
  - sensor:
      - name: "Phone latitude"
        unique_id: phone_latitude
        state: "{{ state_attr('device_tracker.my_phone', 'latitude') | round(5) }}"
        availability: "{{ state_attr('device_tracker.my_phone', 'latitude') is not none }}"
      - name: "Phone longitude"
        unique_id: phone_longitude
        state: "{{ state_attr('device_tracker.my_phone', 'longitude') | round(5) }}"
        availability: "{{ state_attr('device_tracker.my_phone', 'longitude') is not none }}"
      - name: "Phone GPS accuracy"
        unique_id: phone_gps_accuracy
        state: "{{ state_attr('device_tracker.my_phone', 'gps_accuracy') | int(0) }}"
        availability: "{{ state_attr('device_tracker.my_phone', 'gps_accuracy') is not none }}"

mqtt_statestream:
  base_topic: homeassistant
  publish_attributes: false
  publish_timestamps: true
  include:
    entities:
      - sensor.phone_latitude
      - sensor.phone_longitude
      - sensor.phone_gps_accuracy
```

Repeat the three sensors per phone, with distinct names.

`round(5)` is ~1 metre of resolution — precise enough for anything the storm front does, and it damps the GPS jitter that would otherwise republish constantly. The `availability` guard keeps `unknown` off the topic while the app has no fix yet.

**If you already have an `mqtt_statestream` block, merge into it.** Home Assistant accepts only one, and a second would silently drop your current includes.

**Keep `publish_timestamps: true`.** It publishes `last_updated` next to each value, which is the only way to date the *retained* message received on connect. Without it a position from yesterday arrives looking brand new and no staleness check can catch it.

Finally, raise the Companion app's location update frequency (*Settings → Companion app → Manage sensors → Location sensors*). The default "significant change" reporting can lag by minutes at motorway speed, which is exactly when this has to be right. Consider triggering high-accuracy mode from your car's Bluetooth so it only runs while you drive.

### Fields per position

| Field | Default | Description |
|-------|---------|-------------|
| Name | *(blank)* | What alerts are titled with. Make it recognisable. |
| Latitude entity | *(blank)* | Topic path after the prefix, e.g. `sensor/phone_latitude`. |
| Longitude entity | *(blank)* | As above. Both are required. |
| GPS accuracy entity | *(blank)* | Optional. When unset, the accuracy threshold is not applied. |
| Maximum fix age | `15` min | Older and a monitor following this position stops alerting until it comes back. Tripled while a storm is in progress. |
| Maximum GPS accuracy | `500` m | Vaguer fixes are not used. |
| Statestream prefix override | *(blank)* | Defaults to the global MQTT prefix. |

**🔍 Detect entities** listens on MQTT for three seconds and keeps only what looks like a coordinate, so you pick from a handful of candidates rather than every entity on the broker. When exactly one candidate matches a field it is filled in for you; when several do, each field's dropdown offers only its own kind. If nothing is recognised — an unusual naming scheme — every entity is offered instead, so this is never a dead end.

**Test connection** uses the values **currently on screen**, saved or not: testing a form you have not saved yet is the normal case. It connects with its own throwaway client, so the running manager is never disturbed. It reports the fix, its age, its accuracy and your speed, and names the threshold that failed — "no position at all" and "a position from two hours ago" are different problems with different fixes.

**Deleting a position** does not rewrite the monitors following it. Silently converting them back to a fixed place would put them somewhere you never asked to watch, so they freeze instead, and their form shows the dangling reference.

---

## Tools

Each capability contributes tools the single agent can call (see [Tools](Tools) for the full tool list). They are enabled and authenticated here; the agent always uses the **main model**. Common fields per capability:

| Field | Description |
|-------|-------------|
| Enabled | Activate the capability (requires its API key / OAuth token). |
| Test connection | Verify the backend is reachable (Web Search, Weather…). |
| Additional instructions | Apply **only while this capability's tools are in use**. Added to the system prompt when one of them is attached, labelled with the capability and its tool names. |

**Capabilities:** Web Search (Tavily), Weather (Open-Meteo), Google Calendar, Gmail, Google Tasks, URL Fetch (Jina Reader). Per-capability provider/model selectors no longer exist — a notice in each panel explains this.

**Voice (message transcription)** is separate: it transcribes incoming Telegram voice messages via Groq Whisper, then hands the text to the agent. It keeps its own settings:

| Field | Default | Description |
|-------|---------|-------------|
| Enabled | `false` | Activate voice transcription. Requires `groq_api_key`. |
| Whisper Model | `whisper-large-v3-turbo` | Click 🔄 to fetch Whisper models. |
| Language | `it` | ISO 639-1 transcription language code. |
| Send transcription | `true` | Echo transcribed text to Telegram before the reply. |

---

## Tasks

See [Tasks](Tasks) for full details and examples.

| Action | Description |
|--------|-------------|
| `+` button | Create a new task. |
| Sidebar item | Open the task form. Green/red dot = enabled/disabled. |
| **Tools** | Choose which tools the task may use — *All available tools* or *Selected tools* (grouped by capability). Fewer tools = smaller prompt. |
| **▶ Test Task** | Run immediately without altering the cron schedule. |
| **⎘ Copy** | Duplicate the task (disabled by default). |
| **🗑 Delete** | Remove the task. |

---

## Scheduled Monitors

See [Monitors](Monitors) for full details.

| Action | Description |
|--------|-------------|
| `+` button | Create a new monitor. |
| Sidebar item | Open the form. Green/red dot = enabled/disabled. |
| **▶ Test Monitor** | Trigger immediate execution; result to Telegram. |
| **⎘ Copy** | Duplicate the monitor (disabled by default). |
| **🗑 Delete** | Remove the monitor. |

---

## Live Monitors

See [Live-Monitors](Live-Monitors) for full details.

| Action | Description |
|--------|-------------|
| `+` button | Create a new live monitor. |
| Sidebar item | Open the form. Green/red dot = running/stopped. |
| Status badge | 🟢 Running / 🟠 Degraded / 🔴 Stopped — fetched live. |
| **📡 Test radar coverage** | *Rain front only.* Fetches the newest radar product and reports reachability, how late it was published, how much of the watched disc the network can actually see, and the current intensity at your point. |
| **⎘ Copy** | Duplicate the monitor (disabled by default). |
| **🗑 Delete** | Remove the monitor. |

---

## HA Monitors

See [HA-Monitors](HA-Monitors) for full details.

| Action | Description |
|--------|-------------|
| `+` button | Create a new HA monitor. |
| Sidebar item | Open the form. Green/red dot = running/stopped. |
| **🔍 Discover** | Browse entities currently publishing to the MQTT broker. |
| Status badge | 🟢 Running / 🔴 Stopped. |
| **🗑 Delete** | Remove the monitor. |
