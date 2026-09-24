"""`python -m app.bootstrap` - run DB migrations and seed defaults (called on container start)."""
from pathlib import Path

from alembic import command
from alembic.config import Config

from app.db import session_scope
from app.seed import seed

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(cfg, "head")
    with session_scope() as db:
        seed(db)
    print("database ready")


if __name__ == "__main__":
    main()
