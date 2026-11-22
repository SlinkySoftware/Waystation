"""``waystation_common.recent_errors``: the bounded, non-blocking capture of WARNING/ERROR records (T-O10)."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from waystation_common import recent_errors
from waystation_common.logs import log_context
from waystation_common.recent_errors import RecentErrors, read_recent


class FakePipe:
    def __init__(self, owner: FakeValkey) -> None:
        self.owner = owner
        self.cmds: list[tuple[Any, ...]] = []

    def xadd(self, key: str, fields: dict[str, str], **kwargs: Any) -> None:
        self.cmds.append(('xadd', key, fields, kwargs))

    def expire(self, key: str, seconds: int) -> None:
        self.cmds.append(('expire', key, seconds))

    async def execute(self) -> None:
        if self.owner.fail:
            raise ConnectionError('valkey down')
        if self.owner.hang:
            await asyncio.sleep(3600)
        self.owner.cmds.extend(self.cmds)

    async def __aenter__(self) -> FakePipe:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class FakeValkey:
    def __init__(self, *, fail: bool = False, hang: bool = False) -> None:
        self.fail = fail
        self.hang = hang
        self.cmds: list[tuple[Any, ...]] = []
        self.stream: list[tuple[str, dict[str, str]]] = []

    def pipeline(self, transaction: bool = True) -> FakePipe:
        return FakePipe(self)

    async def xrevrange(self, key: str, count: int) -> list[tuple[str, dict[str, str]]]:
        return self.stream[:count]


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _logger(handler: RecentErrors, name: str = 'waystation.test') -> logging.Logger:
    logger = logging.getLogger(name)
    logger.propagate = False
    logger.handlers = [handler]
    logger.setLevel(logging.DEBUG)
    return logger


def _entries(handler: RecentErrors) -> list[dict[str, Any]]:
    return [json.loads(line) for line in handler._take()]


def test_only_warnings_and_errors_are_kept_with_context_and_redaction() -> None:
    handler = RecentErrors('server')
    log = _logger(handler)
    log.info('fine')
    with log_context(request_id='req-1', repo='npm-proxy'):
        log.warning(
            'upstream https://bob:hunter2@registry.example.com/x failed',
            extra={'password': 'hunter2', 'status': 502, 'big': 'x' * 5000},
        )
    try:
        raise RuntimeError('boom at https://alice:s3cret@registry.example.com/')
    except RuntimeError:
        log.exception('job failed')
    first, second = _entries(handler)
    assert first['level'] == 'WARNING'
    assert first['component'] == 'server'
    assert first['host'] and first['pid']
    assert first['request_id'] == 'req-1'
    assert first['repo'] == 'npm-proxy'
    assert first['status'] == 502
    assert first['password'] == '[redacted]'
    assert len(first['big']) <= recent_errors.MAX_VALUE
    assert 'hunter2' not in json.dumps(first)
    assert 'https://***@registry.example.com/x' in first['msg']
    assert second['level'] == 'ERROR'
    assert 'RuntimeError' in second['exc'] and 's3cret' not in second['exc']


def test_long_messages_and_tracebacks_are_clipped() -> None:
    handler = RecentErrors('worker')
    log = _logger(handler)
    try:
        raise ValueError('y' * 10_000)
    except ValueError:
        log.error('z' * 10_000, exc_info=True)
    (entry,) = _entries(handler)
    assert len(entry['msg']) == recent_errors.MAX_MESSAGE
    assert len(entry['exc']) == recent_errors.MAX_EXCEPTION
    assert entry['exc'].startswith('…')  # the end of the traceback is kept


def test_the_buffer_is_bounded_and_drops_are_counted() -> None:
    handler = RecentErrors('server', max_queue=3)
    log = _logger(handler)
    for i in range(10):
        log.warning(f'distinct message {i}')
    assert handler.queued == 3
    assert handler.dropped == 7


def test_a_flood_of_one_message_is_summarised() -> None:
    clock = Clock()
    handler = RecentErrors('server', per_key_limit=2, window_s=60, clock=clock)
    log = _logger(handler)
    for i in range(50):
        log.warning('upstream %s failed', f'host{i}')
    assert handler.queued == 2
    assert handler.suppressed == 48
    assert len(_entries(handler)) == 2  # the window is still open: no summary yet
    clock.now += 61
    (summary,) = _entries(handler)
    assert summary['suppressed'] == 48
    assert summary['msg'] == 'upstream host49 failed'
    log.warning('upstream %s failed', 'again')  # a new window
    assert [e['msg'] for e in _entries(handler)] == ['upstream again failed']


def test_too_many_distinct_messages_reset_the_windows() -> None:
    handler = RecentErrors('server', per_key_limit=1, max_queue=10_000)
    log = _logger(handler)
    log.warning('same')
    log.warning('same')  # suppressed
    for i in range(recent_errors.MAX_KEYS):
        log.warning(f'distinct {i}')
    assert len(handler._windows) <= recent_errors.MAX_KEYS
    assert any(json.loads(line).get('suppressed') == 1 for line in handler._take())


def test_a_broken_record_never_reaches_the_caller() -> None:
    handler = RecentErrors('server')
    log = _logger(handler)
    log.warning('%d apples', 'not a number')  # formatting fails inside the handler
    assert handler.dropped == 1
    assert handler.queued == 0


async def test_pending_records_are_written_to_the_stream_with_maxlen_and_ttl() -> None:
    valkey = FakeValkey()
    handler = RecentErrors('server')
    handler._valkey = valkey  # type: ignore[assignment]
    log = _logger(handler)
    log.error('one')
    log.error('two')
    await handler.write_pending()
    xadds = [c for c in valkey.cmds if c[0] == 'xadd']
    assert [json.loads(c[2]['e'])['msg'] for c in xadds] == ['one', 'two']
    assert all(c[1] == 'errors:recent' and c[3] == {'maxlen': 1000, 'approximate': True} for c in xadds)
    assert ('expire', 'errors:recent', recent_errors.TTL_S) in valkey.cmds
    assert handler.written == 2
    await handler.write_pending()  # nothing pending: no round trip
    assert len(valkey.cmds) == 3


async def test_valkey_down_or_slow_drops_the_batch_silently() -> None:
    handler = RecentErrors('server')
    log = _logger(handler)
    log.error('before start')
    await handler.write_pending()  # not started: no client
    assert handler.dropped == 1
    handler._valkey = FakeValkey(fail=True)  # type: ignore[assignment]
    log.error('down')
    await handler.write_pending()
    handler._valkey = FakeValkey(hang=True)  # type: ignore[assignment]
    log.error('slow')
    await handler.write_pending(timeout_s=0.01)
    assert handler.dropped == 3
    assert handler.queued == 0


async def test_start_attaches_to_the_root_logger_and_aclose_detaches_and_writes() -> None:
    valkey = FakeValkey()
    handler = RecentErrors('worker', flush_interval_s=0.01)
    root = logging.getLogger()
    handler.start(valkey)  # type: ignore[arg-type]
    try:
        assert handler in root.handlers
        logging.getLogger('waystation.somewhere').warning('captured')
        for _ in range(100):
            if valkey.cmds:
                break
            await asyncio.sleep(0.01)
        assert valkey.cmds, 'the background task writes'
        logging.getLogger('waystation.somewhere').warning('at shutdown')
    finally:
        await handler.aclose()
    assert handler not in root.handlers
    entries = [json.loads(c[2]['e']) for c in valkey.cmds if c[0] == 'xadd']
    assert [e['msg'] for e in entries if e['logger'] == 'waystation.somewhere'] == ['captured', 'at shutdown']


async def test_read_recent_parses_entries_newest_first_and_skips_malformed() -> None:
    valkey = FakeValkey()
    valkey.stream = [
        ('2-0', {'e': json.dumps({'msg': 'new'})}),
        ('1-5', {'e': 'not json'}),
        ('1-0', {'e': json.dumps({'msg': 'old'})}),
        ('0-1', {'other': 'x'}),
    ]
    items = await read_recent(valkey, 10)  # type: ignore[arg-type]
    assert items == [{'msg': 'new', 'id': '2-0'}, {'msg': 'old', 'id': '1-0'}]
