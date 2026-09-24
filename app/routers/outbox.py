from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import settings_store
from app.config import config
from app.db import get_db
from app.deps import audit, current_user, flash, render, verify_csrf
from app.models import EMAIL_STATUSES, EmailMessage, Suppression, User
from app.pipeline.emails import in_send_window, sent_today, suppress, utcnow

router = APIRouter()


@router.get("/outbox")
def outbox(request: Request, status: str = "draft", user: User = Depends(current_user), db: Session = Depends(get_db)):
    items = db.scalars(select(EmailMessage).where(EmailMessage.status == status).order_by(EmailMessage.id.desc()).limit(200)).all()
    now = utcnow()
    return render(request, "outbox.html", {
        "items": items, "status": status, "statuses": EMAIL_STATUSES,
        "window_open": in_send_window(db, now), "sent_today": sent_today(db, now),
        "cap": settings_store.get(db, "daily_email_cap"), "smtp_ready": settings_store.is_set(db, "smtp_password"),
    })


@router.post("/outbox/approve-all", dependencies=[Depends(verify_csrf)])
def approve_all(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    n = 0
    for m in db.scalars(select(EmailMessage).where(EmailMessage.status == "draft")):
        m.status = "approved"
        n += 1
    audit(db, user, "email.approve_all", "", str(n))
    db.commit()
    flash(request, f"Approved {n} drafts. They will be sent inside the send window, respecting the daily cap.")
    return RedirectResponse("/outbox?status=approved", 303)


@router.get("/outbox/{mid}")
def message(mid: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    m = db.get(EmailMessage, mid)
    if m is None:
        raise HTTPException(404)
    return render(request, "message.html", {"m": m})


@router.post("/outbox/{mid}", dependencies=[Depends(verify_csrf)])
def message_save(mid: int, request: Request, action: str = Form(...), to_email: str = Form(...), subject: str = Form(...),
                 body: str = Form(...), not_before: str = Form(""), user: User = Depends(current_user),
                 db: Session = Depends(get_db)):
    m = db.get(EmailMessage, mid)
    if m is None:
        raise HTTPException(404)
    if m.status == "sent":
        flash(request, "Already sent - can't edit.", "err")
        return RedirectResponse(f"/outbox/{mid}", 303)
    m.to_email, m.subject, m.body = to_email.strip().lower(), subject.strip(), body
    m.not_before = None
    if not_before:
        try:
            m.not_before = datetime.fromisoformat(not_before).replace(tzinfo=ZoneInfo(config.timezone))
        except ValueError:
            flash(request, "Bad date/time.", "err")
    if action == "approve":
        m.status = "approved"
    elif action == "cancel":
        m.status = "cancelled"
    elif action == "draft":
        m.status = "draft"
    audit(db, user, f"email.{action}", str(mid))
    db.commit()
    flash(request, {"approve": "Approved - it will go out in the next send window.", "cancel": "Cancelled."}.get(action, "Saved."))
    return RedirectResponse(f"/outbox/{mid}", 303)


@router.get("/suppression")
def suppression(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return render(request, "suppression.html", {"items": db.scalars(select(Suppression).order_by(Suppression.id.desc())).all()})


@router.post("/suppression", dependencies=[Depends(verify_csrf)])
def suppression_add(request: Request, value: str = Form(...), user: User = Depends(current_user), db: Session = Depends(get_db)):
    v = value.strip().lower()
    if "@" not in v:
        flash(request, "Enter an email (name@company.com) or a domain as @company.com", "err")
    else:
        suppress(db, v, "manual")
        audit(db, user, "suppression.add", v)
        db.commit()
        flash(request, f"Added {v}.")
    return RedirectResponse("/suppression", 303)


@router.post("/suppression/{sid}/delete", dependencies=[Depends(verify_csrf)])
def suppression_delete(sid: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    s = db.get(Suppression, sid)
    if s:
        audit(db, user, "suppression.delete", s.value)
        db.delete(s)
        db.commit()
    return RedirectResponse("/suppression", 303)
