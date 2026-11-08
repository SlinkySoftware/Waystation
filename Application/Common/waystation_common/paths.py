"""The one path normalisation function (ARCHITECTURE §7). Every repository-relative path goes through it.

``normalise(path)`` returns a canonical relative POSIX path (``a/b/c``) or raises ``PathRejected``:

- ``decode=True`` percent-decodes the input once (strict UTF-8); an encoded ``%2F`` becomes a separator. Pass it only
  for raw, still-encoded input; a path the web framework already decoded must not be decoded again (``a%2520b`` is
  the file ``a%20b``).
- Rejected: NUL and other control characters, backslashes, absolute paths (leading ``/`` or a drive letter),
  ``..`` segments (never resolved, always refused), segments that NFKC-fold to ``.``/``..`` or contain a slash
  (``‥``, fullwidth solidus...), percent-encoded dot/slash/backslash/NUL left after decoding (double encoding),
  lone surrogates, segments longer than 255 UTF-8 bytes and paths longer than 4096 bytes.
- Normalised: Unicode NFC; empty and ``.`` segments are dropped (so ``a//./b/`` is ``a/b``).
- An empty result is rejected unless ``allow_empty=True`` (repository root).

The result is idempotent: ``normalise(normalise(p)) == normalise(p)``.
"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import unquote

from waystation_common.errors import PathRejected

MAX_PATH_BYTES = 4096
MAX_SEGMENT_BYTES = 255

_CONTROL = re.compile(r'[\x00-\x1f\x7f]')
_ENCODED_SPECIAL = re.compile(r'%(?:2e|2f|5c|00)', re.IGNORECASE)
_DRIVE = re.compile(r'^[A-Za-z]:')


def normalise(path: str, *, decode: bool = False, allow_empty: bool = False) -> str:
    if decode:
        try:
            path = unquote(path, errors='strict')
        except UnicodeDecodeError:
            raise PathRejected('path is not valid UTF-8') from None
    if _CONTROL.search(path):
        raise PathRejected('control character in path')
    if '\\' in path:
        raise PathRejected('backslash in path')
    if _ENCODED_SPECIAL.search(path):
        raise PathRejected('encoded separator or dot in path')
    try:
        path.encode('utf-8')
    except UnicodeEncodeError:
        raise PathRejected('path is not valid Unicode') from None
    if path.startswith('/'):
        raise PathRejected('absolute path')
    path = unicodedata.normalize('NFC', path)
    if len(path.encode('utf-8')) > MAX_PATH_BYTES:
        raise PathRejected('path too long')

    segments: list[str] = []
    for segment in path.split('/'):
        if segment in ('', '.'):
            continue
        folded = unicodedata.normalize('NFKC', segment)
        if segment == '..' or folded in ('.', '..') or '/' in folded or '\\' in folded:
            raise PathRejected('path traversal')
        if len(segment.encode('utf-8')) > MAX_SEGMENT_BYTES:
            raise PathRejected('path segment too long')
        segments.append(segment)

    if not segments and not allow_empty:
        raise PathRejected('empty path')
    if segments and _DRIVE.match(segments[0]):
        raise PathRejected('absolute path')
    return '/'.join(segments)
