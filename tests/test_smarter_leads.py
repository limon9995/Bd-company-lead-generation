"""Duplicate people, choosing the contact, company email formats, domain checks and admin feedback."""
from sqlalchemy import select

from app.models import Campaign, Company, EmailMessage, EmailTemplate, Lead, Person
from app.pipeline.stages import _save_people, primary_candidates, set_primary
from app.services.email_finder import choose_email, learn_format
from app.services.extractor import Candidate, merge_and_score
from app.services.normalize import person_key
from app.services.scoring import title_rank
from tests.test_web import client, login  # noqa: F401 - fixture


# ---------------------------------------------------------------- 1. right person
def test_honorifics_do_not_split_one_person():
    assert person_key("Barrister Shameem Haider Patwary") == person_key("Shameem Haider Patwary")
    assert person_key("Brig Gen Prof. Dr. Engr Md Lutfor Rahman (Retd)") == person_key("Lutfor Rahman")
    assert person_key("প্রফেসর মোঃ রহিম উদ্দিন") == person_key("রহিম উদ্দিন")
    assert person_key("Rahim Uddin") != person_key("Karim Uddin")


def test_functional_directors_rank_below_board_directors():
    assert title_rank("Director of Finance") == 3
    assert title_rank("Director for Admissions") == 3
    assert title_rank("Director") == 2 and title_rank("Executive Director") == 2
    assert title_rank("Managing Director of Acme") == 1


def test_equal_people_prefer_the_leadership_page():
    news = Candidate("Karim Ahmed", "Director", "https://x.com/news/1", "", on_website=True,
                     sources=[{"kind": "website", "url": "https://x.com/news/1"}])
    board = Candidate("Rahim Uddin", "Director", "https://x.com/board-of-directors", "", on_website=True,
                      sources=[{"kind": "website", "url": "https://x.com/board-of-directors"}])
    assert [c.name for c in merge_and_score([news, board], [])] == ["Rahim Uddin", "Karim Ahmed"]


def _company(db, **kw):
    c = Company(name="Acme Ltd", domain="acme.com.bd", website="https://acme.com.bd", industry_slug="tech",
                city="Dhaka", generic_emails=kw.pop("emails", []), **kw)
    db.add(c)
    db.flush()
    return c


def test_existing_duplicates_are_merged_and_leads_move_to_the_kept_person(db):
    comp = _company(db)
    camp = Campaign(name="c", industry_slug="tech", cities=["Dhaka"])
    db.add(camp)
    db.flush()
    a = Person(company_id=comp.id, full_name="Barrister Shameem Patwary", name_key="barrister shameem patwary",
               confidence=65, feedback="correct")
    b = Person(company_id=comp.id, full_name="Shameem Patwary", name_key="shameem patwary", confidence=40)
    db.add_all([a, b])
    db.flush()
    lead = Lead(campaign_id=camp.id, company_id=comp.id, primary_person_id=b.id)
    db.add(lead)
    db.commit()
    db.refresh(comp)
    _save_people(db, comp, [])
    db.commit()
    db.expire_all()
    people = db.scalars(select(Person)).all()
    assert [p.full_name for p in people] == ["Barrister Shameem Patwary"]
    assert db.get(Lead, lead.id).primary_person_id == a.id


# ---------------------------------------------------------------- 3. better emails
def test_company_address_format_is_learned_and_applied():
    known = [("Karim Ahmed", "karim.ahmed@acme.com.bd"), ("Nadia Islam", "nadia.islam@acme.com.bd")]
    assert learn_format(known, "acme.com.bd") == "first.last"
    assert learn_format([("Karim Ahmed", "kahmed@acme.com.bd")], "acme.com.bd") == "flast"
    assert learn_format([("Karim Ahmed", "karim@gmail.com")], "acme.com.bd") is None
    got = choose_email("Md. Rahim Uddin", ["info@acme.com.bd", "karim.ahmed@acme.com.bd"], "acme.com.bd", known=known)
    assert got == ("rahim.uddin@acme.com.bd", "pattern")


