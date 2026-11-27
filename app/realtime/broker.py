"""Fan-out of small JSON notices to each user's channel (ADR 0034).

Services call `notify_after_commit(db, user_ids, event)`: the notice is published once
the transaction commits, and dropped if it rolls back. A WebSocket subscribes to its
user's channel with `subscribe(user_id)`.

Two brokers:

- Redis pub/sub, which works across processes and is required in production.
- An in-process broker, for tests and single-process development.

Notices name what changed. They never carry content the receiver would need permission
to see.
"""

import asyncio
import json
import logging
import threading
import uuid
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from typing import Any, Protocol

from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.config import get_settings

log = logging.getLogger(__name__)
PENDING = "kx_realtime_pending"


def channel(user_id: uuid.UUID) -> str:
    return f"user:{user_id}"


class Broker(Protocol):
    def publish(self, channel: str, message: str) -> None: ...

    def subscription(self, channel: str) -> Any:  # an async context manager of a queue
        ...


class MemoryBroker:
    """Process-local: subscribers are asyncio queues, fed thread-safely."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: dict[str, set[tuple[asyncio.AbstractEventLoop, asyncio.Queue[str]]]] = {}

    def publish(self, channel: str, message: str) -> None:
        with self._lock:
            targets = list(self._subscribers.get(channel, ()))
        for loop, queue in targets:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, message)
            except RuntimeError:  # that loop has closed
                continue

    @asynccontextmanager
    async def subscription(self, channel: str) -> AsyncIterator[asyncio.Queue[str]]:
        queue: asyncio.Queue[str] = asyncio.Queue()
        entry = (asyncio.get_running_loop(), queue)
        with self._lock:
            self._subscribers.setdefault(channel, set()).add(entry)
        try:
            yield queue
        finally:
            with self._lock:
                self._subscribers.get(channel, set()).discard(entry)


class RedisBroker:
    """Redis pub/sub: publish with the sync client, subscribe with the asyncio one."""

    def __init__(self, url: str) -> None:
        import redis  # noqa: PLC0415  (only needed when this broker is chosen)

        self._url = url
        self._client = redis.Redis.from_url(url)

    def publish(self, channel: str, message: str) -> None:
        try:
            self._client.publish(channel, message)
        except Exception:  # noqa: BLE001  (a notice is best-effort; pages work without it)
            log.warning("realtime_publish_failed", extra={"channel": channel})

    @asynccontextmanager
    async def subscription(self, channel: str) -> AsyncIterator[asyncio.Queue[str]]:
        import redis.asyncio as aioredis  # noqa: PLC0415

        client = aioredis.Redis.from_url(self._url)
        pubsub = client.pubsub()
        await pubsub.subscribe(channel)
        queue: asyncio.Queue[str] = asyncio.Queue()

        async def pump() -> None:
            async for item in pubsub.listen():
                if item.get("type") == "message":
                    data = item["data"]
                    queue.put_nowait(data.decode() if isinstance(data, bytes) else data)

        task = asyncio.create_task(pump())
        try:
            yield queue
        finally:
            task.cancel()
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()  # type: ignore[no-untyped-call]
            await client.aclose()


_broker: Broker | None = None
_broker_lock = threading.Lock()


def get_broker() -> Broker:
    global _broker  # noqa: PLW0603  (one broker per process)
    with _broker_lock:
        if _broker is None:
            settings = get_settings()
            if settings.realtime_backend == "memory":
                _broker = MemoryBroker()
            else:
                _broker = RedisBroker(settings.redis_url)
        return _broker


def set_broker(broker: Broker | None) -> None:
    """Tests swap in their own broker."""
    global _broker  # noqa: PLW0603
    with _broker_lock:
        _broker = broker


def check_configuration() -> None:
    """Called at start-up: the memory broker can't reach other processes."""
    settings = get_settings()
    if settings.is_production and settings.realtime_backend != "redis":
        raise RuntimeError("KX_REALTIME_BACKEND must be 'redis' in production")


def publish(user_ids: Iterable[uuid.UUID], event_data: dict[str, Any]) -> None:
    message = json.dumps(event_data, default=str)
    broker = get_broker()
    for user_id in set(user_ids):
        broker.publish(channel(user_id), message)


def notify_after_commit(
    db: Session, user_ids: Iterable[uuid.UUID], event_data: dict[str, Any]
) -> None:
    """Queue a notice to these users, sent when `db` commits."""
    db.info.setdefault(PENDING, []).append((list(user_ids), event_data))


@event.listens_for(Session, "after_commit")
def _send_pending(session: Session) -> None:
    pending = session.info.pop(PENDING, [])
    for user_ids, event_data in pending:
        publish(user_ids, event_data)


@event.listens_for(Session, "after_rollback")
def _drop_pending(session: Session) -> None:
    session.info.pop(PENDING, None)
