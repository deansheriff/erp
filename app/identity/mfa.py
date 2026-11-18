"""TOTP two-step verification and recovery codes."""

import io
import secrets
import time

import pyotp
import qrcode
import qrcode.image.svg

ISSUER = "Kxmeleon"
TOTP_PERIOD = 30
RECOVERY_CODE_COUNT = 10
_RECOVERY_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"  # no 0/O/1/I confusion


def new_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(secret: str, account: str, organisation: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=account, issuer_name=f"{ISSUER} {organisation}")


def qr_svg(uri: str) -> str:
    """An inline SVG QR code (no image file, no inline styles)."""
    image = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2)
    buffer = io.BytesIO()
    image.save(buffer)
    svg = buffer.getvalue().decode("utf-8")
    return svg[svg.index("<svg") :]


def grouped(secret: str) -> str:
    """JBSWY3DPEHPK3PXP -> JBSW Y3DP EHPK 3PXP, for typing into an app by hand."""
    return " ".join(secret[i : i + 4] for i in range(0, len(secret), 4))


def verify_code(secret: str, code: str, last_step: int | None) -> int | None:
    """Return the matched time step if `code` is valid and not a replay, else None.

    Accepts the current step and one either side, to allow for clock drift."""
    digits = "".join(ch for ch in code if ch.isdigit())
    if len(digits) != 6:
        return None
    totp = pyotp.TOTP(secret)
    current = int(time.time()) // TOTP_PERIOD
    for step in (current, current - 1, current + 1):
        if secrets.compare_digest(totp.at(step * TOTP_PERIOD), digits):
            if last_step is not None and step <= last_step:
                return None
            return step
    return None


def new_recovery_codes() -> list[str]:
    codes = []
    for _ in range(RECOVERY_CODE_COUNT):
        raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(8))
        codes.append(f"{raw[:4]}-{raw[4:]}")
    return codes


def normalise_recovery_code(code: str) -> str:
    raw = "".join(ch for ch in code.upper() if ch.isalnum())
    return f"{raw[:4]}-{raw[4:]}" if len(raw) == 8 else raw
