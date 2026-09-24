from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import config
from app.db import get_db
from app.deps import audit, current_user, flash, render, verify_csrf
from app.models import Campaign, EmailTemplate, IndustryPreset, Lead, Run, User
from app.pipeline.stages import start_run

router = APIRouter()


def split_lines(s: str) -> list[str]:
    return [x.strip() for x in (s or "").splitlines() if x.strip()]


def split_list(s: str) -> list[str]:
    return [x.strip() for x in (s or "").replace("\n", ",").split(",") if x.strip()]


# ------------------------------------------------------------------ industries ("switch node")
@router.get("/industries")
def industries(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return render(request, "industries.html", {"presets": db.scalars(select(IndustryPreset).order_by(IndustryPreset.name)).all()})


@router.get("/industries/new")
@router.get("/industries/{pid}/edit")
def industry_form(request: Request, pid: int | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)):
    preset = db.get(IndustryPreset, pid) if pid else None
    return render(request, "industry_form.html", {"p": preset})


@router.post("/industries", dependencies=[Depends(verify_csrf)])
@router.post("/industries/{pid}", dependencies=[Depends(verify_csrf)])
def industry_save(request: Request, pid: int | None = None, slug: str = Form(...), name: str = Form(...),
                  places_queries: str = Form(""), default_titles: str = Form(""), pitch_context: str = Form(""),
                  user: User = Depends(current_user), db: Session = Depends(get_db)):
    slug = slug.strip().lower().replace(" ", "_")
    p = db.get(IndustryPreset, pid) if pid else IndustryPreset(slug=slug)
    if pid is None:
        if db.scalar(select(IndustryPreset).where(IndustryPreset.slug == slug)):
            flash(request, "An industry with that slug already exists.", "err")
            return RedirectResponse("/industries/new", 303)
        db.add(p)
    p.name = name.strip()
    p.places_queries = split_list(places_queries)
    p.default_titles = split_list(default_titles)
    p.pitch_context = pitch_context.strip()
    audit(db, user, "industry.save", slug)
    db.commit()
    flash(request, f"Saved industry '{p.name}'.")
    return RedirectResponse("/industries", 303)


# ------------------------------------------------------------------ campaigns
@router.get("/campaigns")
def campaigns(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = []
    for c in db.scalars(select(Campaign).order_by(Campaign.id.desc())):
        leads = db.scalar(select(func.count(Lead.id)).where(Lead.campaign_id == c.id)) or 0
        last = db.scalar(select(Run).where(Run.campaign_id == c.id).order_by(Run.id.desc()).limit(1))
        rows.append({"c": c, "leads": leads, "last": last})
    return render(request, "campaigns.html", {"rows": rows})


@router.get("/campaigns/new")
@router.get("/campaigns/{cid}/edit")
def campaign_form(request: Request, cid: int | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)):
    c = db.get(Campaign, cid) if cid else None
    if cid and c is None:
        raise HTTPException(404)
    return render(request, "campaign_form.html", {
        "c": c, "presets": db.scalars(select(IndustryPreset).order_by(IndustryPreset.name)).all(),
        "templates_": db.scalars(select(EmailTemplate).order_by(EmailTemplate.name)).all(), "tz": config.timezone,
    })


@router.post("/campaigns", dependencies=[Depends(verify_csrf)])
@router.post("/campaigns/{cid}", dependencies=[Depends(verify_csrf)])
def campaign_save(request: Request, cid: int | None = None, name: str = Form(...), industry_slug: str = Form(...),
                  cities: str = Form("Dhaka"), extra_keywords: str = Form(""), target_titles: str = Form(""),
                  max_companies_per_run: int = Form(50), schedule_cron: str = Form(""),
                  email_template_id: str = Form(""), auto_email: str | None = Form(None),
                  discovery_source: str = Form("places_api"), directory_urls: str = Form(""),
                  directory_max_pages: int = Form(5),
                  is_active: str | None = Form(None), user: User = Depends(current_user), db: Session = Depends(get_db)):
    cron = schedule_cron.strip()
    if cron:
        try:
            CronTrigger.from_crontab(cron)
        except ValueError:
            flash(request, f"Invalid schedule '{cron}'. Use 5-field cron, e.g. '0 9 * * 0' (Sundays 09:00).", "err")
            return RedirectResponse(f"/campaigns/{cid}/edit" if cid else "/campaigns/new", 303)
    c = db.get(Campaign, cid) if cid else Campaign()
    if not cid:
        db.add(c)
    c.name = name.strip()
    c.industry_slug = industry_slug
    c.cities = split_list(cities) or ["Dhaka"]
    c.extra_keywords = split_list(extra_keywords)
    c.target_titles = split_list(target_titles)
    c.max_companies_per_run = max(1, min(int(max_companies_per_run), 1000))
    c.schedule_cron = cron
    c.email_template_id = int(email_template_id) if email_template_id else None
    c.auto_email = bool(auto_email)
    c.discovery_source = discovery_source if discovery_source in ("places_api", "maps_browser", "directory") else "places_api"
    c.directory_urls = [u for u in split_lines(directory_urls) if u.startswith(("http://", "https://"))]
    c.directory_max_pages = max(1, min(int(directory_max_pages), 50))
    if c.discovery_source == "directory" and not c.directory_urls:
        flash(request, "Add at least one directory URL (starting with https://) for a directory campaign.", "err")
    c.is_active = bool(is_active) if cid else True
    db.flush()
    audit(db, user, "campaign.save", f"{c.id}:{c.name}")
    db.commit()
    flash(request, f"Saved campaign '{c.name}'.")
    return RedirectResponse("/campaigns", 303)


