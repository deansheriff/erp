"""Identifier generation (ADR 0006)."""

import os
import time
import uuid

_MASK_48 = (1 << 48) - 1
_MASK_12 = (1 << 12) - 1
_MASK_62 = (1 << 62) - 1


def new_id() -> uuid.UUID:
    """Return a UUIDv7 (RFC 9562): 48-bit Unix milliseconds, then 74 random bits."""
    unix_ms = time.time_ns() // 1_000_000
    rand = int.from_bytes(os.urandom(10), "big")
    rand_a = rand & _MASK_12
    rand_b = (rand >> 12) & _MASK_62
    value = (
        (unix_ms & _MASK_48) << 80
        | 0x7 << 76  # version
        | rand_a << 64
        | 0b10 << 62  # RFC 4122 variant
        | rand_b
    )
    return uuid.UUID(int=value)
