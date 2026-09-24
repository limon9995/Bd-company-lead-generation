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
from app.pipeline.stages import campaign_targets, lead_row, primary_candidates, set_primary
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
        "templates_": db.scalars(select(EmailTemplate).order_by(EmailTemplate.name)).all(),
        "back": str(request.url.path) + (f"?{request.url.query}" if request.url.query else ""),
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


@router.post("/leads/bulk", dependencies=[Depends(verify_csrf)])
async def leads_bulk(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    form = await request.form()
    ids = [int(x) for x in form.getlist("ids") if str(x).isdigit()]
    action = form.get("action", "")
    back = form.get("back") or "/leads"
    if not back.startswith("/leads"):
        back = "/leads"
    if not ids:
        flash(request, "Select at least one lead first.", "err")
        return RedirectResponse(back, 303)
    leads = db.scalars(select(Lead).where(Lead.id.in_(ids))).all()
    if action == "export":
        buf = io.StringIO()
        buf.write("\ufeff")
        w = csv.DictWriter(buf, fieldnames=LEAD_HEADERS, extrasaction="ignore")
        w.writeheader()
        for lead in leads:
            w.writerow(lead_row(lead))
        return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                                 headers={"Content-Disposition": "attachment; filename=leads-selected.csv"})
    if action == "status":
        status = form.get("crm_status", "")
        if status not in CRM_STATUSES:
            flash(request, "Pick a CRM status.", "err")
            return RedirectResponse(back, 303)
        for lead in leads:
            lead.crm_status = status
            if status == "do_not_contact":
                for m in db.scalars(select(EmailMessage).where(EmailMessage.lead_id == lead.id,
                                                                  EmailMessage.status.in_(["draft", "approved"]))):
                    m.status, m.error = "cancelled", "lead marked do-not-contact"
        audit(db, user, "lead.bulk_status", ",".join(map(str, ids))[:500], status)
        db.commit()
        flash(request, f"Set {len(leads)} lead(s) to '{status}'.")
    elif action == "draft":
        tpl = db.get(EmailTemplate, int(form.get("template_id") or 0))
        if tpl is None:
            flash(request, "Pick an email template.", "err")
            return RedirectResponse(back, 303)
        made, skipped = 0, 0
        for lead in leads:
            to, _ = outreach_address(lead)
            has_open = db.scalar(select(EmailMessage.id).where(EmailMessage.lead_id == lead.id,
                                                                EmailMessage.status.in_(["draft", "approved"])))
            if not to or has_open or lead.crm_status == "do_not_contact" or is_suppressed(db, to):
                skipped += 1
                continue
            make_draft(db, lead, tpl, to)
            made += 1
        audit(db, user, "lead.bulk_draft", ",".join(map(str, ids))[:500], f"{made} drafts")
        db.commit()
        flash(request, f"Created {made} draft(s)." + (f" Skipped {skipped} (no email, already drafted, or do-not-contact)." if skipped else ""))
        if made:
            return RedirectResponse("/outbox", 303)
    else:
        flash(request, "Unknown action.", "err")
    return RedirectResponse(back, 303)


@router.get("/leads/{lid}")
def lead_detail(lid: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    lead = db.get(Lead, lid)
    if lead is None:
        raise HTTPException(404)
    return render(request, "lead_detail.html", {
        "lead": lead, "company": lead.company,
        # best contact first, people marked wrong last
        "people": (primary_candidates(lead.company, campaign_targets(db, lead.campaign))
                   + [p for p in lead.company.people if p.feedback == "wrong"]),
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
        chosen = next((p for p in lead.company.people if p.id == pid), None)
        if chosen is not None and pid != lead.primary_person_id:
            set_primary(lead.company, lead, chosen)  # also picks the new contact's email
    if crm_status == "do_not_contact":
        for m in db.scalars(select(EmailMessage).where(EmailMessage.lead_id == lid, EmailMessage.status.in_(["draft", "approved"]))):
            m.status, m.error = "cancelled", "lead marked do-not-contact"
    audit(db, user, "lead.update", str(lid), crm_status)
    db.commit()
    flash(request, "Lead updated.")
    return RedirectResponse(f"/leads/{lid}", 303)


@router.post("/leads/{lid}/people/{pid}/feedback", dependencies=[Depends(verify_csrf)])
def person_feedback(lid: int, pid: int, request: Request, verdict: str = Form(...),
                    user: User = Depends(current_user), db: Session = Depends(get_db)):
    """✓ correct: this is the right contact (becomes the primary). ✗ wrong: never use this person again for
    this company - every lead that had them as contact moves to the next best person, and their open
    drafts are cancelled. clear: undo."""
    lead = db.get(Lead, lid)
    person = db.get(Person, pid)
    if lead is None or person is None or person.company_id != lead.company_id or verdict not in ("correct", "wrong", "clear"):
        raise HTTPException(400)
    company = lead.company
    person.feedback = "" if verdict == "clear" else verdict
    if verdict == "correct":
        set_primary(company, lead, person)
    elif verdict == "wrong":
        wrong_email = person.email
        for other in db.scalars(select(Lead).where(Lead.primary_person_id == person.id)).all():
            ranked = primary_candidates(company, campaign_targets(db, other.campaign))
            set_primary(company, other, ranked[0] if ranked else None)
        # unsent emails written for this person ("Dear Rahim ...") are wrong wherever they were going
        written_for = [EmailMessage.person_id == person.id]
        if wrong_email:
            written_for.append(EmailMessage.to_email == wrong_email)
        for m in db.scalars(select(EmailMessage).join(Lead, EmailMessage.lead_id == Lead.id)
                            .where(Lead.company_id == company.id, or_(*written_for),
                                   EmailMessage.status.in_(["draft", "approved"]))):
            m.status, m.error = "cancelled", "contact marked as the wrong person"
    audit(db, user, "person.feedback", f"{pid}:{person.full_name}"[:200], verdict)
    db.commit()
    flash(request, {"correct": f"Marked {person.full_name} as correct - now the contact for this lead.",
                    "wrong": f"Marked {person.full_name} as wrong - they won't be used for this company again.",
                    "clear": f"Cleared feedback for {person.full_name}."}[verdict])
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
