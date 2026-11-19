"""The ClamAV client against a stand-in clamd speaking the INSTREAM protocol (ClamAV
itself runs in docker-compose and CI, not on the development machine)."""

import socket
import socketserver
import struct
import threading
from collections.abc import Iterator

import pytest

from app.files.scanner import ClamdScanner, ScannerUnavailable
from tests.files.conftest import EICAR


class _FakeClamd(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        command = b""
        while not command.endswith(b"\0"):
            command += self.request.recv(1)
        assert command == b"zINSTREAM\0"
        data = b""
        while True:
            size = struct.unpack("!L", self._exact(4))[0]
            if size == 0:
                break
            data += self._exact(size)
        reply = b"stream: Eicar-Test-Signature FOUND\0" if EICAR in data else b"stream: OK\0"
        self.request.sendall(reply)

    def _exact(self, n: int) -> bytes:
        out = b""
        while len(out) < n:
            out += self.request.recv(n - len(out))
        return out


@pytest.fixture()
def clamd() -> Iterator[tuple[str, int]]:
    server = socketserver.TCPServer(("127.0.0.1", 0), _FakeClamd)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address  # type: ignore[misc]
    server.shutdown()
    server.server_close()


def test_clean_streams_pass(clamd: tuple[str, int]) -> None:
    result = ClamdScanner(*clamd).scan([b"hello ", b"world"])
    assert result.clean and result.detail == "OK"


def test_infected_streams_report_the_signature(clamd: tuple[str, int]) -> None:
    result = ClamdScanner(*clamd).scan([b"prefix", EICAR[:20], EICAR[20:]])
    assert not result.clean and result.detail == "Eicar-Test-Signature"


def test_an_unreachable_scanner_is_reported_not_treated_as_clean() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with pytest.raises(ScannerUnavailable):
        ClamdScanner("127.0.0.1", port, timeout=1).scan([b"data"])
