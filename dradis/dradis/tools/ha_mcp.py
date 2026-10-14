"""
tools/ha_mcp.py
───────────────
Home Assistant actions for DRADIS, through HA's own MCP server.

HA 2026.10 serves Model Context Protocol from Settings → System → AI. We talk to
it with a few lines of JSON-RPC over httpx instead of the `mcp` SDK, for the same
reason `core.py` exists: nothing goes out that we did not choose to send.

Endpoint: `/api/mcp/<api_id>`, `assist` by default. Not `/api/mcp`, which serves
every LLM API the instance has (KNX, music search, …) — every one of those tool
schemas would ride along on every chat turn — and which new installs restrict to
administrators. The keyed Assist endpoint is open to any authenticated user and
only sees the entities exposed in Settings → Voice assistants → Expose.

The server is stateless: each POST is one JSON-RPC request answered with one
JSON body. No session id, no initialize handshake needed before tools/call.

`build_tools()` is synchronous, so the tool list is fetched ahead of time by
`refresh()` (startup, settings save, Test connection) and served from a cache.
"""

import json
import os

import httpx

import core as agent_core

SUPERVISOR_URL = "http://supervisor/core/api/mcp/{api_id}"
_TIMEOUT = 20

# Last tools/list answer, raw MCP tool dicts. Empty until a refresh succeeds:
# a failed refresh leaves DRADIS without HA tools rather than with stale ones.
_TOOLS_CACHE: list[dict] = []
# What the last refresh found, for logs and the Web UI.
STATUS: dict = {"ok": None, "error": "", "auth_mode": "", "url": ""}


class McpError(Exception):
    """The MCP endpoint could not answer. The message is meant for the user."""


def endpoint(settings: dict) -> tuple[str, str, str]:
    """(url, token, auth_mode). A URL in the settings wins; otherwise the
    Supervisor proxy with the token the Supervisor hands every add-on that
    declares `homeassistant_api`."""
    api_id = (settings.get("ha_mcp_api_id") or "assist").strip() or "assist"
    url    = (settings.get("ha_mcp_url") or "").strip()
    if url:
        return url, (settings.get("ha_mcp_token") or "").strip(), "token"
    return (SUPERVISOR_URL.format(api_id=api_id),
            os.environ.get("SUPERVISOR_TOKEN", ""), "supervisor")


def _explain_status(code: int, url: str, auth_mode: str) -> str:
    if code == 401:
        return ("Home Assistant rejected the token (401)."
                + (" Paste a long-lived access token in the HA Control panel."
                   if auth_mode == "supervisor" else " Check the long-lived access token."))
    if code == 403:
        return ("Forbidden (403): this endpoint requires an administrator account. "
                "Use the 'assist' API id, or a token of an admin user.")
    if code == 404:
        return ("Not found (404) at " + url + ". Turn the MCP server on in "
                "Settings → System → AI, and check the API id.")
    return f"HTTP {code} from {url}"


def _parse_body(resp: httpx.Response) -> dict:
    """One JSON-RPC message. HA answers plain JSON; a generic Streamable HTTP
    server may answer with an SSE stream, whose last `data:` line is the reply."""
    if "text/event-stream" in resp.headers.get("content-type", ""):
        data = [line[5:].strip() for line in resp.text.splitlines()
                if line.startswith("data:")]
        if not data:
            raise McpError("Empty event stream from the MCP endpoint")
        return json.loads(data[-1])
    return resp.json()


