"""Maven repository layout (FORMATS.md "Maven"): what a repository path names.

``<group/as/path>/<artifactId>/<version>/<artifactId>-<version>[-<classifier>].<extension>``, plus:

- checksum files: any path + ``.md5`` / ``.sha1`` / ``.sha256`` / ``.sha512``;
- signatures: an artifact + ``.asc`` (their checksums too: ``.jar.asc.sha1``);
- ``maven-metadata.xml`` at the groupId/artifactId level (versions), the SNAPSHOT version level (timestamped builds)
  and the groupId level (plugin prefixes); the level cannot be told from the path alone.

A SNAPSHOT version (``1.0-SNAPSHOT``) holds files named with the base version (``a-1.0-SNAPSHOT.jar``) or, as ``mvn
deploy`` and Gradle write them, a timestamped version (``a-1.0-20261108.123456-3.jar``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from waystation_server.formats.maven.version import is_snapshot

CHECKSUMS = ('md5', 'sha1', 'sha256', 'sha512')
HEX_LENGTH = {'md5': 32, 'sha1': 40, 'sha256': 64, 'sha512': 128}
METADATA = 'maven-metadata.xml'
SIGNATURE = '.asc'
MAX_SEGMENTS = 64
# groupId segments and artifactIds: Maven's own pattern ([A-Za-z0-9_\-.]+); versions also allow '+' (semver builds).
_ID = re.compile(r'^[A-Za-z0-9_.-]{1,200}$')
_VERSION = re.compile(r'^[A-Za-z0-9_.+-]{1,200}$')
_TIMESTAMP = re.compile(r'^(?P<base>.+)-(?P<ts>\d{8}\.\d{6})-(?P<build>\d{1,9})$')
_CLASSIFIER = re.compile(r'^[A-Za-z0-9_.+-]{1,200}$')
_EXTENSION = re.compile(r'^[A-Za-z0-9_+-]{1,32}(\.[A-Za-z0-9_+-]{1,32}){0,3}$')


def split_checksum(path: str) -> tuple[str, str] | None:
    """``a/b.jar.sha1`` → (``a/b.jar``, ``sha1``); None for other paths."""
    for alg in CHECKSUMS:
        suffix = '.' + alg
        if path.endswith(suffix) and len(path) > len(suffix) and not path.endswith('/' + suffix):
            return path[: -len(suffix)], alg
    return None


def parse_checksum(text: bytes, alg: str) -> str | None:
    """The hex digest in a checksum file (``<hex>`` or ``<hex>  <file name>``, any case), else None."""
    try:
        decoded = text.decode('ascii').strip()
    except UnicodeDecodeError:
        return None
    if not decoded:
        return None
    first = decoded.split()[0].lower()
    if len(first) != HEX_LENGTH[alg] or any(c not in '0123456789abcdef' for c in first):
        return None
    return first


def is_metadata(path: str) -> bool:
    return path == METADATA or path.endswith('/' + METADATA)


@dataclass(frozen=True)
class Artifact:
    """A file of a version directory."""

    group_id: str
    artifact_id: str
    version: str  # the directory's version (``1.0-SNAPSHOT``)
    file_version: str  # the version in the file name (``1.0-20261108.123456-3`` for a timestamped snapshot)
    classifier: str | None
    extension: str  # ``jar``, ``pom``, ``tar.gz``, ``jar.asc``

    @property
    def snapshot(self) -> bool:
        return is_snapshot(self.version)

    @property
    def timestamp(self) -> tuple[str, int] | None:
        """A timestamped snapshot's (``yyyyMMdd.HHmmss``, build number)."""
        match = _TIMESTAMP.match(self.file_version) if self.snapshot else None
        return (match['ts'], int(match['build'])) if match else None

    @property
    def directory(self) -> str:
        return f'{self.group_id.replace(".", "/")}/{self.artifact_id}/{self.version}'

    @property
    def ga_path(self) -> str:
        return f'{self.group_id.replace(".", "/")}/{self.artifact_id}'

    @property
    def signature(self) -> bool:
        return self.extension.endswith('asc')


def valid_group_path(segments: list[str]) -> bool:
    return bool(segments) and all(_ID.match(s) and s not in ('.', '..') and not s.startswith('.') for s in segments)


