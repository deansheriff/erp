"""Random tokens, token hashing and reversible encryption (ADR 0011)."""

import hashlib
import hmac
import secrets
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.core.config import get_settings


def new_token(nbytes: int = 32) -> str:
    """A URL-safe random token with `nbytes` of entropy."""
    return secrets.token_urlsafe(nbytes)


def hash_token(token: str) -> str:
    """SHA-256 hex digest. Used for high-entropy tokens only, never for passwords."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def tokens_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


class DecryptionFailed(Exception):
    """The value was encrypted with a key that is no longer configured."""


@lru_cache(maxsize=1)
def _fernet() -> MultiFernet:
    keys = [k.strip() for k in get_settings().encryption_key.get_secret_value().split(",")]
    return MultiFernet([Fernet(k) for k in keys if k])


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise DecryptionFailed from exc


def generate_encryption_key() -> str:
    return Fernet.generate_key().decode("ascii")
