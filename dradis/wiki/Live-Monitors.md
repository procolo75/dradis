# Live Monitors

Live monitors are persistent push-based integrations that react to external events in real time — no cron schedule, no LLM, no token cost. They run as always-on asyncio tasks. Live monitors are stored in `/data/live_monitors.json`.

## Creating a Live Monitor

Click `+` in the **Live Monitors** sidebar header. Select a **Type** to reveal the relevant configuration fields:

| Type | Description |
|------|-------------|
| ⚡ Lightning alert | Persistent MQTT listener on Blitzortung; DBSCAN cells reduced to one nearest-threat track; single 🟡/🔴/✅ state machine |
| 🌍 Seismic live | Polls INGV GOSSIP every 60 s; alerts on new events and state promotions |
| ⚽ Football Betting | Polls RapidAPI every 5 min (clock-aligned); alerts on statistically favourable live-match conditions |

All types share **Name**, **Enabled**, and **Telegram bot** fields. Additional fields are type-specific (see sections below).

There is no cron field and no "run now" action — the monitor is always-on when enabled. On disconnect, event-based monitors (Lightning, Seismic) reconnect automatically after a short delay.

---

## ⚡ Lightning Alert

Subscribes to geohash-based MQTT topics covering the configured location and its 8 neighbouring cells. Incoming strikes within `radius_km` are collected in a **15-minute sliding window**. Every 2 minutes a polling task runs **pure-Python DBSCAN** (eps = 8 km, min_samples = 2) to identify storm cells, then reduces all activity to a **single scalar** — the distance of the *nearest significant cell* — appended to a **30-minute series**. That series (not per-cell centroids) drives the approach trend, velocity and ETA, so it does not reset when DBSCAN re-labels cells. A single **threat state machine** per monitor produces one coherent thread per storm episode instead of one alert per cell.

> **Why the change (v2.26.0):** the previous per-cell zone alerts split one storm front into several clusters, each firing its own "Storm detected — Undetermined" with jumping distance/direction, and intermittent strikes caused clear ↔ approaching flapping. It was impossible to tell whether a storm was actually coming.

### Threat Levels

| Level | Meaning |
|-------|---------|
| 🟢 CLEAR | No significant activity, or quiet for ≥ 25 min |
| 🟡 WATCH | Significant activity within 50 km, approach not yet confirmed |
| 🔴 WARNING | Confirmed approach (≥ 2 approaching polls, ≥ 3 strikes) and close (≤ 15 km) or short ETA (≤ 30 min) |

Trend over the series — APPROACHING / RETREATING / STATIONARY / UNKNOWN (last 3 samples, > 0.5 km/sample) — is shown in the WATCH message.

### Alert Triggers (level-based)

| Event | Trigger | Icon |
|-------|---------|------|
| Watch | First significant activity within 50 km | 🟡 |
| Warning | Approach confirmed and close / short-ETA | 🔴 |
| Periodic re-alert | Every 10 min while in WARNING | 🔴 |
| De-escalation | 12-min gap → drops WARNING to WATCH | 🟡 |
| All clear | No significant activity for 25 consecutive minutes | ✅ |

Alerts fire **only on level change** (plus the periodic WARNING re-alert). Confirmation polls (escalation) and a quiet gap (de-escalation) provide hysteresis, so brief strike gaps no longer cause flapping. The state machine advances only on a confirmed Telegram send.

### Alert Examples

**Watch:**
```
🟡 Storm in the area — Bacoli
📍 Activity at 28.3 km to NW (315°)
📊 Approaching
🔢 Strikes (last 15 min): 9
🕐 14:20
```

**Warning:**
```
🔴 Storm WARNING — Bacoli
📍 Approaching: 12.0 km to NW (315°)
🚀 ~42 km/h — estimated arrival: 18 min
🔢 Strikes (last 15 min): 24
🕐 14:32
```

**All clear:**
```
✅ Storm threat cleared — Bacoli
🔇 No lightning for 25 min
🕐 15:10
```

### Example Configuration

```
Name:     Bacoli Lightning
Type:     ⚡ Lightning alert
Location: Bacoli
Radius:   50 km
Language: 🇮🇹 Italiano
```

---

## 🌍 Seismic Live

