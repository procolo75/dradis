"""
tests/test_ha_mcp.py
────────────────────
DRADIS acting on Home Assistant through HA's MCP server.

    cd dradis && python3 -m unittest discover tests

No HA here: a MockTransport plays the stateless `/api/mcp/<api_id>` endpoint of
HA 2026.10, which answers each JSON-RPC POST with one JSON body.
"""

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "dradis"))

import httpx                                                      # noqa: E402

import core                                                       # noqa: E402
from tools import ha_mcp                                          # noqa: E402

_TOOLS = [
    {"name": "HassTurnOn", "description": "Turns on a device",
     "inputSchema": {"$schema": "http://json-schema.org/draft-07/schema#",
                     "type": "object",
                     "properties": {"name": {"type": "string"},
                                    "area": {"type": "string", "enum": ["Studio", "Cucina"]}}}},
    {"name": "GetLiveContext", "description": "Current states", "inputSchema": {}},
]


def _fake_ha(seen: list, *, status=200, call_result=None, sse=False):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append((str(request.url), request.headers.get("authorization"), body))
        if status != 200:
            return httpx.Response(status, text="nope")
        method = body["method"]
        if method == "initialize":
            result = {"serverInfo": {"name": "home-assistant", "version": "2026.10.1"}}
        elif method == "tools/list":
            result = {"tools": _TOOLS}
        elif method == "tools/call":
            result = call_result or {"content": [{"type": "text", "text": "ok"}]}
        else:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                             "error": {"code": -32601, "message": "Method not found"}})
        msg = {"jsonrpc": "2.0", "id": body["id"], "result": result}
        if sse:
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  text="event: message\ndata: " + json.dumps(msg) + "\n\n")
        return httpx.Response(200, json=msg)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


_SETTINGS = {"ha_mcp_enabled": True}


class Endpoint(unittest.TestCase):

    def test_defaults_to_the_supervisor_and_the_assist_api(self):
        with mock.patch.dict(os.environ, {"SUPERVISOR_TOKEN": "sv"}):
            self.assertEqual(ha_mcp.endpoint({}),
                             ("http://supervisor/core/api/mcp/assist", "sv", "supervisor"))

    def test_a_custom_url_brings_its_own_token(self):
        s = {"ha_mcp_url": "http://ha:8123/api/mcp/assist", "ha_mcp_token": "llat"}
        self.assertEqual(ha_mcp.endpoint(s), ("http://ha:8123/api/mcp/assist", "llat", "token"))


