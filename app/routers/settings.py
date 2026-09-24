from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app import settings_store
from app.db import get_db
from app.deps import audit, current_user, flash, render, verify_csrf
from app.models import User
from app.security import mask
from app.services.errors import ProviderError

router = APIRouter()


def _view(db: Session) -> list[dict]:
    groups = []
    tests = settings_store.test_results(db)
    for gid, title, desc in settings_store.GROUPS:
        fields = []
        for d in settings_store.DEFS:
            if d.group != gid:
                continue
            raw = settings_store.get_raw(db, d.key)
            fields.append({"d": d, "value": "" if d.secret else raw, "is_set": settings_store.is_set(db, d.key),
                           "masked": mask(raw) if d.secret else ""})
        groups.append({"id": gid, "title": title, "desc": desc, "fields": fields,
                       "testable": gid in TESTS, "last_test": tests.get(gid)})
    return groups


def paused_sources(db: Session) -> list[dict]:
    from app.models import Setting
    from app.services import browser

    out = []
    for row in db.query(Setting).filter(Setting.key.like("\\_blocked:%", escape="\\")):
        source = row.key.split(":", 1)[1]
        until = browser.blocked_until(db, source)
        if until:
            out.append({"source": source, "until": until})
    return out


@router.get("/settings")
def settings_page(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return render(request, "settings.html", {"groups": _view(db), "paused": paused_sources(db)})


@router.post("/settings/browser/resume", dependencies=[Depends(verify_csrf)])
def resume_source(request: Request, source: str = Form(...), user: User = Depends(current_user), db: Session = Depends(get_db)):
    from sqlalchemy import update

    from app.models import Job
    from app.pipeline.queue import now
    from app.services import browser

    browser.clear_blocked(db, source)
    # jobs postponed by this block run again right away
    db.execute(update(Job).where(Job.status == "queued", Job.last_error.like(f"{source} blocked%")).values(run_after=now()))
    audit(db, user, "browser.resume", source)
    db.commit()
    flash(request, f"Resumed {source}. If it blocks again it will pause again.")
    return RedirectResponse("/settings#browser", 303)


@router.post("/settings/{group}", dependencies=[Depends(verify_csrf)])
async def save_group(group: str, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    form = await request.form()
    changed = []
    for d in settings_store.DEFS:
        if d.group != group:
            continue
        if d.kind == "bool":
            new = "true" if form.get(d.key) else "false"
        elif d.secret:
            if form.get(f"{d.key}__clear"):
                new = ""
            else:
                new = str(form.get(d.key, "")).strip()
                if new == "":
                    continue  # blank secret field = keep existing value
        else:
            new = str(form.get(d.key, "")).strip()
        if new != settings_store.get_raw(db, d.key) or (new == "" and settings_store.is_set(db, d.key)):
            settings_store.set_value(db, d.key, new)
            changed.append(d.key)
    if changed:
        audit(db, user, "settings.update", group, ", ".join(changed))  # never log values
    db.commit()
    from app.redact import refresh

    refresh()  # new keys are hidden from error messages immediately
    flash(request, f"Saved {len(changed)} setting(s)." if changed else "No changes.")
    return RedirectResponse(f"/settings#{group}", 303)


# ------------------------------------------------------------------ connection tests
def _test_places(db):
    from app.services import places

    key = settings_store.get(db, "places_api_key")
    if not key:
        raise ProviderError("API key not set")
    res, _ = places.text_search(key, "hospital in Dhaka, Bangladesh", region=settings_store.get(db, "places_region_code"))
    return f"OK - {len(res)} results for a test query"


def _test_search(db):
    from app.pipeline.providers import get_search

    s = get_search(db)
    if s is None:
        raise ProviderError("provider 'none' or API key not set")
    return f"OK - {len(s('Square Hospital Dhaka managing director', 5))} results"


def _test_gemini(db):
    from app.pipeline.providers import get_llm

    from app.services.llm import list_gemini_models

    llm = get_llm(db)
    if llm is None:
        raise ProviderError("API key not set")
    try:
        out = llm.generate_json('Return exactly this JSON: {"ok": true}')
    except ProviderError as exc:
        try:
            models = [m for m in list_gemini_models(settings_store.get(db, "gemini_api_key")) if "flash" in m]
        except Exception:  # noqa: BLE001
            models = []
        hint = f" Models available to this key: {', '.join(models[:15])}" if models else ""
        raise ProviderError(f"{exc}.{hint}") from exc
    return f"OK - {llm.model} replied {out}"


def _test_telegram(db):
    from app.services.notifier import telegram_get_me, telegram_send

    token = settings_store.get(db, "telegram_bot_token")
    if not token:
        raise ProviderError("bot token not set")
    me = telegram_get_me(token)
    chat = settings_store.get(db, "telegram_chat_id")
    if chat:
        telegram_send(token, chat, "✅ Test message from BD Lead Generation admin panel.")
        return f"OK - bot @{me.get('username')} sent a test message"
    return f"Bot @{me.get('username')} is valid. Chat ID not set yet - use 'Detect chat ID'."


def _test_whatsapp(db):
    from app.services.notifier import whatsapp_ready, whatsapp_send

    if not whatsapp_ready(db):
        raise ProviderError("token, phone number id, recipient and template are all required")
    whatsapp_send(settings_store.snapshot(db), "Test message from BD Lead Generation")
    return "OK - template message sent"


def _test_smtp(db):
    from app.services.mailer import test_smtp

    s = settings_store.snapshot(db)
    if not (s["smtp_username"] and s["smtp_password"]):
        raise ProviderError("username and password required")
    return test_smtp(s)


def _test_sheets(db):
    from app.services import sheets

    sa, sid = settings_store.get(db, "sheets_service_account_json"), settings_store.get(db, "sheets_spreadsheet_id")
    if not (sa and sid):
        raise ProviderError("service account JSON and spreadsheet id required")
    return sheets.test_connection(sa, sid)


TESTS = {"places": _test_places, "search": _test_search, "gemini": _test_gemini, "telegram": _test_telegram,
         "whatsapp": _test_whatsapp, "smtp": _test_smtp, "sheets": _test_sheets}


@router.post("/settings/test/{group}", dependencies=[Depends(verify_csrf)])
def test_group(group: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    fn = TESTS.get(group)
    if fn is None:
        return JSONResponse({"ok": False, "message": "No test for this group"})
    try:
        ok, message = True, fn(db)
    except Exception as exc:  # noqa: BLE001 - show any failure to the admin
        ok, message = False, str(exc)[:500]
    from app.redact import redact

    message = redact(message)
    settings_store.record_test(db, group, ok, message)
    db.commit()
    return JSONResponse({"ok": ok, "message": message})


@router.post("/settings/telegram/detect", dependencies=[Depends(verify_csrf)])
def detect_chat(user: User = Depends(current_user), db: Session = Depends(get_db)):
    from app.services.notifier import telegram_detect_chats

    token = settings_store.get(db, "telegram_bot_token")
    if not token:
        return JSONResponse({"ok": False, "message": "Save the bot token first."})
    try:
        chats = telegram_detect_chats(token)
    except ProviderError as exc:
        from app.redact import redact

        return JSONResponse({"ok": False, "message": redact(exc)})
    if not chats:
        return JSONResponse({"ok": False, "message": "No chats found. Open your bot in Telegram, send /start, then try again."})
    chosen = chats[-1]
    settings_store.set_value(db, "telegram_chat_id", chosen["chat_id"])
    audit(db, user, "settings.update", "telegram", "telegram_chat_id (detected)")
    db.commit()
    return JSONResponse({"ok": True, "message": f"Saved chat ID {chosen['chat_id']} ({chosen['name'] or chosen['type']}).",
                         "chats": chats})
