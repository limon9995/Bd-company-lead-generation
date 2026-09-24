"""Browser-mode sources against local fixture pages in a real headless Chromium (no internet needed)."""
import functools
import http.server
import os
import threading
from pathlib import Path

import pytest
from sqlalchemy import select

from app.config import config
from app.services import browser, facebook_page, maps_browser, search_browser
from app.services.directory import extract_entries, find_next_url
from app.services.errors import SourceBlocked

FIX = Path(__file__).parent / "fixtures"
needs_browser = pytest.mark.skipif(not (config.chromium_executable and os.path.exists(config.chromium_executable)),
                                   reason="no Chromium available")


class Handler(http.server.SimpleHTTPRequestHandler):
    routes: dict = {}

    def do_GET(self):  # noqa: N802
        for prefix, body in self.routes.items():
            if self.path.startswith(prefix):
                data = body.encode() if isinstance(body, str) else body
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(data)
                return
        self.send_error(404)

    def log_message(self, *a):
        pass


@pytest.fixture
def site():
    routes: dict = {}
    handler = type("H", (Handler,), {"routes": routes})
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield routes, f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def fast_session(source):
    return browser.BrowserSession(source, delay=(0, 0), timeout=20)


# ------------------------------------------------------------------ Google Maps
def test_place_key_prefers_real_place_id():
    assert maps_browser.place_key_from_url("/maps/place/A/data=!1s0x1:0x2!19sChIJabc-_1") == "ChIJabc-_1"
    assert maps_browser.place_key_from_url("/maps/place/A/data=!1s0x1a:0x2b!8m2") == "maps:0x1a:0x2b"


def test_parse_place_page_from_fixture():
    p = maps_browser.parse_place_page((FIX / "maps/acme.html").read_text(), "https://g/maps/place/A/data=!19sChIJx")
    assert (p.name, p.category, p.phone, p.website) == ("Acme Hospital", "Hospital", "01713141516", "https://www.acmehospital.com.bd/")
    assert p.address.startswith("18/F Bir Uttam") and p.rating == 4.3 and p.reviews_count == 1234 and p.place_id == "ChIJx"
    closed = maps_browser.parse_place_page((FIX / "maps/closed.html").read_text(), "https://g/maps/place/C")
    assert closed.business_status == "CLOSED_PERMANENTLY"


@needs_browser
def test_maps_collect_and_read_in_real_browser(site):
    routes, base = site
    routes.update({"/maps/search/": (FIX / "maps/search.html").read_text(),
                   "/maps/place/Acme": (FIX / "maps/acme.html").read_text(),
                   "/maps/place/Beta": (FIX / "maps/beta.html").read_text(),
                   "/maps/place/Closed": (FIX / "maps/closed.html").read_text()})
    with fast_session("google") as s:
        links = maps_browser.collect_links(s, "hospital in Dhaka", 10, base_url=base + "/maps/search/{q}")
        assert len(links) == 3 and all(link.startswith(base) for link in links)  # duplicate removed
        acme = maps_browser.read_place(s, links[0])
    assert acme.name == "Acme Hospital" and acme.place_id == "ChIJgWsCh7C4VTcRwgRZ3btjpY8"


@needs_browser
def test_maps_unusual_traffic_page_raises_blocked(site):
    routes, base = site
    routes["/maps/search/"] = (FIX / "maps/sorry.html").read_text()
    with fast_session("google") as s, pytest.raises(SourceBlocked):
        maps_browser.collect_links(s, "x", 5, base_url=base + "/maps/search/{q}")


def test_block_detection_ignores_embedded_recaptcha_on_long_pages():
    long_page = "Contact us. " * 400 + " verify you are human (form widget)"
    assert browser.detect_block("dir:x", "https://a.com/contact", long_page) is None
    assert browser.detect_block("dir:x", "https://a.com", "Verify you are human") is not None
    assert browser.detect_block("google", "https://www.google.com/sorry/index?q=1", "") is not None
    assert browser.detect_block("facebook", "https://www.facebook.com/login/?next=x", "") is not None


