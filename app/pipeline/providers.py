"""Build API clients from admin-panel settings. Returns None when not configured."""
from sqlalchemy.orm import Session

from app import settings_store
from app.services import search as search_mod
from app.services.llm import GeminiProvider, LLMProvider


def get_llm(db: Session) -> LLMProvider | None:
    key = settings_store.get(db, "gemini_api_key")
    if not key:
        return None
    return GeminiProvider(key, settings_store.get(db, "gemini_model"))


def browser_style(db: Session, campaign=None) -> str:
    return (getattr(campaign, "browser_style", "") or settings_store.get(db, "browser_style")) or "type"


def api_search(db: Session, provider: str | None = None):
    """Serper/Brave callable, or None when that provider has no key. provider=None: first one with a key."""
    for name in ([provider] if provider else ["serper", "brave"]):
        key = settings_store.get(db, f"{name}_api_key")
        if key:
            return (lambda n, k: (lambda q, num=10: search_mod.search(n, k, q, num)))(name, key)
    return None


def get_search(db: Session, campaign=None):
    provider = (getattr(campaign, "search_provider", "") or settings_store.get(db, "search_provider"))
    if provider == "none":
        return None
    if provider in ("duckduckgo", "bing"):
        return browser_search(db, provider, campaign)
    return api_search(db, provider)


def places_key(db: Session) -> str:
    return settings_store.get(db, "places_api_key")


def browser_search(db: Session, engine: str, campaign=None):
    """Search through a headless browser. Returns a callable like the API providers, tagged for usage stats.
    If the engine blocks us and the campaign allows fallback, the same query goes to Serper/Brave instead."""
    from app.services import browser, search_browser
    from app.services.errors import SourceBlocked

    fallback = getattr(campaign, "on_block", "fallback") == "fallback"
    typing = browser_style(db, campaign) == "type"

    def _search(q: str, num: int = 10):
        try:
            browser.ensure_not_paused(db, engine)
            with browser.source_lock(engine), browser.session_for(db, engine) as session:
                if typing:
                    return search_browser.search_by_typing(session, engine, q, num)
                return search_browser.search(session, engine, q, num)
        except SourceBlocked as exc:
            api = api_search(db) if fallback else None
            if api is None:
                raise
            from app.pipeline.stages import pause_source

            pause_source(db, exc, fallback_note="Searches use the Serper/Brave API until then.")
            return api(q, num)

    _search.usage_key = "search_browser"
    return _search
