import time
import uuid

from app.core.ids import new_id


def test_new_id_is_rfc9562_v7() -> None:
    value = new_id()
    assert value.version == 7
    assert value.variant == uuid.RFC_4122


def test_new_id_embeds_current_unix_milliseconds() -> None:
    before = time.time_ns() // 1_000_000
    value = new_id()
    after = time.time_ns() // 1_000_000
    assert before <= value.int >> 80 <= after


def test_new_ids_are_unique_and_time_ordered_across_milliseconds() -> None:
    first = new_id()
    time.sleep(0.002)
    second = new_id()
    assert first != second
    assert first < second
