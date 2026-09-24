"""Process-level configuration read from environment variables.

Only bootstrap values live here (DB URL, master encryption key, session secret).
Every integration credential (API keys, tokens, SMTP, ...) is entered through the
admin panel and stored encrypted in the `settings` table - see app/settings_store.py.
"""
import os
from dataclasses import dataclass, field


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    database_url: str = field(default_factory=lambda: os.getenv("DATABASE_URL", "sqlite:///./leadgen.db"))
    # Fernet key (urlsafe base64, 32 bytes). Generate: python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"
    app_secret_key: str = field(default_factory=lambda: os.getenv("APP_SECRET_KEY", ""))
    session_secret: str = field(default_factory=lambda: os.getenv("SESSION_SECRET", ""))
    base_url: str = field(default_factory=lambda: os.getenv("BASE_URL", "http://localhost:8000").rstrip("/"))
    timezone: str = field(default_factory=lambda: os.getenv("APP_TIMEZONE", "Asia/Dhaka"))
    secure_cookies: bool = field(default_factory=lambda: _bool("SECURE_COOKIES", False))
    worker_poll_seconds: float = field(default_factory=lambda: float(os.getenv("WORKER_POLL_SECONDS", "2")))
    worker_concurrency: int = field(default_factory=lambda: int(os.getenv("WORKER_CONCURRENCY", "3")))
    chromium_executable: str | None = field(default_factory=lambda: os.getenv("CHROMIUM_EXECUTABLE") or None)


config = Config()
