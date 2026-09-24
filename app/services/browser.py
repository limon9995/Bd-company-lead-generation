"""Shared headless-browser session for browser-mode sources (Google Maps, web search, directories, Facebook).

Politeness rules, applied everywhere:
  * one browser job per source at a time (``source_lock``) and a random wait before every page load;
  * a normal desktop Chrome user agent - no stealth plugins, no fingerprint spoofing;
  * if a page shows a CAPTCHA, "unusual traffic", or a login wall, we raise ``SourceBlocked`` and stop.
    CAPTCHAs are never solved or bypassed; the source is paused (Settings → Browser scraping).
"""
import random
import re
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from app.config import config
from app.services.errors import SourceBlocked

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/128.0.0.0 Safari/537.36")
_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)

# Checked against the URL and the *visible* page text only (not the HTML), so a contact form with an
# embedded reCAPTCHA widget on a company site is not mistaken for a block page.
BLOCK_URL_PATTERNS = {
    "google": [r"google\.[a-z.]+/sorry/"],
    "bing": [r"bing\.com/(challenge|turing)"],
    "facebook": [r"facebook\.com/login", r"facebook\.com/checkpoint"],
}
BLOCK_TEXT_PATTERNS = {
    "google": [r"our systems have detected unusual traffic", r"unusual traffic from your computer network"],
    "duckduckgo": [r"bots use DuckDuckGo too", r"complete the following challenge"],
    "bing": [r"verify you are a human", r"solve the challenge"],
    "facebook": [r"you must log in to continue", r"log in to facebook to continue", r"log into facebook"],
    "generic": [r"verify you are human", r"checking your browser before accessing", r"access denied"],
}


def source_lock(source: str) -> threading.Lock:
    return _locks[source]


def detect_block(source: str, url: str, visible_text: str) -> str | None:
    for pat in BLOCK_URL_PATTERNS.get(source, []):
        if re.search(pat, url or "", re.I):
            return pat
    text = (visible_text or "")[:20_000]
    # generic phrases only count on short pages (real block pages are short; long pages may just mention them)
    generic = BLOCK_TEXT_PATTERNS["generic"] if len(text) < 3000 else []
    for pat in BLOCK_TEXT_PATTERNS.get(source, []) + generic:
        if re.search(pat, text, re.I):
            return pat
    return None


class BrowserSession:
    """Context manager around one Chromium instance + one tab."""

    def __init__(self, source: str, *, delay: tuple[float, float] = (4, 9), proxy: str = "", timeout: float = 30,
                 headless: bool = True):
        self.source, self.delay, self.proxy, self.timeout_ms, self.headless = source, delay, proxy, int(timeout * 1000), headless
        self.pages_loaded = 0
        self._last_load = 0.0

    def __enter__(self):
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        launch = {"headless": self.headless}
        if config.chromium_executable:
            launch["executable_path"] = config.chromium_executable
        if self.proxy:
            launch["proxy"] = {"server": self.proxy}
        self._browser = self._pw.chromium.launch(**launch)
        self._ctx = self._browser.new_context(user_agent=USER_AGENT, locale="en-US", viewport={"width": 1366, "height": 900},
                                              timezone_id=config.timezone)
        self.page = self._ctx.new_page()
        self.page.set_default_timeout(self.timeout_ms)
        return self

    def __exit__(self, *exc):
        for closer in (self._ctx.close, self._browser.close, self._pw.stop):
            try:
                closer()
            except Exception:  # noqa: BLE001 - best effort cleanup
                pass

    def wait_politely(self) -> None:
        lo, hi = self.delay
        target = self._last_load + random.uniform(lo, max(lo, hi))
        pause = target - time.monotonic()
        if pause > 0 and self._last_load:
            time.sleep(pause)

    def goto(self, url: str, wait_until: str = "domcontentloaded"):
        self.wait_politely()
        self.page.goto(url, wait_until=wait_until)
        self._last_load = time.monotonic()
        self.pages_loaded += 1
        self.raise_if_blocked()
        return self.page

    def after_action(self) -> None:
        """Call after a click/Enter that loaded new content (same politeness + block check as goto)."""
        self._last_load = time.monotonic()
        self.pages_loaded += 1
        self.raise_if_blocked()

    def raise_if_blocked(self) -> None:
        try:
            text = self.page.inner_text("body", timeout=5000)
        except Exception:  # noqa: BLE001 - page without a body yet
            text = ""
        hit = detect_block(self.source, self.page.url, text)
        if hit:
            raise SourceBlocked(self.source, f"matched '{hit}' at {self.page.url[:120]}")

    def html(self) -> str:
        return self.page.content()


