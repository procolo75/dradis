from agent_core import create_agent, _now_str
from web.server import SETTINGS_DEFAULTS


def create_web_search_agent(settings: dict, tavily_api_key: str):
    tz_name = settings.get("timezone", "UTC") or "UTC"

    base_prompt = (
        f"It is {_now_str(tz_name)} ({tz_name}). "
        "You are a web search assistant. "
        "When the user asks a question or wants to find information, call search_web with a "
        "concise, optimised query and synthesise the results into a clear answer. "
        "Synthesise ONLY the information present in the results. "
        "If the results do not contain enough information, say so explicitly. "
        "Never invent or assume facts not present in the results. "
        + settings.get("ws_instructions", "")
    )

    from tavily import TavilyClient
    tavily_client = TavilyClient(api_key=tavily_api_key)

    async def search_web(query: str) -> str:
        """Search the web and return complete content from top results.
        Returns title, full content, and URL for each result.
        Call this when the user asks for current news, prices, stock values, weather forecasts,
        sports fixtures, recent events, or any question requiring a web search.
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
        tool_call_limit=3,
    )
