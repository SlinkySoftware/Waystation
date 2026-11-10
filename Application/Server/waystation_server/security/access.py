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
  One exception (ADR 0005, ``hidden_repo``): on the format routes (``/repository/<repo>/...``) a request that sent
  no credentials at all gets the 401 Basic challenge for a hidden repository **and for a missing one**, so package
  clients that only send credentials after a challenge (Maven, Gradle, pip...) work and anonymous callers still
  cannot tell a hidden repository from a missing one. Authenticated callers keep the 404.
- Built-in roles (read-only): ``admin``, ``reader`` (browse + read all), ``deployer`` (browse + read + write all),
  ``viewer`` (browse all + stats), ``anonymous`` (browse + read on proxy repositories, SPECIFICATION §4.9 default).
  Custom roles live in MongoDB (``roles``); ``security.rbac`` materialises a principal's roles into ``Grants`` with
  ``materialise`` (cached per ``grant_version``) and puts them on ``Principal.grants``. A principal without
  materialised grants (anonymous, unit tests) gets its built-in roles' permissions.
- Signing in never reduces access: every authenticated principal (users of any source, service accounts) also
  receives the anonymous principal's grants (``Principal.anonymous_grants``, repository permissions only), so
  ``check`` decides from own ∪ anonymous. Those grants are not the principal's own: delegation (``may_assign``,
  ``may_manage``) ignores them, and the narrowing of the credential used (API token scopes and repository patterns,
  Docker registry token grants) bounds them like everything else.
- Giving away power is bounded by what the giver holds (``may_assign``): global actions only by someone holding
  them, ``admin`` only by administrators; the same rule decides who may manage a user or edit a role
  (``may_manage``).
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
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Literal, TypeVar

from starlette.requests import Request

from waystation_common.errors import Forbidden, NotFound, Unauthorized, WaystationError
from waystation_server.repos.model import RepoConfig
from waystation_server.security.actions import GLOBAL_ACTIONS, Action, Grants, Permission, TokenScope
from waystation_server.security.authn import authenticate, current_session
from waystation_server.security.principal import ANONYMOUS_ROLE, AuthMethod, Principal, TokenLimits
from waystation_server.security.sessions import CSRF_HEADER, csrf_matches

log = logging.getLogger('waystation.access')


__all__ = ['GLOBAL_ACTIONS', 'Action', 'Grants', 'Permission', 'TokenScope']

# What a granted action also allows on the same scope.
IMPLIES: dict[Action, frozenset[Action]] = {
    Action.WRITE: frozenset({Action.BROWSE, Action.READ}),
    Action.DELETE: frozenset({Action.BROWSE, Action.READ}),
    Action.MANAGE: frozenset({Action.BROWSE, Action.READ}),
}
SAFE_METHODS = frozenset({'GET', 'HEAD', 'OPTIONS'})


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


RoleTable = Mapping[str, Sequence[Permission]]  # custom role id -> permissions (``security.rbac``)


def roles() -> list[tuple[str, str, str]]:
    """The grantable built-in roles as ``(id, name, description)`` (``anonymous`` is the anonymous principal's own
    role and cannot be granted)."""
    return [(role, *ROLE_DESCRIPTIONS.get(role, (role, ''))) for role in BUILTIN_ROLES if is_role(role)]


def materialise(roles: Iterable[str], custom: RoleTable | None = None) -> Grants:
    """The permissions of ``roles`` (built-in first, then ``custom``) plus the implied ones. Unknown roles grant
    nothing (a deleted custom role, a newer release's role)."""
    perms: list[Permission] = []
    for role in roles:
        defined = BUILTIN_ROLES.get(role)
        if defined is None and custom is not None:
            defined = tuple(custom.get(role, ()))
        for perm in defined or ():
            perms.append(perm)
            perms.extend(Permission(implied, perm.scope) for implied in IMPLIES.get(perm.action, ()))
    is_admin = Permission(Action.ADMIN, '*') in perms
    return Grants(is_admin=is_admin, permissions=tuple(dict.fromkeys(perms)))


@lru_cache(maxsize=1024)
def grants_for(roles: tuple[str, ...]) -> Grants:
    """Built-in roles only (principals without materialised grants)."""
    return materialise(roles)


def grants_of(principal: Principal) -> Grants:
    """The principal's **own** grants (its roles and group roles), without the anonymous grants a signed-in
    principal also receives (``effective_grants``)."""
    return principal.grants if principal.grants is not None else grants_for(principal.roles)


