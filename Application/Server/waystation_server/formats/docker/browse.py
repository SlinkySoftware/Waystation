"""Docker browse: what the proxy cached, by image (OpenAPI ``listDockerImages`` / ``listDockerTags`` /
``getDockerManifest``), and the records behind it.

Recording (``DockerPlugin`` ``on_stored`` hooks, in the background, parsed in a thread):

- a **tag manifest** (``<image>/manifests/<tag>``) → ``docker_tags`` (tag → digest; only the full-``Accept`` variant
  moves an existing tag) and ``docker_manifests`` (the manifest or index);
- a **manifest by digest** → ``docker_manifests`` (the platform manifests of an index, as clients pull them).

``image`` is the upstream name (``library/nginx`` on Docker Hub). Index entries for attestations (platform
``unknown/unknown``) are not platforms and are left out of the views.

Views: images (tag count, unique bytes of the cached layer and config blobs their recorded manifests reference),
tags (digest, media type, platforms, size, image ``created``), manifest detail (platform switcher for an index,
layers with "cached" and the config history's ``created_by``, labels, pull commands). Image configs are read from
the cache when present (bounded, parsed in a thread, kept in a small in-process cache: they are immutable).
"""

from __future__ import annotations

import asyncio
import json
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from waystation_common.errors import NotFound, ValidationFailed
from waystation_server.browse.models import (
    BrowseContext,
    DockerImage,
    DockerImagePage,
    DockerLayer,
    DockerManifest,
    DockerPlatformManifest,
    DockerTag,
    DockerTagPage,
    Platform,
    decode_cursor,
    encode_cursor,
)
from waystation_server.db import assets as assets_db
from waystation_server.db import browse as browse_db
from waystation_server.db import docker as docker_db
from waystation_server.formats.docker import protocol as p
from waystation_server.formats.docker.descriptor import DockerOptions

MAX_ENTRIES = 256
MAX_LAYERS = 1000
MAX_LABELS = 100
MAX_HISTORY = 1000
MAX_TEXT = 1024
CONFIG_MAX_BYTES = 4 * 1024 * 1024
CONFIG_CACHE = 256
UNKNOWN = 'unknown'


