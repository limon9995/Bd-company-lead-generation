from sqlalchemy import JSON, Boolean, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._base import TimestampMixin


class Setting(TimestampMixin, Base):
    """Key/value store for integration credentials and tunables. Values are always Fernet-encrypted."""

    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value_encrypted: Mapped[str] = mapped_column(Text, default="")
    is_secret: Mapped[bool] = mapped_column(Boolean, default=False)


class IndustryPreset(TimestampMixin, Base):
    __tablename__ = "industry_presets"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    places_queries: Mapped[list] = mapped_column(JSON, default=list)
    default_titles: Mapped[list] = mapped_column(JSON, default=list)
    pitch_context: Mapped[str] = mapped_column(Text, default="")
