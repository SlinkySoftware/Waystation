"""``cargo publish`` payloads (FORMATS.md "Cargo"): the publish JSON, the index entry made from it, and the checks of
the ``.crate`` archive (run in a thread).

The body of ``PUT /api/v1/crates/new`` is a 32-bit little-endian length, the metadata JSON, another length and the
``.crate`` file (a gzipped tar of ``<name>-<version>/...``). The JSON is bounded (``MAX_JSON``) and checked field by
field; the archive must be a readable gzip tar whose every entry is a safe relative path under ``<name>-<version>/``
(no absolute paths, ``..``, backslashes or NUL; no device files), bounded in entries and unpacked size, with a
``Cargo.toml`` whose ``[package]`` name and version are the published ones.
"""

from __future__ import annotations

import json
import tarfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from waystation_server.formats.cargo.common import valid_name, valid_version

MAX_JSON = 8 * 1024 * 1024
MAX_ENTRIES = 50_000
MAX_UNPACKED = 2 * 1024 * 1024 * 1024
MAX_MANIFEST = 4 * 1024 * 1024
MAX_DEPS = 2_000
MAX_FEATURES = 5_000
KINDS = frozenset({'normal', 'dev', 'build'})


class Invalid(Exception):  # noqa: N818 - reads as an outcome
    """The payload is not acceptable (the message is shown by cargo)."""


@dataclass(frozen=True)
class Published:
    name: str
    vers: str
    deps: list[dict[str, Any]]
    features: dict[str, list[str]]
    links: str | None = None
    rust_version: str | None = None
    description: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def _str(doc: dict[str, Any], key: str, *, limit: int = 1000) -> str | None:
    value = doc.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise Invalid(f'invalid {key!r} in the publish metadata')
    return value


def _strings(value: Any, what: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) and len(v) <= 500 for v in value):
        raise Invalid(f'invalid {what} in the publish metadata')
    return list(value)


def _dep(doc: Any) -> dict[str, Any]:
    if not isinstance(doc, dict):
        raise Invalid('invalid dependency in the publish metadata')
    name = doc.get('name')
    if not isinstance(name, str) or not valid_name(name):
        raise Invalid(f'invalid dependency name {name!r}')
    req = _str(doc, 'version_req', limit=500)
    if req is None:
        raise Invalid(f'dependency {name} has no version requirement')
    kind = doc.get('kind') or 'normal'
    if kind not in KINDS:
        raise Invalid(f'dependency {name} has an invalid kind')
    explicit = _str(doc, 'explicit_name_in_toml', limit=64)
    if explicit is not None and not valid_name(explicit):
        raise Invalid(f'dependency {name} has an invalid renamed name')
    optional, default_features = doc.get('optional', False), doc.get('default_features', True)
    if not isinstance(optional, bool) or not isinstance(default_features, bool):
        raise Invalid(f'dependency {name} has invalid flags')
    entry: dict[str, Any] = {
        'name': explicit or name,
        'req': req,
        'features': _strings(doc.get('features', []), f'features of dependency {name}'),
        'optional': optional,
        'default_features': default_features,
        'target': _str(doc, 'target', limit=500),
        'kind': kind,
    }
    registry = _str(doc, 'registry', limit=2000)
    if registry is not None:
        entry['registry'] = registry
    if explicit is not None:
        entry['package'] = name
    return entry


