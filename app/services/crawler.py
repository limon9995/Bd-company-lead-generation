"""Polite company-website crawler.

Plain HTTP first (fast, cheap); falls back to a headless Chromium (Playwright) when the site
needs JavaScript to render. Only same-site pages likely to mention leadership or contact
details are visited, robots.txt is respected, and requests to one host are spaced out.
"""
import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urldefrag, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from selectolax.parser import HTMLParser

from app.config import config
from app.services.normalize import find_bd_phones, find_emails, host_of, social_kind
from app.services.usage import limiter

log = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (compatible; BDLeadResearchBot/1.0; +business-contact-research)"
PAGE_TEXT_LIMIT = 8000
# (keyword, weight) - English + common Bangla words seen on BD corporate sites
LINK_KEYWORDS = [
    ("leadership", 10), ("management", 9), ("board", 9), ("director", 9), ("team", 8), ("founder", 8),
    ("chairman", 8), ("ceo", 8), ("message", 7), ("about", 6), ("who-we-are", 6), ("our-people", 7),
    ("profile", 4), ("company", 3), ("contact", 5), ("principal", 7), ("committee", 6),
    ("পরিচালনা", 9), ("পরিচিতি", 6), ("আমাদের", 5), ("যোগাযোগ", 5), ("বাণী", 7),
]
SKIP_EXT = re.compile(r"\.(pdf|jpe?g|png|gif|webp|svg|zip|rar|docx?|xlsx?|pptx?|mp4|mp3)(\?|$)", re.I)


@dataclass
class PageData:
    url: str
    title: str
    text: str


@dataclass
class CrawlResult:
    pages: list[PageData] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    socials: dict[str, str] = field(default_factory=dict)
    used_browser: bool = False
    error: str = ""

    @property
    def text(self) -> str:
        return "\n\n".join(f"[PAGE] {p.url}\n{p.text}" for p in self.pages)


# Anti-bot interstitials (Cloudflare & co). We never try to get past them - the site is refusing automated access.
BOT_WALL = re.compile(r"performing security verification|checking your browser|verify(ing)? you are (a )?human|"
                      r"verifies you are not a bot|attention required! \| cloudflare|enable javascript and cookies to "
                      r"continue|ddos protection by|just a moment\.\.\.", re.I)


def is_bot_wall(text: str) -> bool:
    return len(text) < 2000 and bool(BOT_WALL.search(text))


class HttpFetcher:
    def __init__(self, timeout: float):
        self._timeout = timeout
        self.client = httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT},
                                   verify=True)
        self._insecure: httpx.Client | None = None

    def fetch(self, url: str) -> tuple[str, str]:
        try:
            r = self.client.get(url)
        except httpx.ConnectError as exc:
            if "CERTIFICATE_VERIFY_FAILED" not in str(exc):
                raise
            # Many BD sites have expired / mismatched certificates. We only read public pages and send
            # nothing, so read them anyway rather than losing the company.
            if self._insecure is None:
                self._insecure = httpx.Client(timeout=self._timeout, follow_redirects=True,
                                              headers={"User-Agent": USER_AGENT}, verify=False)
            r = self._insecure.get(url)
        r.raise_for_status()
        ctype = r.headers.get("content-type", "")
        if "html" not in ctype and "text" not in ctype:
            raise ValueError(f"not html: {ctype}")
        return str(r.url), r.text

    def close(self):
        self.client.close()
        if self._insecure is not None:
            self._insecure.close()


class BrowserFetcher:
    def __init__(self, timeout: float):
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        launch = {"headless": True}
        if config.chromium_executable:
            launch["executable_path"] = config.chromium_executable
        self._browser = self._pw.chromium.launch(**launch)
        # ignore_https_errors: read-only visits to sites with broken certificates (see HttpFetcher.fetch)
        self._ctx = self._browser.new_context(user_agent=USER_AGENT, ignore_https_errors=True)
        self._timeout_ms = int(timeout * 1000)

    def fetch(self, url: str) -> tuple[str, str]:
        page = self._ctx.new_page()
        try:
            page.goto(url, timeout=self._timeout_ms, wait_until="domcontentloaded")
            page.wait_for_timeout(1500)
            return page.url, page.content()
        finally:
            page.close()

    def close(self):
        for closer in (self._ctx.close, self._browser.close, self._pw.stop):
            try:
                closer()
            except Exception:  # noqa: BLE001 - best effort cleanup
                pass


def html_to_text(html: str) -> tuple[str, str]:
    tree = HTMLParser(html)
    title = tree.css_first("title").text(strip=True) if tree.css_first("title") else ""
    for tag in tree.css("script, style, noscript, svg, iframe, template"):
        tag.decompose()
    body = tree.body or tree.root
    text = body.text(separator="\n") if body else ""
    lines = [re.sub(r"[ \t ]+", " ", ln).strip() for ln in text.splitlines()]
    text = "\n".join(ln for ln in lines if ln)
    return title, text[:PAGE_TEXT_LIMIT]


