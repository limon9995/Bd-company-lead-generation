import contextlib

from sqlalchemy import select

from app.models import Campaign, Company, Job, Run
from app.pipeline import providers, stages
from app.pipeline.stages import start_run
from app.services import browser, maps_browser
from app.services.errors import SourceBlocked
from app.worker.runner import drain
from tests.fakes import RuleLLM, fake_crawl, place


@contextlib.contextmanager
def dummy_session(db, source):
    yield object()


def setup(monkeypatch, configure, links, read, **settings):
    configure(facebook_pages="false", block_retry_minutes=settings.get("block_retry_minutes", 0))
    monkeypatch.setattr(browser, "session_for", dummy_session)
    for name in ("collect_links", "search_by_typing"):
        monkeypatch.setattr(maps_browser, name, lambda s, q, n: links(q))
    monkeypatch.setattr(maps_browser, "read_place", read)
    monkeypatch.setattr(maps_browser, "click_result", read)
    monkeypatch.setattr(stages.crawler, "crawl", fake_crawl({}))
    monkeypatch.setattr(providers, "get_llm", lambda db: RuleLLM())
    monkeypatch.setattr(providers, "get_search", lambda db, *a: None)
    sent = []
    monkeypatch.setattr("app.services.notifier.notify", lambda db, t, *a: sent.append(t) or [])
    return sent


def test_maps_browser_campaign_needs_no_places_key(db, monkeypatch, configure):
    reads = []

    def read(session, url):
        reads.append(url)
        i = int(url.rsplit("-", 1)[1])
        p = place(i, f"https://www.c{i}.com.bd")
        p.place_id = maps_browser.place_key_from_url(url)
        return p

    setup(monkeypatch, configure,
          lambda q: [f"https://www.google.com/maps/place/C/data=!1s0x1:0x{i}!-{i}" for i in (1, 2)] if q.startswith("hospital") else [],
          read)
    camp = Campaign(name="Maps", industry_slug="healthcare", cities=["Dhaka"], discovery_source="maps_browser")
    db.add(camp)
    db.commit()
    run = start_run(db, camp)
    drain()
    db.expire_all()
    run = db.get(Run, run.id)
    assert run.status == "done", run.notes
    assert run.counters["companies"] == 2 and len(reads) == 2
    assert {c.source for c in db.scalars(select(Company))} == {"maps_browser"}
    # second campaign sees the same places: known + fresh → page not opened again
    camp2 = Campaign(name="Maps 2", industry_slug="healthcare", cities=["Dhaka"], discovery_source="maps_browser")
    db.add(camp2)
    db.commit()
    start_run(db, camp2)
    drain()
    assert len(reads) == 2


def test_block_pauses_source_and_postpones_job(db, monkeypatch, configure):
    def blocked(q):
        raise SourceBlocked("google", "unusual traffic")

    sent = setup(monkeypatch, configure, blocked, lambda s, u: None)
    camp = Campaign(name="Maps", industry_slug="healthcare", cities=["Dhaka"], discovery_source="maps_browser")
    db.add(camp)
    db.commit()
    run = start_run(db, camp)
    drain()
    job = db.scalar(select(Job).where(Job.run_id == run.id, Job.type == "discover_maps"))
    assert job.status == "queued" and "blocked" in job.last_error
    assert browser.blocked_until(db, "google") is not None
    assert len([t for t in sent if "Paused until" in t]) == 1
    # a second campaign during the pause is postponed without a second alert
    camp2 = Campaign(name="Maps 2", industry_slug="healthcare", cities=["Dhaka"], discovery_source="maps_browser")
    db.add(camp2)
    db.commit()
    start_run(db, camp2)
    drain()
    assert len([t for t in sent if "Paused until" in t]) == 1


def test_directory_campaign_without_urls_fails_cleanly(db, monkeypatch, configure):
    setup(monkeypatch, configure, lambda q: [], lambda s, u: None)
    camp = Campaign(name="Dir", industry_slug="healthcare", cities=["Dhaka"], discovery_source="directory", directory_urls=[])
    db.add(camp)
    db.commit()
    run = start_run(db, camp)
    drain()
    db.expire_all()
    run = db.get(Run, run.id)
    assert run.status == "failed" and any("directory URLs" in n for n in run.notes)


def test_maps_block_falls_back_to_places_api(db, monkeypatch, configure):
    from tests.fakes import FakePlaces

    def blocked(q):
        raise SourceBlocked("google", "unusual traffic")

    sent = setup(monkeypatch, configure, blocked, lambda s, u: None)
    configure(places_api_key="k")
    monkeypatch.setattr(stages.places, "text_search", FakePlaces({"hospital": [[place(1, "https://a.com.bd"), place(2)]]}))
    camp = Campaign(name="Maps", industry_slug="healthcare", cities=["Dhaka"], discovery_source="maps_browser",
                    on_block="fallback")
    db.add(camp)
    db.commit()
    run = start_run(db, camp)
    drain()
    db.expire_all()
    run = db.get(Run, run.id)
    assert run.status == "done" and run.counters["companies"] == 2
    assert any("switched to the Places API" in n for n in run.notes)
    assert browser.blocked_until(db, "google") is not None
    assert any("continues with the Google Places API" in t for t in sent)