def test_one_word_names_do_not_teach_a_format():
    assert learn_format([("Mr. Mohammad Imtiaj", "imtiaj@buft.edu.bd")], "buft.edu.bd") is None


def test_published_role_mailbox_is_used_for_that_role():
    site = ["info@buft.edu.bd", "chairman@buft.edu.bd", "vc@buft.edu.bd"]
    assert choose_email("Mr. Faruque Hassan", site, "buft.edu.bd", title="Chairman") == ("chairman@buft.edu.bd", "found")
    assert choose_email("Prof. Dr. Ayub Nabi Khan", site, "buft.edu.bd", title="Vice Chancellor") == ("vc@buft.edu.bd", "found")
    # a Vice Chairman never gets the Chairman's inbox
    assert choose_email("Karim Ahmed", site, "buft.edu.bd", title="Vice Chairman") == ("info@buft.edu.bd", "company")


def test_a_staff_members_inbox_is_not_used_as_the_company_address():
    site = ["tajul.islam@bu.edu.bd", "rahman.khalid@bu.edu.bd", "soud20079@bu.edu.bd"]
    assert choose_email("", site, "bu.edu.bd") == ("", "unknown")
    assert choose_email("", site + ["registrar@bu.edu.bd"], "bu.edu.bd") == ("registrar@bu.edu.bd", "company")
    assert choose_email("", ["acme.traders@gmail.com"], None) == ("acme.traders@gmail.com", "company")
    assert choose_email("", ["admission.info@royal.edu.bd"], "royal.edu.bd") == ("admission.info@royal.edu.bd", "company")


def test_invalid_scraped_addresses_are_ignored():
    from app.services.normalize import find_emails

    assert find_emails("Mail doorstep.@ebitan.com or confidence@ebitan.com, demo info@demolink.org") == [
        "confidence@ebitan.com"]
    assert choose_email("", ["doorstep.@ebitan.com", "confidence@ebitan.com"], "ebitan.com") == (
        "confidence@ebitan.com", "company")


def test_campaign_title_order_breaks_ties_between_top_people(db):
    from app.pipeline.stages import target_order

    targets = ["Chairman", "Vice Chancellor", "Principal"]
    assert target_order("Founder Member and Chairman", targets) == 0
    assert target_order("Co-Founder and Co-Chairman", targets) == 0.5
    assert target_order("Pro-Vice-Chancellor", targets) == 1.5
    assert target_order("Vice Chancellor", targets) == 1
    comp = _company(db)
    vc = Person(company_id=comp.id, full_name="Saiful Islam", name_key="saiful islam", title="Vice Chancellor",
                seniority_rank=1, confidence=65, sources=[{"kind": "website", "url": "https://x/a"},
                                                          {"kind": "website", "url": "https://x/board"}])
    chair = Person(company_id=comp.id, full_name="Hasanul Hasan", name_key="hasanul hasan",
                   title="Founder Member and Chairman", seniority_rank=1, confidence=65,
                   sources=[{"kind": "website", "url": "https://x/founders"}])
    db.add_all([vc, chair])
    db.flush()
    db.refresh(comp)
    assert primary_candidates(comp, targets)[0].full_name == "Hasanul Hasan"


def test_addresses_on_domains_that_cannot_receive_mail_are_dropped():
    status = {"deadsite.com.bd": "no", "gmail.com": "yes"}.get
    assert choose_email("Rahim Uddin", ["info@deadsite.com.bd", "acme.bd@gmail.com"], "deadsite.com.bd",
                        mx_check=lambda d: False, mail_ok=status) == ("acme.bd@gmail.com", "company")
    assert choose_email("Rahim Uddin", ["rahim@deadsite.com.bd"], "deadsite.com.bd",
                        mx_check=lambda d: False, mail_ok=status) == ("", "unknown")


