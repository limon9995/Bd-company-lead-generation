import time
from collections import defaultdict

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import audit, current_user, flash, render, verify_csrf
from app.models import User
from app.models._base import utcnow
from app.security import hash_password, verify_password

router = APIRouter()
_failures: dict[str, list[float]] = defaultdict(list)
MAX_FAILURES, WINDOW = 5, 15 * 60


def _blocked(ip: str) -> bool:
    now = time.time()
    _failures[ip] = [t for t in _failures[ip] if now - t < WINDOW]
    return len(_failures[ip]) >= MAX_FAILURES


@router.get("/login")
def login_page(request: Request, db: Session = Depends(get_db)):
    no_users = (db.scalar(select(func.count(User.id))) or 0) == 0
    return render(request, "login.html", {"no_users": no_users})


@router.post("/login", dependencies=[Depends(verify_csrf)])
def login(request: Request, email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    ip = request.client.host if request.client else "?"
    if _blocked(ip):
        flash(request, "Too many failed attempts. Try again in 15 minutes.", "err")
        return RedirectResponse("/login", 303)
    user = db.scalar(select(User).where(User.email == email.strip().lower()))
    if user is None or not user.is_active or not verify_password(password, user.password_hash):
        _failures[ip].append(time.time())
        flash(request, "Wrong email or password.", "err")
        return RedirectResponse("/login", 303)
    _failures.pop(ip, None)
    request.session.clear()
    request.session["uid"] = user.id
    request.session["email"] = user.email
    user.last_login = utcnow()
    audit(db, user, "login")
    db.commit()
    return RedirectResponse("/", 303)


@router.post("/logout", dependencies=[Depends(verify_csrf)])
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", 303)


@router.get("/users")
def users_page(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return render(request, "users.html", {"users": db.scalars(select(User).order_by(User.id)).all(), "me": user})


@router.post("/users", dependencies=[Depends(verify_csrf)])
def add_user(request: Request, email: str = Form(...), password: str = Form(...),
             user: User = Depends(current_user), db: Session = Depends(get_db)):
    email = email.strip().lower()
    if len(password) < 10:
        flash(request, "Password must be at least 10 characters.", "err")
    elif db.scalar(select(User).where(User.email == email)):
        flash(request, "That email already exists.", "err")
    else:
        db.add(User(email=email, password_hash=hash_password(password)))
        audit(db, user, "user.add", email)
        db.commit()
        flash(request, f"Added {email}.")
    return RedirectResponse("/users", 303)


@router.post("/users/{uid}/toggle", dependencies=[Depends(verify_csrf)])
def toggle_user(uid: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    target = db.get(User, uid)
    if target and target.id != user.id:
        target.is_active = not target.is_active
        audit(db, user, "user.toggle", target.email, f"active={target.is_active}")
        db.commit()
    else:
        flash(request, "You can't deactivate yourself.", "err")
    return RedirectResponse("/users", 303)


@router.post("/account/password", dependencies=[Depends(verify_csrf)])
def change_password(request: Request, current: str = Form(...), new: str = Form(...),
                    user: User = Depends(current_user), db: Session = Depends(get_db)):
    if not verify_password(current, user.password_hash):
        flash(request, "Current password is wrong.", "err")
    elif len(new) < 10:
        flash(request, "New password must be at least 10 characters.", "err")
    else:
        user.password_hash = hash_password(new)
        audit(db, user, "user.password")
        db.commit()
        flash(request, "Password changed.")
    return RedirectResponse("/users", 303)
