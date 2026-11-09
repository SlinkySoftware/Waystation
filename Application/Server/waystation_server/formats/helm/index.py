"""Helm repository indexes (FORMATS.md "Helm"): safe YAML loading, the proxy rewrite, listings, group merges.

Pure functions, no I/O; callers run them in a thread (indexes can be tens of MiB).

**Loading** (``load_document``): JSON when the bytes are JSON (what Waystation itself serves), else YAML through a
loader that resolves only ``null`` implicitly (every other plain scalar stays a string: no YAML 1.1 booleans,
numbers or timestamps that would change a chart's ``version: 1.10`` into ``1.1``), refuses Python tags (safe
constructor) and aliases (a shared node would expand exponentially when written out: "billion laughs"), and is
bounded in nodes and depth.

**Serving**: every index Waystation serves is JSON, which is YAML; Helm 3.11+ detects JSON indexes and parses them
directly, older Helm and other tools read them as YAML. Field types follow Helm's ``ChartVersion``: strings, except
``deprecated``/``removed`` and a dependency's ``enabled`` (booleans; YAML spellings like ``yes`` accepted).

**Proxy rewrite** (``rewrite_index``): every chart URL becomes a URL relative to the repository, so the index works
behind any host name and in groups unchanged. A URL under the upstream base keeps its relative path
(``charts/foo-1.0.0.tgz``); any other URL (charts on GitHub releases...) maps to ``_ext/<scheme>/<host>/<path>``.
The repository fetches a chart only for a path its cached index lists (``helm_charts``), so ``_ext/`` is never an
open proxy. Query strings of chart URLs are dropped. Entries without a name, version or usable URL are left out;
a ``digest`` that is not 64 hex digits is dropped (the chart is then stored unverified).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlsplit

import yaml
from yaml.composer import ComposerError
from yaml.resolver import BaseResolver

from waystation_common.errors import PathRejected, ValidationFailed
from waystation_common.paths import normalise

INDEX_PATH = 'index.yaml'
INDEX_TYPE = 'application/x-yaml'
EXT = '_ext'
MAX_NODES = 5_000_000
MAX_DEPTH = 64
MAX_TEXT = 1024
MAX_URL = 2048
URL_SAFE = "/-._~!$&'()*+,;=@"  # ':' escaped so a first segment is never read as a scheme
BOOL_FIELDS = frozenset({'deprecated', 'removed'})
_TRUE = frozenset({'true', 'yes', 'on', 'y'})
_FALSE = frozenset({'false', 'no', 'off', 'n'})
_DIGEST = re.compile(r'[0-9a-f]{64}')
_SEMVER = re.compile(
    r'^v?(0|[1-9]\d*)(?:\.(0|[1-9]\d*))?(?:\.(0|[1-9]\d*))?(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$'
)
_NUMBER_WIDTH = 10


# ---------------------------------------------------------------------------- versions
def version_sort(version: str) -> str:
    """A string whose byte order is semver precedence (prereleases before their release, numeric identifiers
    numerically; ``1.2`` = ``1.2.0`` as Helm reads it). Anything that is not semver sorts before every valid
    version, by string."""
    match = _SEMVER.match(version)
    if match is None:
        return '0' + version
    major, minor, patch, pre = match.groups()
    key = '1' + ''.join(str(int(n or 0))[-_NUMBER_WIDTH:].zfill(_NUMBER_WIDTH) for n in (major, minor, patch))
    if not pre:
        return key + '~'  # a release sorts after its prereleases
    parts = []
    for ident in pre.split('.'):
        parts.append('0' + ident[-_NUMBER_WIDTH:].zfill(_NUMBER_WIDTH) if ident.isdigit() else '1' + ident)
    return key + '!' + '!'.join(parts)


def is_semver(version: str) -> bool:
    return _SEMVER.match(version) is not None


# ---------------------------------------------------------------------------- loading
class _Resolver(BaseResolver):
    """Implicit types: ``null`` only (YAML 1.2 core spellings); every other plain scalar is a string."""


_Resolver.add_implicit_resolver('tag:yaml.org,2002:null', re.compile(r'^(?:~|null|Null|NULL|)$'), ['~', 'n', 'N', ''])

if getattr(yaml, '__with_libyaml__', False):  # pragma: no branch - libyaml ships in the PyYAML wheels
    from yaml._yaml import CParser

    class _Loader(CParser, yaml.constructor.SafeConstructor, _Resolver):
        def __init__(self, stream: bytes) -> None:
            CParser.__init__(self, stream)
            yaml.constructor.SafeConstructor.__init__(self)
            _Resolver.__init__(self)

else:  # pragma: no cover

    class _Loader(  # type: ignore[no-redef]
        yaml.reader.Reader,
        yaml.scanner.Scanner,
        yaml.parser.Parser,
        yaml.composer.Composer,
        yaml.constructor.SafeConstructor,
        _Resolver,
    ):
        def __init__(self, stream: bytes) -> None:
            yaml.reader.Reader.__init__(self, stream)
            yaml.scanner.Scanner.__init__(self)
            yaml.parser.Parser.__init__(self)
            yaml.composer.Composer.__init__(self)
            yaml.constructor.SafeConstructor.__init__(self)
            _Resolver.__init__(self)


class _Walk:
    """Bounds a loaded document: node count, depth, and no shared containers (aliases)."""

    def __init__(self) -> None:
        self.nodes = 0
        self.seen: set[int] = set()

    def visit(self, value: Any, depth: int) -> None:
        self.nodes += 1
        if self.nodes > MAX_NODES:
            raise ValidationFailed('the document is too large')
        if depth > MAX_DEPTH:
            raise ValidationFailed('the document is nested too deeply')
        if isinstance(value, dict | list):
            if id(value) in self.seen:
                raise ValidationFailed('YAML aliases are not allowed')
            self.seen.add(id(value))


def _check(value: Any) -> None:
    walk = _Walk()
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        walk.visit(item, depth)
        if isinstance(item, dict):
            stack.extend((v, depth + 1) for v in item.values())
        elif isinstance(item, list):
            stack.extend((v, depth + 1) for v in item)


def load_yaml(data: bytes) -> Any:
    """A YAML document loaded safely (see the module docstring); raises ``ValidationFailed``."""
    try:
        loader = _Loader(data)
        try:
            doc = loader.get_single_data()
        finally:
            loader.dispose()
    except ComposerError as exc:
        raise ValidationFailed(f'not a valid YAML document ({exc.problem or "composer error"})') from None
    except yaml.YAMLError as exc:
        raise ValidationFailed(f'not a valid YAML document ({type(exc).__name__})') from None
    except RecursionError:
        raise ValidationFailed('the document is nested too deeply') from None
    _check(doc)
    return doc


def load_document(data: bytes) -> Any:
    """JSON when ``data`` is JSON, else YAML (safe, bounded)."""
    head = data[:64].lstrip()
    if head.startswith(b'{'):
        try:
            doc = json.loads(data)
        except ValueError:
            pass
        else:
            _check(doc)
            return doc
    return load_yaml(data)


# ---------------------------------------------------------------------------- normalising entries
def _scalar(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode('utf-8', 'replace')
    return str(value)


def _bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        folded = value.strip().lower()
        if folded in _TRUE:
            return True
        if folded in _FALSE:
            return False
    return None


def plain(value: Any) -> Any:
    """Any loaded value as JSON data with string scalars (maps keyed by strings)."""
    if isinstance(value, dict):
        return {str(_scalar(k)): plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [plain(v) for v in value]
    return _scalar(value)


def normalise_entry(entry: dict[Any, Any]) -> dict[str, Any]:
    """A chart version (index entry or ``Chart.yaml``) with Helm's field types."""
    out: dict[str, Any] = {}
    for key, value in entry.items():
        name = str(_scalar(key))
        if name in BOOL_FIELDS:
            flag = _bool(value)
            if flag is not None:
                out[name] = flag
        elif name == 'dependencies' and isinstance(value, list):
            out[name] = [_dependency(d) for d in value if isinstance(d, dict)]
        else:
            out[name] = plain(value)
    return out


