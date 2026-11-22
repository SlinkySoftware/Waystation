"""Hugging Face browse views (OpenAPI ``listHuggingfaceModels`` / ``listHuggingfaceRevisions`` /
``listHuggingfaceFiles``): models and datasets (``huggingface_repos``) → revisions and the commits with cached files
→ a commit's cached files (``assets`` under ``[datasets/]<id>/resolve/<commit>/``)."""

from __future__ import annotations

from typing import Any

from waystation_common.errors import NotFound
from waystation_common.huggingface import COMMIT, Kind, commit_prefix, parse_display_name
from waystation_server.browse.models import (
    BrowseContext,
    HuggingfaceCommit,
    HuggingfaceFile,
    HuggingfaceFilePage,
    HuggingfaceModel,
    HuggingfaceModelPage,
    HuggingfaceRef,
    HuggingfaceRevisionList,
    decode_cursor,
    encode_cursor,
)
from waystation_server.db import huggingface as hf_db


def _model(doc: dict[str, Any], stats: dict[str, Any] | None) -> HuggingfaceModel:
    stats = stats or {}
    kind: Kind = 'dataset' if doc.get('kind') == 'dataset' else 'model'
    return HuggingfaceModel(
        name=str(doc['name']),
        repo_id=str(doc.get('hf_id') or doc['name']),
        kind=kind,
        latest_commit=doc.get('latest'),
        revisions=len(doc.get('refs') or []),
        files=int(stats.get('files') or 0),
        size_bytes=int(stats.get('size') or 0),
        downloads=int(stats.get('downloads') or 0),
        last_downloaded_at=stats.get('last_downloaded_at'),
        updated_at=doc.get('updated_at'),
    )


def _parse(model: str) -> tuple[Kind, str]:
    found = parse_display_name(model.strip())
    if found is None:
        raise NotFound('model not found')
    return found


class HuggingfaceBrowse:
    async def models(
        self, ctx: BrowseContext, *, q: str | None, cursor: str | None, limit: int
    ) -> HuggingfaceModelPage:
        after = decode_cursor(cursor, str)[0] if cursor else None
        query = q.strip() if q and q.strip() else None
        docs = await hf_db.page(ctx.db, ctx.repo.id, after=after, q=query, limit=limit + 1)
        more = len(docs) > limit
        docs = docs[:limit]
        found = [parse_display_name(str(d['name'])) for d in docs]
        stats = await hf_db.model_stats(ctx.db, ctx.repo.id, [f for f in found if f is not None])
        last = str(docs[-1]['name']) if docs else None
        return HuggingfaceModelPage(
            items=[_model(d, stats.get(str(d['name']))) for d in docs],
            next_cursor=encode_cursor(last) if more and last else None,
        )

    async def revisions(self, ctx: BrowseContext, *, model: str) -> HuggingfaceRevisionList:
        kind, hf_id = _parse(model)
        doc = await hf_db.get(ctx.db, ctx.repo.id, model.strip())
        if doc is None:
            raise NotFound('model not found')
        stats = await hf_db.model_stats(ctx.db, ctx.repo.id, [(kind, hf_id)])
        refs = [
            HuggingfaceRef(revision=str(r['revision']), commit=str(r['commit']), resolved_at=r.get('resolved_at'))
            for r in doc.get('refs') or []
            if isinstance(r, dict) and r.get('revision') and r.get('commit')
        ]
        names: dict[str, list[str]] = {}
        for ref in refs:
            names.setdefault(ref.commit, []).append(ref.revision)
        commits = [
            HuggingfaceCommit(
                commit=str(c['commit']),
                revisions=names.get(str(c['commit']), []),
                files=int(c.get('files') or 0),
                size_bytes=int(c.get('size') or 0),
                cached_at=c.get('cached_at'),
                downloads=int(c.get('downloads') or 0),
                last_downloaded_at=c.get('last_downloaded_at'),
            )
            for c in await hf_db.commits(ctx.db, ctx.repo.id, kind, hf_id)
            if COMMIT.match(str(c.get('commit')))
        ]
        return HuggingfaceRevisionList(model=_model(doc, stats.get(str(doc['name']))), refs=refs, commits=commits)

    async def files(
        self, ctx: BrowseContext, *, model: str, commit: str, cursor: str | None, limit: int
    ) -> HuggingfaceFilePage:
        kind, hf_id = _parse(model)
        if not COMMIT.match(commit):
            raise NotFound('commit not found')
        offset = decode_cursor(cursor, int)[0] if cursor else 0
        docs = await hf_db.files(ctx.db, ctx.repo.id, kind, hf_id, commit, offset=offset, limit=limit + 1)
        more = len(docs) > limit
        prefix = commit_prefix(kind, hf_id, commit)
        items = [
            HuggingfaceFile(
                path=str(d['path'])[len(prefix) :],
                size=int(d.get('size') or 0),
                sha256=str(d.get('blob_sha256') or ''),
                cached_at=d.get('created_at'),
                downloads=int(d.get('download_count') or 0),
                last_downloaded_at=d.get('last_downloaded_at'),
            )
            for d in docs[:limit]
        ]
        return HuggingfaceFilePage(
            model=model.strip(),
            commit=commit,
            items=items,
            next_cursor=encode_cursor(offset + limit) if more else None,
        )