Polls the [INGV GOSSIP JSON API](https://terremoti.ov.ingv.it/gossip) every 60 seconds for one or more volcanic/seismic areas. Sends a Telegram alert when:

- A **new seismic event** is detected (not previously seen)
- An existing event is **promoted from Automatic to Revised** (INGV manually reviewed it)

### Quiet Hours

Configure `quiet_start` / `quiet_end` (HH:MM format) to suppress notifications during a time range (e.g. 23:00–07:00). Events that occur during quiet hours are accumulated in memory. When the quiet period ends, a 🔕 header is sent followed by all accumulated events in order. Cross-midnight intervals are supported.

### Additional Fields

| Field | Description |
|-------|-------------|
| Areas | One or more areas: Campi Flegrei, Vesuvio, Isola di Ischia, Golfo di Napoli. |
| Quiet start | Start of quiet hours (HH:MM). Leave blank to disable quiet hours. |
| Quiet end | End of quiet hours (HH:MM). |

When `enabled: false`, the monitor continues polling and tracking events silently (no Telegram notifications) but keeps the seen-event index in memory, so the first alert after enabling won't re-send old events.

### Alert Icons

| Icon | Meaning |
|------|---------|
| ⚠️ | Automatic (preliminary) |
| ✅ | Revised (final, manually reviewed) |

### Example Configuration

```
Name:         Seismic live — Campi Flegrei
Type:         🌍 Seismic live
Areas:        Campi Flegrei, Vesuvio
Quiet start:  23:00
Quiet end:    07:00
Language:     🇮🇹 Italiano
```

---

## ⚽ Football Betting

Polls [football-betting-odds1.p.rapidapi.com](https://rapidapi.com/fluis.lacasse/api/football-betting-odds1) every 5 minutes at exact clock-aligned boundaries (:00, :05, :10, :15 … regardless of when DRADIS started). Sends a Telegram alert when all of the following conditions are met simultaneously in a live match:

1. Match is in the **2nd half** (`periodID == "3"`)
2. Match minute falls inside a configured **minute window** (e.g. 55′–65′ or 75′–81′)
3. **Goal difference == 1** (one team leads by exactly one goal)
4. The **losing team's next-goal odds are lower** than the winning team's — the market expects the trailing team to score next

This combination identifies matches where the statistics and betting market both suggest the losing team has the momentum to equalise — a classically exploitable live-betting signal.

**Requires:** `rapidapi_football_key` in the HA Configuration tab (available from [RapidAPI](https://rapidapi.com/fluis.lacasse/api/football-betting-odds1)).

### Configuration Fields

| Field | Description |
|-------|-------------|
| Minute windows | Select one or both: **55′–65′** and **75′–81′**. Both are enabled by default. Additional windows are planned for a future release. |
| API pause | Time range during which API calls are suppressed (default 23:00–07:00). Avoids unnecessary API usage overnight. Leave blank to disable. |

### Provider Fallback

The API is queried via `provider1` → `provider2` → `provider3` → `provider4` in order. The first successful non-empty response wins. If all providers fail, the poll is skipped silently and retried at the next 5-minute boundary.

### Alert Message

```
⚽ SEGNALE SCOMMESSA LIVE

🏆 Ethiopia - Premier League
Negele Arsi Ketema vs Hawassa Kenema SC
1-0  ⏱ 57'
```

### Test API Panel

The **🔍 Test API** button in the Web UI fetches all current live matches and renders them in a table:

| Column | Description |
|--------|-------------|
| Min | Current match minute and half |
| Campionato | League / competition name |
| Casa / Fuori | Home and away team names |
| Ris. | Current score |
| NG Casa / NG Fuori | Next-goal odds for home and away |
| ⚽ | 🔔 = signal active (all conditions met); ⚠️ = in window with 1-goal difference but odds not favourable |

Rows are highlighted: 🟩 green = active signal, 🟨 yellow = partial match (in window, 1-goal diff, but odds condition not met).

### Deduplication

One alert is sent per **match × window**. The alert key is pruned as soon as the match leaves the live feed, so a new alert fires correctly if conditions are met again later in the same match (different window).

### Coming Soon

The following options are planned for upcoming releases:
- Additional configurable minute windows
- Minimum next-goal odds threshold (filter out very short-priced favourites)
- Goal-difference threshold (e.g. allow alerts when difference == 2)
- League or competition filter

### Example Configuration

```
Name:          Football Betting
Type:          ⚽ Football Betting (RapidAPI)
Minute windows: 55′–65′ ✅  75′–81′ ✅
API pause:     23:00 – 07:00
Telegram bot:  default
```
