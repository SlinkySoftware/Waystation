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
- ``format_options`` against the format's ``options_model``; docker aliases must not collide with repository names
  or other repositories' aliases; one default docker repository (setting it clears the others);
- on update, ``name``, ``format`` and ``type`` cannot change.
"""

from __future__ import annotations

import ipaddress
import logging
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ValidationError

from waystation_common.errors import Conflict
from waystation_common.events import keys as event_keys
from waystation_common.ids import new_id
from waystation_server.db import repositories as repos_db
from waystation_server.problems import FieldError, FieldErrors, validation_errors
from waystation_server.repos.model import (
    AuthKind,
    RepoConfig,
    RepoType,
    StoredProxy,
    StoredUpstreamAuth,
    secret_aad,
)
from waystation_server.repos.schemas import ProxyConfig, RepositoryWrite
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
    return options.model_dump(mode='json')


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
    elif body.proxy is not None:
        errors.append(FieldError(field='proxy', message='only proxy repositories have an upstream', code='invalid'))
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
        format_options=options,
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
    log.info('repository updated', extra={'repo': repo.name})
    return repo


async def delete(services: Services, repo: RepoConfig) -> None:
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