def _dependency(dep: dict[Any, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in dep.items():
        name = str(_scalar(key))
        if name == 'enabled':
            flag = _bool(value)
            if flag is not None:
                out[name] = flag
        else:
            out[name] = plain(value)
    return out


def _text(value: Any, limit: int = MAX_TEXT) -> str | None:
    return value[:limit] if isinstance(value, str) and value else None


def parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().replace('Z', '+00:00').replace('z', '+00:00')
    # Python accepts at most 6 fractional digits; Go writes up to 9.
    text = re.sub(r'(\.\d{6})\d+', r'\1', text)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def dump(doc: dict[str, Any]) -> bytes:
    return json.dumps(doc, ensure_ascii=False, separators=(',', ':')).encode() + b'\n'


def empty_index(now: datetime) -> bytes:
    return dump({'apiVersion': 'v1', 'entries': {}, 'generated': now.astimezone(UTC).isoformat()})


# ---------------------------------------------------------------------------- chart URLs (proxy)
def _base(upstream_url: str) -> str:
    return upstream_url if upstream_url.endswith('/') else upstream_url + '/'


def index_url(upstream_url: str) -> str:
    return _base(upstream_url) + INDEX_PATH


def chart_path(url: str, upstream_url: str) -> str | None:
    """The repository path a chart URL of the upstream index is served at (None: not a usable URL)."""
    if not url or len(url) > MAX_URL:
        return None
    base = _base(upstream_url)
    resolved = urljoin(base, url.strip())
    parts = urlsplit(resolved)
    scheme = parts.scheme.lower()
    if scheme not in ('http', 'https') or not parts.hostname or '@' in parts.netloc:
        return None
    try:
        if resolved.startswith(base):
            relative = normalise(unquote(urlsplit(resolved[len(base) :]).path))
            if relative.split('/', 1)[0] != EXT and relative != INDEX_PATH:
                return relative
        host = parts.netloc.lower()
        return normalise(f'{EXT}/{scheme}/{host}/{unquote(parts.path)}')
    except PathRejected:
        return None


def upstream_of(path: str, upstream_url: str) -> str:
    """The upstream URL of a chart path made by ``chart_path``."""
    segments = path.split('/', 3)
    if segments[0] == EXT and len(segments) == 4:
        _, scheme, host, rest = segments
        return f'{scheme}://{host}/{quote(rest, safe=URL_SAFE)}'
    return _base(upstream_url) + quote(path, safe=URL_SAFE)


def url_of(path: str) -> str:
    """The relative URL the served index lists for a chart path."""
    return quote(path, safe=URL_SAFE)


# ---------------------------------------------------------------------------- indexes
def _entries(doc: Any) -> dict[Any, Any]:
    if not isinstance(doc, dict):
        raise ValidationFailed('not a Helm repository index')
    entries = doc.get('entries')
    if entries is None:
        return {}
    if not isinstance(entries, dict):
        raise ValidationFailed('not a Helm repository index (entries)')
    return entries


def rewrite_index(data: bytes, *, upstream_url: str) -> bytes:
    """Validate an upstream ``index.yaml`` and point every chart at this repository (see the module docstring).
    Raises ``ValidationFailed`` for anything that is not an index."""
    doc = load_document(data)
    entries = _entries(doc)
    out_entries: dict[str, list[dict[str, Any]]] = {}
    for key, versions in entries.items():
        if not isinstance(versions, list):
            continue
        chart = str(_scalar(key))
        kept: list[dict[str, Any]] = []
        for raw in versions:
            if not isinstance(raw, dict):
                continue
            entry = normalise_entry(raw)
            if not entry.get('name'):
                entry['name'] = chart
            urls = entry.get('urls')
            first = urls[0] if isinstance(urls, list) and urls and isinstance(urls[0], str) else None
            path = chart_path(first, upstream_url) if first else None
            if path is None or not isinstance(entry.get('version'), str) or not entry['version']:
                continue
            entry['urls'] = [url_of(path)]
            digest = entry.get('digest')
            if not (isinstance(digest, str) and _DIGEST.fullmatch(digest.strip().lower())):
                entry.pop('digest', None)
            else:
                entry['digest'] = digest.strip().lower()
            kept.append(entry)
        if kept:
            out_entries[chart] = kept
    out: dict[str, Any] = {'apiVersion': _scalar(doc.get('apiVersion')) or 'v1', 'entries': out_entries}
    for key in ('generated', 'serverInfo', 'annotations'):
        if doc.get(key) is not None:
            out[key] = plain(doc[key])
    return dump(out)


@dataclass(frozen=True)
class Listed:
    """One chart version of an index Waystation serves (``helm_charts`` fields)."""

    name: str
    version: str
    path: str
    digest: str | None
    created: datetime | None
    deprecated: bool
    app_version: str | None
    description: str | None
    icon: str | None

    def record(self) -> dict[str, Any]:
        return {
            'name': self.name,
            'version': self.version,
            'version_sort': version_sort(self.version),
            'path': self.path,
            'digest': self.digest,
            'created': self.created,
            'deprecated': self.deprecated,
            'app_version': self.app_version,
            'description': self.description,
            'icon': self.icon,
        }


def summary(entry: dict[str, Any]) -> dict[str, Any]:
    """Browse fields of a normalised entry or ``Chart.yaml``."""
    icon = _text(entry.get('icon'), MAX_URL)
    return {
        'deprecated': entry.get('deprecated') is True,
        'app_version': _text(entry.get('appVersion'), 256),
        'description': _text(entry.get('description')),
        'icon': icon if icon and icon.startswith(('https://', 'http://')) else None,
    }


def listing(data: bytes) -> list[Listed]:
    """The chart versions of an index Waystation wrote (rewritten proxy index): chart paths decoded."""
    out: list[Listed] = []
    for versions in _entries(load_document(data)).values():
        for entry in versions if isinstance(versions, list) else ():
            if not isinstance(entry, dict):
                continue
            name, version, urls = entry.get('name'), entry.get('version'), entry.get('urls')
            if not (isinstance(name, str) and isinstance(version, str) and isinstance(urls, list) and urls):
                continue
            if not isinstance(urls[0], str):
                continue
            try:
                path = normalise(unquote(urls[0]))
            except PathRejected:
                continue
            digest = entry.get('digest')
            info = summary(entry)
            out.append(
                Listed(
                    name=name[:256],
                    version=version[:256],
                    path=path,
                    digest=digest if isinstance(digest, str) and _DIGEST.fullmatch(digest) else None,
                    created=parse_time(entry.get('created')),
                    **info,
                )
            )
    return out


def merge(indexes: Iterable[bytes], *, now: datetime) -> bytes:
    """Group index: the members' indexes in order, the first member that lists a chart version wins it."""
    merged: dict[str, dict[str, dict[str, Any]]] = {}
    for data in indexes:
        for key, versions in _entries(load_document(data)).items():
            chart = merged.setdefault(str(key), {})
            for entry in versions if isinstance(versions, list) else ():
                if isinstance(entry, dict) and isinstance(entry.get('version'), str):
                    chart.setdefault(entry['version'], entry)
    entries = {
        name: sorted(versions.values(), key=lambda e: version_sort(e['version']), reverse=True)
        for name, versions in sorted(merged.items())
        if versions
    }
    return dump({'apiVersion': 'v1', 'entries': entries, 'generated': now.astimezone(UTC).isoformat()})
