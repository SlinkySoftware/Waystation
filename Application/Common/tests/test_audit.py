"""Audit catalog, redaction, diffs and the non-blocking writer (``waystation_common.audit``)."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import BaseModel

from waystation_common.audit import (
    PENDING,
    SPEC_ACTIONS,
    SYSTEM,
    AuditAction,
    AuditEvent,
    AuditWriter,
    diff,
    is_secret_key,
    sanitize,
)


def event(n: int = 0) -> AuditEvent:
    return AuditEvent(action=AuditAction.TOKEN_CREATE.value, actor=SYSTEM, details={'n': n})


def test_catalog_covers_the_specification() -> None:
    assert {a for actions in SPEC_ACTIONS.values() for a in actions} == set(AuditAction)
    assert set(PENDING) < set(AuditAction)
    assert AuditAction.AUTH_PROVIDER_UPDATE.category == 'auth_provider'
    assert all(a.value.count('.') == 1 for a in AuditAction)


@pytest.mark.parametrize(
    ('key', 'secret'),
    [
        ('password', True),
        ('bind_password', True),
        ('secret_enc', True),
        ('secret', True),
        ('token', True),
        ('access_token', True),
        ('password_hash', True),
        ('csrf', True),
        ('Authorization', True),
        ('token_id', False),
        ('tokens_revoked', False),
        ('bind_password_set', False),
        ('secret_set', False),
        ('prefix', False),
        ('username', False),
        ('upstream_url', False),
    ],
)
def test_secret_keys(key: str, secret: bool) -> None:
    assert is_secret_key(key) is secret


def test_sanitize_redacts_and_bounds() -> None:
    out = sanitize(
        {
            'username': 'alice',
            'password': 'hunter2',
            'empty_secret': '',
            'nested': {'auth': {'secret': 's3cret', 'kind': 'basic'}},
            'url': 'https://ci:pa55@registry.example.com/v2/',
            'when': datetime(2026, 11, 9, tzinfo=UTC),
            'many': list(range(150)),
            'long': 'x' * 2000,
            'deep': {'a': {'b': {'c': {'d': {'e': {'f': {'g': 1}}}}}}},
        }
    )
    assert out['password'] == '***'
    assert out['empty_secret'] is None
    assert out['nested'] == {'auth': {'secret': '***', 'kind': 'basic'}}
    assert out['url'] == 'https://***@registry.example.com/v2/'
    assert out['when'] == '2026-11-09T00:00:00+00:00'
    assert len(out['many']) == 101 and out['many'][-1] == '… 50 more'
    assert len(out['long']) == 1001
    assert out['deep']['a']['b']['c']['d']['e'] == '…'
    assert 'hunter2' not in repr(out) and 's3cret' not in repr(out) and 'pa55' not in repr(out)
    assert sanitize(None) == {}


class Account(BaseModel):
    id: str
    name: str
    roles: list[str]
    updated_at: datetime | None = None


def test_diff_fields_lists_and_ignored_bookkeeping() -> None:
    before = Account(id='1', name='ci', roles=['reader'])
    after = Account(id='1', name='ci', roles=['reader', 'deployer'], updated_at=datetime.now(UTC))
    changes = diff(before, after)
    assert [(c.field, c.before, c.after, c.redacted) for c in changes] == [
        ('roles', ['reader'], ['reader', 'deployer'], False)
    ]
    created = diff(None, {'name': 'x', 'proxy': {'upstream_url': 'https://example.com/', 'auth': {'kind': 'none'}}})
    assert {c.field: c.after for c in created} == {
        'name': 'x',
        'proxy.upstream_url': 'https://example.com/',
        'proxy.auth.kind': 'none',
    }
    assert diff({'a': 1}, {'a': 1}) == []
    deleted = diff({'name': 'x'}, None)
    assert [(c.field, c.before, c.after) for c in deleted] == [('name', 'x', None)]


def test_diff_never_shows_secret_values() -> None:
    def doc(secret: str | None) -> dict[str, Any]:
        return {'proxy': {'auth': {'kind': 'basic', 'secret_enc': secret}}}

    assert diff(doc('enc-1'), doc('enc-1')) == []
    rotated = diff(doc('enc-1'), doc('enc-2'))
    assert [(c.field, c.before, c.after, c.redacted) for c in rotated] == [
        ('proxy.auth.secret_enc', '***', '***', True)
    ]
    added = diff(doc(None), doc('enc-1'))
    assert (added[0].before, added[0].after) == (None, '***')
    removed = {c.field: c for c in diff(doc('enc-1'), None)}
    assert (removed['proxy.auth.secret_enc'].before, removed['proxy.auth.secret_enc'].after) == ('***', None)
    assert removed['proxy.auth.kind'].before == 'basic'
    assert 'enc-' not in repr([*rotated, *added, *removed.values()])


def test_event_document() -> None:
    e = event()
    doc = e.to_doc(retention_days=400)
    assert doc['_id'] == e.id
    assert doc['principal'] == {
        'kind': 'system',
        'id': None,
        'name': 'system',
        'auth': 'system',
        'provider': None,
        'token_id': None,
    }
    assert doc['expires_at'] - doc['ts'] == timedelta(days=400)
    assert doc['target'] is None and doc['outcome'] == 'success'


class Store:
    def __init__(self) -> None:
        self.batches: list[list[dict[str, Any]]] = []
        self.fail = 0

    async def insert(self, batch: list[dict[str, Any]]) -> None:
        if self.fail:
            self.fail -= 1
            raise RuntimeError('mongo down')
        self.batches.append(list(batch))


async def test_writer_batches_and_flushes() -> None:
    store = Store()
    writer = AuditWriter(store.insert, batch_size=10)
    for i in range(25):
        writer.record(event(i))  # queued before the task starts: drained in batches
    writer.start()
    await writer.flush()
    assert [len(b) for b in store.batches] == [10, 10, 5]
    assert writer.written == 25 and writer.dropped == 0
    await writer.aclose()


async def test_writer_retries_then_gives_up(caplog: pytest.LogCaptureFixture) -> None:
    store = Store()
    writer = AuditWriter(store.insert, retries=(0, 0))
    writer.start()
    store.fail = 2
    writer.record(event(1))
    await writer.flush()
    assert writer.written == 1 and writer.dropped == 0
    store.fail = 3
    with caplog.at_level(logging.ERROR, logger='waystation.audit'):
        writer.record(event(2))
        await writer.flush()
    assert writer.dropped == 1
    assert 'AUDIT RECORDS DROPPED' in caplog.text
    await writer.aclose()


async def test_writer_overflow_drops_counts_and_never_blocks(caplog: pytest.LogCaptureFixture) -> None:
    store = Store()
    writer = AuditWriter(store.insert, max_queue=3)
    with caplog.at_level(logging.ERROR, logger='waystation.audit'):
        for i in range(10):
            writer.record(event(i))
    assert writer.queued == 3 and writer.dropped == 7
    assert caplog.text.count('AUDIT RECORDS DROPPED') == 1  # loud, but not once per record
    writer.start()
    await writer.flush()
    assert sum(len(b) for b in store.batches) == 3


async def test_writer_close_drains_and_counts_leftovers() -> None:
    gate = asyncio.Event()

    async def slow(batch: list[dict[str, Any]]) -> None:
        await gate.wait()

    writer = AuditWriter(slow, batch_size=1)
    writer.start()
    for i in range(3):
        writer.record(event(i))
    await writer.aclose(wait_s=0.05)
    assert writer.dropped >= 2
    await writer.flush()  # no task: returns at once
