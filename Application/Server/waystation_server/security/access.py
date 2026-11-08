"""The single authorization decision point (ARCHITECTURE §7, SPECIFICATION §4.10).

Decisions
---------
``check(principal, action, repo=None)``, ``can_see``, ``filter_repos``, ``require``/``require_repo`` (raise) and
``capabilities`` are the only places that look at roles. Nothing else in the server may inspect role names.

- Permissions are ``(action, scope)``. Repository actions: ``browse``, ``read``, ``write``, ``delete``,
  ``manage``; global actions: ``admin``, ``user-admin``, ``audit-read``, ``stats-read``, ``settings-admin``.
  ``admin`` on ``*`` implies everything.
- Scopes: ``*``, a repository name (case-insensitive), a name glob (``docker-*``), ``format:<format>`` and
  ``type:<proxy|hosted|group>``. Global actions are only granted by scope ``*``.
- ``write``/``delete``/``manage`` imply ``browse`` and ``read`` on the same repositories.
- A repository the principal can neither browse nor read is **hidden**: every route answers 404 for it, never 403.
  Visible but not allowed: 401 for anonymous (so clients send credentials), 403 otherwise.
- Built-in roles (M0: no custom roles, groups or grant cache yet): ``admin``, ``reader`` (browse + read all),
  ``deployer`` (browse + read + write all), ``viewer`` (browse all + stats), ``anonymous`` (browse + read on proxy
  repositories, SPECIFICATION §4.9 default).
- API tokens only **narrow** their owner's grants: a token's scopes (``read``, ``write``, ``delete``, ``manage``,
  ``stats-read``, ``metrics``, ``admin``; ``TOKEN_SCOPE_ACTIONS``) limit the actions, its repository patterns (the
  scope syntax above) limit repository actions to matching repositories; the owner's current RBAC still applies.
  A token can only be given scopes its owner holds (``token_scopes_for``).

Route declarations
------------------
Every route declares its rule with a decorator (``public``, ``authenticated``, ``requires``, ``requires_repo``,
``filtered``, ``handler_checked``); the app-wide dependency ``enforce`` applies it before the handler runs, after
authenticating the request and checking CSRF for unsafe methods on session requests. A route without a rule is
refused (fail closed) and the route-enumeration test fails.
"""

from __future__ import annotations

import dataclasses
import fnmatch
import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal, TypeVar

from starlette.requests import Request

from waystation_common.errors import Forbidden, NotFound, Unauthorized
from waystation_server.repos.model import RepoConfig
from waystation_server.security.actions import GLOBAL_ACTIONS, Action, TokenScope
from waystation_server.security.authn import authenticate, current_session
from waystation_server.security.principal import AuthMethod, Principal, TokenLimits
from waystation_server.security.sessions import CSRF_HEADER, csrf_matches

log = logging.getLogger('waystation.access')


__all__ = ['GLOBAL_ACTIONS', 'Action', 'TokenScope']

# What a granted action also allows on the same scope.
IMPLIES: dict[Action, frozenset[Action]] = {
    Action.WRITE: frozenset({Action.BROWSE, Action.READ}),
    Action.DELETE: frozenset({Action.BROWSE, Action.READ}),
    Action.MANAGE: frozenset({Action.BROWSE, Action.READ}),
}
SAFE_METHODS = frozenset({'GET', 'HEAD', 'OPTIONS'})


@dataclass(frozen=True)
class Permission:
    action: Action
    scope: str


def _perms(*pairs: tuple[Action, str]) -> tuple[Permission, ...]:
    return tuple(Permission(action, scope) for action, scope in pairs)


BUILTIN_ROLES: dict[str, tuple[Permission, ...]] = {
    'admin': _perms((Action.ADMIN, '*')),
    'reader': _perms((Action.BROWSE, '*'), (Action.READ, '*')),
    'deployer': _perms((Action.BROWSE, '*'), (Action.READ, '*'), (Action.WRITE, '*')),
    'viewer': _perms((Action.BROWSE, '*'), (Action.STATS_READ, '*')),
    'anonymous': _perms((Action.BROWSE, 'type:proxy'), (Action.READ, 'type:proxy')),
}


