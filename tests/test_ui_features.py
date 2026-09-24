import httpx
import respx
from sqlalchemy import select

from app.models import Campaign, Company, EmailMessage, EmailTemplate, Job, Lead, Person, Run
from app.pipeline.stages import start_run
from app.worker.runner import drain
from tests.test_web import client, login  # noqa: F401 - fixture


def _lead(db, name="Acme", email="md@acme.com.bd", confidence=80):
    camp = db.scalar(select(Campaign)) or Campaign(name="c", industry_slug="healthcare", cities=["Dhaka"])
    db.add(camp)
    co = Company(name=name, name_key=name.lower(), socials={}, generic_emails=[], extra_phones=[], crawled_pages=[])
    db.add(co)
    db.flush()
    p = Person(company_id=co.id, full_name="Rahim Uddin", name_key=f"rahim {name}", title="MD", confidence=confidence, email=email,
               email_status="found")
    db.add(p)
    db.flush()
    le = Lead(campaign_id=camp.id, company_id=co.id, primary_person_id=p.id)
    db.add(le)
    db.commit()
    return le


def test_new_pages_render(client):  # noqa: F811
    login(client)
    for path in ["/setup", "/templates/1/edit"]:
        assert client.get(path).status_code == 200
    home = client.get("/").text
    assert "Finish setup" in home and "New leads per day" in home


def test_dashboard_chart_shows_todays_leads(client, db):  # noqa: F811
    login(client)
    _lead(db)
    _lead(db, name="Beta", email="")
    html = client.get("/").text
    assert "2 total" in html and 'class="bar-mark"' in html and "2 leads · 2 with decision maker" in html


@respx.mock
def test_setup_step_turns_done_after_successful_test(client, db):  # noqa: F811
    tok = login(client)
    client.post("/settings/telegram", data={"telegram_bot_token": "1:x", "telegram_chat_id": "5", "csrf_token": tok})
    page = client.get("/setup").text
    assert "saved, not tested" in page
    respx.post("https://api.telegram.org/bot1:x/getMe").mock(return_value=httpx.Response(200, json={"ok": True, "result": {"username": "b"}}))
    respx.post("https://api.telegram.org/bot1:x/sendMessage").mock(return_value=httpx.Response(200, json={"ok": True, "result": {}}))
    assert client.post("/settings/test/telegram", headers={"X-CSRF-Token": tok}).json()["ok"]
    assert "Test passed" in client.get("/settings").text
    page = client.get("/setup").text
    assert page.count('class="done"') >= 1 and "saved, not tested" not in page


def test_run_status_json_and_cancel(client, db):  # noqa: F811
    tok = login(client)
    camp = Campaign(name="c", industry_slug="healthcare", cities=["Dhaka"])
    db.add(camp)
    db.commit()
    run = start_run(db, camp)
    st = client.get(f"/runs/{run.id}/status").json()
    assert st["status"] == "running" and st["total"] == 1 and st["percent"] == 0
    assert 'data-run-status' in client.get(f"/runs/{run.id}").text
    client.post(f"/runs/{run.id}/cancel", data={"csrf_token": tok})
    db.expire_all()
    assert db.get(Run, run.id).status == "cancelled"
    assert db.scalar(select(Job.status).where(Job.run_id == run.id)) == "skipped"
    drain()  # nothing left to do, and the cancelled status must survive
    db.expire_all()
    assert db.get(Run, run.id).status == "cancelled"


def test_bulk_status_drafts_and_export(client, db):  # noqa: F811
    tok = login(client)
    a, b = _lead(db), _lead(db, name="Beta", email="")
    tpl = db.scalar(select(EmailTemplate))
    r = client.post("/leads/bulk", data={"csrf_token": tok, "ids": [a.id, b.id], "action": "draft", "template_id": tpl.id})
    assert r.status_code == 200
    msgs = db.scalars(select(EmailMessage)).all()
    assert [m.to_email for m in msgs] == ["md@acme.com.bd"]  # Beta has no address → skipped
    client.post("/leads/bulk", data={"csrf_token": tok, "ids": [a.id], "action": "status", "crm_status": "do_not_contact"})
    db.expire_all()
    assert db.get(Lead, a.id).crm_status == "do_not_contact" and db.scalar(select(EmailMessage.status)) == "cancelled"
    csv = client.post("/leads/bulk", data={"csrf_token": tok, "ids": [b.id], "action": "export"})
    assert csv.headers["content-type"].startswith("text/csv") and "Beta" in csv.text and "Acme" not in csv.text
    empty = client.post("/leads/bulk", data={"csrf_token": tok, "action": "export"})
    assert "Select at least one lead" in empty.text


def test_template_preview_uses_latest_lead_and_flags_unknown_vars(client, db):  # noqa: F811
    tok = login(client)
    res = client.post("/templates/preview", headers={"X-CSRF-Token": tok},
                      data={"subject_tpl": "Hi {{company_name}}", "body_tpl": "Dear {{first_name}}, {{nonsense}}"}).json()
    assert res["subject"] == "Hi Example Diagnostic Centre" and "sample data" in res["sample"]
    assert res["unknown_vars"] == ["nonsense"] and "/u/<token>" in res["body"]
    _lead(db)
    res = client.post("/templates/preview", headers={"X-CSRF-Token": tok},
                      data={"subject_tpl": "Hi {{company_name}}", "body_tpl": "Dear {{first_name}}", "use_ai_personalisation": "on"}).json()
    assert res["subject"] == "Hi Acme" and res["body"].startswith("Dear Rahim")


def test_campaign_source_fields_and_resume_paused_source(client, db):  # noqa: F811
    from app.services import browser

    tok = login(client)
    client.post("/campaigns", data={"name": "Dir", "industry_slug": "healthcare", "cities": "Dhaka", "csrf_token": tok,
                                    "discovery_source": "directory", "directory_urls": "https://dir.example/a\nnot-a-url\n",
                                    "directory_max_pages": "3"})
    c = db.scalar(select(Campaign))
    assert c.discovery_source == "directory" and c.directory_urls == ["https://dir.example/a"] and c.directory_max_pages == 3
    assert "Directory · browser" in client.get("/campaigns").text
    browser.set_blocked(db, "google", 6)
    db.commit()
    assert "is paused until" in client.get("/settings").text
    client.post("/settings/browser/resume", data={"source": "google", "csrf_token": tok})
    db.expire_all()
    assert browser.blocked_until(db, "google") is None
