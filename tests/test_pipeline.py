import httpx
import pytest
import respx
from sqlalchemy import func, select

from app.models import ApiUsage, Campaign, Company, EmailMessage, EmailTemplate, Job, Lead, Person, Run
from app.pipeline import providers, stages
from app.pipeline.stages import start_run
from app.services.search import SearchResult
from app.worker.runner import drain
from tests.fakes import FakePlaces, RuleLLM, fake_crawl, fake_search, place


@pytest.fixture
def world(monkeypatch, configure):
    configure(places_api_key="places-key", gemini_api_key="gem-key", telegram_bot_token="123:abc",
              telegram_chat_id="999", serper_api_key="serper-key", facebook_pages="false")
    fp = FakePlaces({
        "hospital": [[place(1, "https://www.acme.com.bd"), place(2, "https://www.facebook.com/clinic2"),
                      place(3, "", status="CLOSED_PERMANENTLY")], [place(4, "https://beta.com.bd")]],
        "diagnostic center": [[place(1, "https://www.acme.com.bd"), place(5, "https://acme.com.bd/branch")]],
    })
    crawl = fake_crawl({
        "https://www.acme.com.bd": [("https://acme.com.bd/board", "Board: Rahim Uddin, Managing Director. Mail rahim@acme.com.bd")],
        "https://beta.com.bd": [("https://beta.com.bd/", "Welcome to Beta Diagnostics. info@beta.com.bd")],
    })
    llm = RuleLLM()
    search = fake_search({"Beta": [SearchResult("Selim Khan, CEO - Clinic 4 Beta", "https://bd.linkedin.com/in/selim",
                                                "Selim Khan, CEO at Clinic 4")]})
    monkeypatch.setattr(stages.places, "text_search", fp)
    monkeypatch.setattr(stages.crawler, "crawl", crawl)
    monkeypatch.setattr(providers, "get_llm", lambda db: llm)
    monkeypatch.setattr(providers, "get_search", lambda db: search)
    return {"places": fp, "crawl": crawl, "llm": llm, "search": search}


def make_campaign(db, **kw):
    tpl = db.scalar(select(EmailTemplate))
    c = Campaign(name=kw.pop("name", "Healthcare Dhaka"), industry_slug=kw.pop("industry_slug", "healthcare"),
                 cities=["Dhaka"], max_companies_per_run=kw.pop("max_companies_per_run", 50),
                 email_template_id=tpl.id, **kw)
    db.add(c)
    db.commit()
    return c


