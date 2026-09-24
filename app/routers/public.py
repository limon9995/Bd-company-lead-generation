"""Unauthenticated endpoints: unsubscribe links and health check."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import EmailMessage
from app.pipeline.emails import suppress

router = APIRouter()

PAGE = """<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Unsubscribe</title><style>body{{font-family:system-ui,sans-serif;max-width:32rem;margin:4rem auto;padding:0 1rem;color:#222}}
button{{padding:.6rem 1.2rem;font-size:1rem;cursor:pointer}}</style></head><body>{body}</body></html>"""


@router.get("/u/{token}", response_class=HTMLResponse)
def unsubscribe_page(token: str, db: Session = Depends(get_db)):
    m = db.scalar(select(EmailMessage).where(EmailMessage.unsubscribe_token == token))
    if m is None:
        return HTMLResponse(PAGE.format(body="<p>This link is not valid.</p>"), 404)
    return HTMLResponse(PAGE.format(body=(
        "<h2>Unsubscribe</h2><p>Stop receiving emails at this address?</p>"
        f'<form method="post"><button type="submit">Yes, unsubscribe</button></form>')))


@router.post("/u/{token}", response_class=HTMLResponse)
def unsubscribe(token: str, request: Request, db: Session = Depends(get_db)):
    # Also used by mail clients' one-click unsubscribe (RFC 8058) - the token itself is the credential.
    m = db.scalar(select(EmailMessage).where(EmailMessage.unsubscribe_token == token))
    if m is None:
        return HTMLResponse(PAGE.format(body="<p>This link is not valid.</p>"), 404)
    suppress(db, m.to_email, "unsubscribed")
    for other in db.scalars(select(EmailMessage).where(EmailMessage.to_email == m.to_email,
                                                       EmailMessage.status.in_(["draft", "approved"]))):
        other.status, other.error = "cancelled", "recipient unsubscribed"
    db.commit()
    return HTMLResponse(PAGE.format(body="<h2>Done</h2><p>You have been unsubscribed. Sorry for the interruption.</p>"))


@router.get("/health")
def health(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return JSONResponse({"ok": True})
