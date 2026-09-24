"""Template rendering, optional AI personalisation, and SMTP sending."""
import re
import smtplib
import ssl
from email.message import EmailMessage as MimeMessage
from email.utils import formataddr, make_msgid

from app.services.errors import ProviderError
from app.services.llm import LLMProvider

VAR = re.compile(r"\{\{\s*(\w+)\s*\}\}")
TEMPLATE_VARS = ["company_name", "person_name", "first_name", "title", "city", "industry", "category",
                 "sender_name", "sender_company", "personal_line"]


def render(tpl: str, ctx: dict) -> str:
    return VAR.sub(lambda m: str(ctx.get(m.group(1), "") or ""), tpl or "")


def personal_line(llm: LLMProvider, ctx: dict, instructions: str) -> str:
    prompt = (
        "Write ONE short, friendly opening sentence (max 30 words) for a B2B cold email.\n"
        f"Recipient: {ctx.get('person_name') or 'the management'} ({ctx.get('title') or ''}) at {ctx.get('company_name')}, "
        f"a {ctx.get('category') or ctx.get('industry')} in {ctx.get('city')}, Bangladesh.\n"
        f"Known facts: {ctx.get('facts') or 'none'}\n"
        f"Sender context: {instructions or 'none'}\n"
        "Use only the facts given - do not invent achievements, numbers or awards. Output only the sentence."
    )
    line = llm.generate_text(prompt).strip().strip('"')
    return line.split("\n")[0][:300]


def footer(unsubscribe_url: str, sender_company: str) -> str:
    who = f" from {sender_company}" if sender_company else ""
    return f"\n\n--\nIf you'd prefer not to hear{who} again, reply 'unsubscribe' or click: {unsubscribe_url}"


def send_smtp(s: dict, to: str, subject: str, body: str, unsubscribe_url: str) -> str:
    if not (s.get("smtp_host") and s.get("smtp_username") and s.get("smtp_password")):
        raise ProviderError("SMTP is not configured")
    msg = MimeMessage()
    sender = s.get("sender_email") or s["smtp_username"]
    msg["From"] = formataddr((s.get("sender_name") or "", sender))
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=sender.split("@")[-1])
    msg["List-Unsubscribe"] = f"<{unsubscribe_url}>"
    msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    msg.set_content(body)
    try:
        if s.get("smtp_use_ssl"):
            with smtplib.SMTP_SSL(s["smtp_host"], int(s["smtp_port"]), context=ssl.create_default_context(), timeout=30) as smtp:
                smtp.login(s["smtp_username"], s["smtp_password"])
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(s["smtp_host"], int(s["smtp_port"]), timeout=30) as smtp:
                smtp.starttls(context=ssl.create_default_context())
                smtp.login(s["smtp_username"], s["smtp_password"])
                smtp.send_message(msg)
    except smtplib.SMTPRecipientsRefused as exc:
        raise ProviderError(f"recipient refused: {exc}") from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise ProviderError(f"SMTP error: {exc}") from exc
    return msg["Message-ID"]


def test_smtp(s: dict) -> str:
    try:
        if s.get("smtp_use_ssl"):
            with smtplib.SMTP_SSL(s["smtp_host"], int(s["smtp_port"]), timeout=20) as smtp:
                smtp.login(s["smtp_username"], s["smtp_password"])
        else:
            with smtplib.SMTP(s["smtp_host"], int(s["smtp_port"]), timeout=20) as smtp:
                smtp.starttls(context=ssl.create_default_context())
                smtp.login(s["smtp_username"], s["smtp_password"])
    except (smtplib.SMTPException, OSError) as exc:
        raise ProviderError(f"SMTP login failed: {exc}") from exc
    return "OK - logged in"
