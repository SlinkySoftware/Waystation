import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr

from waystation_common import errors
from waystation_common.ids import id_time, is_id, new_id
from waystation_common.logs import (
    ConsoleFormatter,
    JsonFormatter,
    bind_context,
    configure_logging,
    get_context,
    log_context,
    redact,
    reset_context,
)


def _record(msg: str = 'hello', **extra: object) -> logging.LogRecord:
    record = logging.LogRecord('waystation.test', logging.INFO, __file__, 1, msg, None, None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_json_formatter_includes_context_and_redacts() -> None:
    with log_context(request_id='r1', principal='alice', repo=None):
        line = JsonFormatter().format(
            _record(cache='HIT', status=200, password='hunter2', headers={'Authorization': 'Bearer x', 'X': 'y'})
        )
    entry = json.loads(line)
    assert entry['msg'] == 'hello'
    assert entry['level'] == 'INFO'
    assert entry['request_id'] == 'r1'
    assert entry['principal'] == 'alice'
    assert 'repo' not in entry
    assert entry['cache'] == 'HIT'
    assert entry['password'] == '[redacted]'
    assert entry['headers'] == {'Authorization': '[redacted]', 'X': 'y'}
    assert entry['ts'].endswith('Z')
    assert 'hunter2' not in line


def test_secretstr_never_rendered() -> None:
    assert redact('anything', SecretStr('s3cret')) == '**********'
    assert redact('api_key', 'abc') == '[redacted]'
    assert redact('repo', 'npm') == 'npm'


def test_exception_rendering() -> None:
    try:
        raise ValueError('boom')
    except ValueError:
        import sys

        record = _record()
        record.exc_info = sys.exc_info()
    assert 'ValueError: boom' in json.loads(JsonFormatter().format(record))['exc']
    assert 'ValueError: boom' in ConsoleFormatter().format(record)


def test_console_formatter() -> None:
    with log_context(repo='npm-proxy'):
        line = ConsoleFormatter().format(_record(token='t0ken', _private=1))
    assert 'waystation.test: hello' in line
    assert 'repo=npm-proxy' in line
    assert 'token=[redacted]' in line
    assert '_private' not in line
    assert ConsoleFormatter().format(_record()).endswith('hello')


def test_context_nesting_and_bind() -> None:
    assert get_context() == {}
    with log_context(a=1):
        with log_context(b=2):
            assert get_context() == {'a': 1, 'b': 2}
        assert get_context() == {'a': 1}
    token = bind_context(op='get')
    assert get_context() == {'op': 'get'}
    reset_context(token)
    assert get_context() == {}


async def test_context_is_per_task() -> None:
    seen: dict[str, object] = {}

    async def worker(name: str) -> None:
        with log_context(request_id=name):
            await asyncio.sleep(0)
            seen[name] = get_context()['request_id']

    await asyncio.gather(worker('a'), worker('b'))
    assert seen == {'a': 'a', 'b': 'b'}


def test_configure_logging(capsys: pytest.CaptureFixture[str]) -> None:
    root = logging.getLogger()
    saved = (list(root.handlers), root.level)
    try:
        configure_logging('debug', 'json')
        configure_logging('info', 'console')  # idempotent: replaces, does not stack
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0].formatter, ConsoleFormatter)
        assert root.level == logging.INFO
        assert logging.getLogger('uvicorn.access').propagate
    finally:
        root.handlers[:] = saved[0]
        root.setLevel(saved[1])


def test_ids() -> None:
    a, b = new_id(), new_id()
    assert a != b
    assert is_id(a)
    assert not is_id('not-an-id')
    assert abs(id_time(a) - datetime.now(UTC)) < timedelta(seconds=5)


def test_errors() -> None:
    err = errors.PathRejected('path traversal')
    assert isinstance(err, errors.ValidationFailed)
    assert err.status == 400
    assert err.code == 'invalid_path'
    assert str(err) == err.detail == 'path traversal'
    assert errors.NotFound().detail == 'not_found'
    assert {e.status for e in (errors.Unauthorized(), errors.Forbidden(), errors.Conflict())} == {401, 403, 409}
    assert errors.InsufficientStorage.status == 507
    assert errors.PayloadTooLarge.status == 413
    assert errors.DigestMismatch.status == errors.UpstreamError.status == 502
    assert errors.CryptoError.status == errors.ConfigError.status == 500