def extract_links(html: str, base_url: str) -> list[tuple[str, str]]:
    tree = HTMLParser(html)
    out = []
    for a in tree.css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if not href or href.startswith(("javascript:", "#")):
            continue
        out.append((urldefrag(urljoin(base_url, href))[0], a.text(strip=True)))
    return out


def rank_candidate_links(links: list[tuple[str, str]], site_host: str) -> list[str]:
    scored: dict[str, int] = {}
    for url, label in links:
        if host_of(url) != site_host or SKIP_EXT.search(url) or url.startswith("mailto:"):
            continue
        hay = (urlparse(url).path + " " + label).lower()
        score = sum(w for kw, w in LINK_KEYWORDS if kw in hay)
        if score:
            scored[url] = max(score, scored.get(url, 0))
    return [u for u, _ in sorted(scored.items(), key=lambda kv: -kv[1])]


def _robots(fetch_client: httpx.Client, root: str) -> RobotFileParser | None:
    rp = RobotFileParser()
    try:
        r = fetch_client.get(urljoin(root, "/robots.txt"), timeout=10)
    except httpx.HTTPError:
        return None
    if r.status_code >= 400:
        return None
    rp.parse(r.text.splitlines())
    return rp


def crawl(website: str, *, max_pages: int = 8, delay: float = 2.0, timeout: float = 20.0,
          respect_robots: bool = True, use_browser: bool = True, fetchers: dict | None = None) -> CrawlResult:
    """`fetchers` lets tests inject fakes: {"http": obj, "browser": callable -> obj}."""
    result = CrawlResult()
    if not website:
        result.error = "no website"
        return result
    if "://" not in website:
        website = "http://" + website
    site_host = host_of(website)
    root = f"{urlparse(website).scheme}://{urlparse(website).netloc}"

    http = (fetchers or {}).get("http") or HttpFetcher(timeout)
    make_browser = (fetchers or {}).get("browser") or (lambda: BrowserFetcher(timeout))
    browser = None
    robots = None
    if respect_robots and isinstance(http, HttpFetcher):
        robots = _robots(http.client, root)

    def allowed(url: str) -> bool:
        return robots is None or robots.can_fetch(USER_AGENT, url)

    def get(url: str) -> tuple[str, str]:
        nonlocal browser
        limiter.wait(site_host, delay)
        if browser is not None:
            return browser.fetch(url)
        return http.fetch(url)

    try:
        if not allowed(website):
            result.error = "blocked by robots.txt"
            return result
        try:
            final_url, html = get(website)
            _, home_text = html_to_text(html)
        except Exception as exc:  # noqa: BLE001
            final_url, html, home_text = website, "", ""
            log.info("http fetch failed for %s: %s", website, exc)
        if len(home_text) < 300 and use_browser:
            try:
                browser = make_browser()
                result.used_browser = True
                final_url, html = get(website)
            except Exception as exc:  # noqa: BLE001
                if not html:
                    result.error = f"could not load site: {exc}"[:300]
                    return result
        if not html:
            result.error = "could not load site"
            return result
        if is_bot_wall(html_to_text(html)[1]):
            result.error = "the site is behind bot protection (e.g. Cloudflare) - not crawled"
            return result

        site_host = host_of(final_url) or site_host
        visited = {final_url}
        queue = [final_url] + rank_candidate_links(extract_links(html, final_url), site_host)
        pages_html: dict[str, str] = {final_url: html}
        for url in queue:
            if len(result.pages) >= max_pages:
                break
            if url not in pages_html:
                if url in visited or not allowed(url):
                    continue
                visited.add(url)
                try:
                    url, pages_html[url] = get(url)
                except Exception as exc:  # noqa: BLE001
                    log.info("skip %s: %s", url, exc)
                    continue
            page_html = pages_html[url]
            title, text = html_to_text(page_html)
            if is_bot_wall(text):
                continue
            result.pages.append(PageData(url=url, title=title, text=text))
            for link, _label in extract_links(page_html, url):
                if link.startswith("mailto:"):
                    for e in find_emails(link[7:].split("?")[0]):
                        if e not in result.emails:
                            result.emails.append(e)
                elif link.startswith("tel:"):
                    for p in find_bd_phones(link[4:]):
                        if p not in result.phones:
                            result.phones.append(p)
                else:
                    kind = social_kind(link)
                    if kind and kind not in result.socials:
                        result.socials[kind] = link
            for e in find_emails(text):
                if e not in result.emails:
                    result.emails.append(e)
            for p in find_bd_phones(text):
                if p not in result.phones:
                    result.phones.append(p)
        return result
    finally:
        for f in (http, browser):
            if f is not None and hasattr(f, "close"):
                f.close()