def repository_only(grants: Grants) -> Grants:
    """``grants`` without any global action: what anonymous access may hold (SPECIFICATION §4.9), also when a
    custom role it uses was later edited to hold one."""
    if not grants.is_admin and all(p.action not in GLOBAL_ACTIONS for p in grants.permissions):
        return grants
    return Grants(is_admin=False, permissions=tuple(p for p in grants.permissions if p.action not in GLOBAL_ACTIONS))


def anonymous_part(principal: Principal) -> Grants | None:
    """The anonymous grants an authenticated principal receives on top of its own (None: none, or the principal
    is the anonymous principal itself, whose own grants they are)."""
    if principal.is_anonymous or principal.anonymous_grants is None:
        return None
    return repository_only(principal.anonymous_grants)


def effective_grants(principal: Principal) -> Grants:
    """Own ∪ anonymous grants (what ``check`` decides from, before any credential narrowing)."""
    own = grants_of(principal)
    extra = anonymous_part(principal)
    if extra is None or not extra.permissions:
        return own
    return Grants(is_admin=own.is_admin, permissions=tuple(dict.fromkeys(own.permissions + extra.permissions)))


def global_actions(grants: Grants) -> frozenset[Action]:
    """The global actions ``grants`` hold (all of them for administrators)."""
    if grants.is_admin:
        return GLOBAL_ACTIONS
    return frozenset(p.action for p in grants.permissions if p.action in GLOBAL_ACTIONS and p.scope == '*')


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
    """The scopes ``principal`` may put on a new token: only what it may do itself (tokens only narrow; the
    anonymous grants count, as the token adds no access the principal does not already have)."""
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


def is_role(name: str, custom: RoleTable | None = None) -> bool:
    """A role that can be granted: a built-in one other than ``anonymous``, or a custom one."""
    if name == ANONYMOUS_ROLE:
        return False
    return name in BUILTIN_ROLES or (custom is not None and name in custom)


def may_assign(principal: Principal, grants: Grants) -> bool:
    """May ``principal`` give these permissions away (grant a role, edit a role to hold them)? ``admin`` needs an
    administrator; any other global action needs a giver holding it. Repository actions are what ``user-admin``
    hands out, so they are not limited."""
    held = global_actions(_owner_grants(principal))
    if grants.is_admin:
        return Action.ADMIN in held
    return global_actions(grants) <= held


def may_assign_role(principal: Principal, role: str, custom: RoleTable | None = None) -> bool:
    return may_assign(principal, materialise((role,), custom))


def may_manage(principal: Principal, target: Grants) -> bool:
    """May ``principal`` change or remove an account holding ``target`` (no stepping above one's own powers: a
    ``user-admin`` cannot reset an administrator's password)?"""
    return may_assign(principal, target)


def _owner_grants(principal: Principal) -> Grants:
    """What the principal holds itself, ignoring the narrowing of the credential used (decisions about delegation
    still go through ``check`` for the credential). Never the anonymous grants: they are not the principal's own to
    hand out (they hold no global action either, ``repository_only``)."""
    grants = grants_of(principal)
    if principal.token is None and principal.token_access is None:
        return grants
    allowed = {a for a in GLOBAL_ACTIONS if check(principal, a)}
    perms = tuple(p for p in grants.permissions if p.action not in GLOBAL_ACTIONS or p.action in allowed)
    return Grants(is_admin=grants.is_admin and Action.ADMIN in allowed, permissions=perms)


def permission_error(action: str, scope: str) -> str | None:
    """Why ``(action, scope)`` is not a valid role permission, or None."""
    try:
        parsed = Action(action)
    except ValueError:
        return f'unknown action {action!r}'
    if parsed in GLOBAL_ACTIONS:
        return None if scope == '*' else 'global actions are granted only on scope *'
    if scope == '*' or valid_repository_pattern(scope):
        return None
    return 'use *, a repository name, a glob (docker-*), format:<format> or type:<proxy|hosted|group>'


# ---------------------------------------------------------------------------- decisions
def check(principal: Principal, action: Action, repo: RepoConfig | None = None) -> bool:
    """May ``principal`` perform ``action`` (on ``repo``; without a repo: on at least one repository)?

    A principal authenticated by a Docker registry token is also limited to what the token grants, and one
    authenticated by an API token to the token's scopes and repository patterns (ARCH §7): tokens only narrow, and
    the narrowing is an upper bound on everything, the anonymous grants of a signed-in principal included."""
    if principal.token is not None and not token_permits(principal.token, action, repo):
        return False
    if principal.token_access is not None:
        if action in GLOBAL_ACTIONS or repo is None:
            return False
        granted = {a for name, actions in principal.token_access if name == repo.name for a in actions}
        if action not in granted:
            return False
    grants = grants_of(principal)
    if grants.is_admin or _allows(grants, action, repo):
        return True
    extra = anonymous_part(principal)
    return extra is not None and action not in GLOBAL_ACTIONS and _allows(extra, action, repo)