@respx.mock
def test_full_run_discovers_enriches_notifies_and_drafts(db, world):
    tg = respx.post(url__regex=r"https://api.telegram.org/bot123:abc/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {}}))
    camp = make_campaign(db, auto_email=True)
    run = start_run(db, camp)
    drain()
    db.expire_all()
    run = db.get(Run, run.id)
    assert run.status == "done", run.notes
    # 5 unique places: pid-1 twice (dedupe), pid-3 closed, pid-5 shares acme's domain → merged into pid-1's company
    companies = db.scalars(select(Company).order_by(Company.id)).all()
    assert [c.place_id for c in companies] == ["pid-1", "pid-2", "pid-4"]
    assert companies[1].website == "" and companies[1].socials["facebook"].startswith("https://www.facebook.com")
    assert run.counters["companies"] == 3 and run.counters["dm_found"] == 1

    acme_lead = db.scalar(select(Lead).join(Company).where(Company.place_id == "pid-1"))
    p = acme_lead.primary_person
    assert (p.full_name, p.title, p.email, p.email_status) == ("Rahim Uddin", "Managing Director", "rahim@acme.com.bd", "found")
    assert p.source_url == "https://acme.com.bd/board" and p.confidence >= 55

    # website had no DM for Beta → search fallback was used (but the snippet names another company → rejected)
    assert any("Clinic 4" in q for q in world["search"].calls)

    msgs = db.scalars(select(EmailMessage)).all()
    by_to = {m.to_email: m for m in msgs}
    assert set(by_to) == {"rahim@acme.com.bd", "info@beta.com.bd"}
    assert by_to["info@beta.com.bd"].status == "draft"  # no DM → not auto-approved
    assert "Congratulations" in by_to["rahim@acme.com.bd"].body and "/u/" in by_to["rahim@acme.com.bd"].body
    assert tg.called and "Run #" in tg.calls[0].request.content.decode()

    usage = {u.provider: u.calls for u in db.scalars(select(ApiUsage))}
    # 5 healthcare phrases: "hospital" has 2 pages, the other 4 phrases one page each
    assert usage["places"] == 6
    # 2 companies needed the search fallback (Beta: no DM on site; Clinic 2: Facebook-only) x 2 queries
    assert usage["search"] == 4 and len(world["search"].calls) == 4
    assert usage["gemini"] == world["llm"].calls


@respx.mock
def test_second_run_other_industry_does_not_duplicate_or_recrawl(db, world):
    respx.post(url__regex=r".*/sendMessage").mock(return_value=httpx.Response(200, json={"ok": True, "result": {}}))
    c1 = make_campaign(db)
    start_run(db, c1)
    drain()
    crawls_before = len(world["crawl"].calls)
    c2 = make_campaign(db, name="Same places, other campaign", industry_slug="healthcare")
    start_run(db, c2)
    drain()
    assert db.scalar(select(func.count(Company.id))) == 3
    assert db.scalar(select(func.count(Lead.id))) == 6  # each campaign has its own leads
    assert len(world["crawl"].calls) == crawls_before  # fresh companies are not re-crawled
    lead = db.scalar(select(Lead).join(Company).where(Lead.campaign_id == c2.id, Company.place_id == "pid-1"))
    assert lead.primary_person.full_name == "Rahim Uddin"
    # re-running the same campaign finds nothing new
    r3 = start_run(db, c1)
    drain()
    db.expire_all()
    assert db.get(Run, r3.id).counters.get("companies", 0) == 0


def test_max_companies_limit_is_respected(db, world, monkeypatch):
    monkeypatch.setattr("app.services.notifier.notify", lambda *a, **k: [])
    camp = make_campaign(db, max_companies_per_run=2)
    run = start_run(db, camp)
    drain()
    db.expire_all()
    assert db.get(Run, run.id).counters["companies"] == 2
    assert db.scalar(select(func.count(Lead.id))) == 2


def test_missing_places_key_fails_run_cleanly(db, world, configure, monkeypatch):
    sent = []
    monkeypatch.setattr("app.services.notifier.notify", lambda db, t, *a: sent.append(t) or [])
    configure(places_api_key="")
    run = start_run(db, make_campaign(db))
    drain()
    db.expire_all()
    run = db.get(Run, run.id)
    assert run.status == "failed" and any("Places" in n for n in run.notes)
    assert sent and "could not start" in sent[0]


def test_missing_gemini_key_still_collects_companies(db, world, monkeypatch):
    monkeypatch.setattr(providers, "get_llm", lambda db: None)
    monkeypatch.setattr("app.services.notifier.notify", lambda *a, **k: [])
    run = start_run(db, make_campaign(db))
    drain()
    db.expire_all()
    run = db.get(Run, run.id)
    assert run.status == "done" and run.counters["companies"] == 3
    assert db.scalar(select(func.count(Person.id))) == 0
    assert any("Gemini" in n for n in run.notes)


def test_budget_cap_postpones_jobs(db, world, configure, monkeypatch):
    monkeypatch.setattr("app.services.notifier.notify", lambda *a, **k: [])
    configure(places_daily_cap=1)
    run = start_run(db, make_campaign(db))
    drain()
    db.expire_all()
    postponed = db.scalars(select(Job).where(Job.run_id == run.id, Job.status == "queued")).all()
    assert postponed and "Daily cap" in postponed[0].last_error
    assert db.get(Run, run.id).status == "running"  # resumes tomorrow


def test_transient_error_is_retried_with_backoff(db, world, monkeypatch):
    from app.services.errors import ProviderError

    def boom(*a, **k):
        raise ProviderError("Places API 503")

    monkeypatch.setattr(stages.places, "text_search", boom)
    run = start_run(db, make_campaign(db))
    drain()
    job = db.scalar(select(Job).where(Job.run_id == run.id, Job.type == "discover"))
    assert job.status == "queued" and job.attempts == 1 and "503" in job.last_error
