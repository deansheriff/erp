"""Type detection from content and SVG safety checks (no database)."""

import pytest

from app.core.formatting import format_size
from app.files import detect
from app.files.service import sanitise_filename
from tests.files.conftest import SAFE_SVG, png


def test_types_come_from_the_content_not_the_name() -> None:
    assert detect.detect(png()) == "image/png"
    assert detect.detect(b"%PDF-1.7\n%...") == "application/pdf"
    assert detect.detect(SAFE_SVG) == detect.SVG
    assert detect.detect(b"just some text pretending to be a png") is None


def test_a_safe_svg_passes() -> None:
    detect.check_svg(SAFE_SVG)


@pytest.mark.parametrize(
    "svg",
    [
        b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
        b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>',
        b'<svg xmlns="http://www.w3.org/2000/svg"><foreignObject><p>x</p></foreignObject></svg>',
        b'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">'
        b'<image xlink:href="https://evil.example/x.png"/></svg>',
        b'<svg xmlns="http://www.w3.org/2000/svg">'
        b'<a href="javascript:alert(1)"><text>x</text></a></svg>',
        b'<svg xmlns="http://www.w3.org/2000/svg">'
        b'<rect style="fill:url(https://evil.example/a)"/></svg>',
        b'<svg xmlns="http://www.w3.org/2000/svg"><style>@import url(x.css);</style></svg>',
        b'<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY x "boom">]>'
        b'<svg xmlns="http://www.w3.org/2000/svg">&x;</svg>',
        b"<html><svg/></html>",
    ],
)
def test_unsafe_svgs_are_refused(svg: bytes) -> None:
    with pytest.raises(detect.UnsafeSvg):
        detect.check_svg(svg)


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("logo.png", "logo.png"),
        ("../../etc/passwd", "passwd"),
        ("C:\\Users\\me\\logo.png", "logo.png"),
        ('bad<name>:"?.png', "bad_name_.png"),
        ("", "file"),
        (None, "file"),
    ],
)
def test_file_names_are_sanitised_for_display(raw: str | None, clean: str) -> None:
    assert sanitise_filename(raw) == clean


def test_sizes_are_readable() -> None:
    assert format_size(512) == "512 bytes"
    assert format_size(2 * 1024 * 1024) == "2 MB"
    assert format_size(1536) == "1.5 KB"
