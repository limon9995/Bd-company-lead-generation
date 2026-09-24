"""Web search through a real browser (no API key): DuckDuckGo HTML results or Bing.

Both sites' terms restrict automated use; this mode is free but slow and can be blocked, in which case the
source is paused (SourceBlocked) - never bypassed.
"""
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

from selectolax.parser import HTMLParser

from app.services.search import SearchResult

DDG_URL = "https://html.duckduckgo.com/html/?q={q}&kl=bd-en"
BING_URL = "https://www.bing.com/search?q={q}&setlang=en&cc=BD"


def _ddg_target(href: str) -> str:
    """DuckDuckGo wraps result links: //duckduckgo.com/l/?uddg=<encoded target>."""
    if "uddg=" in href:
        q = parse_qs(urlparse(href if "://" in href else "https:" + href).query)
        if q.get("uddg"):
            return unquote(q["uddg"][0])
    return href


def parse_duckduckgo(html: str) -> list[SearchResult]:
    out = []
    for r in HTMLParser(html).css("div.result"):
        a = r.css_first("a.result__a")
        if a is None or "result--ad" in (r.attributes.get("class") or ""):
            continue
        snip = r.css_first(".result__snippet")
        out.append(SearchResult(a.text(strip=True), _ddg_target(a.attributes.get("href") or ""),
                                snip.text(strip=True) if snip else ""))
    return out


def parse_bing(html: str) -> list[SearchResult]:
    out = []
    for r in HTMLParser(html).css("li.b_algo"):
        a = r.css_first("h2 a")
        if a is None:
            continue
        snip = r.css_first(".b_caption p") or r.css_first("p")
        out.append(SearchResult(a.text(strip=True), a.attributes.get("href") or "", snip.text(strip=True) if snip else ""))
    return out


ENGINES = {"duckduckgo": (DDG_URL, parse_duckduckgo), "bing": (BING_URL, parse_bing)}


def search(session, engine: str, query: str, num: int = 10, base_url: str | None = None) -> list[SearchResult]:
    url_tpl, parse = ENGINES[engine]
    session.goto((base_url or url_tpl).format(q=quote_plus(query)))
    return parse(session.html())[:num]
