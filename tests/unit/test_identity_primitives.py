import time

import pyotp

from app.core.secrets import decrypt, encrypt, hash_token, new_token
from app.identity import mfa
from app.identity.passwords import hash_password, password_problems, verify_password


def test_passwords_hash_with_argon2id_and_verify() -> None:
    hashed = hash_password("Correct-Horse-Battery-71")
    assert hashed.startswith("$argon2id$")
    assert verify_password(hashed, "Correct-Horse-Battery-71")
    assert not verify_password(hashed, "correct-horse-battery-71")
    assert not verify_password(None, "anything")


def test_password_rule_confirmed_2026_11_17() -> None:
    def ok(pw: str, personal: tuple[str, ...] = ()) -> bool:
        return not password_problems(pw, min_length=12, personal=personal)

    assert ok("Correct-Horse-Battery-71")
    assert not ok("Short-1")  # under 12 characters
    assert not ok("Password2024!!")  # common word underneath
    assert not ok("123456789012")  # sequence
    assert not ok("aaaaaaaaaaaaaa")  # one repeated character
    assert not ok("Fatima-Lagoon-2026", personal=("Fatima Bello", "fatima.bello@x.ng"))
    assert ok("Saffron-Lagoon-Tiger-88", personal=("Fatima Bello",))


def test_totp_accepts_drift_and_rejects_replays() -> None:
    secret = mfa.new_secret()
    totp = pyotp.TOTP(secret)
    now = int(time.time())
    step = mfa.verify_code(secret, totp.at(now), None)
    assert step is not None
    assert mfa.verify_code(secret, totp.at(now - 30), None) is not None  # one step behind
    assert mfa.verify_code(secret, totp.at(now), step) is None  # replay
    assert mfa.verify_code(secret, "12345", None) is None


def test_recovery_codes_are_unique_and_readable() -> None:
    codes = mfa.new_recovery_codes()
    assert len(codes) == 10 == len(set(codes))
    assert all(len(c) == 9 and c[4] == "-" for c in codes)
    assert not any(ch in "".join(codes) for ch in "01OI")
    assert mfa.normalise_recovery_code(codes[0].lower().replace("-", " ")) == codes[0]


def test_qr_code_is_inline_svg_without_styles() -> None:
    svg = mfa.qr_svg(mfa.provisioning_uri(mfa.new_secret(), "a@b.ng", "Adeyemi"))
    assert svg.startswith("<svg")
    assert "style=" not in svg


def test_encryption_round_trip_and_token_hashing() -> None:
    assert decrypt(encrypt("smtp-password")) == "smtp-password"
    token = new_token()
    assert len(token) >= 40
    assert hash_token(token) == hash_token(token) != token
