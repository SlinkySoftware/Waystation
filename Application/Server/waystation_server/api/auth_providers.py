"""``/api/v1/auth-providers`` (external sign-in providers: CRUD, test connection, test user) and ``/api/v1/roles``.

Needs ``settings-admin`` (``admin`` implies it). Validation errors are 422 field errors with dotted paths into the
body (``ldap.connection.servers.1``, ``group_mapping.0.roles``); a name already used by another provider is 409.
A provider has the configuration of its kind (``ldap`` or ``oidc``) and not the other one.
The LDAP bind password and the OIDC client secret are write-only: encrypted with ``WAYSTATION_SECRET_KEYS``
(associated data ``auth_provider:<id>``), kept when an update omits them, dropped when no longer needed (bind DN
cleared, public OIDC client); reads say ``bind_password_set`` / ``client_secret_set``. OIDC reads also give the
redirect and post-logout URIs to register at the provider. Saving a provider replaces its group mapping (``groups``
documents) and bumps its ``config_version`` (cached password checks of the old configuration stop matching).
"Test connection" checks every LDAP server, or for OIDC: discovery, keys, algorithms, PKCE and the client
credentials. "Test user" is LDAP only (an OIDC sign-in needs the browser).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from waystation_common.audit import REDACTED, AuditChange
from waystation_common.errors import Conflict, NotFound
from waystation_common.ids import new_id
from waystation_server import audit
from waystation_server.api.deps import ServicesDep
from waystation_server.audit import AuditAction
from waystation_server.db import auth_providers as providers_db
from waystation_server.db import groups as groups_db
from waystation_server.oidc_auth import callback_uri, post_logout_uri
from waystation_server.problems import FieldError, FieldErrors
from waystation_server.security import access
from waystation_server.security import ldap as ldap_mod
from waystation_server.security import oidc as oidc_mod
from waystation_server.security.access import Action
from waystation_server.security.authn import authenticate
from waystation_server.security.groups import GroupRecord, mapped_roles
from waystation_server.security.providers import (
    AuthProviderWrite,
    GroupMapping,
    GroupMode,
    LdapConfigView,
    LdapConfigWrite,
    LdapConnectionView,
    OidcClientAuth,
    OidcConfigView,
    OidcConfigWrite,
    ProviderKind,
    ProviderRecord,
    StoredLdapConfig,
    StoredOidcConfig,
    decrypt_bind_password,
    decrypt_client_secret,
    dump_stored,
    dump_stored_oidc,
    group_key,
    secret_aad,
)
from waystation_server.services import Services

router = APIRouter(tags=['admin'])

PROBLEM = {'content': {'application/problem+json': {'schema': {'$ref': '#/components/schemas/Problem'}}}}
_ATTRIBUTE = re.compile(r'^(?:[A-Za-z][A-Za-z0-9-]*|\d+(?:\.\d+)+)$')


# ---------------------------------------------------------------------------- schemas
class AuthProviderConfig(BaseModel):
    id: str
    kind: ProviderKind
    name: str
    enabled: bool
    order: int
    ldap: LdapConfigView | None
    oidc: OidcConfigView | None
    group_mapping: list[GroupMapping]
    created_at: datetime
    updated_at: datetime


class Role(BaseModel):
    id: str
    name: str
    description: str
    builtin: bool


class AuthProviderTestRequest(BaseModel):
    id: str | None = Field(None, max_length=64)
    provider: AuthProviderWrite
    username: str | None = Field(None, max_length=256)
    password: str | None = Field(None, max_length=1024, json_schema_extra={'writeOnly': True})


class ProviderCheck(BaseModel):
    name: str
    ok: bool
    message: str


class LdapServerTest(BaseModel):
    server: str
    ok: bool
    latency_ms: float | None
    tls: str | None = None
    checks: list[ProviderCheck]


class OidcDiscovery(BaseModel):
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    userinfo_endpoint: str | None
    end_session_endpoint: str | None
    signing_algs: list[str] | None = Field(description='`id_token_signing_alg_values_supported`')
    scopes_supported: list[str] | None
    keys: int = Field(description='Signing keys usable with the accepted algorithms')
    redirect_uri: str = Field(description='Register this redirect URI at the provider')
    post_logout_redirect_uri: str


class AuthProviderConnectionTest(BaseModel):
    ok: bool
    servers: list[LdapServerTest]
    discovery: OidcDiscovery | None = Field(None, description='OIDC: what discovery found')


class ExternalGroup(BaseModel):
    id: str
    name: str


class DirectoryUser(BaseModel):
    dn: str
    username: str
    display_name: str
    email: str | None


class AuthProviderUserTest(BaseModel):
    ok: bool
    found: bool
    server: str | None
    user: DirectoryUser | None
    groups: list[ExternalGroup]
    require_group_ok: bool | None
    password: Literal['not_tested', 'ok', 'failed']
    roles: list[str]
    checks: list[ProviderCheck]


# ---------------------------------------------------------------------------- validation
def _check_ldap(config: LdapConfigWrite, *, stored_secret: bool, keyring_set: bool) -> list[FieldError]:
    errors: list[FieldError] = []
    conn = config.connection

    def add(field: str, message: str, code: str = 'invalid') -> None:
        errors.append(FieldError(field=f'ldap.{field}', message=message, code=code))

    for i, server in enumerate(conn.servers):
        try:
            ldap_mod.parse_server(server, conn.security)
        except ValueError as exc:
            add(f'connection.servers.{i}', str(exc), 'format')
    if len({s.strip().lower() for s in conn.servers}) != len(conn.servers):
        add('connection.servers', 'servers must be unique', 'duplicate')
    if conn.ca_pem and conn.ca_pem.strip():
        try:
            ldap_mod.check_ca(conn.ca_pem)
        except ValueError as exc:
            add('connection.ca_pem', str(exc), 'format')
    if conn.bind_dn:
        try:
            ldap_mod.check_dn(conn.bind_dn)
        except ValueError as exc:
            add('connection.bind_dn', str(exc), 'format')
        if not conn.bind_password and not stored_secret:
            add('connection.bind_password', 'a bind DN needs a password', 'required')
    if conn.bind_password and not keyring_set:
        add('connection.bind_password', 'WAYSTATION_SECRET_KEYS is not set: secrets cannot be stored', 'secret_keys')
    for field, value in (('users.base_dn', config.users.base_dn), ('groups.base_dn', config.groups.base_dn)):
        if value:
            try:
                ldap_mod.check_dn(value)
            except ValueError as exc:
                add(field, str(exc), 'format')
    if config.groups.mode is GroupMode.SEARCH and not config.groups.base_dn:
        add('groups.base_dn', 'group search needs a base DN', 'required')
    for field, value in (('users.filter', config.users.filter), ('groups.filter', config.groups.filter)):
        try:
            ldap_mod.check_filter(value)
        except ValueError as exc:
            add(field, str(exc), 'format')
    attributes = {
        'users.username_attribute': config.users.username_attribute,
        'users.display_name_attribute': config.users.display_name_attribute,
        'users.email_attribute': config.users.email_attribute,
        'groups.member_attribute': config.groups.member_attribute,
        'groups.name_attribute': config.groups.name_attribute,
        'groups.member_of_attribute': config.groups.member_of_attribute,
    }
    for field, value in attributes.items():
        if not _ATTRIBUTE.match(value):
            add(field, 'an attribute name (letters, digits, hyphens) or an OID', 'format')
    return errors


def _check_mapping(mapping: list[GroupMapping]) -> list[FieldError]:
    errors: list[FieldError] = []
    seen: set[str] = set()
    for i, row in enumerate(mapping):
        key = group_key(row.external_group)
        if key in seen:
            errors.append(
                FieldError(field=f'group_mapping.{i}.external_group', message='mapped twice', code='duplicate')
            )
        seen.add(key)
        unknown = [r for r in row.roles if not access.is_role(r)]
        if unknown:
            errors.append(
                FieldError(field=f'group_mapping.{i}.roles', message=f'unknown role: {unknown[0]}', code='unknown')
            )
    return errors


def _check_oidc(config: OidcConfigWrite, *, stored_secret: bool, keyring_set: bool) -> list[FieldError]:
    errors: list[FieldError] = []

    def add(field: str, message: str, code: str = 'invalid') -> None:
        errors.append(FieldError(field=f'oidc.{field}', message=message, code=code))

    problem = oidc_mod.check_issuer(config.issuer, allow_private=config.allow_private)
    if problem:
        add('issuer', f'must be {problem}', 'format')
    if config.client_auth is not OidcClientAuth.NONE and not config.client_secret and not stored_secret:
        add(
            'client_secret', 'a confidential client needs its secret (or choose "none" for a public client)', 'required'
        )
    if config.client_secret and not keyring_set:
        add('client_secret', 'WAYSTATION_SECRET_KEYS is not set: secrets cannot be stored', 'secret_keys')
    if config.require_group and not config.groups_claim:
        add('require_group', 'a required group needs a groups claim', 'required')
    if len(set(config.signing_algs)) != len(config.signing_algs):
        add('signing_algs', 'algorithms must be unique', 'duplicate')
    return errors


def _check_kind(body: AuthProviderWrite, prefix: str = '') -> list[FieldError]:
    """The kind's configuration is present and the other kind's is not."""
    errors: list[FieldError] = []
    present = {'ldap': body.ldap is not None, 'oidc': body.oidc is not None}
    for kind, given in present.items():
        if given and body.kind.value != kind:
            errors.append(FieldError(field=f'{prefix}{kind}', message=f'only for kind {kind}', code='unexpected'))
        if not given and body.kind.value == kind:
            errors.append(FieldError(field=f'{prefix}{kind}', message=f'kind {kind} needs it', code='required'))
    return errors


def _check_config(
    body: AuthProviderWrite, existing: ProviderRecord | None, *, keyring_set: bool, prefix: str = ''
) -> list[FieldError]:
    errors = _check_kind(body, prefix)
    if errors:
        return errors
    if body.ldap is not None:
        stored = bool(existing and existing.ldap and existing.ldap.connection.bind_password_enc)
        errors = _check_ldap(body.ldap, stored_secret=stored, keyring_set=keyring_set)
    elif body.oidc is not None:
        stored = bool(existing and existing.oidc and existing.oidc.client_secret_enc)
        errors = _check_oidc(body.oidc, stored_secret=stored, keyring_set=keyring_set)
    return [e.model_copy(update={'field': f'{prefix}{e.field}'}) for e in errors] if prefix else errors


def _validate(svc: Services, body: AuthProviderWrite, existing: ProviderRecord | None) -> None:
    errors = _check_config(body, existing, keyring_set=svc.keyring is not None)
    errors += _check_mapping(body.group_mapping)
    if errors:
        raise FieldErrors(errors)


async def _check_name(svc: Services, name: str, own_id: str | None) -> None:
    for record in await providers_db.list_all(svc.db):
        if record.id != own_id and record.name.strip().lower() == name.strip().lower():
            raise Conflict('a provider with this name already exists')


def _encrypt(svc: Services, provider_id: str, secret: str) -> str:
    assert svc.keyring is not None  # checked by _validate
    return svc.keyring.encrypt_str(secret, aad=secret_aad(provider_id))


def _stored_config(
    svc: Services, provider_id: str, body: AuthProviderWrite, existing: ProviderRecord | None
) -> dict[str, Any]:
    """The ``ldap`` / ``oidc`` fields to store: new secrets encrypted, omitted ones kept, unneeded ones dropped."""
    ldap_doc: dict[str, Any] | None = None
    oidc_doc: dict[str, Any] | None = None
    if body.ldap is not None:
        conn = body.ldap.connection
        bind_enc = None
        if conn.bind_dn:
            stored = existing.ldap.connection.bind_password_enc if existing and existing.ldap else None
            bind_enc = _encrypt(svc, provider_id, conn.bind_password) if conn.bind_password else stored
        ldap_doc = dump_stored(body.ldap, bind_enc)
    if body.oidc is not None:
        secret_enc = None
        if body.oidc.client_auth is not OidcClientAuth.NONE:
            stored = existing.oidc.client_secret_enc if existing and existing.oidc else None
            secret_enc = _encrypt(svc, provider_id, body.oidc.client_secret) if body.oidc.client_secret else stored
        oidc_doc = dump_stored_oidc(body.oidc, secret_enc)
    return {'ldap': ldap_doc, 'oidc': oidc_doc}


# ---------------------------------------------------------------------------- views
def _ldap_view(stored: StoredLdapConfig) -> LdapConfigView:
    data = stored.connection.model_dump(exclude={'bind_password_enc'})
    connection = LdapConnectionView(**data, bind_password_set=bool(stored.connection.bind_password_enc))
    return LdapConfigView(
        connection=connection, users=stored.users, groups=stored.groups, cache_ttl_s=stored.cache_ttl_s
    )


def _oidc_view(svc: Services, record: ProviderRecord, stored: StoredOidcConfig) -> OidcConfigView:
    return OidcConfigView(
        **stored.model_dump(exclude={'client_secret_enc'}),
        client_secret_set=bool(stored.client_secret_enc),
        redirect_uri=callback_uri(svc.base_url, record.id),
        post_logout_redirect_uri=post_logout_uri(svc.base_url),
    )


def _view(svc: Services, record: ProviderRecord, groups: list[GroupRecord]) -> AuthProviderConfig:
    mapping = [GroupMapping(external_group=g.external_group or g.external_id or '', roles=g.roles) for g in groups]
    return AuthProviderConfig(
        id=record.id,
        kind=record.kind,
        name=record.name,
        enabled=record.enabled,
        order=record.order,
        ldap=_ldap_view(record.ldap) if record.ldap else None,
        oidc=_oidc_view(svc, record, record.oidc) if record.oidc else None,
        group_mapping=mapping,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


async def _load(svc: Services, provider_id: str) -> ProviderRecord:
    record = await providers_db.get(svc.db, provider_id)
    if record is None:
        raise NotFound('provider not found')
    return record


def _audit_changes(
    before: AuthProviderConfig | None, after: AuthProviderConfig | None, body: AuthProviderWrite | None
) -> list[AuditChange]:
    """Changes between two views (which never hold secrets), plus a redacted entry when a new LDAP bind password
    or OIDC client secret was supplied."""
    changes = audit.diff(before, after)
    if body is None:
        return changes
    if body.ldap is not None and body.ldap.connection.bind_password:
        had = before is not None and before.ldap is not None and before.ldap.connection.bind_password_set
        changes.append(
            AuditChange(
                field='ldap.connection.bind_password', before=REDACTED if had else None, after=REDACTED, redacted=True
            )
        )
    if body.oidc is not None and body.oidc.client_secret:
        had = before is not None and before.oidc is not None and before.oidc.client_secret_set
        changes.append(
            AuditChange(field='oidc.client_secret', before=REDACTED if had else None, after=REDACTED, redacted=True)
        )
    return changes


def _changed(svc: Services) -> None:
    svc.auth_providers.invalidate()
    svc.groups.invalidate()
    svc.oidc.forget()


# ---------------------------------------------------------------------------- routes
@router.get('/auth-providers', response_model=list[AuthProviderConfig], operation_id='listAuthProviderConfigs')
@access.requires(Action.SETTINGS_ADMIN)
async def list_auth_providers(svc: ServicesDep) -> list[AuthProviderConfig]:
    records = sorted(await providers_db.list_all(svc.db), key=lambda r: (r.order, r.name.lower()))
    return [_view(svc, r, await groups_db.for_provider(svc.db, r.id)) for r in records]


@router.post(
    '/auth-providers',
    status_code=201,
    response_model=AuthProviderConfig,
    operation_id='createAuthProvider',
    responses={409: PROBLEM, 422: PROBLEM},
)
@access.requires(Action.SETTINGS_ADMIN)
async def create_auth_provider(
    body: AuthProviderWrite, request: Request, response: Response, svc: ServicesDep
) -> AuthProviderConfig:
    principal = await authenticate(request)
    _validate(svc, body, None)
    await _check_name(svc, body.name, None)
    provider_id = new_id()
    now = datetime.now(UTC)
    doc = {
        '_id': provider_id,
        'kind': body.kind.value,
        'name': body.name,
        'enabled': body.enabled,
        'order': body.order,
        **_stored_config(svc, provider_id, body, None),
        'config_version': 1,
        'created_at': now,
        'updated_at': now,
        'created_by': principal.display,
        'updated_by': principal.display,
    }
    record = await providers_db.insert(svc.db, doc)
    await groups_db.replace_mapping(
        svc.db,
        kind=record.kind.value,
        provider_id=record.id,
        provider_name=record.name,
        mapping=body.group_mapping,
        now=now,
    )
    _changed(svc)
    response.headers['Location'] = f'/api/v1/auth-providers/{record.id}'
    view = _view(svc, record, await groups_db.for_provider(svc.db, record.id))
    audit.record(
        AuditAction.AUTH_PROVIDER_CREATE,
        target=audit.target('auth_provider', record.id, record.name),
        changes=_audit_changes(None, view, body),
    )
    return view


@router.post(
    '/auth-providers/test-connection',
    response_model=AuthProviderConnectionTest,
    operation_id='testAuthProviderConnection',
    responses={422: PROBLEM},
)
@access.requires(Action.SETTINGS_ADMIN)
async def test_connection(body: AuthProviderTestRequest, svc: ServicesDep) -> AuthProviderConnectionTest:
    if body.provider.kind is ProviderKind.OIDC:
        return await _test_discovery(svc, body)
    directory = await _test_directory(svc, body)
    _audit_test(body, 'connection')
    try:
        servers = await ldap_mod.run_blocking(directory.config, directory.test_connection)
    except TimeoutError:
        servers = [
            ldap_mod.ServerTest(server=s.url, checks=[ldap_mod.Check('connect', False, 'timed out')])
            for s in directory.servers
        ]
    views = [
        LdapServerTest(
            server=s.server,
            ok=s.ok,
            latency_ms=s.latency_ms,
            tls=s.tls,
            checks=[ProviderCheck(name=c.name, ok=c.ok, message=c.message) for c in s.checks],
        )
        for s in servers
    ]
    return AuthProviderConnectionTest(ok=all(v.ok for v in views), servers=views)


@router.post(
    '/auth-providers/test-user',
    response_model=AuthProviderUserTest,
    operation_id='testAuthProviderUser',
    responses={422: PROBLEM},
)
@access.requires(Action.SETTINGS_ADMIN)
async def test_user(body: AuthProviderTestRequest, svc: ServicesDep) -> AuthProviderUserTest:
    if not body.username or not body.username.strip():
        raise FieldErrors([FieldError(field='username', message='a username is needed', code='required')])
    directory = await _test_directory(svc, body)
    username, password = body.username.strip(), body.password or None
    _audit_test(body, 'user')
    try:
        result = await ldap_mod.run_blocking(directory.config, lambda: directory.test_user(username, password))
    except TimeoutError:
        result = ldap_mod.UserTest(checks=[ldap_mod.Check('connect', False, 'timed out')])
    groups = [(g.id, g.name) for g in result.groups]
    mapping = [(row.external_group, row.roles) for row in body.provider.group_mapping]
    user = result.user
    return AuthProviderUserTest(
        ok=result.ok,
        found=result.found,
        server=result.server,
        user=DirectoryUser(dn=user.dn, username=user.username, display_name=user.display_name, email=user.email)
        if user
        else None,
        groups=[ExternalGroup(id=g.id, name=g.name) for g in result.groups],
        require_group_ok=result.require_group_ok,
        password=result.bind_outcome,
        roles=mapped_roles(mapping, groups) if result.found else [],
        checks=[ProviderCheck(name=c.name, ok=c.ok, message=c.message) for c in result.checks],
    )


def _audit_test(body: AuthProviderTestRequest, kind: str) -> None:
    """A user test with a password checks credentials against the directory: audited like the other changes."""
    audit.record(
        AuditAction.AUTH_PROVIDER_TEST,
        target=audit.target('auth_provider', body.id, body.provider.name),
        details={
            'test': kind,
            'servers': body.provider.ldap.connection.servers if body.provider.ldap else None,
            'username': (body.username or '').strip()[:256] or None,
            'with_password': bool(body.password),
        },
    )


async def _test_existing(svc: Services, body: AuthProviderTestRequest) -> ProviderRecord | None:
    """The saved provider whose secret an unsaved test may reuse; validates the tested configuration."""
    existing = await providers_db.get(svc.db, body.id) if body.id else None
    if body.id and existing is None:
        raise NotFound('provider not found')
    if existing is not None and existing.kind is not body.provider.kind:
        raise FieldErrors([FieldError(field='provider.kind', message='the kind cannot change', code='immutable')])
    errors = _check_config(body.provider, existing, keyring_set=True, prefix='provider.')
    if errors:
        raise FieldErrors(errors)
    return existing


async def _test_discovery(svc: Services, body: AuthProviderTestRequest) -> AuthProviderConnectionTest:
    existing = await _test_existing(svc, body)
    cfg = body.provider.oidc
    assert cfg is not None  # checked by _test_existing
    secret = cfg.client_secret or (decrypt_client_secret(svc.keyring, existing) if existing else None)
    if cfg.client_auth is OidcClientAuth.NONE:
        secret = None
    redirect_uri = callback_uri(svc.base_url, existing.id if existing else 'new')
    result = await svc.oidc.diagnose(cfg, secret, redirect_uri=redirect_uri)
    server = LdapServerTest(
        server=cfg.issuer,
        ok=result.ok,
        latency_ms=result.latency_ms,
        tls=None,
        checks=[ProviderCheck(name=c.name, ok=c.ok, message=c.message) for c in result.checks],
    )
    meta = result.metadata
    discovery = (
        OidcDiscovery(
            issuer=meta.issuer,
            authorization_endpoint=meta.authorization_endpoint,
            token_endpoint=meta.token_endpoint,
            jwks_uri=meta.jwks_uri,
            userinfo_endpoint=meta.userinfo_endpoint,
            end_session_endpoint=meta.end_session_endpoint,
            signing_algs=list(meta.signing_algs) if meta.signing_algs is not None else None,
            scopes_supported=list(meta.scopes_supported) if meta.scopes_supported is not None else None,
            keys=result.keys,
            redirect_uri=redirect_uri,
            post_logout_redirect_uri=post_logout_uri(svc.base_url),
        )
        if meta
        else None
    )
    return AuthProviderConnectionTest(ok=result.ok, servers=[server], discovery=discovery)


async def _test_directory(svc: Services, body: AuthProviderTestRequest) -> ldap_mod.LdapDirectory:
    existing = await _test_existing(svc, body)
    if body.provider.ldap is None:
        raise FieldErrors(
            [FieldError(field='provider.kind', message='OIDC providers are tested with a real sign-in', code='kind')]
        )
    conn = body.provider.ldap.connection
    password = conn.bind_password or (decrypt_bind_password(svc.keyring, existing) if existing else None)
    if not conn.bind_dn:
        password = None
    source = existing.source if existing else 'ldap:test'
    return ldap_mod.LdapDirectory(source, body.provider.ldap, password)


@router.get('/auth-providers/{id}', response_model=AuthProviderConfig, operation_id='getAuthProviderConfig')
@access.requires(Action.SETTINGS_ADMIN)
async def get_auth_provider(id: str, svc: ServicesDep) -> AuthProviderConfig:
    record = await _load(svc, id)
    return _view(svc, record, await groups_db.for_provider(svc.db, record.id))


@router.put(
    '/auth-providers/{id}',
    response_model=AuthProviderConfig,
    operation_id='updateAuthProvider',
    responses={409: PROBLEM, 422: PROBLEM},
)
@access.requires(Action.SETTINGS_ADMIN)
async def update_auth_provider(
    id: str,
    body: AuthProviderWrite,
    request: Request,
    svc: ServicesDep,
) -> AuthProviderConfig:
    principal = await authenticate(request)
    existing = await _load(svc, id)
    before = _view(svc, existing, await groups_db.for_provider(svc.db, existing.id))
    if body.kind != existing.kind:
        raise FieldErrors([FieldError(field='kind', message='the kind cannot change', code='immutable')])
    _validate(svc, body, existing)
    await _check_name(svc, body.name, existing.id)
    now = datetime.now(UTC)
    record = await providers_db.replace(
        svc.db,
        existing.id,
        {
            'name': body.name,
            'enabled': body.enabled,
            'order': body.order,
            **_stored_config(svc, existing.id, body, existing),
            'updated_at': now,
            'updated_by': principal.display,
        },
    )
    if record is None:
        raise NotFound('provider not found')
    await groups_db.replace_mapping(
        svc.db,
        kind=record.kind.value,
        provider_id=record.id,
        provider_name=record.name,
        mapping=body.group_mapping,
        now=now,
    )
    _changed(svc)
    view = _view(svc, record, await groups_db.for_provider(svc.db, record.id))
    audit.record(
        AuditAction.AUTH_PROVIDER_UPDATE,
        target=audit.target('auth_provider', record.id, record.name),
        changes=_audit_changes(before, view, body),
    )
    return view


@router.delete('/auth-providers/{id}', status_code=204, operation_id='deleteAuthProvider')
@access.requires(Action.SETTINGS_ADMIN)
async def delete_auth_provider(id: str, svc: ServicesDep) -> Response:
    existing = await _load(svc, id)
    before = _view(svc, existing, await groups_db.for_provider(svc.db, existing.id))
    if not await providers_db.delete(svc.db, id):
        raise NotFound('provider not found')
    await groups_db.delete_for_provider(svc.db, id)
    _changed(svc)
    audit.record(
        AuditAction.AUTH_PROVIDER_DELETE,
        target=audit.target('auth_provider', existing.id, existing.name),
        changes=_audit_changes(before, None, None),
    )
    return Response(status_code=204)


@router.get('/roles', response_model=list[Role], operation_id='listRoles')
@access.authenticated
async def list_roles() -> list[Role]:
    return [Role(id=rid, name=name, description=desc, builtin=True) for rid, name, desc in access.roles()]
