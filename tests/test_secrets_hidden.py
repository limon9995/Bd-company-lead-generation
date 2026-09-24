"""API keys must never appear in pages, errors, notes, test results or logs."""
import logging

import httpx
import respx
from sqlalchemy import select

from app import redact
from app.models import Campaign, Job, Setting
from app.pipeline import queue
from tests.test_web import client, login  # noqa: F401 - fixture

GEMINI = "AIzaSyD-TESTKEY-1234567890abcdefghijklmn"
TG = "7654321098:AAH-test-telegram-token-abcdefghijk"


def test_redact_known_values_and_patterns(configure):
    configure(gemini_api_key=GEMINI, telegram_bot_token=TG, smtp_password="gmail app pass 16",
              browser_proxy="http://bob:s3cretpw@proxy.example:8080")
    redact.refresh()
    msg = (f"Gemini error for key {GEMINI}; POST https://api.telegram.org/bot{TG}/sendMessage failed; "
           "login gmail app pass 16 refused; proxy http://bob:s3cretpw@proxy.example:8080; ?key=AIzaOTHER123456789012345678901234567")
    out = redact.redact(msg)
    for secret in (GEMINI, TG, "gmail app pass 16", "s3cretpw", "AIzaOTHER"):
        assert secret not in out
    assert "[hidden]" in out


def test_job_errors_and_notes_are_redacted(db, configure):
    configure(gemini_api_key=GEMINI)
    redact.refresh()
    job = queue.enqueue(db, "discover", {})
    db.commit()
    queue.claim(db)
    queue.retry_or_fail(db, db.get(Job, job.id), f"boom with {GEMINI}")
    assert GEMINI not in db.get(Job, job.id).last_error
    from app.models import Run
    from app.pipeline.stages import add_note

    run = Run(campaign_id=None, notes=[])
    add_note(run, f"key {GEMINI} rejected")
    assert GEMINI not in run.notes[0]


def test_log_filter_hides_secrets(configure, caplog):
    configure(telegram_bot_token=TG)
    redact.refresh()
    logger = logging.getLogger("leak-test")
    handler = logging.StreamHandler()
    handler.addFilter(redact.RedactingFilter())
    records = []
    handler.emit = lambda r: records.append(r.getMessage())
    logger.addHandler(handler)
    logger.warning("calling https://api.telegram.org/bot%s/getMe", TG)
    assert records and TG not in records[0]


@respx.mock
def test_settings_page_and_test_result_never_show_keys(client, db):  # noqa: F811
    tok = login(client)
    client.post("/settings/telegram", data={"telegram_bot_token": TG, "telegram_chat_id": "1", "csrf_token": tok})
    client.post("/settings/gemini", data={"gemini_api_key": GEMINI, "gemini_model": "m", "csrf_token": tok})
    page = client.get("/settings").text
    assert TG not in page and GEMINI not in page
    # a provider error that echoes the URL (with the token) must come back redacted
    respx.post(f"https://api.telegram.org/bot{TG}/getMe").mock(side_effect=httpx.ConnectError(f"cannot reach https://api.telegram.org/bot{TG}/getMe"))
    res = client.post("/settings/test/telegram", headers={"X-CSRF-Token": tok}).json()
    assert not res["ok"] and TG not in res["message"]
    assert TG not in client.get("/settings").text and TG not in client.get("/setup").text
    # stored encrypted, not in plain text
    assert all(TG not in (row.value_encrypted or "") for row in db.scalars(select(Setting)))
    assert TG not in client.get("/audit").text
