from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import settings_store
from app.db import get_db
from app.deps import audit, current_user, flash, render, verify_csrf
from app.models import ApiUsage, AuditLog, Campaign, EmailMessage, Job, Lead, Person, Run, User
from app.pipeline.queue import now
from app.pipeline.stages import sync_leads_to_sheet
from app.services.errors import ProviderError

router = APIRouter()

REQUIRED = [
    ("places_api_key", "Google Places API key - needed to discover companies"),
    ("gemini_api_key", "Gemini API key - needed to extract decision makers"),
    ("telegram_bot_token", "Telegram bot token - needed for notifications"),
    ("smtp_password", "SMTP password - needed to send emails"),
]


@router.get("/")
def dashboard(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    total_leads = db.scalar(select(func.count(Lead.id))) or 0
    with_dm = db.scalar(select(func.count(Lead.id)).where(Lead.primary_person_id.is_not(None))) or 0
    high = db.scalar(select(func.count(Lead.id)).join(Person, Lead.primary_person_id == Person.id).where(Person.confidence >= 70)) or 0
    sent_7d = db.scalar(select(func.count(EmailMessage.id)).where(EmailMessage.sent_at >= now() - timedelta(days=7))) or 0
    drafts = db.scalar(select(func.count(EmailMessage.id)).where(EmailMessage.status == "draft")) or 0
    per_campaign = []
    for c in db.scalars(select(Campaign).order_by(Campaign.name)):
        n = db.scalar(select(func.count(Lead.id)).where(Lead.campaign_id == c.id)) or 0
        d = db.scalar(select(func.count(Lead.id)).where(Lead.campaign_id == c.id, Lead.primary_person_id.is_not(None))) or 0
        per_campaign.append({"c": c, "n": n, "dm": d, "rate": round(100 * d / n) if n else 0})
    usage = db.scalars(select(ApiUsage).order_by(ApiUsage.day.desc(), ApiUsage.provider).limit(21)).all()
    warnings = [msg for key, msg in REQUIRED if not settings_store.is_set(db, key)]
    if settings_store.is_set(db, "telegram_bot_token") and not settings_store.get(db, "telegram_chat_id"):
        warnings.append("Telegram chat ID not set - Settings → Telegram → Detect chat ID")
    return render(request, "dashboard.html", {
        "total": total_leads, "with_dm": with_dm, "high": high, "sent_7d": sent_7d, "drafts": drafts,
        "rate": round(100 * with_dm / total_leads) if total_leads else 0, "per_campaign": per_campaign,
        "usage": usage, "warnings": warnings,
        "runs": db.scalars(select(Run).order_by(Run.id.desc()).limit(8)).all(),
        "failed_jobs": db.scalar(select(func.count(Job.id)).where(Job.status == "failed")) or 0,
    })


@router.get("/runs")
def runs(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    items = db.scalars(select(Run).order_by(Run.id.desc()).limit(100)).all()
    names = {c.id: c.name for c in db.scalars(select(Campaign))}
    return render(request, "runs.html", {"items": items, "names": names})


@router.get("/runs/{rid}")
def run_detail(rid: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    run = db.get(Run, rid)
    if run is None:
        raise HTTPException(404)
    jobs = db.scalars(select(Job).where(Job.run_id == rid).order_by(Job.id)).all()
    by_status: dict[str, int] = {}
    for j in jobs:
        by_status[j.status] = by_status.get(j.status, 0) + 1
    problem_jobs = [j for j in jobs if j.status in ("failed", "skipped") or (j.status == "queued" and j.last_error)]
    return render(request, "run_detail.html", {"run": run, "campaign": db.get(Campaign, run.campaign_id),
                                               "by_status": by_status, "problems": problem_jobs[:200], "total_jobs": len(jobs)})


@router.post("/runs/{rid}/retry-failed", dependencies=[Depends(verify_csrf)])
def retry_failed(rid: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    n = 0
    for j in db.scalars(select(Job).where(Job.run_id == rid, Job.status.in_(["failed", "skipped"]))):
        j.status, j.attempts, j.run_after, j.last_error = "queued", 0, now(), ""
        n += 1
    fin = db.scalar(select(Job).where(Job.dedupe_key == f"finalize:{rid}"))
    if n and fin is not None:
        db.delete(fin)  # so the run is finalized again after the retries
    run = db.get(Run, rid)
    if n and run:
        run.status = "running"
    audit(db, user, "run.retry", str(rid), str(n))
    db.commit()
    flash(request, f"Re-queued {n} job(s).")
    return RedirectResponse(f"/runs/{rid}", 303)


@router.post("/runs/{rid}/sync-sheet", dependencies=[Depends(verify_csrf)])
def sync_sheet(rid: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    leads = list(db.scalars(select(Lead).where(Lead.run_id == rid)))
    try:
        flash(request, sync_leads_to_sheet(db, leads))
        db.commit()
    except ProviderError as exc:
        flash(request, f"Sheet sync failed: {exc}", "err")
    return RedirectResponse(f"/runs/{rid}", 303)


@router.get("/audit")
def audit_page(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    items = db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(300)).all()
    emails = {u.id: u.email for u in db.scalars(select(User))}
    return render(request, "audit.html", {"items": items, "emails": emails})
