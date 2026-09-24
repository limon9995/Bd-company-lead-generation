import httpx
import pytest
import respx
from sqlalchemy import select

from app.models import ApiUsage, Campaign
from app.pipeline import providers
from app.services import browser
from app.services.errors import SourceBlocked
from app.services.search import SearchResult

SERPER = "https://google.serper.dev/search"
BRAVE = "https://api.search.brave.com/res/v1/web/search"


def serper_ok(title="From Serper"):
    return httpx.Response(200, json={"organic": [{"title": title, "link": "https://a.com", "snippet": "s"}]})


def brave_ok():
    return httpx.Response(200, json={"web": {"results": [{"title": "From Brave", "url": "https://b.com", "description": "d"}]}})


def calls(db, provider):
    return sum(r.calls for r in db.scalars(select(ApiUsage).where(ApiUsage.provider == provider)))


@pytest.fixture
def no_browser(monkeypatch):
    seen = []

    def fake(db, engine, campaign, q, num):
        seen.append(engine)
        return [SearchResult(f"From {engine}", f"https://{engine}.com", "")]

    monkeypatch.setattr(providers, "_browser_query", fake)
    monkeypatch.setattr("app.services.notifier.notify", lambda *a, **k: [])
    return seen


def test_auto_uses_serper_first(db, configure, no_browser):
    configure(search_provider="auto", serper_api_key="sk", brave_api_key="bk", search_cache_days=0)
    with respx.mock:
        respx.post(SERPER).mock(return_value=serper_ok())
        assert providers.get_search(db)("acme ceo")[0].title == "From Serper"
    assert calls(db, "search") == 1 and no_browser == []


def test_out_of_credit_serper_moves_to_brave_and_is_skipped_for_a_day(db, configure, no_browser):
    configure(search_provider="auto", serper_api_key="sk", brave_api_key="bk", search_cache_days=0)
    with respx.mock:
        serper = respx.post(SERPER).mock(return_value=httpx.Response(400, json={"message": "Not enough credits"}))
        respx.get(BRAVE).mock(return_value=brave_ok())
        search = providers.get_search(db)
        assert search("acme ceo")[0].title == "From Brave"
        assert browser.blocked_until(db, "serper") is not None
        assert search("other ceo")[0].title == "From Brave"
        assert serper.call_count == 1  # not asked again while paused


def test_no_keys_goes_to_free_browser_search(db, configure, no_browser):
    configure(search_provider="auto", search_cache_days=0)
    assert providers.get_search(db)("acme ceo")[0].title == "From duckduckgo"
    assert no_browser == ["duckduckgo"]


def test_blocked_duckduckgo_is_paused_and_bing_is_used(db, configure, monkeypatch):
    configure(search_provider="auto", search_cache_days=0)
    monkeypatch.setattr("app.services.notifier.notify", lambda *a, **k: [])

    def fake(db, engine, campaign, q, num):
        if engine == "duckduckgo":
            raise SourceBlocked("duckduckgo", "bots use DuckDuckGo too")
        return [SearchResult("From bing", "https://bing.com", "")]

    monkeypatch.setattr(providers, "_browser_query", fake)
    assert providers.get_search(db)("acme ceo")[0].title == "From bing"
    assert browser.blocked_until(db, "duckduckgo") is not None


def test_nothing_available_raises_an_already_handled_block(db, configure):
    configure(search_provider="auto", search_cache_days=0)
    for engine in ("duckduckgo", "bing"):
        browser.set_blocked(db, engine, 6)
    db.commit()
    with pytest.raises(SourceBlocked) as err:
        providers.get_search(db)("acme ceo")
    assert err.value.source == "search" and err.value.already_paused


def test_cached_query_costs_nothing_the_second_time(db, configure):
    configure(search_provider="serper", serper_api_key="sk", search_cache_days=30)
    with respx.mock:
        route = respx.post(SERPER).mock(return_value=serper_ok())
        assert providers.get_search(db)("Acme  CEO")[0].url == "https://a.com"
        again = providers.get_search(db, Campaign(name="other", industry_slug="tech", cities=[]))("acme ceo")
        assert again[0].url == "https://a.com"
        assert route.call_count == 1
    assert calls(db, "search") == 1


def test_cache_off_and_expired_entries_call_the_api(db, configure):
    from datetime import datetime, timedelta, timezone

    from app.models import SearchCache

    configure(search_provider="serper", serper_api_key="sk", search_cache_days=30)
    with respx.mock:
        route = respx.post(SERPER).mock(return_value=serper_ok())
        search = providers.get_search(db)
        search("acme ceo")
        for row in db.scalars(select(SearchCache)):
            row.updated_at = datetime.now(timezone.utc) - timedelta(days=31)
        db.commit()
        search("acme ceo")
        assert route.call_count == 2
