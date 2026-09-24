"""Shared web helpers: templates, auth, CSRF, flash messages, audit."""
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import config
from app.db import get_db
from app.models import AuditLog, User
from app.security import new_token, tokens_equal
from app.services.scoring import bucket

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _local(dt: datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(ZoneInfo(config.timezone)).strftime(fmt)


templates.env.filters["local"] = _local
templates.env.filters["bucket"] = bucket
templates.env.globals["tz"] = config.timezone


class LoginRequired(Exception):
    pass


def csrf_token(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = new_token(16)
        request.session["csrf"] = tok
    return tok


async def verify_csrf(request: Request) -> None:
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    sent = request.headers.get("x-csrf-token")
    if not sent:
        form = await request.form()
        sent = form.get("csrf_token")
    if not tokens_equal(str(sent or ""), request.session.get("csrf", "")):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token - reload the page and try again.")


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    uid = request.session.get("uid")
    user = db.get(User, uid) if uid else None
    if user is None or not user.is_active:
        raise LoginRequired()
    return user


def flash(request: Request, message: str, kind: str = "ok") -> None:
    request.session.setdefault("flash", [])
    request.session["flash"] = [*request.session["flash"], [kind, message]]


def pop_flash(request: Request) -> list:
    msgs = request.session.get("flash", [])
    request.session["flash"] = []
    return msgs


def render(request: Request, name: str, ctx: dict | None = None, status_code: int = 200):
    base = {"request": request, "csrf": csrf_token(request), "flashes": pop_flash(request),
            "user_email": request.session.get("email", ""), "path": request.url.path}
    return templates.TemplateResponse(request, name, {**base, **(ctx or {})}, status_code=status_code)


def audit(db: Session, user: User | None, action: str, entity: str = "", detail: str = "") -> None:
    db.add(AuditLog(user_id=user.id if user else None, action=action, entity=entity, detail=detail[:2000]))
