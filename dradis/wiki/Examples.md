# Usage Examples

## Voice appointment
*Requires: Voice + Google Calendar*

Send a Telegram voice message:
> 🎙️ *"Add a meeting with Marco on Friday at 3pm"*

DRADIS transcribes the audio via Groq Whisper, interprets the request, creates the event in Google Calendar, and confirms via Telegram.

---

## Weather query
> *"What's the weather in Milan tomorrow?"*

DRADIS calls the Weather tool (Open-Meteo, no API key needed) and replies with current conditions and a multi-day forecast including temperature, rain probability, wind, and UV index.

---

## Web search
> *"What are the latest Home Assistant announcements?"*

DRADIS calls the Web Search tool (Tavily), retrieves up to 5 results, and sends a concise summarised answer.

---

## Read a specific URL
> *"Summarise this article: https://www.example.com/article"*

DRADIS calls `read_url` directly via Jina Reader. The page content is fetched (max 8 000 characters) and analysed with its own model. No API key required, no extra LLM call.

---

## Storm front *(live monitor — no cron, no LLM, no token cost)*

DRADIS keeps a persistent MQTT connection to Blitzortung. Every 60 seconds the last 10 minutes of strikes are binned into concentric rings × 12 sectors, and each sector's leading edge — its *front* — drives the alerts. No API polling, no cron, no LLM.

A ring is announced **at most once per storm**, so one storm produces at most 4 messages plus an all-clear — never a stream. Each message says whether the storm is on a collision course or will pass by (CBDR), and carries a polar radar. See [Live-Monitors](Live-Monitors) for the full algorithm.

| Field | Value |
|-------|-------|
| Type | 🌩️ Storm front / CBDR |
| Location | Bacoli (auto-resolves to lat/lon) |
| Radius | 30 km |
| Updates per storm | 4 |
| Radar | on |
| Language | 🇮🇹 Italiano |

Sample alert (second ring, storm on a collision course):
```
⚡ Temporale più vicino — Bacoli
📍 Fronte a 18 km a NO (307°)
🎯 Anello 2/4 · entro 20 km
🧭 Rotta costante: ti arriva addosso
⏱️ Da 27 a 18 km in 9 min
🔢 22 fulmini in 10 min (settore NO)
🕐 14:29
```

Sample alert (storm that will miss):
```
⛈️ Temporale nel raggio — Bacoli
📍 Fronte a 27 km a NO (312°)
🎯 Anello 1/4 · entro 30 km
🧭 Ti sfiora: passa a N
🔢 14 fulmini in 10 min (settore NO)
🕐 14:20
```

Sample alert (all clear):
```
✅ Temporale cessato — Bacoli
🔇 Nessuna attività entro 30 km da 10 min
📉 Massimo avvicinamento: 5 km (anello 4/4) alle 14:42
🕐 15:05
```

---

## Daily thunderstorm risk digest *(scheduled monitor)*

Every morning DRADIS fetches atmospheric instability data for the next 2 days and sends a convective risk summary divided by time band — with no LLM call, no token cost, and deterministic output.

| Field | Value |
|-------|-------|
| Monitor type | ⛈️ Thunderstorm risk (Open-Meteo) |
| Location | your city (e.g. *Bacoli*) |
| Forecast days | 2 |
| Cron | `0 7 * * *` |

The Telegram message shows one line per time band (NIGHT / MORNING / AFTERNOON / EVENING) with CAPE, Lifted Index, CIN, wind gusts, precipitation probability, and a risk level (🟢 LOW · 🟡 MODERATE · 🟠 HIGH · 🔴 SEVERE).

---

## Hourly rain alert *(scheduled monitor)*

Check every hour whether rain is expected in the next 2 hours. No notification is sent when skies are clear — only when precipitation is actually forecast.

| Field | Value |
|-------|-------|
| Monitor type | 🌧️ Rain alert (Open-Meteo) |
| Location | your city (e.g. *Bacoli*) |
| Hours ahead | 2 |
| Cron | `0 * * * *` |

The Telegram message lists each 15-minute slot with the expected precipitation in mm (🔵 rainy / ⚪ dry) and the total at the end.

---

## Daily appointments digest *(scheduled task)*

Every morning DRADIS sends a Telegram message with your Google Calendar events for the day.

| Field | Value |
|-------|-------|
| Cron | `0 8 * * *` |
| Instructions | `Fetch today's calendar events and send a tidy summary to Telegram.` |

*Requires: Google Calendar enabled.*

---

## Morning news briefing *(scheduled task)*

Every weekday morning DRADIS searches the web for the latest tech news and delivers a digest.

| Field | Value |
|-------|-------|
| Cron | `0 7 * * 1-5` |
| Instructions | `Search for today's top technology news and send a short summary to Telegram.` |

*Requires: Web Search enabled.*

---

## Morning email digest *(scheduled task)*

Every weekday morning DRADIS checks unread emails and sends a summary to Telegram.

| Field | Value |
|-------|-------|
| Cron | `0 8 * * 1-5` |
| Instructions | `Check unread emails and send a brief summary of each (sender, subject, key points) to Telegram.` |

*Requires: Gmail enabled.*

---

## Email-to-calendar sync *(scheduled task)*

Every 12 hours DRADIS scans recent emails for deadlines or appointments and creates the corresponding Google Calendar events.

| Field | Value |
|-------|-------|
| Cron | `0 */12 * * *` |
| Instructions | `Read all emails received in the last 12 hours, including those with no subject. Ignore any automated notifications sent by Google Calendar itself. For each email that mentions a deadline, meeting, appointment, or event with a specific date and time, create the corresponding event in Google Calendar. Do not send any summary to Telegram — just create the events silently.` |

*Requires: Gmail and Google Calendar both enabled.*

---

## Aviation TAF briefing *(scheduled task)*

Every morning DRADIS fetches the Terminal Aerodrome Forecast (TAF) for a configured airport, decodes the encoded meteorological notation, and sends a plain-language summary to Telegram.

| Field | Value |
|-------|-------|
| Cron | `0 6 * * *` |
| Instructions | `Fetch the latest TAF for airport ICAO code LIRN (replace with your airport). Search the web for "TAF LIRN site:aviationweather.gov" or use https://aviationweather.gov/api/data/taf?ids=LIRN&format=json to get the raw forecast. Decode the TAF and send a clear plain-language summary to Telegram: validity period, wind (direction and speed in knots), visibility, significant weather (rain, thunderstorms, fog, snow), and cloud layers (few/scattered/broken/overcast). Highlight any conditions below VFR minimums (visibility < 5 km or ceiling < 1 500 ft).` |

*Requires: Web Search enabled.*
*Replace `LIRN` with the ICAO code of your airport (e.g. `EGLL` for London Heathrow, `KJFK` for New York JFK, `LFPG` for Paris CDG).*

---

## Task management *(Google Tasks)*

> *"Add buy milk and call the doctor"*
> → DRADIS creates two tasks in your Google Tasks list and confirms.

> *"What do I have to do?"* → Lists all open tasks with IDs.

> *"Mark task 2 as done"* → Marks the task as completed.

*Requires: Google Tasks enabled.*
