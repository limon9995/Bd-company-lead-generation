"""Pipeline stages. Each stage is one job type; stages are idempotent so retries/restarts are safe."""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import settings_store
from app.models import Campaign, Company, EmailTemplate, IndustryPreset, Job, Lead, Person, Run
from app.pipeline import providers
from app.pipeline.queue import enqueue, now
from app.services import crawler, places
from app.services.email_finder import choose_email
from app.services.errors import ProviderError, SkipStage, SourceBlocked
from app.services.extractor import Source, extract_candidates, merge_and_score, search_queries
from app.services.normalize import name_key, normalize_bd_phone, normalize_domain, person_key, social_kind
from app.services.scoring import bucket
from app.services.usage import check_budget, record_call

log = logging.getLogger(__name__)
MAX_PLACES_PAGES = 3  # Places Text Search returns at most 60 results (3 x 20)


def add_note(run: Run | None, text: str) -> None:
    if run is not None and text not in (run.notes or []):
        run.notes = [*(run.notes or []), text]


def bump(run: Run, key: str, n: int = 1) -> None:
    c = dict(run.counters or {})
    c[key] = c.get(key, 0) + n
    run.counters = c


def campaign_targets(db: Session, campaign: Campaign) -> list[str]:
    if campaign.target_titles:
        return campaign.target_titles
    preset = db.scalar(select(IndustryPreset).where(IndustryPreset.slug == campaign.industry_slug))
    return preset.default_titles if preset else []


def run_progress(db: Session, run: Run) -> dict:
    """Live progress for the run page (polled as JSON while a run is active)."""
    rows = db.execute(select(Job.status, func.count(Job.id)).where(Job.run_id == run.id).group_by(Job.status)).all()
    by_status = {s: n for s, n in rows}
    total = sum(by_status.values())
    finished = sum(n for s, n in by_status.items() if s in ("done", "failed", "skipped"))
    return {"id": run.id, "status": run.status, "counters": run.counters or {}, "notes": run.notes or [],
            "jobs": by_status, "total": total, "finished": finished,
            "percent": 100 if run.status != "running" else (round(100 * finished / total) if total else 0)}


def cancel_run(db: Session, run: Run) -> int:
    """Stop a run: queued jobs are skipped; jobs already running finish their current step."""
    n = 0
    for j in db.scalars(select(Job).where(Job.run_id == run.id, Job.status == "queued")):
        j.status, j.last_error = "skipped", "run cancelled"
        n += 1
    run.status = "cancelled"
    run.finished_at = now()
    add_note(run, "Run cancelled by an admin.")
    return n


def start_run(db: Session, campaign: Campaign, trigger: str = "manual") -> Run:
    run = Run(campaign_id=campaign.id, trigger=trigger, status="running", counters={}, notes=[], plan=[])
    db.add(run)
    db.flush()
    enqueue(db, "run_campaign", {"run_id": run.id}, run_id=run.id, dedupe_key=f"run:{run.id}")
    db.commit()
    return run


# ---------------------------------------------------------------- run_campaign
def run_campaign(db: Session, job: Job) -> None:
    run = db.get(Run, job.payload["run_id"])
    campaign = db.get(Campaign, run.campaign_id)
    source = campaign.discovery_source or "places_api"
    if source == "directory":
        urls = [u for u in (campaign.directory_urls or []) if u.startswith(("http://", "https://"))]
        if not urls:
            _fail_start(run, "No directory URLs set on this campaign (Campaigns → Edit → Directory URLs).")
        if providers.get_llm(db) is None:
            _fail_start(run, "Directory scraping needs the Gemini API key (Settings → Gemini) to read the pages.")
        run.plan = urls
        for i in range(len(urls)):
            enqueue(db, "discover_directory", {"run_id": run.id, "idx": i}, run_id=run.id, dedupe_key=f"dir:{run.id}:{i}")
        return
    if source == "places_api" and not providers.places_key(db):
        _fail_start(run, "Google Places API key is not set (Settings → Google Places API), "
                         "or switch the campaign to 'Google Maps (browser)'.")
    preset = db.scalar(select(IndustryPreset).where(IndustryPreset.slug == campaign.industry_slug))
    queries = list(preset.places_queries if preset else []) + list(campaign.extra_keywords or [])
    if not queries:
        queries = [campaign.industry_slug.replace("_", " ")]
    cities = campaign.cities or ["Dhaka"]
    run.plan = [f"{q} in {city}, Bangladesh" for city in cities for q in queries]
    job_type = "discover_maps" if source == "maps_browser" else "discover"
    enqueue(db, job_type, {"run_id": run.id, "idx": 0}, run_id=run.id, dedupe_key=f"{job_type}:{run.id}:0")


