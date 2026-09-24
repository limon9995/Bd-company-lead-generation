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

from app.services.places import Place

SEARCH_URL = "https://www.google.com/maps/search/{q}?hl=en"
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
