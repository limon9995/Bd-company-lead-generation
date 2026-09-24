from app.services.crawler import crawl, rank_candidate_links

HOME = """<html><head><title>Acme Hospital</title></head><body>
<nav><a href="/about-us">About Us</a> <a href="/board-of-directors">Board of Directors</a>
<a href="/contact">Contact</a> <a href="/gallery">Gallery</a> <a href="https://facebook.com/acmehospital">FB</a>
<a href="mailto:info@acme.com.bd">Email</a> <a href="https://other.com/team">external</a> <a href="/brochure.pdf">PDF</a></nav>
<p>Welcome to Acme Hospital, the best care in Dhaka. """ + ("Lorem ipsum dolor sit amet. " * 20) + """</p>
<script>var x = "hidden@script.com";</script></body></html>"""
BOARD = "<html><body><h2>Board</h2><p>Dr. Rahim Uddin, Managing Director</p><p>Phone: 01711-000111</p></body></html>"
CONTACT = "<html><body>Hotline: 10666, Tel 02-9123456, contact [at] acme [dot] com.bd</body></html>"
ABOUT = "<html><body>About Acme</body></html>"


class FakeHttp:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def fetch(self, url):
        self.calls.append(url)
        if url not in self.pages:
            raise ValueError("404")
        return url, self.pages[url]

    def close(self):
        pass


def test_rank_candidate_links_prefers_leadership_pages_and_same_host():
    links = [("https://acme.com.bd/gallery", "Gallery"), ("https://acme.com.bd/board-of-directors", "Board"),
             ("https://acme.com.bd/contact", "Contact"), ("https://other.com/team", "Team"),
             ("https://acme.com.bd/file.pdf", "Leadership PDF")]
    ranked = rank_candidate_links(links, "acme.com.bd")
    assert ranked[0] == "https://acme.com.bd/board-of-directors"
    assert "https://other.com/team" not in ranked and "https://acme.com.bd/file.pdf" not in ranked
    assert "https://acme.com.bd/gallery" not in ranked


def test_crawl_collects_pages_emails_phones_socials():
    http = FakeHttp({"https://acme.com.bd": HOME, "https://acme.com.bd/board-of-directors": BOARD,
                     "https://acme.com.bd/contact": CONTACT, "https://acme.com.bd/about-us": ABOUT})
    res = crawl("https://acme.com.bd", delay=0, fetchers={"http": http}, respect_robots=False)
    urls = [p.url for p in res.pages]
    assert urls[0] == "https://acme.com.bd" and "https://acme.com.bd/board-of-directors" in urls
    assert "https://acme.com.bd/gallery" not in http.calls
    assert set(res.emails) == {"info@acme.com.bd", "contact@acme.com.bd"}
    assert "hidden@script.com" not in res.emails  # script content removed
    assert "+8801711000111" in res.phones and "+88029123456" in res.phones
    assert res.socials["facebook"] == "https://facebook.com/acmehospital"
    assert "Rahim Uddin" in res.text and not res.used_browser


def test_crawl_falls_back_to_browser_for_js_sites():
    http = FakeHttp({"https://spa.com": "<html><body><div id=root></div></body></html>"})
    browser = FakeHttp({"https://spa.com": "<html><body>" + "Rendered content. " * 40 + "</body></html>"})
    res = crawl("https://spa.com", delay=0, fetchers={"http": http, "browser": lambda: browser}, respect_robots=False)
    assert res.used_browser and "Rendered content" in res.pages[0].text


def test_crawl_reports_unreachable_site():
    res = crawl("https://down.example", delay=0, use_browser=False, fetchers={"http": FakeHttp({})}, respect_robots=False)
    assert res.error and not res.pages


def test_real_http_crawl_respects_robots(tmp_path):
    """Exercises the real httpx fetcher + robots.txt handling against a local web server."""
    import functools
    import http.server
    import threading

    (tmp_path / "index.html").write_text(
        '<html><body><a href="/management.html">Management</a> <a href="/private/board.html">Board</a>'
        + "<p>" + "Local test company. " * 30 + "</p></body></html>")
    (tmp_path / "management.html").write_text("<html><body>Selina Akter, CEO. ceo@local.test</body></html>")
    (tmp_path / "private").mkdir()
    (tmp_path / "private" / "board.html").write_text("<html><body>Secret Person, Chairman</body></html>")
    (tmp_path / "robots.txt").write_text("User-agent: *\nDisallow: /private/\n")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(tmp_path))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        res = crawl(f"http://127.0.0.1:{srv.server_port}/", delay=0, use_browser=False)
    finally:
        srv.shutdown()
    text = res.text
    assert "Selina Akter, CEO" in text and "Secret Person" not in text
    assert res.emails == ["ceo@local.test"]