def _fail_start(run: Run, note: str) -> None:
    run.status = "failed"
    run.finished_at = now()
    add_note(run, note)
    raise SkipStage(note)


# ---------------------------------------------------------------- discover
def upsert_company(db: Session, p: places.Place, industry: str, city: str, campaign_id: int,
                   source: str = "places_api", source_url: str = "") -> tuple[Company, bool]:
    domain = normalize_domain(p.website)
    company = db.scalar(select(Company).where(Company.place_id == p.place_id)) if p.place_id else None
    if company is None and domain:
        company = db.scalar(select(Company).where(Company.domain == domain))
    phone = normalize_bd_phone(p.phone) or p.phone
    if company is None and phone:
        company = db.scalar(select(Company).where(Company.name_key == name_key(p.name), Company.phone == phone))
    if company is None and not phone and not domain and city:
        company = db.scalar(select(Company).where(Company.name_key == name_key(p.name), Company.city == city))
    created = company is None
    if created:
        company = Company(place_id=p.place_id or None, domain=domain, name=p.name, name_key=name_key(p.name),
                          industry_slug=industry, first_seen_campaign_id=campaign_id, socials={}, generic_emails=[],
                          extra_phones=[], crawled_pages=[], source=source, source_url=source_url or p.maps_url or "")
        db.add(company)
    company.category = p.category or company.category
    company.address = p.address or company.address
    company.city = company.city or city
    company.phone = phone or company.phone
    company.rating = p.rating if p.rating is not None else company.rating
    company.reviews_count = p.reviews_count if p.reviews_count is not None else company.reviews_count
    company.google_maps_url = p.maps_url or company.google_maps_url
    if p.website:
        kind = social_kind(p.website)
        if kind:
            company.socials = {**(company.socials or {}), kind: p.website}
        else:
            company.website = p.website
    db.flush()
    return company, created


def register_place(db: Session, run: Run, campaign: Campaign, p: places.Place, city: str, source: str,
                   source_url: str = "", extra_emails: list[str] | None = None) -> bool:
    """Upsert the company, make it a lead of this campaign and queue enrichment. True if a new lead was added."""
    if p.business_status == "CLOSED_PERMANENTLY" or not p.name:
        return False
    company, created = upsert_company(db, p, campaign.industry_slug, city, campaign.id, source, source_url)
    if extra_emails:
        company.generic_emails = list(dict.fromkeys((company.generic_emails or []) + extra_emails))[:20]
    lead = db.scalar(select(Lead).where(Lead.campaign_id == campaign.id, Lead.company_id == company.id))
    if lead is not None:
        return False  # already a lead of this campaign (earlier run or earlier query)
    lead = Lead(campaign_id=campaign.id, company_id=company.id, run_id=run.id, crm_status="new")
    db.add(lead)
    db.flush()
    bump(run, "companies")
    bump(run, "new_companies" if created else "existing_companies")
    if needs_enrichment(db, company):
        enqueue(db, "crawl_company", {"run_id": run.id, "company_id": company.id, "lead_id": lead.id},
                run_id=run.id, dedupe_key=f"crawl:{run.id}:{company.id}")
    else:
        lead.primary_person_id = best_person_id(company)
    return True


def needs_enrichment(db: Session, company: Company) -> bool:
    if company.enriched_at is None:
        return True
    days = settings_store.get(db, "recrawl_after_days")
    enriched = company.enriched_at if company.enriched_at.tzinfo else company.enriched_at.replace(tzinfo=timezone.utc)
    return enriched < now() - timedelta(days=days)


