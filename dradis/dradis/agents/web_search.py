from agno.tools.jina import JinaReaderTools

from agent_core import create_agent, _now_str
from web.server import SETTINGS_DEFAULTS


def create_web_search_agent(settings: dict, tavily_api_key: str):
    tz_name = settings.get("timezone", "UTC") or "UTC"

    base_prompt = (
        f"It is {_now_str(tz_name)} ({tz_name}). "
        "You are a web research assistant. "
        "You have TWO tools available: search_web and read_url. "
        "Use search_web when the user asks a question or wants to find information on the web — "
        "pass a concise, optimised search query and synthesise the results into a clear answer. "
        "Use read_url when the user provides a specific URL they want you to read or summarise — "
        "pass the URL exactly as given and synthesise the returned content. "
        "After calling either tool, synthesise ONLY the information present in the results. "
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
        tools=[
            search_web,
            JinaReaderTools(
                enable_read_url=True,
                enable_search_query=False,
                max_content_length=8000,
            ),
        ],
        name="web_search",
        tool_call_limit=3,
    )
