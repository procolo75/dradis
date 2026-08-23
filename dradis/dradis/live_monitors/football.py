"""
live_monitors/football.py
─────────────────────────
LLM-free live monitor: polls football-betting-odds1.p.rapidapi.com every 300s
and sends Telegram alerts when a losing team's next-goal odds are lower (better)
than the winning team's inside a configured minute window of the 2nd half.

Alert conditions (all must be true):
  - periodID == "3" (2nd half)
  - match minute inside an enabled window (bounds are configurable per monitor)
  - goal difference == 1
  - losing team's next-goal odds < winning team's next-goal odds
  - losing team's next-goal odds < that window's max_odds (0 = no cap)

There are two windows, "early" and "late". Their minutes and their odds caps are
per-monitor configuration; only the two ids are fixed, because they are what the
dedup key is built from.

Providers are tried in order (provider1→4); first successful response wins.
Deduplication: one alert per (match_id + window id). Key is pruned only when the
match disappears from the live feed (guard: only prune if feed returned results).
"""

import asyncio
import html
import json
import logging
from datetime import datetime
from typing import NamedTuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

import bot.state as _state

_LOGGER = logging.getLogger(__name__)

POLL_INTERVAL_SEC = 300

_BASE_URL  = "https://football-betting-odds1.p.rapidapi.com"
_PROVIDERS = ["provider1", "provider2", "provider3", "provider4"]

WINDOW_EARLY = "early"
WINDOW_LATE  = "late"

MINUTE_FLOOR   = 1
MINUTE_CEILING = 120

# id → (start, end, max_odds). The late band's 0.0 is not a placeholder: it is
# "no cap", which is what this monitor did before the cap became per-window, and
# so what every monitor saved before that keeps doing until its owner sets one.
_WINDOW_DEFAULTS: dict[str, tuple[int, int, float]] = {
    WINDOW_EARLY: (55, 65, 2.0),
    WINDOW_LATE:  (75, 81, 0.0),
}

# Monitors saved before the minutes were settable stored the bounds themselves as
# the window id ("55-65"). The id is stable now and the minutes are config, so an
# old label is read back as the band it used to name.
_LEGACY_WINDOW_IDS = {"55-65": WINDOW_EARLY, "75-81": WINDOW_LATE}


class WindowSpec(NamedTuple):
    id:       str
    start:    int
    end:      int
    max_odds: float   # 0 = no cap

    @property
    def label(self) -> str:
        return f"{self.start}'–{self.end}'"

    @property
    def cap_label(self) -> str:
        return f"max {self.max_odds:.2f}" if self.max_odds else "nessun max"


def _clamp_minute(value, default: int) -> int:
    """A minute of play, or the default when the config cannot say which one."""
    if value is None or value == "":
        return default
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(MINUTE_FLOOR, min(MINUTE_CEILING, parsed))


def _clamp_odds(value, default: float) -> float:
    """An odds cap. 0 is a real answer — it means no cap — so it is never the
    same thing as an absent value, which is why nothing here uses `or`."""
    if value is None or value == "":
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, parsed)


def _window_specs(cfg: dict) -> list[WindowSpec]:
    """The enabled windows of one monitor, minutes and caps resolved.

    The single place that decides what a window is: the poll, the Test API table
    and the Telegram detail all read it, so the rule cannot drift between them.
    """
    raw_enabled = cfg.get("windows")
    if raw_enabled is None:
        raw_enabled = list(_WINDOW_DEFAULTS)
    enabled = {_LEGACY_WINDOW_IDS.get(str(w), str(w)) for w in raw_enabled}

    specs: list[WindowSpec] = []
    for wid, (d_start, d_end, d_odds) in _WINDOW_DEFAULTS.items():
        if wid not in enabled:
            continue
        start = _clamp_minute(cfg.get(f"window_{wid}_start"), d_start)
        end   = _clamp_minute(cfg.get(f"window_{wid}_end"),   d_end)
        if end <= start:
            start, end = d_start, d_end
        raw_odds = cfg.get(f"window_{wid}_max_odds")
        if raw_odds is None and wid == WINDOW_EARLY:
            # Legacy: the one cap there used to be gated the early window only.
            raw_odds = cfg.get("max_odds")
        specs.append(WindowSpec(wid, start, end, _clamp_odds(raw_odds, d_odds)))
    return specs


def _in_quiet_window(quiet_start: str, quiet_end: str, tz_name: str = "UTC") -> bool:
    if not quiet_start or not quiet_end:
        return False
    try:
        try:
            tz = ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            tz = ZoneInfo("UTC")
        now = datetime.now(tz).time()
        qs  = datetime.strptime(quiet_start, "%H:%M").time()
        qe  = datetime.strptime(quiet_end,   "%H:%M").time()
        if qs <= qe:
            return qs <= now <= qe
        return now >= qs or now <= qe   # overnight window e.g. 23:00–07:00
    except ValueError:
        return False


