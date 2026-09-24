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


def setup(monkeypatch, configure, links, read):
    configure(facebook_pages="false")
    monkeypatch.setattr(browser, "session_for", dummy_session)
    for name in ("collect_links", "search_by_typing"):
        monkeypatch.setattr(maps_browser, name, lambda s, q, n: links(q))
    monkeypatch.setattr(maps_browser, "read_place", read)
    monkeypatch.setattr(maps_browser, "click_result", read)
    monkeypatch.setattr(stages.crawler, "crawl", fake_crawl({}))
    monkeypatch.setattr(providers, "get_llm", lambda db: RuleLLM())
    monkeypatch.setattr(providers, "get_search", lambda db: None)
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
