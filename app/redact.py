"""Keep API keys out of anything a person can see: error messages, run notes, test results and logs.

Every saved secret (and a few well-known key formats) is replaced with "[hidden]". The list of secrets is
re-read from the database at most every 30 seconds.
"""
import json
import logging
import re
import threading
import time

_lock = threading.Lock()
_cache: tuple[float, list[str]] = (0.0, [])
TTL = 30
PATTERNS = [
    (re.compile(r"bot\d{5,}:[A-Za-z0-9_-]{20,}"), "bot[hidden]"),        # Telegram bot token in URLs
    (re.compile(r"AIza[0-9A-Za-z_-]{30,}"), "[hidden]"),                  # Google API keys
    (re.compile(r"(?i)([?&](?:key|api_key|apikey|token|access_token)=)[^&\s\"']+"), r"\1[hidden]"),
    (re.compile(r"(?i)(authorization:\s*bearer\s+)\S+"), r"\1[hidden]"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "[hidden private key]"),
    (re.compile(r"(://[^/\s:@]+:)[^@\s/]+@"), r"\1[hidden]@"),           # user:password@host in proxy URLs
]


def _load_secrets() -> list[str]:
    from app import settings_store
    from app.db import SessionLocal

    values: list[str] = []
    with SessionLocal() as db:
        for d in settings_store.DEFS:
            if not d.secret:
                continue
            try:
                v = settings_store.get_raw(db, d.key)
            except Exception:  # noqa: BLE001 - e.g. APP_SECRET_KEY changed; nothing we can show anyway
                continue
            if not v:
                continue
            values.append(v)
            if d.key == "sheets_service_account_json":
                try:
                    info = json.loads(v)
                    values += [str(info.get(k, "")) for k in ("private_key", "private_key_id")]
                except ValueError:
                    pass
    # longest first so a key that contains another is fully hidden; ignore tiny values
    return sorted({v.strip() for v in values if len(v.strip()) >= 6}, key=len, reverse=True)


def secrets(force: bool = False) -> list[str]:
    global _cache
    with _lock:
        loaded, vals = _cache
        if force or time.monotonic() - loaded > TTL:
            try:
                vals = _load_secrets()
            except Exception:  # noqa: BLE001 - DB not ready (startup, tests)
                vals = vals or []
            _cache = (time.monotonic(), vals)
        return vals


def refresh() -> None:
    secrets(force=True)


def redact(text) -> str:
    if text is None:
        return ""
    out = str(text)
    for s in secrets():
        if s in out:
            out = out.replace(s, "[hidden]")
    for pat, repl in PATTERNS:
        out = pat.sub(repl, out)
    return out


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if record.name == "uvicorn.access" and isinstance(record.args, tuple):
                # its formatter unpacks exactly (client, method, path, version, status): redact each, keep the shape
                record.args = tuple(redact(a) if isinstance(a, str) else a for a in record.args)
                return True
            record.msg = redact(record.getMessage())
            record.args = ()
        except Exception:  # noqa: BLE001 - never break logging
            pass
        return True


def install_log_filter() -> None:
    # httpx logs every request URL at INFO - Telegram puts the bot token in the URL
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    f = RedactingFilter()
    root = logging.getLogger()
    for h in root.handlers:
        h.addFilter(f)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "apscheduler", "httpx"):
        for h in logging.getLogger(name).handlers:
            h.addFilter(f)
