"""Registry + encrypted storage for everything configurable from the admin panel.

Add a new integration setting by adding a `SettingDef` below; the Settings page renders
it automatically and `get()` returns its (decrypted, typed) value.
"""
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models import Setting
from app.security import decrypt, encrypt


@dataclass(frozen=True)
class SettingDef:
    key: str
    label: str
    group: str
    kind: str = "text"  # text | secret | textarea | secret_textarea | int | float | bool | select
    default: str = ""
    help: str = ""
    options: tuple[str, ...] = ()

    @property
    def secret(self) -> bool:
        return self.kind.startswith("secret")


GROUPS: list[tuple[str, str, str]] = [
    # (id, title, description)
    ("places", "Google Places API", "Company discovery (official API - no Maps scraping)."),
    ("search", "Web Search API", "Used to find decision makers from public search results (news, public profile snippets)."),
    ("gemini", "Gemini (AI)", "Extracts decision makers from page text and personalises emails."),
    ("telegram", "Telegram", "Lead notifications and alerts."),
    ("whatsapp", "WhatsApp Cloud API (optional)", "Official Meta API only. Requires an approved message template."),
    ("smtp", "Email sending (SMTP)", "Gmail works: smtp.gmail.com, port 587, with an App Password. No custom domain needed."),
    ("email_rules", "Email schedule & limits", "When and how many emails go out."),
    ("sheets", "Google Sheets mirror", "Leads are copied (one-way) to a Google Sheet."),
    ("limits", "Crawling & budgets", "Politeness and daily API call caps (budget guard)."),
]