def _get_window(minute: int, windows: list[WindowSpec]) -> WindowSpec | None:
    """Bounds are exclusive on both sides: 55–65 matches minutes 56–64."""
    for spec in windows:
        if spec.start < minute < spec.end:
            return spec
    return None


def _next_goal_odds(match: dict) -> tuple[float, float] | None:
    """(home, away) next-goal odds, falling back to rest-of-match (provider2)."""
    odds = match["odds"]
    tot  = match["home_score"] + match["away_score"]
    for key_home, key_away in ((f"next-goal-{tot + 1}-1", f"next-goal-{tot + 1}-2"),
                               ("rest-of-match-1",        "rest-of-match-2")):
        try:
            return float(odds[key_home]), float(odds[key_away])
        except (KeyError, TypeError, ValueError):
            continue
    return None


def _is_signal(diff: int, odds_home: float, odds_away: float, spec: WindowSpec) -> bool:
    """The alert rule itself, given a match already known to be in `spec`.

    The market must price the trailing side as the likelier next scorer, and —
    when that window carries a cap — price it short enough that the preference
    is worth acting on. A cap of 0 means the comparison alone decides.
    """
    if abs(diff) != 1:
        return False
    losing_odds  = odds_away if diff > 0 else odds_home
    winning_odds = odds_home if diff > 0 else odds_away
    if losing_odds >= winning_odds:
        return False
    return not (spec.max_odds and losing_odds >= spec.max_odds)


def _build_headers() -> dict:
    return {
        "x-rapidapi-host": "football-betting-odds1.p.rapidapi.com",
        "x-rapidapi-key":  _state.RAPIDAPI_FOOTBALL_KEY,
    }


