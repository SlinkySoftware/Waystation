"""Map one Nexus 3 repository configuration to a Waystation repository (``RepositoryWrite`` body). Pure functions.

What maps (SPECIFICATION §4.12):

- name (unchanged; ADR 0003), format (``maven2`` → ``maven``; formats without a Waystation plugin are unsupported),
  type (when the format supports it), ``online``;
- proxy: ``proxy.remoteUrl`` → ``upstream_url``; ``metadataMaxAge`` (minutes, -1 = never) →
  ``metadata_max_age_s`` (-1 keeps the Waystation default: metadata is always revalidated eventually);
  ``contentMaxAge`` → ``content_max_age_s`` for raw only (-1 = forever); docker, npm, apt and yum content is
  checksum-verified and immutable, so it is kept forever; ``negativeCache`` → ``negative_ttl_s`` (disabled = 0);
  ``httpClient.autoBlock`` → ``auto_block.enabled``; ``httpClient.blocked`` → ``offline``;
- upstream credentials: Nexus never returns passwords, so they cannot be imported: the repository is created
  without authentication and the difference says so (set the secret after the import);
- docker: ``dockerProxy.indexType`` ``HUB`` (or a Docker Hub remote) → ``docker_hub``; the upstream host becomes an
  upstream alias (``docker.io`` for Docker Hub) unless disabled; connector ports/subdomains are reported (clients use
  ``/v2/<name>/`` or an alias instead; dedicated connectors are T-P6-DOCKERC);
- apt: ``apt.distribution``/``apt.flat``; raw: ``raw.contentDisposition``; maven: ``maven.versionPolicy``/
  ``maven.layoutPolicy``; yum and npm need no options;
- hosted: ``storage.writePolicy`` (``ALLOW``/``ALLOW_ONCE``/``DENY``) → ``hosted.write_policy``; group:
  ``group.memberNames``/``group.writableMember`` → ``group`` (only when the server's repository model has them).

Every Nexus setting without an exact equivalent becomes a ``Difference`` (field, Nexus value, Waystation value, note).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlsplit

from waystation_server.migration.nexus_client import NexusRepository
from waystation_server.repos.schemas import NAME_PATTERN, RepositoryWrite

Action = Literal['create', 'exists', 'unsupported', 'invalid']

# Nexus format name → Waystation format (None: no Waystation equivalent planned).
FORMATS: dict[str, str | None] = {
    'docker': 'docker',
    'npm': 'npm',
    'apt': 'apt',
    'yum': 'yum',
    'raw': 'raw',
    'pypi': 'pypi',
    'maven2': 'maven',
    'go': 'go',
    'helm': 'helm',
    'nuget': 'nuget',
    'rubygems': 'rubygems',
    'conda': 'conda',
    'cargo': 'cargo',
    'composer': 'composer',
    'huggingface': 'huggingface',
    'terraform': 'terraform',
    'swift': 'swift',
    'r': None,
    'conan': None,
    'cocoapods': None,
    'p2': None,
    'gitlfs': None,
    'bower': None,
}
# Content of these formats is addressed or verified by checksum (Go: module versions are immutable and checked
# against the checksum database): it never changes, so it is cached forever.
IMMUTABLE_CONTENT = frozenset({'docker', 'npm', 'apt', 'yum', 'pypi', 'go'})
DOCKER_HUB_HOSTS = frozenset({'registry-1.docker.io', 'docker.io', 'index.docker.io', 'registry.hub.docker.com'})
WRITE_POLICIES = {'ALLOW': 'allow_redeploy', 'ALLOW_ONCE': 'disable_redeploy', 'DENY': 'read_only'}
_HOST = re.compile(
    r'^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*(:\d{1,5})?$'
)
_NAME = re.compile(NAME_PATTERN)
DEFAULT_METADATA_MAX_AGE_S = 1800


@dataclass(frozen=True)
class Difference:
    field: str
    nexus: str | None
    waystation: str | None
    note: str


@dataclass
class Mapped:
    name: str
    nexus_format: str
    nexus_type: str
    format: str | None = None
    type: str | None = None
    action: Action = 'create'
    reason: str | None = None
    body: dict[str, Any] | None = None
    differences: list[Difference] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # docker routing facts used by the planner
    docker_hub: bool = False
    connector: bool = False  # a Nexus connector (port or subdomain) serves this repository at a host root
    members: list[str] = field(default_factory=list)
    shown: Any = None  # ``exists``: the RepositoryWrite the import would have created (for reference)

    def refuse(self, action: Action, reason: str) -> Mapped:
        self.action, self.reason, self.body = action, reason, None
        return self

    def differ(self, field_: str, nexus: Any, waystation: Any, note: str) -> None:
        self.differences.append(Difference(field_, _text(nexus), _text(waystation), note))


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (list, tuple)):
        return ', '.join(str(v) for v in value)
    return str(value)[:500]


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _minutes(value: Any) -> int | None:
    """Nexus ages are whole minutes; -1 (or any negative) = never. Non-numbers read as None (not set)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def upstream_host(url: str) -> str | None:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    host = (parts.hostname or '').lower()
    if not host:
        return None
    default = 443 if parts.scheme == 'https' else 80
    return f'{host}:{port}' if port and port != default else host


