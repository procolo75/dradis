# HA Monitors

Monitor any Home Assistant entity via MQTT and receive a Telegram alert whenever its state changes. Each monitor supports two alert modes: **LLM** (DRADIS writes the message using your instructions and its full capabilities) or **Direct Telegram** (immediate fixed-format message, zero LLM cost). HA monitors are stored in `/data/ha_monitors.json`.

## Prerequisites

- **Mosquitto broker** add-on (HA Add-on store)
- **MQTT integration** (HA Devices & Services)
- **`mqtt_discoverystream_alt`** custom integration (installed via HACS)

## Quick Setup

**Step 1 — Install and configure `mqtt_discoverystream_alt`:**

Add to `configuration.yaml`:

```yaml
mqtt_discoverystream_alt:
  - base_topic: homeassistant
    publish_attributes: true
    publish_timestamps: true
    publish_retain: true
    republish_time: 1
    publish_discovery: true
    include:
      entities:
        - switch.your_entity_here
        - sensor.your_sensor
```

Restart Home Assistant.

**Step 2 — Configure MQTT in DRADIS:**

In the Web UI go to **Settings → MQTT / Home Assistant**:

| Field | Default | Description |
|-------|---------|-------------|
| Broker host | `core-mosquitto` | Hostname or IP of the MQTT broker. Use `core-mosquitto` for the HA Mosquitto add-on. |
| Port | `1883` | MQTT broker port. |
| Username | *(blank)* | MQTT username (leave blank if no authentication). |
| Password | *(blank)* | MQTT password. |
| Statestream prefix | `homeassistant` | Must match the `base_topic` in `configuration.yaml`. |

Click **Save**, then **Test connection** to verify.

**Step 3 — Create an HA Monitor:**

Expand **HA Monitors** → click `+` → configure the monitor fields → click **Save**.

## Monitor Fields

| Field | Description |
|-------|-------------|
| Name | Display name shown in the sidebar. |
| Enabled | Green dot when active. |
| Entities | One or more HA entities to watch. Type `domain/entity_id` (e.g. `switch/lights`) or native HA format (`switch.lights`). Click **🔍 Discover** to browse entities currently publishing to the broker. |
| State filter | Optional comma-separated list of states that trigger an alert (e.g. `on, off`). Leave blank to alert on any state change. States not in the list are silently discarded before any LLM call or Telegram send. |
| Alert mode | **LLM** — DRADIS processes the state change using your instructions. **Direct Telegram** — sends a fixed-format message immediately, no LLM call. |
| DRADIS Instructions | *(LLM mode only)* What DRADIS should do when the state changes. Examples: *"Send a Telegram message warning the switch turned off."* / *"Send an email with subject 'Sensor alert'."* Instructions are binding — the LLM always follows them. If empty, DRADIS sends a default Telegram alert. |
| Message template | *(Direct mode only)* Fixed Telegram message. Supports `{entity}`, `{state}`, `{previous_state}`, `{time}`. Default: `⚡ {entity}: {state} — {time}`. |
| Alert language | Language of the alert: 🇮🇹 Italiano or 🇬🇧 English. |
| Cooldown per entity (min) | Minimum time between alerts for the same entity (1–1440 min, default 60). Cooldown is only consumed when an alert is actually sent (SKIP responses do not consume cooldown). |
| Status badge | 🟢 Running / 🔴 Stopped — fetched live from the backend. |

## Alert Modes

### LLM Mode

The full DRADIS agent (with all enabled tools: Gmail, Google Calendar, Google Tasks, etc.) receives the entity ID, new state, previous state, timestamp, and your instructions. It executes the instructions directly — there is no SKIP mechanism. Use this when you want smart, context-aware responses (e.g. send an email, create a task, check a calendar).

### Direct Telegram Mode

Sends a fixed Telegram message immediately on state change. Zero LLM cost, zero latency. Use this for simple notifications where no intelligence is needed.

**Template variables:**

| Variable | Description |
|----------|-------------|
| `{entity}` | Entity ID (e.g. `switch/lights`) |
| `{state}` | New state (e.g. `on`) |
| `{previous_state}` | Previous state (e.g. `off`) |
| `{time}` | Local timestamp (e.g. `14:32`) |

## Behaviour Details

- **Retained message on (re)connect**: when the MQTT broker sends a retained message (initial state) on connect, the monitor silently records it as the baseline and does **not** alert. Only subsequent real state changes trigger alerts.
- **State filter**: states not in the filter list are discarded before any processing — this prevents unnecessary LLM calls or Telegram messages.
- **Cooldown**: updated only when an alert is actually sent. A SKIP response (LLM mode) or a filtered state does not update the cooldown.

## Example — Switch Alert

```
Name:         Lights monitor
Entities:     switch.living_room_lights
State filter: off
Alert mode:   Direct Telegram
Template:     ⚠️ {entity} turned {state} at {time}
Cooldown:     5 min
```

## Example — Smart Alert with Email

```
Name:         Security sensor
Entities:     binary_sensor.front_door
State filter: on
Alert mode:   LLM
Instructions: Send a Telegram message "🚨 Front door opened at {time}".
              Also send an email to me with subject "Security alert: front door".
Cooldown:     10 min
```
