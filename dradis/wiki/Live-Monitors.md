# Live Monitors

Live monitors are persistent push-based integrations that react to external events in real time — no cron schedule, no LLM, no token cost. They run as always-on asyncio tasks. Live monitors are stored in `/data/live_monitors.json`.

## Creating a Live Monitor

Click `+` in the **Live Monitors** sidebar header. Select a **Type** to reveal the relevant configuration fields:

| Type | Description |
|------|-------------|
| 🌩️ Storm front / CBDR | Persistent MQTT listener on Blitzortung; strikes binned into rings × sectors, each sector's leading edge drives a bounded ladder of alerts; says whether the storm will hit you or pass by |
| 🌍 Seismic live | Polls INGV GOSSIP every 60 s; alerts on new events and state promotions |
| ⚽ Football Betting | Polls RapidAPI every 5 min (clock-aligned); alerts on statistically favourable live-match conditions |

All types share **Name**, **Enabled**, and **Telegram bot** fields. Additional fields are type-specific (see sections below).

There is no cron field and no "run now" action — the monitor is always-on when enabled. On disconnect, event-based monitors (Storm front, Seismic) reconnect automatically after a short delay.

---

## 🌩️ Storm Front / CBDR

Subscribes to the geohash MQTT topics covering the configured location. Strikes are buffered for **10 minutes** as `(time, lat, lon)` — without their distance, which is derived at evaluation time.

### The grid

Every 60 seconds the buffer is binned into **concentric rings × 12 sectors of 30°**. `(lat, lon) → (sector, distance)` is a pure function of the strike and the origin: no assignment step, no `min_samples`, no neighbour search, so **nothing can be re-labelled between two polls**. Two consecutive frames differ only by strikes that arrived and strikes that aged out.

Rings are proportional to the radius, so changing the radius does not change the shape of the ladder:

| Updates per storm | Ring edges at R = 30 km |
|---|---|
| 2 | 30 / 12 km |
| 3 | 30 / 16.5 / 7.5 km |
| 4 *(default)* | 30 / 19.5 / 12 / 6 km |

The feed observes to **1.6 × the radius**. Those strikes never alert; they populate the bearing history so the *first* message can already carry a track verdict.

### The front

Each sector's **front** is a low quantile of its distances, floored at the **3rd-nearest strike** of that sector. The floor is the important half: Blitzortung mislocates strikes by a few km routinely, and this makes it impossible for one or two phantom strikes to pull the front inward, however busy the sector. A sector needs 4 strikes to count at all.

Only the **dominant** sector — the active one with the nearest front — drives the ring, which is why several simultaneous cells can never produce two parallel streams of messages.

> **Why the change (v4.0.0):** the previous design reduced everything to `d10`, the 10th percentile of *all* strike distances in the radius. That is a function of the count ratio between cells, not of geometry: a very active storm 80 km away dominates the percentile, and when it dies `d10` collapses to 20 km although nothing moved. On top of that, WARNING latched — leaving it required distance *and* closing speed conditions but nothing about activity, so a weak cell parked between the thresholds re-alerted every 10 minutes for hours. And no version ever answered the question that actually matters: will it hit me, or pass by?

### CBDR — will it hit me?

The mariner's collision rule: *constant bearing, decreasing range*. A descending ring is the precondition; whether the bearing holds or rotates is the discriminant.

The rotation is converted into **sideways travel in kilometres**, because degrees are not comparable across distances — the same 25° swing is 12 km of sideways travel at 28 km out but only 2 km at 5 km out, so a degree threshold would call a storm about to hit you "a glancing pass" exactly when it is closest.

| Sideways travel | Verdict |
|---|---|
| ≤ 2.5 km | 🧭 Constant bearing — heading straight for you |
| 2.5 – 4 km | 🧭 Track not yet determinable |
| ≥ 4 km | 🧭 Glancing pass — going by to the *(side)* |

Calibrated over 175 seeded scenarios: **zero** head-on storms mislabelled as grazing, 73 of 75 grazing storms correctly identified.

**There is no ETA.** The only time figure in a message is the measured interval between two confirmed ring crossings ("from 27 to 18 km in 9 min"). The sequence of messages *is* the approach timeline.

### When it speaks

A ring is announced **at most once per event**. No periodic re-alert, no message on retreat. Two independent barriers: 15 % hysteresis on the ring index, and an announced ring is never announced again.

| Event | Trigger | Icon |
|-------|---------|------|
| Ring message | Front reaches a ring deeper than any announced, confirmed over 2 polls | ⛈️ ⚡ 🟠 🔴 |
| All clear | No activity inside the radius for 10 min, feed connected ≥ 2 min | ✅ |

**At most `ring_count` messages plus one all-clear per storm**, for any input. A direct hit is 5 messages; a glancing storm 2–3; a cell parked at 25 km for four hours, 1–2 and then silence.

### Two invariants

- **A · Bounded messages.** `notified_ring` increases strictly at every message and resets only when the event closes. The v3.3.0 field failure is not unlikely here, it is arithmetically impossible.
- **B · Every state has a reachable exit.** Only activity inside the radius holds an event open, over a sliding window with unconditional expiry. No exit condition mentions speed, ring, bearing, verdict or ETA — one variable holds the event open and it decays on its own.

Both are enforced by tests, including an exhaustive sweep proving no combination of state variables is a trap.

**Quiet hours** silence the outer rings and the all-clear; a front within 40 % of the radius always gets through. Suppressed alerts are still committed, so they do not queue up and arrive as a burst.

**State survives restarts.** Persisted to `/data/storm_front_state.json` and restored if under 30 minutes old, so a storm in progress does not re-announce rings it already announced. Saving an unrelated live monitor does not restart the monitor — only a change to its own configuration does.

**If the feed goes down** everything freezes: no alerts, and the all-clear countdown restarts on reconnect. A dead socket and a clear sky look identical if you only count strikes, so the monitor refuses to guess.

### The radar

Every ring message carries a polar plot — rings and sectors *are* the model, so it is a literal photograph of the monitor's state: strikes coloured and sized by age (the direction of travel is visible at a glance), the dominant sector highlighted, the front marked, north up. Rendered on a worker thread in ~45 ms; any failure degrades to text only. Image and caption arrive as **one** Telegram message, so a ring alert notifies once, not twice.

### Alert Examples

**Ring:**
```
⚡ Temporale più vicino — Bacoli
📍 Fronte a 18 km a NO (307°)
🎯 Anello 2/4 · entro 20 km
🧭 Rotta costante: ti arriva addosso
⏱️ Da 27 a 18 km in 9 min
🔢 22 fulmini in 10 min (settore NO)
🕐 14:29
```

**Glancing pass:**
```
⛈️ Temporale nel raggio — Bacoli
📍 Fronte a 27 km a NO (312°)
🎯 Anello 1/4 · entro 30 km
🧭 Ti sfiora: passa a N
🔢 14 fulmini in 10 min (settore NO)
🕐 14:20
```

**All clear:**
```
✅ Temporale cessato — Bacoli
🔇 Nessuna attività entro 30 km da 10 min
📉 Massimo avvicinamento: 5 km (anello 4/4) alle 14:42
🕐 15:05
```

### Example Configuration

```
Name:              Bacoli Temporali
Type:              🌩️ Storm front / CBDR
Location:          Bacoli
Radius:            30 km      (10-60)
Updates per storm: 4
Radar:             on
Language:          🇮🇹 Italiano
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
