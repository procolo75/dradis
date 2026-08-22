"""
tests/addon_import.py
─────────────────────
Importing `bot.state` outside the add-on container.

Two things stand in the way. `bot.state` reads /data/options.json at import time
and raises without it, and it uses the same absolute imports the running add-on
does (`from agents.… import …`), so its own directory has to be on the path.
Both are dealt with here so no test module has to remember.
"""

import builtins
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

_ADDON_ROOT = Path(__file__).resolve().parent.parent / "dradis"


def import_bot_state():
    """Import and return `bot.state`, or skip the calling test module.

    Unlike most of the suite, this pulls in the LLM and Google SDKs. They are
    present in the add-on image; anyone running the tests with only the light
    dependencies installed gets a skip rather than a failure.
    """
    if str(_ADDON_ROOT) not in sys.path:
        sys.path.insert(0, str(_ADDON_ROOT))

    if "aiomqtt" not in sys.modules:
        sys.modules["aiomqtt"] = types.ModuleType("aiomqtt")

    options = Path(tempfile.mktemp(suffix="-options.json"))
    options.write_text(json.dumps({
        "telegram_bot_token": "test", "telegram_allowed_chat_id": 1,
        "tavily_api_key": "test-key",
    }))

    real_open = builtins.open

    def patched_open(path, *args, **kwargs):
        if str(path) == "/data/options.json":
            return real_open(options, *args, **kwargs)
        return real_open(path, *args, **kwargs)

    builtins.open = patched_open
    try:
        import bot.state as st
        return st
    except ImportError as e:
        raise unittest.SkipTest(f"bot.state dependencies unavailable: {e}")
    finally:
        builtins.open = real_open
