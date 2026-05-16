# Troubleshooting

## DRADIS doesn't respond to Telegram messages

1. Check that `telegram_bot_token` and `telegram_allowed_chat_id` are set correctly in the Configuration tab.
2. Confirm the Telegram user ID is the one configured (send a message to [@userinfobot](https://t.me/userinfobot) to check).
3. Check the add-on log (HA → Add-ons → DRADIS → Log) for startup errors.
4. Confirm the add-on is running (green "Running" indicator in HA).

---

## "Model error" or empty replies

1. Verify the LLM provider API key is correct and has credits.
2. Try the **⚡ speed test** in the Web UI to check which models respond correctly.
3. Configure a **Fallback Provider / Fallback Model** — DRADIS will retry automatically on failure.
4. Check the add-on log for the error message from the provider.

---

## Cron jobs (tasks / monitors) fire at the wrong time

- Check the **Timezone** setting in **Settings → DRADIS**. All cron expressions are interpreted in the configured timezone.
- Use the live cron validator in the task/monitor form — it shows the next fire time in the configured timezone.
- APScheduler weekday convention: **0 = Monday … 6 = Sunday** (not Unix convention where 0 = Sunday).

---

## Monitor runs but no Telegram message is received

- Confirm the monitor is **Enabled** (green dot in sidebar).
- For rain monitors: no message is sent when no rain is expected. This is intentional.
- Check the add-on log for any HTTP or send errors.
- Use **▶ Test Monitor** to trigger an immediate run and see if the report arrives.

---

## Live monitor shows 🔴 Stopped

- Check the add-on log for MQTT connection errors.
- Verify the MQTT broker is running (Mosquitto add-on in HA).
- For Lightning monitor: verify the geohash topics are being published by the upstream data source.
- Try disabling and re-enabling the monitor to force a reconnect.

---

## Google Calendar / Gmail / Tasks OAuth

**"Authorization failed" on token fetch:**
- The redirect URL must be received within 5 minutes of sending `/gcalauth`.
- If the automatic redirect fails, copy the full URL from your browser and send it manually as `/gcalauth <url>`.

**Token expired / revoked (happens every 7 days if app is in Testing mode):**
- Go to [Google Cloud Console → APIs & Services → OAuth consent screen → Publishing status → Publish app](https://console.cloud.google.com).
- This makes the token permanent. No Google review required for personal use.
- After publishing, run `/gcalauth` (or `/gmailauth`, `/gtasksauth`) again to get a fresh permanent token.

**"Not authenticated" even after `/gcalauth`:**
- Check if the token file exists: `/data/google_calendar_token.json`.
- Verify `google_client_id` and `google_client_secret` in the Configuration tab are the correct values from Google Cloud Console.

---

## HA Monitor never triggers

1. Check that `mqtt_discoverystream_alt` is installed and configured with `publish_retain: true`.
2. Use **🔍 Discover** in the HA Monitor form — if no entities appear, the statestream is not publishing. Trigger a state change in HA and try again.
3. Verify the **Statestream prefix** matches `base_topic` in your `mqtt_discoverystream_alt` configuration.
4. Check the **State filter** field — if set, only the listed states trigger an alert.
5. Check the **Cooldown** setting — the same entity won't alert again until the cooldown expires.

---

## HA Monitor triggers spuriously on startup

This is the "retained message" behaviour: on MQTT connect the broker replays the last known state. DRADIS v2.15.8+ handles this correctly — it silently records the first retained state and only alerts on subsequent changes. If you're still seeing false alerts, check if there's an older version running.

---

## Web UI not loading

- Open the add-on in HA and click **Open Web UI** — this uses the correct HA Ingress URL.
- Do not access the Web UI directly via the add-on port from outside HA (the API paths are ingress-relative).
- If the page loads but settings don't save, check the add-on log for FastAPI errors.

---

## Logs

View the add-on log in **Home Assistant → Settings → Add-ons → DRADIS → Log**. Key log patterns:

| Log prefix | Meaning |
|------------|---------|
| `[DRADIS]` | General agent activity |
| `[Trajectory]` | Lightning monitor DBSCAN analysis result |
| `[Monitor]` | Scheduled monitor execution |
| `WARNING` | Non-fatal issue (MQTT disconnect, etc.) |
| `ERROR` | Requires attention |
