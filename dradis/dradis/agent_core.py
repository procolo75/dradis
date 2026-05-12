import json
from datetime import datetime
from pathlib import Path

from agno.agent import Agent
from agno.models.openai.like import OpenAILike

API_KEYS: dict = {}


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
