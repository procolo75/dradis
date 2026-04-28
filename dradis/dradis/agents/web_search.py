from agent_core import create_agent, _now_str
from web.server import SETTINGS_DEFAULTS


def create_web_search_agent(settings: dict, tavily_api_key: str):
    tz_name = settings.get("timezone", "UTC") or "UTC"

    base_prompt = (
        f"It is {_now_str(tz_name)} ({tz_name}). "
        "You are a web research assistant. "
        "You have exactly ONE tool available: search_web. "
        "CRITICAL: Do NOT call open_url, fetch_url, browse_url, or any other tool — "
        "they do not exist and will cause an error. "
        "Each search_web call already returns the FULL page content; "
        "there is absolutely no need to open or fetch any URL. "
        "After calling search_web, immediately synthesise the results and respond. "
        "Synthesise ONLY the information present in the search results into a clear, concise answer. "
        "If the results do not contain enough information, say so explicitly. "
        "Never invent or assume facts not present in the results. "
        + settings.get("ws_instructions", "")
    )

    async def open_url(url: str = "", id: int = 0, **kwargs) -> str:  # noqa: ARG001
        """STUB — this tool does not exist. Do not call it.
        If you have a URL from search results, the content is already included above.
        Do NOT call this tool; call search_web instead if you need more results."""
        return (
            "ERROR: open_url is not available. "
            "The search results already contain the full page content — "
            "synthesise your answer from what search_web returned. "
            "Do NOT attempt to call open_url or any URL-fetching tool again."
        )

    from tavily import TavilyClient
    tavily_client = TavilyClient(api_key=tavily_api_key)

    async def search_web(query: str) -> str:
        """Search the web and return complete content from top results.
        Returns title, full content, and URL for each result — no further URL fetching needed.
        IMPORTANT: after calling this tool, synthesise the results directly.
        Do NOT call open_url or any other tool after this.
        Call this when the user asks for current news, prices, stock values, weather forecasts,
        sports fixtures, or recent events. Pass a concise, optimised search query."""
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
        tools=[search_web, open_url],
        name="web_search",
        tool_call_limit=3,
    )