async def _rpc(settings: dict, method: str, params: dict | None = None,
               client: httpx.AsyncClient | None = None) -> dict:
    url, token, auth_mode = endpoint(settings)
    if not token:
        raise McpError("No token: SUPERVISOR_TOKEN is missing (is "
                       "homeassistant_api enabled?) and no long-lived token is set.")
    body = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    headers = {"Authorization": f"Bearer {token}",
               "Accept": "application/json, text/event-stream",
               "Content-Type": "application/json"}
    own = client is None
    client = client or httpx.AsyncClient(timeout=_TIMEOUT)
    try:
        resp = await client.post(url, json=body, headers=headers)
    except httpx.HTTPError as e:
        raise McpError(f"Cannot reach {url}: {e}") from e
    finally:
        if own:
            await client.aclose()
    if resp.status_code >= 400:
        raise McpError(_explain_status(resp.status_code, url, auth_mode))
    msg = _parse_body(resp)
    if "error" in msg:
        err = msg["error"] or {}
        raise McpError(f"MCP error {err.get('code')}: {err.get('message')}")
    return msg.get("result") or {}


async def probe(settings: dict, client: httpx.AsyncClient | None = None) -> dict:
    """initialize + tools/list, for the Test connection button."""
    init = await _rpc(settings, "initialize", {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "dradis", "version": "1"},
    }, client)
    listed = await _rpc(settings, "tools/list", {}, client)
    url, _, auth_mode = endpoint(settings)
    return {"server": init.get("serverInfo") or {}, "tools": listed.get("tools") or [],
            "url": url, "auth_mode": auth_mode}


async def refresh(settings: dict, client: httpx.AsyncClient | None = None) -> None:
    """Re-read the tool list. Never raises: HA being down must not stop DRADIS."""
    global _TOOLS_CACHE
    if not settings.get("ha_mcp_enabled"):
        _TOOLS_CACHE = []
        STATUS.update(ok=None, error="", auth_mode="", url="")
        return
    url, _, auth_mode = endpoint(settings)
    try:
        listed = await _rpc(settings, "tools/list", {}, client)
        _TOOLS_CACHE = listed.get("tools") or []
        STATUS.update(ok=True, error="", auth_mode=auth_mode, url=url)
        print(f"[DRADIS] HA MCP: {len(_TOOLS_CACHE)} tools via {auth_mode} ({url})")
    except Exception as e:
        _TOOLS_CACHE = []
        STATUS.update(ok=False, error=str(e), auth_mode=auth_mode, url=url)
        print(f"[DRADIS] HA MCP refresh failed: {e}")


async def call_tool(settings: dict, name: str, args: dict,
                    client: httpx.AsyncClient | None = None) -> str:
    try:
        result = await _rpc(settings, "tools/call",
                            {"name": name, "arguments": args or {}}, client)
    except McpError as e:
        raise agent_core.ToolError(str(e)) from e
    text = "\n".join(c.get("text", "") for c in result.get("content") or []
                     if c.get("type") == "text").strip()
    if result.get("isError"):
        raise agent_core.ToolError(text or "Home Assistant reported an error")
    return text or "Done."


def _parameters(tool: dict) -> dict:
    schema = dict(tool.get("inputSchema") or {})
    schema.pop("$schema", None)
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    return schema


def schema_tokens(tool: dict) -> int:
    """What this tool costs on every turn it is attached to."""
    return agent_core.estimate_schema_tokens([{
        "type": "function",
        "function": {"name": tool.get("name", ""),
                     "description": tool.get("description", ""),
                     "parameters": _parameters(tool)}}])


def ha_mcp_tools(settings: dict, cache: list[dict] | None = None) -> list[dict]:
    """Tool specs for `core.run_agent`, filtered by the panel's allowlist
    (empty = all). The Assist schemas enumerate area and device names, so on a
    small per-minute budget the allowlist is what keeps chat affordable."""
    allowed = set(settings.get("ha_mcp_allowed") or [])
    specs = []
    for tool in _TOOLS_CACHE if cache is None else cache:
        name = tool.get("name")
        if not name or (allowed and name not in allowed):
            continue

        async def fn(_name=name, **kwargs):
            return await call_tool(settings, _name, kwargs)

        specs.append({"name": name, "description": tool.get("description", ""),
                      "parameters": _parameters(tool), "fn": fn})
    return specs
