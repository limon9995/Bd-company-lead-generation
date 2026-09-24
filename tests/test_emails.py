from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.models import Campaign, Company, EmailMessage, Lead, Person, Suppression
from app.pipeline.emails import Dispatcher, is_suppressed
from app.security import new_token
from app.services.errors import ProviderError

DHAKA = ZoneInfo("Asia/Dhaka")
SUNDAY_11 = datetime(2026, 9, 27, 11, 0, tzinfo=DHAKA)  # a Sunday


@pytest.fixture
def lead(db):
    camp = Campaign(name="c", industry_slug="healthcare", cities=["Dhaka"])
    co = Company(name="Acme", name_key="acme", place_id="p1", socials={}, generic_emails=[], extra_phones=[], crawled_pages=[])
    db.add_all([camp, co])
    db.flush()
    p = Person(company_id=co.id, full_name="Rahim Uddin", name_key="rahim uddin", title="MD", confidence=80, email="rahim@acme.com.bd")
    db.add(p)
    db.flush()
    le = Lead(campaign_id=camp.id, company_id=co.id, primary_person_id=p.id)
    db.add(le)
    db.commit()
    return le


def add_msg(db, lead, to="rahim@acme.com.bd", status="approved"):
    m = EmailMessage(lead_id=lead.id, person_id=lead.primary_person_id, to_email=to, subject="Hi", body="Body",
                     status=status, unsubscribe_token=new_token())
    db.add(m)
    db.commit()
    return m


class Outbox:
    def __init__(self, fail: str | None = None):
        self.sent, self.fail = [], fail

    def __call__(self, s, to, subject, body, unsub):
        if self.fail:
            raise ProviderError(self.fail)
        self.sent.append((to, unsub))


def test_sends_inside_window_and_marks_lead_contacted(db, lead, configure):
    m = add_msg(db, lead)
    out = Outbox()
    assert Dispatcher().tick(db, SUNDAY_11, sender=out).startswith("sent")
    db.refresh(m)
    db.refresh(lead)
    assert m.status == "sent" and lead.crm_status == "contacted"
    assert out.sent[0][1].endswith(f"/u/{m.unsubscribe_token}")


def test_window_days_cap_and_gap(db, lead, configure):
    add_msg(db, lead)
    d = Dispatcher()
    assert d.tick(db, SUNDAY_11.replace(hour=20), sender=Outbox()) == "outside window"
    friday = SUNDAY_11 - timedelta(days=2)
    assert d.tick(db, friday, sender=Outbox()) == "outside window"
    configure(daily_email_cap=1)
    assert d.tick(db, SUNDAY_11, sender=Outbox()).startswith("sent")
    add_msg(db, lead, to="other@acme.com.bd")
    assert d.tick(db, SUNDAY_11 + timedelta(seconds=5), sender=Outbox()) == "waiting gap"
    assert d.tick(db, SUNDAY_11 + timedelta(minutes=10), sender=Outbox()) == "daily cap reached"


def test_suppressed_and_cooldown_are_cancelled(db, lead):
    db.add(Suppression(value="@acme.com.bd", reason="manual"))
    db.commit()
    m = add_msg(db, lead)
    assert Dispatcher().tick(db, SUNDAY_11, sender=Outbox()) == "cancelled (suppressed)"
    db.refresh(m)
    assert m.status == "cancelled"
    db.execute(Suppression.__table__.delete())
    first = add_msg(db, lead, to="a@x.com", status="sent")
    first.sent_at = SUNDAY_11 - timedelta(days=3)
    db.commit()
    add_msg(db, lead, to="b@x.com")
    assert Dispatcher().tick(db, SUNDAY_11, sender=Outbox()) == "cancelled (cooldown)"


def test_bounce_adds_to_suppression(db, lead):
    m = add_msg(db, lead)
    assert Dispatcher().tick(db, SUNDAY_11, sender=Outbox(fail="recipient refused: 550")).startswith("failed")
    db.refresh(m)
    assert m.status == "failed" and is_suppressed(db, "rahim@acme.com.bd")


def test_nothing_to_send(db, lead):
    add_msg(db, lead, status="draft")
    assert Dispatcher().tick(db, SUNDAY_11, sender=Outbox()) == "nothing to send"
    assert datetime.now(timezone.utc)  # sanity
    assert db.scalar(select(EmailMessage.status)) == "draft"
