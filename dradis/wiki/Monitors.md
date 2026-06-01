# Scheduled Monitors

Scheduled monitors fetch data from external APIs and compute results entirely in Python, then deliver them to your Telegram chat on a cron schedule. **No LLM is invoked by default** — output is deterministic and costs zero tokens. Monitors are stored in `/data/monitors.json`.

## Alert Modes

Each monitor supports two alert modes:

| Mode | Description |
|------|-------------|
| **Direct Telegram** (default) | Sends the report immediately without an LLM call. Zero token cost. |
| **LLM (Call DRADIS)** | Passes the generated report to the full DRADIS agent together with custom instructions. The agent can send Telegram messages, emails, create tasks, etc. Consumes tokens. |

## Creating a Monitor

Click `+` in the **Scheduled Monitors** sidebar header.

| Field | Description |
|-------|-------------|
| Name | Display name shown in the sidebar. |
| Enabled | Green dot in sidebar when active. |
| Monitor type | **⛈️ Thunderstorm risk**, **🌧️ Rain alert**, **🌍 Seismic report**, or **☁️ Google Drive Backup**. |
| Response language | 🇮🇹 Italiano (default) or 🇬🇧 English. |
| Location | City name — resolved to coordinates via Open-Meteo geocoding. Live hint shows coordinates as you type. Not used for seismic type (uses area checkboxes instead). |
| Cron expression | 5-part cron with live validation and next-fire preview. |

---

## ⛈️ Thunderstorm Risk Monitor

