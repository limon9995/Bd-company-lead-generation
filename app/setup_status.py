"""Onboarding checklist shown on the dashboard and the Setup page."""
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import settings_store
from app.models import Campaign, EmailTemplate, Run


# "How to get it" guide per checklist step: numbered steps, where to go, and anything that costs money or can
# go wrong. Shown (collapsed) under each step on the Setup page.
GUIDES: dict[str, dict] = {
    "places": {
        "link": ("Google Cloud console", "https://console.cloud.google.com/"),
        "steps": [
            "Open console.cloud.google.com and sign in with a Google account.",
            "Top bar → project picker → New project → name it (e.g. \"BD Leads\") → Create, then select it.",
            "Menu → Billing → link a billing account (a card is required; Google gives a monthly free allowance).",
            "Menu → APIs & Services → Library → search \"Places API (New)\" → Enable.",
            "APIs & Services → Credentials → Create credentials → API key → copy the key.",
            "Click the new key → API restrictions → Restrict key → tick \"Places API (New)\" → Save.",
            "Here: Settings → Google Places API → paste into API key → Save → Test.",
        ],
        "note": "Not needed if your campaigns use \"Google Maps (browser)\". Check Google's current Places pricing "
                "and set a budget alert in Billing.",
    },
    "gemini": {
        "link": ("Google AI Studio", "https://aistudio.google.com/apikey"),
        "steps": [
            "Open aistudio.google.com/apikey and sign in with a Google account.",
            "Create API key → pick or create a Google Cloud project → copy the key.",
            "Here: Settings → Gemini (AI) → paste into API key. Leave Model as it is → Save → Test.",
            "If the test says the model is not found, it lists the models your key can use - copy one into Model.",
        ],
        "note": "Free tier: no card, but rate-limited, and Google may use free-tier data to improve its products. "
                "Without a key, decision makers are still found by title matching (less accurate).",
    },
    "telegram": {
        "link": ("Open @BotFather", "https://t.me/BotFather"),
        "steps": [
            "In Telegram search @BotFather (blue tick) → Start.",
            "Send /newbot → type a name → type a username ending in \"bot\" (e.g. bdleads_alert_bot).",
            "BotFather replies with a token like 123456789:AAH... → copy it.",
            "Open your new bot (link in BotFather's reply) → press Start, or send /start.",
            "Here: Settings → Telegram → paste into Bot token → Save → Detect chat ID → Save → Test.",
            "For a group instead: add the bot to the group, send any message there, then Detect chat ID.",
        ],
        "note": "Free. Keep the token private - anyone with it can control the bot.",
    },
    "smtp": {
        "link": ("Google App Passwords", "https://myaccount.google.com/apppasswords"),
        "steps": [
            "myaccount.google.com → Security → turn on 2-Step Verification (App Passwords need it).",
            "Open myaccount.google.com/apppasswords → name it \"BD Leads\" → Create → copy the 16-letter password.",
            "Here: Settings → Email sending: host smtp.gmail.com, port 587, Username = your full Gmail address.",
            "Password = the 16-letter App Password (NOT your normal Gmail password).",
            "Fill Sender name, Sender email (same Gmail), Your company name, Signature → Save → Test.",
        ],
        "note": "Keep \"Max emails per day\" low (default 40). Spam reports can suspend a Gmail account.",
    },
    "search": {
        "link": ("Serper.dev", "https://serper.dev/"),
        "steps": [
            "Open serper.dev → Sign up (Google login works, no card) → you get 2,500 free searches.",
            "Dashboard → API Key → copy it.",
            "Here: Settings → Web search → Provider: auto → paste into Serper.dev API key → Save → Test.",
            "Optional second key: brave.com/search/api → sign up → create an API key → paste into "
            "Brave Search API key. Auto uses it when Serper's credits run out.",
            "Campaigns → Edit → Decision-maker search: \"Auto\" (or \"Settings default\").",
        ],
        "note": "Without keys, Auto uses DuckDuckGo/Bing in the browser (free, slow, can be blocked, weak for LinkedIn). "
                "Repeated searches are answered from saved results for 30 days at no cost.",
    },
    "sheets": {
        "link": ("Google Cloud console", "https://console.cloud.google.com/"),
        "steps": [
            "console.cloud.google.com (same project as Places, or a new one) → APIs & Services → Library → "
            "enable \"Google Sheets API\".",
            "Menu → IAM & Admin → Service Accounts → Create service account → any name → Done.",
            "Open the service account → Keys → Add key → Create new key → JSON → a .json file downloads.",
            "Open that file in Notepad → copy everything → here: Settings → Google Sheets mirror → Service account JSON.",
            "Create a sheet at sheets.new → Share → paste the \"client_email\" from the JSON file → Editor → Send.",
            "Copy the sheet ID from its address (the part between /d/ and /edit) → Spreadsheet ID → Save → Test.",
        ],
        "note": "Free. Keep the JSON file private - it is a password for that sheet.",
    },
    "template": {
        "steps": [
            "Templates → open the default template → Edit.",
            "Replace \"<one line about your service>\" with one short sentence about what you offer.",
            "Keep the {{placeholders}} such as {{company_name}} and {{city}} - they are filled in per lead. "
            "Save → Preview.",
        ],
    },
    "campaign": {
        "steps": [
            "Campaigns → New.",
            "Pick an industry and the cities (e.g. Dhaka, Chattogram).",
            "Where companies come from: \"Google Places API\" (key, reliable) or \"Google Maps (browser)\" (free).",
            "Decision-maker search: \"Auto\". Set max companies per run to 20 for the first try → Save.",
        ],
    },
    "first_run": {
        "steps": [
            "Campaigns → Run now. Watch progress on the Runs page.",
            "Check Leads: is the decision maker right? Open a lead to see every person found and the source page.",
            "Check Outbox before approving any email. Emails go out only inside the send window you set.",
        ],
    },
}