DEFS: list[SettingDef] = [
    SettingDef("places_api_key", "API key", "places", "secret", help="Google Cloud console → APIs & Services → enable 'Places API (New)' → Credentials."),
    SettingDef("places_region_code", "Region code", "places", default="bd"),
    SettingDef("places_language", "Language", "places", default="en"),
    SettingDef("places_cost_per_call", "Est. cost per call (USD)", "places", "float", default="0.035",
               help="Estimate only, used for the dashboard. Check Google's current Places pricing and update."),

    SettingDef("search_provider", "Provider", "search", "select", default="serper", options=("serper", "brave", "none")),
    SettingDef("serper_api_key", "Serper.dev API key", "search", "secret"),
    SettingDef("brave_api_key", "Brave Search API key", "search", "secret"),
    SettingDef("search_cost_per_call", "Est. cost per call (USD)", "search", "float", default="0.001"),

    SettingDef("gemini_api_key", "API key", "gemini", "secret", help="https://aistudio.google.com → Get API key."),
    SettingDef("gemini_model", "Model", "gemini", default="gemini-flash-lite-latest",
               help="'gemini-flash-lite-latest' (cheapest) or 'gemini-flash-latest' (better quality) always point to "
                    "Google's current model. 'Test' lists the models your key can use."),
    SettingDef("gemini_cost_per_call", "Est. cost per call (USD)", "gemini", "float", default="0.002",
               help="Estimate (~6k input tokens per call). Check Google AI Studio billing for real cost."),

    SettingDef("telegram_bot_token", "Bot token", "telegram", "secret", help="Create a bot with @BotFather."),
    SettingDef("telegram_chat_id", "Chat ID", "telegram",
               help="Send /start to your bot, then click 'Detect chat ID' (or run scripts/get_telegram_chat_id.py)."),
    SettingDef("telegram_lead_cards", "Send a card for each high-confidence lead", "telegram", "bool", default="true"),

    SettingDef("whatsapp_token", "Access token", "whatsapp", "secret"),
    SettingDef("whatsapp_phone_number_id", "Phone number ID", "whatsapp"),
    SettingDef("whatsapp_to", "Notify number (e.g. 8801XXXXXXXXX)", "whatsapp"),
    SettingDef("whatsapp_template", "Template name", "whatsapp",
               help="Approved template with ONE body variable {{1}} (the summary text)."),
    SettingDef("whatsapp_template_lang", "Template language", "whatsapp", default="en"),
    SettingDef("whatsapp_api_version", "Graph API version", "whatsapp", default="v21.0"),

    SettingDef("smtp_host", "SMTP host", "smtp", default="smtp.gmail.com"),
    SettingDef("smtp_port", "SMTP port", "smtp", "int", default="587"),
    SettingDef("smtp_username", "Username", "smtp"),
    SettingDef("smtp_password", "Password / App Password", "smtp", "secret"),
    SettingDef("smtp_use_ssl", "Use SSL (port 465) instead of STARTTLS", "smtp", "bool", default="false"),
    SettingDef("sender_name", "Sender name", "smtp"),
    SettingDef("sender_email", "Sender email", "smtp", help="Usually the same as the username for Gmail."),
    SettingDef("sender_company", "Your company name", "smtp"),
    SettingDef("sender_signature", "Signature", "smtp", "textarea"),

    SettingDef("send_days", "Send days", "email_rules", default="sun,mon,tue,wed,thu",
               help="Comma separated: sat,sun,mon,tue,wed,thu,fri"),
    SettingDef("send_window_start", "Window start (HH:MM, Asia/Dhaka)", "email_rules", default="10:00"),
    SettingDef("send_window_end", "Window end (HH:MM, Asia/Dhaka)", "email_rules", default="17:00"),
    SettingDef("daily_email_cap", "Max emails per day", "email_rules", "int", default="40",
               help="Keep low on a Gmail account (Gmail hard limit is ~500/day; spam reports can suspend it)."),
    SettingDef("email_min_gap_seconds", "Min gap between emails (s)", "email_rules", "int", default="60"),
    SettingDef("email_max_gap_seconds", "Max gap between emails (s)", "email_rules", "int", default="180"),
    SettingDef("company_cooldown_days", "Don't email the same company again within (days)", "email_rules", "int", default="30"),
    SettingDef("auto_send_min_confidence", "Auto-send only if confidence ≥", "email_rules", "int", default="70"),

    SettingDef("sheets_service_account_json", "Service account JSON", "sheets", "secret_textarea",
               help="Paste the full JSON key. Then share the spreadsheet with the service account's client_email (Editor)."),
    SettingDef("sheets_spreadsheet_id", "Spreadsheet ID", "sheets", help="The long id in the sheet URL: /spreadsheets/d/<ID>/edit"),

    SettingDef("crawl_max_pages", "Max pages per website", "limits", "int", default="8"),
    SettingDef("crawl_delay_seconds", "Delay between requests to the same site (s)", "limits", "float", default="2"),
    SettingDef("crawl_timeout_seconds", "Page timeout (s)", "limits", "int", default="20"),
    SettingDef("crawl_respect_robots", "Respect robots.txt", "limits", "bool", default="true"),
    SettingDef("crawl_use_browser", "Use headless browser (Playwright) for JS sites", "limits", "bool", default="true"),
    SettingDef("recrawl_after_days", "Re-enrich a company after (days)", "limits", "int", default="30"),
    SettingDef("places_daily_cap", "Places API max calls/day", "limits", "int", default="300"),
    SettingDef("search_daily_cap", "Search API max calls/day", "limits", "int", default="300"),
    SettingDef("gemini_daily_cap", "Gemini max calls/day", "limits", "int", default="800"),
]

BY_KEY = {d.key: d for d in DEFS}


def _coerce(d: SettingDef, raw: str):
    if d.kind == "int":
        try:
            return int(float(raw))
        except ValueError:
            return int(float(d.default or 0))
    if d.kind == "float":
        try:
            return float(raw)
        except ValueError:
            return float(d.default or 0)
    if d.kind == "bool":
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}
    return raw


def get_raw(db: Session, key: str) -> str:
    d = BY_KEY[key]
    row = db.get(Setting, key)
    if row is None or row.value_encrypted == "":
        return d.default
    return decrypt(row.value_encrypted)


def get(db: Session, key: str):
    return _coerce(BY_KEY[key], get_raw(db, key))


def is_set(db: Session, key: str) -> bool:
    row = db.get(Setting, key)
    return row is not None and row.value_encrypted != ""


def set_value(db: Session, key: str, value: str) -> None:
    d = BY_KEY[key]
    row = db.get(Setting, key)
    if row is None:
        row = Setting(key=key, is_secret=d.secret)
        db.add(row)
    row.value_encrypted = encrypt(value) if value != "" else ""


def snapshot(db: Session) -> dict:
    """All settings as a typed dict (one DB round-trip per key is fine at this size)."""
    return {d.key: get(db, d.key) for d in DEFS}
