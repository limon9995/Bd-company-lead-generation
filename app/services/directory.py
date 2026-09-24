"""Scrape companies from any public directory / listing page the admin provides (e.g. a trade association
member list or a business directory category page). The browser renders each page, Gemini turns the page
text into structured entries, and we follow the "next page" link up to a limit.

Guard: an entry is kept only if its name literally appears in the page text (no invented companies).
"""
import re
from dataclasses import dataclass
from urllib.parse import urljoin

from selectolax.parser import HTMLParser

from app.services.crawler import html_to_text
from app.services.llm import LLMProvider
from app.services.normalize import squash_ws

PAGE_TEXT_LIMIT = 24000
NEXT_LABELS = re.compile(r"^(next|next page|next ›|next »|›|»|>|>>|পরবর্তী|পরের)$", re.I)
SYSTEM = "You convert a directory web page into structured data. Use only what the page says; never guess."


@dataclass
class DirectoryEntry:
    name: str
    category: str = ""
    address: str = ""
    city: str = ""
    phone: str = ""
    email: str = ""
    website: str = ""
    detail_url: str = ""


def page_text(html: str) -> str:
    title, text = html_to_text(html)
    return text[:PAGE_TEXT_LIMIT]


def find_next_url(html: str, current_url: str) -> str | None:
    tree = HTMLParser(html)
    link = tree.css_first('a[rel~="next"], link[rel~="next"]')
    if link is not None and link.attributes.get("href"):
        return urljoin(current_url, link.attributes["href"])
    for a in tree.css("a[href]"):
        label = squash_ws(a.text()) or (a.attributes.get("aria-label") or "")
        if NEXT_LABELS.match(label.strip()):
            return urljoin(current_url, a.attributes["href"])
    return None


def build_prompt(url: str, text: str, links: list[tuple[str, str]]) -> str:
    link_lines = "\n".join(f"- {label[:80]} -> {href}" for href, label in links[:300] if label)
    return (
        f"Directory page: {url}\n\n"
        "List every business / organisation listed on this page. Return a JSON array; each item: "
        '{"name": str, "category": str, "address": str, "city": str, "phone": str, "email": str, '
        '"website": str, "detail_url": str}. Use "" when the page does not show a value. '
        "detail_url = the link to that business's own detail page on this directory, if any (pick from LINKS). "
        "Ignore navigation, ads and footer links. Return [] if the page lists no businesses.\n\n"
        f"PAGE TEXT:\n{text}\n\nLINKS (label -> url):\n{link_lines}"
    )


def extract_entries(llm: LLMProvider, url: str, html: str) -> list[DirectoryEntry]:
    text = page_text(html)
    links = []
    for a in HTMLParser(html).css("a[href]"):
        links.append((urljoin(url, a.attributes.get("href") or ""), squash_ws(a.text())))
    raw = llm.generate_json(build_prompt(url, text, links), SYSTEM)
    if isinstance(raw, dict):
        raw = raw.get("items") or raw.get("businesses") or []
    norm_text = squash_ws(text).lower()
    out: list[DirectoryEntry] = []
    seen = set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        name = squash_ws(str(item.get("name", "")))
        if len(name) < 2 or name.lower() in seen or name.lower() not in norm_text:
            continue  # guard: must be on the page
        seen.add(name.lower())
        out.append(DirectoryEntry(name=name, **{k: squash_ws(str(item.get(k, "") or "")) for k in
                                                 ("category", "address", "city", "phone", "email", "website", "detail_url")}))
    return out
