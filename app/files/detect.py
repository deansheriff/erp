"""Working out what a file really is from its bytes (never from the name or the client's
claim), and making sure SVGs can't carry scripts (ADR 0016)."""

import re
from xml.etree.ElementTree import Element

import filetype
from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

SVG = "image/svg+xml"
_SVG_NS = "{http://www.w3.org/2000/svg}"
_XLINK_HREF = "{http://www.w3.org/1999/xlink}href"
_FORBIDDEN_TAGS = {"script", "foreignobject", "iframe", "embed", "object", "handler", "listener"}
_SAFE_HREF = re.compile(r"^#[A-Za-z][\w.-]*$")
_URL_IN_STYLE = re.compile(r"url\s*\(\s*['\"]?\s*(?!#)", re.IGNORECASE)


class UnsafeSvg(ValueError):
    """`message` is written for the person uploading the file."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def detect(data: bytes) -> str | None:
    """The MIME type from the content, or None when unrecognised."""
    kind = filetype.guess(data)
    if kind is not None:
        return str(kind.mime)
    if _looks_like_svg(data):
        return SVG
    return None


def _looks_like_svg(data: bytes) -> bool:
    head = data[:4096].lstrip(b"\xef\xbb\xbf").lstrip().lower()
    return head.startswith((b"<?xml", b"<svg", b"<!--")) and b"<svg" in data[:65536].lower()


def _local(tag: str) -> str:
    return tag.split("}", 1)[-1].lower()


def check_svg(data: bytes) -> None:
    """Refuse SVGs that could run code or load anything from elsewhere."""
    try:
        root = ElementTree.fromstring(data, forbid_dtd=True, forbid_entities=True)
    except (DefusedXmlException, ElementTree.ParseError) as exc:
        raise UnsafeSvg("This SVG couldn't be read safely.") from exc
    if _local(root.tag) != "svg":
        raise UnsafeSvg("This isn't an SVG image.")
    for element in root.iter():
        _check_element(element)


def _check_element(element: Element) -> None:
    if _local(element.tag) in _FORBIDDEN_TAGS:
        raise UnsafeSvg("SVGs with scripts or embedded content aren't allowed.")
    for name, value in element.attrib.items():
        attr = _local(name)
        if attr.startswith("on"):
            raise UnsafeSvg("SVGs with event handlers aren't allowed.")
        if (attr == "href" or name == _XLINK_HREF) and not _SAFE_HREF.match(value.strip()):
            raise UnsafeSvg("SVGs may only link to their own parts.")
        if attr == "style" and _URL_IN_STYLE.search(value):
            raise UnsafeSvg("SVGs may not load anything from elsewhere.")
    if (
        _local(element.tag) == "style"
        and element.text
        and (_URL_IN_STYLE.search(element.text) or "@import" in element.text.lower())
    ):
        raise UnsafeSvg("SVGs may not load anything from elsewhere.")
