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

Registry routes (T-P5-TFM, ``parse_registry_route``): the Module Registry Protocol below ``v1/modules/`` (proxy and
hosted repositories) and the Provider Registry Protocol below ``v1/providers/`` (the hosted repository); module
archives are stored at ``v1/modules/<namespace>/<name>/<system>/<version>/archive.tar.gz`` (``.zip`` for a proxied zip
source), hosted provider files at ``v1/providers/<namespace>/<type>/<version>/<file>``. A proxy keeps the origin's
version list and download answer under ``<hostname>/v1/modules/<namespace>/<name>/<system>/...`` (internal).
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


# ---------------------------------------------------------------------------- registry protocols (T-P5-TFM)
# Module Registry Protocol (proxy and hosted repositories) and Provider Registry Protocol (the hosted repository),
# below ``/repository/<repo>/v1/`` (what ``/.well-known/terraform.json`` announces as ``modules.v1`` /
# ``providers.v1``). Names are lower-cased, as the public registries treat them case-insensitively.
REGISTRY_PREFIX = 'v1/'
MODULE_PART = re.compile(r'^[0-9a-z](?:[0-9a-z_-]{0,62}[0-9a-z])?$')  # namespace, name
MODULE_SYSTEM = re.compile(r'^[0-9a-z]{1,64}$')
PROVIDER_FILE = re.compile(
    r'^terraform-provider-(?P<type>[a-z0-9-]{1,64})_(?P<version>[0-9A-Za-z.+-]{1,128})'
    r'_(?:(?P<os>[a-z0-9]{1,32})_(?P<arch>[a-z0-9]{1,32})\.zip|SHA256SUMS(?P<sig>\.sig)?)$'
)
ARCHIVES = ('archive.tar.gz', 'archive.zip')


class RegistryOp(StrEnum):
    MODULE_VERSIONS = 'module_versions'  # GET  v1/modules/<ns>/<name>/<system>/versions
    MODULE_DOWNLOAD = 'module_download'  # GET  v1/modules/<ns>/<name>/<system>/<version>/download
    MODULE_ARCHIVE = 'module_archive'  # GET  v1/modules/<ns>/<name>/<system>/<version>/archive.{tar.gz,zip}
    MODULE_VERSION = 'module_version'  # PUT (upload) / DELETE  v1/modules/<ns>/<name>/<system>/<version>
    PROVIDER_VERSIONS = 'provider_versions'  # GET v1/providers/<ns>/<type>/versions
    PROVIDER_DOWNLOAD = 'provider_download'  # GET v1/providers/<ns>/<type>/<version>/download/<os>/<arch>
    PROVIDER_FILE = 'provider_file'  # GET v1/providers/<ns>/<type>/<version>/<zip, SHA256SUMS or .sig>
    PROVIDER_VERSION = 'provider_version'  # POST (upload) / DELETE  v1/providers/<ns>/<type>/<version>


@dataclass(frozen=True)
class Module:
    namespace: str
    name: str
    system: str

    @property
    def key(self) -> str:
        """``<namespace>/<name>/<system>``: the module's address on its registry."""
        return f'{self.namespace}/{self.name}/{self.system}'


@dataclass(frozen=True)
class HostedProvider:
    namespace: str
    type: str

    @property
    def key(self) -> str:
        """``<namespace>/<type>`` (the address without the host name, which is this instance's)."""
        return f'{self.namespace}/{self.type}'


@dataclass(frozen=True)
class RegistryRoute:
    op: RegistryOp
    module: Module | None = None
    provider: HostedProvider | None = None
    version: str = ''
    os: str = ''
    arch: str = ''
    filename: str = ''


def parse_module(namespace: str, name: str, system: str) -> Module | None:
    ns, nm, sy = namespace.lower(), name.lower(), system.lower()
    if not MODULE_PART.match(ns) or not MODULE_PART.match(nm) or not MODULE_SYSTEM.match(sy):
        return None
    return Module(ns, nm, sy)


def parse_module_address(text: str) -> Module | None:
    """``<namespace>/<name>/<system>`` (browse queries)."""
    parts = text.strip().split('/')
    return parse_module(*parts) if len(parts) == 3 else None


def parse_hosted_provider(namespace: str, type_: str) -> HostedProvider | None:
    ns, kind = namespace.lower(), type_.lower()
    if not PART.match(ns) or not PART.match(kind):
        return None
    return HostedProvider(ns, kind)


