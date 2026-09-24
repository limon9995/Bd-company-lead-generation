"""Postgres-backed job queue (SELECT ... FOR UPDATE SKIP LOCKED). Works on SQLite for dev/tests."""
import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import is_postgres
from app.models import Job
from app.redact import redact

_sqlite_claim_lock = threading.Lock()
BACKOFF_SECONDS = [60, 300, 1800]
FINAL_TYPES = {"finalize_run"}


def now() -> datetime:
    return datetime.now(timezone.utc)


def enqueue(db: Session, type_: str, payload: dict, *, run_id: int | None = None, dedupe_key: str | None = None,
            run_after: datetime | None = None, max_attempts: int = 4) -> Job | None:
    if dedupe_key and db.scalar(select(Job.id).where(Job.dedupe_key == dedupe_key)):
        return None
    job = Job(type=type_, payload=payload, run_id=run_id, dedupe_key=dedupe_key, run_after=run_after or now(),
              max_attempts=max_attempts)
    db.add(job)
    try:
        db.flush()
    except IntegrityError:  # concurrent insert with the same dedupe key
        db.rollback()
        return None
    return job


def claim(db: Session) -> Job | None:
    q = select(Job).where(Job.status == "queued", Job.run_after <= now()).order_by(Job.run_after, Job.id).limit(1)
    if is_postgres():
        job = db.scalar(q.with_for_update(skip_locked=True))
        return _mark_running(db, job)
    with _sqlite_claim_lock:
        return _mark_running(db, db.scalar(q))


def _mark_running(db: Session, job: Job | None) -> Job | None:
    if job is None:
        db.rollback()
        return None
    job.status = "running"
    job.locked_at = now()
    job.attempts += 1
    db.commit()
    return job


def finish(db: Session, job: Job, status: str = "done", error: str = "") -> None:
    job.status = status
    job.last_error = redact(error)[:2000]
    job.locked_at = None
    db.commit()


def retry_or_fail(db: Session, job: Job, error: str) -> None:
    if job.attempts >= job.max_attempts:
        finish(db, job, "failed", error)
        return
    delay = BACKOFF_SECONDS[min(job.attempts - 1, len(BACKOFF_SECONDS) - 1)]
    job.status = "queued"
    job.run_after = now() + timedelta(seconds=delay)
    job.last_error = redact(error)[:2000]
    job.locked_at = None
    db.commit()


def postpone(db: Session, job: Job, until: datetime, reason: str) -> None:
    job.status = "queued"
    job.attempts = max(0, job.attempts - 1)  # postponement isn't a failed attempt
    job.run_after = until
    job.last_error = redact(reason)[:2000]
    job.locked_at = None
    db.commit()


def requeue_stale(db: Session, older_than_minutes: int = 20, all_running: bool = False) -> int:
    cond = Job.status == "running"
    if not all_running:
        cond = cond & (Job.locked_at < now() - timedelta(minutes=older_than_minutes))
    res = db.execute(update(Job).where(cond).values(status="queued", locked_at=None, run_after=now()))
    db.commit()
    return res.rowcount or 0


def pending_for_run(db: Session, run_id: int) -> int:
    return db.scalar(select(func.count(Job.id)).where(
        Job.run_id == run_id, Job.status.in_(["queued", "running"]), Job.type.not_in(FINAL_TYPES))) or 0
