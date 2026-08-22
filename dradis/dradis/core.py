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
A tool that cannot do its job raises `ToolError`: the message still reaches the
model (a failed tool is not a failed turn), but it is also recorded on the result
so the caller can tell the user instead of letting a fabricated answer through.
"""

import asyncio
import json
import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

API_KEYS: dict = {}

# Completion cap. Keeps prompt+completion within the model window; overridable
# from user settings via set_generation_config().
GENERATION: dict = {"max_tokens": 2048, "temperature": 0.2}

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

# Tokens-per-minute ceilings, per provider. This is NOT the context window: it is
# a rolling-60s budget that counts every request a turn makes, prompt AND
# completion, summed. Groq's free tier is the one that bites — a single task that
# reads a web page and then calls one more tool can spend 10K tokens across three
# rounds and be refused, while the same task on the same page passes when the
# model happens to answer one round earlier.
PROVIDER_TPM: dict = {"groq": 8000}

# Safety net: never loop more tool rounds than this even if a caller passes more.
_MAX_TOOL_ROUNDS = 8

# Chars per token. Deliberately low: the estimate exists to keep us UNDER a limit,
# so it must over-count rather than under-count. Real ratios run ~3.5–4.5 for
# English prose and worse for accented Italian, markdown links and tables — which
# is exactly the content read_url brings back.
_CHARS_PER_TOKEN = 3.2

# Left free for the tokens our estimate cannot see: tool schemas as the provider
# serialises them, role framing, tool_call ids.
_BUDGET_HEADROOM = 512

_TRUNCATION_MARKER = "\n\n[… content truncated to fit the model budget …]"

# Floor on any single tool result. Below this a failure message ("HTTP 429 from
# r.jina.ai") would itself be trimmed to the truncation marker, which loses the
# one thing the model and the user most need to know. If honouring the floor
# overruns the budget, the next round's pre-flight reclaims the space by dropping
# older results — losing old context, not the newest failure.
_MIN_TOOL_RESULT_TOKENS = 128

_RETRY_AFTER_RE = re.compile(r"try again in ([\d.]+)\s*s", re.IGNORECASE)


class ToolError(Exception):
    """A tool could not do its job. Non-fatal for the turn, reportable to the user."""


def setup(api_keys: dict) -> None:
    API_KEYS.update(api_keys)


def set_generation_config(max_tokens: int | None = None,
                          temperature: float | None = None) -> None:
    if max_tokens and max_tokens > 0:
        GENERATION["max_tokens"] = int(max_tokens)
    if temperature is not None:
        GENERATION["temperature"] = float(temperature)


def context_window_for(model: str) -> int:
    low = (model or "").lower()
    for key, window in MODEL_CONTEXT_WINDOW.items():
        if key in low:
            return window
    return DEFAULT_CONTEXT_WINDOW


def ceiling_for(model: str, provider: str) -> int:
    """The largest a single request may be: the tighter of window and TPM budget.

    The provider's *whole* minute is used here, not what is left of it right now.
    Sizing a page against the instantaneous remainder would make the same article
    come back a different length on every run — exactly the non-determinism this
    release exists to remove. What is left of the minute is the pacer's problem,
    and waiting is the right answer to it; permanently discarding half a page is
    not.
    """
    window = context_window_for(model)
    limit  = PROVIDER_TPM.get(provider)
    return min(window, limit) if limit else window


def estimate_tokens(text: str) -> int:
    """Conservative local token estimate — no tokenizer dependency.

    `tiktoken` would add weight and still be wrong for gpt-oss and llama, which
    is where the ceiling actually hurts. What matters is the direction of the
    error: this must never under-count.
    """
    return int(len(text or "") / _CHARS_PER_TOKEN) + 1


def estimate_messages_tokens(messages: list[dict]) -> int:
    total = 0
    for m in messages:
        total += estimate_tokens(str(m.get("content") or ""))
        for tc in m.get("tool_calls") or []:
            total += estimate_tokens(str(tc.get("function", {}).get("arguments") or ""))
            total += 8  # name + id + framing
        total += 4      # role framing
    return total


def fit_tool_result(text: str, budget: int) -> str:
    """Trim a tool result to `budget` tokens, saying so.

    The marker is not decoration: a model handed a silently halved page will
    confidently summarise the half it got as if it were the whole. Telling it the
    text was cut is what turns a wrong answer into a partial one.
    """
    text = text or ""
    if budget <= 0:
        return _TRUNCATION_MARKER.strip()
    if estimate_tokens(text) <= budget:
        return text
    # budget - 1 because estimate_tokens rounds up by one; without it the result
    # comes back one token over the budget it was handed.
    keep = max(0, int((budget - 1) * _CHARS_PER_TOKEN) - len(_TRUNCATION_MARKER))
    return text[:keep] + _TRUNCATION_MARKER


# ── Rolling tokens-per-minute budget ──────────────────────────────────────────
#
# Keyed by provider, because the ceiling belongs to the API key, not the model:
# swapping to the fallback model on the same provider draws from the same bucket,
# which is why the fallback used to fail a heartbeat after the primary.

_tpm_spend: dict[str, deque] = {}


def _tpm_used(provider: str, now: float) -> int:
    spent = _tpm_spend.get(provider)
    if not spent:
        return 0
    while spent and now - spent[0][0] >= 60.0:
        spent.popleft()
    return sum(tokens for _, tokens in spent)


def record_tpm(provider: str, tokens: int, now: float | None = None) -> None:
    if tokens <= 0:
        return
    now = time.monotonic() if now is None else now
    _tpm_spend.setdefault(provider, deque()).append((now, tokens))


def tpm_wait_seconds(provider: str, need: int, now: float | None = None) -> float:
    """How long to wait before `need` tokens fit in the rolling minute."""
    limit = PROVIDER_TPM.get(provider)
    if not limit:
        return 0.0
    now   = time.monotonic() if now is None else now
    spent = _tpm_spend.get(provider)
    used  = _tpm_used(provider, now)
    if used + need <= limit:
        return 0.0
    if not spent:
        return 0.0
    # Wait for just enough of the oldest spend to age out of the window.
    freed = 0
    for ts, tokens in spent:
        freed += tokens
        if used - freed + need <= limit:
            return max(0.0, 60.0 - (now - ts)) + 0.1
    return max(0.0, 60.0 - (now - spent[0][0])) + 0.1


def reset_tpm() -> None:
    _tpm_spend.clear()


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


def retry_after_seconds(exc: BaseException) -> float | None:
    """Seconds to wait from a rate-limit error, or None if this is not one.

    Groq puts the figure in both the `retry-after` header and the message body
    ("Please try again in 1.52s"); OpenRouter and OpenAI send the header only.
    """
    status = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    text   = str(exc)
    if status != 429 and "rate_limit" not in text and "429" not in text:
        return None
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers:
        raw = headers.get("retry-after") or headers.get("Retry-After")
        try:
            if raw is not None:
                return max(0.0, float(raw))
        except (TypeError, ValueError):
            pass
    match = _RETRY_AFTER_RE.search(text)
    if match:
        return max(0.0, float(match.group(1)))
    return 2.0  # rate-limited without a figure: a short, bounded wait


# ── Result ────────────────────────────────────────────────────────────────────

@dataclass
class AgentResult:
    content: str = ""
    tools_used: list[str] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # (tool name, human-readable reason). Populated whenever a tool raised —
    # the turn carried on, but the answer was built on less than it asked for.
    tool_errors: list[tuple[str, str]] = field(default_factory=list)

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


def _trim_to_window(messages: list[dict], ceiling: int, cap: int) -> None:
    """Drop the oldest tool results, in place, until the prompt fits.

    Only `tool` messages are candidates: the system prompt carries the rules and
    the last user message carries the question, so dropping either changes what
    was asked rather than how much context backs it.

    The newest tool result is spared as well. It is what the model is about to
    reason over — discarding it makes the round that fetched it pointless, and
    when the failing tool is the newest one it throws away the reason for the
    failure. If the request still does not fit after everything older is gone,
    it goes as it is and the provider decides.
    """
    budget = ceiling - cap - _BUDGET_HEADROOM
    if budget <= 0 or estimate_messages_tokens(messages) <= budget:
        return
    marker  = _TRUNCATION_MARKER.strip()
    indexes = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    for i in indexes[:-1]:
        if messages[i]["content"] == marker:
            continue
        messages[i]["content"] = marker
        if estimate_messages_tokens(messages) <= budget:
            return


async def run_agent(
    system_prompt: str,
    user_prompt: str,
    tools: list[dict],
    model: str,
    provider: str,
    max_tokens: int | None = None,
    tool_call_limit: int = 6,
    history: list[dict] | None = None,
    temperature: float | None = None,
) -> AgentResult:
    """Run one agent turn: call the model, execute any tool calls, loop until the
    model returns a plain text answer (or the tool-round budget is exhausted).
    Raises on API/transport errors so the caller can trigger a fallback model.
    """
    # Imported here, as httpx and tavily are elsewhere: it keeps the budget
    # arithmetic above testable without the provider SDK installed.
    from openai import AsyncOpenAI
    client   = AsyncOpenAI(api_key=_api_key_for_provider(provider),
                           base_url=_base_url_for_provider(provider))
    schemas  = _schemas(tools)
    fn_map   = {t["name"]: t["fn"] for t in tools}
    cap      = max_tokens or GENERATION["max_tokens"]
    temp     = GENERATION["temperature"] if temperature is None else temperature
    rounds   = min(max(tool_call_limit, 0), _MAX_TOOL_ROUNDS)
    ceiling  = ceiling_for(model, provider)

    messages: list[dict] = [{"role": "system", "content": system_prompt}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_prompt})

    tools_used: list[str] = []
    tool_errors: list[tuple[str, str]] = []
    prompt_tokens = completion_tokens = 0

    def _result(content: str) -> AgentResult:
        return AgentResult(
            content=content,
            tools_used=tools_used,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            tool_errors=tool_errors,
        )

    for round_idx in range(rounds + 1):
        _trim_to_window(messages, ceiling, cap)
        kwargs: dict = {"model": model, "messages": messages,
                        "max_tokens": cap, "temperature": temp}
        # On the final allowed round, drop tools to force a text answer.
        if schemas and round_idx < rounds:
            kwargs["tools"] = schemas
            kwargs["tool_choice"] = "auto"

        resp = await _create_with_pacing(client, provider, kwargs, cap)

        usage = getattr(resp, "usage", None)
        if usage:
            # Sum across tool rounds so the totals reflect the whole turn — this is
            # what actually counts against Groq's tokens-per-minute limit.
            round_prompt     = getattr(usage, "prompt_tokens", 0) or 0
            round_completion = getattr(usage, "completion_tokens", 0) or 0
            prompt_tokens     += round_prompt
            completion_tokens += round_completion
            record_tpm(provider, round_prompt + round_completion)
            print(f"[DRADIS] round={round_idx} prompt={round_prompt} "
                  f"completion={round_completion} cumulative={prompt_tokens + completion_tokens} "
                  f"tpm_used={_tpm_used(provider, time.monotonic())}")
        msg = resp.choices[0].message

        tool_calls = getattr(msg, "tool_calls", None)
        if not tool_calls:
            return _result((msg.content or "").strip())

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
        # Whatever room is left this round, shared by every result in it.
        room  = ceiling - cap - _BUDGET_HEADROOM - estimate_messages_tokens(messages)
        share = max(_MIN_TOOL_RESULT_TOKENS, max(0, room) // max(1, len(tool_calls)))

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
                tool_errors.append((name, "unknown tool"))
            else:
                try:
                    result = await fn(**args)
                except ToolError as e:
                    # Expected failure: the model is told so it can adapt, and the
                    # caller is told so the user is not handed a confident answer
                    # built on a page that never loaded.
                    result = f"Tool {name} failed: {e}"
                    tool_errors.append((name, str(e)))
                except Exception as e:  # tool failures are reported to the model, not fatal
                    result = f"Tool {name} error: {e}"
                    tool_errors.append((name, f"{type(e).__name__}: {e}"))
            messages.append({"role": "tool", "tool_call_id": tc.id,
                             "content": fit_tool_result(str(result), share)})

    # Tool budget exhausted without a final text answer.
    return _result("")


async def _create_with_pacing(client, provider: str, kwargs: dict, cap: int):
    """Send one request, waiting out the rolling TPM budget first and retrying
    once if the provider rate-limits anyway.

    Waiting is the cheap option: these turns are scheduled tasks, where a few
    seconds of latency costs nothing and a refused request costs the whole run.
    """
    need = estimate_messages_tokens(kwargs["messages"]) + cap
    wait = tpm_wait_seconds(provider, need)
    if wait > 0:
        print(f"[DRADIS] pacing {provider}: waiting {wait:.1f}s for TPM budget "
              f"(need ~{need}, used {_tpm_used(provider, time.monotonic())})")
        await asyncio.sleep(wait)
    try:
        return await client.chat.completions.create(**kwargs)
    except Exception as e:
        delay = retry_after_seconds(e)
        if delay is None:
            raise
        print(f"[DRADIS] rate limited by {provider}, retrying in {delay + 0.5:.1f}s: {e}")
        await asyncio.sleep(delay + 0.5)
        return await client.chat.completions.create(**kwargs)
