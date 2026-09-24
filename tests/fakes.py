"""Fake external services used by pipeline/web tests (no network)."""
from app.services.crawler import CrawlResult, PageData
from app.services.places import Place
from app.services.search import SearchResult


def place(i: int, website: str = "", name: str | None = None, status: str = "OPERATIONAL") -> Place:
    return Place(place_id=f"pid-{i}", name=name or f"Clinic {i}", address=f"Road {i}, Dhaka", phone=f"01711-00{i:04d}",
                 website=website, rating=4.2, reviews_count=10 * i, maps_url=f"https://maps.google.com/?cid={i}",
                 category="Hospital", business_status=status)


class FakePlaces:
    def __init__(self, pages: dict[str, list[list[Place]]]):
        self.pages, self.calls = pages, []

    def __call__(self, key, query, region="bd", language="en", page_token=None, client=None):
        self.calls.append((query, page_token))
        pages = self.pages.get(query.split(" in ")[0], [[]])
        i = int(page_token or 0)
        nxt = str(i + 1) if i + 1 < len(pages) else None
        return pages[i], nxt


def fake_crawl(site_pages: dict[str, list[tuple[str, str]]]):
    calls = []

    def _crawl(website, **kw):
        calls.append(website)
        pages = site_pages.get(website, [])
        res = CrawlResult(pages=[PageData(u, "", t) for u, t in pages])
        for _, t in pages:
            for w in t.split():
                if "@" in w:
                    res.emails.append(w.strip(".,"))
        if not pages:
            res.error = "could not load site"
        return res

    _crawl.calls = calls
    return _crawl


class RuleLLM:
    """Pretends to be Gemini: finds 'Name, Title' lines in the prompt sources."""

    def __init__(self):
        self.calls = 0

    def generate_json(self, prompt, system=""):
        import re

        self.calls += 1
        out = []
        blocks = re.split(r"### SOURCE (\d+) \(\w+\) URL: \S+\n", prompt)[1:]
        for idx, text in zip(blocks[::2], blocks[1::2]):
            for m in re.finditer(r"([A-Z][a-z]+ [A-Z][a-z]+), (Managing Director|CEO|Chairman|Principal)", text):
                out.append({"name": m.group(1), "title": m.group(2), "source_index": int(idx), "evidence_quote": m.group(0)})
        return out

    def generate_text(self, prompt, system="", max_tokens=400):
        self.calls += 1
        return "Congratulations on serving patients in Dhaka."


def fake_search(results: dict[str, list[SearchResult]]):
    calls = []

    def _search(q, num=10):
        calls.append(q)
        for key, res in results.items():
            if key in q:
                return res
        return []

    _search.calls = calls
    return _search
