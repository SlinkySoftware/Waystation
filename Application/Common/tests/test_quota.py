"""Quota thresholds, alert wording and the low-disk guard floor (T-O03, SPECIFICATION §4.8)."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from waystation_common import quota
from waystation_common.db import storage_guard
from waystation_common.db import usage as usage_db
from waystation_common.errors import InsufficientStorage
from waystation_common.events.schema import EventType
from waystation_common.storage import BlobStore

NOW = datetime(2026, 11, 10, tzinfo=UTC)


@pytest.mark.parametrize(
    ('used', 'limit', 'level'),
    [(0, None, 0), (10, 0, 0), (79, 100, 0), (80, 100, 80), (99, 100, 80), (100, 100, 100), (250, 100, 100)],
)
def test_level_of(used: int, limit: int | None, level: int) -> None:
    assert usage_db.level_of(used, limit) == level


def test_usage_from_doc_clamps() -> None:
    usage = usage_db.Usage.from_doc({'_id': 'r', 'bytes': -5, 'assets': None, 'alert_level': 80})
    assert (usage.repo_id, usage.bytes, usage.assets, usage.alert_level) == ('r', 0, 0, 80)


@pytest.mark.parametrize(
    ('n', 'text'),
    [(0, '0 B'), (1023, '1023 B'), (1536, '1.5 KiB'), (5 * 1024**3, '5.0 GiB'), (3 * 1024**5, '3072.0 TiB')],
)
def test_human_bytes(n: int, text: str) -> None:
    assert quota.human_bytes(n) == text


def test_quota_events() -> None:
    common = {'repo_id': 'id', 'repo': 'apps', 'format': 'docker', 'quota': 1024**3, 'now': NOW}
    warning = quota.quota_event(level=80, used=900 * 1024**2, mode='hard', **common)  # type: ignore[arg-type]
    assert warning.type is EventType.QUOTA_WARNING
    assert warning.detail['summary'] == 'apps reached 80 % of its 1.0 GiB quota (900.0 MiB stored)'
    hard = quota.quota_event(level=100, used=1024**3, mode='hard', **common)  # type: ignore[arg-type]
    assert hard.type is EventType.QUOTA_EXCEEDED and 'refused' in hard.detail['summary']
    soft = quota.quota_event(level=100, used=1024**3, mode='soft', **common)  # type: ignore[arg-type]
    assert 'nothing is refused' in soft.detail['summary']
    assert (soft.repo, soft.bytes, soft.detail['mode'], soft.detail['level']) == ('apps', 1024**3, 'soft', 100)


def test_storage_events() -> None:
    low = quota.storage_event(low=True, free=10, floor=20, total=100, now=NOW)
    assert low.type is EventType.STORAGE_LOW and 'refused' in low.detail['summary']
    ok = quota.storage_event(low=False, free=30, floor=20, total=100, now=NOW)
    assert ok.type is EventType.STORAGE_OK and ok.detail['floor_bytes'] == 20


def test_guard_floor_is_the_larger_setting() -> None:
    assert storage_guard.GuardSettings(min_free_bytes=100).floor(10_000) == 100
    assert storage_guard.GuardSettings(min_free_bytes=100, min_free_percent=5).floor(10_000) == 500
    assert storage_guard.GuardSettings(min_free_bytes=1000, min_free_percent=5).floor(10_000) == 1000


async def test_blob_store_percent_floor(tmp_path: Path) -> None:
    store = BlobStore(tmp_path)
    free, total = await store.space()
    assert 0 < free <= total
    await store.check_free_space()
    refused: list[bool] = []
    store.min_free_percent = 100.0  # the whole filesystem must stay free: always refused
    store.on_refused = lambda: refused.append(True)
    assert store.floor(total) == total
    with pytest.raises(InsufficientStorage):
        await store.open_writer()
    assert refused == [True]