@router.post("/campaigns/{cid}/run", dependencies=[Depends(verify_csrf)])
def campaign_run(cid: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    c = db.get(Campaign, cid)
    if c is None:
        raise HTTPException(404)
    running = db.scalar(select(Run.id).where(Run.campaign_id == cid, Run.status == "running"))
    if running:
        flash(request, f"Run #{running} is still in progress.", "err")
        return RedirectResponse(f"/runs/{running}", 303)
    run = start_run(db, c, "manual")
    audit(db, user, "campaign.run", f"{c.id}:{c.name}", f"run {run.id}")
    db.commit()
    flash(request, f"Run #{run.id} queued. The worker will pick it up in a few seconds.")
    return RedirectResponse(f"/runs/{run.id}", 303)


# ------------------------------------------------------------------ email templates
@router.get("/templates")
def templates_list(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return render(request, "email_templates.html", {"items": db.scalars(select(EmailTemplate).order_by(EmailTemplate.id)).all()})


@router.get("/templates/new")
@router.get("/templates/{tid}/edit")
def template_form(request: Request, tid: int | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)):
    from app.services.mailer import TEMPLATE_VARS

    return render(request, "email_template_form.html", {"t": db.get(EmailTemplate, tid) if tid else None, "vars": TEMPLATE_VARS})


@router.post("/templates/preview", dependencies=[Depends(verify_csrf)])
async def template_preview(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Render the (unsaved) template against a real lead, or sample data when there are no leads yet."""
    from fastapi.responses import JSONResponse

    from app import settings_store
    from app.models import Lead
    from app.pipeline.emails import build_context, unsubscribe_url
    from app.services import mailer

    form = await request.form()
    lead = db.scalar(select(Lead).where(Lead.primary_person_id.is_not(None)).order_by(Lead.id.desc()).limit(1)) \
        or db.scalar(select(Lead).order_by(Lead.id.desc()).limit(1))
    if lead is not None:
        ctx, sample = build_context(db, lead), f"{lead.company.name} (lead #{lead.id})"
    else:
        ctx = {"company_name": "Example Diagnostic Centre", "person_name": "Rahim Uddin", "first_name": "Rahim",
               "title": "Managing Director", "city": "Dhaka", "industry": "healthcare", "category": "Diagnostic center",
               "sender_name": settings_store.get(db, "sender_name") or "Your Name",
               "sender_company": settings_store.get(db, "sender_company") or "Your Company"}
        sample = "sample data (no leads yet)"
    # show what's missing instead of silently blank text ("I'm  from .")
    ctx["sender_name"] = ctx.get("sender_name") or "[your name - Settings → Email sending]"
    ctx["sender_company"] = ctx.get("sender_company") or "[your company]"
    use_ai = bool(form.get("use_ai_personalisation"))
    ctx["personal_line"] = "[Gemini writes one opening sentence here, using only facts about the company]" if use_ai else ""
    import re as _re

    body = _re.sub(r"\n{3,}", "\n\n", mailer.render(str(form.get("body_tpl", "")), ctx)).strip()
    sig = settings_store.get(db, "sender_signature")
    if sig:
        body += "\n\n" + sig
    body += mailer.footer(unsubscribe_url("<token>"), ctx.get("sender_company", ""))
    unknown = sorted({v for v in mailer.VAR.findall(str(form.get("subject_tpl", "")) + str(form.get("body_tpl", "")))
                      if v not in mailer.TEMPLATE_VARS})
    return JSONResponse({"subject": mailer.render(str(form.get("subject_tpl", "")), ctx).strip(), "body": body,
                         "sample": sample, "unknown_vars": unknown})


@router.post("/templates", dependencies=[Depends(verify_csrf)])
@router.post("/templates/{tid}", dependencies=[Depends(verify_csrf)])
def template_save(request: Request, tid: int | None = None, name: str = Form(...), subject_tpl: str = Form(...),
                  body_tpl: str = Form(...), use_ai_personalisation: str | None = Form(None), ai_instructions: str = Form(""),
                  user: User = Depends(current_user), db: Session = Depends(get_db)):
    t = db.get(EmailTemplate, tid) if tid else EmailTemplate()
    if not tid:
        db.add(t)
    t.name, t.subject_tpl, t.body_tpl = name.strip(), subject_tpl.strip(), body_tpl
    t.use_ai_personalisation, t.ai_instructions = bool(use_ai_personalisation), ai_instructions.strip()
    audit(db, user, "template.save", t.name)
    db.commit()
    flash(request, "Template saved.")
    return RedirectResponse("/templates", 303)
