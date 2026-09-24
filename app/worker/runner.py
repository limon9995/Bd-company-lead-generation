"""Job runner: N threads pull jobs from the queue and dispatch them to pipeline stage handlers."""
import logging
import threading
import time
import traceback
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from app.config import config
from app.db import SessionLocal
from app.models import ApiUsage, Job, Run
from app.pipeline import queue
from app.pipeline.stages import HANDLERS, add_note
from app.services.errors import BudgetExceeded, SkipStage
from app.services.usage import today_local

log = logging.getLogger(__name__)


def next_local_midnight() -> datetime:
    tz = ZoneInfo(config.timezone)
    tomorrow = datetime.now(tz).date() + timedelta(days=1)
    return datetime.combine(tomorrow, dtime(0, 5), tz)


def _alert_budget_once(db, provider: str) -> None:
    from sqlalchemy import select

    from app.services.notifier import notify

    row = db.scalar(select(ApiUsage).where(ApiUsage.provider == provider, ApiUsage.day == today_local()))
    if row is not None and row.last_error != "alerted":
        row.last_error = "alerted"
        db.commit()
        notify(db, f"⚠️ Daily cap reached for <b>{provider}</b>. Remaining jobs resume tomorrow. "
                   "Raise the cap in Settings → Crawling & budgets if needed.")


def process(db, job: Job) -> None:
    handler = HANDLERS.get(job.type)
    run = db.get(Run, job.run_id) if job.run_id else None
    try:
        if handler is None:
            raise SkipStage(f"unknown job type {job.type}")
        handler(db, job)
        db.commit()
        queue.finish(db, job, "done")
    except SkipStage as exc:
        # keep what the handler recorded on the run (notes/status) but nothing else half-done
        notes, status = (list(run.notes or []), run.status) if run else ([], None)
        db.rollback()
        if run is not None:
            run = db.get(Run, job.run_id)
            run.notes, run.status = notes, status
            if job.type != "run_campaign":  # run_campaign already wrote a clearer note
                add_note(run, str(exc))
        queue.finish(db, db.get(Job, job.id), "skipped", str(exc))
    except BudgetExceeded as exc:
        db.rollback()
        queue.postpone(db, db.get(Job, job.id), next_local_midnight(), str(exc))
        _alert_budget_once(db, exc.provider)
    except Exception as exc:  # noqa: BLE001 - any failure is recorded and retried
        db.rollback()
        log.warning("job %s (%s) failed: %s", job.id, job.type, exc)
        queue.retry_or_fail(db, db.get(Job, job.id), f"{exc}\n{traceback.format_exc(limit=3)}")
    maybe_finalize(db, job)


def maybe_finalize(db, job: Job) -> None:
    if not job.run_id or job.type in queue.FINAL_TYPES:
        return
    if queue.pending_for_run(db, job.run_id) == 0:
        queue.enqueue(db, "finalize_run", {"run_id": job.run_id}, run_id=job.run_id, dedupe_key=f"finalize:{job.run_id}")
        db.commit()


def drain(max_jobs: int = 10_000) -> int:
    """Process queued jobs synchronously until none are due (used by tests and the CLI)."""
    n = 0
    while n < max_jobs:
        db = SessionLocal()
        try:
            job = queue.claim(db)
            if job is None:
                return n
            process(db, job)
            n += 1
        finally:
            db.close()
    return n


class Runner:
    def __init__(self, concurrency: int | None = None):
        self.concurrency = concurrency or config.worker_concurrency
        self.stop = threading.Event()
        self.threads: list[threading.Thread] = []

    def _loop(self):
        while not self.stop.is_set():
            db = SessionLocal()
            try:
                job = queue.claim(db)
                if job is None:
                    db.close()
                    self.stop.wait(config.worker_poll_seconds)
                    continue
                process(db, job)
            except Exception:  # noqa: BLE001 - keep the thread alive
                log.exception("runner loop error")
                time.sleep(2)
            finally:
                db.close()

    def start(self):
        for i in range(self.concurrency):
            t = threading.Thread(target=self._loop, name=f"job-runner-{i}", daemon=True)
            t.start()
            self.threads.append(t)