# ---------------------------------------------------------------------------- pure helpers (run in a thread)
def _obj(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _digest(value: Any) -> str | None:
    return value if isinstance(value, str) and p.DIGEST_RE.fullmatch(value) else None


def _size(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _text(value: Any, limit: int = MAX_TEXT) -> str | None:
    return value[:limit] if isinstance(value, str) and value else None


def _platform(value: Any) -> dict[str, str | None] | None:
    if not isinstance(value, dict):
        return None
    os_, arch = _text(value.get('os'), 64), _text(value.get('architecture'), 64)
    if os_ is None or arch is None:
        return None
    return {'os': os_, 'architecture': arch, 'variant': _text(value.get('variant'), 64)}


def parse_manifest(data: bytes, content_type: str | None) -> dict[str, Any]:
    """The ``docker_manifests`` fields of a manifest or index (bounded). Raises ``ValidationFailed``."""
    try:
        doc = json.loads(data)
    except ValueError:
        raise ValidationFailed('manifest is not valid JSON') from None
    if not isinstance(doc, dict):
        raise ValidationFailed('manifest is not a JSON object')
    media_type = _text(doc.get('mediaType'), 255) or p.media_type(content_type) or p.OCI_MANIFEST
    subject = doc.get('subject')
    record: dict[str, Any] = {
        'media_type': media_type,
        'subject_digest': _digest(subject.get('digest')) if isinstance(subject, dict) else None,
        'artifact_type': _text(doc.get('artifactType'), 255),
    }
    entries = doc.get('manifests')
    if isinstance(entries, list):
        items: list[dict[str, Any]] = []
        for entry in entries[:MAX_ENTRIES]:
            digest = _digest(entry.get('digest')) if isinstance(entry, dict) else None
            if digest is None:
                continue
            items.append(
                {
                    'digest': digest,
                    'size': _size(entry.get('size')),
                    'media_type': _text(entry.get('mediaType'), 255) or '',
                    'platform': _platform(entry.get('platform')),
                }
            )
        platforms = [e['platform'] for e in items if e['platform'] and e['platform']['os'] != UNKNOWN]
        return record | {
            'is_index': True,
            'manifests': items,
            'platforms': platforms,
            'layers': [],
            'config_digest': None,
            'config_size': 0,
            'size_total': sum(e['size'] for e in items),
        }
    layers: list[dict[str, Any]] = []
    for layer in doc.get('layers') or []:
        digest = _digest(layer.get('digest')) if isinstance(layer, dict) else None
        if digest is None:
            continue
        layers.append(
            {'digest': digest, 'size': _size(layer.get('size')), 'media_type': _text(layer.get('mediaType'), 255) or ''}
        )
        if len(layers) >= MAX_LAYERS:
            break
    config = _obj(doc.get('config'))
    config_size = _size(config.get('size'))
    return record | {
        'is_index': False,
        'manifests': [],
        'platforms': [],
        'layers': layers,
        'config_digest': _digest(config.get('digest')),
        'config_size': config_size,
        'size_total': config_size + sum(layer['size'] for layer in layers),
    }


@dataclass(frozen=True)
class ImageConfig:
    platform: Platform | None = None
    created: datetime | None = None
    labels: dict[str, str] = field(default_factory=dict)
    layer_history: list[str | None] = field(default_factory=list)  # created_by of each non-empty layer, in order


def parse_config(data: bytes) -> ImageConfig:
    """The browse view of an image config blob (bounded). Malformed configs give an empty view."""
    try:
        doc = json.loads(data)
    except ValueError:
        return ImageConfig()
    if not isinstance(doc, dict):
        return ImageConfig()
    platform = _platform(doc)
    created: datetime | None = None
    if isinstance(doc.get('created'), str):
        try:
            created = datetime.fromisoformat(doc['created'])
        except ValueError:
            created = None
    raw_labels = _obj(_obj(doc.get('config')).get('Labels'))
    labels = {str(k)[:255]: v[:MAX_TEXT] for k, v in list(raw_labels.items())[:MAX_LABELS] if isinstance(v, str)}
    history: list[str | None] = []
    for entry in (doc.get('history') or [])[:MAX_HISTORY]:
        if isinstance(entry, dict) and not entry.get('empty_layer'):
            history.append(_text(entry.get('created_by')))
    return ImageConfig(
        platform=Platform(
            os=str(platform['os']), architecture=str(platform['architecture']), variant=platform['variant']
        )
        if platform
        else None,
        created=created,
        labels=labels,
        layer_history=history,
    )


def platform_key(platform: Platform) -> str:
    return '/'.join(x for x in (platform.os, platform.architecture, platform.variant) if x)


def _platforms(doc: dict[str, Any]) -> list[Platform]:
    return [Platform(**pl) for pl in doc.get('platforms') or [] if isinstance(pl, dict)]


def _entries(doc: dict[str, Any]) -> list[DockerPlatformManifest]:
    out = []
    for entry in doc.get('manifests') or []:
        platform = entry.get('platform')
        if isinstance(platform, dict) and platform.get('os') != UNKNOWN:
            out.append(
                DockerPlatformManifest(platform=Platform(**platform), digest=entry['digest'], size=entry['size'])
            )
    return out


# ---------------------------------------------------------------------------- views
class DockerBrowse:
    def __init__(self) -> None:
        self._configs: OrderedDict[str, ImageConfig] = OrderedDict()

    async def _config(self, ctx: BrowseContext, digest: str | None) -> ImageConfig | None:
        """The image config blob ``digest``, when cached."""
        if digest is None:
            return None
        found = self._configs.get(digest)
        if found is not None:
            self._configs.move_to_end(digest)
            return found
        asset = await assets_db.get(ctx.db, ctx.repo.id, f'blobs/{digest}')
        if asset is None or asset.size > CONFIG_MAX_BYTES:
            return None
        config = await asyncio.to_thread(parse_config, await ctx.proxy.read(asset, max_bytes=CONFIG_MAX_BYTES))
        self._configs[digest] = config
        while len(self._configs) > CONFIG_CACHE:
            self._configs.popitem(last=False)
        return config

    async def images(self, ctx: BrowseContext, *, q: str | None, cursor: str | None, limit: int) -> DockerImagePage:
        after = decode_cursor(cursor, str)[0] if cursor else None
        docs = await docker_db.images_page(ctx.db, ctx.repo.id, after=after, q=q, limit=limit + 1)
        more = len(docs) > limit
        docs = docs[:limit]
        names = [str(d['_id']) for d in docs]
        referenced: dict[str, set[str]] = {name: set() for name in names}
        for manifest in await docker_db.manifests_of_images(ctx.db, ctx.repo.id, names):
            blobs = referenced[str(manifest['image'])]
            if manifest.get('config_digest'):
                blobs.add(str(manifest['config_digest']))
            blobs.update(str(layer['digest']) for layer in manifest.get('layers') or [])
        everything = {d for blobs in referenced.values() for d in blobs}
        cached = await browse_db.by_paths(ctx.db, ctx.repo.id, (f'blobs/{d}' for d in everything))
        items = [
            DockerImage(
                name=name,
                tag_count=int(doc['tag_count']),
                size_bytes=sum(int(cached[f'blobs/{d}']['size']) for d in referenced[name] if f'blobs/{d}' in cached),
            )
            for name, doc in zip(names, docs, strict=True)
        ]
        return DockerImagePage(items=items, next_cursor=encode_cursor(names[-1]) if more and names else None)

    async def tags(self, ctx: BrowseContext, *, image: str, cursor: str | None, limit: int) -> DockerTagPage:
        after: tuple[datetime, str] | None = None
        if cursor:
            raw_time, tag = decode_cursor(cursor, str, str)
            try:
                after = (datetime.fromisoformat(raw_time), tag)
            except ValueError:
                raise ValidationFailed('invalid cursor') from None
        docs = await docker_db.tags_page(ctx.db, ctx.repo.id, image, after=after, limit=limit + 1)
        if not docs and cursor is None:
            raise NotFound('image not found')
        more = len(docs) > limit
        docs = docs[:limit]
        manifests = await docker_db.manifests(ctx.db, ctx.repo.id, image, (str(d['manifest_digest']) for d in docs))
        children = await docker_db.manifests(
            ctx.db,
            ctx.repo.id,
            image,
            (e['digest'] for m in manifests.values() for e in m.get('manifests') or []),
        )
        items = []
        for doc in docs:
            digest = str(doc['manifest_digest'])
            manifest = manifests.get(digest, {})
            items.append(await self._tag(ctx, doc, digest, manifest, children))
        next_cursor = None
        if more and docs:
            last = docs[-1]
            next_cursor = encode_cursor(last['updated_at'].isoformat(), str(last['tag']))
        return DockerTagPage(image=image, items=items, next_cursor=next_cursor)

    async def _tag(
        self,
        ctx: BrowseContext,
        doc: dict[str, Any],
        digest: str,
        manifest: dict[str, Any],
        children: dict[str, dict[str, Any]],
    ) -> DockerTag:
        if manifest.get('is_index'):
            platforms = _platforms(manifest)
            recorded = [children[e['digest']] for e in manifest.get('manifests') or [] if e['digest'] in children]
            size = sum(int(c.get('size_total') or 0) for c in recorded)
            config = await self._config(ctx, recorded[0].get('config_digest')) if recorded else None
        else:
            config = await self._config(ctx, manifest.get('config_digest'))
            platforms = [config.platform] if config is not None and config.platform is not None else []
            size = int(manifest.get('size_total') or 0)
        return DockerTag(
            tag=str(doc['tag']),
            digest=digest,
            media_type=str(manifest.get('media_type') or ''),
            platforms=platforms,
            size_bytes=size,
            created_at=config.created if config else None,
            updated_at=doc['updated_at'],
        )

    async def manifest(self, ctx: BrowseContext, *, image: str, reference: str, platform: str | None) -> DockerManifest:
        if p.DIGEST_RE.fullmatch(reference):
            digest = reference
            tag = await docker_db.tag_for_digest(ctx.db, ctx.repo.id, image, digest)
        else:
            found = await docker_db.get_tag(ctx.db, ctx.repo.id, image, reference)
            if found is None:
                raise NotFound('tag not found')
            digest, tag = str(found['manifest_digest']), reference
        doc = (await docker_db.manifests(ctx.db, ctx.repo.id, image, [digest])).get(digest)
        if doc is None:
            raise NotFound('manifest not found')
        entries: list[DockerPlatformManifest] = []
        chosen: Platform | None = None
        platform_digest: str | None = digest
        source: dict[str, Any] | None = doc
        if doc.get('is_index'):
            entries = _entries(doc)
            pick = next((e for e in entries if platform and platform_key(e.platform) == platform), None)
            pick = pick or (entries[0] if entries else None)
            chosen = pick.platform if pick else None
            platform_digest = pick.digest if pick else None
            source = (
                (await docker_db.manifests(ctx.db, ctx.repo.id, image, [pick.digest])).get(pick.digest)
                if pick
                else None
            )
        config = await self._config(ctx, source.get('config_digest') if source else None)
        if chosen is None and config is not None:
            chosen = config.platform
        layers_raw = (source.get('layers') if source else None) or []
        cached = await browse_db.by_paths(ctx.db, ctx.repo.id, (f'blobs/{layer["digest"]}' for layer in layers_raw))
        history = config.layer_history if config else []
        layers = [
            DockerLayer(
                digest=layer['digest'],
                size=int(layer['size']),
                media_type=str(layer.get('media_type') or ''),
                cached=f'blobs/{layer["digest"]}' in cached,
                created_by=history[i] if i < len(history) else None,
            )
            for i, layer in enumerate(layers_raw)
        ]
        pull = self._pull_name(ctx, image)
        return DockerManifest(
            image=image,
            reference=reference,
            digest=digest,
            media_type=str(doc.get('media_type') or ''),
            is_index=bool(doc.get('is_index')),
            manifests=entries,
            platform=chosen,
            platform_digest=platform_digest,
            config_digest=source.get('config_digest') if source else None,
            created_at=config.created if config else None,
            layers=layers,
            total_size=sum(layer.size for layer in layers),
            labels=config.labels if config else {},
            pull_by_tag=f'docker pull {pull}:{tag}' if tag else '',
            pull_by_digest=f'docker pull {pull}@{digest}',
        )

    @staticmethod
    def _pull_name(ctx: BrowseContext, image: str) -> str:
        """How a client names the image through this repository (Docker Hub official images without ``library/``)."""
        options = DockerOptions.model_validate(ctx.repo.format_options)
        name = image
        if options.docker_hub and image.startswith('library/') and image.count('/') == 1:
            name = image.removeprefix('library/')
        return f'{ctx.registry_host}/{ctx.repo.name}/{name}'
