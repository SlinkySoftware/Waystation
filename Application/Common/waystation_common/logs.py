"""Structured logging with request context (ARCHITECTURE §12, SPECIFICATION §5.5).

- ``configure_logging(level, fmt)`` once at process start: one handler on the root logger writing JSON lines (or a
  readable console format) to stderr.
- Request context lives in a ``contextvars`` mapping, so it follows the request through ``await`` and tasks::

      with log_context(request_id=rid, principal='alice', repo='npm-proxy', format='npm', op='get'):
          log.info('served', extra={'cache': 'HIT', 'status': 200, 'duration_ms': 3.1, 'bytes': 1024})

  Context keys and ``extra`` fields appear as top-level JSON keys.
- Never secrets: values under keys that look sensitive (password, token, secret, authorization, cookie, key...)
  are replaced by ``[redacted]``, and ``SecretStr`` values render as ``**********``.
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
import traceback
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, Final

from pydantic import SecretBytes, SecretStr

CONTEXT_KEYS: Final = ('request_id', 'principal', 'repo', 'format', 'op', 'job_id', 'worker_id')
REDACTED: Final = '[redacted]'
_SENSITIVE = re.compile(
    r'pass(word|wd)?|secret|token|authori[sz]ation|cookie|credential|api[_-]?key|private[_-]?key', re.I
)

# Attributes every LogRecord has; anything else on a record came from ``extra``.
_RECORD_ATTRS: Final = frozenset(vars(logging.LogRecord('', 0, '', 0, '', None, None))) | {'message', 'asctime'}

_EMPTY: Mapping[str, Any] = MappingProxyType({})
_context: contextvars.ContextVar[Mapping[str, Any]] = contextvars.ContextVar('waystation_log_context', default=_EMPTY)


def get_context() -> Mapping[str, Any]:
    return _context.get()


@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    """Add fields to the logging context for the duration of the block (nests; ``None`` values are ignored)."""
    merged = {**_context.get(), **{k: v for k, v in fields.items() if v is not None}}
    token = _context.set(merged)
    try:
        yield
    finally:
        _context.reset(token)


def bind_context(**fields: Any) -> contextvars.Token[Mapping[str, Any]]:
    """Like ``log_context`` without a block (middleware); undo with ``reset_context(token)``."""
    return _context.set({**_context.get(), **{k: v for k, v in fields.items() if v is not None}})


def reset_context(token: contextvars.Token[Mapping[str, Any]]) -> None:
    _context.reset(token)


def redact(key: str, value: Any) -> Any:
    """Make ``value`` safe to log under ``key``."""
    if isinstance(value, SecretStr | SecretBytes):
        return '**********'
    if _SENSITIVE.search(key):
        return REDACTED
    if isinstance(value, Mapping):
        return {str(k): redact(str(k), v) for k, v in value.items()}
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            'ts': datetime.fromtimestamp(record.created, UTC).isoformat(timespec='milliseconds').replace('+00:00', 'Z'),
            'level': record.levelname,
            'logger': record.name,
            'msg': record.getMessage(),
        }
        for key, value in get_context().items():
            entry[key] = redact(key, value)
        for key, value in vars(record).items():
            if key not in _RECORD_ATTRS and not key.startswith('_'):
                entry[key] = redact(key, value)
        if record.exc_info:
            entry['exc'] = ''.join(traceback.format_exception(*record.exc_info)).rstrip()
        return json.dumps(entry, default=str, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, UTC).strftime('%H:%M:%S.%f')[:-3]
        fields = {**get_context(), **{k: v for k, v in vars(record).items() if k not in _RECORD_ATTRS}}
        tail = ' '.join(f'{k}={redact(k, v)}' for k, v in fields.items() if not k.startswith('_'))
        line = f'{ts} {record.levelname:<7} {record.name}: {record.getMessage()}' + (f'  {tail}' if tail else '')
        if record.exc_info:
            line += '\n' + ''.join(traceback.format_exception(*record.exc_info)).rstrip()
        return line


def configure_logging(level: str = 'INFO', fmt: str = 'json') -> None:
    """Install the Waystation handler on the root logger (idempotent)."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(ConsoleFormatter() if fmt == 'console' else JsonFormatter())
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())
    # uvicorn installs its own handlers; route them through ours.
    for name in ('uvicorn', 'uvicorn.error', 'uvicorn.access'):
        uv = logging.getLogger(name)
        uv.handlers.clear()
        uv.propagate = True
