"""
tests/test_system_prompt.py
────────────────────────────
How per-capability instructions reach the model.

    cd dradis && python3 -m unittest discover tests

The bug these tests pin down: the extra instructions written for one tool were
appended to the system prompt as bare sentences, directly under the global agent
instructions and with nothing saying which tool they belonged to. From the
model's side they were indistinguishable from standing rules, so "report
temperatures in Celsius" shaped a web search too. Attaching fewer tools was not a
fix, because ordinary chat attaches all of them.
"""

import builtins
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

# bot.state reads /data/options.json at import time and raises without it, so the
# file is faked for the duration of the import and nothing else.
_OPTIONS = Path(tempfile.mktemp(suffix="-options.json"))
_OPTIONS.write_text(json.dumps({
    "telegram_bot_token": "test", "telegram_allowed_chat_id": 1,
    "tavily_api_key": "test-key",
}))

if "aiomqtt" not in sys.modules:
    sys.modules["aiomqtt"] = types.ModuleType("aiomqtt")

_real_open = builtins.open


def _patched_open(path, *args, **kwargs):
    if str(path) == "/data/options.json":
        return _real_open(_OPTIONS, *args, **kwargs)
    return _real_open(path, *args, **kwargs)


# `bot.state` uses the same absolute imports the running add-on does (`from
# agents.… import …`), so its own directory has to be importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dradis"))

builtins.open = _patched_open
try:
    import bot.state as st                                        # noqa: E402
except ImportError as e:
    # Unlike the rest of the suite, this module pulls in the LLM and Google SDKs.
    # They are present in the add-on image; skip rather than fail for anyone
    # running the tests with only the light dependencies installed.
    raise unittest.SkipTest(f"bot.state dependencies unavailable: {e}")
finally:
    builtins.open = _real_open


def settings(**overrides) -> dict:
    base = {
        "timezone": "UTC",
        "agent_instructions": "You are DRADIS.",
        "ws_enabled": False, "ws_instructions": "",
        "weather_enabled": False, "weather_instructions": "",
        "gcal_enabled": False, "gcal_instructions": "",
        "gmail_enabled": False, "gmail_instructions": "",
        "gtasks_enabled": False, "gtasks_instructions": "",
        "read_url_enabled": False,
    }
    base.update(overrides)
    return base


WEATHER_INSTR = "Report temperatures in Celsius and add a 3-day outlook."
SEARCH_INSTR = "Always cite the source URL for every claim."


class ScopingTest(unittest.TestCase):

    def setUp(self):
        self._read = st.read_settings

    def tearDown(self):
        st.read_settings = self._read

    def prompt(self, s, selected=None) -> str:
        st.read_settings = lambda: s
        return st._system_prompt(s, st.build_tools(s, selected))

    def test_an_instruction_is_never_a_bare_line(self):
        # THE regression. A bare sentence under the agent instructions reads as a
        # global rule, which is exactly how it leaked onto other tools.
        s = settings(weather_enabled=True, weather_instructions=WEATHER_INSTR)
        text = self.prompt(s)
        self.assertNotIn(f"\n{WEATHER_INSTR}", text)
        self.assertIn(f": {WEATHER_INSTR}", text)

    def test_the_instruction_names_its_capability(self):
        s = settings(weather_enabled=True, weather_instructions=WEATHER_INSTR)
        self.assertIn("- Weather (", self.prompt(s))

    def test_the_instruction_names_the_tools_it_governs(self):
        # Binding the text to the actual function names is what lets the model
        # tell "while using this" from "always".
        s = settings(weather_enabled=True, weather_instructions=WEATHER_INSTR)
        text = self.prompt(s)
        tool_names = [t["name"] for t in st.build_tools(s, None)
                      if t.get("capability") == "weather"]
        self.assertTrue(tool_names)
        for name in tool_names:
            self.assertIn(name, text)

    def test_the_header_states_the_scope(self):
        s = settings(weather_enabled=True, weather_instructions=WEATHER_INSTR)
        text = self.prompt(s)
        self.assertIn("ONLY while you are using that tool", text)

    def test_two_capabilities_stay_on_separate_labelled_lines(self):
        s = settings(weather_enabled=True, weather_instructions=WEATHER_INSTR,
                     ws_enabled=True, ws_instructions=SEARCH_INSTR)
        text = self.prompt(s)
        lines = [ln for ln in text.splitlines() if ln.startswith("- ")]
        self.assertEqual(len(lines), 2)
        self.assertTrue(any(WEATHER_INSTR in ln and "Weather" in ln for ln in lines))
        self.assertTrue(any(SEARCH_INSTR in ln and "Web Search" in ln for ln in lines))

    def test_a_capability_with_no_instruction_adds_nothing(self):
        s = settings(weather_enabled=True, weather_instructions="",
                     ws_enabled=True, ws_instructions=SEARCH_INSTR)
        text = self.prompt(s)
        self.assertEqual(len([ln for ln in text.splitlines()
                              if ln.startswith("- ")]), 1)
        self.assertNotIn("Weather (", text)

    def test_blank_instructions_cost_no_header_at_all(self):
        # Every enabled capability but nothing written: the prompt must be exactly
        # the base one, with no tokens spent on an empty section.
        s = settings(weather_enabled=True, ws_enabled=True)
        self.assertNotIn("Tool-specific", self.prompt(s))

    def test_no_tool_no_instruction(self):
        # A task that selected only web_search must not carry the weather rule.
        s = settings(weather_enabled=True, weather_instructions=WEATHER_INSTR,
                     ws_enabled=True, ws_instructions=SEARCH_INSTR)
        text = self.prompt(s, selected=["web_search"])
        self.assertIn(SEARCH_INSTR, text)
        self.assertNotIn(WEATHER_INSTR, text)

    def test_no_tools_at_all_leaves_the_base_prompt(self):
        s = settings(weather_enabled=True, weather_instructions=WEATHER_INSTR)
        text = self.prompt(s, selected=[])
        self.assertNotIn(WEATHER_INSTR, text)
        self.assertNotIn("Tool-specific", text)

    def test_the_agent_instructions_are_still_global(self):
        s = settings(weather_enabled=True, weather_instructions=WEATHER_INSTR)
        text = self.prompt(s)
        self.assertIn("You are DRADIS.", text)
        self.assertLess(text.index("You are DRADIS."), text.index("Tool-specific"))


class ReadUrlTest(unittest.TestCase):
    """read_url's line says WHEN to reach for the tool, so it is a routing rule
    and belongs with the global instructions. Filing it under "applies only while
    using that tool" would make it circular."""

    def setUp(self):
        self._read = st.read_settings

    def tearDown(self):
        st.read_settings = self._read

    def prompt(self, s) -> str:
        st.read_settings = lambda: s
        return st._system_prompt(s, st.build_tools(s, None))

    def test_the_routing_rule_is_present_when_the_tool_is(self):
        self.assertIn("call read_url", self.prompt(settings(read_url_enabled=True)))

    def test_it_is_absent_when_the_tool_is_not(self):
        self.assertNotIn("read_url", self.prompt(settings(read_url_enabled=False)))

    def test_it_sits_outside_the_scoped_block(self):
        s = settings(read_url_enabled=True, weather_enabled=True,
                     weather_instructions=WEATHER_INSTR)
        text = self.prompt(s)
        self.assertLess(text.index("call read_url"), text.index("Tool-specific"))

    def test_it_alone_needs_no_scoped_header(self):
        self.assertNotIn("Tool-specific",
                         self.prompt(settings(read_url_enabled=True)))


if __name__ == "__main__":
    unittest.main()
