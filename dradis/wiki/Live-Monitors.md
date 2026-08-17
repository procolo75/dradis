# Live Monitors

Live monitors are persistent push-based integrations that react to external events in real time — no cron schedule, no LLM, no token cost. They run as always-on asyncio tasks. Live monitors are stored in `/data/live_monitors.json`.

## Creating a Live Monitor

Click `+` in the **Live Monitors** sidebar header. Select a **Type** to reveal the relevant configuration fields:

| Type | Description |
|------|-------------|
| 🌩️ Storm front / CBDR | Persistent MQTT listener on Blitzortung; strikes binned into rings × sectors, each sector's leading edge drives a bounded ladder of alerts; says whether the storm will hit you or pass by |
| 🌧️ Rain front | Polls the Protezione Civile national radar composite (free, no API key, Italy only); same ring ladder, but the rain's own drift is measured so the alert can say in how many minutes it reaches you and by how much it misses |
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

### Checking it without waiting for a storm

`/storm` on Telegram replies with the polar chart this monitor would send right now, plus where it thinks it is and how many strikes it is holding. Unlike `/rain` it needs the monitor to be running — lightning is only buffered while the subscription is up — and it changes nothing. See [Telegram-Commands](Telegram-Commands#rain-and-storm).

### Where to watch — a fixed place, or a phone

**Where to watch** chooses where the radar is centred. `📌 A fixed place` (the default, and what every monitor configured before v4.1.0 inherits) uses the Location field and never consults the position manager. Selecting a position instead makes the monitor follow that phone — and the Location field disappears, because it is no longer used for anything.

Nothing in the collision logic changed for this, and the reason is worth stating. The strike buffer holds **absolute** coordinates and the geometry is rebuilt against the current origin on every poll, so a moving origin simply produces a different — and correct — frame. CBDR then follows for free: it compares bearings and ranges *measured from the origin*, so a moving origin turns those into **relative** bearings and ranges, which is exactly what the mariner's rule was always defined on. Constant relative bearing with decreasing relative range means collision whether or not you are under way.

What is not free is telling **moving** apart from **being moved**:

- A continuous track is signal and is kept — that is the whole point.
- A **jump** is a change of reference frame: a mislocated fix, or the position returning after a blackout somewhere else entirely. The stored bearings were measured from somewhere else, so the CBDR history is dropped. The *event* stays open on purpose — reopening it would reset the notification ladder and let one storm emit a second full set of ring messages, which is precisely what invariant A exists to prevent.
- A single wild fix is **held until the next report agrees with it**, the same rule ring descents follow. One bad reading cannot move the radar 300 km.

When you are moving, ring alerts carry an extra line:

```
⚡ Temporale più vicino — Cellulare di Procolo
📍 Fronte a 18 km a N (0°)
🎯 Anello 2/4 · entro 20 km
🧭 Rotta costante: ti arriva addosso
🚗 Ti stai dirigendo verso il temporale a 90 km/h
🔢 22 fulmini in 10 min (settore N)
🕐 12:05
```

The 🚗 line appears when your course is within 45° of the front, otherwise `🚗 In movimento a 90 km/h verso NE`. It **explains** the track verdict above it rather than competing with it: the CBDR reading already accounts for your motion, so the two lines can never disagree. Below the GPS noise floor the monitor reports nothing at all rather than inventing a heading — a fabricated course on a 20 km lever arm is enough to claim you are driving into a storm.

The header is the **position's name**, not the configured place: it says where these distances were measured from, which is what you need to know when several phones are monitored.

#### With no usable position, it freezes

There is **no fallback**. When the fix is missing, too old or too imprecise — or the position was deleted — the monitor does not know where it is and therefore perceives nothing.

That is the same blindness it already handles when the strike feed drops, and it is handled the same way rather than with new state: no alerts, and **no all-clear**. A monitor that cannot tell "nothing is happening" from "I cannot see" would otherwise cheerfully report that the storm has cleared. The freeze is silent and lifts by itself when the position returns; the CBDR history is dropped at that point, since the bearings from before the blackout were measured wherever you were then.

While a storm is in progress the age budget is **tripled**, so losing GPS in a tunnel does not blind the monitor mid-event: the last known position is still the best evidence available.

Blitzortung topics are geohash cells (~110 km each) derived from the origin, so the subscription is rebuilt only when you travel far enough to change the cell set — in practice almost never. Without it the monitor would quietly stop hearing the sky you moved into while still reporting itself healthy. The strike buffer survives a re-aim: absolute coordinates stay true wherever you go. A monitor following a position connects on its **first fix** rather than at start-up, since until then it has nothing to derive topics from.

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
Where to watch:    📌 A fixed place   (or a named position)
Radius:            30 km      (10-60)
Updates per storm: 4
Radar:             on
Language:          🇮🇹 Italiano
```

**Travelling:**
```
⚡ Temporale più vicino — Cellulare di Procolo
📍 Fronte a 18 km a N (0°)
🎯 Anello 2/4 · entro 20 km
🧭 Rotta costante: ti arriva addosso
🚗 Ti stai dirigendo verso il temporale a 90 km/h
🔢 22 fulmini in 10 min (settore N)
🕐 12:01
```

---

## 🌧️ Rain Front

Watches the Italian national weather radar and tells you when rain will reach you.

It is the twin of the Storm front: same rings, same bounded ladder of messages, same "follow a phone or watch a fixed place" choice. The difference is what it looks at, and what it can therefore tell you.

| | Storm front | Rain front |
|---|---|---|
| Watches | Lightning strikes | Rain on the radar |
| Source | Blitzortung (MQTT) | Protezione Civile radar |
| API key | none | none |
| Coverage | worldwide | **Italy only** |
| Answers | Will the storm hit me or pass by? | *When* will the rain reach me, and by how much will it miss? |

### What you actually receive

Rain approaching you produces a short ladder of messages — one each time it crosses into a closer ring, then an all-clear once it has gone. **At most 4 messages plus the all-clear**, for any weather whatsoever. Rain that stalls 25 km away all afternoon is mentioned once and then never again.

```
🌧️ Pioggia nel raggio — Casa
📍 Fronte a 26 km a O (270°)
🌧️ Intensità massima 18.0 mm/h (forte)
🎯 Anello 1/4 · entro 30 km
🧭 Rotta d'incontro: ti raggiunge fra 26 min
🌬️ La pioggia si muove verso E a 60 km/h
🔢 241 km² di pioggia nel settore O
📡 Radar delle 17:00 (10 min fa)
🕐 17:10
```

The 🧭 line is the one the whole monitor exists for, and it is **measured, not forecast** — see [Why it can give you a time](#why-it-can-give-you-a-time) below.

When the rain has no measurable direction of travel, the monitor says so instead of inventing a number:

```
🟠 Pioggia vicina — Casa
📍 Fronte a 11 km a SO (225°)
🌧️ Intensità massima 4.7 mm/h (moderata)
🎯 Anello 3/4 · entro 12 km
🧭 Traiettoria non ancora determinabile
🌬️ Movimento della pioggia non misurabile
📡 Radar delle 17:05 (10 min fa)
🕐 17:15
```

And when it is over:

```
✅ Pioggia cessata — Casa
🔇 Niente pioggia entro 30 km da 10 min
📉 Massimo avvicinamento: 7 km (anello 3/4) alle 16:40
📡 Radar delle 17:25 (10 min fa)
🕐 17:35
```

### Settings

| Field | What it does |
|-------|--------------|
| **Where to watch** | A fixed place, or a phone to follow — identical to the Storm front, including the rule that a monitor following a phone has *no* fallback and freezes when the position is lost. |
| **Alert radius** | 10–60 km. At 30 km you get roughly half an hour of warning for a front moving at 60 km/h. |
| **Updates per rain event** | Hard cap of 2, 3 or 4 messages per event. Not a sensitivity setting — it changes how often you are told, never what the monitor decides. |
| **Rain worth telling you about** | The minimum intensity that counts. See the table below. |
| **Also mention hail** | Adds a line when the approaching rain carries a real risk of hail. |
| **Radar picture** | Attaches the radar image to each message. |

#### Choosing the minimum intensity

The radar sees everything, down to a damp mist. This is where you decide what deserves a notification.

| Setting | Meaning | Good for |
|---|---|---|
| `0.2 mm/h` | Even drizzle | You want to know about anything at all |
| **`1 mm/h`** *(default)* | Rain you would notice walking to the car | Most people |
| `4 mm/h` | A real shower | You only care if it is worth waiting out |
| `10 mm/h` | Heavy rain only | Washing on the line, driving alerts |

Set it too low and a grey afternoon keeps the event open for hours. Too high and light rain arrives unannounced.

#### 📡 Test radar coverage

**The radar network does not cover every corner of Italy.** This button fetches the newest radar image on demand and tells you whether the service is reachable, how old the image is, **how much of the area you asked to watch is actually visible to the radar**, and what it is doing there right now.

That coverage figure is the one that matters. A monitor watching a blind spot would report calm forever, and there is no way to notice that from the outside. Pantelleria, for example, is genuinely outside the network and is reported as such.

### Where the data comes from

The [Dipartimento della Protezione Civile radar platform](https://dpc-radar.readthedocs.io/) publishes a combined image from the 24 national radars. It is Open Access — **no key, no registration, no quota**.

| | |
|---|---|
| What is downloaded | `SRI`, rainfall intensity at ground level, directly in mm/h |
| Size | One 1200 × 1400 image covering the whole country, 1 km per pixel |
| How often | Every 5 minutes |
| Also, if hail is enabled | `POH`, the probability of hail |

Because one image covers all of Italy, a second monitor watching a different town **costs nothing extra**: the download is shared between every rain front monitor, and stops entirely when the last one is disabled.

> **A blind spot is not a dry spell.** Where the radar cannot see, the image contains a marker meaning *"no measurement"*, which the monitor never treats as an absence of rain. Confusing the two would turn the edge of the network into permanent sunshine.

### The radar is ten minutes behind

Every radar image becomes available **about ten minutes after the moment it describes**. This is how the service works, not a delay inside DRADIS, and two things follow from it.

- **Every message tells you when the radar looked**, not when the message was sent: `📡 Radar delle 17:00 (10 min fa)`. Anything else would fall apart the first time you looked out of the window.
- **The monitor compensates.** Rain moving at 60 km/h has travelled 10 km since the picture was taken, so when the drift is measurable the geometry is shifted forward by exactly that before any distance is reported.

DRADIS asks for a new image only when one is actually due — the schedule comes from the service's own declared cadence — so a cycle costs one small request plus one download.

### How often it checks

Three different rhythms, and it is worth not confusing them.

| What | How often | Why |
|---|---|---|
| **Decides** | every **60 s** | Because *you* may be moving. At 130 km/h you cover 2.2 km a minute, far faster than the sky changes. |
| Downloads | every 5 min | The rate at which new images exist. |
| Measures the drift | with each new image | Comparing two images is what reveals which way the rain is going. |

Deciding every minute is also what keeps the two-poll confirmation meaning **two minutes**, exactly as in the Storm front, rather than ten.

### Why it can give you a time

This is the one thing rain can do that lightning cannot.

Lightning is a scatter of individual strikes: you can see *where* they are, but a bag of dots has no direction of its own, so the Storm front has to infer the answer from how the bearing drifts over a quarter of an hour, and can only ever return a verdict — *hit* or *miss*.

Rain on the radar is a **picture**. Compare two consecutive pictures, find how far the pattern has shifted, and you have measured the rain's actual speed and direction over the ground. DRADIS already knows your own speed and direction from the position you follow. With both, the meeting point is simple geometry rather than guesswork:

> Subtract your motion from the rain's motion, and you get how the two are closing on each other. From that comes **how many minutes** until the moment of closest approach, and **how many kilometres** apart you will be at that moment. Zero kilometres means it lands on you.

That is why the alert can say *"ti raggiunge fra 26 min"* or *"ti sfiora: passa a N a 14 km, fra 21 min"*.

#### It refuses to invent one

Two pictures do not always give a clear answer. Scattered summer showers grow and die faster than they travel, and comparing them yields a smear rather than a shift.

So the measurement has to pass a test: the best match must stand clearly above the runner-up. This was calibrated against real radar images at four locations — every physically absurd result (195 km/h over Rome, contradicted by the same site's other readings) sat in one range, every result that agreed across different settings sat in a distinctly higher one, and the threshold sits in the empty gap between the two.

**On scattered convection this test fails often.** That is the honest answer rather than a shortcoming, and it is why the Storm front's bearing-drift method remains as the fallback: it needs no speed measurement at all, only the rotation of a bearing, which a moving observer supplies by itself. When neither can answer, the alert gives you distance and direction and no time at all.

#### "It is raining on you" is a measurement, not a ring

The innermost ring is a fifth of the radius — **4 km** with a 20 km radius — so a front reaching it is close, not overhead. Until 4.4.3 the alert said *"🔵 Pioggia su di te"* and *"🧭 Sei sotto la pioggia"* on that basis alone, and it was possible to receive it under a dry sky with the radar picture perfectly correct.

Now each alert also reads **the strongest intensity within 2 km of where you are**, and those two lines require it to reach the same threshold you set for the monitor. Otherwise the message keeps the ring, keeps the distance, and says nothing about your pavement:

```
🟠 Pioggia vicina — Casa
📍 Fronte a 3 km a NO (308°)
🌧️ Intensità massima 0.4 mm/h (pioviggine)
🌂 Solo pioviggine sul radar: a queste intensità può evaporare prima di toccare terra
🎯 Anello 4/4 · entro 4 km
```

**Why 2 km and not the pixel you are standing on.** The picture is 10–15 minutes old and is carried forward only when the drift was measurable — which at drizzle intensities it usually is not, and that is exactly when this check matters. Two kilometres covers the position error plus ten minutes of unresolved drift.

**Why drizzle gets a line of its own.** Below 0.5 mm/h the echo evaporates before reaching the ground more often than not (*virga*), and along a coast sea clutter and anomalous propagation add returns that were never rain. The radar is not wrong and neither are you: the alert now says so instead of leaving you to work it out from a dry window.

`/rain` reports the same reading — *"☂️ Su di te: 2.4 mm/h"* — so the diagnostic and the alert cannot tell you different things.

#### And it will not report a drift it cannot resolve

The image is 1 km per pixel and the pictures are 5 minutes apart, so **one pixel of movement is 12 km/h**. Anything slower than half a pixel between two frames is not a slow drift, it is a pattern that did not move: the best match sat where it started, and the direction attached to it comes entirely from the arithmetic that estimates fractions of a pixel — which always returns a number, including for noise.

Below that floor the monitor reports a standstill and no compass direction at all:

```
🌬️ Pioggia stazionaria
```

That line used to read *"La pioggia si muove verso NO a 3 km/h"*, printed under a front 19 km to the north-west that was closing — a drift pointing exactly away from the observer it then rained on.

**A still field and an advancing front are not a contradiction.** A large mass of light rain can sit almost motionless while its nearest edge grows towards you, and the two lines then say so together:

```
🌬️ Pioggia stazionaria
⏱️ Da 19 a 11 km in 14 min
```

The ⏱️ line measures the distance between you and the rain, which is what you asked about; the 🌬️ line measures the body of the rain against the ground. When the drift *is* resolved and does point away while the front keeps closing, the alert states the two as one fact instead of two:

```
🌬️ Il grosso della pioggia deriva verso NO a 20 km/h, ma il fronte continua ad avvicinarsi
```

The verdict on the 🧭 line stays with the bearing-drift method in that case. A ring descent is measured against *you*; the drift is measured against the previous picture, and only one of those two answers the question you asked.

### The picture attached to each alert

The Storm front draws a diagram of strikes, because lightning has no image of its own. Here the measurement **is** the image: the radar around you, coloured by intensity, with the rings drawn on it, the nearest edge of the rain marked, an arrow showing which way it is drifting, and a cross where it will come closest to you.

It is centred on the same point the numbers were worked out from, so the picture and the text always agree. Image and text arrive as **one** Telegram message, so your phone buzzes once. If the drawing fails for any reason the text is sent on its own — a picture must never be able to stop a warning.

### Three ways it goes blind

The monitor stops speaking — **no alerts, and importantly no all-clear** — whenever it cannot see:

| Situation | Meaning |
|---|---|
| The position it follows is missing or stale | It does not know where you are |
| No radar image newer than 25 minutes | It does not know what the sky is doing |
| Less than 40 % of the watched area is covered | It is looking into a blind spot |

All three are the same problem: *not knowing* is not the same as *nothing happening*. A monitor that cannot tell them apart would cheerfully announce that the rain had cleared.

### What it inherits from the Storm front

The ring ladder, the 15 % hysteresis, the two-poll confirmation, the event lifecycle, the all-clear dwell, the saved state and **invariant A** (one event can never emit more than `ring_count` messages plus one all-clear) are taken unchanged. None of that is specific to lightning, and it is the part that six generations of storm monitor paid for.

One thing could **not** be inherited: how the *nearest edge* of the weather is found.

> The Storm front takes a low percentile of the distances in each sector. That is right for lightning, where strikes are sparse and each one may be mislocated by kilometres. But rain fills a sector rather than dotting it, and a sector's area grows with distance, so a low percentile always lands about a third of the way out **no matter where the edge really is**.
>
> On a real image over Arezzo, the nearest rain was **3.7 km** away and that method reported **14.7 km** — it would have placed the rain outside the innermost ring while the user was getting wet.
>
> The rain front takes the fifth-nearest raining pixel instead: still immune to a stray speckle, and within about a kilometre of the true edge everywhere it was tested.

### Checking it without waiting for rain

`/rain` on Telegram replies with the picture this monitor would send right now, and a caption saying where it believes it is — coordinates, a map link, the age of the fix, and the drift if it is measurable. It works even while the monitor is switched off, and it changes nothing. See [Telegram-Commands](Telegram-Commands#rain-and-storm).

### Example Configuration

| Field | Value |
|-------|-------|
| Name | Rain — home |
| Type | 🌧️ Rain front (Protezione Civile radar) |
| Where to watch | Casa |
| Alert radius | 30 km |
| Updates per rain event | 4 |
| Rain worth telling you about | 1 mm/h |
| Also mention hail | ❌ |
| Radar picture | ✅ |

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
