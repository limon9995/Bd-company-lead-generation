import os
import tempfile

from cryptography.fernet import Fernet

_tmp = tempfile.mkdtemp()
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_tmp}/test.db")
os.environ["APP_SECRET_KEY"] = Fernet.generate_key().decode()
os.environ["SESSION_SECRET"] = "test-session-secret"
os.environ["BASE_URL"] = "http://testserver"
_chromium = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
if os.path.exists(_chromium):
    os.environ.setdefault("CHROMIUM_EXECUTABLE", _chromium)

import pytest  # noqa: E402

import app.models  # noqa: E402,F401
from app import settings_store  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.seed import seed  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        seed(db)
    from app.services.usage import limiter

    limiter._next.clear()
    yield


@pytest.fixture
def db():
    s = SessionLocal()
    yield s
    s.close()


@pytest.fixture
def configure(db):
    def _set(**kv):
        for k, v in kv.items():
            settings_store.set_value(db, k, str(v))
        db.commit()
    return _set
