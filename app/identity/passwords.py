"""Password hashing (Argon2id, ADR 0011) and the password rule confirmed on 2026-11-17:
at least 12 characters, not common, not built from the person's own details."""

import re
from collections.abc import Iterable

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_hasher = PasswordHasher()  # argon2id with the library's recommended parameters

# A short list of the passwords most often seen in breaches and in Nigerian contexts.
# Matching is on the normalised password (lower case, digits and symbols stripped), so
# "Password2024!" is caught by "password".
_COMMON = frozenset(
    {
        "password",
        "passw0rd",
        "qwerty",
        "qwertyuiop",
        "asdfgh",
        "asdfghjkl",
        "zxcvbnm",
        "letmein",
        "welcome",
        "admin",
        "administrator",
        "iloveyou",
        "monkey",
        "dragon",
        "football",
        "baseball",
        "sunshine",
        "princess",
        "master",
        "shadow",
        "superman",
        "trustno",
        "abc",
        "abcdef",
        "abcdefgh",
        "changeme",
        "default",
        "secret",
        "login",
        "nigeria",
        "naija",
        "lagos",
        "abuja",
        "jesus",
        "jesuschrist",
        "godisgood",
        "blessing",
        "favour",
        "gracious",
        "chelsea",
        "arsenal",
        "manchester",
        "barcelona",
        "kxmeleon",
        "erp",
        "company",
        "office",
        "summer",
        "winter",
        "spring",
        "autumn",
    }
)
_SEQUENCES = ("0123456789", "abcdefghijklmnopqrstuvwxyz", "qwertyuiopasdfghjklzxcvbnm")

# A real hash of a random string, used to spend the same time on unknown accounts.
_DUMMY_HASH = _hasher.hash("kxmeleon-timing-equaliser-not-a-real-password")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    """Constant-effort check: unknown users still cost one Argon2 verification."""
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and bool(password_hash)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


def _normalise(text: str) -> str:
    return re.sub(r"[^a-z]", "", text.lower())


def _personal_terms(values: Iterable[str]) -> set[str]:
    terms: set[str] = set()
    for value in values:
        for part in re.split(r"[\s@._\-+]+", value.lower()):
            normalised = _normalise(part)
            if len(normalised) >= 3:
                terms.add(normalised)
    return terms


def password_problems(password: str, *, min_length: int, personal: Iterable[str] = ()) -> list[str]:
    """Plain-language reasons the password isn't allowed. Empty means it's fine."""
    problems: list[str] = []
    if len(password) < min_length:
        problems.append(f"Use at least {min_length} characters.")
    lowered = password.lower()
    letters = _normalise(password)
    if letters in _COMMON or lowered in _COMMON or len(set(lowered)) <= 2:
        problems.append("That password is too common. Choose something less predictable.")
    elif any(lowered in seq or (len(lowered) >= 6 and lowered[:6] in seq) for seq in _SEQUENCES):
        problems.append("Avoid simple sequences like 123456 or abcdef.")
    if any(term in letters for term in _personal_terms(personal)):
        problems.append("Don't use your name, email address or the organisation's name.")
    return problems