def parse_artifact(path: str) -> Artifact | None:
    """The coordinates of an artifact file path (checksums excluded), or None when the path is not one."""
    segments = path.split('/')
    if len(segments) < 4 or len(segments) > MAX_SEGMENTS:
        return None
    *group, artifact_id, version, filename = segments
    if not valid_group_path(group) or not _ID.match(artifact_id) or not _VERSION.match(version):
        return None
    if version.startswith('.') or artifact_id.startswith('.') or filename == METADATA:
        return None
    prefix = f'{artifact_id}-'
    if not filename.startswith(prefix):
        return None
    rest = filename[len(prefix) :]
    candidates = [version]
    if is_snapshot(version):
        base = version[: -len('-SNAPSHOT')]
        match = re.match(rf'^{re.escape(base)}-\d{{8}}\.\d{{6}}-\d{{1,9}}', rest)
        if match:
            candidates.insert(0, match.group(0))
    for file_version in candidates:
        if not rest.startswith(file_version):
            continue
        tail = rest[len(file_version) :]
        classifier: str | None = None
        if tail.startswith('-'):
            classifier, dot, extension = tail[1:].partition('.')
            if not dot or not _CLASSIFIER.match(classifier):
                continue
        elif tail.startswith('.'):
            extension = tail[1:]
        else:
            continue
        if not _EXTENSION.match(extension):
            continue
        return Artifact('.'.join(group), artifact_id, version, file_version, classifier, extension)
    return None


@dataclass(frozen=True)
class MetadataPath:
    """``<dir>/maven-metadata.xml``: ``dir`` split into what it may be."""

    directory: str  # '' at the repository root

    @property
    def snapshot_version(self) -> tuple[str, str, str] | None:
        """(groupId, artifactId, version) when the directory is a SNAPSHOT version directory."""
        segments = self.directory.split('/') if self.directory else []
        if len(segments) < 3 or not is_snapshot(segments[-1]):
            return None
        *group, artifact_id, version = segments
        if not valid_group_path(group) or not _ID.match(artifact_id) or not _VERSION.match(version):
            return None
        return '.'.join(group), artifact_id, version

    @property
    def ga(self) -> tuple[str, str] | None:
        """(groupId, artifactId) when the directory can be a groupId/artifactId directory."""
        segments = self.directory.split('/') if self.directory else []
        if len(segments) < 2:
            return None
        *group, artifact_id = segments
        if not valid_group_path(group) or not _ID.match(artifact_id):
            return None
        return '.'.join(group), artifact_id


def parse_metadata_path(path: str) -> MetadataPath | None:
    if not is_metadata(path):
        return None
    return MetadataPath(path[: -len(METADATA)].rstrip('/'))


def version_of_path(path: str) -> str | None:
    """The version directory a path is in, when it is an artifact or a version-level file (for version policies)."""
    base = split_checksum(path)
    plain = base[0] if base else path
    artifact = parse_artifact(plain)
    if artifact is not None:
        return artifact.version
    meta = parse_metadata_path(plain)
    if meta is not None and meta.snapshot_version is not None:
        return meta.snapshot_version[2]
    return None


def strict_ok(path: str) -> bool:
    """Is ``path`` a Maven layout path (an artifact, a signature, a ``maven-metadata.xml``, or a checksum of one)?"""
    base = split_checksum(path)
    plain = base[0] if base else path
    if parse_metadata_path(plain) is not None:
        return plain != METADATA
    return parse_artifact(plain) is not None


CONTENT_TYPES = {
    'jar': 'application/java-archive',
    'war': 'application/java-archive',
    'ear': 'application/java-archive',
    'aar': 'application/java-archive',
    'pom': 'application/xml',
    'xml': 'application/xml',
    'module': 'application/json',
    'json': 'application/json',
    'asc': 'text/plain',
    'zip': 'application/zip',
    'gz': 'application/gzip',
    'tgz': 'application/gzip',
    'md5': 'text/plain',
    'sha1': 'text/plain',
    'sha256': 'text/plain',
    'sha512': 'text/plain',
}


def content_type(path: str) -> str:
    name = path.rsplit('/', 1)[-1]
    extension = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
    return CONTENT_TYPES.get(extension, 'application/octet-stream')
