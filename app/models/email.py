from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._base import TimestampMixin

EMAIL_STATUSES = ["draft", "approved", "sent", "failed", "cancelled"]


class EmailTemplate(TimestampMixin, Base):
    __tablename__ = "email_templates"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    subject_tpl: Mapped[str] = mapped_column(String(500))
    body_tpl: Mapped[str] = mapped_column(Text)
    use_ai_personalisation: Mapped[bool] = mapped_column(Boolean, default=True)
    ai_instructions: Mapped[str] = mapped_column(Text, default="")


class EmailMessage(TimestampMixin, Base):
    __tablename__ = "email_messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    person_id: Mapped[int | None] = mapped_column(ForeignKey("people.id", ondelete="SET NULL"), nullable=True)
    to_email: Mapped[str] = mapped_column(String(255), index=True)
    subject: Mapped[str] = mapped_column(String(500))
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)
    not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")
    unsubscribe_token: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    lead = relationship("Lead")


class Suppression(TimestampMixin, Base):
    __tablename__ = "suppression_list"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    value: Mapped[str] = mapped_column(String(255), unique=True, index=True)  # email or @domain
    reason: Mapped[str] = mapped_column(String(50), default="manual")
