"""Who is making a request (SPECIFICATION §3 "Principal")."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class PrincipalKind(StrEnum):
    USER = 'user'
    ANONYMOUS = 'anonymous'
    SERVICE_ACCOUNT = 'service_account'


class AuthMethod(StrEnum):
    NONE = 'none'
    SESSION = 'session'
    REGISTRY_TOKEN = 'registry_token'  # noqa: S105 - an enum label, not a secret
    BASIC = 'basic'


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
    extra: dict[str, str] = field(default_factory=dict, compare=False)

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
