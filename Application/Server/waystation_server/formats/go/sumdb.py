"""The checksum database proxy (``go help module-auth``, "Checksum database"; FORMATS.md "Go modules").

A Go proxy repository serves its checksum database (option ``sumdb_name``, default ``sum.golang.org``, fetched from
``sumdb_url``) under ``/repository/<repo>/sumdb/<name>/``, so a go command whose ``GOPROXY`` is the repository
verifies modules without reaching the database itself (it asks ``<proxy>/sumdb/<name>/supported`` first):

- ``supported``: answered here (``200``, empty) when the option ``sumdb_proxy`` is on; other names answer ``404``,
  so the go command falls back to contacting its database directly.
- ``latest`` (the signed tree head): metadata, revalidated after ``LATEST_MAX_AGE_S``.
- ``lookup/<module>@<version>`` (a record and a signed tree head): metadata kept forever (a record never changes;
  an older tree head is still valid, the go command proves it against newer ones), validated
  (``gosum.parse_lookup``); module paths and versions case-encoded and normalised as for module files.
- ``tile/<H>/<L>/<K>[.p/<W>]`` (``L`` a level or ``data``): full tiles are immutable content; partial tiles (``.p/W``)
  are metadata. Hash tiles must be exactly ``W * 32`` bytes (``2^H`` hashes when full); data tiles are bounded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from waystation_server.formats.go import gosum, modpath
from waystation_server.formats.go.common import TEXT
from waystation_server.proxy.engine import AssetKind, AssetSpec, Expected, UpstreamInfo

LATEST_MAX_AGE_S = 60
FOREVER_S = 100 * 365 * 86_400
MAX_LATEST = 16 * 1024
MAX_DATA_TILE = 4 * 1024 * 1024
MAX_HEIGHT = 16
_TILE = re.compile(r'^tile/([0-9]{1,2})/(data|[0-9]{1,2})/((?:x[0-9]{3}/)*[0-9]{3})(?:\.p/([0-9]{1,6}))?$')
OCTETS = 'application/octet-stream'


@dataclass(frozen=True)
class SumdbPath:
    kind: str  # supported, latest, lookup, tile
    rest: str  # canonical path below sumdb/<name>/
    module: str = ''
    version: str = ''
    height: int = 0
    data: bool = False
    width: int | None = None  # partial tiles


def parse(rest: str) -> SumdbPath | None:
    """A path below ``sumdb/<name>/`` (None: not a checksum database path)."""
    if rest in ('supported', 'latest'):
        return SumdbPath(rest, rest)
    if rest.startswith('lookup/'):
        target = rest.removeprefix('lookup/')
        escaped_module, at, escaped_version = target.rpartition('@')
        if not at:
            return None
        try:
            module = modpath.unescape_path(escaped_module)
            version = modpath.unescape_version(escaped_version)
        except modpath.InvalidModule:
            return None
        if not modpath.is_canonical(version):
            return None
        canonical = f'lookup/{modpath.escape_path(module)}@{modpath.escape_version(version)}'
        return SumdbPath('lookup', canonical, module=module, version=version)
    tile = _TILE.match(rest)
    if tile is None:
        return None
    height, level, _, width = tile.groups()
    h = int(height)
    w = int(width) if width is not None else None
    if not 1 <= h <= MAX_HEIGHT or (w is not None and not 1 <= w < 2**h):
        return None
    return SumdbPath('tile', rest, height=h, data=level == 'data', width=w)


def lookup_spec(base_url: str, name: str, path: SumdbPath) -> AssetSpec:
    """The lookup record of ``path`` (``kind == 'lookup'``), validated as such."""
    module, version = path.module, path.version

    def validate(data: bytes, info: UpstreamInfo) -> None:
        gosum.parse_lookup(data, module, version)

    return AssetSpec(
        path=f'sumdb/{name}/{path.rest}',
        kind=AssetKind.METADATA,
        upstream_url=f'{base_url.rstrip("/")}/{path.rest}',
        content_type=TEXT,
        max_size=gosum.MAX_LOOKUP,
        max_age_s=FOREVER_S,
        validate=validate,
        head_passthrough=False,
        artifact=f'{module}@{version} (checksum)',
        component=module,
    )


def spec(base_url: str, name: str, path: SumdbPath) -> AssetSpec:
    """The asset of a checksum database path other than ``supported``."""
    upstream = f'{base_url.rstrip("/")}/{path.rest}'
    key = f'sumdb/{name}/{path.rest}'
    if path.kind == 'lookup':
        return lookup_spec(base_url, name, path)
    if path.kind == 'latest':
        return AssetSpec(
            path=key,
            kind=AssetKind.METADATA,
            upstream_url=upstream,
            content_type=TEXT,
            max_size=MAX_LATEST,
            max_age_s=LATEST_MAX_AGE_S,
            artifact=f'{name} latest',
        )
    if path.data:
        expected = None
        max_size = MAX_DATA_TILE
    else:
        size = (path.width if path.width is not None else 2**path.height) * 32
        expected = Expected(size=size)
        max_size = size
    return AssetSpec(
        path=key,
        kind=AssetKind.METADATA if path.width is not None else AssetKind.CONTENT,
        upstream_url=upstream,
        content_type=OCTETS,
        expected=expected,
        max_size=max_size,
        head_passthrough=False,
        artifact=f'{name} {path.rest}',
    )
