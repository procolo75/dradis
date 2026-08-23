"""
tests/test_football_signal.py
──────────────────────────────
The football betting signal rule, as the 🔍 Test API table computes it —
`live_monitors.football._normalise_for_ui`.

    cd dradis && python3 -m unittest discover tests

What is pinned is that the minute bounds and the odds cap are **per window and
configurable**: each window alerts only when the trailing side is both the
shorter price and under that window's own maximum, and a maximum of `0` means
no maximum at all. `0` is the late window's default, because that is what this
monitor did before the cap became per-window — reading it as "unset" and
substituting a default would silently start dropping alerts on every monitor
already saved.

Legacy configs are pinned too: a monitor saved when the window id *was* its
bounds ("55-65") must come back as the same band with the same cap.
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
        WINDOW_EARLY, WINDOW_LATE, WindowSpec, _get_window, _normalise_for_ui,
        _window_specs,
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


def windows(early=(55, 65, 2.0), late=(75, 81, 0.0)):
    specs = []
    if early:
        specs.append(WindowSpec(WINDOW_EARLY, *early))
    if late:
        specs.append(WindowSpec(WINDOW_LATE, *late))
    return specs


def signal(spec_list=None, **kwargs):
    return _normalise_for_ui(
        "m1", match(**kwargs), "provider1", spec_list or windows(),
    )["signal"]


class LateWindowCapTest(unittest.TestCase):
    """The second window now has a maximum of its own."""

    def test_no_cap_lets_a_long_price_through(self):
        # The old regression, and still the default: away trails and is the
        # shorter price at 3.00 vs 4.00 — a real signal, and with no maximum set
        # on the late window nothing swallows it.
        self.assertTrue(signal(minute=78, home_score=1, away_score=0,
                               ng_home=4.00, ng_away=3.00))

    def test_a_cap_filters_the_same_match(self):
        self.assertFalse(signal(windows(late=(75, 81, 2.5)),
                                minute=78, home_score=1, away_score=0,
                                ng_home=4.00, ng_away=3.00))

    def test_under_its_own_cap_is_still_a_signal(self):
        self.assertTrue(signal(windows(late=(75, 81, 2.5)),
                               minute=78, home_score=1, away_score=0,
                               ng_home=3.00, ng_away=1.80))

    def test_wrong_direction_is_not_a_signal(self):
        self.assertFalse(signal(minute=78, home_score=1, away_score=0,
                                ng_home=3.00, ng_away=4.00))

    def test_two_goal_difference_is_not_a_signal(self):
        self.assertFalse(signal(minute=78, home_score=2, away_score=0,
                                ng_home=4.00, ng_away=3.00))


class EarlyWindowCapTest(unittest.TestCase):
    """55'–65' by default: the trailing side must also be short-priced."""

    def test_favourable_odds_above_cap_is_not_a_signal(self):
        self.assertFalse(signal(minute=60, home_score=1, away_score=0,
                                ng_home=4.00, ng_away=3.00))

    def test_favourable_odds_under_cap_is_a_signal(self):
        self.assertTrue(signal(minute=60, home_score=1, away_score=0,
                               ng_home=3.00, ng_away=1.80))

    def test_cap_is_configurable(self):
        self.assertTrue(signal(windows(early=(55, 65, 3.5)),
                               minute=60, home_score=1, away_score=0,
                               ng_home=4.00, ng_away=3.00))

    def test_a_zero_cap_means_no_cap(self):
        self.assertTrue(signal(windows(early=(55, 65, 0.0)),
                               minute=60, home_score=1, away_score=0,
                               ng_home=4.00, ng_away=3.00))


class ConfigurableMinutesTest(unittest.TestCase):

    def test_a_moved_window_alerts_on_its_own_minutes(self):
        moved = windows(early=(50, 60, 0.0), late=None)
        self.assertTrue(signal(moved, minute=55, home_score=1, away_score=0,
                               ng_home=4.00, ng_away=3.00))

    def test_and_not_on_the_old_ones(self):
        moved = windows(early=(50, 60, 0.0), late=None)
        self.assertFalse(signal(moved, minute=62, home_score=1, away_score=0,
                                ng_home=4.00, ng_away=3.00))

    def test_a_disabled_window_never_alerts(self):
        self.assertFalse(signal(windows(late=None),
                                minute=78, home_score=1, away_score=0,
                                ng_home=4.00, ng_away=3.00))


class OutsideTheWindowsTest(unittest.TestCase):

    def test_between_the_windows_is_not_a_signal(self):
        self.assertFalse(signal(minute=70, home_score=1, away_score=0,
                                ng_home=4.00, ng_away=3.00))

    def test_first_half_is_not_a_signal(self):
        self.assertFalse(signal(minute=60, home_score=1, away_score=0,
                                ng_home=3.00, ng_away=1.80, period="2"))

    def test_window_bounds_are_exclusive(self):
        # 55-65 and 75-81 read as bounds; the feed's minute must be strictly
        # inside, so the boundary minutes themselves never alert.
        for minute in (55, 65, 75, 81):
            with self.subTest(minute=minute):
                self.assertFalse(signal(minute=minute, home_score=1, away_score=0,
                                        ng_home=3.00, ng_away=1.10))


class WindowSpecsTest(unittest.TestCase):
    """`_window_specs` is the one place that decides what a window is."""

    def test_defaults(self):
        self.assertEqual(_window_specs({}), [
            WindowSpec(WINDOW_EARLY, 55, 65, 2.0),
            WindowSpec(WINDOW_LATE,  75, 81, 0.0),
        ])

    def test_a_legacy_config_keeps_its_bands_and_its_cap(self):
        # Saved before the minutes were settable: the id *was* the bounds, and
        # the single cap gated the early band only.
        self.assertEqual(
            _window_specs({"windows": ["55-65", "75-81"], "max_odds": 3.0}),
            [WindowSpec(WINDOW_EARLY, 55, 65, 3.0),
             WindowSpec(WINDOW_LATE,  75, 81, 0.0)],
        )

    def test_a_zero_cap_survives_being_read(self):
        # `or` would read a deliberate 0 as "not set" and hand back 2.0.
        specs = _window_specs({"window_early_max_odds": 0})
        self.assertEqual(specs[0].max_odds, 0.0)

    def test_custom_minutes(self):
        specs = _window_specs({"windows": ["early"],
                               "window_early_start": 40, "window_early_end": 50})
        self.assertEqual(specs, [WindowSpec(WINDOW_EARLY, 40, 50, 2.0)])

    def test_nonsense_minutes_fall_back_to_the_defaults(self):
        for bad in ({"window_late_start": 90, "window_late_end": 80},
                    {"window_late_start": "?", "window_late_end": None}):
            with self.subTest(cfg=bad):
                specs = _window_specs({"windows": ["late"], **bad})
                self.assertEqual(specs, [WindowSpec(WINDOW_LATE, 75, 81, 0.0)])

    def test_minutes_are_clamped_to_a_football_match(self):
        specs = _window_specs({"windows": ["early"],
                               "window_early_start": -10, "window_early_end": 999})
        self.assertEqual((specs[0].start, specs[0].end), (1, 120))

    def test_an_empty_window_list_disables_everything(self):
        self.assertEqual(_window_specs({"windows": []}), [])


class PollMirrorsUiTest(unittest.TestCase):
    """The window lookup the poll uses is the one the UI copy reads from."""

    def test_get_window_returns_the_spec(self):
        specs = windows()
        self.assertEqual(_get_window(60, specs).id, WINDOW_EARLY)
        self.assertEqual(_get_window(78, specs).id, WINDOW_LATE)
        self.assertIsNone(_get_window(70, specs))

    def test_only_the_configured_window_matches(self):
        self.assertIsNone(_get_window(60, windows(early=None)))


if __name__ == "__main__":
    unittest.main()