def discover(db: Session, job: Job) -> None:
    run = db.get(Run, job.payload["run_id"])
    campaign = db.get(Campaign, run.campaign_id)
    idx = job.payload["idx"]
    if idx >= len(run.plan or []):
        return
    query = run.plan[idx]
    city = query.split(" in ")[-1].split(",")[0].strip()
    key = providers.places_key(db)
    if not key:
        raise SkipStage("Places API key not set")
    limit = campaign.max_companies_per_run
    token = job.payload.get("page_token")
    pages_done = job.payload.get("pages_done", 0)
    while pages_done < MAX_PLACES_PAGES and (run.counters or {}).get("companies", 0) < limit:
        check_budget(db, "places")
        results, token = places.text_search(key, query, region=settings_store.get(db, "places_region_code"),
                                            language=settings_store.get(db, "places_language"), page_token=token)
        record_call(db, "places")
        for p in results:
            if (run.counters or {}).get("companies", 0) >= limit:
                break
            register_place(db, run, campaign, p, city, "places_api")
        pages_done += 1
        # persist progress so a retry doesn't pay for the same page twice
        job.payload = {**job.payload, "page_token": token, "pages_done": pages_done}
        db.commit()
        if not token:
            break
    if (run.counters or {}).get("companies", 0) < limit and idx + 1 < len(run.plan):
        enqueue(db, "discover", {"run_id": run.id, "idx": idx + 1}, run_id=run.id, dedupe_key=f"discover:{run.id}:{idx + 1}")


def discover_maps(db: Session, job: Job) -> None:
    """Google Maps in a headless browser (no Places API key). One browser job at a time."""
    from app.services import browser, maps_browser

    run = db.get(Run, job.payload["run_id"])
    campaign = db.get(Campaign, run.campaign_id)
    idx = job.payload["idx"]
    if idx >= len(run.plan or []):
        return
    query = run.plan[idx]
    city = query.split(" in ")[-1].split(",")[0].strip()
    limit = campaign.max_companies_per_run
    browser.ensure_not_paused(db, "google")
    done = set(job.payload.get("done", []))
    with browser.source_lock("google"), browser.session_for(db, "google") as session:
        links = maps_browser.collect_links(session, query, settings_store.get(db, "maps_max_results"))
        for link in links:
            if (run.counters or {}).get("companies", 0) >= limit:
                break
            key = maps_browser.place_key_from_url(link)
            if key in done:
                continue
            known = db.scalar(select(Company).where(Company.place_id == key)) if key else None
            if known is not None and not needs_enrichment(db, known):
                # already have this place: no need to open its page again
                p = places.Place(key, known.name, known.address, known.phone, known.website, known.rating,
                                 known.reviews_count, known.google_maps_url, known.category, "OPERATIONAL")
            else:
                check_budget(db, "maps_browser")
                p = maps_browser.read_place(session, link)
                record_call(db, "maps_browser")
            register_place(db, run, campaign, p, city, "maps_browser", link)
            done.add(key)
            job.payload = {**job.payload, "done": sorted(done)}
            db.commit()
    if (run.counters or {}).get("companies", 0) < limit and idx + 1 < len(run.plan):
        enqueue(db, "discover_maps", {"run_id": run.id, "idx": idx + 1}, run_id=run.id,
                dedupe_key=f"discover_maps:{run.id}:{idx + 1}")


