"""Reading a pushed ``.gem`` safely (FORMATS.md "RubyGems"; blocking: run in a thread).

A ``.gem`` is a plain tar of ``metadata.gz`` (the gzipped YAML ``Gem::Specification``), ``data.tar.gz`` (the files)
and optionally ``checksums.yaml.gz`` and signatures (``*.sig``). Nothing is extracted to disk:

- at most ``MAX_MEMBERS`` regular files with plain names (no links, directories, paths or devices);
- ``metadata.gz`` at most ``MAX_METADATA_GZ`` bytes, inflated to at most ``MAX_METADATA`` bytes, parsed by a YAML
  ``SafeLoader`` that reads every scalar as a string, maps Ruby tags (``!ruby/object:Gem::Version``...) to plain
  mappings, and bounds aliases and nesting (nothing Ruby-specific is ever instantiated);
- ``data.tar.gz`` must be gzip; it is hashed, not unpacked;
- ``checksums.yaml.gz``, when present, must match the SHA-256 / SHA-512 of ``metadata.gz`` and ``data.tar.gz``.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import re
import tarfile
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import IO, Any

import yaml

from waystation_common import rubygems
from waystation_common.archives import READ_ERRORS, BoundedTarInfo
from waystation_common.errors import ValidationFailed
from waystation_server.formats.rubygems import marshal
from waystation_server.formats.rubygems.protocol import REQUIREMENT_OPS

MAX_MEMBERS = 16
MAX_METADATA_GZ = 1024 * 1024
MAX_METADATA = 4 * 1024 * 1024
MAX_CHECKSUMS = 64 * 1024
MAX_ALIASES = 1000
MAX_DEPTH = 32
MAX_DEPENDENCIES = 500
MAX_TEXT = 64 * 1024
MERGE_TAG = 'tag:yaml.org,2002:merge'
CHUNK = 1024 * 1024
_MEMBER = re.compile(r'^(metadata\.gz|data\.tar\.gz|checksums\.yaml\.gz|[A-Za-z0-9._-]{1,64}\.sig)$')
_DATE = re.compile(r'^(\d{4})-(\d{2})-(\d{2})')


class _Loader(yaml.SafeLoader):
    """Safe YAML: every plain scalar is a string, Ruby tags are plain values, aliases and depth bounded."""

    yaml_implicit_resolvers: dict[Any, Any] = {}  # noqa: RUF012 - the class attribute PyYAML reads

    def compose_node(self, parent: yaml.Node | None, index: int) -> yaml.Node | None:
        if self.check_event(yaml.AliasEvent):
            self._aliases = getattr(self, '_aliases', 0) + 1
            if self._aliases > MAX_ALIASES:
                raise yaml.YAMLError('too many aliases')
        depth = getattr(self, '_depth', 0)
        if depth > MAX_DEPTH:
            raise yaml.YAMLError('nested too deeply')
        self._depth = depth + 1
        try:
            return super().compose_node(parent, index)
        finally:
            self._depth = depth

    def flatten_mapping(self, node: yaml.MappingNode) -> None:
        # Explicit ``!!merge`` keys: each one copies the merged mappings, exponential work for nested merges.
        if any(key.tag == MERGE_TAG for key, _ in node.value):
            raise yaml.YAMLError('YAML merge keys are not allowed')
        super().flatten_mapping(node)


def _plain(loader: _Loader, suffix: str, node: yaml.Node) -> Any:
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_scalar(node)  # type: ignore[arg-type]


def _binary(loader: _Loader, node: yaml.Node) -> str:
    if not isinstance(node, yaml.ScalarNode):
        raise yaml.YAMLError('a binary value must be a scalar')
    return bytes(loader.construct_yaml_binary(node)).decode('utf-8', 'replace')


# str, seq and map keep SafeLoader's constructors (exact tags win over the multi constructors).
_Loader.add_multi_constructor('!ruby/', _plain)
_Loader.add_multi_constructor('tag:yaml.org,2002:', _plain)  # any other standard tag read plainly
_Loader.add_constructor('tag:yaml.org,2002:binary', _binary)
_Loader.add_constructor('!binary', _binary)


def load_yaml(text: bytes) -> Any:
    try:
        return yaml.load(text.decode('utf-8'), Loader=_Loader)  # noqa: S506 - _Loader is a SafeLoader
    except UnicodeDecodeError:
        raise ValidationFailed('the gem metadata is not UTF-8') from None
    except (yaml.YAMLError, RecursionError, ValueError, TypeError) as exc:
        raise ValidationFailed(f'the gem metadata is not valid YAML ({type(exc).__name__})') from None


@dataclass(frozen=True)
class GemDependency:
    name: str
    requirements: tuple[tuple[str, str], ...]
    type: str  # runtime / development

    def text(self) -> str:
        return ', '.join(f'{op} {v}' for op, v in self.requirements) or '>= 0'


@dataclass(frozen=True)
class GemSpec:
    name: str
    version: str
    platform: str
    date: str
    summary: str
    description: str | None
    authors: tuple[str, ...]
    email: str | tuple[str, ...] | None
    homepage: str | None
    licenses: tuple[str, ...]
    metadata: dict[str, str]
    dependencies: tuple[GemDependency, ...]
    required_ruby_version: tuple[tuple[str, str], ...]
    required_rubygems_version: tuple[tuple[str, str], ...]
    rubygems_version: str
    specification_version: int

    @property
    def full_name(self) -> str:
        return rubygems.full_name(self.name, self.version, self.platform)

    @property
    def runtime(self) -> list[GemDependency]:
        return [d for d in self.dependencies if d.type == 'runtime']

    def marshal_fields(self) -> marshal.SpecFields:
        return marshal.SpecFields(
            name=self.name,
            version=self.version,
            platform=self.platform,
            date=self.date,
            summary=self.summary,
            description=self.description,
            authors=self.authors,
            email=self.email,
            homepage=self.homepage,
            licenses=self.licenses,
            metadata=self.metadata,
            dependencies=[
                marshal.Dependency(d.name, marshal.Requirement(d.requirements or (('>=', '0'),)), d.type)
                for d in self.dependencies
            ],
            required_ruby_version=marshal.Requirement(self.required_ruby_version or (('>=', '0'),)),
            required_rubygems_version=marshal.Requirement(self.required_rubygems_version or (('>=', '0'),)),
            rubygems_version=self.rubygems_version,
            specification_version=self.specification_version,
        )


def _text(value: Any, what: str, *, limit: int = MAX_TEXT, required: bool = False) -> str | None:
    if value is None or value == '':
        if required:
            raise ValidationFailed(f'the gem metadata has no {what}')
        return None
    if not isinstance(value, str):
        raise ValidationFailed(f'the gem metadata has an invalid {what}')
    return value[:limit]


def _texts(value: Any, what: str, *, limit: int = 100) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or len(value) > limit:
        raise ValidationFailed(f'the gem metadata has an invalid {what}')
    return tuple(str(v)[:1024] for v in value if isinstance(v, str) and v)


def _version(value: Any, what: str) -> str:
    text = value.get('version') if isinstance(value, dict) else value
    if not isinstance(text, str) or not rubygems.valid_version(text.strip()):
        raise ValidationFailed(f'the gem metadata has an invalid {what}')
    return text.strip()


def requirements(value: Any, what: str) -> tuple[tuple[str, str], ...]:
    """A ``Gem::Requirement`` mapping (``requirements: [[op, version], ...]``) → ((op, version), ...)."""
    if value is None:
        return ()
    items = value.get('requirements') if isinstance(value, dict) else None
    if not isinstance(items, list) or len(items) > 20:
        raise ValidationFailed(f'the gem metadata has an invalid {what}')
    out = []
    for item in items:
        if not isinstance(item, list) or len(item) != 2 or item[0] not in REQUIREMENT_OPS:
            raise ValidationFailed(f'the gem metadata has an invalid {what}')
        out.append((str(item[0]), _version(item[1], what)))
    return tuple(out)


def _symbol(value: Any) -> str:
    return value.removeprefix(':') if isinstance(value, str) else ''


def spec_from(doc: Any) -> GemSpec:
    """The fields Waystation keeps from a gem's YAML specification (validated, bounded)."""
    if not isinstance(doc, dict):
        raise ValidationFailed('the gem metadata is not a specification')
    name = _text(doc.get('name'), 'name', required=True) or ''
    if not rubygems.valid_name(name):
        raise ValidationFailed(f'{name[:100]!r} is not a valid gem name')
    version = _version(doc.get('version'), 'version')
    platform = doc.get('platform') or rubygems.RUBY
    if not isinstance(platform, str) or not rubygems.valid_platform(platform):
        raise ValidationFailed('the gem metadata has an invalid platform')
    deps_raw = doc.get('dependencies') or []
    if not isinstance(deps_raw, list) or len(deps_raw) > MAX_DEPENDENCIES:
        raise ValidationFailed('the gem metadata has an invalid dependency list')
    deps = []
    for dep in deps_raw:
        if not isinstance(dep, dict) or not isinstance(dep.get('name'), str) or not rubygems.valid_name(dep['name']):
            raise ValidationFailed('the gem metadata has an invalid dependency')
        kind = _symbol(dep.get('type')) or 'runtime'
        if kind not in ('runtime', 'development'):
            raise ValidationFailed('the gem metadata has an invalid dependency type')
        reqs = requirements(dep.get('requirement') or dep.get('version_requirements'), 'dependency requirement')
        deps.append(GemDependency(dep['name'], reqs, kind))
    date = doc.get('date')
    found = _DATE.match(date.strip()) if isinstance(date, str) else None
    meta = doc.get('metadata') or {}
    if not isinstance(meta, dict) or len(meta) > 100:
        raise ValidationFailed('the gem metadata has an invalid metadata map')
    email = doc.get('email')
    spec_version = doc.get('specification_version')
    return GemSpec(
        name=name,
        version=version,
        platform=platform,
        date='-'.join(found.groups()) if found else datetime.now(UTC).strftime('%Y-%m-%d'),
        summary=_text(doc.get('summary'), 'summary', limit=1024) or '',
        description=_text(doc.get('description'), 'description'),
        authors=_texts(doc.get('authors'), 'author list'),
        email=email[:1024] if isinstance(email, str) else (_texts(email, 'email list') or None),
        homepage=_text(doc.get('homepage'), 'homepage', limit=2048),
        licenses=_texts(doc.get('licenses'), 'license list'),
        metadata={str(k)[:128]: v[:1024] for k, v in meta.items() if isinstance(v, str)},
        dependencies=tuple(deps),
        required_ruby_version=requirements(doc.get('required_ruby_version'), 'Ruby requirement'),
        required_rubygems_version=requirements(doc.get('required_rubygems_version'), 'RubyGems requirement'),
        rubygems_version=(_text(doc.get('rubygems_version'), 'rubygems_version', limit=64) or '0'),
        specification_version=int(spec_version) if isinstance(spec_version, str) and spec_version.isdigit() else 4,
    )


