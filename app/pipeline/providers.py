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


def get_search(db: Session):
    provider = settings_store.get(db, "search_provider")
    if provider == "none":
        return None
    if provider in ("duckduckgo", "bing"):
        return browser_search(db, provider)
    key = settings_store.get(db, f"{provider}_api_key")
    if not key:
        return None
    return lambda q, num=10: search_mod.search(provider, key, q, num)


def places_key(db: Session) -> str:
    return settings_store.get(db, "places_api_key")


def browser_search(db: Session, engine: str):
    """Search through a headless browser. Returns a callable like the API providers, tagged for usage stats."""
    from app.services import browser, search_browser

    def _search(q: str, num: int = 10):
        browser.ensure_not_paused(db, engine)
        typing = settings_store.get(db, "browser_style") == "type"
        with browser.source_lock(engine), browser.session_for(db, engine) as session:
            if typing:
                return search_browser.search_by_typing(session, engine, q, num)
            return search_browser.search(session, engine, q, num)

    _search.usage_key = "search_browser"
    return _search
