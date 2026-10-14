# HA Assist — DRADIS on your voice assistant

Talk to DRADIS through **any Home Assistant voice assistant** (an Assist satellite, a modified Echo,
a Voice PE…) exactly as you do in the Telegram chat: same model, same tools and the **same
conversation history**. Ask aloud, follow up on Telegram, or the other way round.

Home commands stay with Home Assistant. With **Prefer handling commands locally** on, HA's own
intents handle "turn off the living room light" — instantly, with its exact names and aliases, no
model involved — and only what HA does not understand is passed to DRADIS.

```
voice ──► HA Assist pipeline ──► HA local intents ── understood ──► HA acts (lights, switches…)
                                        │
                                        └─ not understood ──► DRADIS integration ──► add-on /api/assist
                                                                                        │
                       ┌────────────────────────────────────────────────────────────────┘
                       ├─ /rain, /storm, /stations words ──► direct command (no model, no tokens)
                       └─ anything else ──► the Telegram chat turn (model + tools + shared history)
```

## Setup (once)

1. **Update** the add-on (not just rebuild: the new version asks for access to Home Assistant's
   config folder, the Supervisor API and the Home Assistant API, and only an update applies
   permissions). DRADIS copies its integration to `/config/custom_components/dradis` and sends a
   Telegram message: *restart Home Assistant*.
2. Restart Home Assistant.
3. In the DRADIS Web UI, **Settings → 🎙️ HA Assist → Show URL and token**. The URL is
   `http://<add-on hostname>:8099` — `http://local-dradis:8099` for a local add-on.
4. *Settings → Devices & services → Add integration → **DRADIS*** and paste URL and token.
   If *Discovered: DRADIS* appears instead, just confirm it: URL and token come with the discovery.
5. *Settings → Voice assistants* → your assistant → **Conversation agent: DRADIS**, and turn on
   **Prefer handling commands locally**.
6. Back in the HA Assist panel: pick the **Default speaker** (and a **TTS engine** if it is a media
   player), **Save**, and press **Say a test sentence**.

## What you can say

### Anything — as in the chat

Every question goes to DRADIS with all its tools and the shared history: "che tempo fa domani a
Napoli?", "leggimi le ultime mail", "aggiungi un promemoria per domani alle 9", and then on Telegram
"e dopodomani?".

### Direct commands — no model, instant, no tokens

Matched on the words before any model is involved (case, accents and punctuation do not matter;
`_` counts as a space):

| The sentence contains… | Runs | Examples |
|---|---|---|
| *rain* | `/rain` | "rain", "lancia il comando rain" |
| *storm* | `/storm` | "storm", "storm temporali 2" |
| *radar* + pioggia / piove / piovere | `/rain` | "radar pioggia", "il radar dice che piove?" |
| *radar* + temporale / temporali / fulmine / fulmini | `/storm` | "radar temporali", "ci sono fulmini sul radar?" |
| *radar* alone | `/rain` and `/storm` | "cosa dice il radar?" |
| *sta per piovere* | `/rain` | "sta per piovere?" |
| *misura/e*, *stazione/i*, *sensore/i* | `/stations` | "misure meteo", "dammi i dati della stazione meteo", "sensori a Bacoli", "stazioni di Monte di Procida" |

- **Which monitors.** A monitor's name in the sentence narrows to it ("storm temporali 2" →
  `Temporali_2`). A name made only of the trigger words ("Temporali", "Pioggia") does not narrow:
  "radar temporali" asks for the kind. Otherwise **every** monitor of that kind answers in turn —
  the spoken stand-in for the Telegram keyboard. Each caption opens with its monitor's name.
- **Where, for stations.** A place after *a / ad / di / in / presso / vicino a* is read there;
  otherwise at your position (the first configured position with a usable fix).
- **Not commands.** "Piove domani?", "temporali stasera?" have no trigger word and go to the model,
  i.e. to the forecast.
- The exchange is kept in the shared history like any answer. In the Telegram copy the footer says
  `🔧 radar_now (direct)` or `🔧 station_readings (direct)`.

### DRADIS commands — through the model, with exact names

With **DRADIS commands** on, the Telegram menu is available as tools, by voice and in the chat. The
model is given the **exact names** of your tasks and monitors as a closed list (an enum): it cannot
invent a name or add a word to one.

