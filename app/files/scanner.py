"""Virus scanning (ADR 0016): ClamAV over its INSTREAM protocol, or an accept-all
scanner for development and tests that the app refuses to use in production."""

import socket
import struct
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from app.core.config import get_settings


@dataclass(frozen=True, slots=True)
class ScanResult:
    clean: bool
    detail: str  # "OK" or the signature found


class ScannerUnavailable(RuntimeError):
    """The scanner couldn't be reached. The file stays pending and is retried."""


class Scanner(Protocol):
    def scan(self, chunks: Iterable[bytes]) -> ScanResult: ...


class ClamdScanner:
    """Talks to clamd: `zINSTREAM`, then length-prefixed chunks, then a zero length."""

    def __init__(self, host: str, port: int, timeout: float = 30.0) -> None:
        self.host, self.port, self.timeout = host, port, timeout

    def scan(self, chunks: Iterable[bytes]) -> ScanResult:
        try:
            with socket.create_connection((self.host, self.port), timeout=self.timeout) as conn:
                conn.sendall(b"zINSTREAM\0")
                for chunk in chunks:
                    if chunk:
                        conn.sendall(struct.pack("!L", len(chunk)) + chunk)
                conn.sendall(struct.pack("!L", 0))
                reply = b""
                while not reply.endswith(b"\0"):
                    part = conn.recv(4096)
                    if not part:
                        break
                    reply += part
        except OSError as exc:
            raise ScannerUnavailable(f"clamd at {self.host}:{self.port} is unreachable") from exc
        text = reply.rstrip(b"\0").decode("utf-8", "replace").strip()
        # "stream: OK" or "stream: Eicar-Signature FOUND" or "... ERROR"
        if text.endswith("OK"):
            return ScanResult(True, "OK")
        if text.endswith("FOUND"):
            return ScanResult(False, text.removeprefix("stream:").removesuffix("FOUND").strip())
        raise ScannerUnavailable(text or "empty reply from clamd")


class AcceptAllScanner:
    """Development and tests only."""

    def scan(self, chunks: Iterable[bytes]) -> ScanResult:
        for _ in chunks:
            pass
        return ScanResult(True, "OK (not scanned: development scanner)")


_override: Scanner | None = None


def set_scanner(scanner: Scanner | None) -> None:
    """Tests swap in their own scanner."""
    global _override  # noqa: PLW0603
    _override = scanner


def get_scanner() -> Scanner:
    if _override is not None:
        return _override
    s = get_settings()
    if s.scanner == "accept_all":
        return AcceptAllScanner()
    return ClamdScanner(s.clamd_host, s.clamd_port)


def check_configuration() -> None:
    """Called at start-up: never run without a real scanner in production."""
    s = get_settings()
    if s.is_production and s.scanner != "clamd":
        raise RuntimeError("KX_SCANNER must be 'clamd' in production")
