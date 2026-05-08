import json
from datetime import datetime
from pathlib import Path

from agno.agent import Agent
from agno.models.openai.like import OpenAILike

API_KEYS: dict = {}

TOKEN_STATS_FILE   = Path("/data/dradis_token_stats.json")
_TOKEN_CATEGORIES  = ("dradis", "weather", "ws", "gcal", "gmail", "gtasks")
_TOKEN_STATS: dict = {}


def setup(api_keys: dict):
    API_KEYS.update(api_keys)


def _api_key_for_provider(provider_id: str) -> str:
    return API_KEYS.get(provider_id, "")


def _base_url_for_provider(provider_id: str) -> str:
    from web.server import PROVIDERS
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


def create_agent(
    system_prompt: str,
    model: str,
    provider: str,
    tools: list | None = None,
    name: str = "DRADIS",
    tool_call_limit: int | None = None,
) -> Agent:
    return Agent(
        name=name,
        model=OpenAILike(
            id=model,
            api_key=_api_key_for_provider(provider),
            base_url=_base_url_for_provider(provider),
        ),
        instructions=system_prompt,
        tools=tools or [],
        markdown=False,
        tool_call_limit=tool_call_limit,
    )


def create_team(system_prompt: str, model: str, provider: str, members: list, tools: list | None = None):
    from agno.team import Team
    return Team(
        name="DRADIS",
        mode="coordinate",
        members=members,
        model=OpenAILike(
            id=model,
            api_key=_api_key_for_provider(provider),
            base_url=_base_url_for_provider(provider),
        ),
        instructions=system_prompt,
        tools=tools or [],
        store_member_responses=True,
        markdown=False,
    )


def _load_token_stats() -> dict:
    default = {k: {"models": {}} for k in _TOKEN_CATEGORIES}
    default["last_reset"] = None
    try:
        data = json.loads(TOKEN_STATS_FILE.read_text())
        for k in _TOKEN_CATEGORIES:
            if k not in data:
                data[k] = {"models": {}}
            elif "in" in data[k]:
                # migrate flat format → per-model
                old = data[k]
                data[k] = {"models": {"unknown": {
                    "in": old.get("in", 0), "out": old.get("out", 0),
                    "cr": old.get("cr", 0), "cw": old.get("cw", 0),
                }}}
            else:
                data[k].setdefault("models", {})
        data.pop("model", None)
        data.setdefault("last_reset", None)
        return data
    except Exception:
        return default


def init_token_stats():
    _TOKEN_STATS.clear()
    _TOKEN_STATS.update(_load_token_stats())


def _save_token_stats():
    try:
        TOKEN_STATS_FILE.write_text(json.dumps(_TOKEN_STATS, ensure_ascii=False))
    except Exception as e:
        print(f"[DRADIS] WARNING: could not save token stats: {e}")


def _extract_tokens(response) -> tuple[int, int, int, int]:
    try:
        m = response.metrics
        def _sum_key(key):
            v = m.get(key) if isinstance(m, dict) else getattr(m, key, None)
            if v is None:
                return 0
            if isinstance(v, list):
                return sum(int(x) for x in v if x is not None)
            return int(v)
        return (
            _sum_key("input_tokens"),
            _sum_key("output_tokens"),
            _sum_key("cached_tokens"),
            _sum_key("cache_creation_input_tokens"),
        )
    except Exception:
        return 0, 0, 0, 0


def _add_tokens(category: str, response):
    if response is None:
        return
    in_t, out_t, cr_t, cw_t = _extract_tokens(response)
    if in_t == 0 and out_t == 0 and cr_t == 0 and cw_t == 0:
        return
    model = getattr(response, "model", None) or "unknown"
    bucket = _TOKEN_STATS[category]["models"].setdefault(
        model, {"in": 0, "out": 0, "cr": 0, "cw": 0}
    )
    bucket["in"]  += in_t
    bucket["out"] += out_t
    bucket["cr"]  += cr_t
    bucket["cw"]  += cw_t
    _save_token_stats()