def supports_section(section: str) -> bool:
    """Whether this server's repository API has a ``hosted``/``group`` section (added with hosted/group repos)."""
    return section in RepositoryWrite.model_fields


def _proxy(m: Mapped, cfg: dict[str, Any], fmt: str) -> dict[str, Any] | None:
    proxy = _dict(cfg.get('proxy'))
    remote = proxy.get('remoteUrl')
    if not isinstance(remote, str) or not remote.strip():
        m.refuse('invalid', 'the Nexus proxy has no remote URL')
        return None
    out: dict[str, Any] = {'upstream_url': remote.strip()}
    metadata = _minutes(proxy.get('metadataMaxAge'))
    if metadata is not None and metadata >= 0:
        out['metadata_max_age_s'] = metadata * 60
    elif metadata is not None:
        out['metadata_max_age_s'] = DEFAULT_METADATA_MAX_AGE_S
        m.differ(
            'proxy.metadata_max_age_s',
            'never (-1)',
            DEFAULT_METADATA_MAX_AGE_S,
            'Nexus never revalidates this metadata; Waystation revalidates it after the default 30 minutes',
        )
    content = _minutes(proxy.get('contentMaxAge'))
    if fmt in IMMUTABLE_CONTENT:
        out['content_max_age_s'] = None
        if content is not None and content >= 0:
            m.differ(
                'proxy.content_max_age_s',
                f'{content} min',
                'forever',
                f'{fmt} content is verified by checksum and never changes, so Waystation keeps it until cleanup',
            )
    elif content is not None:
        out['content_max_age_s'] = content * 60 if content >= 0 else None
    negative = _dict(cfg.get('negativeCache'))
    if negative:
        ttl = _minutes(negative.get('timeToLive'))
        if negative.get('enabled') is False:
            out['negative_ttl_s'] = 0
        elif ttl is not None and ttl >= 0:
            out['negative_ttl_s'] = ttl * 60
    http = _dict(cfg.get('httpClient'))
    if isinstance(http.get('autoBlock'), bool):
        out['auto_block'] = {'enabled': http['autoBlock']}
    if http.get('blocked') is True:
        out['offline'] = True
        m.differ(
            'proxy.offline',
            'blocked',
            True,
            'the repository is blocked in Nexus: imported offline (serves only what is cached)',
        )
    auth = _dict(http.get('authentication'))
    if auth:
        kind = str(auth.get('type') or '')
        who = auth.get('username')
        m.differ(
            'proxy.auth',
            f'{kind} ({who})' if who else kind or 'set',
            'none',
            'Nexus does not return upstream passwords: set the credentials on the repository after the import'
            if kind in ('username', 'bearerToken')
            else f'Nexus {kind or "unknown"} authentication has no Waystation equivalent',
        )
    connection = _dict(http.get('connection'))
    if connection.get('useTrustStore') is True:
        m.differ(
            'proxy.ca_pem',
            'Nexus truststore',
            None,
            'the upstream certificate was trusted through the Nexus truststore: add its CA bundle if it is private',
        )
    for key, label in (('timeout', 'timeout'), ('retries', 'retries'), ('userAgentSuffix', 'user agent suffix')):
        if connection.get(key) not in (None, '', 0):
            m.differ(
                f'httpClient.connection.{key}',
                connection.get(key),
                None,
                f'the Nexus {label} is not imported (Waystation uses its own upstream timeouts)',
            )
    if isinstance(cfg.get('routingRuleName'), str) and cfg['routingRuleName']:
        m.differ('routingRuleName', cfg['routingRuleName'], None, 'Nexus routing rules are not imported')
    return out


