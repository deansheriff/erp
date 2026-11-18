"""Jinja2 environment shared by every web route."""

import hashlib
from datetime import date, datetime, timedelta
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, FileSystemLoader

from app.core.config import get_settings
from app.core.formatting import format_date, format_datetime, format_money

WEB_DIR = Path(__file__).parent
APP_DIR = WEB_DIR.parent
STATIC_DIR = WEB_DIR / "static"


def _template_dirs() -> list[Path]:
    """app/web/templates first, then every package's own templates (app/<pkg>/templates
    and app/modules/<module>/templates). Mail templates are rendered separately."""
    dirs = [WEB_DIR / "templates"]
    candidates = list(APP_DIR.glob("*/templates")) + list((APP_DIR / "modules").glob("*/templates"))
    dirs.extend(
        sorted(p for p in candidates if p.is_dir() and p.parent.name not in ("web", "mail"))
    )
    return dirs


def _money(amount: Decimal, currency: str = "NGN") -> str:
    return format_money(amount, currency)


def _date(value: date) -> str:
    return format_date(value)


def _datetime(value: datetime) -> str:
    return format_datetime(value, get_settings().timezone)


def _period_end(value: datetime) -> str:
    """The last moment of a period that ends (exclusively) at `value`: a delegation until
    30 Nov is stored as ending at 1 Dec 00:00 but shown as `30 Nov 2026, 23:59 WAT`."""
    return format_datetime(value - timedelta(minutes=1), get_settings().timezone)


@lru_cache(maxsize=64)
def _hashed_version(path: str) -> str:
    return hashlib.sha256((STATIC_DIR / path).read_bytes()).hexdigest()[:12]


def asset_version(path: str) -> str:
    """Cache-busting token for a static file: content hash in production, mtime in dev."""
    if get_settings().is_production:
        return _hashed_version(path)
    try:
        return str((STATIC_DIR / path).stat().st_mtime_ns)
    except FileNotFoundError:
        return "missing"


def build_templates() -> Jinja2Templates:
    templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))
    env = templates.env
    env.loader = ChoiceLoader([FileSystemLoader(str(d)) for d in _template_dirs()])
    env.autoescape = True
    env.trim_blocks = True
    env.lstrip_blocks = True
    filters: dict[str, Any] = {
        "money": _money,
        "date": _date,
        "datetime": _datetime,
        "period_end": _period_end,
    }
    env.filters.update(filters)
    env.globals["asset_version"] = asset_version
    return templates


templates = build_templates()