def session_for(db, source: str) -> BrowserSession:
    from app import settings_store

    return BrowserSession(source, delay=(settings_store.get(db, "browser_delay_min"), settings_store.get(db, "browser_delay_max")),
                          proxy=settings_store.get(db, "browser_proxy") or "",
                          timeout=settings_store.get(db, "crawl_timeout_seconds") + 10)


# ---- pause bookkeeping (stored with the settings, shown in Settings → Browser scraping)
def blocked_until(db, source: str) -> datetime | None:
    from app.models import Setting
    from app.security import decrypt

    row = db.get(Setting, f"_blocked:{source}")
    if row is None or not row.value_encrypted:
        return None
    until = datetime.fromisoformat(decrypt(row.value_encrypted))
    return until if until > datetime.now(timezone.utc) else None


def set_blocked(db, source: str, hours: int) -> datetime:
    from app.models import Setting
    from app.security import encrypt

    until = datetime.now(timezone.utc) + timedelta(hours=hours)
    key = f"_blocked:{source}"
    row = db.get(Setting, key) or Setting(key=key, is_secret=False)
    row.value_encrypted = encrypt(until.isoformat())
    db.add(row)
    return until


def clear_blocked(db, source: str) -> None:
    from app.models import Setting

    row = db.get(Setting, f"_blocked:{source}")
    if row is not None:
        db.delete(row)


def ensure_not_paused(db, source: str) -> None:
    until = blocked_until(db, source)
    if until:
        err = SourceBlocked(source, f"paused until {until.isoformat(timespec='minutes')}")
        err.already_paused = True
        raise err


# ---- "act like a person" helpers: type into boxes, press Enter, wheel-scroll, click
SEARCH_BOX_SELECTORS = [
    "input#searchboxinput", "textarea#sb_form_q", "input#sb_form_q", "input[name=q]", "textarea[name=q]",
    "input[type=search]", "input[name=search]", "input[name=s]", "input[name=keyword]", "input[name=query]",
    "input[placeholder*='earch' i]",
]


def find_search_box(page):
    """First visible text search box on the page (never a password/email field)."""
    for sel in SEARCH_BOX_SELECTORS:
        loc = page.locator(sel)
        for i in range(min(loc.count(), 5)):
            el = loc.nth(i)
            try:
                if el.is_visible() and (el.get_attribute("type") or "text").lower() not in ("password", "email", "hidden"):
                    return el
            except Exception:  # noqa: BLE001 - element went away
                continue
    return None


def type_like_person(page, locator, text: str, submit: bool = True) -> None:
    locator.click()
    locator.fill("")
    page.keyboard.type(text, delay=random.randint(60, 150))
    page.wait_for_timeout(random.randint(300, 800))
    if submit:
        page.keyboard.press("Enter")


def wheel_scroll(page, over_selector: str | None = None, steps: int = 4) -> None:
    """Scroll with the mouse wheel in small steps, over a panel if given (e.g. the Maps results list)."""
    if over_selector:
        box = page.locator(over_selector).first.bounding_box()
        if box:
            page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    for _ in range(steps):
        page.mouse.wheel(0, random.randint(350, 700))
        page.wait_for_timeout(random.randint(250, 600))
