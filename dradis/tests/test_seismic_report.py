"""
tests/test_seismic_report.py
────────────────────────────
The scheduled seismic report — `monitors/seismic.py`.

    cd dradis && python3 -m unittest discover tests

No network: `httpx.AsyncClient` is replaced with a stub serving fixture events.

What is pinned, and why it bites:
  · The report is sent with parse_mode=HTML, so every "<" in it must open a tag
    Telegram accepts. The magnitude bin "< 0" went out raw and Telegram refused
    the whole message ("unsupported start tag"). The line only exists when an
    event has negative magnitude, so the report failed some weeks and not others.
"""

import asyncio
import re
import sys
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dradis"))

# `httpx` is only the transport and is replaced below anyway. Stubbed only if the
# real one will not import, and taken back out of sys.modules straight after, so
# the rest of the suite still gets the real module — as in test_football_signal.py.
_stubbed = False
try:
    import httpx                                            # noqa: F401
except ImportError:
    _fake = types.ModuleType("httpx")
    _fake.AsyncClient = object
    sys.modules["httpx"] = _fake
    _stubbed = True
try:
    from monitors import seismic                            # noqa: E402
finally:
    if _stubbed:
        sys.modules.pop("httpx", None)

_ALLOWED_TAG = re.compile(r"</?(?:b|i|a)(?:\s[^>]*)?>")


def _event(age_h: float, mag: float | None, depth: float | None,
           cls: str = "Rivisto") -> dict:
    return {
        "epoch": time.time() - age_h * 3600,
        "magnitudos": [] if mag is None else [{"type": "D", "value": mag}],
        "class": cls,
        "location": {"depth": depth, "latitude": 40.83, "longitude": 14.14},
    }


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _Client:
    payload: list = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url):
        return _Response(self.payload)


def _run(events: list[dict], lang: str = "it") -> str:
    _Client.payload = events
    monitor = {"seismic_area": "flegrei", "time_range": "last_7d", "language": lang}
    with mock.patch.object(seismic.httpx, "AsyncClient", _Client):
        return asyncio.run(seismic.run_seismic_monitor(monitor, tz_name="Europe/Rome"))


class TelegramHtmlTest(unittest.TestCase):

    def _assert_only_allowed_tags(self, report: str):
        leftover = _ALLOWED_TAG.sub("", report)
        self.assertNotIn("<", leftover, f"raw '<' would be rejected by Telegram:\n{report}")

    def test_negative_magnitude_is_escaped(self):
        report = _run([
            _event(1, -0.4, 1.2),
            _event(2, None, None),
            _event(3, 0.5, 2.5, cls="Automatico"),
            _event(4, 2.3, 3.1),
        ])
        self.assertIn("&lt; 0", report)
        self._assert_only_allowed_tags(report)

    def test_every_bin_is_valid_html(self):
        events = [_event(i + 1, m, 1.0 + i)
                  for i, m in enumerate([None, -0.8, 0.3, 1.4, 2.6, 3.2, 4.1])]
        for lang in ("it", "en"):
            with self.subTest(lang=lang):
                self._assert_only_allowed_tags(_run(events, lang))

    def test_empty_period_is_valid_html(self):
        self._assert_only_allowed_tags(_run([]))


if __name__ == "__main__":
    unittest.main()