class FootballLiveMonitor:
    def __init__(self, cfg: dict, send_fn, tz_name: str = "UTC"):
        self.monitor_id = cfg["id"]
        self.name       = cfg.get("name", "Football Betting")
        self._send      = send_fn
        self._enabled   = bool(cfg.get("enabled", True))
        self.tz_name    = tz_name

        self._windows: list[WindowSpec] = _window_specs(cfg)

        self._quiet_start: str = cfg.get("quiet_start") or "23:00"
        self._quiet_end:   str = cfg.get("quiet_end")   or "07:00"

        self._alerted: set[str] = set()
        self._task: asyncio.Task | None = None
        windows_desc = " · ".join(f"{w.id} {w.label} ({w.cap_label})" for w in self._windows) or "none"
        print(f"[FootballMonitor] '{self.name}' init — windows: {windows_desc} quiet: {self._quiet_start}–{self._quiet_end}")

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self._run(), name=f"live_football:{self.monitor_id}"
            )
            print(f"[FootballMonitor] '{self.name}' started (poll={POLL_INTERVAL_SEC}s)")

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            print(f"[FootballMonitor] '{self.name}' stopped")

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def _run(self) -> None:
        while True:
            try:
                await self._poll()
            except asyncio.CancelledError:
                return
            except Exception as e:
                _LOGGER.error("[FootballMonitor] '%s' unexpected error: %s", self.name, e)
            # Sleep until the next clock-aligned 5-minute boundary (:00, :05, :10 …)
            now = datetime.now()
            elapsed = (now.minute % 5) * 60 + now.second + now.microsecond / 1_000_000
            await asyncio.sleep(max(1.0, POLL_INTERVAL_SEC - elapsed))

    # ── Poll ──────────────────────────────────────────────────────────────────

    async def _poll(self) -> None:
        if not self._enabled:
            return
        if _in_quiet_window(self._quiet_start, self._quiet_end, self.tz_name):
            print(f"[FootballMonitor] '{self.name}' quiet window ({self._quiet_start}–{self._quiet_end}), skipping poll")
            return
        if not _state.RAPIDAPI_FOOTBALL_KEY:
            _LOGGER.warning(
                "[FootballMonitor] '%s' skipped — rapidapi_football_key not configured",
                self.name,
            )
            return

        async with httpx.AsyncClient(timeout=30) as client:
            raw_data = await self._fetch_inplaying(client)

        if not raw_data:
            return

        matches = [
            self._normalise(match_id, obj)
            for match_id, obj in raw_data.items()
        ]
        live_ids: set[str] = {m["id"] for m in matches}

        n_total      = len(matches)
        n_2nd        = 0
        n_window     = 0
        n_diff1      = 0
        n_new        = 0
        n_odds_ok    = 0
        n_signal     = 0

        for match in matches:
            if match["period_id"] != "3":
                continue
            n_2nd += 1

            minute = match["minutes"]
            spec = _get_window(minute, self._windows)
            if spec is None:
                continue
            n_window += 1

            home_score = match["home_score"]
            away_score = match["away_score"]
            diff = home_score - away_score
            if abs(diff) != 1:
                continue
            n_diff1 += 1

            alert_key = f"{match['id']}:{spec.id}"
            if alert_key in self._alerted:
                continue
            n_new += 1

            odds = _next_goal_odds(match)
            if odds is None:
                _LOGGER.debug(
                    "[FootballMonitor] '%s' missing next-goal and rest-of-match odds for %s",
                    self.name, match["id"],
                )
                continue
            odds_home_next, odds_away_next = odds
            n_odds_ok += 1

            if not _is_signal(diff, odds_home_next, odds_away_next, spec):
                continue
            n_signal += 1

            if diff > 0:
                winning_team, losing_team = match["home"], match["away"]
                winning_odds, losing_odds = odds_home_next, odds_away_next
            else:
                winning_team, losing_team = match["away"], match["home"]
                winning_odds, losing_odds = odds_away_next, odds_home_next

            self._alerted.add(alert_key)
            msg = self._build_alert(
                league       = match["country_leagues"],
                home         = match["home"],
                away         = match["away"],
                score        = match["score"],
                minute       = minute,
                odds_home    = odds_home_next,
                odds_away    = odds_away_next,
            )
            print(
                f"[FootballMonitor] '{self.name}' ALERT {alert_key} ({spec.label}) "
                f"{losing_team} next={losing_odds:.2f} < {winning_team} next={winning_odds:.2f}"
                + (f" (max_odds={spec.max_odds:g})" if spec.max_odds else " (no odds cap in this window)")
            )
            try:
                await self._send(msg)
            except Exception as e:
                _LOGGER.error("[FootballMonitor] '%s' send error: %s", self.name, e)

        print(
            f"[FootballMonitor] '{self.name}' poll: "
            f"total={n_total} 2nd={n_2nd} window={n_window} diff1={n_diff1} "
            f"new={n_new} odds_ok={n_odds_ok} signal={n_signal}"
        )

        # Prune alerted keys for matches no longer in the live feed
        if live_ids:
            stale = {k for k in self._alerted if k.split(":")[0] not in live_ids}
            self._alerted -= stale

    # ── API ───────────────────────────────────────────────────────────────────

    async def _fetch_inplaying(self, client: httpx.AsyncClient) -> dict:
        for provider in _PROVIDERS:
            url = f"{_BASE_URL}/{provider}/live/inplaying"
            try:
                resp = await client.get(url, headers=_build_headers())
                resp.raise_for_status()
                data = resp.json()
                if isinstance(data, dict) and data:
                    print(f"[FootballMonitor] '{self.name}' fetched via {provider} ({len(data)} matches)")
                    return data
                _LOGGER.warning("[FootballMonitor] '%s' %s returned empty/invalid data: %s", self.name, provider, type(data).__name__)
            except Exception as e:
                _LOGGER.warning("[FootballMonitor] '%s' %s failed: %s", self.name, provider, e)
        return {}

    # ── Normalisation ─────────────────────────────────────────────────────────

    @staticmethod
    def _normalise(match_id: str, raw: dict) -> dict:
        return {
            "id":              match_id,
            "period_id":       str(raw.get("periodID", "")),
            "minutes":         int(raw.get("minutes") or 0),
            "home":            raw.get("home", ""),
            "away":            raw.get("away", ""),
            "home_score":      int(raw.get("home_score") or 0),
            "away_score":      int(raw.get("away_score") or 0),
            "score":           raw.get("score", ""),
            "country_leagues": raw.get("country_leagues", ""),
            "odds":            raw.get("odds", {}),
        }

    # ── Alert message ─────────────────────────────────────────────────────────

    @staticmethod
    def _build_alert(*, league: str, home: str, away: str, score: str, minute: int,
                     odds_home: float, odds_away: float) -> str:
        return (
            "⚽ <b>SEGNALE SCOMMESSA LIVE</b>\n\n"
            f"🏆 {html.escape(league)}\n"
            f"{html.escape(home)} vs {html.escape(away)}\n"
            f"{html.escape(score)}  ⏱ {minute}'\n\n"
            f"📊 Quote prossimo gol:\n"
            f"  {html.escape(home)}: <b>{odds_home:.2f}</b>\n"
            f"  {html.escape(away)}: <b>{odds_away:.2f}</b>"
        )


# ── Standalone test helpers (used by /api/football/…) ────────────────────────

