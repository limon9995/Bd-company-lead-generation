import hmac
import secrets

import bcrypt
from cryptography.fernet import Fernet, InvalidToken

from app.config import config


class CryptoError(RuntimeError):
    pass


def _fernet() -> Fernet:
    if not config.app_secret_key:
        raise CryptoError("APP_SECRET_KEY is not set. Generate one and put it in .env (see README).")
    try:
        return Fernet(config.app_secret_key.encode())
    except (ValueError, TypeError) as exc:
        raise CryptoError("APP_SECRET_KEY is not a valid Fernet key.") from exc


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(token: str) -> str:
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise CryptoError("Could not decrypt a stored setting - was APP_SECRET_KEY changed?") from exc


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        return False


def new_token(nbytes: int = 24) -> str:
    return secrets.token_urlsafe(nbytes)


def tokens_equal(a: str, b: str) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(a, b)


def mask(value: str) -> str:
    if not value:
        return ""
    return "•" * 8 + value[-4:] if len(value) > 8 else "•" * 8