def discover_directory(db: Session, job: Job) -> None:
    """Any public directory/listing URL: browser renders it, Gemini lists the companies, follow 'next'."""
    from urllib.parse import urlparse

    from app.services import browser, directory

    run = db.get(Run, job.payload["run_id"])
    campaign = db.get(Campaign, run.campaign_id)
    url = job.payload.get("next_url") or run.plan[job.payload["idx"]]
    pages_done = job.payload.get("pages_done", 0)
    llm = providers.get_llm(db)
    if llm is None:
        raise SkipStage("Gemini API key not set")
    host = urlparse(url).hostname or "directory"
    source_name = f"dir:{host}"
    browser.ensure_not_paused(db, source_name)
    limit = campaign.max_companies_per_run
    default_city = (campaign.cities or [""])[0]
    with browser.source_lock(source_name), browser.session_for(db, source_name) as session:
        while url and pages_done < campaign.directory_max_pages and (run.counters or {}).get("companies", 0) < limit:
            session.goto(url, wait_until="load")
            html = session.html()
            check_budget(db, "gemini")
            entries = directory.extract_entries(llm, url, html)
            record_call(db, "gemini")
            for e in entries:
                if (run.counters or {}).get("companies", 0) >= limit:
                    break
                p = places.Place("", e.name, e.address, e.phone, e.website, None, None, "", e.category, "OPERATIONAL")
                register_place(db, run, campaign, p, e.city or default_city, "directory", e.detail_url or url,
                               extra_emails=[e.email.lower()] if "@" in e.email else None)
            bump(run, "directory_pages")
            pages_done += 1
            nxt = directory.find_next_url(html, url)
            url = nxt if nxt and nxt != url else None
            job.payload = {**job.payload, "next_url": url, "pages_done": pages_done}
            db.commit()


def best_person_id(company: Company) -> int | None:
    from app.services.scoring import title_rank

    people = sorted(company.people, key=lambda p: (title_rank(p.title), -p.confidence))
    return people[0].id if people else None


# ---------------------------------------------------------------- crawl
def crawl_company(db: Session, job: Job) -> None:
    run = db.get(Run, job.payload["run_id"])
    company = db.get(Company, job.payload["company_id"])
    if company.website:
        res = crawler.crawl(
            company.website,
            max_pages=settings_store.get(db, "crawl_max_pages"),
            delay=settings_store.get(db, "crawl_delay_seconds"),
            timeout=settings_store.get(db, "crawl_timeout_seconds"),
            respect_robots=settings_store.get(db, "crawl_respect_robots"),
            use_browser=settings_store.get(db, "crawl_use_browser"),
        )
        company.crawled_pages = [{"url": p.url, "title": p.title, "text": p.text} for p in res.pages]
        company.crawled_text = res.text[:60000]
        company.generic_emails = list(dict.fromkeys((company.generic_emails or []) + res.emails))[:20]
        company.extra_phones = list(dict.fromkeys((company.extra_phones or []) + res.phones))[:10]
        company.socials = {**res.socials, **(company.socials or {})}
        if res.error:
            add_note(run, f"Some websites could not be crawled (e.g. {company.website}: {res.error})")
        bump(run, "crawled" if res.pages else "crawl_failed")
    else:
        bump(run, "no_website")
        fb = (company.socials or {}).get("facebook")
        if fb and settings_store.get(db, "facebook_pages"):
            try:
                read_facebook(db, run, company, fb)
            except SourceBlocked:
                raise
            except Exception as exc:  # noqa: BLE001 - Facebook is a bonus source; never fail the company for it
                log.info("facebook read failed for %s: %s", fb, exc)
                bump(run, "facebook_failed")
    company.enrichment_status = "crawled"
    enqueue(db, "find_decision_maker", dict(job.payload), run_id=run.id,
            dedupe_key=f"dm:{run.id}:{company.id}")


def read_facebook(db: Session, run: Run, company: Company, fb_url: str) -> None:
    from app.services import browser, facebook_page

    if browser.blocked_until(db, "facebook"):
        return
    with browser.source_lock("facebook"), browser.session_for(db, "facebook") as session:
        res = facebook_page.read_page(session, fb_url)
    if res.login_wall or not res.text:
        bump(run, "facebook_login_wall")
        add_note(run, "Facebook showed a login wall for some pages - their details could not be read without logging in "
                      "(we never log in).")
        return
    bump(run, "facebook_read")
    company.crawled_pages = [*(company.crawled_pages or []), {"url": res.url, "title": "Facebook page", "text": res.text}]
    company.generic_emails = list(dict.fromkeys((company.generic_emails or []) + res.emails))[:20]
    company.extra_phones = list(dict.fromkeys((company.extra_phones or []) + res.phones))[:10]


