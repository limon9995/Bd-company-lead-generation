"""Idempotent seeding of industry presets and a default email template."""
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import EmailTemplate, IndustryPreset

SEED_FILE = Path(__file__).parent / "seeds" / "industries.yaml"

DEFAULT_TEMPLATE_BODY = """Dear {{first_name}},

{{personal_line}}

I'm {{sender_name}} from {{sender_company}}. We help {{industry}} businesses in {{city}} with <one line about your service>.

Would you be open to a 15-minute call next week to see if this could be useful for {{company_name}}?

Best regards,
{{sender_name}}"""


def seed(db: Session) -> None:
    for item in yaml.safe_load(SEED_FILE.read_text(encoding="utf-8")):
        if db.scalar(select(IndustryPreset).where(IndustryPreset.slug == item["slug"])):
            continue
        db.add(IndustryPreset(slug=item["slug"], name=item["name"], places_queries=item["places_queries"],
                              default_titles=item["default_titles"], pitch_context=item.get("pitch_context", "")))
    if not db.scalar(select(EmailTemplate.id).limit(1)):
        db.add(EmailTemplate(name="Intro (default)", subject_tpl="Quick question for {{company_name}}",
                             body_tpl=DEFAULT_TEMPLATE_BODY, use_ai_personalisation=True,
                             ai_instructions="We offer <your service>. Keep it polite and short."))
    db.commit()
