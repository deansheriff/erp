"""Source-code rules from the build brief, checked by walking the AST of app/."""

import ast
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "app"
FILES_PACKAGE = APP / "files"
RAW_SQL_ALLOWED = {APP / "core" / "db.py"}


def _python_files(base: Path = APP) -> Iterator[Path]:
    yield from sorted(p for p in base.rglob("*.py") if "__pycache__" not in p.parts)


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def test_no_session_query() -> None:
    offenders = [
        f"{_rel(p)}:{node.lineno}"
        for p in _python_files()
        for node in ast.walk(_tree(p))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "query"
    ]
    assert offenders == [], "Use select(); session.query() is banned"


def test_raw_sql_only_in_db_module() -> None:
    offenders = []
    for p in _python_files():
        if p in RAW_SQL_ALLOWED:
            continue
        for node in ast.walk(_tree(p)):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "sqlalchemy"
                and any(a.name == "text" for a in node.names)
            ):
                offenders.append(_rel(p))
    assert offenders == []


def test_exception_text_never_reaches_a_response() -> None:
    """Inside `except ... as exc:` blocks, the exception must not be stringified."""
    offenders = []
    for p in _python_files():
        if p.name == "cli.py":  # operator console output, not an HTTP response
            continue
        for handler in (n for n in ast.walk(_tree(p)) if isinstance(n, ast.ExceptHandler)):
            if not handler.name:
                continue
            for node in ast.walk(handler):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id in {"str", "repr", "format"}
                    and any(isinstance(a, ast.Name) and a.id == handler.name for a in node.args)
                ) or (
                    isinstance(node, ast.FormattedValue)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == handler.name
                ):
                    offenders.append(f"{_rel(p)}:{node.lineno}")
    assert offenders == [], "Never put exception text in output (ADR 0008)"


def test_only_the_files_package_touches_storage_or_disk() -> None:
    banned_modules = {"boto3", "botocore", "shutil", "tempfile"}
    write_methods = {"write_text", "write_bytes", "mkdir", "touch", "unlink", "rename"}
    offenders = []
    for p in _python_files():
        if FILES_PACKAGE in p.parents:
            continue
        for node in ast.walk(_tree(p)):
            if isinstance(node, ast.Import):
                names = {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = {node.module.split(".")[0]}
            else:
                names = set()
            if names & banned_modules:
                offenders.append(f"{_rel(p)}:{node.lineno} imports {names & banned_modules}")
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr in write_methods:
                    offenders.append(f"{_rel(p)}:{node.lineno} .{func.attr}()")
                if isinstance(func, ast.Name) and func.id == "open":
                    mode = node.args[1] if len(node.args) > 1 else None
                    for kw in node.keywords:
                        if kw.arg == "mode":
                            mode = kw.value
                    if not (isinstance(mode, ast.Constant) and mode.value in ("r", "rb", "rt")):
                        offenders.append(f"{_rel(p)}:{node.lineno} open() for writing")
    assert offenders == [], "Store files through app/files only (brief section 5.2)"


def test_modules_import_each_other_only_through_service() -> None:
    modules_dir = APP / "modules"
    offenders = []
    for p in _python_files(modules_dir):
        own = p.relative_to(modules_dir).parts[0]
        for node in ast.walk(_tree(p)):
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            parts = node.module.split(".")
            if parts[:2] != ["app", "modules"] or len(parts) < 3 or parts[2] == own:
                continue
            target = parts[3:]
            imports_service_module = target == ["service"]
            imports_service_from_package = target == [] and all(
                a.name == "service" for a in node.names
            )
            if not (imports_service_module or imports_service_from_package):
                offenders.append(f"{_rel(p)}:{node.lineno} imports {node.module}")
    assert offenders == [], "Modules talk to each other only via <module>/service.py"


def test_services_never_import_the_web_layer() -> None:
    banned = {"fastapi", "starlette", "jinja2"}
    offenders = []
    for p in _python_files():
        if p.name not in {"service.py", "models.py", "schemas.py", "policies.py"}:
            continue
        for node in ast.walk(_tree(p)):
            mods: set[str] = set()
            if isinstance(node, ast.Import):
                mods = {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods = {node.module.split(".")[0]}
                if node.module.startswith("app.web"):
                    mods.add("app.web")
            if mods & (banned | {"app.web"}):
                offenders.append(f"{_rel(p)}:{node.lineno}")
    assert offenders == []
