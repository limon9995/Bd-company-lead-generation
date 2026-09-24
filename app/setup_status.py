"""Onboarding checklist shown on the dashboard and the Setup page."""
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import settings_store
from app.models import Campaign, EmailTemplate, Run


def checklist(db: Session) -> list[dict]:
    tests = settings_store.test_results(db)

    def key_step(group: str, keys: list[str], title: str, why: str, optional: bool = False) -> dict:
        configured = all(settings_store.is_set(db, k) or settings_store.get(db, k) not in ("", None) for k in keys)
        t = tests.get(group)
        state = "done" if configured and t and t["ok"] else ("error" if t and not t["ok"] else ("todo" if not configured else "untested"))
        return {"title": title, "why": why, "state": state, "href": f"/settings#{group}", "optional": optional,
                "detail": t["message"] if t else ""}

    campaigns = db.scalar(select(func.count(Campaign.id))) or 0
    tpl = db.scalar(select(EmailTemplate).order_by(EmailTemplate.id).limit(1))
    tpl_edited = bool(tpl and "<one line about your service>" not in tpl.body_tpl)
    done_runs = db.scalar(select(func.count(Run.id)).where(Run.status == "done")) or 0
    uses_api = db.scalar(select(func.count(Campaign.id)).where(Campaign.discovery_source == "places_api")) or 0
    places_optional = campaigns > 0 and uses_api == 0
    return [
        key_step("places", ["places_api_key"], "Google Places API key",
                 "Finds the companies." + (" Optional: your campaigns use browser sources." if places_optional else
                                           " Or set a campaign to 'Google Maps (browser)' to skip this key."),
                 optional=places_optional),
        key_step("gemini", ["gemini_api_key"], "Gemini API key", "Reads websites and finds the decision maker."),
        key_step("telegram", ["telegram_bot_token", "telegram_chat_id"], "Telegram bot + chat ID", "Sends you alerts."),
        key_step("smtp", ["smtp_username", "smtp_password"], "Gmail SMTP", "Sends the approved emails."),
        key_step("search", ["serper_api_key"] if settings_store.get(db, "search_provider") == "serper" else ["brave_api_key"],
                 "Search API", "Finds more decision makers when the website has none.", optional=True),
        key_step("sheets", ["sheets_service_account_json", "sheets_spreadsheet_id"], "Google Sheet",
                 "Keeps a copy of every lead in a Sheet.", optional=True),
        {"title": "Write your email template", "why": "Replace the placeholder line with what you offer.",
         "state": "done" if tpl_edited else "todo", "href": f"/templates/{tpl.id}/edit" if tpl else "/templates", "optional": False, "detail": ""},
        {"title": "Create a campaign", "why": "Pick an industry and cities.", "state": "done" if campaigns else "todo",
         "href": "/campaigns/new", "optional": False, "detail": ""},
        {"title": "Finish a first run", "why": "Run a small pilot (e.g. 20 companies) and check the leads.",
         "state": "done" if done_runs else "todo", "href": "/campaigns", "optional": False, "detail": ""},
    ]


def progress(steps: list[dict]) -> tuple[int, int]:
    required = [s for s in steps if not s["optional"]]
    return sum(1 for s in required if s["state"] == "done"), len(required)