def test_pattern_addresses_are_never_auto_sent():
    from app.pipeline.emails import SENDABLE_EMAIL_STATUSES

    assert "pattern" not in SENDABLE_EMAIL_STATUSES and "guessed" not in SENDABLE_EMAIL_STATUSES


# ---------------------------------------------------------------- 5. feedback
def _lead_with_two_people(db):
    comp = _company(db, emails=["info@acme.com.bd"])
    tpl = db.scalar(select(EmailTemplate))
    camp = Campaign(name="Tech Dhaka", industry_slug="tech", cities=["Dhaka"], email_template_id=tpl.id)
    db.add(camp)
    db.flush()
    boss = Person(company_id=comp.id, full_name="Rahim Uddin", name_key="rahim uddin", title="Managing Director",
                  seniority_rank=1, confidence=65)
    other = Person(company_id=comp.id, full_name="Karim Ahmed", name_key="karim ahmed", title="Director",
                   seniority_rank=2, confidence=55)
    db.add_all([boss, other])
    db.flush()
    lead = Lead(campaign_id=camp.id, company_id=comp.id)
    db.add(lead)
    db.flush()
    set_primary(comp, lead, boss)
    db.add(EmailMessage(lead_id=lead.id, person_id=boss.id, to_email=boss.email, subject="Hi", body="Dear Rahim",
                        status="draft", unsubscribe_token="tok-1"))
    db.commit()
    return lead, boss, other


def test_marking_wrong_moves_the_lead_to_the_next_person_and_cancels_drafts(client, db):  # noqa: F811
    tok = login(client)
    lead, boss, other = _lead_with_two_people(db)
    r = client.post(f"/leads/{lead.id}/people/{boss.id}/feedback", data={"verdict": "wrong", "csrf_token": tok})
    assert r.status_code == 200
    db.expire_all()
    assert db.get(Lead, lead.id).primary_person_id == other.id
    assert db.get(Person, boss.id).feedback == "wrong"
    assert db.scalar(select(EmailMessage)).status == "cancelled"
    comp = db.get(Company, lead.company_id)
    assert boss.id not in [p.id for p in primary_candidates(comp)]
    # a later run finds Rahim again: he is still never chosen
    _save_people(db, comp, merge_and_score([Candidate("Rahim Uddin", "Managing Director", "https://acme.com.bd/board",
                                                      "", on_website=True)], []))
    assert primary_candidates(comp)[0].id == other.id


def test_marking_correct_makes_the_contact_and_feeds_the_dashboard(client, db):  # noqa: F811
    tok = login(client)
    lead, boss, other = _lead_with_two_people(db)
    client.post(f"/leads/{lead.id}/people/{other.id}/feedback", data={"verdict": "correct", "csrf_token": tok})
    db.expire_all()
    assert db.get(Lead, lead.id).primary_person_id == other.id
    client.post(f"/leads/{lead.id}/people/{boss.id}/feedback", data={"verdict": "wrong", "csrf_token": tok})
    page = client.get("/").text
    assert "Right person?" in page and "50%" in page
    client.post(f"/leads/{lead.id}/people/{boss.id}/feedback", data={"verdict": "clear", "csrf_token": tok})
    db.expire_all()
    assert db.get(Person, boss.id).feedback == ""
    assert "✗ Wrong" in client.get(f"/leads/{lead.id}").text


def test_feedback_rejects_a_person_from_another_company(client, db):  # noqa: F811
    tok = login(client)
    lead, boss, _ = _lead_with_two_people(db)
    stranger_co = Company(name="Other", industry_slug="tech")
    db.add(stranger_co)
    db.flush()
    stranger = Person(company_id=stranger_co.id, full_name="X Y", name_key="x y")
    db.add(stranger)
    db.commit()
    r = client.post(f"/leads/{lead.id}/people/{stranger.id}/feedback", data={"verdict": "correct", "csrf_token": tok})
    assert r.status_code == 400
