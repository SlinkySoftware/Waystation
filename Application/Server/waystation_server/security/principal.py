"""Who is making a request (SPECIFICATION §3 "Principal")."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from waystation_server.security.actions import Grants


class PrincipalKind(StrEnum):
    USER = 'user'
    ANONYMOUS = 'anonymous'
    SERVICE_ACCOUNT = 'service_account'


class AuthMethod(StrEnum):
    NONE = 'none'
    SESSION = 'session'
    REGISTRY_TOKEN = 'registry_token'  # noqa: S105 - an enum label, not a secret
    BASIC = 'basic'
    API_TOKEN = 'api_token'  # noqa: S105 - an enum label, not a secret


@dataclass(frozen=True)
class TokenLimits:
    """What an API token narrows its owner's grants to (ARCHITECTURE §7): only ``actions``, and repository actions
    only on repositories matching one of ``repositories`` (RBAC scope patterns; empty = every repository)."""

    token_id: str
    actions: frozenset[str]
    repositories: tuple[str, ...] = ()
    scopes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Principal:
    kind: PrincipalKind
    id: str
    name: str
    roles: tuple[str, ...] = ()
    method: AuthMethod = AuthMethod.NONE
    session_id: str | None = None
    # Docker registry tokens carry their own grants (``(resource_name, frozenset(actions))``); access is the
    # intersection of the token grants and the principal's RBAC grants (ARCHITECTURE §7).
    token_access: tuple[tuple[str, frozenset[str]], ...] | None = None
    # API tokens (and registry tokens issued for one) narrow the owner's grants further (``TokenLimits``).
    token: TokenLimits | None = None
    extra: dict[str, str] = field(default_factory=dict, compare=False)
    # Where a user signs in from (``local``, ``ldap:<id>``, ``oidc:<id>``): audit records only, never authorisation.
    provider: str | None = field(default=None, compare=False)
    # The permissions of ``roles``, materialised by ``security.rbac`` (custom roles, ``grant_version`` cache). None:
    # ``security.access`` derives them from the built-in roles only (the anonymous principal, unit tests).
    grants: Grants | None = field(default=None, compare=False)
    # A local user still holding a one-time password: its sessions may only change it or sign out (SPEC §4.9).
    must_change_password: bool = field(default=False, compare=False)

    @property
    def is_anonymous(self) -> bool:
        return self.kind is PrincipalKind.ANONYMOUS

    @property
    def display(self) -> str:
        return 'anonymous' if self.is_anonymous else self.name


ANONYMOUS_ROLE = 'anonymous'
ANONYMOUS = Principal(PrincipalKind.ANONYMOUS, 'anonymous', 'anonymous', roles=(ANONYMOUS_ROLE,))


def anonymous(*, method: AuthMethod = AuthMethod.NONE) -> Principal:
    return (
        ANONYMOUS
        if method is AuthMethod.NONE
        else Principal(PrincipalKind.ANONYMOUS, 'anonymous', 'anonymous', roles=(ANONYMOUS_ROLE,), method=method)
    )
