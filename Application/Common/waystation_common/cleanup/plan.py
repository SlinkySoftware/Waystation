"""Cleanup planning: which cached content a policy removes from one repository (SPECIFICATION §4.8).

Pure functions over rows loaded by ``waystation_common.db.cleanup``. The server's preview and the worker's run call
the same ``plan_repository``, so a preview equals what a run removes at the same moment.

Components and versions come from asset paths (``coordinates``) until components are indexed (T-S09):

* npm ``<pkg>/-/<name>-<version>.tgz``; apt pool ``<name>_<version>_<arch>.deb`` (grouped per name and arch); yum
  ``<name>-<version>-<release>.<arch>.rpm`` (per name and arch); pypi ``packages/<project>/<wheel or sdist>`` (and
  its ``.metadata`` sidecar; per project, PEP 440, every file of a version shares its rank); go
  ``<module>/@v/<version>.{zip,mod,info}`` (case-encoding decoded; per module, Go semver); galaxy
  ``collections/<ns>/<name>/<ns>-<name>-<version>.tar.gz`` (per collection, SemVer); terraform
  ``<hostname>/<namespace>/<type>/<version>/<os>/<arch>/<archive>.zip`` (per provider, semver; every platform of a
  version shares its rank); every other format
  (raw...): the name is the path,
  the version the file name, grouped by folder and ordered by when it was cached.
* docker works on tags (name = image, version = tag; newest tags by ``updated_at``). Removing a tag also removes the
  manifests only it reached (index children, referrers) and the layers no remaining manifest uses (repository-wide,
  as layers are cached per repository); ``untagged_older_than_days`` removes manifests no tag reached before the
  run. Layer files no indexed manifest names are left alone.

Only ``content`` assets are candidates for non-docker formats; metadata (indexes, packuments) is never removed.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import unquote

from waystation_common.cleanup.model import Candidate, Criteria
from waystation_common.cleanup.versions import is_prerelease, sort_key
from waystation_common.galaxy import parse_artifact_path

DAY = timedelta(days=1)


@dataclass(frozen=True)
class AssetRow:
    id: str
    path: str
    kind: str  # content / metadata
    size: int
    blob_sha256: str
    created_at: datetime
    last_downloaded_at: datetime | None = None


@dataclass(frozen=True)
class TagRow:
    image: str
    tag: str
    digest: str
    updated_at: datetime


@dataclass(frozen=True)
class ManifestRow:
    image: str
    digest: str
    updated_at: datetime
    children: tuple[str, ...] = ()  # index entries
    subject: str | None = None  # referrers (signatures, SBOMs) point at their subject
    blobs: tuple[str, ...] = ()  # layer + config digests


@dataclass(frozen=True)
class Coord:
    name: str
    version: str | None
    group: str
    ordered_by_version: bool  # False: newest = most recently cached


@dataclass
class RepoData:
    """What a plan needs about one repository. ``truncated``: the loader stopped at its cap (preview)."""

    format: str
    assets: list[AssetRow] = field(default_factory=list)
    tags: list[TagRow] = field(default_factory=list)
    manifests: list[ManifestRow] = field(default_factory=list)
    truncated: bool = False


# ---------------------------------------------------------------- coordinates
def _parent(path: str) -> str:
    return path.rsplit('/', 1)[0] if '/' in path else ''


def coordinates(fmt: str, path: str) -> Coord:
    """Component name/version of a content asset path (see the module docstring)."""
    filename = path.rsplit('/', 1)[-1]
    if fmt == 'npm' and '/-/' in path and filename.endswith('.tgz'):
        package = path.split('/-/', 1)[0]
        base = package.rsplit('/', 1)[-1]
        if filename.startswith(base + '-'):
            return Coord(package, filename[len(base) + 1 : -4], package, True)
    if fmt == 'apt' and filename.endswith(('.deb', '.udeb', '.ddeb')):
        parts = filename.rsplit('.', 1)[0].split('_')
        if len(parts) == 3:
            name, version, arch = parts[0], unquote(parts[1]), parts[2]
            return Coord(name, version, f'{name}/{arch}', True)
    if fmt == 'yum' and filename.endswith('.rpm'):
        stem, _, arch = filename[:-4].rpartition('.')
        name_ver, _, release = stem.rpartition('-')
        name, _, version = name_ver.rpartition('-')
        if name and version and release and arch:
            return Coord(name, f'{version}-{release}', f'{name}.{arch}', True)
    if fmt == 'pypi' and path.startswith('packages/') and path.count('/') == 2:
        dist_version = pypi_version(filename.removesuffix('.metadata'))
        if dist_version is not None:
            project = path.split('/')[1]
            return Coord(project, dist_version, project, True)
    if fmt == 'go' and '/@v/' in path and filename.endswith(('.zip', '.mod', '.info')):
        escaped, _, _ = path.partition('/@v/')
        stem = filename.rsplit('.', 1)[0]
        if escaped and stem.startswith('v'):
            module = _go_unescape(escaped)
            return Coord(module, _go_unescape(stem), module, True)
    if fmt == 'galaxy':
        artifact = parse_artifact_path(path)
        if artifact is not None:
            return Coord(artifact.fqcn, artifact.version, artifact.fqcn, True)
    if fmt == 'terraform' and filename.endswith('.zip') and path.count('/') == 6:
        hostname, namespace, kind, version, _ = path.split('/', 4)
        provider = f'{hostname}/{namespace}/{kind}'
        return Coord(provider, version, provider, True)
    return Coord(path, filename, _parent(path), False)


_GO_BANG = re.compile(r'!([a-z])')


def _go_unescape(text: str) -> str:
    """A GOPROXY case-encoded module path or version (``!`` + lower-case = upper-case)."""
    return _GO_BANG.sub(lambda m: m.group(1).upper(), text)


_PYPI_SDIST = ('.tar.gz', '.zip', '.tar.bz2', '.tar.xz', '.tgz', '.tar')


def pypi_version(filename: str) -> str | None:
    """The version in a wheel (``name-version-...whl``) or sdist (``name-version.tar.gz``) file name."""
    if filename.endswith('.whl'):
        parts = filename[:-4].split('-')
        return parts[1] if len(parts) in (5, 6) and parts[1] else None
    for ext in _PYPI_SDIST:
        if filename.endswith(ext):
            name, _, version = filename[: -len(ext)].rpartition('-')
            return version if name and version else None
    return None


# ---------------------------------------------------------------- matching
@dataclass(frozen=True)
class _Item:
    name: str
    version: str | None
    group: str
    created_at: datetime
    last_downloaded_at: datetime  # never downloaded = since it was cached / published
    order: Any


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _search(pattern: str | None, text: str | None) -> bool | None:
    if pattern is None:
        return None
    return text is not None and re.search(pattern, text) is not None


def _reasons(criteria: Criteria, fmt: str, item: _Item, rank: int, now: datetime) -> list[str] | None:
    """Why ``item`` matches every set criterion, or None when one does not match (or it is protected)."""
    if criteria.keep_latest_n_versions is not None and rank < criteria.keep_latest_n_versions:
        return None
    if _search(criteria.name_exclude, item.name) or _search(criteria.version_exclude, item.version):
        return None
    reasons: list[str] = []
    if criteria.name_include is not None:
        if not _search(criteria.name_include, item.name):
            return None
        reasons.append('name matches')
    if criteria.version_include is not None:
        if not _search(criteria.version_include, item.version):
            return None
        reasons.append('version matches')
    if criteria.prerelease_only:
        if item.version is None or not is_prerelease(fmt, item.version):
            return None
        reasons.append('pre-release')
    if criteria.last_downloaded_older_than_days is not None:
        idle = (now - item.last_downloaded_at) // DAY
        if idle < criteria.last_downloaded_older_than_days:
            return None
        reasons.append(f'not downloaded for {idle} days')
    if criteria.published_older_than_days is not None:
        age = (now - item.created_at) // DAY
        if age < criteria.published_older_than_days:
            return None
        reasons.append(f'published {age} days ago')
    if criteria.keep_latest_n_versions is not None:
        reasons.append(f'not among the newest {criteria.keep_latest_n_versions}')
    return reasons


def _ranked(items: Iterable[tuple[_Item, Any]]) -> list[tuple[_Item, Any, int]]:
    """Rank within each group, newest first (0 = newest). Files of the same version share its rank (PyPI: the
    wheels and the sdist of one release)."""
    groups: dict[str, list[tuple[_Item, Any]]] = defaultdict(list)
    for item, ref in items:
        groups[item.group].append((item, ref))
    out: list[tuple[_Item, Any, int]] = []
    for members in groups.values():
        members.sort(key=lambda pair: pair[0].order, reverse=True)
        seen: dict[str, int] = {}
        for index, (item, ref) in enumerate(members):
            if item.version is None:
                out.append((item, ref, index))
                continue
            rank = seen.setdefault(item.version, len(seen))
            out.append((item, ref, rank))
    return out


# ---------------------------------------------------------------- plans
def plan_assets(fmt: str, criteria: Criteria, assets: Iterable[AssetRow], now: datetime) -> list[Candidate]:
    if not criteria.selects_versions:
        return []
    items: list[tuple[_Item, AssetRow]] = []
    for asset in assets:
        if asset.kind != 'content':
            continue
        coord = coordinates(fmt, asset.path)
        created = _aware(asset.created_at)
        last = max(created, _aware(asset.last_downloaded_at)) if asset.last_downloaded_at else created
        if coord.ordered_by_version and coord.version is not None:
            order: Any = (sort_key(fmt, coord.version), created, asset.path)
        else:
            order = (created, asset.path)
        items.append((_Item(coord.name, coord.version, coord.group, created, last, order), asset))
    out: list[Candidate] = []
    for item, asset, rank in _ranked(items):
        reasons = _reasons(criteria, fmt, item, rank, now)
        if reasons is None:
            continue
        out.append(
            Candidate(
                kind='asset',
                name=item.name,
                version=item.version,
                path=asset.path,
                size=asset.size,
                created_at=item.created_at,
                last_downloaded_at=_aware(asset.last_downloaded_at) if asset.last_downloaded_at else None,
                reason=', '.join(reasons),
                asset_id=asset.id,
                blob_sha256=asset.blob_sha256,
            )
        )
    return out


def _reach(
    roots: Iterable[tuple[str, str]],
    manifests: Mapping[tuple[str, str], ManifestRow],
    referrers: Mapping[tuple[str, str], list[tuple[str, str]]],
) -> set[tuple[str, str]]:
    seen: set[tuple[str, str]] = set()
    stack = [root for root in roots if root in manifests]
    while stack:
        key = stack.pop()
        if key in seen:
            continue
        seen.add(key)
        image = key[0]
        for child in manifests[key].children:
            if (image, child) in manifests:
                stack.append((image, child))
        stack.extend(referrers.get(key, ()))
    return seen


def plan_docker(
    criteria: Criteria,
    tags: Iterable[TagRow],
    manifests: Iterable[ManifestRow],
    assets: Iterable[AssetRow],
    now: datetime,
) -> list[Candidate]:
    by_path = {a.path: a for a in assets}
    by_key = {(m.image, m.digest): m for m in manifests}
    referrers: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    for m in by_key.values():
        if m.subject:
            referrers[(m.image, m.subject)].append((m.image, m.digest))
    all_tags = list(tags)
    out: list[Candidate] = []
    removed_tags: set[tuple[str, str]] = set()
    if criteria.selects_versions:
        items: list[tuple[_Item, TagRow]] = []
        for t in all_tags:
            asset = by_path.get(f'{t.image}/manifests/{t.tag}')
            updated = _aware(t.updated_at)
            last = updated
            if asset is not None and asset.last_downloaded_at is not None:
                last = max(updated, _aware(asset.last_downloaded_at))
            items.append((_Item(t.image, t.tag, t.image, updated, last, (updated, t.tag)), t))
        for item, t, rank in _ranked(items):
            reasons = _reasons(criteria, 'docker', item, rank, now)
            if reasons is None:
                continue
            removed_tags.add((t.image, t.tag))
            path = f'{t.image}/manifests/{t.tag}'
            asset = by_path.get(path)
            out.append(
                Candidate(
                    kind='tag',
                    name=t.image,
                    version=t.tag,
                    path=path,
                    size=asset.size if asset else 0,
                    created_at=item.created_at,
                    last_downloaded_at=_aware(asset.last_downloaded_at) if asset and asset.last_downloaded_at else None,
                    reason=', '.join(reasons),
                    asset_id=asset.id if asset else None,
                    blob_sha256=asset.blob_sha256 if asset else None,
                    image=t.image,
                    tag=t.tag,
                    digest=t.digest,
                )
            )
    before = _reach(((t.image, t.digest) for t in all_tags), by_key, referrers)
    after = (
        _reach(((t.image, t.digest) for t in all_tags if (t.image, t.tag) not in removed_tags), by_key, referrers)
        if removed_tags
        else before
    )
    removed: dict[tuple[str, str], str] = dict.fromkeys(before - after, 'only reached by removed tags')
    if criteria.untagged_older_than_days is not None:
        cutoff = now - criteria.untagged_older_than_days * DAY
        for key, m in by_key.items():
            if key not in before and _aware(m.updated_at) <= cutoff:
                age = (now - _aware(m.updated_at)) // DAY
                removed[key] = f'untagged, updated {age} days ago'
    for key, reason in removed.items():
        m = by_key[key]
        path = f'{m.image}/manifests/{m.digest}'
        asset = by_path.get(path)
        out.append(
            Candidate(
                kind='manifest',
                name=m.image,
                version=m.digest,
                path=path,
                size=asset.size if asset else 0,
                created_at=_aware(asset.created_at) if asset else _aware(m.updated_at),
                last_downloaded_at=_aware(asset.last_downloaded_at) if asset and asset.last_downloaded_at else None,
                reason=reason,
                asset_id=asset.id if asset else None,
                blob_sha256=asset.blob_sha256 if asset else None,
                image=m.image,
                digest=m.digest,
            )
        )
    if removed:
        kept = {b for key, m in by_key.items() if key not in removed for b in m.blobs}
        layers: dict[str, str] = {}
        for key in removed:
            for blob in by_key[key].blobs:
                if blob not in kept:
                    layers.setdefault(blob, key[0])
        for digest, image in layers.items():
            asset = by_path.get(f'blobs/{digest}')
            if asset is None:
                continue
            out.append(
                Candidate(
                    kind='layer',
                    name=image,
                    version=digest,
                    path=asset.path,
                    size=asset.size,
                    created_at=_aware(asset.created_at),
                    last_downloaded_at=_aware(asset.last_downloaded_at) if asset.last_downloaded_at else None,
                    reason='no remaining manifest uses it',
                    asset_id=asset.id,
                    blob_sha256=asset.blob_sha256,
                    digest=digest,
                )
            )
    return out


def plan_repository(data: RepoData, criteria: Criteria, now: datetime) -> list[Candidate]:
    """Everything a run of ``criteria`` removes from the repository now, in a stable order."""
    if data.format == 'docker':
        plan = plan_docker(criteria, data.tags, data.manifests, data.assets, now)
    else:
        plan = plan_assets(data.format, criteria, data.assets, now)
    plan.sort(key=Candidate.sort_key)
    return plan
