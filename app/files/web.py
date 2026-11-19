"""Reading an upload from the request as it arrives (ADR 0016). The size limit is
enforced while the bytes stream in, and nothing is written to disk: Starlette's own form
parser would spool large parts to temporary files.

Routes that accept a file declare
`Upload = Annotated[ReceivedUpload, Depends(upload_body(limit))]`.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from fastapi import Request
from python_multipart.multipart import MultipartParser, parse_options_header

from app.core.errors import ValidationFailed
from app.core.formatting import format_size

FIELD_LIMIT = 8 * 1024  # text fields in an upload form are small
OVERHEAD = 64 * 1024  # headers and boundaries around the file


class UploadTooLarge(ValidationFailed):
    pass


@dataclass(slots=True)
class ReceivedUpload:
    filename: str | None = None
    content_type: str | None = None
    data: bytes = b""
    fields: dict[str, str] = field(default_factory=dict)
    # Set instead of raising, so the route can show the problem on its own page.
    errors: dict[str, str] = field(default_factory=dict)


def _too_large(limit: int) -> UploadTooLarge:
    return UploadTooLarge(
        field_errors={"file": f"This file is too large. The limit is {format_size(limit)}."}
    )


class _Collector:
    """Callbacks for python-multipart: gathers one file part and small text fields."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.result = ReceivedUpload()
        self.seen_file = False
        self.headers: dict[bytes, bytes] = {}
        self.field = bytearray()
        self.value = bytearray()
        self.body = bytearray()

    def _disposition(self) -> dict[bytes, bytes]:
        return parse_options_header(self.headers.get(b"content-disposition", b""))[1]

    def on_part_begin(self) -> None:
        self.headers = {}
        self.body = bytearray()

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self.field += data[start:end]

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self.value += data[start:end]

    def on_header_end(self) -> None:
        self.headers[bytes(self.field).lower()] = bytes(self.value)
        self.field.clear()
        self.value.clear()

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        is_file = b"filename" in self._disposition()
        if len(self.body) + (end - start) > (self.limit if is_file else FIELD_LIMIT):
            if is_file:
                raise _too_large(self.limit)
            raise ValidationFailed("A form field was too long.")
        self.body += data[start:end]

    def on_part_end(self) -> None:
        disposition = self._disposition()
        if b"filename" in disposition:
            if self.seen_file:
                raise ValidationFailed(field_errors={"file": "Upload one file at a time."})
            self.seen_file = True
            self.result.filename = disposition[b"filename"].decode("utf-8", "replace")
            content_type = self.headers.get(b"content-type", b"").decode("latin-1")
            self.result.content_type = content_type or None
            self.result.data = bytes(self.body)
        elif name := disposition.get(b"name", b"").decode("utf-8", "replace"):
            self.result.fields[name] = bytes(self.body).decode("utf-8", "replace")

    def callbacks(self) -> dict[str, Callable[..., None]]:
        return {
            "on_part_begin": self.on_part_begin,
            "on_header_field": self.on_header_field,
            "on_header_value": self.on_header_value,
            "on_header_end": self.on_header_end,
            "on_part_data": self.on_part_data,
            "on_part_end": self.on_part_end,
        }


def upload_body(limit: int) -> Callable[[Request], Awaitable[ReceivedUpload]]:
    """A dependency that reads one file (at most `limit` bytes) and small text fields."""

    async def read(request: Request) -> ReceivedUpload:
        try:
            return await _read(request, limit)
        except ValidationFailed as exc:
            return ReceivedUpload(errors=exc.field_errors or {"file": exc.message})

    return read


async def _read(request: Request, limit: int) -> ReceivedUpload:
    content_type, params = parse_options_header(request.headers.get("content-type", ""))
    boundary = params.get(b"boundary")
    if content_type != b"multipart/form-data" or not boundary:
        raise ValidationFailed(field_errors={"file": "Choose a file to upload."})
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit + OVERHEAD:
        raise _too_large(limit)
    collector = _Collector(limit)
    parser = MultipartParser(boundary, collector.callbacks())  # type: ignore[arg-type]
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > limit + OVERHEAD:
            raise _too_large(limit)
        parser.write(chunk)
    parser.finalize()
    return collector.result
