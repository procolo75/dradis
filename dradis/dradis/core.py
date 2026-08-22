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
#
# These are the models' real windows, which is not what the free tier lets you
# spend — that is PROVIDER_TPM, and `ceiling_for()` takes the tighter of the two.
# Keeping the two apart matters the day the tier changes: gpt-oss-120b was listed
# here at 8192 because Groq's free minute is 8000, so raising the plan would have
# changed nothing at all — the runtime would have gone on trimming pages to fit a
# window the model never had.
MODEL_CONTEXT_WINDOW: dict = {
    "gpt-oss":       131072,
    "llama-3.1-8b":  131072,
    "llama-3.3-70b": 131072,
    "llama3":        8192,
    "gemma":         8192,
    "qwen":          131072,
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
# The figures we know without being told. A paid plan raises them, so the value
# is overridable per provider from the settings — see `set_provider_tpm()`.
PROVIDER_TPM_DEFAULT: dict = {"groq": 8000}
PROVIDER_TPM: dict = dict(PROVIDER_TPM_DEFAULT)

# Safety net: never loop more tool rounds than this even if a caller passes more.
_MAX_TOOL_ROUNDS = 8

# Chars per token. Deliberately low: the estimate exists to keep us UNDER a limit,
# so it must over-count rather than under-count. 3.2 was still optimistic, and it
# was optimistic about the one thing that matters — a request estimated at ~4 300
# tokens was billed at ~6 033, because three quarters of that page was URLs and
# percent-encoded signatures, which tokenise at barely two characters each. The
# constant now tracks the worst measured case rather than the average one, and
# `calibrate()` corrects it upward from the provider's own usage figures.
_CHARS_PER_TOKEN = 2.2

# Left free for the tokens our estimate cannot see: role framing, tool_call ids,
# whatever the provider wraps a request in. It used to stand in for the tool
# schemas too, which it was never big enough to do — those are counted now.
_BUDGET_HEADROOM = 192

# Two different things happen to a tool result, and telling the model which one
# is the difference between a partial answer and another round of fetching. Cut
# short: there was more, and it is gone. Dropped: the whole result is gone, it is
# not coming back, and asking for it again costs the turn.
_TRUNCATION_MARKER = "\n\n[… content truncated to fit the model budget …]"
_DROPPED_MARKER = ("[… an earlier tool result was dropped to fit the token budget. "
                   "It cannot be recovered — do not call the tool again, answer "
                   "with what is left …]")

# Floor on any single tool result. Below this a failure message ("HTTP 429 from
# r.jina.ai") would itself be trimmed to the truncation marker, which loses the
# one thing the model and the user most need to know. If honouring the floor
# overruns the budget, the next round's pre-flight reclaims the space by dropping
# older results — losing old context, not the newest failure.
_MIN_TOOL_RESULT_TOKENS = 128

_RETRY_AFTER_RE = re.compile(r"try again in ([\d.]+)\s*s", re.IGNORECASE)
_TOO_LARGE_RE   = re.compile(r"Limit\s+(\d+),\s*Requested\s+(\d+)", re.IGNORECASE)


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


def set_provider_tpm(provider: str, limit) -> None:
    """Override the rolling per-minute ceiling for one provider.

    The 8000 that shaped this whole runtime is Groq's *free* tier. It is a plan,
    not a law, so it lives in the settings: a positive figure overrides, 0 or
    blank restores whatever we know about that provider, and a provider we know
    nothing about stays unpaced.
    """
    try:
        limit = int(limit or 0)
    except (TypeError, ValueError):
        limit = 0
    if limit > 0:
        PROVIDER_TPM[provider] = limit
    elif provider in PROVIDER_TPM_DEFAULT:
        PROVIDER_TPM[provider] = PROVIDER_TPM_DEFAULT[provider]
    else:
        PROVIDER_TPM.pop(provider, None)


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


def estimate_schema_tokens(schemas: list[dict] | None) -> int:
    """What the tool definitions cost. They are prompt tokens like any other.

    Nothing counted them before: a dozen Google tool schemas went out on every
    round of every turn, priced at a flat 512-token headroom that a handful of
    them already exceeded.
    """
    if not schemas:
        return 0
    return estimate_tokens(json.dumps(schemas, ensure_ascii=False))


# How much our estimate has been observed to under-count, per provider. Keyed by
# provider because it is the tokenizer behind the API key that we are wrong about.
_ratio_correction: dict[str, float] = {}

# A correction beyond this is not a tokenizer, it is a bug in the estimate — and
# multiplying by it would starve every request of context.
_MAX_RATIO_CORRECTION = 2.0


def calibrate(provider: str, estimated: int, actual: int) -> None:
    """Learn from the request that just went out.

    `usage.prompt_tokens` is the provider stating what it billed for a payload we
    had already priced ourselves. Over-counting is the safe direction and is left
    alone; under-counting is corrected, and it is corrected upward and kept —
    being right on average is worth nothing against a hard ceiling.
    """
    if estimated <= 0 or actual <= 0:
        return
    ratio = actual / estimated
    if ratio <= 1.0:
        return
    current = _ratio_correction.get(provider, 1.0)
    _ratio_correction[provider] = min(_MAX_RATIO_CORRECTION, max(current, ratio))


def correction_for(provider: str) -> float:
    return _ratio_correction.get(provider, 1.0)


def reset_calibration() -> None:
    _ratio_correction.clear()


def estimate_request_tokens(messages: list[dict],
                            schemas: list[dict] | None = None,
                            provider: str = "") -> int:
    """The size of a whole request, as the provider will see it.

    One function so that the pacer, the trimmer and the tool-result sizer cannot
    disagree about what a request costs — they used to, and the disagreement was
    a page and a half of context wide.
    """
    raw = estimate_messages_tokens(messages) + estimate_schema_tokens(schemas)
    return int(raw * correction_for(provider))


_MD_IMAGE_RE = re.compile(r'!\[[^\]]*\]\([^)]*\)')
_MD_LINK_RE  = re.compile(r'\[([^\]]*)\]\([^)]*\)')
_BARE_URL_RE = re.compile(r'https?://\S+')

# Below this share of prose, Jina's readability pass kept the wrong part of the
# page. Measured on five reads: a school news archive scores 0.22 and its monthly
# view 0.23 — both come back without a single one of the eighteen headlines that
# are in the HTML, all menu and thumbnails — while an ANSA index (0.36), a
# Wikipedia article (0.49) and that same archive in text mode (0.91) are fine.
# The cost of being wrong here is one HTTP request, since the two readings are
# compared and the richer one wins.
MIN_PROSE_SHARE = 0.30


def clean_page(text: str) -> str:
    """Drop what a text model cannot use but pays for anyway.

    Image tags are the expensive half of a Spaggiari school page: eighteen signed
    thumbnail URLs at ~330 characters each, 4 715 of 11 615, and not one of them
    says anything. Runs of whitespace go too — Jina's text mode is generous with
    them, and they cost the same as words.
    """
    text = _MD_IMAGE_RE.sub("", text or "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def prose_chars(text: str) -> int:
    """Characters that carry meaning. A link target is an address, not a word."""
    text = _MD_IMAGE_RE.sub("", text or "")
    text = _MD_LINK_RE.sub(r"\1", text)
    text = _BARE_URL_RE.sub("", text)
    return len(re.sub(r"\s+", " ", text).strip())


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


def request_too_large(exc: BaseException) -> tuple[int, int] | None:
    """(limit, requested) when a request was refused for its size, else None.

    This reads like a rate limit — Groq answers it with `rate_limit_exceeded` —
    but it is the opposite kind of problem. A rate limit says "not now"; this says
    "not ever": the request on its own is bigger than the whole minute's budget,
    so waiting the minute out and sending the identical payload gets the identical
    refusal. The only answer is to make it smaller. It is also a free, exact
    measurement of how wrong our estimate was, which is what `calibrate()` wants.
    """
    status = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    text   = str(exc)
    if status != 413 and "too large" not in text.lower():
        return None
    match = _TOO_LARGE_RE.search(text)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def retry_after_seconds(exc: BaseException) -> float | None:
    """Seconds to wait from a rate-limit error, or None if this is not one.

    Groq puts the figure in both the `retry-after` header and the message body
    ("Please try again in 1.52s"); OpenRouter and OpenAI send the header only.
    """
    status = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    text   = str(exc)
    if request_too_large(exc):
        return None    # waiting cannot shrink a request
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


def _trim_to_window(messages: list[dict], ceiling: int, cap: int,
                    schemas: list[dict] | None = None, provider: str = "") -> None:
    """Drop the oldest tool results, in place, until the prompt fits.

    Only `tool` messages are candidates: the system prompt carries the rules and
    the last user message carries the question, so dropping either changes what
    was asked rather than how much context backs it.

    The newest tool result is spared as well. It is what the model is about to
    reason over — discarding it makes the round that fetched it pointless, and
    when the failing tool is the newest one it throws away the reason for the
    failure. If the request still does not fit after everything older is gone,
    it goes as it is and the provider decides.

    What replaces a dropped result is not the truncation marker. A model told its
    page was "truncated" goes and fetches the rest, which is exactly the round
    this function was trying to pay for; told the result is gone for good, it
    answers with what it has.
    """
    budget = ceiling - cap - _BUDGET_HEADROOM
    if budget <= 0 or estimate_request_tokens(messages, schemas, provider) <= budget:
        return
    indexes = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    for i in indexes[:-1]:
        if messages[i]["content"] == _DROPPED_MARKER:
            continue
        messages[i]["content"] = _DROPPED_MARKER
        if estimate_request_tokens(messages, schemas, provider) <= budget:
            return


_FINAL_ROUND_INSTRUCTION = (
    "The tool budget for this turn is spent. Answer now, in plain text, using "
    "only the material above. Do not call any tool."
)

_NO_TOOLS_INSTRUCTION = (
    "Tools are not available. Reply with plain text only — any tool call will be "
    "rejected. Use the material above and answer the question as best you can."
)


def flatten_tool_transcript(messages: list[dict]) -> list[dict]:
    """Rewrite tool calls and their results as plain assistant prose.

    A request whose history is full of `tool_calls` but whose body carries no
    tools is what Groq reads as `tool_choice: none`. gpt-oss, seeing a tool
    session, answers it with another tool call, and the provider refuses the
    whole request with a 400 — losing a turn that had already paid for its
    context. Flattened, there is no tool session left to continue.

    It costs one line of instruction and saves the call ids and the argument
    JSON, so on anything the size of a real page it is roughly a wash. The point
    is the round that arrives instead of the 400.
    """
    out: list[dict] = []

    def _say(text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        if out and out[-1]["role"] == "assistant":
            out[-1]["content"] = f"{out[-1]['content']}\n{text}"
        else:
            out.append({"role": "assistant", "content": text})

    for m in messages:
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            names = ", ".join((tc.get("function") or {}).get("name", "?")
                              for tc in m["tool_calls"])
            _say(f"{(m.get('content') or '').strip()}\n(consulted: {names})".strip())
        elif role == "tool":
            _say(f"Tool result:\n{m.get('content') or ''}")
        else:
            out.append(dict(m))
    out.append({"role": "user", "content": _FINAL_ROUND_INSTRUCTION})
    return out


def _is_tool_use_failed(exc: BaseException) -> bool:
    """Groq's 400 for a model that called a tool it was not offered."""
    text = str(exc)
    return "tool_use_failed" in text or "but model called a tool" in text


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
    # (tool, arguments) → what happened, for this turn only. A model whose page
    # was dropped to fit the budget asks for it again; without this the second
    # read costs as much as the first and buys nothing, which is how a two-round
    # task became a refused one.
    already_called: dict[tuple[str, str], str] = {}

    def _result(content: str) -> AgentResult:
        return AgentResult(
            content=content,
            tools_used=tools_used,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            tool_errors=tool_errors,
        )

    for round_idx in range(rounds + 1):
        final_round = not schemas or round_idx >= rounds
        _trim_to_window(messages, ceiling, cap,
                        None if final_round else schemas, provider)
        # On the final allowed round the tools come off — and so does every trace
        # of them in the history, or the model just calls one anyway. A turn that
        # never called one is left alone: there is nothing to flatten, and telling
        # a plain chat that its tool budget is spent is a lie about its own turn.
        flattened = final_round and any(m.get("tool_calls") for m in messages)
        payload   = flatten_tool_transcript(messages) if flattened else messages
        kwargs: dict = {"model": model, "messages": payload,
                        "max_tokens": cap, "temperature": temp}
        if not final_round:
            kwargs["tools"] = schemas
            kwargs["tool_choice"] = "auto"

        estimated = estimate_messages_tokens(payload) + estimate_schema_tokens(
            None if final_round else schemas)
        try:
            resp = await _create_with_pacing(client, provider, kwargs, cap)
        except Exception as e:
            if not (final_round and _is_tool_use_failed(e)):
                raise
            # Flattening is usually enough; when it is not, the documented lever
            # is a colder sample and a blunter instruction. Cheaper than handing
            # the whole turn, context and all, to the fallback model.
            print(f"[DRADIS] {provider} refused a text-only round "
                  f"(tool_use_failed) — retrying with a stricter instruction")
            kwargs["messages"] = (payload[:-1] if flattened else list(payload)) + [
                {"role": "user", "content": _NO_TOOLS_INSTRUCTION}]
            kwargs["temperature"] = 0.0
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
            calibrate(provider, estimated, round_prompt)
            print(f"[DRADIS] round={round_idx} prompt={round_prompt} "
                  f"completion={round_completion} cumulative={prompt_tokens + completion_tokens} "
                  f"tpm_used={_tpm_used(provider, time.monotonic())} "
                  f"est={estimated} x{correction_for(provider):.2f}")
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
        room  = ceiling - cap - _BUDGET_HEADROOM - estimate_request_tokens(
            messages, schemas, provider)
        share = max(_MIN_TOOL_RESULT_TOKENS, max(0, room) // max(1, len(tool_calls)))

        for tc in tool_calls:
            name = tc.function.name
            tools_used.append(name)
            fn = fn_map.get(name)
            raw_args = (tc.function.arguments or "").strip()
            try:
                args = json.loads(raw_args or "{}")
            except json.JSONDecodeError:
                args = {}
            memo_key = (name, json.dumps(args, sort_keys=True, ensure_ascii=False))
            if memo_key in already_called:
                messages.append({"role": "tool", "tool_call_id": tc.id,
                                 "content": already_called[memo_key]})
                continue
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
            already_called[memo_key] = (
                f"You already called {name} with these arguments in round "
                f"{round_idx} of this turn. Its result is above — asking again "
                f"returns the same thing and spends the budget twice. Answer "
                f"with what you have.")
            messages.append({"role": "tool", "tool_call_id": tc.id,
                             "content": fit_tool_result(str(result), share)})

    # Tool budget exhausted without a final text answer.
    return _result("")


def _fit_to_hard_limit(messages: list[dict], schemas: list[dict] | None,
                       provider: str, cap: int, limit: int) -> bool:
    """Shrink a request that the provider's whole minute could never hold.

    `tpm_wait_seconds` returns 0 for one of these, correctly — no amount of
    waiting makes a request smaller than it is. What it used to mean in practice
    was that the request went out anyway, to be refused. Older results go first,
    then the newest one is cut down; only then is it worth sending.
    """
    room = limit - cap - _BUDGET_HEADROOM
    if room <= 0:
        return False
    if estimate_request_tokens(messages, schemas, provider) <= room:
        return True
    _trim_to_window(messages, limit, cap, schemas, provider)
    over = estimate_request_tokens(messages, schemas, provider) - room
    if over <= 0:
        return True
    newest = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    if newest:
        i = newest[-1]
        keep = estimate_tokens(messages[i]["content"]) - int(over * correction_for(provider)) - 1
        messages[i]["content"] = fit_tool_result(messages[i]["content"],
                                                 max(_MIN_TOOL_RESULT_TOKENS, keep))
    return estimate_request_tokens(messages, schemas, provider) <= room


async def _create_with_pacing(client, provider: str, kwargs: dict, cap: int):
    """Send one request, waiting out the rolling TPM budget first and retrying
    once if the provider refuses it anyway.

    Waiting is the cheap option: these turns are scheduled tasks, where a few
    seconds of latency costs nothing and a refused request costs the whole run.
    But waiting only answers "not now". A request bigger than the entire minute
    is refused whatever the clock says, so that one is cut down before it is sent
    and cut down again, against the provider's own figures, if it still refuses.
    """
    schemas = kwargs.get("tools")
    limit   = PROVIDER_TPM.get(provider)
    if limit:
        _fit_to_hard_limit(kwargs["messages"], schemas, provider, cap, limit)
    need = estimate_request_tokens(kwargs["messages"], schemas, provider) + cap
    wait = tpm_wait_seconds(provider, need)
    if wait > 0:
        print(f"[DRADIS] pacing {provider}: waiting {wait:.1f}s for TPM budget "
              f"(need ~{need}, used {_tpm_used(provider, time.monotonic())})")
        await asyncio.sleep(wait)
    try:
        return await client.chat.completions.create(**kwargs)
    except Exception as e:
        oversize = request_too_large(e)
        if oversize:
            hard_limit, requested = oversize
            # The provider just told us exactly how wrong we were. Record it, then
            # send something that fits inside what it says it will accept.
            calibrate(provider, max(1, need - cap), max(1, requested - cap))
            PROVIDER_TPM[provider] = min(PROVIDER_TPM.get(provider, hard_limit), hard_limit)
            print(f"[DRADIS] {provider} refused {requested} tokens against a limit of "
                  f"{hard_limit}; re-trimming (estimate now x{correction_for(provider):.2f})")
            if not _fit_to_hard_limit(kwargs["messages"], schemas, provider, cap, hard_limit):
                raise
            return await client.chat.completions.create(**kwargs)
        delay = retry_after_seconds(e)
        if delay is None:
            raise
        print(f"[DRADIS] rate limited by {provider}, retrying in {delay + 0.5:.1f}s: {e}")
        await asyncio.sleep(delay + 0.5)
        return await client.chat.completions.create(**kwargs)