def _allows(grants: Grants, action: Action, repo: RepoConfig | None) -> bool:
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


def filter_query(
    principal: Principal, action: Action, repos: Iterable[RepoConfig], field: str = 'repo_id'
) -> dict[str, Any]:
    """A MongoDB filter limiting ``field`` (a repository id) to the repositories ``principal`` may ``action``:
    ``{}`` when that is every repository in ``repos``, else ``{field: {'$in': [...]}}`` (possibly empty)."""
    every = list(repos)
    allowed = filter_repos(principal, action, every)
    if len(allowed) == len(every):
        return {}
    return {field: {'$in': sorted(r.id for r in allowed)}}


class PermissionRequired(Forbidden):
    """A signed-in principal lacks a permission (problem code ``permission_required``; the UI shows a calm "no
    access" state for it). CSRF and pending sign-in steps keep their own codes."""

    code = 'permission_required'


def _denied(principal: Principal, detail: str) -> Exception:
    return Unauthorized('authentication required') if principal.is_anonymous else PermissionRequired(detail)


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
    if check(principal, Action.BROWSE) or check(principal, Action.READ):
        caps.append('repositories.browse')  # some repository may be visible: Repositories, Browse, Search
    if check(principal, Action.STATS_READ):
        caps.append('stats.read')
    if check(principal, Action.AUDIT_READ):
        caps.append('audit.read')
    if check(principal, Action.USER_ADMIN):
        caps.append('users.admin')
    if check(principal, Action.SETTINGS_ADMIN):
        caps.append('settings.admin')
    return caps


def method_action(method: str) -> Action:
    if method in ('GET', 'HEAD'):
        return Action.READ
    if method == 'DELETE':
        return Action.DELETE
    return Action.WRITE


FORMAT_ROUTE_PREFIX = '/repository/'


def hidden_repo(request: Request, principal: Principal) -> WaystationError:
    """The answer for a repository that is hidden from ``principal`` or does not exist (ADR 0005).

    On the format routes (``/repository/<repo>/...``) a request with no credentials at all (anonymous principal, no
    ``Authorization`` header) gets ``401`` (the error handler adds ``WWW-Authenticate: Basic``), so clients that
    only send credentials after a challenge retry with them. Everything else, including authenticated principals
    and requests whose credentials were not accepted as a principal, gets ``404``. Missing and hidden repositories
    get the same answer, so repository names cannot be enumerated.
    """
    if (
        principal.is_anonymous
        and 'authorization' not in request.headers
        and request.url.path.startswith(FORMAT_ROUTE_PREFIX)
    ):
        return Unauthorized('authentication required')
    return NotFound('Not Found')


async def authorize_repo(request: Request, repo: RepoConfig | None, action: Action) -> Principal:
    """Authenticate and authorise ``action`` on ``repo`` (format routes); ``repo`` is None when the requested
    repository does not exist (answered exactly like a hidden one, ``hidden_repo``)."""
    principal = await authenticate(request)
    if repo is None or not can_see(principal, repo):
        raise hidden_repo(request, principal)
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


PENDING_PASSWORD_ATTR = '__ws_password_change_allowed__'  # noqa: S105 - an attribute name


def password_change_allowed[T: Callable[..., object]](fn: T) -> T:
    """The route stays usable by a session whose user must change a one-time password first (``/auth/me``, the
    password change itself). Every other route answers 403 for such a session (SPECIFICATION §4.9)."""
    setattr(fn, PENDING_PASSWORD_ATTR, True)
    return fn


class PasswordChangeRequired(Forbidden):
    code = 'password_change_required'


PENDING_TOTP_ATTR = '__ws_totp_setup_allowed__'


def totp_setup_allowed[T: Callable[..., object]](fn: T) -> T:
    """The route stays usable by a session whose user must enrol a second factor first (``/auth/me``, the TOTP
    enrolment routes). Every other route answers 403 for such a session (SPECIFICATION §4.9)."""
    setattr(fn, PENDING_TOTP_ATTR, True)
    return fn


class TotpSetupRequired(Forbidden):
    code = 'totp_setup_required'


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
    if (
        principal.must_change_password
        and principal.method is AuthMethod.SESSION
        and not getattr(getattr(route, 'endpoint', None), PENDING_PASSWORD_ATTR, False)
    ):
        raise PasswordChangeRequired('change your one-time password first')
    if (
        principal.totp_setup_required
        and principal.method is AuthMethod.SESSION
        and not getattr(getattr(route, 'endpoint', None), PENDING_TOTP_ATTR, False)
    ):
        raise TotpSetupRequired('set up two-factor sign-in first')
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