ROLE_DESCRIPTIONS: dict[str, tuple[str, str]] = {
    'admin': ('Administrator', 'Everything, including users, providers and settings'),
    'reader': ('Reader', 'Browse and download from every repository'),
    'deployer': ('Deployer', 'Browse, download and upload to every repository'),
    'viewer': ('Viewer', 'Browse every repository and read statistics'),
}


def roles() -> list[tuple[str, str, str]]:
    """Grantable roles as ``(id, name, description)`` (built-in only until custom roles land; ``anonymous`` is the
    anonymous principal's own role and cannot be granted)."""
    return [(role, *ROLE_DESCRIPTIONS.get(role, (role, ''))) for role in BUILTIN_ROLES if is_role(role)]


@dataclass(frozen=True)
class Grants:
    is_admin: bool
    permissions: tuple[Permission, ...]


@lru_cache(maxsize=1024)
def grants_for(roles: tuple[str, ...]) -> Grants:
    perms: list[Permission] = []
    for role in roles:
        for perm in BUILTIN_ROLES.get(role, ()):
            perms.append(perm)
            perms.extend(Permission(implied, perm.scope) for implied in IMPLIES.get(perm.action, ()))
    is_admin = Permission(Action.ADMIN, '*') in perms
    return Grants(is_admin=is_admin, permissions=tuple(dict.fromkeys(perms)))


def scope_matches(scope: str, repo: RepoConfig) -> bool:
    if scope == '*':
        return True
    kind, sep, value = scope.partition(':')
    if sep and kind == 'format':
        return repo.format == value
    if sep and kind == 'type':
        return repo.type.value == value
    if any(ch in scope for ch in '*?['):
        return fnmatch.fnmatchcase(repo.name.lower(), scope.lower())
    return repo.name.lower() == scope.lower()


# ---------------------------------------------------------------------------- API token scopes
_REPO_READ = frozenset({Action.BROWSE, Action.READ})
TOKEN_SCOPE_ACTIONS: dict[TokenScope, frozenset[Action]] = {
    TokenScope.READ: _REPO_READ,
    TokenScope.WRITE: _REPO_READ | {Action.WRITE},
    TokenScope.DELETE: _REPO_READ | {Action.DELETE},
    TokenScope.MANAGE: _REPO_READ | {Action.MANAGE},
    TokenScope.STATS_READ: frozenset({Action.STATS_READ}),
    TokenScope.METRICS: frozenset(),
    TokenScope.ADMIN: frozenset(Action),
}
# What the owner must hold (somewhere) to put a scope on a token.
TOKEN_SCOPE_REQUIRES: dict[TokenScope, Action] = {
    TokenScope.READ: Action.READ,
    TokenScope.WRITE: Action.WRITE,
    TokenScope.DELETE: Action.DELETE,
    TokenScope.MANAGE: Action.MANAGE,
    TokenScope.STATS_READ: Action.STATS_READ,
    TokenScope.METRICS: Action.STATS_READ,
    TokenScope.ADMIN: Action.ADMIN,
}
_PATTERN = re.compile(r'^(?:format:[a-z0-9]+|type:(?:proxy|hosted|group)|[A-Za-z0-9._\-*?]{1,128})$')


def token_actions(scopes: Iterable[str]) -> frozenset[str]:
    """The actions a token with ``scopes`` may use (before the owner's RBAC is applied)."""
    actions: set[str] = set()
    for scope in scopes:
        try:
            actions |= TOKEN_SCOPE_ACTIONS[TokenScope(scope)]
        except ValueError:
            continue  # unknown scope (newer release): grants nothing
    return frozenset(actions)


def token_permits(limits: TokenLimits, action: Action, repo: RepoConfig | None) -> bool:
    if action not in limits.actions:
        return False
    if repo is None or action in GLOBAL_ACTIONS or not limits.repositories:
        return True
    return any(scope_matches(pattern, repo) for pattern in limits.repositories)


