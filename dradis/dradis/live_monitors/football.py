"""
live_monitors/football.py
─────────────────────────
LLM-free live monitor: polls football-betting-odds1.p.rapidapi.com every 300s
and sends Telegram alerts when a losing team's next-goal odds are lower (better)
than the winning team's in configured minute windows of the 2nd half.

Alert conditions (all must be true):
  - periodID == "3" (2nd half)
  - match minute inside a configured window ("55-65" and/or "75-81")
  - goal difference == 1
  - losing team's next-goal odds < winning team's next-goal odds

Providers are tried in order (provider1→4); first successful response wins.
Deduplication: one alert per (match_id + window). Key is pruned only when the
match disappears from the live feed (guard: only prune if feed returned results).
"""

import asyncio
import html
import logging
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

import bot.state as _state

_LOGGER = logging.getLogger(__name__)

POLL_INTERVAL_SEC = 300

_BASE_URL  = "https://football-betting-odds1.p.rapidapi.com"
_PROVIDERS = ["provider1", "provider2", "provider3", "provider4"]

_ALL_WINDOWS: dict[str, tuple[int, int]] = {
    "55-65": (55, 65),
    "75-81": (75, 81),
}


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


def _get_window(minute: int, windows: list[tuple[str, int, int]]) -> str | None:
    for label, lo, hi in windows:
        if lo < minute < hi:
            return label
    return None


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

        raw_windows = cfg.get("windows") or list(_ALL_WINDOWS.keys())
        self._windows: list[tuple[str, int, int]] = [
            (label, lo, hi)
            for label, (lo, hi) in _ALL_WINDOWS.items()
            if label in raw_windows
        ]

        self._quiet_start: str = cfg.get("quiet_start") or "23:00"
        self._quiet_end:   str = cfg.get("quiet_end")   or "07:00"

        # Only alert when the losing team's next-goal odds are below this cap.
        self._max_odds: float = float(cfg.get("max_odds") or 2.0)

        self._alerted: set[str] = set()
        self._task: asyncio.Task | None = None
        print(f"[FootballMonitor] '{self.name}' init — windows: {[w[0] for w in self._windows]} quiet: {self._quiet_start}–{self._quiet_end} max_odds: {self._max_odds}")

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
            window = _get_window(minute, self._windows)
            if window is None:
                continue
            n_window += 1

            home_score = match["home_score"]
            away_score = match["away_score"]
            diff = home_score - away_score
            if abs(diff) != 1:
                continue
            n_diff1 += 1

            alert_key = f"{match['id']}:{window}"
            if alert_key in self._alerted:
                continue
            n_new += 1

            # Try next-goal-N-1/2 first (provider1/3), fall back to rest-of-match (provider2)
            odds = match["odds"]
            tot = home_score + away_score
            try:
                odds_home_next = float(odds[f"next-goal-{tot + 1}-1"])
                odds_away_next = float(odds[f"next-goal-{tot + 1}-2"])
            except (KeyError, TypeError, ValueError):
                try:
                    odds_home_next = float(odds["rest-of-match-1"])
                    odds_away_next = float(odds["rest-of-match-2"])
                except (KeyError, TypeError, ValueError):
                    _LOGGER.debug(
                        "[FootballMonitor] '%s' missing next-goal and rest-of-match odds for %s",
                        self.name, match["id"],
                    )
                    continue
            n_odds_ok += 1

            # Determine winning/losing team and their next-goal odds
            if diff > 0:
                # home winning
                winning_team  = match["home"]
                losing_team   = match["away"]
                winning_odds  = odds_home_next
                losing_odds   = odds_away_next
            else:
                # away winning
                winning_team  = match["away"]
                losing_team   = match["home"]
                winning_odds  = odds_away_next
                losing_odds   = odds_home_next

            if losing_odds >= winning_odds:
                continue
            # Skip long-shot bets: the losing team's next-goal odds must be below
            # the configured cap (default 2.0) for the signal to be worth an alert.
            if losing_odds >= self._max_odds:
                continue
            n_signal += 1

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
                f"[FootballMonitor] '{self.name}' ALERT {alert_key} "
                f"{losing_team} next={losing_odds:.2f} < {winning_team} next={winning_odds:.2f} "
                f"(max_odds={self._max_odds})"
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