# ---------------------------------------------------------------- decision maker
def _save_people(db: Session, company: Company, cands) -> list[Person]:
    saved = []
    existing = {p.name_key: p for p in company.people}
    for c in cands:
        k = person_key(c.name)
        p = existing.get(k)
        if p is None:
            p = Person(company_id=company.id, full_name=c.name, name_key=k)
            db.add(p)
            company.people.append(p)
            existing[k] = p
        p.title = c.title
        p.seniority_rank = c.rank
        p.source_url = c.source_url
        p.evidence = c.evidence[:1000]
        p.linkedin_url = c.linkedin_url or p.linkedin_url
        p.sources = c.sources
        p.confidence = c.confidence
        p.confidence_breakdown = c.breakdown
        saved.append(p)
    db.flush()
    return saved


def find_decision_maker(db: Session, job: Job) -> None:
    run = db.get(Run, job.payload["run_id"])
    company = db.get(Company, job.payload["company_id"])
    lead = db.get(Lead, job.payload["lead_id"])
    campaign = db.get(Campaign, run.campaign_id)
    targets = campaign_targets(db, campaign)
    llm = providers.get_llm(db)
    cands = []
    if llm is None:
        add_note(run, "Gemini API key is not set - decision makers were not extracted (Settings → Gemini).")
    else:
        web_sources = [Source("website", p["url"], p["text"]) for p in (company.crawled_pages or []) if p.get("text")]
        if web_sources:
            check_budget(db, "gemini")
            cands = extract_candidates(llm, company.name, targets, web_sources)
            record_call(db, "gemini")
        scored = merge_and_score(cands, targets)
        if not any(c.confidence >= 55 and c.rank <= 2 for c in scored):
            search = providers.get_search(db)
            if search is None:
                add_note(run, "Search API not configured - only company websites were used for decision makers.")
            else:
                search_sources = []
                usage_key = getattr(search, "usage_key", "search")
                for q in search_queries(company.name, company.city or "", targets):
                    check_budget(db, usage_key)
                    try:
                        results = search(q, 10)
                    finally:
                        record_call(db, usage_key)
                    search_sources += [Source("search", r.url, f"{r.title} — {r.snippet}") for r in results if r.url]
                if search_sources:
                    check_budget(db, "gemini")
                    cands += extract_candidates(llm, company.name, targets, search_sources)
                    record_call(db, "gemini")
    scored = merge_and_score(cands, targets)
    people = _save_people(db, company, scored)
    primary = people[0] if people else None
    email, status = choose_email(primary.full_name if primary else "", company.generic_emails or [], company.domain)
    if primary is not None:
        primary.email, primary.email_status = email, status
        lead.primary_person_id = primary.id
        bump(run, "dm_found")
        bump(run, f"dm_{bucket(primary.confidence)}")
    if email:
        bump(run, "with_email")
    company.enrichment_status = "dm_done"
    company.enriched_at = now()


# ---------------------------------------------------------------- finalize
def lead_row(lead: Lead) -> dict:
    c, p, camp = lead.company, lead.primary_person, lead.campaign
    return {
        "lead_id": lead.id, "campaign": camp.name if camp else "", "industry": c.industry_slug, "company": c.name,
        "category": c.category, "city": c.city, "address": c.address, "phone": c.phone, "website": c.website,
        "company_emails": ", ".join(c.generic_emails or []), "facebook": (c.socials or {}).get("facebook", ""),
        "linkedin_company": (c.socials or {}).get("linkedin", ""),
        "decision_maker": p.full_name if p else "", "title": p.title if p else "", "dm_email": p.email if p else "",
        "email_status": p.email_status if p else "", "dm_linkedin": p.linkedin_url if p else "",
        "confidence": p.confidence if p else "", "confidence_level": bucket(p.confidence) if p else "",
        "source_url": p.source_url if p else "", "google_maps": c.google_maps_url, "rating": c.rating or "",
        "crm_status": lead.crm_status, "notes": lead.notes, "updated_at": lead.updated_at.isoformat() if lead.updated_at else "",
    }


