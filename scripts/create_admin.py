"""Create (or reset the password of) an admin user.

    python -m scripts.create_admin you@example.com            # prompts for password
    docker compose exec web python -m scripts.create_admin you@example.com
"""
import getpass
import sys

from sqlalchemy import select

from app.db import session_scope
from app.models import User
from app.security import hash_password


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: python -m scripts.create_admin EMAIL")
    email = sys.argv[1].strip().lower()
    pw = getpass.getpass("Password (10+ chars): ")
    if len(pw) < 10:
        raise SystemExit("Password too short.")
    if pw != getpass.getpass("Repeat password: "):
        raise SystemExit("Passwords do not match.")
    with session_scope() as db:
        user = db.scalar(select(User).where(User.email == email))
        if user is None:
            db.add(User(email=email, password_hash=hash_password(pw)))
            print(f"Created admin {email}")
        else:
            user.password_hash, user.is_active = hash_password(pw), True
            print(f"Password reset for {email}")


if __name__ == "__main__":
    main()
