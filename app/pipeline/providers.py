"""Build API clients from admin-panel settings. Returns None when not configured."""
import logging
import re

from sqlalchemy.orm import Session

from app import settings_store
from app.services import search as search_mod
from app.services.errors import BudgetExceeded, ProviderError
from app.services.llm import GeminiProvider, LLMProvider
from app.services.usage import check_budget, limiter, record_call

log = logging.getLogger(__name__)


def get_llm(db: Session) -> LLMProvider | None:
    key = settings_store.get(db, "gemini_api_key")
    if not key:
        return None
    return GeminiProvider(key, settings_store.get(db, "gemini_model"))


def browser_style(db: Session, campaign=None) -> str:
    return (getattr(campaign, "browser_style", "") or settings_store.get(db, "browser_style")) or "type"


API_ENGINES = ("serper", "brave")
BROWSER_ENGINES = ("duckduckgo", "bing")
SEARCH_CHAIN = API_ENGINES + BROWSER_ENGINES  # provider "auto": cheapest reliable first, free browser last
OUT_OF_CREDIT_PAUSE_HOURS = 24
_OUT_OF_CREDIT = re.compile(r"\b(401|402|403|429)\b|credit|quota|limit exceeded|rate limit", re.I)

# Every callable returned here meters its own calls (budget cap + usage stats) and is safe to cache in front of.


def _metered_api(db: Session, name: str, key: str):
    def _search(q: str, num: int = 10):
        check_budget(db, "search")
        try:
            return search_mod.search(name, key, q, num)
        finally:
            record_call(db, "search")

    _search.provider = name
    return _search


def api_search(db: Session, provider: str | None = None):
    """Serper/Brave callable, or None when that provider has no key. provider=None: first one with a key."""
    for name in ([provider] if provider else list(API_ENGINES)):
        key = settings_store.get(db, f"{name}_api_key")
        if key:
            return _metered_api(db, name, key)
    return None


def _cached(db: Session, fn):
    """Answer a query from saved results when possible; save fresh results for next time."""
    from app.services import search_cache

    def _search(q: str, num: int = 10):
        days = settings_store.get(db, "search_cache_days")
        hit = search_cache.get(db, q, days)
        if hit is not None:
            return hit[:num]
        results = fn(q, num)
        if days > 0:
            search_cache.put(db, q, getattr(fn, "last_provider", "") or getattr(fn, "provider", ""), results)
        return results

    _search.provider = getattr(fn, "provider", "")
    return _search


def get_search(db: Session, campaign=None):
    provider = (getattr(campaign, "search_provider", "") or settings_store.get(db, "search_provider"))
    if provider == "none":
        return None
    if provider == "auto":
        fn = chain_search(db, campaign)
    elif provider in BROWSER_ENGINES:
        fn = browser_search(db, provider, campaign)
    else:
        fn = api_search(db, provider)
    return _cached(db, fn) if fn is not None else None


def places_key(db: Session) -> str:
    return settings_store.get(db, "places_api_key")


def _browser_query(db: Session, engine: str, campaign, q: str, num: int):
    """One paced, metered browser search; fresh browser on crashes. Raises SourceBlocked on a block page."""
    from app.services import browser, search_browser
    from app.services.errors import SourceBlocked

    typing = browser_style(db, campaign) == "type"
    browser.ensure_not_paused(db, engine)
    check_budget(db, "search_browser")
    limiter.wait(f"search_browser:{engine}", settings_store.get(db, "browser_search_min_gap_seconds"))
    last_error = None
    try:
        for _ in range(1 + settings_store.get(db, "browser_retries")):  # crash/timeout: fresh browser each time
            try:
                with browser.source_lock(engine), browser.session_for(db, engine) as session:
                    if typing:
                        return search_browser.search_by_typing(session, engine, q, num)
                    return search_browser.search(session, engine, q, num)
            except SourceBlocked:
                raise
            except Exception as exc:  # noqa: BLE001
                last_error = exc
        raise last_error
    finally:
        record_call(db, "search_browser")


def browser_search(db: Session, engine: str, campaign=None):
    """Search through a headless browser. Returns a callable like the API providers.
    If the engine blocks us and the campaign allows fallback, the same query goes to Serper/Brave instead."""
    from app.services.errors import SourceBlocked

    fallback = getattr(campaign, "on_block", "fallback") == "fallback"

    def _search(q: str, num: int = 10):
        api = api_search(db) if fallback else None
        try:
            return _browser_query(db, engine, campaign, q, num)
        except SourceBlocked as exc:
            # a search happens inside a bigger step, so no 30-minute wait here: API now, or pause
            if api is None:
                raise
            if not getattr(exc, "already_paused", False):
                from app.pipeline.stages import pause_source

                pause_source(db, exc, fallback_note="Searches use the Serper/Brave API until then.")
            return api(q, num)
        except Exception:  # crash/timeout after retries, or the daily cap: API if allowed
            if api is None:
                raise
            return api(q, num)

    _search.provider = engine
    return _search


def chain_search(db: Session, campaign=None):
    """Provider "auto": Serper → Brave → DuckDuckGo → Bing. A provider without a key, out of credit, over its
    daily cap, failing, or paused after a block is skipped; blocks are never bypassed."""
    from app.services import browser
    from app.services.errors import SourceBlocked

    def _search(q: str, num: int = 10):
        tried = []
        for name in SEARCH_CHAIN:
            if browser.blocked_until(db, name):
                tried.append(f"{name}: paused")
                continue
            try:
                if name in API_ENGINES:
                    key = settings_store.get(db, f"{name}_api_key")
                    if not key:
                        continue
                    results = _metered_api(db, name, key)(q, num)
                else:
                    results = _browser_query(db, name, campaign, q, num)
            except BudgetExceeded:
                tried.append(f"{name}: daily cap reached")
                continue
            except ProviderError as exc:
                if _OUT_OF_CREDIT.search(str(exc)):
                    # free credits used up / key refused: stop asking this provider for a day
                    browser.set_blocked(db, name, OUT_OF_CREDIT_PAUSE_HOURS)
                    db.commit()
                    log.warning("search provider %s skipped for %sh: %s", name, OUT_OF_CREDIT_PAUSE_HOURS, exc)
                tried.append(f"{name}: {exc}"[:200])
                continue
            except SourceBlocked as exc:
                if not getattr(exc, "already_paused", False):
                    from app.pipeline.stages import pause_source

                    pause_source(db, exc, fallback_note="Searches move to the next provider until then.")
                tried.append(f"{name}: blocked")
                continue
            except Exception as exc:  # noqa: BLE001 - crash/timeout: try the next provider
                tried.append(f"{name}: {exc}"[:200])
                continue
            _search.last_provider = name
            return results
        err = SourceBlocked("search", "no search provider available (" + "; ".join(tried) + ")")
        err.already_paused = True  # nothing new to pause - each provider was handled above
        raise err

    _search.provider = "auto"
    _search.last_provider = ""
    return _search
