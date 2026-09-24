"""Google Maps in a real (headless) browser - an alternative to the paid Places API.

Honest notes (also shown in the admin panel):
  * Google's Terms of Service do not allow automated collection from Maps pages. The official Places API
    is the compliant option; this mode exists because it costs nothing. The admin chooses per campaign.
  * Google changes the page structure from time to time. Selectors below prefer stable attributes
    (data-item-id, aria-label, role) but may still need an update; tests/fixtures/maps_* document the
    structure this parser expects.
  * On a consent page we click the normal "Reject all"/"Accept all" button. On "unusual traffic" /
    CAPTCHA pages we stop (SourceBlocked) - no solving, no bypass.
"""
import re
from urllib.parse import quote_plus, unquote, urlsplit

from selectolax.parser import HTMLParser

from app.services.browser import find_search_box, type_like_person, wheel_scroll
from app.services.places import Place

SEARCH_URL = "https://www.google.com/maps/search/{q}?hl=en"
MAPS_HOME = "https://www.google.com/maps?hl=en"
FEED = 'div[role="feed"]'
END_OF_LIST = re.compile(r"reached the end of the list", re.I)


def place_key_from_url(url: str) -> str:
    """Stable id from a Maps URL: the real place id (ChIJ...) when present, else the feature id (0x..:0x..)."""
    u = unquote(url or "")
    m = re.search(r"!19s(ChIJ[\w-]+)", u)
    if m:
        return m.group(1)
    m = re.search(r"!1s(0x[0-9a-f]+:0x[0-9a-f]+)", u)
    if m:
        return "maps:" + m.group(1)
    m = re.search(r"/maps/place/([^/@?]+)", u)
    return "maps:" + m.group(1).lower() if m else ""


def _aria(node) -> str:
    return (node.attributes.get("aria-label") or "").strip() if node else ""


def _strip_label(text: str, *labels: str) -> str:
    for lab in labels:
        if text.lower().startswith(lab.lower()):
            return text[len(lab):].strip(" :")
    return text


def parse_place_page(html: str, url: str) -> Place:
    tree = HTMLParser(html)
    h1 = tree.css_first("h1")
    name = h1.text(strip=True) if h1 else ""
    cat = tree.css_first('button[jsaction*="category"]')
    category = cat.text(strip=True) if cat else ""
    address = _strip_label(_aria(tree.css_first('button[data-item-id="address"]')), "Address")
    phone = ""
    ph = tree.css_first('button[data-item-id^="phone:tel:"]')
    if ph:
        phone = (ph.attributes.get("data-item-id") or "").split("phone:tel:", 1)[-1] or _strip_label(_aria(ph), "Phone")
    web = tree.css_first('a[data-item-id="authority"]')
    website = (web.attributes.get("href") or "") if web else ""
    rating, reviews = None, None
    for node in tree.css("[aria-label]"):
        label = _aria(node)
        if rating is None:
            m = re.match(r"^([0-5](?:\.\d)?)\s+stars?", label)
            if m:
                rating = float(m.group(1))
        if reviews is None:
            m = re.match(r"^([\d,]+)\s+reviews?", label)
            if m:
                reviews = int(m.group(1).replace(",", ""))
    body_text = tree.body.text(separator=" ") if tree.body else ""
    status = "CLOSED_PERMANENTLY" if re.search(r"Permanently closed", body_text) else \
        "CLOSED_TEMPORARILY" if re.search(r"Temporarily closed", body_text) else "OPERATIONAL"
    return Place(place_id=place_key_from_url(url), name=name, address=address, phone=phone, website=website,
                 rating=rating, reviews_count=reviews, maps_url=url, category=category, business_status=status)


def result_links(html: str, origin: str = "https://www.google.com") -> list[str]:
    seen, out = set(), []
    for a in HTMLParser(html).css('a[href*="/maps/place/"]'):
        href = a.attributes.get("href") or ""
        if href.startswith("/"):
            href = origin + href
        key = place_key_from_url(href)
        if key and key not in seen:
            seen.add(key)
            out.append(href)
    return out


def _accept_consent(page) -> None:
    if "consent." not in page.url:
        return
    for label in ("Reject all", "Accept all", "I agree"):
        btn = page.get_by_role("button", name=label)
        if btn.count():
            btn.first.click()
            page.wait_for_load_state("domcontentloaded")
            return


