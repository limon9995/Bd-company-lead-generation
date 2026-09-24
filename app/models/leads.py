from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._base import TimestampMixin

CRM_STATUSES = ["new", "contacted", "replied", "interested", "not_interested", "do_not_contact"]


class Campaign(TimestampMixin, Base):
    __tablename__ = "campaigns"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    industry_slug: Mapped[str] = mapped_column(String(64), index=True)
    cities: Mapped[list] = mapped_column(JSON, default=list)
    extra_keywords: Mapped[list] = mapped_column(JSON, default=list)
    target_titles: Mapped[list] = mapped_column(JSON, default=list)
    max_companies_per_run: Mapped[int] = mapped_column(Integer, default=50)
    schedule_cron: Mapped[str] = mapped_column(String(100), default="")  # empty = manual only
    email_template_id: Mapped[int | None] = mapped_column(ForeignKey("email_templates.id", ondelete="SET NULL"), nullable=True)
    auto_email: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Company(TimestampMixin, Base):
    __tablename__ = "companies"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    place_id: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    domain: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    name: Mapped[str] = mapped_column(String(300), index=True)
    name_key: Mapped[str] = mapped_column(String(300), index=True, default="")
    category: Mapped[str] = mapped_column(String(200), default="")
    industry_slug: Mapped[str] = mapped_column(String(64), default="", index=True)
    address: Mapped[str] = mapped_column(Text, default="")
    city: Mapped[str] = mapped_column(String(100), default="", index=True)
    phone: Mapped[str] = mapped_column(String(50), default="")
    website: Mapped[str] = mapped_column(String(500), default="")
    rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    reviews_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    google_maps_url: Mapped[str] = mapped_column(String(500), default="")
    socials: Mapped[dict] = mapped_column(JSON, default=dict)
    generic_emails: Mapped[list] = mapped_column(JSON, default=list)
    extra_phones: Mapped[list] = mapped_column(JSON, default=list)
    crawled_text: Mapped[str] = mapped_column(Text, default="")
    crawled_pages: Mapped[list] = mapped_column(JSON, default=list)
    enrichment_status: Mapped[str] = mapped_column(String(30), default="new")  # new/crawled/dm_done/failed
    enriched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_seen_campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True)

    people: Mapped[list["Person"]] = relationship(back_populates="company", cascade="all, delete-orphan")


class Person(TimestampMixin, Base):
    __tablename__ = "people"
    __table_args__ = (UniqueConstraint("company_id", "name_key", name="uq_person_company_name"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    full_name: Mapped[str] = mapped_column(String(200))
    name_key: Mapped[str] = mapped_column(String(200), default="")
    title: Mapped[str] = mapped_column(String(200), default="")
    seniority_rank: Mapped[int] = mapped_column(Integer, default=99)
    email: Mapped[str] = mapped_column(String(255), default="")
    email_status: Mapped[str] = mapped_column(String(20), default="unknown")  # found/guessed/unknown
    linkedin_url: Mapped[str] = mapped_column(String(500), default="")
    source_url: Mapped[str] = mapped_column(String(1000), default="")
    evidence: Mapped[str] = mapped_column(Text, default="")
    sources: Mapped[list] = mapped_column(JSON, default=list)  # [{"kind": "website"|"search", "url": ...}]
    confidence: Mapped[int] = mapped_column(Integer, default=0)
    confidence_breakdown: Mapped[list] = mapped_column(JSON, default=list)

    company: Mapped[Company] = relationship(back_populates="people")


class Lead(TimestampMixin, Base):
    __tablename__ = "leads"
    __table_args__ = (UniqueConstraint("campaign_id", "company_id", name="uq_lead_campaign_company"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    primary_person_id: Mapped[int | None] = mapped_column(ForeignKey("people.id", ondelete="SET NULL"), nullable=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"), nullable=True)
    crm_status: Mapped[str] = mapped_column(String(30), default="new", index=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    owner: Mapped[str] = mapped_column(String(100), default="")
    last_contacted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notified: Mapped[bool] = mapped_column(Boolean, default=False)
    sheet_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    campaign: Mapped[Campaign] = relationship()
    company: Mapped[Company] = relationship()
    primary_person: Mapped[Person | None] = relationship()
