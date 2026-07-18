"""
core.py — agno-free agent runtime
──────────────────────────────────
A thin tool-calling loop over any OpenAI-compatible provider (OpenRouter, OpenAI,
GitHub Models, Gemini, Groq) using the `openai` SDK.

This replaces the previous agno Agent/Team layer. Measurements on Groq showed
agno added ~8000 prompt tokens of framework overhead per request (8 tools:
797 tokens raw vs 8797 with agno), which made the 8K free-tier limit unreachable
for any multi-tool task. The runtime here sends only the system prompt, the
conversation and the exact tool schemas we choose — nothing else.

A "tool" is a plain spec dict:
    {"name": str, "description": str, "parameters": <JSON schema>, "fn": async callable}
`fn` is called with the JSON arguments the model produced and must return a str.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime

from openai import AsyncOpenAI

API_KEYS: dict = {}

# Completion cap. Keeps prompt+completion within the model window; overridable
# from user settings via set_generation_config().
GENERATION: dict = {"max_tokens": 2048}

# Known context windows (total tokens) per model id. Substring match, first hit.
MODEL_CONTEXT_WINDOW: dict = {
    "gpt-oss":       8192,
    "llama-3.1-8b":  8192,
    "llama-3.3-70b": 8192,
    "llama3":        8192,
    "gemma":         8192,
    "qwen":          32768,
    "mixtral":       32768,
    "gpt-4o":        128000,
    "gemini":        1000000,
    "nemotron":      128000,
}
DEFAULT_CONTEXT_WINDOW = 8192

# Safety net: never loop more tool rounds than this even if a caller passes more.
_MAX_TOOL_ROUNDS = 8


def setup(api_keys: dict) -> None:
    API_KEYS.update(api_keys)


def set_generation_config(max_tokens: int | None = None) -> None:
    if max_tokens and max_tokens > 0:
        GENERATION["max_tokens"] = int(max_tokens)


def context_window_for(model: str) -> int:
    low = (model or "").lower()
    for key, window in MODEL_CONTEXT_WINDOW.items():
        if key in low:
            return window
    return DEFAULT_CONTEXT_WINDOW


def _api_key_for_provider(provider_id: str) -> str:
    return API_KEYS.get(provider_id, "")


def _base_url_for_provider(provider_id: str) -> str:
    from web.store import PROVIDERS
    for p in PROVIDERS:
        if p["id"] == provider_id:
            return p["base_url"]
    return PROVIDERS[0]["base_url"]


def _now_str(tz_name: str | None = None) -> str:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        tz = ZoneInfo(tz_name or "UTC")
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")
    return datetime.now(tz).strftime("%A %d %B %Y, %H:%M")


# ── Result ────────────────────────────────────────────────────────────────────

@dataclass
class AgentResult:
    content: str = ""
    tools_used: list[str] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def failed(self) -> bool:
        return not (self.content or "").strip()


# ── Tool-calling loop ─────────────────────────────────────────────────────────

def _schemas(tools: list[dict]) -> list[dict]:
    return [
        {"type": "function", "function": {
            "name":        t["name"],
            "description": t["description"],
            "parameters":  t.get("parameters") or {"type": "object", "properties": {}},
        }}
        for t in tools
    ]


async def run_agent(
    system_prompt: str,
    user_prompt: str,
    tools: list[dict],
    model: str,
    provider: str,
    max_tokens: int | None = None,
    tool_call_limit: int = 6,
    history: list[dict] | None = None,
) -> AgentResult:
    """Run one agent turn: call the model, execute any tool calls, loop until the
    model returns a plain text answer (or the tool-round budget is exhausted).
    Raises on API/transport errors so the caller can trigger a fallback model.
    """
    client   = AsyncOpenAI(api_key=_api_key_for_provider(provider),
                           base_url=_base_url_for_provider(provider))
    schemas  = _schemas(tools)
    fn_map   = {t["name"]: t["fn"] for t in tools}
    cap      = max_tokens or GENERATION["max_tokens"]
    rounds   = min(max(tool_call_limit, 0), _MAX_TOOL_ROUNDS)

    messages: list[dict] = [{"role": "system", "content": system_prompt}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_prompt})

    tools_used: list[str] = []
    prompt_tokens = completion_tokens = 0

    for round_idx in range(rounds + 1):
        kwargs: dict = {"model": model, "messages": messages, "max_tokens": cap}
        # On the final allowed round, drop tools to force a text answer.
        if schemas and round_idx < rounds:
            kwargs["tools"] = schemas
            kwargs["tool_choice"] = "auto"

        resp  = await client.chat.completions.create(**kwargs)
        usage = getattr(resp, "usage", None)
        if usage:
            # Sum across tool rounds so the totals reflect the whole turn — this is
            # what actually counts against Groq's tokens-per-minute limit.
            prompt_tokens     += getattr(usage, "prompt_tokens", 0) or 0
            completion_tokens += getattr(usage, "completion_tokens", 0) or 0
        msg = resp.choices[0].message

        tool_calls = getattr(msg, "tool_calls", None)
        if not tool_calls:
            return AgentResult(
                content=(msg.content or "").strip(),
                tools_used=tools_used,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )

        # Echo the assistant tool-call message, then append each tool result.
        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in tool_calls
            ],
        })
        for tc in tool_calls:
            name = tc.function.name
            tools_used.append(name)
            fn = fn_map.get(name)
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            if fn is None:
                result = f"Error: unknown tool {name!r}."
            else:
                try:
                    result = await fn(**args)
                except Exception as e:  # tool failures are reported to the model, not fatal
                    result = f"Tool {name} error: {e}"
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": str(result)})

    # Tool budget exhausted without a final text answer.
    return AgentResult(
        content="",
        tools_used=tools_used,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )
