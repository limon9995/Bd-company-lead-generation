"""Read a company's *public* Facebook page without logging in.

Facebook shows limited information to logged-out visitors and often a login wall; Meta's terms do not
allow automated collection. We never log in. When the wall appears we return what is visible (often
nothing) and mark it, so the admin can see how useful this source really is.
"""
from dataclasses import dataclass, field
from urllib.parse import urlparse

from app.services.crawler import html_to_text
from app.services.errors import SourceBlocked
from app.services.normalize import find_bd_phones, find_emails


@dataclass
class FacebookResult:
    url: str
    text: str = ""
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    login_wall: bool = False


def canonical(url: str) -> str:
    p = urlparse(url if "://" in url else "https://" + url)
    path = p.path.rstrip("/")
    return f"https://www.facebook.com{path}" if path else ""


def read_page(session, url: str) -> FacebookResult:
    target = canonical(url)
    res = FacebookResult(url=target)
    if not target:
        return res
    try:
        session.goto(target)
    except SourceBlocked:
        res.login_wall = True
        return res
    page = session.page
    for label in ("Close", "Not now"):  # dismiss the login pop-up if it can be closed
        try:
            btn = page.get_by_role("button", name=label)
            if btn.count():
                btn.first.click(timeout=2000)
                break
        except Exception:  # noqa: BLE001
            pass
    _, text = html_to_text(page.content())
    res.text = text[:8000]
    res.emails = find_emails(text)
    res.phones = find_bd_phones(text)
    return res
