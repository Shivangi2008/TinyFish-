"""TinyFish REST client: Fetch (rendered page), Search (SERP), Agent (real-browser UX audit)."""
import os

import httpx

FETCH_URL = "https://api.fetch.tinyfish.ai"
SEARCH_URL = "https://api.search.tinyfish.ai"
AGENT_URL = "https://agent.tinyfish.ai/v1/automation/run"


class TinyFishError(RuntimeError):
    pass


def _headers() -> dict:
    key = os.environ.get("TINYFISH_API_KEY")
    if not key:
        raise TinyFishError("TINYFISH_API_KEY is not set")
    return {"X-API-Key": key, "Content-Type": "application/json"}


async def fetch_rendered(http: httpx.AsyncClient, url: str) -> dict:
    """Load the page in a real browser and return the post-JavaScript HTML + head metadata."""
    resp = await http.post(
        FETCH_URL,
        headers=_headers(),
        json={
            "urls": [url],
            "format": "html",
            "links": True,
            "image_links": True,
            "page_metadata": True,
            "ttl": 0,
            "per_url_timeout_ms": 60000,
            "purpose": "SEO audit: compare the JavaScript-rendered page with its raw HTML",
        },
        timeout=90,
    )
    if resp.status_code >= 400:
        raise TinyFishError(f"Fetch failed ({resp.status_code}): {resp.text[:300]}")
    data = resp.json()
    if not data.get("results"):
        raise TinyFishError(f"Fetch returned no result: {data.get('errors')}")
    return data["results"][0]


async def search(http: httpx.AsyncClient, query: str, location: str = "US") -> dict:
    """Live web search, used to check whether the page shows up for its own topic."""
    resp = await http.get(
        SEARCH_URL,
        headers=_headers(),
        params={"query": query, "location": location},
        timeout=30,
    )
    if resp.status_code >= 400:
        raise TinyFishError(f"Search failed ({resp.status_code}): {resp.text[:300]}")
    return resp.json()


UX_GOAL = """You are auditing this web page for SEO user-experience problems that crawlers and visitors hit.
Never submit forms, log in, buy anything, or follow links to other pages.

1. Wait for the page to finish loading. BEFORE closing anything, note whether a cookie banner,
   newsletter popup, modal, app-install banner or interstitial covers the main content. Describe it.
2. Close that overlay if there is one (prefer "close" or "reject"; never "accept all" marketing).
3. Read the main visible headline of the page.
4. Scroll down to the bottom of the page once. Note content that only appeared after scrolling,
   and sections that stayed blank, stuck on "loading", or showed placeholders.
5. Note visible error messages, broken images, or empty sections.

Return JSON matching the schema. Use false, empty strings or empty arrays when nothing was found."""

UX_SCHEMA = {
    "type": "object",
    "properties": {
        "blocking_overlay": {"type": "boolean"},
        "overlay_description": {"type": "string"},
        "main_headline": {"type": "string"},
        "content_visible_without_interaction": {"type": "boolean"},
        "content_loaded_on_scroll": {"type": "string"},
        "broken_or_empty_elements": {"type": "array", "items": {"type": "string"}},
        "ux_issues": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "blocking_overlay",
        "overlay_description",
        "main_headline",
        "content_visible_without_interaction",
        "content_loaded_on_scroll",
        "broken_or_empty_elements",
        "ux_issues",
    ],
}


async def ux_audit(http: httpx.AsyncClient, url: str) -> dict:
    """Run the TinyFish web agent on the page like a real visitor. Metered (per step)."""
    resp = await http.post(
        AGENT_URL,
        headers=_headers(),
        json={
            "url": url,
            "goal": UX_GOAL,
            "output_schema": UX_SCHEMA,
            "agent_config": {"max_steps": 15, "max_duration_seconds": 150},
        },
        timeout=200,
    )
    try:
        data = resp.json()
    except ValueError:
        raise TinyFishError(f"Agent failed ({resp.status_code}): {resp.text[:300]}")
    if data.get("status") != "COMPLETED" or not data.get("result"):
        err = data.get("error") or {}
        raise TinyFishError(f"Agent run failed: {err.get('message') or resp.text[:300]}")
    result = data["result"]
    # COMPLETED only means the browser ran; the goal itself can still report failure.
    if result.get("status") == "failure":
        raise TinyFishError(f"Agent could not audit the page: {result.get('reason')}")
    result["_run_id"] = data.get("run_id")
    result["_steps"] = data.get("num_of_steps")
    return result
