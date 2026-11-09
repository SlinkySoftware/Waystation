"""Repository create/update/delete with validation (SPECIFICATION §3, §4.12; ADR 0003).

Validation, reported as 422 field errors (``errors[].field`` a dotted path into the request body):

- names: SPECIFICATION §3 pattern; not a reserved top-level path (``api``, ``ui``, ``v2``, ``auth``,
  ``repository``, ``healthz``, ``readyz``, ``metrics``) and not a docker upstream alias, case-insensitively; unique
  case-insensitively (409 when taken);
- the format must be registered and support the type; proxy repositories need ``proxy``, others must not send it;
- ``upstream_url``: http(s), a host, no credentials in the URL (use ``auth``), no fragment; a literal private address
  needs ``allow_private_upstream``;
- ``auth``: ``basic`` needs a username; ``basic``/``bearer`` need a secret (kept from the stored config on update
  when omitted); secrets are encrypted with ``WAYSTATION_SECRET_KEYS`` (associated data ``repo:<id>``);
- ``ca_pem``: PEM certificates (at most 100) usable as a CA bundle; ``http_proxy``: an ``http(s)://host[:port]`` URL
  without credentials, path or query; ``auto_block.max_s`` not below ``auto_block.initial_s``;
- hosted repositories: ``hosted`` (write policy, immutable tag patterns that must compile as regular expressions;
  defaults when omitted); other types must not send it;
- group repositories: ``group`` is required: 1-50 distinct existing members of the same format that are not groups
  (stored by their exact names, in order); a ``writable_member`` only where the format supports one, and it must be a
  hosted member; other types must not send it. A repository that is a group member cannot be deleted;
- ``format_options`` against the format's ``options_model`` and its per-type rules (``check_options``); docker
  aliases must not collide with repository names or other repositories' aliases; one default docker repository
  (setting it clears the others);
- on update, ``name``, ``format`` and ``type`` cannot change.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ValidationError

from waystation_common.errors import Conflict
from waystation_common.events import keys as event_keys
from waystation_common.ids import new_id
from waystation_server.db import repositories as repos_db
from waystation_server.problems import FieldError, FieldErrors, validation_errors
from waystation_server.proxy.http import check_ca_pem, check_proxy_url
from waystation_server.repos.model import (
    AuthKind,
    RepoConfig,
    RepoType,
    StoredAutoBlock,
    StoredGroup,
    StoredHosted,
    StoredProxy,
    StoredUpstreamAuth,
    secret_aad,
)
from waystation_server.repos.schemas import GroupConfig, HostedConfig, ProxyConfig, RepositoryWrite
from waystation_server.security.principal import Principal
from waystation_server.services import Services

log = logging.getLogger('waystation.repos')

RESERVED_NAMES = frozenset({'api', 'ui', 'v2', 'auth', 'repository', 'healthz', 'readyz', 'metrics'})


def _check_url(proxy: ProxyConfig, errors: list[FieldError]) -> None:
    field = 'proxy.upstream_url'
    try:
        parts = urlsplit(proxy.upstream_url.strip())
        port = parts.port
    except ValueError:
        errors.append(FieldError(field=field, message='not a valid URL', code='format'))
        return
    if parts.scheme not in ('http', 'https') or not parts.hostname:
        errors.append(FieldError(field=field, message='must be an http(s) URL with a host', code='format'))
        return
    if parts.username or parts.password:
        errors.append(FieldError(field=field, message='put credentials in auth, not in the URL', code='credentials'))
    if parts.fragment:
        errors.append(FieldError(field=field, message='must not have a fragment', code='format'))
    if port == 0:
        errors.append(FieldError(field=field, message='invalid port', code='format'))
    try:
        literal = ipaddress.ip_address(parts.hostname)
    except ValueError:
        return
    if not proxy.allow_private_upstream and not literal.is_global:
        errors.append(
            FieldError(
                field=field,
                message='private or reserved address: set allow_private_upstream to allow it',
                code='ssrf',
            )
        )


def _clean(value: str | None) -> str | None:
    return (value or '').strip() or None


async def check_network(proxy: ProxyConfig) -> list[FieldError]:
    """Field errors of the network options (CA bundle, outbound proxy, auto-block periods)."""
    errors: list[FieldError] = []
    if (pem := _clean(proxy.ca_pem)) is not None:
        problem = await asyncio.to_thread(check_ca_pem, pem)
        if problem:
            errors.append(FieldError(field='proxy.ca_pem', message=problem, code='format'))
    if (url := _clean(proxy.http_proxy)) is not None and (problem := check_proxy_url(url)):
        errors.append(FieldError(field='proxy.http_proxy', message=problem, code='format'))
    if proxy.auto_block.max_s < proxy.auto_block.initial_s:
        errors.append(
            FieldError(field='proxy.auto_block.max_s', message='must not be shorter than initial_s', code='range')
        )
    return errors


def _options(services: Services, body: RepositoryWrite, errors: list[FieldError]) -> dict[str, Any]:
    descriptor = services.formats.descriptor(body.format)
    if descriptor is None:
        errors.append(
            FieldError(field='format', message=f'format {body.format} is not available yet', code='unsupported')
        )
        return {}
    if body.type not in descriptor.types:
        supported = ', '.join(sorted(t.value for t in descriptor.types))
        errors.append(FieldError(field='type', message=f'{body.format} supports: {supported}', code='unsupported'))
    try:
        options: BaseModel = descriptor.parse_options(body.format_options)
    except ValidationError as exc:
        for err in validation_errors(exc.errors()):
            errors.append(FieldError(field=f'format_options.{err.field}', message=err.message, code=err.code))
        return {}
    for field, message in descriptor.check_options(options, body.type):
        errors.append(FieldError(field=f'format_options.{field}', message=message, code='invalid'))
    return options.model_dump(mode='json')


async def _stored_group(
    services: Services, body: RepositoryWrite, group: GroupConfig, errors: list[FieldError]
) -> StoredGroup:
    members: list[RepoConfig] = []
    seen: set[str] = set()
    for i, name in enumerate(group.members):
        field = f'group.members.{i}'
        member = await services.catalog.get(name) or await services.catalog.get_ci(name)
        if member is None:
            errors.append(FieldError(field=field, message=f'there is no repository {name!r}', code='not_found'))
        elif member.name.lower() == body.name.lower():
            errors.append(FieldError(field=field, message='a group cannot contain itself', code='invalid'))
        elif member.format != body.format:
            errors.append(
                FieldError(field=field, message=f'{member.name} is a {member.format} repository', code='format')
            )
        elif member.type is RepoType.GROUP:
            errors.append(FieldError(field=field, message='groups cannot contain groups', code='invalid'))
        elif member.name in seen:
            errors.append(FieldError(field=field, message=f'{member.name} is listed twice', code='duplicate'))
        else:
            seen.add(member.name)
            members.append(member)
    writable: str | None = None
    if group.writable_member:
        descriptor = services.formats.descriptor(body.format)
        target = next((m for m in members if m.name.lower() == group.writable_member.lower()), None)
        field = 'group.writable_member'
        if descriptor is None or not descriptor.writable_group_member:
            errors.append(FieldError(field=field, message=f'{body.format} groups are read-only', code='unsupported'))
        elif target is None:
            errors.append(FieldError(field=field, message='must be one of the members', code='invalid'))
        elif target.type is not RepoType.HOSTED:
            errors.append(FieldError(field=field, message='must be a hosted repository', code='invalid'))
        else:
            writable = target.name
    return StoredGroup(members=tuple(m.name for m in members), writable_member=writable)


def _stored_hosted(hosted: HostedConfig, errors: list[FieldError]) -> StoredHosted:
    for i, pattern in enumerate(hosted.immutable_tag_patterns):
        try:
            re.compile(pattern)
        except re.error as exc:
            errors.append(
                FieldError(
                    field=f'hosted.immutable_tag_patterns.{i}',
                    message=f'not a regular expression: {exc.msg}',
                    code='format',
                )
            )
    return StoredHosted(
        write_policy=hosted.write_policy, immutable_tag_patterns=tuple(dict.fromkeys(hosted.immutable_tag_patterns))
    )


def _aliases(options: dict[str, Any]) -> list[str]:
    return [str(a).lower() for a in options.get('aliases') or []]


async def _check_names(
    services: Services, body: RepositoryWrite, options: dict[str, Any], repo_id: str | None, errors: list[FieldError]
) -> None:
    lowered = body.name.lower()
    if lowered in RESERVED_NAMES:
        errors.append(FieldError(field='name', message=f'{body.name!r} is reserved', code='reserved'))
    others = [r for r in await services.catalog.all() if r.id != repo_id]
    other_aliases = {alias: r.name for r in others if r.format == 'docker' for alias in _aliases(r.format_options)}
    if lowered in other_aliases:
        errors.append(FieldError(field='name', message=f'is a docker alias of {other_aliases[lowered]}', code='alias'))
    names = {r.name.lower() for r in others}
    for i, alias in enumerate(_aliases(options)):
        if alias in names:
            errors.append(FieldError(field=f'format_options.aliases.{i}', message='is a repository name', code='taken'))
        elif alias in other_aliases:
            errors.append(
                FieldError(
                    field=f'format_options.aliases.{i}',
                    message=f'is already an alias of {other_aliases[alias]}',
                    code='taken',
                )
            )
        elif alias == lowered:
            errors.append(
                FieldError(field=f'format_options.aliases.{i}', message='equals the repository name', code='taken')
            )


def _stored_proxy(
    services: Services,
    proxy: ProxyConfig,
    repo_id: str,
    previous: StoredProxy | None,
    errors: list[FieldError],
) -> StoredProxy | None:
    _check_url(proxy, errors)
    auth = proxy.auth
    secret_enc: str | None = None
    if auth.kind is AuthKind.BASIC and not auth.username:
        errors.append(FieldError(field='proxy.auth.username', message='required for basic auth', code='required'))
    if auth.kind is not AuthKind.NONE:
        if auth.secret:
            if services.keyring is None:
                errors.append(
                    FieldError(
                        field='proxy.auth.secret',
                        message='the server has no WAYSTATION_SECRET_KEYS to encrypt credentials',
                        code='unavailable',
                    )
                )
            else:
                secret_enc = services.keyring.encrypt_str(auth.secret, aad=secret_aad(repo_id))
        elif previous is not None and previous.auth.kind is auth.kind and previous.auth.secret_enc:
            secret_enc = previous.auth.secret_enc
        else:
            errors.append(FieldError(field='proxy.auth.secret', message='required', code='required'))
    return StoredProxy(
        upstream_url=proxy.upstream_url.strip(),
        auth=StoredUpstreamAuth(
            kind=auth.kind, username=auth.username if auth.kind is AuthKind.BASIC else None, secret_enc=secret_enc
        ),
        metadata_max_age_s=proxy.metadata_max_age_s,
        content_max_age_s=proxy.content_max_age_s,
        negative_ttl_s=proxy.negative_ttl_s,
        stale_on_error=proxy.stale_on_error,
        offline=proxy.offline,
        allow_private_upstream=proxy.allow_private_upstream,
        ca_pem=_clean(proxy.ca_pem),
        http_proxy=_clean(proxy.http_proxy),
        auto_block=StoredAutoBlock(**proxy.auto_block.model_dump()),
    )


async def _build(
    services: Services, body: RepositoryWrite, principal: Principal, existing: RepoConfig | None
) -> RepoConfig:
    errors: list[FieldError] = []
    if existing is not None:
        if body.name != existing.name:
            errors.append(FieldError(field='name', message='cannot be changed', code='immutable'))
        if body.format != existing.format:
            errors.append(FieldError(field='format', message='cannot be changed', code='immutable'))
        if body.type is not existing.type:
            errors.append(FieldError(field='type', message='cannot be changed', code='immutable'))
    repo_id = existing.id if existing else new_id()
    options = _options(services, body, errors)
    await _check_names(services, body, options, repo_id, errors)
    proxy: StoredProxy | None = None
    if body.type is RepoType.PROXY:
        if body.proxy is None:
            errors.append(FieldError(field='proxy', message='required for proxy repositories', code='required'))
        else:
            proxy = _stored_proxy(services, body.proxy, repo_id, existing.proxy if existing else None, errors)
            errors.extend(await check_network(body.proxy))
    elif body.proxy is not None:
        errors.append(FieldError(field='proxy', message='only proxy repositories have an upstream', code='invalid'))
    hosted: StoredHosted | None = None
    if body.type is RepoType.HOSTED:
        hosted = _stored_hosted(body.hosted or HostedConfig(), errors)
    elif body.hosted is not None:
        errors.append(
            FieldError(field='hosted', message='only hosted repositories have a write policy', code='invalid')
        )
    group: StoredGroup | None = None
    if body.type is RepoType.GROUP:
        if body.group is None:
            errors.append(FieldError(field='group', message='required for group repositories', code='required'))
        else:
            group = await _stored_group(services, body, body.group, errors)
    elif body.group is not None:
        errors.append(FieldError(field='group', message='only group repositories have members', code='invalid'))
    if errors:
        raise FieldErrors(errors)
    now = datetime.now(UTC)
    return RepoConfig(
        id=repo_id,
        name=body.name,
        format=body.format,
        type=body.type,
        online=body.online,
        description=body.description,
        proxy=proxy,
        hosted=hosted,
        group=group,
        format_options=options,
        cleanup_policy_ids=existing.cleanup_policy_ids if existing else (),
        created_at=existing.created_at if existing else now,
        updated_at=now,
        created_by=existing.created_by if existing else principal.id,
        updated_by=principal.id,
        config_version=(existing.config_version + 1) if existing else 1,
    )


async def _after_write(services: Services, repo: RepoConfig) -> None:
    if repo.format == 'docker' and repo.format_options.get('default_repo'):
        await repos_db.unset_default_docker(services.db, except_id=repo.id)
    services.catalog.invalidate()


async def create(services: Services, body: RepositoryWrite, principal: Principal) -> RepoConfig:
    if await services.catalog.get_ci(body.name) is not None:
        raise Conflict('A repository with this name already exists.', field='name')
    repo = await _build(services, body, principal, None)
    await repos_db.insert(services.db, repo)
    await _after_write(services, repo)
    log.info('repository created', extra={'repo': repo.name, 'format': repo.format, 'type': repo.type.value})
    return repo


async def update(services: Services, existing: RepoConfig, body: RepositoryWrite, principal: Principal) -> RepoConfig:
    repo = await _build(services, body, principal, existing)
    if not await repos_db.replace(services.db, repo, expected_version=existing.config_version):
        services.catalog.invalidate()
        raise Conflict('the repository was changed by someone else; reload and try again')
    await _after_write(services, repo)
    if repo.proxy is not None:
        await services.proxy.breaker.reset(repo.id)  # a saved configuration starts with a clean slate
    log.info('repository updated', extra={'repo': repo.name})
    return repo


async def delete(services: Services, repo: RepoConfig) -> None:
    groups = [r.name for r in await services.catalog.all() if r.group is not None and repo.name in r.group.members]
    if groups:
        raise Conflict(f'{repo.name} is a member of {", ".join(sorted(groups))}: remove it from the group first')
    await repos_db.delete(services.db, repo.id)
    services.catalog.invalidate()
    try:
        await services.valkey.delete(
            event_keys.upstream_health_key(repo.id), event_keys.upstream_ratelimit_key(repo.id)
        )
    except Exception as exc:  # live state only; it would expire with the repository anyway
        log.warning('could not clear upstream state', extra={'error': type(exc).__name__})
    log.info('repository deleted', extra={'repo': repo.name})


def decrypt_secret(services: Services, repo_id: str, auth: StoredUpstreamAuth) -> str | None:
    if not auth.secret_enc or services.keyring is None:
        return None
    return services.keyring.decrypt_str(auth.secret_enc, aad=secret_aad(repo_id))
