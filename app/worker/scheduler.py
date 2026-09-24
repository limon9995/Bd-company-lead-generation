"""Cron scheduling of campaigns, email dispatch ticks, stale-job recovery and the daily digest."""
import logging
from datetime import timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import func, select

from app.config import config
from app.db import SessionLocal, session_scope
from app.models import Campaign, EmailMessage, Lead, Run
from app.pipeline import queue
from app.pipeline.emails import Dispatcher
from app.pipeline.stages import start_run

log = logging.getLogger(__name__)
dispatcher = Dispatcher()


def fire_campaign(campaign_id: int) -> None:
    with session_scope() as db:
        campaign = db.get(Campaign, campaign_id)
        if campaign is None or not campaign.is_active:
            return
        running = db.scalar(select(Run.id).where(Run.campaign_id == campaign_id, Run.status == "running"))
        if running:
            log.info("campaign %s still running (run %s) - skipping scheduled run", campaign_id, running)
            return
        start_run(db, campaign, trigger="schedule")


def email_tick() -> None:
    db = SessionLocal()
    try:
        result = dispatcher.tick(db)
        if result.startswith(("sent", "failed")):
            log.info("email: %s", result)
    except Exception:  # noqa: BLE001
        log.exception("email tick failed")
    finally:
        db.close()


def recover_stale() -> None:
    with session_scope() as db:
        n = queue.requeue_stale(db)
        if n:
            log.warning("requeued %s stale jobs", n)


def daily_digest() -> None:
    from app.services.notifier import notify

    with session_scope() as db:
        since = queue.now() - timedelta(days=1)
        leads = db.scalar(select(func.count(Lead.id)).where(Lead.created_at >= since)) or 0
        sent = db.scalar(select(func.count(EmailMessage.id)).where(EmailMessage.sent_at >= since)) or 0
        drafts = db.scalar(select(func.count(EmailMessage.id)).where(EmailMessage.status == "draft")) or 0
        notify(db, f"📊 <b>Daily digest</b>\nNew leads (24h): {leads}\nEmails sent (24h): {sent}\nDrafts waiting for approval: {drafts}")


class CampaignScheduler:
    def __init__(self):
        self.sched = BackgroundScheduler(timezone=config.timezone)
        self._crons: dict[int, str] = {}

    def sync(self) -> None:
        with session_scope() as db:
            wanted = {c.id: c.schedule_cron.strip() for c in db.scalars(select(Campaign).where(Campaign.is_active))
                      if c.schedule_cron and c.schedule_cron.strip()}
        for cid in list(self._crons):
            if cid not in wanted:
                self.sched.remove_job(f"campaign-{cid}")
                del self._crons[cid]
        for cid, cron in wanted.items():
            if self._crons.get(cid) == cron:
                continue
            try:
                trigger = CronTrigger.from_crontab(cron, timezone=config.timezone)
            except ValueError:
                log.warning("campaign %s has invalid cron %r", cid, cron)
                continue
            self.sched.add_job(fire_campaign, trigger, args=[cid], id=f"campaign-{cid}", replace_existing=True,
                               max_instances=1, coalesce=True, misfire_grace_time=3600)
            self._crons[cid] = cron

    def start(self) -> None:
        self.sched.add_job(self.sync, "interval", seconds=60, id="sync-campaigns", max_instances=1)
        self.sched.add_job(email_tick, "interval", seconds=30, id="email-tick", max_instances=1)
        self.sched.add_job(recover_stale, "interval", minutes=5, id="recover-stale", max_instances=1)
        self.sched.add_job(daily_digest, CronTrigger(hour=9, minute=0, timezone=config.timezone), id="daily-digest")
        self.sched.start()
        self.sync()
