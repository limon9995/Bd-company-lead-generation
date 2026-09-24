import math
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import settings_store
from app.config import config
from app.setup_status import checklist, progress
from app.db import get_db
from app.deps import audit, current_user, flash, render, verify_csrf
from app.models import ApiUsage, AuditLog, Campaign, EmailMessage, Job, Lead, Person, Run, User
from app.pipeline.queue import now
from app.pipeline.stages import cancel_run, run_progress, sync_leads_to_sheet
from app.services.errors import ProviderError

router = APIRouter()

REQUIRED = [
    ("places_api_key", "Google Places API key - needed to discover companies"),
    ("gemini_api_key", "Gemini API key - needed to extract decision makers"),
    ("telegram_bot_token", "Telegram bot token - needed for notifications"),
    ("smtp_password", "SMTP password - needed to send emails"),
]


def nice_step(raw: float) -> int:
    """Smallest 1/2/5 x 10^k integer >= raw (so the y-axis ticks are round numbers)."""
    if raw <= 1:
        return 1
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 5, 10):
        if m * mag >= raw:
            return int(m * mag)
    return int(10 * mag)


def leads_per_day(db: Session, days: int = 14) -> dict:
    """New leads per local (Dhaka) day, plus how many of them got a decision maker."""
    tz = ZoneInfo(config.timezone)
    today = datetime.now(tz).date()
    start = datetime.combine(today - timedelta(days=days - 1), time(0), tz)
    buckets = {today - timedelta(days=i): [0, 0] for i in range(days)}
    for created, pid in db.execute(select(Lead.created_at, Lead.primary_person_id).where(Lead.created_at >= start)):
        created = created if created.tzinfo else created.replace(tzinfo=ZoneInfo("UTC"))
        d = created.astimezone(tz).date()
        if d in buckets:
            buckets[d][0] += 1
            buckets[d][1] += 1 if pid else 0
    rows = [{"day": d, "n": v[0], "dm": v[1]} for d, v in sorted(buckets.items())]
    top = max((r["n"] for r in rows), default=0)
    step = nice_step(top / 4)
    ymax = step * 4
    # geometry for an inline SVG (viewBox 0 0 1000 200; plot area x 36..992, y 12..172)
    w = 956 / days
    for i, r in enumerate(rows):
        h = 160 * r["n"] / ymax
        r.update(x=round(36 + i * w + 6, 1), w=round(w - 12, 1), y=round(172 - h, 1), h=round(h, 1),
                 cx=round(36 + i * w + w / 2, 1), label=r["day"].strftime("%d %b"))
    ticks = [{"v": step * k, "y": round(172 - 40 * k, 1)} for k in range(5)]
    return {"rows": rows, "ticks": ticks, "total": sum(r["n"] for r in rows), "has_data": top > 0}


@router.get("/setup")
def setup_page(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    steps = checklist(db)
    done, total = progress(steps)
    return render(request, "setup.html", {"steps": steps, "done": done, "total": total})


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
    steps = checklist(db)
    setup_done, setup_total = progress(steps)
    return render(request, "dashboard.html", {
        "chart": leads_per_day(db), "steps": steps, "setup_done": setup_done, "setup_total": setup_total,
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
                                               "prog": run_progress(db, run),
                                               "by_status": by_status, "problems": problem_jobs[:200], "total_jobs": len(jobs)})


@router.get("/runs/{rid}/status")
def run_status(rid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    run = db.get(Run, rid)
    if run is None:
        raise HTTPException(404)
    return JSONResponse(run_progress(db, run))


@router.post("/runs/{rid}/cancel", dependencies=[Depends(verify_csrf)])
def run_cancel(rid: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    run = db.get(Run, rid)
    if run is None:
        raise HTTPException(404)
    if run.status != "running":
        flash(request, "This run is not running.", "err")
    else:
        n = cancel_run(db, run)
        audit(db, user, "run.cancel", str(rid), f"{n} queued jobs skipped")
        db.commit()
        flash(request, f"Run #{rid} cancelled. {n} queued step(s) will not run; steps already in progress finish first.")
    return RedirectResponse(f"/runs/{rid}", 303)


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