def _normalise_for_ui(match_id: str, obj: dict, provider: str,
                      windows: list[WindowSpec]) -> dict:
    m       = FootballLiveMonitor._normalise(match_id, obj)
    odds    = _next_goal_odds(m)
    ng_home = odds[0] if odds else None
    ng_away = odds[1] if odds else None
    diff    = m["home_score"] - m["away_score"]
    minute  = m["minutes"]
    spec    = _get_window(minute, windows) if m["period_id"] == "3" else None
    signal  = bool(spec and odds and _is_signal(diff, ng_home, ng_away, spec))
    return {
        "id":           m["id"],
        "league":       m["country_leagues"],
        "home":         m["home"],
        "away":         m["away"],
        "score":        m["score"],
        "minutes":      minute,
        "period_id":    m["period_id"],
        "home_score":   m["home_score"],
        "away_score":   m["away_score"],
        "ng_home":      ng_home,
        "ng_away":      ng_away,
        "window":       spec.id if spec else None,
        "window_label": spec.label if spec else None,
        "signal":       signal,
        "provider":     provider,
    }


async def fetch_provider_data(provider_name: str, windows: list[WindowSpec]) -> dict:
    """Fetch from a single named provider. Returns {ok, count, matches, error}."""
    async with httpx.AsyncClient(timeout=30) as client:
        url = f"{_BASE_URL}/{provider_name}/live/inplaying"
        try:
            resp = await client.get(url, headers=_build_headers())
            resp.raise_for_status()
            raw = resp.json()
        except Exception as e:
            return {"ok": False, "error": str(e), "count": 0, "matches": []}
        if not isinstance(raw, dict) or not raw:
            return {"ok": False, "error": f"Empty/invalid response ({type(raw).__name__})", "count": 0, "matches": []}
        matches = [_normalise_for_ui(mid, obj, provider_name, windows) for mid, obj in raw.items()]
        matches.sort(key=lambda x: x["minutes"], reverse=True)
        return {"ok": True, "error": None, "count": len(matches), "matches": matches}


async def fetch_inplaying_data(windows: list[WindowSpec]) -> list[dict]:
    """Fetch from providers in order; return matches from first successful one."""
    for provider in _PROVIDERS:
        result = await fetch_provider_data(provider, windows)
        if result["ok"]:
            return result["matches"]
    return []


# ── Manager ───────────────────────────────────────────────────────────────────

def _fingerprint(cfg: dict, tz_name: str) -> str:
    """Everything about this monitor that would change how it behaves.

    The whole config is hashed rather than a curated list of fields: missing one
    would mean a real edit that never takes effect, and there is nothing here
    cheap enough to be worth that risk. `created_at` is excluded because it never
    changes, and `id` because it is the key.
    """
    payload = {k: v for k, v in cfg.items() if k not in ("id", "created_at")}
    return json.dumps([payload, tz_name], sort_keys=True, default=str)


class FootballMonitorManager:
    def __init__(self):
        self._monitors: dict[str, FootballLiveMonitor] = {}
        self._fingerprints: dict[str, str] = {}

    def reload(self, configs: list[dict], make_send_fn, tz_name: str = "UTC") -> None:
        wanted: set[str] = set()
        for cfg in configs:
            if cfg.get("type") != "football_betting":
                continue
            # Disabled monitors are not started; the cleanup loop below stops and
            # removes any that were previously running so status reports "stopped".
            if not cfg.get("enabled"):
                continue
            mid = cfg["id"]
            wanted.add(mid)

            # Every manager is handed the WHOLE live-monitor list on every save,
            # so without this check toggling an unrelated task would tear this
            # monitor down and back up — losing its dedup state and its place in
            # the polling cycle for something that has nothing to do with it.
            fingerprint = _fingerprint(cfg, tz_name)
            existing = self._monitors.get(mid)
            if (existing and self._fingerprints.get(mid) == fingerprint
                    and existing.is_running()):
                continue

            self._fingerprints[mid] = fingerprint
            if existing:
                existing.stop()
            m = FootballLiveMonitor(cfg, make_send_fn(cfg), tz_name)
            self._monitors[mid] = m
            m.start()
        for mid in list(self._monitors):
            if mid not in wanted:
                self._monitors[mid].stop()
                del self._monitors[mid]
                self._fingerprints.pop(mid, None)

    def stop_all(self) -> None:
        for m in self._monitors.values():
            m.stop()
        self._monitors.clear()

    def status(self, monitor_id: str) -> str:
        m = self._monitors.get(monitor_id)
        return "running" if (m and m.is_running()) else "stopped"


football_monitor_manager = FootballMonitorManager()
