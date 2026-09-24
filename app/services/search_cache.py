"""Saved search results: a query asked again within N days is answered from the database for free."""
import hashlib
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models import SearchCache
from app.services.normalize import squash_ws
from app.services.search import SearchResult


def _key(query: str) -> str:
    return hashlib.sha1(squash_ws(query).lower().encode("utf-8")).hexdigest()


def get(db: Session, query: str, max_age_days: int) -> list[SearchResult] | None:
    if max_age_days <= 0:
        return None
    row = db.get(SearchCache, _key(query))
    if row is None:
        return None
    saved = row.updated_at if row.updated_at.tzinfo else row.updated_at.replace(tzinfo=timezone.utc)
    if saved < datetime.now(timezone.utc) - timedelta(days=max_age_days):
        return None
    return [SearchResult(r.get("title", ""), r.get("url", ""), r.get("snippet", "")) for r in row.results or []]


def put(db: Session, query: str, provider: str, results: list[SearchResult]) -> None:
    row = db.get(SearchCache, _key(query)) or SearchCache(query_hash=_key(query))
    row.query, row.provider = squash_ws(query)[:2000], provider
    row.results = [{"title": r.title, "url": r.url, "snippet": r.snippet} for r in results]
    row.updated_at = datetime.now(timezone.utc)
    db.add(row)
    db.flush()
