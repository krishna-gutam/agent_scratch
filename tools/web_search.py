import os
import json
from tavily import TavilyClient
from .decorator import tool

@tool("""Searches the web for information using Tavily.

    Args:
        query: The search query.
        justification: Explain why you need to search the web for this information.
    """)
def web_search(query: str, justification: str) -> str:
    """Searches the web for information using Tavily.

    Args:
        query: The search query.
        justification: Explain why you need to search the web for this information.
    """
    try:
        api_key = os.getenv("TAVILY_API_KEY")
        if not api_key:
            return "Error: TAVILY_API_KEY not found in environment variables."
        tavily = TavilyClient(api_key=api_key)
        response = tavily.search(query=query, search_depth="advanced")
        
        # Extract only the necessary keys
        filtered_response = {
            #"answer": response.get("answer"),
            "results": [
                {"url": r.get("url"), "title": r.get("title"), "content": r.get("content")}
                for r in response.get("results", [])
            ]
        }
        return json.dumps(filtered_response, indent=2)
    except Exception as e:
        return f"Error performing web search: {e}"
