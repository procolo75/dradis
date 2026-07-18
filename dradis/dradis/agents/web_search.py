def web_search_tools(settings: dict, tavily_api_key: str) -> list[dict]:
    """Return the Web Search (Tavily) tool specs."""
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
            f"Title: {r['title']}\n{r['content'][:800]}\nURL: {r['url']}"
            for r in results
        )

    return [
        {"name": "search_web", "fn": search_web,
         "description": "Search the web and return content from the top results. Call this for current news, prices, stock values, weather, sports fixtures, recent events, or any question needing a web search. Pass a concise, optimised query.",
         "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    ]
