"""Hugging Face prefetch (T-O12; FORMATS.md "Hugging Face", "Prefetch").

Warm items: ``<repo_id>[@<revision>]`` (every file of the revision, default ``main``) and
``<repo_id>[@<revision>]:<path>`` (one file); datasets as ``datasets/<repo_id>...``, e.g.
``julien-c/dummy-unknown``, ``openai-community/gpt2@main:config.json``. Warming fetches the revision's info document
(the file list, with sizes and digests), then each file at the revision's commit, verified as for a client. A whole
revision is bounded: at most ``MAX_WARM_FILES`` files and ``MAX_WARM_BYTES`` bytes (else the item fails; name files).

Refresh: info documents of branches and tags (``api/<kind>s/<id>[/revision/<rev>]``); a commit's never change and
tree listings are refreshed when clients ask for them.
"""

from __future__ import annotations

import asyncio

from waystation_common.errors import NotFound, PathRejected, UpstreamError
from waystation_common.huggingface import (
    DATASETS,
    Kind,
    file_key,
    is_commit,
    split_revision,
    valid_repo_id,
)
from waystation_common.paths import normalise
from waystation_server.formats.base import RefreshRequest, WarmContext
from waystation_server.formats.huggingface.metadata import MAX_INFO, parse_info
from waystation_server.formats.huggingface.paths import info_key, parse_route

MAX_ITEM = 1024
MAX_WARM_FILES = 2000
MAX_WARM_BYTES = 200 * 1024**3
DEFAULT_REVISION = 'main'


def parse_item(item: str) -> tuple[Kind, str, str, str | None]:
    """``(kind, repo_id, revision, path or None)``; raises ``ValueError`` with a readable message."""
    text = item.strip()
    if not text or len(text) > MAX_ITEM:
        raise ValueError(f'must be 1-{MAX_ITEM} characters')
    head, sep, path = text.partition(':')
    kind: Kind = 'model'
    if head.startswith(f'{DATASETS}/'):
        kind, head = 'dataset', head[len(DATASETS) + 1 :]
    repo_id, at, revision = head.partition('@')
    if not valid_repo_id(repo_id):
        raise ValueError('not a Hub repo id: <name> or <namespace>/<name> (letters, digits, -, _ and .)')
    if at:
        found = split_revision(revision.split('/'))
        if found is None or found[1]:
            raise ValueError('after @: a branch, tag, commit or refs/pr/<n>')
    clean: str | None = None
    if sep:
        try:
            clean = normalise(path)
        except PathRejected as exc:
            raise ValueError(f'after ":": a file path ({exc.detail})') from None
    return kind, repo_id, revision if at else DEFAULT_REVISION, clean


def check_item(item: str) -> str | None:
    try:
        parse_item(item)
    except ValueError as exc:
        return str(exc)
    return None


def refresh_request(asset_key: str) -> RefreshRequest | None:
    route = parse_route(asset_key)
    if (
        route is None
        or route.path
        or '?' in asset_key
        or asset_key != info_key(route.kind, route.repo_id, route.revision)
    ):
        return None
    return None if route.revision is not None and is_commit(route.revision) else RefreshRequest(asset_key)


async def warm(ctx: WarmContext, item: str) -> None:
    kind, repo_id, revision, path = parse_item(item)
    fetched = await ctx.get(info_key(kind, repo_id, revision))
    parsed = await asyncio.to_thread(parse_info, await ctx.read(fetched, max_bytes=MAX_INFO))
    if path is not None:
        if path not in parsed.files:
            raise NotFound(f'{path} is not a file of {repo_id}@{revision}')
        names = [path]
    else:
        names = sorted(parsed.files)
        if len(names) > MAX_WARM_FILES or parsed.size > MAX_WARM_BYTES:
            raise UpstreamError(
                f'{repo_id}@{revision} has {len(names)} files ({parsed.size} bytes), more than a prefetch warms '
                f'({MAX_WARM_FILES} files, {MAX_WARM_BYTES // 1024**3} GiB): name the files (<repo_id>@<rev>:<path>)'
            )
    for name in names:
        await ctx.get(file_key(kind, repo_id, parsed.commit, name))
