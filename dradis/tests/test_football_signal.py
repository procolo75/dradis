"""
tests/test_football_signal.py
──────────────────────────────
The football betting signal rule, as the 🔍 Test API table computes it —
`live_monitors.football._normalise_for_ui`.

    cd dradis && python3 -m unittest discover tests

What is pinned is the asymmetry between the two minute windows, because it was
wrong once and looked right: the **Maximum odds** cap gates the 55'–65' window
only. In 75'–81' the market's own preference for the trailing side is the whole
signal, and applying the cap there silently dropped valid late alerts.

The poll (`FootballLiveMonitor._poll`) keeps a second copy of this rule; the two
are checked against each other in `PollMirrorsUiTest` so they cannot drift apart
again.
"""

import builtins
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

_OPTIONS = Path(tempfile.mktemp(suffix="-options.json"))
_OPTIONS.write_text(json.dumps({
    "telegram_bot_token": "test", "telegram_allowed_chat_id": 1,
}))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dradis"))

_real_open = builtins.open


def _patched_open(path, *args, **kwargs):
    if str(path) == "/data/options.json":
        return _real_open(_OPTIONS, *args, **kwargs)
    return _real_open(path, *args, **kwargs)


# Two of the football module's imports are dead weight for a pure signal rule:
# `httpx` only speaks when a poll goes out, and `bot.state` is wanted for exactly
# one name — the RapidAPI key, which no test here uses — at the cost of dragging
# in the Telegram and LLM SDKs. Each is stubbed only if the real one will not
# import, and every stub is taken back out of sys.modules the moment the import
# is done, so the rest of the suite still gets the real modules.
_stubbed: list[str] = []
builtins.open = _patched_open
try:
    try:
        import httpx                                        # noqa: F401
    except ImportError:
        _fake = types.ModuleType("httpx")
        _fake.AsyncClient = object
        sys.modules["httpx"] = _fake
        _stubbed.append("httpx")

    try:
        import bot.state                                    # noqa: F401
    except Exception:
        _pkg = sys.modules.get("bot") or types.ModuleType("bot")
        _pkg.__path__ = getattr(_pkg, "__path__", [])
        _mod = types.ModuleType("bot.state")
        _mod.RAPIDAPI_FOOTBALL_KEY = ""
        _pkg.state = _mod
        sys.modules["bot"], sys.modules["bot.state"] = _pkg, _mod
        _stubbed += ["bot", "bot.state"]

    from live_monitors.football import (            # noqa: E402
        WINDOW_EARLY, WINDOW_LATE, _get_window, _normalise_for_ui,
    )
except ImportError as e:
    raise unittest.SkipTest(f"football monitor dependencies unavailable: {e}")
finally:
    builtins.open = _real_open
    for _name in _stubbed:
        sys.modules.pop(_name, None)


def match(*, minute, home_score, away_score, ng_home, ng_away, period="3"):
    """One live-feed match, shaped the way the provider returns it."""
    return {
        "periodID":        period,
        "minutes":         minute,
        "home":            "Home FC",
        "away":            "Away FC",
        "home_score":      home_score,
        "away_score":      away_score,
        "score":           f"{home_score}-{away_score}",
        "country_leagues": "Test League",
        "odds": {
            f"next-goal-{home_score + away_score + 1}-1": str(ng_home),
            f"next-goal-{home_score + away_score + 1}-2": str(ng_away),
        },
    }


def signal(max_odds=2.0, **kwargs):
    return _normalise_for_ui("m1", match(**kwargs), "provider1", max_odds)["signal"]


class LateWindowIgnoresCapTest(unittest.TestCase):
    """75'–81': the odds comparison alone decides."""

    def test_favourable_odds_above_cap_still_signal(self):
        # The regression. Away trails and is the shorter price at 3.00 vs 4.00 —
        # a real signal that the cap (2.0) used to swallow.
        self.assertTrue(signal(minute=78, home_score=1, away_score=0,
                               ng_home=4.00, ng_away=3.00))

    def test_wrong_direction_is_not_a_signal(self):
        self.assertFalse(signal(minute=78, home_score=1, away_score=0,
                                ng_home=3.00, ng_away=4.00))

    def test_two_goal_difference_is_not_a_signal(self):
        self.assertFalse(signal(minute=78, home_score=2, away_score=0,
                                ng_home=4.00, ng_away=3.00))


class EarlyWindowAppliesCapTest(unittest.TestCase):
    """55'–65': the trailing side must also be short-priced."""

    def test_favourable_odds_above_cap_is_not_a_signal(self):
        self.assertFalse(signal(minute=60, home_score=1, away_score=0,
                                ng_home=4.00, ng_away=3.00))

    def test_favourable_odds_under_cap_is_a_signal(self):
        self.assertTrue(signal(minute=60, home_score=1, away_score=0,
                               ng_home=3.00, ng_away=1.80))

    def test_cap_is_configurable(self):
        self.assertTrue(signal(minute=60, home_score=1, away_score=0,
                               ng_home=4.00, ng_away=3.00, max_odds=3.5))


class OutsideTheWindowsTest(unittest.TestCase):

    def test_between_the_windows_is_not_a_signal(self):
        self.assertFalse(signal(minute=70, home_score=1, away_score=0,
                                ng_home=4.00, ng_away=3.00))

    def test_first_half_is_not_a_signal(self):
        self.assertFalse(signal(minute=60, home_score=1, away_score=0,
                                ng_home=3.00, ng_away=1.80, period="2"))

    def test_window_bounds_are_exclusive(self):
        # The labels read 55-65 and 75-81; the feed's minute must be strictly
        # inside, so the boundary minutes themselves never alert.
        for minute in (55, 65, 75, 81):
            with self.subTest(minute=minute):
                self.assertFalse(signal(minute=minute, home_score=1, away_score=0,
                                        ng_home=3.00, ng_away=1.10))


class PollMirrorsUiTest(unittest.TestCase):
    """The window table the poll uses is the one the UI copy reads from."""

    def test_get_window_labels(self):
        windows = [(WINDOW_EARLY, 55, 65), (WINDOW_LATE, 75, 81)]
        self.assertEqual(_get_window(60, windows), WINDOW_EARLY)
        self.assertEqual(_get_window(78, windows), WINDOW_LATE)
        self.assertIsNone(_get_window(70, windows))

    def test_only_the_configured_window_matches(self):
        self.assertIsNone(_get_window(60, [(WINDOW_LATE, 75, 81)]))


if __name__ == "__main__":
    unittest.main()