# ------------------------------------------------------------------ browser search
def test_parse_duckduckgo_and_bing():
    ddg = """<div class="result results_links"><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Facme.com.bd%2Fboard&rut=x">Board - Acme</a>
    <a class="result__snippet">Rahim Uddin, Managing Director of Acme</a></div>
    <div class="result result--ad"><a class="result__a" href="https://ad.example">Ad</a></div>"""
    [r] = search_browser.parse_duckduckgo(ddg)
    assert r.url == "https://acme.com.bd/board" and "Rahim" in r.snippet
    bing = """<ol><li class="b_algo"><h2><a href="https://bd.linkedin.com/in/rahim">Rahim Uddin - MD - Acme</a></h2>
    <div class="b_caption"><p>Managing Director at Acme Hospital</p></div></li></ol>"""
    [b] = search_browser.parse_bing(bing)
    assert b.url.startswith("https://bd.linkedin.com") and b.snippet.startswith("Managing Director")


@needs_browser
def test_browser_search_blocked_page(site):
    routes, base = site
    routes["/html/"] = "<html><body>Unfortunately, bots use DuckDuckGo too. Please complete the following challenge</body></html>"
    with fast_session("duckduckgo") as s, pytest.raises(SourceBlocked):
        search_browser.search(s, "duckduckgo", "acme", base_url=base + "/html/?q={q}")


# ------------------------------------------------------------------ directory
class DirLLM:
    def generate_json(self, prompt, system=""):
        import re

        names = re.findall(r"^Company: (.+)$", prompt, re.M)
        return [{"name": n, "phone": "01811000000" if n == "Delta Pharma" else "", "city": "Dhaka"} for n in names] + \
            [{"name": "Invented Ltd"}]


PAGE1 = """<html><body><h1>Members</h1><ul><li>Company: Delta Pharma</li><li>Company: Epsilon Labs</li></ul>
<a href="/members?page=2">Next ›</a></body></html>"""
PAGE2 = "<html><body><ul><li>Company: Zeta Diagnostics</li></ul><a href='/members?page=1'>Previous</a></body></html>"


def test_directory_extraction_guard_and_next_link():
    entries = extract_entries(DirLLM(), "https://dir.example/members", PAGE1)
    assert [e.name for e in entries] == ["Delta Pharma", "Epsilon Labs"]  # "Invented Ltd" is not on the page
    assert find_next_url(PAGE1, "https://dir.example/members") == "https://dir.example/members?page=2"
    assert find_next_url(PAGE2, "https://dir.example/members?page=2") is None


@needs_browser
def test_directory_campaign_end_to_end(site, db, monkeypatch):
    from app.models import Campaign, Company, Run
    from app.pipeline import providers, stages
    from app.pipeline.stages import start_run
    from app.worker.runner import drain

    routes, base = site
    routes.update({"/members?page=2": PAGE2, "/members": PAGE1})
    monkeypatch.setattr(providers, "get_llm", lambda db: DirLLM())
    monkeypatch.setattr(stages.crawler, "crawl", lambda *a, **k: stages.crawler.CrawlResult(error="skip"))
    monkeypatch.setattr("app.services.notifier.notify", lambda *a, **k: [])
    monkeypatch.setattr(browser, "session_for", lambda db, src: fast_session(src))
    camp = Campaign(name="Pharma members", industry_slug="healthcare", cities=["Dhaka"], discovery_source="directory",
                    directory_urls=[base + "/members"], directory_max_pages=5)
    db.add(camp)
    db.commit()
    run = start_run(db, camp)
    drain()
    db.expire_all()
    run = db.get(Run, run.id)
    assert run.status == "done", run.notes
    names = sorted(c.name for c in db.scalars(select(Company)))
    assert names == ["Delta Pharma", "Epsilon Labs", "Zeta Diagnostics"]
    assert run.counters["directory_pages"] == 2
    assert db.scalar(select(Company).where(Company.name == "Delta Pharma")).source == "directory"


# ------------------------------------------------------------------ Facebook
@needs_browser
def test_facebook_login_wall_and_public_text(site, monkeypatch):
    routes, base = site
    routes["/wall"] = "<html><body>You must log in to continue.</body></html>"
    routes["/openpage"] = "<html><body><h1>Beta Clinic</h1><p>Call 01912-345678 · beta.clinic@gmail.com</p></body></html>"
    monkeypatch.setattr(facebook_page, "canonical", lambda u: base + urlpath(u))
    with fast_session("facebook") as s:
        assert facebook_page.read_page(s, "https://www.facebook.com/wall").login_wall
        ok = facebook_page.read_page(s, "https://www.facebook.com/openpage")
    assert ok.phones == ["+8801912345678"] and ok.emails == ["beta.clinic@gmail.com"]


def urlpath(u):
    from urllib.parse import urlparse

    return urlparse(u).path