def _inflate(data: bytes, limit: int, what: str) -> bytes:
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as gz:
            out = gz.read(limit + 1)
    except OSError, EOFError, zlib.error:
        raise ValidationFailed(f'{what} is not valid gzip') from None
    if len(out) > limit:
        raise ValidationFailed(f'{what} is too large')
    return out


@dataclass(frozen=True)
class ReadGem:
    spec: GemSpec
    sha256: str  # of the whole .gem


def _check_checksums(raw: bytes, digests: dict[str, dict[str, str]]) -> None:
    doc = load_yaml(_inflate(raw, MAX_CHECKSUMS, 'checksums.yaml.gz'))
    if not isinstance(doc, dict):
        raise ValidationFailed('checksums.yaml.gz is not a checksum list')
    for algorithm, entries in doc.items():
        name = str(algorithm).lower()
        if name not in ('sha256', 'sha512') or not isinstance(entries, dict):
            continue
        for member, value in entries.items():
            actual = digests.get(str(member), {}).get(name)
            if actual is not None and str(value).strip().lower() != actual:
                raise ValidationFailed(f'checksums.yaml.gz does not match {member}')


def read_gem(file: IO[bytes]) -> ReadGem:
    """Check a ``.gem`` and read its specification (see the module docstring)."""
    whole = hashlib.sha256()
    while chunk := file.read(CHUNK):
        whole.update(chunk)
    file.seek(0)
    metadata: bytes | None = None
    checksums: bytes | None = None
    digests: dict[str, dict[str, str]] = {}
    seen: set[str] = set()
    try:
        with tarfile.open(fileobj=file, mode='r:', tarinfo=BoundedTarInfo) as tar:
            for count, member in enumerate(tar):
                if count >= MAX_MEMBERS:
                    raise ValidationFailed('the gem has too many files')
                if not member.isfile() or not _MEMBER.match(member.name) or member.name in seen:
                    raise ValidationFailed(f'the gem holds an unexpected entry {member.name[:100]!r}')
                seen.add(member.name)
                if member.name == 'metadata.gz' and member.size > MAX_METADATA_GZ:
                    raise ValidationFailed('metadata.gz is too large')
                if member.name == 'checksums.yaml.gz' and member.size > MAX_CHECKSUMS:
                    raise ValidationFailed('checksums.yaml.gz is too large')
                source = tar.extractfile(member)
                if source is None:
                    raise ValidationFailed('the gem is not readable')
                sha256, sha512 = hashlib.sha256(), hashlib.sha512()
                head = b''
                kept = bytearray()
                while chunk := source.read(CHUNK):
                    head = head or chunk[:2]
                    sha256.update(chunk)
                    sha512.update(chunk)
                    if member.name in ('metadata.gz', 'checksums.yaml.gz'):
                        kept += chunk
                digests[member.name] = {'sha256': sha256.hexdigest(), 'sha512': sha512.hexdigest()}
                if member.name == 'metadata.gz':
                    metadata = bytes(kept)
                elif member.name == 'checksums.yaml.gz':
                    checksums = bytes(kept)
                elif member.name == 'data.tar.gz' and head != b'\x1f\x8b':
                    raise ValidationFailed('data.tar.gz is not gzip')
    except READ_ERRORS:
        raise ValidationFailed('the upload is not a .gem (a tar archive)') from None
    if metadata is None or 'data.tar.gz' not in seen:
        raise ValidationFailed('the gem has no metadata.gz or data.tar.gz')
    if checksums is not None:
        _check_checksums(checksums, digests)
    spec = spec_from(load_yaml(_inflate(metadata, MAX_METADATA, 'metadata.gz')))
    return ReadGem(spec, whole.hexdigest())
