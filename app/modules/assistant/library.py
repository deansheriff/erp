"""The policy library (ADR 0035): uploading a policy or a new version (PDF, Word or text),
reading its text into passages of about 900 characters, and finding the passages that
match a question with Postgres full-text search over each policy's current version."""

import io
import re
import uuid
import zipfile
from dataclasses import dataclass
from datetime import date

from defusedxml import ElementTree
from pypdf import PdfReader
from pypdf.errors import PdfReadError
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.audit.service import record_by
from app.core.errors import NotFound, ValidationFailed
from app.files import detect
from app.files import service as files
from app.identity.models import User
from app.identity.schemas import Actor
from app.modules.assistant import common
from app.modules.assistant.common import Source
from app.modules.assistant.files import DOCX
from app.modules.assistant.models import Passage, Policy, PolicyVersion

PASSAGE = 900
TOP = 6
_WORD = re.compile(r"[A-Za-z]{2,40}")  # letters only: safe inside to_tsquery
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


# ------------------------------------------------------------------ reading text


def _pdf_text(data: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(data))
        return "\n\n".join((page.extract_text() or "") for page in reader.pages)
    except (PdfReadError, ValueError, KeyError) as exc:
        raise ValidationFailed(field_errors={"file": "This PDF couldn't be read."}) from exc


def _docx_text(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            xml = archive.read("word/document.xml")
        root = ElementTree.fromstring(xml, forbid_dtd=True)
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError) as exc:
        raise ValidationFailed(
            field_errors={"file": "This Word document couldn't be read."}
        ) from exc
    paragraphs = []
    for para in root.iter(f"{_W}p"):
        text = "".join(t.text or "" for t in para.iter(f"{_W}t"))
        if text.strip():
            paragraphs.append(text)
    return "\n\n".join(paragraphs)


def extract(data: bytes) -> str:
    kind = detect.detect(data)
    if kind == "application/pdf":
        text = _pdf_text(data)
    elif kind == DOCX:
        text = _docx_text(data)
    elif kind == "text/plain":
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("latin-1")
    else:
        raise ValidationFailed(
            field_errors={"file": "Upload a PDF, a Word document or a text file."}
        )
    text = text.replace("\x00", "").strip()
    if len(re.sub(r"\s", "", text)) < 40:
        raise ValidationFailed(
            field_errors={
                "file": "No text could be read from this file. Scanned PDFs can't be used."
            }
        )
    return text


def passages(text: str) -> list[str]:
    """Paragraphs packed into passages of about 900 characters (long ones split)."""
    out: list[str] = []
    current = ""
    for raw in re.split(r"\n\s*\n", text):
        para = re.sub(r"[ \t]+", " ", raw.strip())
        while len(para) > PASSAGE * 1.5:
            cut = para.rfind(". ", 0, PASSAGE) + 1 or PASSAGE
            out.append(para[:cut].strip())
            para = para[cut:].strip()
        if current and len(current) + len(para) > PASSAGE:
            out.append(current)
            current = ""
        current = f"{current}\n\n{para}".strip() if para else current
    if current:
        out.append(current)
    return [p for p in out if p]


# ------------------------------------------------------------------ uploading


@dataclass(frozen=True, slots=True)
class PolicyInput:
    policy_id: str  # blank for a new policy
    title: str
    version: str
    effective_on: date | None


def add_version(
    db: Session,
    actor: Actor,
    data: PolicyInput,
    *,
    filename: str | None,
    mime: str | None,
    content: bytes,
) -> uuid.UUID:
    common.require(db, actor, common.MANAGE_POLICIES)
    errors: dict[str, str] = {}
    existing = db.get(Policy, common_uuid(data.policy_id)) if data.policy_id else None
    if data.policy_id and existing is None:
        raise NotFound()
    title = " ".join(data.title.split())[:160]
    if existing is None and not title:
        errors["title"] = "Give the policy a title."
    elif existing is None and db.scalar(
        select(Policy.id).where(func.lower(Policy.title) == title.lower())
    ):
        errors["title"] = "There's already a policy with this title. Add a new version to it."
    version = data.version.strip()[:20]
    if not version:
        errors["version"] = "Enter the version, like 3 or 2026-01."
    if data.effective_on is None:
        errors["effective_on"] = "Enter the date it takes effect."
    if not content:
        errors["file"] = "Choose the policy file."
    if errors:
        raise ValidationFailed(field_errors=errors)
    assert data.effective_on is not None  # noqa: S101
    chunks = passages(extract(content))
    policy = existing or Policy(title=title)
    if existing is None:
        db.add(policy)
        db.flush()
    file_id = files.upload(
        db,
        actor,
        purpose_key="assistant_policy",
        entity_id=str(policy.id),
        filename=filename,
        declared_mime=mime,
        data=content,
    )
    db.execute(
        update(PolicyVersion).where(PolicyVersion.policy_id == policy.id).values(is_current=False)
    )
    row = PolicyVersion(
        policy_id=policy.id,
        version=version,
        effective_on=data.effective_on,
        file_id=file_id,
        is_current=True,
        passages=len(chunks),
        created_by=actor.user_id,
    )
    db.add(row)
    db.flush()
    db.add_all(Passage(version_id=row.id, ordinal=i, body=body) for i, body in enumerate(chunks))
    db.flush()
    record_by(
        db,
        actor,
        "assistant.policy.add",
        entity_type="assistant_policy",
        entity_id=str(policy.id),
        changes={"version": version, "passages": len(chunks)},
    )
    return policy.id


def common_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class PolicyRow:
    policy: Policy
    current: PolicyVersion | None
    older: list[PolicyVersion]
    updated_by: str | None


def library(db: Session, actor: Actor) -> list[PolicyRow]:
    """Every policy with its current and earlier versions (anyone may read them)."""
    common.require(db, actor, common.ASK)
    versions: dict[uuid.UUID, list[PolicyVersion]] = {}
    for v in db.scalars(select(PolicyVersion).order_by(PolicyVersion.created_at.desc())):
        versions.setdefault(v.policy_id, []).append(v)
    names = dict(
        db.execute(
            select(User.id, User.full_name).where(
                User.id.in_({v.created_by for vs in versions.values() for v in vs if v.created_by})
            )
        ).all()
    )
    rows = []
    for p in db.scalars(select(Policy).order_by(Policy.title)):
        vs = versions.get(p.id, [])
        current = next((v for v in vs if v.is_current), None)
        rows.append(
            PolicyRow(
                p,
                current,
                [v for v in vs if v is not current],
                names.get(current.created_by) if current and current.created_by else None,
            )
        )
    return rows


# ------------------------------------------------------------------ finding passages


def search(db: Session, question: str, limit: int = TOP) -> list[Source]:
    """The best-matching passages from current versions, as numbered sources."""
    words = {w.lower() for w in _WORD.findall(question)}
    if not words:
        return []
    query = func.to_tsquery("english", " | ".join(sorted(words)))
    rank = func.ts_rank_cd(Passage.search, query)
    rows = db.execute(
        select(Passage, PolicyVersion, Policy)
        .join(PolicyVersion, PolicyVersion.id == Passage.version_id)
        .join(Policy, Policy.id == PolicyVersion.policy_id)
        .where(PolicyVersion.is_current, Passage.search.op("@@")(query))
        .order_by(rank.desc(), Passage.ordinal)
        .limit(limit)
    ).all()
    return [
        Source(
            f"S{i}",
            f"{policy.title} v{version.version} (effective {version.effective_on:%d %b %Y})",
            passage.body,
            f"/files/{version.file_id}",
        )
        for i, (passage, version, policy) in enumerate(rows, start=1)
    ]