def test_on_block_pause_does_not_fall_back(db, monkeypatch, configure):
    def blocked(q):
        raise SourceBlocked("google", "unusual traffic")

    setup(monkeypatch, configure, blocked, lambda s, u: None)
    configure(places_api_key="k")
    camp = Campaign(name="Maps", industry_slug="healthcare", cities=["Dhaka"], discovery_source="maps_browser", on_block="pause")
    db.add(camp)
    db.commit()
    run = start_run(db, camp)
    drain()
    assert db.scalar(select(Job.status).where(Job.run_id == run.id, Job.type == "discover_maps")) == "queued"
    assert db.scalar(select(Job.id).where(Job.run_id == run.id, Job.type == "discover")) is None


def test_browser_search_block_falls_back_to_serper(db, monkeypatch, configure):
    import httpx
    import respx

    configure(search_provider="duckduckgo", serper_api_key="sk")

    @contextlib.contextmanager
    def blocked_session(db, source):
        raise SourceBlocked(source, "bots use DuckDuckGo too")
        yield

    monkeypatch.setattr(browser, "session_for", blocked_session)
    monkeypatch.setattr("app.services.notifier.notify", lambda *a, **k: [])
    camp = Campaign(name="c", industry_slug="healthcare", cities=["Dhaka"], on_block="fallback")
    search = providers.get_search(db, camp)
    with respx.mock:
        respx.post("https://google.serper.dev/search").mock(return_value=httpx.Response(200, json={
            "organic": [{"title": "T", "link": "https://x.com", "snippet": "S"}]}))
        assert search("acme md")[0].url == "https://x.com"
    assert browser.blocked_until(db, "duckduckgo") is not None
    camp.on_block = "pause"
    with __import__("pytest").raises(SourceBlocked):
        providers.get_search(db, camp)("acme chairman")  # new query: "acme md" is now answered from the cache



def _run_due_now(db, run_id):
    """Pretend the delay passed: make the run's queued jobs due and lift a cool-down pause."""
    from app.pipeline.queue import now

    for j in db.scalars(select(Job).where(Job.run_id == run_id, Job.status == "queued")):
        j.run_after = now()
    browser.clear_blocked(db, "google")
    db.commit()
    drain()


def test_block_waits_then_retries_once_then_uses_api(db, monkeypatch, configure):
    from tests.fakes import FakePlaces

    calls = []

    def blocked(q):
        calls.append(q)
        raise SourceBlocked("google", "unusual traffic")

    sent = setup(monkeypatch, configure, blocked, lambda s, u: None, block_retry_minutes=30)
    configure(places_api_key="k")
    monkeypatch.setattr(stages.places, "text_search", FakePlaces({"hospital": [[place(1, "https://a.com.bd")]]}))
    camp = Campaign(name="Maps", industry_slug="healthcare", cities=["Dhaka"], discovery_source="maps_browser")
    db.add(camp)
    db.commit()
    run = start_run(db, camp)
    drain()
    job = db.scalar(select(Job).where(Job.run_id == run.id, Job.type == "discover_maps"))
    assert job.status == "queued" and "one retry in 30 min" in job.last_error and len(calls) == 1
    assert not [t for t in sent if "Paused until" in t]  # no alarm for the first block
    _run_due_now(db, run.id)
    assert len(calls) == 2  # tried once more the normal way
    db.expire_all()
    run = db.get(Run, run.id)
    assert run.status == "done" and run.counters["companies"] == 1
    assert any("switched to the Places API" in n for n in run.notes)


def test_browser_crash_retries_with_fresh_browser_then_uses_api(db, monkeypatch, configure):
    from tests.fakes import FakePlaces

    opened = []

    @contextlib.contextmanager
    def counting_session(db, source):
        opened.append(source)
        yield object()

    def crash(q):
        raise RuntimeError("Target page, context or browser has been closed")

    setup(monkeypatch, configure, crash, lambda s, u: None)
    monkeypatch.setattr(browser, "session_for", counting_session)
    configure(places_api_key="k", browser_retries=2)
    monkeypatch.setattr(stages.places, "text_search", FakePlaces({"hospital": [[place(1, "https://a.com.bd")]]}))
    camp = Campaign(name="Maps", industry_slug="healthcare", cities=["Dhaka"], discovery_source="maps_browser")
    db.add(camp)
    db.commit()
    run = start_run(db, camp)
    drain()
    for _ in range(3):
        _run_due_now(db, run.id)
    assert len(opened) == 3  # first try + 2 retries, each with a new browser
    db.expire_all()
    run = db.get(Run, run.id)
    assert run.status == "done" and run.counters["companies"] == 1
    assert any("kept failing" in n for n in run.notes)


def test_browser_search_crash_retries_then_serper(db, monkeypatch, configure):
    import httpx
    import respx

    configure(search_provider="bing", serper_api_key="sk", browser_retries=2)
    opened = []

    @contextlib.contextmanager
    def crashing(db, source):
        opened.append(source)
        raise RuntimeError("browser crashed")
        yield

    monkeypatch.setattr(browser, "session_for", crashing)
    camp = Campaign(name="c", industry_slug="healthcare", cities=["Dhaka"], on_block="fallback")
    with respx.mock:
        respx.post("https://google.serper.dev/search").mock(return_value=httpx.Response(200, json={
            "organic": [{"title": "T", "link": "https://x.com", "snippet": "S"}]}))
        assert providers.get_search(db, camp)("q")[0].url == "https://x.com"
    assert len(opened) == 3 and browser.blocked_until(db, "bing") is None  # a crash is not a block
