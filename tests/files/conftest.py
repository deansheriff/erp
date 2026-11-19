"""Helpers for file tests: sample files and a scanner that flags the EICAR test string."""

import struct
import zlib
from collections.abc import Iterable, Iterator

import pytest

from app.files.scanner import ScannerUnavailable, ScanResult, set_scanner

# The industry-standard harmless test string every antivirus detects.
EICAR = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"


def png(width: int = 2, height: int = 2) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack("!I", len(data)) + kind + data + struct.pack("!I", zlib.crc32(kind + data))
        )

    raw = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack("!IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


SAFE_SVG = (
    b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
    b'<defs><linearGradient id="g"><stop offset="0" stop-color="#5A4BDB"/></linearGradient></defs>'
    b'<rect width="10" height="10" fill="url(#g)"/></svg>'
)


class EicarScanner:
    """Flags files containing the EICAR string; can be told the scanner is down."""

    def __init__(self) -> None:
        self.down = False

    def scan(self, chunks: Iterable[bytes]) -> ScanResult:
        if self.down:
            raise ScannerUnavailable("connection refused")
        data = b"".join(chunks)
        if EICAR in data:
            return ScanResult(False, "Eicar-Test-Signature")
        return ScanResult(True, "OK")


@pytest.fixture()
def scanner() -> Iterator[EicarScanner]:
    fake = EicarScanner()
    set_scanner(fake)
    yield fake
    set_scanner(None)