def parse_hosted_provider_address(text: str) -> HostedProvider | None:
    """``<namespace>/<type>`` (browse queries)."""
    parts = text.strip().split('/')
    return parse_hosted_provider(*parts) if len(parts) == 2 else None


def _module_route(parts: list[str]) -> RegistryRoute | None:
    if len(parts) < 4:
        return None
    module = parse_module(parts[0], parts[1], parts[2])
    if module is None:
        return None
    rest = parts[3:]
    if rest == ['versions']:
        return RegistryRoute(RegistryOp.MODULE_VERSIONS, module=module)
    if not is_version(rest[0]):
        return None
    version = rest[0]
    if len(rest) == 1:
        return RegistryRoute(RegistryOp.MODULE_VERSION, module=module, version=version)
    if len(rest) == 2 and rest[1] == 'download':
        return RegistryRoute(RegistryOp.MODULE_DOWNLOAD, module=module, version=version)
    if len(rest) == 2 and rest[1] in ARCHIVES:
        return RegistryRoute(RegistryOp.MODULE_ARCHIVE, module=module, version=version, filename=rest[1])
    return None


def _provider_route(parts: list[str]) -> RegistryRoute | None:
    if len(parts) < 3:
        return None
    provider = parse_hosted_provider(parts[0], parts[1])
    if provider is None:
        return None
    rest = parts[2:]
    if rest == ['versions']:
        return RegistryRoute(RegistryOp.PROVIDER_VERSIONS, provider=provider)
    if not is_version(rest[0]):
        return None
    version = rest[0]
    if len(rest) == 1:
        return RegistryRoute(RegistryOp.PROVIDER_VERSION, provider=provider, version=version)
    if len(rest) == 4 and rest[1] == 'download' and PLATFORM.match(rest[2]) and PLATFORM.match(rest[3]):
        return RegistryRoute(RegistryOp.PROVIDER_DOWNLOAD, provider=provider, version=version, os=rest[2], arch=rest[3])
    if len(rest) == 2:
        match = PROVIDER_FILE.match(rest[1])
        if match is not None and match['type'] == provider.type and match['version'] == version:
            return RegistryRoute(
                RegistryOp.PROVIDER_FILE,
                provider=provider,
                version=version,
                os=match['os'] or '',
                arch=match['arch'] or '',
                filename=rest[1],
            )
    return None


def parse_registry_route(path: str) -> RegistryRoute | None:
    """The module or provider registry route of a repository path (None: not one)."""
    if not path.startswith(REGISTRY_PREFIX):
        return None
    kind, _, rest = path[len(REGISTRY_PREFIX) :].partition('/')
    parts = rest.split('/') if rest else []
    if any(not p for p in parts):
        return None
    if kind == 'modules':
        return _module_route(parts)
    if kind == 'providers':
        return _provider_route(parts)
    return None


def module_prefix(module: Module) -> str:
    """Where a module's files live in a repository (and its registry URLs): ``v1/modules/<address>/``."""
    return f'{REGISTRY_PREFIX}modules/{module.key}/'


def module_archive_key(module: Module, version: str, filename: str = ARCHIVES[0]) -> str:
    return f'{module_prefix(module)}{version}/{filename}'


def provider_prefix(provider: HostedProvider) -> str:
    return f'{REGISTRY_PREFIX}providers/{provider.key}/'


def provider_file_key(provider: HostedProvider, version: str, filename: str) -> str:
    return f'{provider_prefix(provider)}{version}/{filename}'


def provider_filename(provider: HostedProvider, version: str, os_: str = '', arch: str = '', *, sums: str = '') -> str:
    """``terraform-provider-<type>_<version>_<os>_<arch>.zip``, or with ``sums`` ('SHA256SUMS', 'SHA256SUMS.sig')
    the checksum list and its signature."""
    base = f'terraform-provider-{provider.type}_{version}'
    return f'{base}_{sums}' if sums else f'{base}_{os_}_{arch}.zip'


# Proxy: what is fetched from the origin module registry (internal asset keys, never routed).
def module_versions_key(hostname: str, module: Module) -> str:
    return f'{hostname}/v1/modules/{module.key}/versions'


def module_download_key(hostname: str, module: Module, version: str) -> str:
    return f'{hostname}/v1/modules/{module.key}/{version}/download'