def _connectors(cfg: dict[str, Any]) -> list[str]:
    docker = _dict(cfg.get('docker'))
    ports = [f'{k} {docker[k]}' for k in ('httpPort', 'httpsPort') if isinstance(docker.get(k), int)]
    if isinstance(docker.get('subdomain'), str) and docker['subdomain']:
        ports.append(f'subdomain {docker["subdomain"]}')
    return ports


def _facts(m: Mapped, cfg: dict[str, Any]) -> None:
    """Docker routing facts the planner needs even for repositories it cannot import (a connector group)."""
    if m.format != 'docker':
        return
    m.connector = bool(_connectors(cfg))
    m.members = [str(n) for n in _dict(cfg.get('group')).get('memberNames') or [] if isinstance(n, str)]
    if m.type == 'proxy':
        remote = _dict(cfg.get('proxy')).get('remoteUrl')
        host = upstream_host(remote) if isinstance(remote, str) else None
        m.docker_hub = _dict(cfg.get('dockerProxy')).get('indexType') == 'HUB' or host in DOCKER_HUB_HOSTS


def _docker(m: Mapped, cfg: dict[str, Any], body: dict[str, Any], *, aliases: bool) -> dict[str, Any]:
    docker = _dict(cfg.get('docker'))
    ports = _connectors(cfg)
    if ports:
        m.differ(
            'docker connector',
            ', '.join(ports),
            f'/v2/{m.name}/',
            'Nexus connectors are not imported: clients pull through <host>/<repository>/<image> or an upstream '
            'alias (or as the default docker repository)',
        )
    if docker.get('v1Enabled') is True:
        m.differ('docker.v1Enabled', True, None, 'the Docker V1 API is not supported')
    options: dict[str, Any] = {}
    if m.type != 'proxy':
        return options
    proxy_cfg = _dict(cfg.get('dockerProxy'))
    host = upstream_host((body.get('proxy') or {}).get('upstream_url', ''))
    index = str(proxy_cfg.get('indexType') or '')
    if m.docker_hub:
        options['docker_hub'] = True
    elif index == 'CUSTOM' and proxy_cfg.get('indexUrl'):
        m.differ('dockerProxy.indexUrl', proxy_cfg['indexUrl'], None, 'a custom Docker index is not used')
    if proxy_cfg.get('cacheForeignLayers') is True:
        m.differ(
            'dockerProxy.cacheForeignLayers',
            True,
            None,
            'foreign (non-distributable) layers are fetched by clients from their own URLs',
        )
    alias = 'docker.io' if m.docker_hub else host
    if aliases and alias and _HOST.fullmatch(alias):
        options['aliases'] = [alias]
    return options


def _format_options(m: Mapped, cfg: dict[str, Any], body: dict[str, Any], *, aliases: bool) -> dict[str, Any] | None:
    fmt = m.format
    if fmt == 'docker':
        return _docker(m, cfg, body, aliases=aliases)
    if fmt == 'apt' and m.type == 'proxy':
        apt = _dict(cfg.get('apt'))
        distribution = apt.get('distribution')
        if not isinstance(distribution, str) or not distribution:
            m.refuse('invalid', 'the Nexus apt repository has no distribution')
            return None
        return {'distribution': distribution, 'flat': apt.get('flat') is True}
    if fmt == 'raw':
        raw = _dict(cfg.get('raw'))
        disposition = str(raw.get('contentDisposition') or '').lower()
        return {'content_disposition': disposition} if disposition in ('inline', 'attachment') else {}
    if fmt == 'maven':  # T-P5-MAVEN: maven.versionPolicy / maven.layoutPolicy (groups have neither)
        maven = _dict(cfg.get('maven'))
        out: dict[str, Any] = {}
        version_policy = str(maven.get('versionPolicy') or '').lower()
        if version_policy in ('release', 'snapshot', 'mixed') and m.type != 'group':
            out['version_policy'] = version_policy
        layout_policy = str(maven.get('layoutPolicy') or '').lower()
        if layout_policy in ('strict', 'permissive'):
            out['layout_policy'] = layout_policy
        return out
    if fmt == 'yum' and _dict(cfg.get('yumSigning')):
        m.differ('yumSigning', 'set', None, 'the Nexus yum signing key is not imported')
    return {}


