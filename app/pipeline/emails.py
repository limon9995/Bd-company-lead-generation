"""Email drafts and the rate-limited dispatcher (send window, daily cap, gaps, suppression, cooldown)."""
import logging
import random
import re
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import settings_store
from app.config import config
from app.models import Campaign, EmailMessage, EmailTemplate, Lead, Suppression
from app.pipeline import providers
from app.services import mailer
from app.services.errors import BudgetExceeded, ProviderError
from app.services.usage import check_budget, record_call
from app.security import new_token

log = logging.getLogger(__name__)
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
SENDABLE_EMAIL_STATUSES = {"found", "company"}  # guessed addresses are never auto-approved


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def is_suppressed(db: Session, email: str) -> bool:
    email = (email or "").lower()
    domain = "@" + email.split("@")[-1]
    return db.scalar(select(Suppression.id).where(Suppression.value.in_([email, domain]))) is not None


def suppress(db: Session, value: str, reason: str) -> None:
    value = value.strip().lower()
    if value and not db.scalar(select(Suppression.id).where(Suppression.value == value)):
        db.add(Suppression(value=value, reason=reason))


def unsubscribe_url(token: str) -> str:
    return f"{config.base_url}/u/{token}"


def build_context(db: Session, lead: Lead) -> dict:
    c, p = lead.company, lead.primary_person
    first = ""
    if p:
        from app.services.normalize import person_key

        parts = person_key(p.full_name).split()
        first = parts[0].title() if parts else ""
    facts = "; ".join(filter(None, [
        c.category, f"rated {c.rating} on Google" if c.rating else "", c.address,
    ]))
    return {
        "company_name": c.name, "person_name": p.full_name if p else "", "first_name": first or "Sir/Madam",
        "title": p.title if p else "", "city": c.city, "industry": c.industry_slug.replace("_", " "),
        "category": c.category, "sender_name": settings_store.get(db, "sender_name"),
        "sender_company": settings_store.get(db, "sender_company"), "facts": facts,
    }


def make_draft(db: Session, lead: Lead, tpl: EmailTemplate, to_email: str) -> EmailMessage:
    ctx = build_context(db, lead)
    ctx["personal_line"] = ""
    if tpl.use_ai_personalisation:
        llm = providers.get_llm(db)
        if llm is not None:
            try:
                check_budget(db, "gemini")
                ctx["personal_line"] = mailer.personal_line(llm, ctx, tpl.ai_instructions)
                record_call(db, "gemini")
            except (ProviderError, BudgetExceeded) as exc:
                log.warning("personalisation skipped: %s", exc)
    token = new_token(24)
    body = re.sub(r"\n{3,}", "\n\n", mailer.render(tpl.body_tpl, ctx)).strip()  # empty personal_line
    signature = settings_store.get(db, "sender_signature")
    if signature:
        body += "\n\n" + signature
    body += mailer.footer(unsubscribe_url(token), ctx["sender_company"])
    msg = EmailMessage(lead_id=lead.id, person_id=lead.primary_person_id, to_email=to_email.lower(),
                       subject=mailer.render(tpl.subject_tpl, ctx).strip(), body=body, status="draft",
                       unsubscribe_token=token)
    db.add(msg)
    db.flush()
    return msg


def outreach_address(lead: Lead) -> tuple[str, str]:
    p = lead.primary_person
    if p and p.email:
        return p.email, p.email_status
    emails = lead.company.generic_emails or []
    return (emails[0], "company") if emails else ("", "unknown")


def create_drafts_for_leads(db: Session, campaign: Campaign, tpl: EmailTemplate | None, leads: list[Lead]) -> int:
    if tpl is None:
        return 0
    threshold = settings_store.get(db, "auto_send_min_confidence")
    made = 0
    for lead in leads:
        if lead.crm_status == "do_not_contact":
            continue
        if db.scalar(select(EmailMessage.id).where(EmailMessage.lead_id == lead.id)):
            continue
        to, status = outreach_address(lead)
        if not to or is_suppressed(db, to):
            continue
        msg = make_draft(db, lead, tpl, to)
        p = lead.primary_person
        if campaign.auto_email and p and p.confidence >= threshold and status in SENDABLE_EMAIL_STATUSES:
            msg.status = "approved"
        made += 1
    return made


# ---------------------------------------------------------------- dispatcher
def in_send_window(db: Session, at: datetime) -> bool:
    local = at.astimezone(ZoneInfo(config.timezone))
    days = {d.strip().lower()[:3] for d in settings_store.get(db, "send_days").split(",") if d.strip()}
    if DAYS[local.weekday()] not in days:
        return False
    try:
        start = time.fromisoformat(settings_store.get(db, "send_window_start"))
        end = time.fromisoformat(settings_store.get(db, "send_window_end"))
    except ValueError:
        return False
    return start <= local.time() < end


def sent_today(db: Session, at: datetime) -> int:
    tz = ZoneInfo(config.timezone)
    local_midnight = datetime.combine(at.astimezone(tz).date(), time(0), tz)
    return db.scalar(select(func.count(EmailMessage.id)).where(
        EmailMessage.status == "sent", EmailMessage.sent_at >= local_midnight.astimezone(timezone.utc))) or 0


def _aware(dt: datetime | None) -> datetime | None:
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class Dispatcher:
    """Sends at most one email per tick, respecting all limits. Called every ~30s by the worker."""

    def __init__(self):
        self.next_allowed: datetime | None = None

    def tick(self, db: Session, at: datetime | None = None, sender=mailer.send_smtp) -> str:
        at = at or utcnow()
        if self.next_allowed and at < self.next_allowed:
            return "waiting gap"
        if not in_send_window(db, at):
            return "outside window"
        if sent_today(db, at) >= settings_store.get(db, "daily_email_cap"):
            return "daily cap reached"
        msg = db.scalar(select(EmailMessage).where(EmailMessage.status == "approved").where(
            (EmailMessage.not_before.is_(None)) | (EmailMessage.not_before <= at)).order_by(EmailMessage.id).limit(1))
        if msg is None:
            return "nothing to send"
        lead = msg.lead
        if is_suppressed(db, msg.to_email) or lead.crm_status == "do_not_contact":
            msg.status, msg.error = "cancelled", "suppressed / do-not-contact"
            db.commit()
            return "cancelled (suppressed)"
        cooldown = settings_store.get(db, "company_cooldown_days")
        recent = db.scalar(select(EmailMessage.id).join(Lead).where(
            Lead.company_id == lead.company_id, EmailMessage.status == "sent", EmailMessage.id != msg.id,
            EmailMessage.sent_at >= at - timedelta(days=cooldown)))
        if recent:
            msg.status, msg.error = "cancelled", f"company already emailed in the last {cooldown} days"
            db.commit()
            return "cancelled (cooldown)"
        s = settings_store.snapshot(db)
        try:
            sender(s, msg.to_email, msg.subject, msg.body, unsubscribe_url(msg.unsubscribe_token))
        except ProviderError as exc:
            msg.status, msg.error = "failed", str(exc)[:1000]
            if "recipient refused" in str(exc):
                suppress(db, msg.to_email, "bounced")
            db.commit()
            return f"failed: {exc}"
        msg.status, msg.sent_at, msg.error = "sent", at, ""
        if lead.crm_status == "new":
            lead.crm_status = "contacted"
        lead.last_contacted_at = at
        db.commit()
        gap = random.randint(int(s["email_min_gap_seconds"]), max(int(s["email_min_gap_seconds"]), int(s["email_max_gap_seconds"])))
        self.next_allowed = at + timedelta(seconds=gap)
        return f"sent to {msg.to_email}"