def sync_leads_to_sheet(db: Session, leads: list[Lead]) -> str:
    from app.services import sheets

    sa = settings_store.get(db, "sheets_service_account_json")
    sid = settings_store.get(db, "sheets_spreadsheet_id")
    if not (sa and sid):
        return "Google Sheets not configured - skipped sheet sync."
    n = sheets.upsert_rows(sa, sid, [lead_row(le) for le in leads])
    ts = now()
    for le in leads:
        le.sheet_synced_at = ts
    return f"Synced {n} leads to Google Sheet."


def finalize_run(db: Session, job: Job) -> None:
    from app.pipeline.emails import create_drafts_for_leads
    from app.services.notifier import esc, notify

    run = db.get(Run, job.payload["run_id"])
    campaign = db.get(Campaign, run.campaign_id)
    if run.status == "cancelled":
        run.finished_at = run.finished_at or now()
        return
    if not run.plan:  # run_campaign never got going (e.g. missing Places key)
        run.status = "failed"
        run.finished_at = run.finished_at or now()
        db.commit()
        notify(db, f"<b>❌ Run #{run.id} ({esc(campaign.name)}) could not start</b>\n" +
               "\n".join(f"ℹ️ {esc(n)}" for n in run.notes or []))
        return
    leads = list(db.scalars(select(Lead).where(Lead.run_id == run.id)))
    failed = db.scalar(select(func.count(Job.id)).where(Job.run_id == run.id, Job.status == "failed")) or 0
    c = dict(run.counters or {})
    c["failed_jobs"] = failed
    run.counters = c
    try:
        add_note(run, sync_leads_to_sheet(db, leads))
    except ProviderError as exc:
        add_note(run, f"Sheet sync failed: {exc}")
    if campaign.email_template_id:
        tpl = db.get(EmailTemplate, campaign.email_template_id)
        made = create_drafts_for_leads(db, campaign, tpl, leads)
        c = dict(run.counters)
        c["email_drafts"] = made
        run.counters = c
    run.status = "done"
    run.finished_at = now()
    db.commit()

    total = len(leads)
    with_dm = sum(1 for le in leads if le.primary_person_id)
    lines = [
        f"<b>✅ Run #{run.id} finished — {esc(campaign.name)}</b>",
        f"Industry: {esc(campaign.industry_slug)} · Cities: {esc(', '.join(campaign.cities or []))}",
        f"Companies: {total} (new {c.get('new_companies', 0)})",
        f"Decision makers: {with_dm}/{total} · high {c.get('dm_high', 0)} · medium {c.get('dm_medium', 0)} · low {c.get('dm_low', 0)}",
        f"With email: {c.get('with_email', 0)} · Drafts: {c.get('email_drafts', 0)} · Failed jobs: {failed}",
    ]
    for n in run.notes or []:
        lines.append(f"ℹ️ {esc(n)}")
    notify(db, "\n".join(lines))
    if settings_store.get(db, "telegram_lead_cards"):
        hot = [le for le in leads if le.primary_person and le.primary_person.confidence >= 70 and not le.notified][:10]
        for le in hot:
            p, co = le.primary_person, le.company
            notify(db, "\n".join(filter(None, [
                f"🔥 <b>{esc(co.name)}</b> ({esc(co.category)}, {esc(co.city)})",
                f"👤 {esc(p.full_name)} — {esc(p.title)} · confidence {p.confidence}",
                f"✉️ {esc(p.email)} ({esc(p.email_status)})" if p.email else "",
                f"📞 {esc(co.phone)}" if co.phone else "",
                f"🌐 {esc(co.website)}" if co.website else "",
                f"🔗 source: {esc(p.source_url)}",
            ])))
            le.notified = True


HANDLERS = {
    "run_campaign": run_campaign,
    "discover": discover,
    "discover_maps": discover_maps,
    "discover_directory": discover_directory,
    "crawl_company": crawl_company,
    "find_decision_maker": find_decision_maker,
    "finalize_run": finalize_run,
}