def token_scopes_for(principal: Principal) -> list[TokenScope]:
    """The scopes ``principal`` may put on a new token: only what it holds itself (tokens only narrow)."""
    return [scope for scope in TokenScope if check(principal, TOKEN_SCOPE_REQUIRES[scope])]


def may_scrape_metrics(principal: Principal) -> bool:
    """``/metrics`` from outside the allowed networks: an API token with the ``metrics`` scope whose owner holds
    ``stats-read``."""
    if principal.token is None or TokenScope.METRICS.value not in principal.token.scopes:
        return False
    return check(dataclasses.replace(principal, token=None, token_access=None), Action.STATS_READ)


def valid_repository_pattern(pattern: str) -> bool:
    """A token's repository restriction uses the RBAC scope syntax: a name, a glob, ``format:``, ``type:``."""
    return bool(_PATTERN.fullmatch(pattern)) and pattern != '*'


def is_role(name: str) -> bool:
    return name in BUILTIN_ROLES and name != 'anonymous'


def may_assign_role(principal: Principal, role: str) -> bool:
    """Giving a role away needs the role's global powers: only administrators grant ``admin``."""
    return not grants_for((role,)).is_admin or check(principal, Action.ADMIN)


# ---------------------------------------------------------------------------- decisions
def check(principal: Principal, action: Action, repo: RepoConfig | None = None) -> bool:
    """May ``principal`` perform ``action`` (on ``repo``; without a repo: on at least one repository)?

    A principal authenticated by a Docker registry token is also limited to what the token grants, and one
    authenticated by an API token to the token's scopes and repository patterns (ARCH §7): tokens only narrow."""
    if principal.token is not None and not token_permits(principal.token, action, repo):
        return False
    if principal.token_access is not None:
        if action in GLOBAL_ACTIONS or repo is None:
            return False
        granted = {a for name, actions in principal.token_access if name == repo.name for a in actions}
        if action not in granted:
            return False
    grants = grants_for(principal.roles)
    if grants.is_admin:
        return True
    if action in GLOBAL_ACTIONS:
        return any(p.action is action and p.scope == '*' for p in grants.permissions)
    if repo is None:
        return any(p.action is action for p in grants.permissions)
    return any(p.action is action and scope_matches(p.scope, repo) for p in grants.permissions)


def can_see(principal: Principal, repo: RepoConfig) -> bool:
    return check(principal, Action.BROWSE, repo) or check(principal, Action.READ, repo)


def filter_repos(principal: Principal, action: Action, repos: Iterable[RepoConfig]) -> list[RepoConfig]:
    """Repositories ``principal`` may ``action`` (listings are filtered, never just hidden in the UI)."""
    return [repo for repo in repos if check(principal, action, repo)]


def _denied(principal: Principal, detail: str) -> Exception:
    return Unauthorized('authentication required') if principal.is_anonymous else Forbidden(detail)


def require(principal: Principal, action: Action) -> None:
    if not check(principal, action):
        raise _denied(principal, f'{action.value} permission required')


def require_repo(principal: Principal, action: Action, repo: RepoConfig) -> None:
    """Raise ``NotFound`` when ``repo`` is hidden from ``principal``, 401/403 when visible but not allowed."""
    if not can_see(principal, repo):
        raise NotFound('Not Found')
    if not check(principal, action, repo):
        raise _denied(principal, f'{action.value} permission required on this repository')


def capabilities(principal: Principal) -> list[str]:
    """UI hints (OpenAPI ``Session.capabilities``); never a substitute for server-side checks."""
    caps: list[str] = []
    if check(principal, Action.ADMIN):
        caps += ['admin', 'repositories.create']
    if check(principal, Action.MANAGE):
        caps.append('repositories.manage')
    if check(principal, Action.STATS_READ):
        caps.append('stats.read')
    if check(principal, Action.AUDIT_READ):
        caps.append('audit.read')
    return caps