def collect_links(session, query: str, max_results: int, base_url: str = SEARCH_URL, max_scrolls: int = 25) -> list[str]:
    """Open a Maps search and scroll the results list until enough place links are loaded."""
    page = session.goto(base_url.format(q=quote_plus(query)))
    _accept_consent(page)
    parts = urlsplit(page.url)
    origin = f"{parts.scheme}://{parts.netloc}"
    session.raise_if_blocked()
    if "/maps/place/" in page.url:  # Google jumped straight to a single place
        return [page.url]
    try:
        page.wait_for_selector('div[role="feed"]', timeout=15000)
    except Exception:  # noqa: BLE001 - no list (no results) or a layout we don't know
        return result_links(page.content(), origin)
    links: list[str] = []
    stale = 0
    for _ in range(max_scrolls):
        links = result_links(page.content(), origin)
        if len(links) >= max_results:
            break
        before = len(links)
        page.eval_on_selector('div[role="feed"]', "el => el.scrollBy(0, el.scrollHeight)")
        page.wait_for_timeout(1800)
        if END_OF_LIST.search(page.inner_text('div[role="feed"]')):
            links = result_links(page.content(), origin)
            break
        stale = stale + 1 if len(result_links(page.content(), origin)) == before else 0
        if stale >= 3:
            break
    session.raise_if_blocked()
    return links[:max_results]


def read_place(session, url: str) -> Place:
    page = session.goto(url)
    try:
        page.wait_for_selector("h1", timeout=15000)
    except Exception:  # noqa: BLE001
        pass
    return parse_place_page(page.content(), page.url or url)


# ---------------------------------------------------------------- "type & scroll" mode (acts like a person)
def search_by_typing(session, query: str, max_results: int, home_url: str = MAPS_HOME, max_scrolls: int = 30) -> list[str]:
    """Open Maps, type the query into the search box, press Enter, then wheel-scroll the results panel."""
    page = session.goto(home_url)
    _accept_consent(page)
    session.raise_if_blocked()
    box = find_search_box(page)
    if box is None:  # layout we don't recognise: fall back to the search URL
        return collect_links(session, query, max_results)
    session.wait_politely()
    type_like_person(page, box, query)
    try:
        page.wait_for_function(
            "() => document.querySelector('div[role=\"feed\"]') || location.href.includes('/maps/place/')", timeout=20000)
    except Exception:  # noqa: BLE001 - no results
        pass
    session.after_action()
    parts = urlsplit(page.url)
    origin = f"{parts.scheme}://{parts.netloc}"
    if page.locator(FEED).count() == 0:
        return [page.url] if "/maps/place/" in page.url else []
    links: list[str] = []
    stale = 0
    for _ in range(max_scrolls):
        links = result_links(page.content(), origin)
        if len(links) >= max_results:
            break
        before = len(links)
        wheel_scroll(page, FEED, steps=3)
        page.wait_for_timeout(1200)
        if END_OF_LIST.search(page.inner_text(FEED)):
            links = result_links(page.content(), origin)
            break
        stale = stale + 1 if len(result_links(page.content(), origin)) == before else 0
        if stale >= 3:
            break
    session.raise_if_blocked()
    return links[:max_results]


def click_result(session, link: str) -> Place:
    """Click a result in the list (like a person), read the details panel, then go back to the list.
    Falls back to opening the place URL directly if the result can't be clicked."""
    page = session.page
    key = place_key_from_url(link)
    target = None
    anchors = page.locator(f'{FEED} a[href*="/maps/place/"]')
    for i in range(anchors.count()):
        a = anchors.nth(i)
        if place_key_from_url(a.get_attribute("href") or "") == key:
            target = a
            break
    if target is None:
        return read_place(session, link)
    session.wait_politely()
    try:
        target.scroll_into_view_if_needed()
        target.click()
        page.wait_for_function("k => location.href.includes('/maps/place/')", arg=key, timeout=15000)
        page.wait_for_selector("h1", timeout=15000)
    except Exception:  # noqa: BLE001
        return read_place(session, link)
    session.after_action()
    place = parse_place_page(page.content(), page.url)
    if not place.place_id:
        place.place_id = key
    # back to the results list for the next click
    back = page.locator('button[aria-label="Back"]')
    try:
        if back.count():
            back.first.click()
        else:
            page.go_back()
        page.wait_for_selector(FEED, timeout=10000)
    except Exception:  # noqa: BLE001 - list lost; the next result falls back to its URL
        pass
    return place
