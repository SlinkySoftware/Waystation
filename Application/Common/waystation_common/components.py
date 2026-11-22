"""Which component an asset belongs to (DATA_MODEL ``components``; T-S09).

A component is what a user thinks of as one artifact of a repository: a package version (npm, apt, yum, PyPI, Go,
Galaxy, Terraform, Helm, Swift, NuGet, Composer, Cargo, Conda), a Maven ``groupId:artifactId:version``, a docker image
tag, or a raw file. Its assets are the files that make it up; only the files a client downloads to *get* the
artifact link to it (a ``.tgz``, a ``.deb``, a wheel, a docker tag's manifest, a Swift source archive...), never
checksums, signatures or index metadata, so download counts are honest.

Fields: ``namespace`` (npm scope, Maven groupId, Galaxy namespace, raw folder; '' when the format has none),
``name`` (the full name a client uses: ``@scope/pkg``, the artifactId, ``ns.collection``, the module or provider
address, the docker image, the raw file name), ``version`` ('' for raw files), ``version_sort`` (the
``waystation_common.versionsort`` contract) and ``display`` (``lodash@4.17.21``, ``library/nginx:1.27``...).

Asset paths are parsed with the cleanup planner's coordinates (``waystation_common.cleanup.plan.coordinates``) where
they exist, so cleanup and the component index agree on what a version is. Docker tags need the tag records
(``docker_ref``); the indexer (``waystation_common.db.components``) resolves them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from waystation_common.cleanup.plan import coordinates
from waystation_common.swift import parse_archive_path
from waystation_common.versionsort import version_sort

MAVEN_SKIP = ('.md5', '.sha1', '.sha256', '.sha512', '.asc', 'maven-metadata.xml')
_MAVEN_ID = re.compile(r'^[A-Za-z0-9_.-]{1,200}$')
_HELM_CHART = re.compile(r'^(?P<name>.+?)-(?P<version>v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.+-]*)?)\.tgz$')
_DIGEST = re.compile(r'^sha256:[0-9a-f]{64}$')
# Files that make up a package version, per format (the cleanup coordinates also accept side files).
PACKAGE_FILES: dict[str, tuple[str, ...]] = {
    'npm': ('.tgz',),
    'apt': ('.deb', '.udeb', '.ddeb'),
    'yum': ('.rpm',),
    'go': ('.zip',),
    'terraform': ('.zip', '.tar.gz'),
    'galaxy': ('.tar.gz',),
    'cargo': ('.crate',),
    'nuget': ('.nupkg',),
    'composer': ('.zip', '.tar'),
    'conda': ('.conda', '.tar.bz2'),
}


@dataclass(frozen=True)
class ComponentKey:
    format: str
    namespace: str
    name: str
    version: str

    @property
    def version_sort(self) -> str:
        return version_sort(self.format, self.version)

    @property
    def display(self) -> str:
        match self.format:
            case 'npm' | 'go':
                return f'{self.name}@{self.version}'
            case 'docker':
                return f'{self.name}:{self.version}'
            case 'maven':
                return f'{self.namespace}:{self.name}:{self.version}'
            case 'pypi':
                return f'{self.name}=={self.version}'
            case 'yum' | 'helm':
                return f'{self.name}-{self.version}'
            case 'raw':
                return f'{self.namespace}/{self.name}' if self.namespace else self.name
            case _:
                return f'{self.name} {self.version}' if self.version else self.name


def _maven(path: str) -> ComponentKey | None:
    if path.endswith(MAVEN_SKIP):
        return None
    parts = path.split('/')
    if len(parts) < 4:
        return None
    *group, artifact, version, filename = parts
    if not all(_MAVEN_ID.match(p) for p in (*group, artifact)) or not version or not filename.startswith(artifact):
        return None
    base = version.removesuffix('-SNAPSHOT')
    if not filename.startswith(f'{artifact}-{base}'):
        return None
    return ComponentKey('maven', '.'.join(group), artifact, version)


def _helm(path: str) -> ComponentKey | None:
    match = _HELM_CHART.match(path.rsplit('/', 1)[-1])
    return ComponentKey('helm', '', match['name'], match['version']) if match else None


def key_for(fmt: str, path: str, *, content: bool = True) -> ComponentKey | None:
    """The component of the asset at ``path`` in a repository of format ``fmt``; None when the file is not part
    of one (metadata, checksums, signatures, docker blobs). Docker manifests: see ``docker_ref``."""
    if fmt == 'docker':
        return None
    if fmt == 'maven':
        return _maven(path)
    if fmt == 'helm':
        return _helm(path) if content else None
    if fmt == 'swift':
        found = parse_archive_path(path) if content else None
        return ComponentKey('swift', found[0], f'{found[0]}.{found[1]}', found[2]) if found else None
    if fmt == 'raw':
        if not content:
            return None
        folder, _, filename = path.rpartition('/')
        return ComponentKey('raw', folder, filename, '')
    suffixes = PACKAGE_FILES.get(fmt)
    if fmt == 'pypi':
        if path.endswith('.metadata'):
            return None
    elif suffixes is None or not path.endswith(suffixes):
        return None
    coord = coordinates(fmt, path)
    if not coord.ordered_by_version or not coord.version:
        return None
    namespace = ''
    if fmt == 'npm' and coord.name.startswith('@') and '/' in coord.name:
        namespace = coord.name.split('/', 1)[0]
    elif fmt == 'galaxy' and '.' in coord.name:
        namespace = coord.name.split('.', 1)[0]
    return ComponentKey(fmt, namespace, coord.name, coord.version)


def docker_ref(path: str) -> tuple[str, str, bool] | None:
    """``<image>/manifests/<reference>[@accept=...]`` → (image, reference, reference is a digest); None for other
    docker assets (blobs, uploads)."""
    image, sep, ref = path.rpartition('/manifests/')
    if not sep or not image or not ref:
        return None
    ref = ref.split('@accept=', 1)[0]
    if not ref:
        return None
    return image, ref, bool(_DIGEST.match(ref))


def docker_key(image: str, tag: str) -> ComponentKey:
    return ComponentKey('docker', '', image, tag)


__all__ = ['ComponentKey', 'docker_key', 'docker_ref', 'key_for']
