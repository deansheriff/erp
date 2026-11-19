"""Object storage. The only module that talks to S3 (ADR 0016, import-linter)."""

from collections.abc import Iterator
from functools import lru_cache
from typing import TYPE_CHECKING, Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.core.config import get_settings

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client

CHUNK = 64 * 1024


@lru_cache(maxsize=1)
def client() -> "S3Client":
    s = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=s.s3_endpoint_url or None,
        region_name=s.s3_region,
        aws_access_key_id=s.s3_access_key.get_secret_value() or None,
        aws_secret_access_key=s.s3_secret_key.get_secret_value() or None,
        config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
    )


def quarantine_bucket() -> str:
    return get_settings().s3_quarantine_bucket


def clean_bucket() -> str:
    return get_settings().s3_clean_bucket


def ensure_buckets() -> None:
    """Create the buckets if missing (development and tests; production creates them in
    infrastructure)."""
    c = client()
    for bucket in (quarantine_bucket(), clean_bucket()):
        try:
            c.head_bucket(Bucket=bucket)
        except ClientError:
            c.create_bucket(Bucket=bucket)


def put(bucket: str, key: str, data: bytes, content_type: str) -> None:
    client().put_object(Bucket=bucket, Key=key, Body=data, ContentType=content_type)


def read(bucket: str, key: str, limit: int) -> bytes:
    """The whole object, for small objects only (at most `limit` bytes)."""
    body = client().get_object(Bucket=bucket, Key=key)["Body"]
    data: bytes = body.read(limit + 1)
    if len(data) > limit:
        raise ValueError("object larger than expected")
    return data


def stream(bucket: str, key: str) -> Iterator[bytes]:
    body: Any = client().get_object(Bucket=bucket, Key=key)["Body"]
    try:
        yield from body.iter_chunks(CHUNK)
    finally:
        body.close()


def copy(src_bucket: str, dst_bucket: str, key: str) -> None:
    client().copy_object(Bucket=dst_bucket, Key=key, CopySource={"Bucket": src_bucket, "Key": key})


def delete(bucket: str, key: str) -> None:
    client().delete_object(Bucket=bucket, Key=key)


def ping() -> bool:
    try:
        client().head_bucket(Bucket=clean_bucket())
    except Exception:  # noqa: BLE001  (used by health checks; any failure means "down")
        return False
    return True