def method_action(method: str) -> Action:
    if method in ('GET', 'HEAD'):
        return Action.READ
    if method == 'DELETE':
        return Action.DELETE
    return Action.WRITE


async def authorize_repo(request: Request, repo: RepoConfig, action: Action) -> Principal:
    """Authenticate and authorise ``action`` on ``repo`` (format routes)."""
    principal = await authenticate(request)
    require_repo(principal, action, repo)
    return principal


# ---------------------------------------------------------------------------- route declarations
Kind = Literal['public', 'authenticated', 'global', 'repo', 'filtered', 'handler']
F = TypeVar('F', bound=Callable[..., object])
ATTR = '__ws_access__'


@dataclass(frozen=True)
class RouteAccess:
    kind: Kind
    action: Action | None = None
    param: str | None = None


def _declare(rule: RouteAccess) -> Callable[[F], F]:
    def decorate(fn: F) -> F:
        setattr(fn, ATTR, rule)
        return fn

    return decorate


def public[T: Callable[..., object]](fn: T) -> T:
    """No authentication needed (health, sign-in, instance config)."""
    setattr(fn, ATTR, RouteAccess('public'))
    return fn


def authenticated[T: Callable[..., object]](fn: T) -> T:
    """Any signed-in principal (not anonymous)."""
    setattr(fn, ATTR, RouteAccess('authenticated'))
    return fn


def requires(action: Action) -> Callable[[F], F]:
    """A global action (``stats-read``, ``admin``...)."""
    if action not in GLOBAL_ACTIONS:
        raise ValueError(f'{action} is a repository action: use requires_repo')
    return _declare(RouteAccess('global', action))


def requires_repo(action: Action, param: str = 'name') -> Callable[[F], F]:
    """An action on the repository named by path parameter ``param``; the repository lands on
    ``request.state.repo``. Hidden or unknown repositories are 404."""
    return _declare(RouteAccess('repo', action, param))


def filtered(action: Action) -> Callable[[F], F]:
    """Any principal; the handler returns only what ``filter_repos(principal, action, ...)`` allows."""
    return _declare(RouteAccess('filtered', action))


def handler_checked[T: Callable[..., object]](fn: T) -> T:
    """The handler authorises itself through this module (format routes resolve the repository first)."""
    setattr(fn, ATTR, RouteAccess('handler'))
    return fn


def route_rule(endpoint: object) -> RouteAccess | None:
    rule = getattr(endpoint, ATTR, None)
    return rule if isinstance(rule, RouteAccess) else None


def _check_csrf(request: Request, principal: Principal) -> None:
    if request.method in SAFE_METHODS or principal.method is not AuthMethod.SESSION:
        return
    session = current_session(request)
    if session is None or not csrf_matches(session, request.headers.get(CSRF_HEADER)):
        raise Forbidden('CSRF token missing or invalid')


async def enforce(request: Request) -> None:
    """App-wide dependency: apply the matched route's declared rule (fail closed when there is none)."""
    route = request.scope.get('route')
    rule = route_rule(getattr(route, 'endpoint', None))
    if rule is None:
        log.error('route without an access rule', extra={'path': getattr(route, 'path', '?')})
        raise Forbidden('access rule missing')
    if rule.kind == 'public':
        return
    principal = await authenticate(request)
    _check_csrf(request, principal)
    match rule.kind:
        case 'authenticated':
            if principal.is_anonymous:
                raise Unauthorized('authentication required')
        case 'global':
            assert rule.action is not None
            require(principal, rule.action)
        case 'repo':
            assert rule.action is not None
            assert rule.param is not None
            name = str(request.path_params.get(rule.param, ''))
            catalog = request.app.state.services.catalog
            repo = await catalog.get(name) or await catalog.get_ci(name)
            if repo is None:
                raise NotFound('repository not found')
            require_repo(principal, rule.action, repo)
            request.state.repo = repo
        case 'filtered' | 'handler':
            pass
