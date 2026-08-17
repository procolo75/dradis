"""
tests/test_car_mode_gate.py
───────────────────────────
The Car Mode GATE, as opposed to the wording — `bot.state.for_car`,
`bot.state.car_mode_enabled` and the two places that drop content outright.

    cd dradis && python3 -m unittest discover tests

Split from `test_car_mode.py` on purpose: the sanitiser is a pure function and
must stay testable without the Telegram and LLM SDKs, so the gate — which needs
`bot.state`, and therefore `/data/options.json` — lives here and skips cleanly
when those are unavailable.

What is pinned:
  · The gate is OFF by default and must leave the text byte-identical, because it
    runs on the way out of EVERY message DRADIS sends.
  · Car Mode DROPS THE PARSE MODE. Sanitising resolves entities back into
    characters, so an answer containing "a < b" reaches Telegram as a literal
    `<`; parsed as HTML that is an unclosed tag and the send fails outright.
  · `reply_footer` returns nothing in Car Mode. The footer is diagnostics; read
    aloud it competes with the alert.
  · A monitor's own language reaches the sanitiser, or an English monitor is
    announced with Italian units.
"""

import builtins
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

_OPTIONS = Path(tempfile.mktemp(suffix="-options.json"))
_OPTIONS.write_text(json.dumps({
    "telegram_bot_token": "test", "telegram_allowed_chat_id": 1,
}))

if "aiomqtt" not in sys.modules:
    sys.modules["aiomqtt"] = types.ModuleType("aiomqtt")

# `bot.*` uses the same absolute imports the running add-on does.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dradis"))

_real_open = builtins.open


def _patched_open(path, *args, **kwargs):
    if str(path) == "/data/options.json":
        return _real_open(_OPTIONS, *args, **kwargs)
    return _real_open(path, *args, **kwargs)


builtins.open = _patched_open
try:
    import bot.state as _state                                    # noqa: E402
except ImportError as e:
    raise unittest.SkipTest(f"bot dependencies unavailable: {e}")
finally:
    builtins.open = _real_open


ALERT = "⛈️ <b>Temporale — Casa</b>\n\U0001F4CD Fronte a <b>12 km</b> a O"


class _Result:
    """Stands in for the runtime result `reply_footer` reads."""
    prompt_tokens = 1234
    completion_tokens = 567
    tools_used = ["web_search"]


class GateTest(unittest.TestCase):

    def test_off_by_default_leaves_the_text_and_parse_mode_untouched(self):
        # This runs on every outgoing message; a no-op that is not exactly a
        # no-op would corrupt DRADIS's entire output the moment it shipped.
        self.assertFalse(_state.car_mode_enabled({}))
        self.assertEqual(_state.for_car(ALERT, settings={}), (ALERT, "HTML"))

    def test_on_rewrites_the_text(self):
        text, _ = _state.for_car(ALERT, settings={"car_mode_enabled": True})
        self.assertEqual(text, "Temporale, Casa. Fronte a 12 chilometri a ovest.")

    def test_on_drops_the_parse_mode(self):
        # Not cosmetic: with HTML parsing still on, the first bare `<` that
        # survives sanitising makes Telegram reject the whole message.
        _, parse_mode = _state.for_car(ALERT, settings={"car_mode_enabled": True})
        self.assertIsNone(parse_mode)

    def test_an_answer_containing_a_bare_angle_bracket_still_sends(self):
        # `md_to_html` escapes it, `to_spoken` unescapes it — and HTML parse mode
        # would then choke on the literal character it just recreated.
        text, parse_mode = _state.for_car("temperatura a &lt; 5 gradi",
                                          settings={"car_mode_enabled": True})
        self.assertIn("<", text)
        self.assertIsNone(parse_mode)

    def test_the_monitors_language_reaches_the_sanitiser(self):
        text, _ = _state.for_car("Front at 12 km to W", "en",
                                 settings={"car_mode_enabled": True})
        self.assertEqual(text, "Front at 12 kilometres to west.")

    def test_settings_are_read_fresh_when_not_supplied(self):
        # Nothing caches the flag — that is why toggling it needs no reload.
        with mock.patch.object(_state, "read_settings",
                               return_value={"car_mode_enabled": True}):
            self.assertTrue(_state.car_mode_enabled())
            self.assertNotIn("<b>", _state.for_car(ALERT)[0])


class FooterTest(unittest.TestCase):

    def test_the_footer_is_dropped_in_car_mode(self):
        settings = {"token_usage_enabled": True, "tools_usage_enabled": True,
                    "car_mode_enabled": True}
        self.assertEqual(_state.reply_footer(settings, _Result()), "")

    def test_the_footer_is_still_built_when_car_mode_is_off(self):
        settings = {"token_usage_enabled": True, "tools_usage_enabled": True,
                    "car_mode_enabled": False}
        footer = _state.reply_footer(settings, _Result())
        self.assertIn("1234", footer)
        self.assertIn("web_search", footer)


if __name__ == "__main__":
    unittest.main()
