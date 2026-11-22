"""Shared prefetch helpers for format plugins (T-O12, adr/0012; FORMATS.md "Prefetch").

A format that can warm items gives its descriptor a ``warm_item`` checker and its plugin a ``warm(ctx, item)``; a
format whose cached metadata can be refreshed by a client request gives its descriptor a ``refresh_request``. The
helpers here cover the common cases: formats whose asset keys are their request paths, and items that are paths.
"""

from __future__ import annotations

import json
from typing import Any

from waystation_common.errors import PathRejected, UpstreamError
from waystation_common.paths import normalise
from waystation_server.formats.base import RefreshRequest

MAX_ITEM = 512


def same_path(asset_key: str) -> RefreshRequest | None:
    """Formats whose asset key is the client's request path (raw, Maven, Go): ask for the same path again."""
    return RefreshRequest(asset_key) if asset_key else None


def check_path(item: str) -> str | None:
    """A repository-relative file path (raw): normalised, not a folder."""
    if not item or len(item) > MAX_ITEM:
        return f'must be 1-{MAX_ITEM} characters'
    if item.endswith('/') or '?' in item or '#' in item:
        return 'must be a file path, without a query'
    try:
        clean = normalise(item.strip())
    except PathRejected as exc:
        return exc.detail
    return None if clean else 'must be a file path'


def parse_json(data: bytes, what: str) -> Any:
    try:
        return json.loads(data)
    except ValueError:
        raise UpstreamError(f'{what} is not valid JSON') from None