def _storage(m: Mapped, cfg: dict[str, Any]) -> None:
    storage = _dict(cfg.get('storage'))
    blob_store = storage.get('blobStoreName')
    if isinstance(blob_store, str) and blob_store not in ('', 'default'):
        m.differ('storage.blobStoreName', blob_store, None, 'Waystation has one blob store (content is de-duplicated)')
    if storage.get('strictContentTypeValidation') is False:
        m.differ('storage.strictContentTypeValidation', False, None, 'Waystation always validates content')
    cleanup = _dict(cfg.get('cleanup')).get('policyNames')
    if isinstance(cleanup, list) and cleanup:
        m.differ(
            'cleanup.policyNames',
            [str(c) for c in cleanup],
            None,
            'Nexus cleanup policies are not imported: attach a Waystation cleanup policy instead',
        )


def _hosted(m: Mapped, cfg: dict[str, Any]) -> dict[str, Any]:
    storage = _dict(cfg.get('storage'))
    policy = WRITE_POLICIES.get(str(storage.get('writePolicy') or '').upper())
    out: dict[str, Any] = {}
    if policy:
        out['write_policy'] = policy
    if m.format == 'docker' and storage.get('latestPolicy') is True:
        m.differ('storage.latestPolicy', True, None, 'the Nexus "latest" redeploy exception is not imported')
    if m.format in ('apt', 'yum') and (_dict(cfg.get('aptSigning')) or _dict(cfg.get('yum'))):
        m.differ('signing', 'set', None, 'hosted index signing keys are not imported')
    return out


def _group(m: Mapped, cfg: dict[str, Any]) -> dict[str, Any] | None:
    group = _dict(cfg.get('group'))
    members = [str(n) for n in group.get('memberNames') or [] if isinstance(n, str)]
    if not members:
        m.refuse('invalid', 'the Nexus group has no members')
        return None
    m.members = members
    out: dict[str, Any] = {'members': members}
    writable = group.get('writableMember')
    if isinstance(writable, str) and writable:
        out['writable_member'] = writable
    return out


def map_repository(repo: NexusRepository, *, supported: Mapping[str, frozenset[str]], aliases: bool = True) -> Mapped:
    """The Waystation equivalent of ``repo``: ``create`` with a body, or ``unsupported``/``invalid`` with a reason.
    ``supported`` maps each registered Waystation format to the repository types it offers."""
    m = Mapped(name=repo.name, nexus_format=repo.format, nexus_type=repo.type)
    if not _NAME.fullmatch(repo.name):
        return m.refuse('invalid', 'the name does not fit Waystation repository names (2-63 of A-Z a-z 0-9 . _ -)')
    if repo.format not in FORMATS:
        return m.refuse('unsupported', f'Nexus format {repo.format!r} is unknown to the importer')
    fmt = FORMATS[repo.format]
    if fmt is None:
        return m.refuse('unsupported', f'Waystation has no {repo.format} format')
    m.format = fmt
    if repo.type not in ('proxy', 'hosted', 'group'):
        return m.refuse('unsupported', f'unknown repository type {repo.type!r}')
    m.type = repo.type
    cfg = repo.config
    _facts(m, cfg)
    if fmt not in supported:
        return m.refuse('unsupported', f'Waystation does not serve {fmt} yet')
    if repo.type not in supported[fmt]:
        return m.refuse('unsupported', f'Waystation has no {fmt} {repo.type} repositories yet')
    if repo.type in ('hosted', 'group') and not supports_section(repo.type):
        return m.refuse('unsupported', f'Waystation has no {fmt} {repo.type} repositories yet')
    if repo.detail_error:
        m.notes.append(repo.detail_error)
    body: dict[str, Any] = {
        'name': repo.name,
        'format': fmt,
        'type': repo.type,
        'online': cfg.get('online') is not False,
        'description': f'Imported from Nexus ({repo.format} {repo.type})',
    }
    if repo.type == 'proxy':
        proxy = _proxy(m, cfg, fmt)
        if proxy is None:
            return m
        body['proxy'] = proxy
    elif repo.type == 'hosted':
        body['hosted'] = _hosted(m, cfg)
    else:
        group = _group(m, cfg)
        if group is None:
            return m
        body['group'] = group
    _storage(m, cfg)
    options = _format_options(m, cfg, body, aliases=aliases)
    if options is None:
        return m
    body['format_options'] = options
    m.body = body
    return m
