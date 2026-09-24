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
    key = settings_store.get(db, f"{provider}_api_key")
    if not key:
        return None
    return lambda q, num=10: search_mod.search(provider, key, q, num)


def places_key(db: Session) -> str:
    return settings_store.get(db, "places_api_key")
