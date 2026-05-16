# Live Monitors

Live monitors are persistent push-based integrations that stay connected to an external data source and react to events in real time — no cron schedule, no polling interval. They run as always-on asyncio tasks. Live monitors are stored in `/data/live_monitors.json`.

## Creating a Live Monitor

Click `+` in the **Live Monitors** sidebar header.

| Field | Description |
|-------|-------------|
| Name | Display name shown in the sidebar. |
| Enabled | When enabled, DRADIS starts the listener at startup (or immediately on save). Green dot in sidebar. |
| Type | **⚡ Lightning alert** or **🌍 Seismic live**. |
| Location | City name resolved to coordinates via Open-Meteo geocoding (used for distance calculations). *(Lightning only)* |
| Alert language | 🇮🇹 Italiano (default) or 🇬🇧 English. |
| Radius (km) | Alert threshold distance from configured location (1–500 km, default 100). *(Lightning only)* |
| Status badge | 🟢 Running / 🔴 Stopped — fetched live from the backend. |

There is no cron field and no "run now" action — the monitor is always-on when enabled. On disconnect, it reconnects automatically after a short delay.

---

## ⚡ Lightning Alert

Subscribes to geohash-based MQTT topics covering the configured location and its 8 neighbouring cells. Incoming strikes within `radius_km` are collected in a **15-minute sliding window**. Every 2 minutes a polling task runs **pure-Python DBSCAN** (eps = 8 km, min_samples = 2) to identify distinct storm cells, tracks each cell's centroid over a 20-minute rolling history, and classifies each cell as APPROACHING / RETREATING / STATIONARY / UNKNOWN. Multiple simultaneous storms are tracked independently.

### Storm States

| State | Condition |
|-------|-----------|
| APPROACHING | Last 3 centroid distances all decreasing by > 0.5 km/sample |
| RETREATING | Last 3 centroid distances all increasing by > 0.5 km/sample |
| STATIONARY | Trend below threshold |
| UNKNOWN | Fewer than 3 centroid history samples (insufficient data — no alert sent) |

### Alert Triggers (zone-based)

| Event | Trigger | Icon |
|-------|---------|------|
| Initial detection | New storm cell appears within radius | ⚡ |
| Zone approaching | Cell crosses a zone boundary inward (<15 / 15–30 / 30–50 km) | 🔴 |
| Zone retreating | Cell crosses a zone boundary outward (RETREATING state) | 🟢 |
| Periodic re-alert | Every 10 min while APPROACHING | 🔴 |
| All clear | No strikes for 15 consecutive minutes | ✅ |

Hard cooldown of 5 minutes between any two alerts for the same cluster.

### Alert Examples

**Initial detection:**
```
⚡ Storm detected — Bacoli
📍 Distance: 48.2 km to NW (315°)
🏷 Zone: Distant zone (>50 km)
🟡 Status: Undetermined
🕐 14:20
```

**Zone approaching:**
```
🔴 Storm approaching — Bacoli
📍 Distance: 28.3 km to NW (315°)
🏷 Zone: Near zone (15–30 km)
🚀 ~42 km/h — estimated arrival: 40 min
🕐 14:32
```

**All clear:**
```
✅ Storm cleared — Bacoli
🔇 No lightning in the last 15 min
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
