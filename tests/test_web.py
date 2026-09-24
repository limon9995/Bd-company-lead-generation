import re

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import settings_store
from app.main import app
from app.models import Campaign, EmailMessage, Job, Setting, Suppression, User
from app.security import hash_password


@pytest.fixture
def client(db):
    db.add(User(email="admin@example.com", password_hash=hash_password("correct-horse-1")))
    db.commit()
    return TestClient(app)


def csrf_of(html: str) -> str:
    return re.search(r'name="csrf" content="([^"]+)"', html).group(1) if 'name="csrf"' in html else \
        re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def login(client) -> str:
    tok = csrf_of(client.get("/login").text)
    r = client.post("/login", data={"email": "admin@example.com", "password": "correct-horse-1", "csrf_token": tok},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    return csrf_of(client.get("/").text)


def test_requires_login_and_rejects_bad_password(client):
    assert client.get("/leads", follow_redirects=False).headers["location"] == "/login"
    tok = csrf_of(client.get("/login").text)
    r = client.post("/login", data={"email": "admin@example.com", "password": "nope", "csrf_token": tok})
    assert "Wrong email or password" in r.text


def test_csrf_is_enforced(client):
    login(client)
    r = client.post("/settings/telegram", data={"telegram_chat_id": "1", "csrf_token": "forged"})
    assert r.status_code == 403


def test_all_pages_render(client):
    login(client)
    for path in ["/", "/campaigns", "/campaigns/new", "/leads", "/outbox", "/runs", "/settings", "/industries",
                 "/industries/new", "/templates", "/templates/new", "/suppression", "/users", "/audit", "/health"]:
        r = client.get(path)
        assert r.status_code == 200, path


def test_settings_saved_encrypted_masked_and_kept_when_blank(client, db):
    tok = login(client)
    r = client.post("/settings/gemini", data={"gemini_api_key": "AIzaSECRET1234", "gemini_model": "gemini-2.5-flash",
                                              "gemini_cost_per_call": "0.0005", "csrf_token": tok})
    assert r.status_code == 200
    row = db.get(Setting, "gemini_api_key")
    assert row.is_secret and "AIzaSECRET1234" not in row.value_encrypted
    page = client.get("/settings").text
    assert "AIzaSECRET1234" not in page and "1234" in page  # masked, last 4 only
    client.post("/settings/gemini", data={"gemini_api_key": "", "gemini_model": "gemini-2.5-flash", "csrf_token": tok})
    db.expire_all()
    assert settings_store.get(db, "gemini_api_key") == "AIzaSECRET1234"
    client.post("/settings/gemini", data={"gemini_api_key__clear": "on", "gemini_model": "x", "csrf_token": tok})
    db.expire_all()
    assert settings_store.get(db, "gemini_api_key") == ""


@respx.mock
def test_telegram_detect_chat_id_and_test_button(client, db):
    tok = login(client)
    client.post("/settings/telegram", data={"telegram_bot_token": "123:abc", "telegram_chat_id": "", "csrf_token": tok})
    respx.post("https://api.telegram.org/bot123:abc/getUpdates").mock(return_value=httpx.Response(200, json={
        "ok": True, "result": [{"update_id": 1, "message": {"chat": {"id": 55501, "type": "private", "first_name": "Faisal"}}}]}))
    respx.post("https://api.telegram.org/bot123:abc/getMe").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"username": "leadbot"}}))
    send = respx.post("https://api.telegram.org/bot123:abc/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {}}))
    r = client.post("/settings/telegram/detect", headers={"X-CSRF-Token": tok}).json()
    assert r["ok"] and "55501" in r["message"]
    db.expire_all()
    assert settings_store.get(db, "telegram_chat_id") == "55501"
    r = client.post("/settings/test/telegram", headers={"X-CSRF-Token": tok}).json()
    assert r["ok"] and send.called


def test_campaign_create_and_run_now_queues_job(client, db):
    tok = login(client)
    r = client.post("/campaigns", data={"name": "Edu Chattogram", "industry_slug": "education", "cities": "Chattogram, Dhaka",
                                        "max_companies_per_run": "20", "schedule_cron": "0 9 * * 0", "csrf_token": tok})
    assert r.status_code == 200
    c = db.scalar(select(Campaign))
    assert c.cities == ["Chattogram", "Dhaka"] and c.industry_slug == "education"
    bad = client.post(f"/campaigns/{c.id}", data={"name": "x", "industry_slug": "education", "schedule_cron": "every day",
                                                  "csrf_token": tok})
    assert "Invalid schedule" in bad.text
    client.post(f"/campaigns/{c.id}/run", data={"csrf_token": tok})
    assert db.scalar(select(Job.type)) == "run_campaign"


def test_unsubscribe_link_suppresses(client, db):
    from app.models import Company, Lead
    from tests.test_emails import add_msg

    camp = Campaign(name="c", industry_slug="healthcare", cities=[])
    co = Company(name="A", name_key="a", socials={}, generic_emails=[], extra_phones=[], crawled_pages=[])
    db.add_all([camp, co])
    db.flush()
    le = Lead(campaign_id=camp.id, company_id=co.id)
    db.add(le)
    db.commit()
    m = add_msg(db, le, to="x@a.com")
    assert "Yes, unsubscribe" in client.get(f"/u/{m.unsubscribe_token}").text
    assert "unsubscribed" in client.post(f"/u/{m.unsubscribe_token}").text
    db.expire_all()
    assert db.scalar(select(Suppression.value)) == "x@a.com"
    assert db.get(EmailMessage, m.id).status == "cancelled"
    assert client.get("/u/bogus").status_code == 404
