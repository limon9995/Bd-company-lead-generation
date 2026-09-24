"""Pluggable web search (used only on public search results - no logged-in scraping)."""
from dataclasses import dataclass

import httpx

from app.services.errors import ProviderError


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str


def serper(api_key: str, query: str, num: int = 10, client: httpx.Client | None = None) -> list[SearchResult]:
    c = client or httpx.Client(timeout=30)
    try:
        r = c.post("https://google.serper.dev/search", json={"q": query, "gl": "bd", "num": num},
                   headers={"X-API-KEY": api_key, "Content-Type": "application/json"})
    except httpx.HTTPError as exc:
        raise ProviderError(f"Serper request failed: {exc}") from exc
    finally:
        if client is None:
            c.close()
    if r.status_code != 200:
        raise ProviderError(f"Serper {r.status_code}: {r.text[:300]}")
    return [SearchResult(o.get("title", ""), o.get("link", ""), o.get("snippet", "")) for o in r.json().get("organic", [])]


def brave(api_key: str, query: str, num: int = 10, client: httpx.Client | None = None) -> list[SearchResult]:
    c = client or httpx.Client(timeout=30)
    try:
        r = c.get("https://api.search.brave.com/res/v1/web/search", params={"q": query, "count": min(num, 20), "country": "BD"},
                  headers={"X-Subscription-Token": api_key, "Accept": "application/json"})
    except httpx.HTTPError as exc:
        raise ProviderError(f"Brave request failed: {exc}") from exc
    finally:
        if client is None:
            c.close()
    if r.status_code != 200:
        raise ProviderError(f"Brave {r.status_code}: {r.text[:300]}")
    results = (r.json().get("web") or {}).get("results", [])
    return [SearchResult(o.get("title", ""), o.get("url", ""), o.get("description", "")) for o in results]


PROVIDERS = {"serper": serper, "brave": brave}


def search(provider: str, api_key: str, query: str, num: int = 10) -> list[SearchResult]:
    fn = PROVIDERS.get(provider)
    if fn is None:
        raise ValueError(f"Unknown search provider: {provider}")
    return fn(api_key, query, num)
