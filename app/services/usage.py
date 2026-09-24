"""Per-provider daily call counting (budget guard) and simple in-process rate limiting."""
import threading
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import settings_store
from app.config import config
from app.models import ApiUsage
from app.services.errors import BudgetExceeded

CAP_KEYS = {"places": "places_daily_cap", "search": "search_daily_cap", "gemini": "gemini_daily_cap"}
COST_KEYS = {"places": "places_cost_per_call", "search": "search_cost_per_call", "gemini": "gemini_cost_per_call"}


def today_local() -> date:
    return datetime.now(ZoneInfo(config.timezone)).date()


def _row(db: Session, provider: str) -> ApiUsage:
    day = today_local()
    row = db.scalar(select(ApiUsage).where(ApiUsage.provider == provider, ApiUsage.day == day))
    if row is None:
        row = ApiUsage(provider=provider, day=day, calls=0, est_cost_usd=0.0)
        db.add(row)
        db.flush()
    return row


def check_budget(db: Session, provider: str) -> None:
    cap_key = CAP_KEYS.get(provider)
    if not cap_key:
        return
    cap = settings_store.get(db, cap_key)
    if cap and _row(db, provider).calls >= cap:
        raise BudgetExceeded(provider)


def record_call(db: Session, provider: str, n: int = 1) -> None:
    row = _row(db, provider)
    row.calls += n
    cost_key = COST_KEYS.get(provider)
    if cost_key:
        row.est_cost_usd = round(row.est_cost_usd + n * float(settings_store.get(db, cost_key) or 0), 6)
    # committed immediately so a later failure in the same job doesn't lose the count
    db.commit()


class MinInterval:
    """Thread-safe 'at most one call every N seconds' limiter, keyed (e.g. per domain)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._next: dict[str, float] = {}

    def wait(self, key: str, interval: float) -> None:
        if interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            at = max(now, self._next.get(key, 0.0))
            self._next[key] = at + interval
        delay = at - time.monotonic()
        if delay > 0:
            time.sleep(delay)


limiter = MinInterval()