def parse_metadata(data: bytes) -> Published:
    """The publish JSON (bounded by the caller), checked."""
    try:
        doc = json.loads(data)
    except ValueError:
        raise Invalid('the publish metadata is not valid JSON') from None
    if not isinstance(doc, dict):
        raise Invalid('the publish metadata is not a JSON object')
    name, vers = doc.get('name'), doc.get('vers')
    if not isinstance(name, str) or not valid_name(name):
        raise Invalid(f'invalid crate name {name!r}: ASCII letters, digits, - and _, a letter first, at most 64')
    if not isinstance(vers, str) or not valid_version(vers):
        raise Invalid(f'invalid version {vers!r}: a SemVer version is required')
    deps = doc.get('deps', [])
    if not isinstance(deps, list) or len(deps) > MAX_DEPS:
        raise Invalid('invalid dependencies in the publish metadata')
    features = doc.get('features', {})
    if not isinstance(features, dict) or len(features) > MAX_FEATURES:
        raise Invalid('invalid features in the publish metadata')
    checked: dict[str, list[str]] = {}
    for key, values in features.items():
        if not isinstance(key, str) or len(key) > 200:
            raise Invalid('invalid feature name in the publish metadata')
        checked[key] = _strings(values, f'feature {key}')
    return Published(
        name=name,
        vers=vers,
        deps=[_dep(d) for d in deps],
        features=checked,
        links=_str(doc, 'links', limit=200),
        rust_version=_str(doc, 'rust_version', limit=100),
        description=_str(doc, 'description', limit=10_000),
    )


def index_entry(meta: Published, cksum: str) -> dict[str, Any]:
    """The index entry of a published version (without ``yanked``): features using ``dep:`` or ``?/`` syntax go to
    ``features2`` with ``v: 2``, as crates.io does."""
    plain: dict[str, list[str]] = {}
    newer: dict[str, list[str]] = {}
    for key, values in meta.features.items():
        target = newer if any(v.startswith('dep:') or '?/' in v for v in values) else plain
        target[key] = values
    entry: dict[str, Any] = {
        'name': meta.name,
        'vers': meta.vers,
        'deps': sorted(meta.deps, key=lambda d: (str(d['name']), str(d['kind']))),
        'cksum': cksum,
        'features': plain,
        'links': meta.links,
    }
    if newer:
        entry['features2'] = newer
        entry['v'] = 2
    if meta.rust_version:
        entry['rust_version'] = meta.rust_version
    return entry


def _safe(name: str, top: str) -> bool:
    if not name or '\\' in name or '\x00' in name or name.startswith('/'):
        return False
    parts = PurePosixPath(name).parts
    return bool(parts) and parts[0] == top and '..' not in parts


def inspect_crate(path: Path, name: str, vers: str) -> None:
    """Check a ``.crate`` file (raises ``Invalid``)."""
    top = f'{name}-{vers}'
    manifest: bytes | None = None
    total = 0
    try:
        with tarfile.open(path, mode='r:gz') as archive:
            for count, member in enumerate(archive, start=1):
                if count > MAX_ENTRIES:
                    raise Invalid('the crate has too many files')
                if not _safe(member.name, top):
                    raise Invalid(f'the crate has a file outside {top}/: {member.name[:200]!r}')
                if not (member.isfile() or member.isdir() or member.issym()):
                    raise Invalid(f'the crate has an unsupported entry: {member.name[:200]!r}')
                if member.issym() and (member.linkname.startswith('/') or '..' in PurePosixPath(member.linkname).parts):
                    raise Invalid(f'the crate has a link leaving the package: {member.name[:200]!r}')
                total += member.size
                if total > MAX_UNPACKED:
                    raise Invalid('the crate is too large unpacked')
                if member.name == f'{top}/Cargo.toml' and member.isfile():
                    if member.size > MAX_MANIFEST:
                        raise Invalid('Cargo.toml is too large')
                    handle = archive.extractfile(member)
                    manifest = handle.read() if handle is not None else None
    except tarfile.TarError, OSError, EOFError:
        raise Invalid('the crate is not a gzipped tar archive') from None
    if manifest is None:
        raise Invalid(f'the crate has no {top}/Cargo.toml')
    try:
        doc = tomllib.loads(manifest.decode('utf-8'))
    except UnicodeDecodeError, tomllib.TOMLDecodeError:
        raise Invalid('Cargo.toml in the crate is not valid TOML') from None
    package = doc.get('package')
    if not isinstance(package, dict):
        raise Invalid('Cargo.toml in the crate has no [package]')
    if package.get('name') != name or package.get('version') != vers:
        raise Invalid(f'Cargo.toml in the crate is not for {name} {vers}')