class Rpc(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"SUPERVISOR_TOKEN": "sv"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(lambda: ha_mcp._TOOLS_CACHE.clear())

    async def test_probe_reads_server_and_tools_with_the_bearer_token(self):
        seen = []
        async with _fake_ha(seen) as client:
            found = await ha_mcp.probe(_SETTINGS, client)
        self.assertEqual(found["server"]["version"], "2026.10.1")
        self.assertEqual([t["name"] for t in found["tools"]], ["HassTurnOn", "GetLiveContext"])
        self.assertEqual(seen[0][1], "Bearer sv")
        self.assertEqual(seen[0][0], "http://supervisor/core/api/mcp/assist")

    async def test_an_sse_reply_is_read_too(self):
        async with _fake_ha([], sse=True) as client:
            found = await ha_mcp.probe(_SETTINGS, client)
        self.assertEqual(len(found["tools"]), 2)

    async def test_404_says_where_to_turn_the_server_on(self):
        async with _fake_ha([], status=404) as client:
            with self.assertRaises(ha_mcp.McpError) as ctx:
                await ha_mcp.probe(_SETTINGS, client)
        self.assertIn("Settings → System → AI", str(ctx.exception))

    async def test_no_token_at_all_is_explained_not_sent(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ha_mcp.McpError) as ctx:
                await ha_mcp.probe(_SETTINGS)
        self.assertIn("homeassistant_api", str(ctx.exception))

    async def test_refresh_failure_leaves_no_tools_and_does_not_raise(self):
        ha_mcp._TOOLS_CACHE[:] = _TOOLS
        async with _fake_ha([], status=401) as client:
            await ha_mcp.refresh(_SETTINGS, client)
        self.assertEqual(ha_mcp._TOOLS_CACHE, [])
        self.assertFalse(ha_mcp.STATUS["ok"])
        self.assertIn("401", ha_mcp.STATUS["error"])

    async def test_refresh_when_disabled_clears_the_cache_without_calling_ha(self):
        ha_mcp._TOOLS_CACHE[:] = _TOOLS
        seen = []
        async with _fake_ha(seen) as client:
            await ha_mcp.refresh({"ha_mcp_enabled": False}, client)
        self.assertEqual((ha_mcp._TOOLS_CACHE, seen), ([], []))

    async def test_call_returns_the_text_blocks(self):
        seen = []
        async with _fake_ha(seen, call_result={"content": [
                {"type": "text", "text": "Turned on"}, {"type": "image", "data": "x"}]}) as client:
            out = await ha_mcp.call_tool(_SETTINGS, "HassTurnOn", {"name": "Luce"}, client)
        self.assertEqual(out, "Turned on")
        self.assertEqual(seen[0][2]["params"], {"name": "HassTurnOn", "arguments": {"name": "Luce"}})

    async def test_an_is_error_result_is_a_tool_error(self):
        async with _fake_ha([], call_result={"isError": True, "content": [
                {"type": "text", "text": "No entity named Luce"}]}) as client:
            with self.assertRaises(core.ToolError) as ctx:
                await ha_mcp.call_tool(_SETTINGS, "HassTurnOn", {}, client)
        self.assertIn("No entity named Luce", str(ctx.exception))

    async def test_an_unreachable_ha_is_a_tool_error(self):
        async with _fake_ha([], status=500) as client:
            with self.assertRaises(core.ToolError):
                await ha_mcp.call_tool(_SETTINGS, "HassTurnOn", {}, client)


class Specs(unittest.TestCase):

    def test_specs_carry_a_clean_object_schema(self):
        specs = ha_mcp.ha_mcp_tools(_SETTINGS, _TOOLS)
        self.assertEqual([s["name"] for s in specs], ["HassTurnOn", "GetLiveContext"])
        self.assertNotIn("$schema", specs[0]["parameters"])
        self.assertEqual(specs[1]["parameters"], {"type": "object", "properties": {}})
        self.assertTrue(callable(specs[0]["fn"]))

    def test_the_allowlist_filters(self):
        specs = ha_mcp.ha_mcp_tools({"ha_mcp_allowed": ["GetLiveContext"]}, _TOOLS)
        self.assertEqual([s["name"] for s in specs], ["GetLiveContext"])

    def test_an_empty_cache_means_no_tools(self):
        self.assertEqual(ha_mcp.ha_mcp_tools(_SETTINGS, []), [])

    def test_schema_tokens_counts_the_enum(self):
        self.assertGreater(ha_mcp.schema_tokens(_TOOLS[0]), ha_mcp.schema_tokens(_TOOLS[1]))


class SpecForwardsArguments(unittest.IsolatedAsyncioTestCase):
    """HassTurnOn has an argument called `name`: it must reach HA untouched."""

    async def test_a_name_argument_is_not_the_tool_name(self):
        captured = {}

        async def fake_call(settings, name, args, client=None):
            captured.update(tool=name, args=args)
            return "ok"

        spec = ha_mcp.ha_mcp_tools(_SETTINGS, _TOOLS)[0]
        with mock.patch.object(ha_mcp, "call_tool", fake_call):
            await spec["fn"](name="Luce studio", area="Studio")
        self.assertEqual(captured, {"tool": "HassTurnOn",
                                    "args": {"name": "Luce studio", "area": "Studio"}})


if __name__ == "__main__":
    unittest.main()
