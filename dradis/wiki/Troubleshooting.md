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

## A task fails on the token limit — but only sometimes

Symptom: the same scheduled task, on the same page, is refused by Groq roughly every other run.

The 8K figure on the Groq free tier is **tokens per minute**, not the size of one request: a rolling 60-second budget counting every call a turn makes, prompt and completion together. A turn re-sends the whole conversation on each tool round, so the cost is cumulative, and one extra round can double it.

What to check in the logs — each round now prints its own line:

```
[DRADIS] round=0 prompt=980 completion=48 cumulative=1028 tpm_used=1028
[DRADIS] round=1 prompt=3540 completion=402 cumulative=4970 tpm_used=4970
```

- **A third and fourth round appearing on the failing runs but not the good ones** — the model is calling tools it does not need. Lower **Settings → DRADIS → Max tool rounds**, and check **Sampling temperature** is low: at the provider default the same prompt costs a different number of rounds every time.
- **`read_url` in `tools_used` twice** — the fetch failed and the model retried. You should also see a `⚠️` notice naming the HTTP status; if Jina is rate-limiting you often, that is the thing to fix.
- **A single round already near the ceiling** — the page is large. Lower **Max completion tokens**, or point the task's model at a provider with a roomier budget.
- **`pacing groq: waiting …`** — the runtime is holding a request back so it fits the minute. Expected on heavy tasks; latency, not an error.
- **"primary and fallback both failed"** right after a rate limit — the fallback shares the API key's budget. DRADIS now waits out the provider's retry hint before trying it, but a fallback on a *different* provider avoids the problem entirely.

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

## The same live-monitor alert arrived twice

Fixed in 4.4.2. Both copies were composed by the same monitor at two consecutive polls: the first send had in fact been delivered, but Telegram's answer never came back before the timeout, so the alert was recorded as undelivered and sent again a minute or two later.

**How to tell a retry from anything else, from the messages alone.** Compare the `⏱️` line of the two copies:

| What you see | What it means |
|---|---|
| `⏱️ Da 19 a 11 km in 14 min` in one, `… in 16 min` in the other | A retry. That figure counts from the last *confirmed* delivery, so a larger value in the second copy proves the first was never recorded as sent. |
| The identical `⏱️` value in both | Two separate monitors, each keeping its own books. Check the monitor list for a duplicate watching the same position. |

The same `📡 Radar delle 21:35` in both copies tells you only that no new image had arrived between them — every image is used for about five minutes, so it proves nothing either way.

In the log a retry looked like this:

```
[DRADIS] send_photo(bot_id='default') TimedOut after 60.1s: Timed out
[RainFront] 'Casa' alert NOT delivered — state held, retry next poll
```

Since 4.4.2 a timeout logs `delivery UNCONFIRMED — committed anyway` instead and no second message is sent. The `NOT delivered … retry next poll` line now appears only when the send genuinely failed — a rejected message, a blocked bot, an unreachable network — where retrying is the right answer.

---

## Live monitor shows 🔴 Stopped

- Check the add-on log for MQTT connection errors.
- Verify the MQTT broker is running (Mosquitto add-on in HA).
- For the Storm front monitor: verify the geohash topics are being published by the upstream data source.
- Try disabling and re-enabling the monitor to force a reconnect.

---

## Live monitor shows 🟠 Degraded

The task is alive but the feed is not delivering: either no message has arrived for 15 minutes, or reconnection keeps failing. Previously this state was indistinguishable from a quiet sky — the monitor reported 🟢 Running while silently receiving nothing.

- Check the log for repeated `disconnected: … retry in 15s` lines with a rising `failures=` count.
- The Blitzortung broker (`blitzortung.ha.sed.pl:1883`) is a public service; an outage there shows up exactly this way.
- Note that Degraded during genuinely calm weather is still meaningful — it means no strike data at all is arriving, not that there are no storms nearby.

---

## Storm front monitor says too much, too little, or the wrong thing

**Too many messages** is no longer possible: a ring is announced at most once per event, so a storm can produce at most `ring_count` messages plus one all-clear, whatever it does. If you are seeing more than that, you are seeing *separate events* — check the log for the all-clear between them.

**Too few messages.** Read the per-poll diagnostic line:

```
[StormFront] name | ring=2/4 notified=2 front=18.3km sec=10 act=3 n=22 evt=ACTIVE pend=-/0
```

- `front` is how close the leading edge of the nearest cell really is, `act` how many sectors are active, `n` how many strikes are inside the radius.
- A sector needs **4 strikes in 10 minutes** to count at all, and the front is never placed nearer than the 3rd-nearest strike of its sector. Isolated discharges are deliberately invisible.
- If `evt=IDLE` while you can see lightning, the activity is outside your radius — the monitor observes to 1.6 × the radius but only alerts inside it.

**It said "track not yet determinable".** That is the honest answer, not a bug. Far out, geometry makes every storm look head-on, and a bearing derived from a handful of discharges jitters by several degrees. The monitor declares the ambiguity rather than guessing, and the next ring usually settles it.

**It said "ti sfiora" and then it hit me.** Report this — it is the error the design works hardest to avoid, and it did not occur once in 100 simulated head-on storms. Include the log lines for the whole event.

**No radar image.** The chart is rendered on a worker thread and any failure degrades to text-only, by design. Look for `chart failed (…) — sending text only` in the log.

---

## A reply looks right but the data behind it is missing

A tool that fails no longer does so silently. **Settings → DRADIS → Report tool failures** (on by default) sends a `⚠️` message ahead of the reply naming what broke:

```
⚠️ Task Rassegna stampa: tool failure — the answer below may be incomplete.
• read_url: HTTP 429 from r.jina.ai reading https://example.com/article
• get_emails: Gmail not authenticated. Send /gmailauth to connect.
```

If you get an answer with no such warning, the tools did run. If you switched the setting off, you are back to being unable to tell a good run from a fabricated one — which is why it defaults to on.

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
| `[StormFront]` | Storm front ring, front distance and event state, one line per poll |
| `[Monitor]` | Scheduled monitor execution |
| `WARNING` | Non-fatal issue (MQTT disconnect, etc.) |
| `ERROR` | Requires attention |