| Tool | What it does | Example |
|---|---|---|
| `run_task` | runs a task now | "esegui il task Meteo mattina" |
| `run_monitor` | runs a **scheduled** monitor now (thunderstorm risk, rain, weather charts, seismic…) | "lancia il monitor rischio temporali" |
| `radar_now` | `/rain` / `/storm` when the sentence was not a direct command | "com'è la situazione dei temporali?" |
| `set_enabled` | enables / disables a task or scheduled monitor, or Car Mode | "disattiva il monitor sismico", "car mode on" |
| `station_readings` | `/stations` (only with MeteoHub on) | "che dicono le stazioni a Pozzuoli?" |

**Live and HA monitors are not driven by voice** — no status, no on/off. Their on-demand views
(`/rain`, `/storm`) are.

A task or monitor started by voice answers **"started"** at once and, when it finishes, its result
(or "chart sent to Telegram") is **spoken on the assistant that asked**. It still goes to Telegram as
always.

These tools are sent with every message: the panel shows their cost in tokens
(≈1000–1200 with a typical setup). Switch them off if you only want the direct commands.

## How the answer sounds

Everything said on a speaker goes through `voice.for_voice`: Car Mode's sanitiser (no emoji, links
or markup; units, compass points and dates in words) plus what a TTS engine would otherwise spell
out — `_` and `-` ("trattino"), number ranges (*10-15 km* → "10 a 15 chilometri"), Italian decimals
(*27.5* → "27,5"), list markers and slashes. The `/stations` readout has a spoken layout: each
station is named once with everything it measured. Car Mode's Telegram output is not affected.

- Long text is cut at a sentence end with "il resto è su Telegram".
- Token/tool footers and the fallback / tool-failure notices are **never read aloud**.
- If no model answers, the speaker hears one short sentence and the error goes to Telegram.

## Speaking at any time — the speaker

DRADIS can speak when nobody just asked: a task's result minutes later, a live monitor's alert.

- **Where.** On the device that asked, when there is one: HA sends its `device_id`, DRADIS looks up
  its entities and uses its `assist_satellite.*`, else its `media_player.*`. Otherwise the
  **Default speaker** from the panel.
- **How.** Through Home Assistant's API (via the Supervisor):
  - `assist_satellite.*` → `assist_satellite.announce` — the satellite speaks with its pipeline's
    voice. Preferred.
  - `media_player.*` → `tts.speak` with the **TTS engine** chosen in the panel.
- A speaker that is off or unreachable never breaks anything: the log says `[DRADIS] Speaker: …`.

## Live monitors that speak

In each live monitor's form:

- **Announce by voice** (off by default) — every alert is also said on the Default speaker.
- **🔕 Voice quiet hours** — inside the window the alert is not spoken but still reaches Telegram.
  Separate from the monitor's Telegram *Quiet hours*. Windows may cross midnight (23:00–07:00).

Telegram delivery is unchanged and stays what the monitor tracks: the voice is fire-and-forget.

## The HA Assist panel

| Field | Description |
|---|---|
| Enabled | Off → DRADIS refuses requests (HA says "DRADIS non risponde"). |
| Copy each exchange to Telegram | "🎙️ question", the formatted answer with the chat footer, pictures (radar) and notices. Off by default. |
| DRADIS commands | The control tools on/off, with their token cost. Direct commands follow the same switch. |
| Default speaker | `assist_satellite.*` or `media_player.*` (🔄 reloads the list from HA). |
| TTS engine | `tts.*`, needed only for a media player. |
| Say a test sentence | Speaks on the selected speaker, saved or not. |
| Status | Integration version, discovery, last question and how long it took. |
| Test | Re-installs the integration if needed, announces DRADIS again and asks one question (not kept in the history). |
| Show URL and token | For *Add integration → DRADIS*. Shown only through the add-on page (ingress). |

## Under the hood

| Piece | Where |
|---|---|
| HA integration (config flow + conversation agent) | `ha_integration/dradis/` — copied to `custom_components` by the add-on |
| Install, token, discovery | `dradis/assist.py` — token in `/data/assist_token` |
| Endpoint called by HA | `POST /api/assist` (`Authorization: Bearer <token>`) in `web/routes/assist.py` |
| One chat turn, shared with Telegram | `bot/state.py: chat_turn()`; origin of the turn in `state.CHANNEL` |
| Direct commands and control tools | `bot/control.py` (`match_radar`, `match_stations`, the five tools) |
| Speaking | `dradis/speaker.py` |
| Text for speech | `dradis/voice.py` |

The integration waits up to 90 s for an answer — enough for a rate-limited provider's minute to roll
over.
