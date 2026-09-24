import csv
import io

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, joinedload

from app.db import get_db
from app.deps import audit, current_user, flash, render, verify_csrf
from app.models import CRM_STATUSES, Campaign, Company, EmailMessage, EmailTemplate, Lead, Person, User
from app.pipeline.emails import is_suppressed, make_draft, outreach_address
from app.pipeline.stages import lead_row
from app.services.sheets import LEAD_HEADERS

router = APIRouter()
PAGE_SIZE = 50


def _query(campaign: str, status: str, level: str, q: str):
    stmt = (select(Lead).join(Company, Lead.company_id == Company.id)
            .outerjoin(Person, Lead.primary_person_id == Person.id)
            .options(joinedload(Lead.company), joinedload(Lead.primary_person), joinedload(Lead.campaign)))
    if campaign:
        stmt = stmt.where(Lead.campaign_id == int(campaign))
    if status:
        stmt = stmt.where(Lead.crm_status == status)
    if level == "high":
        stmt = stmt.where(Person.confidence >= 70)
    elif level == "medium":
        stmt = stmt.where(Person.confidence >= 40, Person.confidence < 70)
    elif level == "low":
        stmt = stmt.where(Person.confidence < 40)
    elif level == "none":
        stmt = stmt.where(Lead.primary_person_id.is_(None))
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Company.name.ilike(like), Person.full_name.ilike(like), Company.city.ilike(like)))
    return stmt.order_by(Lead.id.desc())


@router.get("/leads")
def leads(request: Request, campaign: str = "", status: str = "", level: str = "", q: str = "", page: int = 1,
          user: User = Depends(current_user), db: Session = Depends(get_db)):
    page = max(1, page)
    items = db.scalars(_query(campaign, status, level, q).offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE + 1)).unique().all()
    return render(request, "leads.html", {
        "items": items[:PAGE_SIZE], "has_next": len(items) > PAGE_SIZE, "page": page,
        "f": {"campaign": campaign, "status": status, "level": level, "q": q},
        "campaigns": db.scalars(select(Campaign).order_by(Campaign.name)).all(), "statuses": CRM_STATUSES,
    })


@router.get("/leads/export.csv")
def export_csv(campaign: str = "", status: str = "", level: str = "", q: str = "",
               user: User = Depends(current_user), db: Session = Depends(get_db)):
    buf = io.StringIO()
    buf.write("﻿")  # BOM so Excel opens UTF-8 (Bangla) correctly
    w = csv.DictWriter(buf, fieldnames=LEAD_HEADERS, extrasaction="ignore")
    w.writeheader()
    for lead in db.scalars(_query(campaign, status, level, q)).unique():
        w.writerow(lead_row(lead))
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=leads.csv"})


@router.get("/leads/{lid}")
def lead_detail(lid: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    lead = db.get(Lead, lid)
    if lead is None:
        raise HTTPException(404)
    return render(request, "lead_detail.html", {
        "lead": lead, "company": lead.company, "people": sorted(lead.company.people, key=lambda p: -p.confidence),
        "messages": db.scalars(select(EmailMessage).where(EmailMessage.lead_id == lid).order_by(EmailMessage.id.desc())).all(),
        "statuses": CRM_STATUSES, "templates_": db.scalars(select(EmailTemplate)).all(),
    })


@router.post("/leads/{lid}", dependencies=[Depends(verify_csrf)])
def lead_update(lid: int, request: Request, crm_status: str = Form(...), notes: str = Form(""), owner: str = Form(""),
                primary_person_id: str = Form(""), user: User = Depends(current_user), db: Session = Depends(get_db)):
    lead = db.get(Lead, lid)
    if lead is None or crm_status not in CRM_STATUSES:
        raise HTTPException(400)
    lead.crm_status, lead.notes, lead.owner = crm_status, notes, owner.strip()
    if primary_person_id:
        pid = int(primary_person_id)
        if any(p.id == pid for p in lead.company.people):
            lead.primary_person_id = pid
    if crm_status == "do_not_contact":
        for m in db.scalars(select(EmailMessage).where(EmailMessage.lead_id == lid, EmailMessage.status.in_(["draft", "approved"]))):
            m.status, m.error = "cancelled", "lead marked do-not-contact"
    audit(db, user, "lead.update", str(lid), crm_status)
    db.commit()
    flash(request, "Lead updated.")
    return RedirectResponse(f"/leads/{lid}", 303)


@router.post("/leads/{lid}/draft", dependencies=[Depends(verify_csrf)])
def lead_draft(lid: int, request: Request, template_id: int = Form(...), to_email: str = Form(""),
               user: User = Depends(current_user), db: Session = Depends(get_db)):
    lead = db.get(Lead, lid)
    tpl = db.get(EmailTemplate, template_id)
    if lead is None or tpl is None:
        raise HTTPException(404)
    to = to_email.strip() or outreach_address(lead)[0]
    if not to:
        flash(request, "No email address known - type one in.", "err")
    elif is_suppressed(db, to):
        flash(request, f"{to} is on the suppression list.", "err")
    else:
        msg = make_draft(db, lead, tpl, to)
        db.commit()
        flash(request, "Draft created - review it in the Outbox.")
        return RedirectResponse(f"/outbox/{msg.id}", 303)
    return RedirectResponse(f"/leads/{lid}", 303)