def _normalise_for_ui(match_id: str, obj: dict, provider: str, max_odds: float = 2.0) -> dict:
    m    = FootballLiveMonitor._normalise(match_id, obj)
    odds = m["odds"]
    tot  = m["home_score"] + m["away_score"]
    try:
        ng_home = float(odds[f"next-goal-{tot + 1}-1"])
    except (KeyError, TypeError, ValueError):
        try:
            ng_home = float(odds["rest-of-match-1"])
        except (KeyError, TypeError, ValueError):
            ng_home = None
    try:
        ng_away = float(odds[f"next-goal-{tot + 1}-2"])
    except (KeyError, TypeError, ValueError):
        try:
            ng_away = float(odds["rest-of-match-2"])
        except (KeyError, TypeError, ValueError):
            ng_away = None
    diff           = m["home_score"] - m["away_score"]
    is_second_half = m["period_id"] == "3"
    minute         = m["minutes"]
    in_55_65       = is_second_half and 55 < minute < 65
    in_75_81       = is_second_half and 75 < minute < 81
    signal = False
    if is_second_half and (in_55_65 or in_75_81) and abs(diff) == 1 and ng_home is not None and ng_away is not None:
        # Losing team's next-goal odds must beat the winning team's AND be below the cap.
        if diff > 0 and ng_away < ng_home and ng_away < max_odds:
            signal = True
        elif diff < 0 and ng_home < ng_away and ng_home < max_odds:
            signal = True
    return {
        "id":         m["id"],
        "league":     m["country_leagues"],
        "home":       m["home"],
        "away":       m["away"],
        "score":      m["score"],
        "minutes":    minute,
        "period_id":  m["period_id"],
        "home_score": m["home_score"],
        "away_score": m["away_score"],
        "ng_home":    ng_home,
        "ng_away":    ng_away,
        "in_55_65":   in_55_65,
        "in_75_81":   in_75_81,
        "signal":     signal,
        "provider":   provider,
    }


async def fetch_provider_data(provider_name: str, max_odds: float = 2.0) -> dict:
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
        matches = [_normalise_for_ui(mid, obj, provider_name, max_odds) for mid, obj in raw.items()]
        matches.sort(key=lambda x: x["minutes"], reverse=True)
        return {"ok": True, "error": None, "count": len(matches), "matches": matches}


async def fetch_inplaying_data(max_odds: float = 2.0) -> list[dict]:
    """Fetch from providers in order; return matches from first successful one."""
    for provider in _PROVIDERS:
        result = await fetch_provider_data(provider, max_odds)
        if result["ok"]:
            return result["matches"]
    return []


# ── Manager ───────────────────────────────────────────────────────────────────

class FootballMonitorManager:
    def __init__(self):
        self._monitors: dict[str, FootballLiveMonitor] = {}

    def reload(self, configs: list[dict], make_send_fn, tz_name: str = "UTC") -> None:
        wanted: set[str] = set()
        for cfg in configs:
            if cfg.get("type") != "football_betting":
                continue
            mid = cfg["id"]
            wanted.add(mid)
            if mid in self._monitors:
                self._monitors[mid].stop()
            m = FootballLiveMonitor(cfg, make_send_fn(cfg), tz_name)
            self._monitors[mid] = m
            m.start()
        for mid in list(self._monitors):
            if mid not in wanted:
                self._monitors[mid].stop()
                del self._monitors[mid]

    def stop_all(self) -> None:
        for m in self._monitors.values():
            m.stop()
        self._monitors.clear()

    def status(self, monitor_id: str) -> str:
        m = self._monitors.get(monitor_id)
        return "running" if (m and m.is_running()) else "stopped"


football_monitor_manager = FootballMonitorManager()
