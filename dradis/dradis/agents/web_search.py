from agent_core import create_agent, _now_str
from web.server import SETTINGS_DEFAULTS


async def fetch_web_search(query: str, tavily_api_key: str) -> str:
    from tavily import TavilyClient
    raw     = TavilyClient(api_key=tavily_api_key).search(query=query, max_results=5)
    results = raw.get("results", [])
    if not results:
        return "No web results found for this query. Do not invent information."
    return "\n\n".join(
        f"Title: {r['title']}\n{r['content']}\nURL: {r['url']}"
        for r in results
    )


def create_web_search_agent(settings: dict, tavily_api_key: str, prefetched_data: str | None = None):
    tz_name = settings.get("timezone", "UTC") or "UTC"

    base_prompt = (
        f"It is {_now_str(tz_name)} ({tz_name}). "
        "You are a web research assistant. Synthesise ONLY the information "
        "present in the search results into a clear, concise answer. "
        "If the results do not contain enough information, say so explicitly. "
        "Never invent or assume facts not present in the results. "
        + settings.get("ws_instructions", "")
    )

    if prefetched_data:
        return create_agent(
            system_prompt=base_prompt + f"\n\nPre-fetched search results:\n{prefetched_data}",
            model=settings.get("ws_model", SETTINGS_DEFAULTS["ws_model"]),
            provider=settings.get("ws_provider", SETTINGS_DEFAULTS["ws_provider"]),
            tools=[],
            name="web_search",
            tool_call_limit=2,
        )

    from tavily import TavilyClient
    tavily_client = TavilyClient(api_key=tavily_api_key)

    async def search_web(query: str) -> str:
        """Search the web for current information and return raw search results.
        Call this when the user asks for current news, prices, stock values, or recent events,
        or uses phrases like 'search for', 'look up', 'find online', 'latest on',
        or when you need information that may have changed since your training cutoff.
        Pass a concise, optimised search query."""
        raw     = tavily_client.search(query=query, max_results=5)
        results = raw.get("results", [])
        if not results:
            return "No web results found for this query. Do not invent information."
        return "\n\n".join(
            f"Title: {r['title']}\n{r['content']}\nURL: {r['url']}"
            for r in results
        )

    return create_agent(
        system_prompt=base_prompt,
        model=settings.get("ws_model", SETTINGS_DEFAULTS["ws_model"]),
        provider=settings.get("ws_provider", SETTINGS_DEFAULTS["ws_provider"]),
        tools=[search_web],
        name="web_search",
        tool_call_limit=2,
    )
