# Live Monitors

Live monitors are persistent push-based integrations that react to external events in real time — no cron schedule, no LLM, no token cost. They run as always-on asyncio tasks. Live monitors are stored in `/data/live_monitors.json`.

## Creating a Live Monitor

Click `+` in the **Live Monitors** sidebar header. Select a **Type** to reveal the relevant configuration fields:

| Type | Description |
|------|-------------|
| ⚡ Lightning alert | Persistent MQTT listener on Blitzortung; strikes reduced to proximity / activity / closing-speed observables driving a 🟡/🔴/✅ state machine with hysteresis |
| 🌍 Seismic live | Polls INGV GOSSIP every 60 s; alerts on new events and state promotions |
| ⚽ Football Betting | Polls RapidAPI every 5 min (clock-aligned); alerts on statistically favourable live-match conditions |

All types share **Name**, **Enabled**, and **Telegram bot** fields. Additional fields are type-specific (see sections below).

There is no cron field and no "run now" action — the monitor is always-on when enabled. On disconnect, event-based monitors (Lightning, Seismic) reconnect automatically after a short delay.

---

## ⚡ Lightning Alert

Subscribes to the geohash-based MQTT topics covering the configured location; the subscribed area widens automatically with `radius_km`. Incoming strikes are buffered for **15 minutes** as `(time, lat, lon)` — without their distance, which is derived at evaluation time against the current origin.

Every 2 minutes the buffer is reduced to **three observables**:

| Observable | Definition | Why |
|------------|-----------|-----|
| **d10** | 10th percentile of the individual strike distances | A percentile, not a minimum: one stray strike cannot move it |
| **r_near** | Strikes/min within `d10 + 15 km` | The activity of the relevant storm body, self-scaling with distance |
| **v_c** | Closing speed, from the shift of the strike-field centroid between the older and newer half of the window | The motion of a *mass* of strikes, immune to individual cells appearing and disappearing |

`d10` and `v_c` are EMA-smoothed; an **ETA** is only computed once the field has been closing for 3 consecutive polls.

> **Why the change (v3.3.0):** the previous design drove the state machine with the distance of the nearest DBSCAN cluster centroid — a minimum over an unstable set, recomputed from scratch every poll with `min_samples=2`. Two stray strikes forming closer than the current storm made that scalar jump (e.g. 60 → 20 km) in one step, which read as a fast approach and fired a **WARNING for a storm that did not exist**. And de-escalation required *zero* clusters anywhere in the radius, so activity 70 km away kept the **all-clear from ever being sent**. Both symptoms came from the same design error: escalation and de-escalation were evaluated on different variables, so they were not complements and the machine had states it could not leave. No threshold tuning could fix that.

### Threat Levels

Thresholds shown for the default *Medium* sensitivity.

| Level | Enter | Exit |
|-------|-------|------|
| 🟡 WATCH | `d10 ≤ 40 km` and `r_near ≥ 0.20/min` | `d10 ≥ 55 km` or `r_near < 0.07/min`, held 20 min → ✅ |
| 🔴 WARNING | `r_near ≥ 0.50/min` **and** (`d10 ≤ 15 km` or confirmed ETA ≤ 25 min within 45 km), held 2 polls | `d10 ≥ 22 km` and `v_c ≤ 3 km/h`, held 10 min → 🟡 |

Enter and exit conditions use the **same** variables with strictly separated thresholds (a Schmitt trigger), and every transition carries a dwell time. That is what makes ✅ always reachable — a storm simply moving away is enough — and what keeps 🔴 from firing on activity that is not actually approaching.

### Sensitivity

| | Bassa | Media *(default)* | Alta |
|---|---|---|---|
| WATCH enter / exit | 30 / 45 km | 40 / 55 km | 55 / 70 km |
| WARNING enter / exit | 10 / 16 km | 15 / 22 km | 22 / 30 km |
| WARNING min. activity | 0.80/min | 0.50/min | 0.30/min |
| Max ETA | 20 min | 25 min | 35 min |
| All-clear dwell | 25 min | 20 min | 15 min |

### Alert Triggers (level-based)

| Event | Trigger | Icon |
|-------|---------|------|
| Watch | Activity enters the WATCH range | 🟡 |
| Warning | Storm already close, or a confirmed approach with a short ETA | 🔴 |
| Periodic re-alert | Every 10 min while in WARNING | 🔴 |
| De-escalation | Storm pulls away past the exit threshold | 🟡 |
| All clear | Exit condition held for the all-clear dwell | ✅ |

Alerts fire **only on level change** (plus the periodic WARNING re-alert). The state machine advances only on a confirmed Telegram send, so a dropped message is retried rather than lost.

**Quiet hours** silence 🟡 WATCH and ✅ CLEAR only — a 🔴 WARNING is always delivered.

**State survives restarts.** The threat level is persisted to `/data/lightning_state.json` and restored on startup (if less than an hour old), so a storm in progress does not lose its all-clear when the add-on restarts. Saving an unrelated live monitor no longer restarts the lightning monitor either — only a change to its own configuration does.

### Tuning on Real Storms

Enable **Record strikes** and every received strike is appended to `/data/lightning_rec/<id>-<date>.ndjson` (daily rotation, 7-day retention). Replay a recording through the exact same decision code, and compare all three presets on it:

```
cd /app/dradis && python3 -m live_monitors.replay \
    /data/lightning_rec/abc-2026-08-09.ndjson --monitor abc --compare
```

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

**All clear:** the message states *why* it cleared, since a storm can either move off or die out.
```
✅ Storm threat cleared — Bacoli
🔇 Remaining activity 62 km away, moving off
🕐 15:10
```

### Example Configuration

```
Name:        Bacoli Lightning
Type:        ⚡ Lightning alert
Location:    Bacoli
Radius:      50 km
Sensitivity: 🟠 Media
Language:    🇮🇹 Italiano
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
5. The **losing team's next-goal odds are below the configured maximum** (default `2.0`) — filters out long-shot signals

This combination identifies matches where the statistics and betting market both suggest the losing team has the momentum to equalise — a classically exploitable live-betting signal.

**Requires:** `rapidapi_football_key` in the HA Configuration tab (available from [RapidAPI](https://rapidapi.com/fluis.lacasse/api/football-betting-odds1)).

### Configuration Fields

| Field | Description |
|-------|-------------|
| Minute windows | Select one or both: **55′–65′** and **75′–81′**. Both are enabled by default. Additional windows are planned for a future release. |
| Maximum odds | Alert only when the losing team's next-goal odds are below this value (default `2.0`). The 🔍 Test API table honours the same cap. |
| API pause | Time range during which API calls are suppressed (default 23:00–07:00, evaluated in the configured timezone). Avoids unnecessary API usage overnight. Leave blank to disable. |

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
- Goal-difference threshold (e.g. allow alerts when difference == 2)
- League or competition filter

### Example Configuration

```
Name:          Football Betting
Type:          ⚽ Football Betting (RapidAPI)
Minute windows: 55′–65′ ✅  75′–81′ ✅
Maximum odds:  2.0
API pause:     23:00 – 07:00
Telegram bot:  default
```
