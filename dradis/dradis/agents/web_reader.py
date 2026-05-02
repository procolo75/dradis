import httpx

from agent_core import create_agent, _now_str
from web.server import SETTINGS_DEFAULTS


def create_web_reader_agent(settings: dict, jina_api_key: str = ""):
    tz_name = settings.get("timezone", "UTC") or "UTC"

    base_prompt = (
        f"It is {_now_str(tz_name)} ({tz_name}). "
        "You are a web page reader. "
        "When the user provides a URL, call read_url with that exact URL and summarise the content. "
        "Synthesise ONLY the information present in the returned content. "
        "If the content is insufficient, say so explicitly. "
        "Never invent or assume facts not present in the content. "
        + settings.get("ws_instructions", "")
    )

    async def read_url(url: str) -> str:
        """Fetch and return the text content of a web page.
        Call this ONLY when the user provides a specific URL starting with http:// or https://."""
        if not url.startswith("http://") and not url.startswith("https://"):
            return "Error: a valid URL starting with http:// or https:// is required."
        jina_url = f"https://r.jina.ai/{url}"
        headers = {"Accept": "text/plain"}
        if jina_api_key:
            headers["Authorization"] = f"Bearer {jina_api_key}"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(jina_url, headers=headers, follow_redirects=True)
        return resp.text[:8000]

    return create_agent(
        system_prompt=base_prompt,
        model=settings.get("ws_model", SETTINGS_DEFAULTS["ws_model"]),
        provider=settings.get("ws_provider", SETTINGS_DEFAULTS["ws_provider"]),
        tools=[read_url],
        name="web_reader",
        tool_call_limit=1,
    )