Fetches atmospheric instability data from [Open-Meteo](https://open-meteo.com) (free, no API key required) and computes a **Thunderstorm Risk Score (TRS)** for each time band of each forecast day. All computation is in Python — no LLM, zero token cost.

**Additional fields:**

| Field | Default | Description |
|-------|---------|-------------|
| Forecast days | 2 | Number of days to fetch (1–7). |

**Variables fetched (hourly):** CAPE, Lifted Index (LI), Convective Inhibition (CIN) — all provided directly by Open-Meteo, no pressure-level variables required.

**Risk formula — multiplicative composite (TRS ∈ 0.0–1.0):**

```
TRS = CAPE_norm × LI_norm × CIN_norm
```

The multiplicative structure means that if any single ingredient is absent the score collapses to zero.

| Component | Normalisation | Notes |
|---|---|---|
| CAPE_norm | `min(CAPE / cape_sat, 1.0)` | Mediterranean default: 1200 J/kg |
| LI_norm | `min(max(−LI / li_sat, 0), 1.0)` | LI −3°C = 60%; Mediterranean default sat. 5°C |
| CIN_norm | `max(1 − \|CIN\| / cin_supp, 0.0)` | Mediterranean default ceiling: 100 J/kg |

**Climate calibration** — the three saturation constants are saved per monitor and auto-populated from the location's country when a location is resolved in the UI:

| Preset | Countries | CAPE sat. | LI sat. | CIN ceiling |
|--------|-----------|-----------|---------|-------------|
| Mediterranean | IT ES GR HR PT TR … | 1200 J/kg | 5.0°C | 100 J/kg |
| Continental | DE AT CH FR PL … | 1500–1800 J/kg | 5.0°C | 110–120 J/kg |
| Northern Europe | GB IE NO SE FI … | 400–700 J/kg | 3.5–4.0°C | 60–80 J/kg |

**Risk levels:**

| TRS | Level |
|---|---|
| 0.0 – 0.2 | 🟢 NEGLIGIBLE |
| 0.2 – 0.4 | 🟡 LOW |
| 0.4 – 0.6 | 🟡 MODERATE |
| 0.6 – 0.8 | 🟠 HIGH |
| 0.8 – 1.0 | 🔴 VERY HIGH |

The Telegram message shows one line per time band (00–06 / 06–12 / 12–18 / 18–24) with TRS score and risk label. Each day ends with the daily peak risk.

**Example configuration:**

```
Name:           Daily thunderstorm risk
Type:           ⛈️ Thunderstorm risk
Location:       Bacoli
Forecast days:  2
Cron:           0 7 * * *
```

**Example output:**
```
⛈️ Thunderstorm Monitor — Bacoli
📍 40.7967, 14.0735 | Forecast 2 days
🕐 01/06/2026 07:00 (Europe/Rome)

📅 1 June 2026
  00–06  🟢 NEGLIGIBLE  0.04
  06–12  🟢 NEGLIGIBLE  0.08
  12–18  🟡 LOW  0.24
  18–24  🟡 MODERATE  0.41
➤ Peak risk: 🟡 MODERATE  (0.41)
```

---

## 🌧️ Rain Alert Monitor

Fetches 15-minute precipitation data from [Open-Meteo](https://open-meteo.com) for the next 24 hours and checks whether rain is forecast within the configured time window. **Silent when skies are clear** — no Telegram message is sent if no rain is expected.

**Additional fields:**

| Field | Default | Description |
|-------|---------|-------------|
| Hours ahead | 2 | How many hours ahead to check for rain (1–24). |

**Example configuration:**

```
Name:        Hourly rain check
Type:        🌧️ Rain alert
Location:    Bacoli
Hours ahead: 2
Cron:        0 * * * *
```

---

## 🌍 Seismic Report Monitor

Fetches seismic event data from the [INGV GOSSIP JSON API](https://terremoti.ov.ingv.it/gossip) for one or more volcanic/seismic areas. Sends a statistical Telegram report with the total event count (automatic vs revised) and two histogram distributions (magnitude and depth). No LLM used.

**Additional fields:**

| Field | Description |
|-------|-------------|
| Area checkboxes | One or more areas: Campi Flegrei, Vesuvio, Isola di Ischia, Golfo di Napoli. |
| Time range | From start of day / Last 24 hours / From start of week / Last 7 days / From start of month / Last month / From start of year / Last year. |

The report includes:
- Total events (automatic / revised)
- Magnitude histogram (n/a · <0 · 0–0.9 · 1–1.9 · 2–2.9 · 3–3.9 · 4+)
- Depth histogram (0–1 · 1–2 · 2–3 · … km, with per-bin event count and maximum Md)
- Event list: up to 80 events, one per line (magnitude icon, local datetime, Md, depth, status)

**Icons:**

| Icon | Meaning |
|------|---------|
| ⚠️ | Automatic (preliminary, may be revised) |
| ✅ | Revised (final, manually reviewed by INGV) |

**Example configuration:**

```
Name:       Daily seismic — Campi Flegrei
Type:       🌍 Seismic report
Areas:      Campi Flegrei
Time range: Last 24 hours
Cron:       0 8 * * *
```

---

---

## ☁️ Google Drive Backup Monitor

Uploads all sensitive DRADIS configuration files to a "DRADIS Backup" folder on Google Drive on a cron schedule. Uses `drive.file` OAuth scope — DRADIS can only access files it created; no full Drive access is required.

**Files backed up:**

| File | Content |
|------|---------|
| `options.json` | Add-on configuration (all API keys and tokens) |
| `dradis_settings.json` | Runtime settings from the Web UI |
| `google_calendar_token.json` | Google Calendar OAuth2 token |
| `google_gmail_token.json` | Gmail OAuth2 token |
| `google_tasks_token.json` | Google Tasks OAuth2 token |
| `gdrive_backup_token.json` | Google Drive Backup OAuth2 token |
| `tasks.json` | Scheduled tasks |
| `monitors.json` | Scheduled monitors |
| `live_monitors.json` | Live monitors |
| `ha_monitors.json` | HA monitors |
| `agents.json` | Custom agents |

**Setup:**

1. Send `/backupauth` to the Telegram bot and complete the OAuth2 flow.
2. Create a monitor of type ☁️ Google Drive Backup with your preferred cron schedule.
3. No location field is required.

**Restore procedure:** download the files from the "DRADIS Backup" folder on Google Drive and place them in `/data/` inside the HA add-on container.

**Example configuration:**

```
Name:     Weekly Drive backup
Type:     ☁️ Google Drive Backup
Cron:     0 6 * * 1
```

The Telegram message confirms how many files were uploaded or updated and lists any files that were skipped (not found on disk).

---

## Testing and Duplicating

- **▶ Test Monitor**: triggers an immediate execution. Result delivered to Telegram within seconds.
- **⎘ Copy**: creates a copy named `Copy of <name>`, disabled by default. Useful for the same monitor type at multiple locations or with different schedules.
