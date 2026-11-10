"""Terraform/OpenTofu provider network mirror paths (FORMATS.md "Terraform / OpenTofu").

Client routes below ``/repository/<repo>/`` (the Provider Network Mirror Protocol; Terraform and OpenTofu lower-case
host names, namespaces and types, and so does Waystation):

- ``<hostname>/<namespace>/<type>/index.json``: the provider's versions.
- ``<hostname>/<namespace>/<type>/<version>.json``: one version's archives per platform with their hashes.
- ``<hostname>/<namespace>/<type>/<version>/<os>/<arch>/<filename>``: an archive (the URL the version document
  gives, relative to it).

Asset keys of what is fetched from the origin registry for them (internal, never routed):

- ``<hostname>/.well-known/terraform.json`` (service discovery), ``<provider>/versions`` (the registry's version
  list), ``<provider>/<version>/download/<os>/<arch>`` (a platform's download document),
  ``<provider>/<version>/SHA256SUMS`` and ``SHA256SUMS.sig``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

_LABEL = r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?'
HOSTNAME = re.compile(rf'^{_LABEL}(?:\.{_LABEL})*(?::[0-9]{{1,5}})?$')
PART = re.compile(r'^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$')  # namespace, type
VERSION = re.compile(
    r'^(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})'
    r'(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$'
)
PLATFORM = re.compile(r'^[a-z0-9]{1,32}$')  # os, arch
FILENAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._+-]{0,200}\.zip$')
MAX_VERSION = 128


class Op(StrEnum):
    INDEX = 'index'
    VERSION = 'version'
    ARCHIVE = 'archive'


@dataclass(frozen=True)
class Provider:
    hostname: str
    namespace: str
    type: str

    @property
    def key(self) -> str:
        """``<hostname>/<namespace>/<type>``: the provider's address and the asset key prefix."""
        return f'{self.hostname}/{self.namespace}/{self.type}'


@dataclass(frozen=True)
class Route:
    op: Op
    provider: Provider
    version: str = ''
    os: str = ''
    arch: str = ''
    filename: str = ''


def is_version(text: str) -> bool:
    return len(text) <= MAX_VERSION and VERSION.match(text) is not None


def is_hostname(text: str) -> bool:
    if len(text) > 253 or HOSTNAME.match(text) is None:
        return False
    _, _, port = text.partition(':')
    return not port or 0 < int(port) < 65536


def parse_provider(hostname: str, namespace: str, type_: str) -> Provider | None:
    host, ns, kind = hostname.lower(), namespace.lower(), type_.lower()
    if not is_hostname(host) or not PART.match(ns) or not PART.match(kind):
        return None
    return Provider(host, ns, kind)


def parse_address(text: str) -> Provider | None:
    """``<hostname>/<namespace>/<type>`` (browse queries)."""
    parts = text.strip().split('/')
    return parse_provider(*parts) if len(parts) == 3 else None


def parse_route(path: str) -> Route | None:
    """The mirror route of a repository path (None: not a mirror path)."""
    parts = path.split('/')
    if len(parts) not in (4, 7):
        return None
    provider = parse_provider(parts[0], parts[1], parts[2])
    if provider is None:
        return None
    if len(parts) == 4:
        last = parts[3]
        if last == 'index.json':
            return Route(Op.INDEX, provider)
        version = last.removesuffix('.json')
        if version != last and is_version(version):
            return Route(Op.VERSION, provider, version=version)
        return None
    version, os_, arch, filename = parts[3:]
    if not is_version(version) or not PLATFORM.match(os_) or not PLATFORM.match(arch) or not FILENAME.match(filename):
        return None
    return Route(Op.ARCHIVE, provider, version=version, os=os_, arch=arch, filename=filename)


# ---------------------------------------------------------------------------- asset keys
def discovery_key(hostname: str) -> str:
    return f'{hostname}/.well-known/terraform.json'


def versions_key(provider: Provider) -> str:
    return f'{provider.key}/versions'


def download_key(provider: Provider, version: str, os_: str, arch: str) -> str:
    return f'{provider.key}/{version}/download/{os_}/{arch}'


def sums_key(provider: Provider, version: str) -> str:
    return f'{provider.key}/{version}/SHA256SUMS'


def signature_key(provider: Provider, version: str) -> str:
    return f'{provider.key}/{version}/SHA256SUMS.sig'


def archive_key(provider: Provider, version: str, os_: str, arch: str, filename: str) -> str:
    return f'{provider.key}/{version}/{os_}/{arch}/{filename}'


def archive_url(version: str, os_: str, arch: str, filename: str) -> str:
    """The archive's URL in a version document: relative to ``<provider>/<version>.json``."""
    return f'{version}/{os_}/{arch}/{filename}'