def checklist(db: Session) -> list[dict]:
    tests = settings_store.test_results(db)

    def key_step(group: str, keys: list[str], title: str, why: str, optional: bool = False) -> dict:
        configured = all(settings_store.is_set(db, k) or settings_store.get(db, k) not in ("", None) for k in keys)
        t = tests.get(group)
        state = "done" if configured and t and t["ok"] else ("error" if t and not t["ok"] else ("todo" if not configured else "untested"))
        return {"title": title, "why": why, "state": state, "href": f"/settings#{group}", "optional": optional,
                "detail": t["message"] if t else "", "guide": GUIDES.get(group)}

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
        key_step("search", ["brave_api_key"] if settings_store.get(db, "search_provider") == "brave" else ["serper_api_key"],
                 "Search API", "Finds more decision makers when the website has none.", optional=True),
        key_step("sheets", ["sheets_service_account_json", "sheets_spreadsheet_id"], "Google Sheet",
                 "Keeps a copy of every lead in a Sheet.", optional=True),
        {"title": "Write your email template", "why": "Replace the placeholder line with what you offer.",
         "state": "done" if tpl_edited else "todo", "href": f"/templates/{tpl.id}/edit" if tpl else "/templates", "optional": False,
         "detail": "", "guide": GUIDES["template"]},
        {"title": "Create a campaign", "why": "Pick an industry and cities.", "state": "done" if campaigns else "todo",
         "href": "/campaigns/new", "optional": False, "detail": "", "guide": GUIDES["campaign"]},
        {"title": "Finish a first run", "why": "Run a small pilot (e.g. 20 companies) and check the leads.",
         "state": "done" if done_runs else "todo", "href": "/campaigns", "optional": False, "detail": "",
         "guide": GUIDES["first_run"]},
    ]


def progress(steps: list[dict]) -> tuple[int, int]:
    required = [s for s in steps if not s["optional"]]
    return sum(1 for s in required if s["state"] == "done"), len(required)
