"""Alpine apk prefetch (T-O12; FORMATS.md "Alpine apk", "Prefetch").

Warm items: ``name`` (the version the index lists; the newest when it lists several), ``name=version``
(``busybox=1.37.0-r19``), each looked up in the repository's default branch: for every configured architecture, the
first configured repository whose index lists it (``latest-stable/main/x86_64``, then ``.../community/...``); or
``<branch>/<repository>/<arch>/name[=version]`` for one directory. Warming fetches what ``apk add`` does, through
the repository's own routes: the directory's ``APKINDEX.tar.gz`` and the package (verified against its ``C:``
checksum as for a client).

Refresh (``descriptor.refresh_request``): ``APKINDEX.tar.gz`` files, asked for again by their paths.
"""

from __future__ import annotations

import asyncio

from waystation_common import apk
from waystation_common.errors import NotFound
from waystation_server.formats.base import WarmContext

MAX_DEPTH = 8


def parse_item(item: str) -> tuple[str | None, str, str | None]:
    """``(directory or None, name, version or None)``; raises ``ValueError`` with a readable message."""
    text = item.strip()
    directory, _, spec = text.rpartition('/')
    if directory:
        parts = directory.split('/')
        if (
            len(parts) < 3
            or len(parts) > MAX_DEPTH
            or not all(apk.SEGMENT.match(p) and p not in ('.', '..') for p in parts)
        ):
            raise ValueError('a path is <branch>/<repository>/<arch>/ before the package name')
        if not apk.ARCH.match(parts[-1]):
            raise ValueError('the last folder of the path must be an architecture (x86_64, aarch64...)')
    name, sep, version = spec.partition('=')
    if not apk.NAME.match(name):
        raise ValueError('a package name (letters, digits, ".", "_", "+" and "-"), optionally =version')
    if sep and not apk.valid_version(version):
        raise ValueError('after "=": an apk version (1.37.0-r19)')
    return directory or None, name, version if sep else None


def check_item(item: str) -> str | None:
    try:
        parse_item(item)
    except ValueError as exc:
        return str(exc)
    return None


async def _find(ctx: WarmContext, directory: str, name: str, version: str | None) -> apk.IndexEntry | None:
    fetched = await ctx.get(f'{directory}/{apk.INDEX_NAME}')
    data = await ctx.read(fetched, max_bytes=apk.INDEX_MAX)
    index = await asyncio.to_thread(apk.parse_index, data)
    found = [e for e in index.entries if e.name == name and (version is None or e.version == version)]
    newest = apk.newest([e.version for e in found])
    return next((e for e in found if e.version == newest), None)


async def warm(ctx: WarmContext, item: str, *, branch: str, repositories: list[str], architectures: list[str]) -> None:
    directory, name, version = parse_item(item)
    groups = (
        [[directory]] if directory else [[f'{branch}/{repo}/{arch}' for repo in repositories] for arch in architectures]
    )
    for candidates in groups:
        for candidate in candidates:
            entry = await _find(ctx, candidate, name, version)
            if entry is not None:
                await ctx.get(f'{candidate}/{entry.file_name}')
                break
        else:
            where = ', '.join(candidates)
            raise NotFound(f'{item.strip()} is not listed in {where}')
